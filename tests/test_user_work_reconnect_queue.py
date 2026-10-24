"""Atomic queue owner and in-process HTTP contract tests; no live PostgreSQL/API."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, _reviewed_statement
from db.user_work import user_work_reconnect as queue
from user_work_reconnect import reconnect_snapshot
from user_work_service import create_user_work_router
from user_work_backup import UserWorkError
from saved_work import SavedWorkError

ID='a0000000-0000-4000-8000-000000000001'
TOKEN='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
CHANNEL='UC'+'a'*22


def item(n=1,**changes):
    row=dict(id=str(UUID(int=n,version=4)),revision=1,is_hidden=False,reconnect_state='idle',
             reconnect_revision=None,user_input='https://www.youtube.com/@synthetic',parent_ref=None)
    row.update(changes)
    return row


class Database:
    def __init__(self,items=None):
        self.items=deepcopy(items or [])
        self.calls=[];self.operations=[];self.ends=[];self.locked=True;self.unknown_commit=False
        self.fence=dict(id=ID,revision=1,reconnect_revision=1,reconnect_state='running',is_hidden=False,lease_token=TOKEN,live=True)
        self.control=dict(initial_long_inventory_status='pending',is_hidden=False,owner_permission='accepted',control_epoch=9)
        self.effects=[]

    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation);before=deepcopy((self.items,self.effects))
        try:
            yield self
        except BaseException:
            self.items,self.effects=before;self.ends.append('rollback');raise
        else:
            self.ends.append('commit')
            if self.unknown_commit:
                raise DatabaseUnavailable('database_connection_failed',outcome_unknown=True)

    def fetch_one(self,statement,parameters=()):
        self.calls.append((statement,parameters))
        if statement==queue._LOCK:return {'locked':self.locked}
        if statement==queue._FENCE:return self.fence
        if statement==queue._CONTROL:return self.control
        raise AssertionError('unexpected SQL owner')

    def fetch_all(self,statement,parameters=(),*,max_rows):
        self.calls.append((statement,parameters))
        if statement in (queue._FACTS,queue._LOCKED_FACTS):return deepcopy(self.items)
        if statement==queue._CANDIDATES:return [{'id':row['id']} for row in self.items[:max_rows]]
        raise AssertionError('unexpected SQL owner')

    def returning_all(self,statement,parameters=(),*,max_rows):
        self.calls.append((statement,parameters))
        if statement==queue._CLAIM:return []
        if statement!=queue._ENQUEUE:raise AssertionError('unexpected SQL owner')
        changed=[]
        for row in self.items:
            if row['id'] in parameters[0] and row['reconnect_state']=='idle' and not row['is_hidden']:
                row['reconnect_state']='blocked' if row['user_input'] is None else 'queued'
                row['reconnect_revision']=row['revision']
                changed.append(deepcopy(row))
        return changed

    def returning_one(self,statement,parameters=()):
        self.calls.append((statement,parameters))
        if statement==queue._RETRY_SINGLE:
            row=next(row for row in self.items if row['id']==parameters[0])
            if row['user_input'] is None:return None
            row.update(revision=row['revision']+1,reconnect_revision=row['revision']+1,reconnect_state='queued')
            return deepcopy(row)
        return {'id':ID}


class QueueOwnerTests(unittest.TestCase):
    def test_fifty_imported_rows_enqueue_once_and_hidden_never_queue(self):
        db=Database([item(i,is_hidden=i==49,user_input=None if i==50 else 'saved') for i in range(1,51)])
        body={'expected_snapshot':reconnect_snapshot(db.items)}
        first=queue.enqueue_reconnect(db,body)
        second=queue.enqueue_reconnect(db,body)
        self.assertEqual((first['queued_count'],first['blocked_count'],first['excluded_hidden']),(48,1,1))
        self.assertEqual((second['queued_count'],second['already_requested_count']),(0,49))
        self.assertEqual(db.items[48]['reconnect_state'],'idle')
        self.assertEqual(sum(sql==queue._ENQUEUE for sql,_ in db.calls),1)

    def test_stale_set_and_unknown_ack_consumed_set_never_mutate(self):
        db=Database([item(1),item(2)])
        body={'expected_snapshot':reconnect_snapshot(db.items)}
        db.items.pop()
        with self.assertRaises(UserWorkError) as caught:queue.enqueue_reconnect(db,body)
        self.assertEqual(caught.exception.status,409)
        self.assertNotIn(queue._ENQUEUE,[sql for sql,_ in db.calls])
        self.assertEqual(db.ends,['rollback'])

    def test_single_refuses_hidden_and_revision_conflict(self):
        for changes,outcome in (({'is_hidden':True},'hidden'),({'revision':2},'conflict')):
            db=Database([item(id=ID,**changes)])
            self.assertEqual(queue.enqueue_reconnect(db,{'expected_revision':1},item_id=ID),{'outcome':outcome})
            self.assertNotIn(queue._ENQUEUE,[sql for sql,_ in db.calls])

    def test_key_correction_retry_advances_revision_and_double_click_conflicts(self):
        db=Database([item(id=ID,reconnect_state='blocked',reconnect_revision=1,reconnect_error_code='provider_key_rejected')])
        old={'expected_revision':1}
        self.assertEqual(queue.enqueue_reconnect(db,old,item_id=ID)['queued_count'],1)
        self.assertEqual(db.items[0]['revision'],2)
        self.assertEqual(queue.enqueue_reconnect(db,old,item_id=ID),{'outcome':'conflict'})
        self.assertEqual(sum(sql==queue._RETRY_SINGLE for sql,_ in db.calls),1)

    def test_retry_lost_ack_then_old_request_does_not_retry_again(self):
        db=Database([item(id=ID,reconnect_state='blocked',reconnect_revision=1)])
        db.unknown_commit=True
        with self.assertRaises(DatabaseUnavailable):queue.enqueue_reconnect(db,{'expected_revision':1},item_id=ID)
        db.unknown_commit=False
        self.assertEqual(queue.enqueue_reconnect(db,{'expected_revision':1},item_id=ID),{'outcome':'conflict'})
        self.assertEqual(db.items[0]['revision'],2)

    def test_bulk_preserves_blocked_and_single_preserves_active_wait_or_lease(self):
        for state in ('blocked','queued','waiting','running'):
            db=Database([item(id=ID,reconnect_state=state,reconnect_revision=1)])
            before=deepcopy(db.items)
            result=queue.enqueue_reconnect(db,{'expected_snapshot':reconnect_snapshot(db.items)})
            self.assertEqual(result['already_requested_count'],1);self.assertEqual(db.items,before)
            if state!='blocked':
                result=queue.enqueue_reconnect(db,{'expected_revision':1},item_id=ID)
                self.assertEqual(result['already_requested_count'],1);self.assertEqual(db.items,before)

    def test_claim_is_bounded_and_late_release_uses_revision_and_token(self):
        db=Database()
        self.assertEqual(queue.claim_reconnect(db,limit=3,lease_seconds=60),[])
        self.assertEqual(db.calls[-1],(queue._CLAIM,{'limit':3,'lease_seconds':60,'identity':None}))
        for bad in (0,51,True):
            with self.assertRaises(ValueError):queue.claim_reconnect(db,limit=bad)
        self.assertEqual(queue.defer_reconnect(db,ID,1,TOKEN,reason='channel_add_rate_limited',retry_seconds=60),{'outcome':'ready'})
        self.assertEqual(db.calls[-1][1][-4:],(ID,1,1,TOKEN))
        self.assertIn('reconnect_lease_expires_at>clock_timestamp()',queue._RELEASE)

    def test_candidates_read_ids_without_advisory_lock_or_lease(self):
        db=Database([item(i) for i in range(1,5)])
        before=deepcopy(db.items)
        self.assertEqual(queue.reconnect_candidates(db,limit=2),[{'id':row['id']} for row in before[:2]])
        self.assertEqual(db.items,before)
        self.assertTrue(db.operations[0].read_only)
        self.assertEqual(db.operations[0].total_timeout_ms,15000)
        self.assertEqual(db.calls,[(queue._CANDIDATES,{'limit':2})])
        for bad in (0,101,True,'16'):
            with self.assertRaises(ValueError):queue.reconnect_candidates(db,limit=bad)
        self.assertEqual(len(db.operations),1)

    def test_exact_candidate_claim_cannot_substitute_another_identity(self):
        db=Database()
        self.assertEqual(queue.claim_reconnect(db,identity=ID),[])
        self.assertEqual(db.calls[-1],(queue._CLAIM,{'limit':1,'lease_seconds':180,'identity':ID}))
        self.assertIn('candidate.id=%(identity)s::uuid',queue._CLAIM)
        for identity in ('not-a-uuid',True,'UC'+'a'*22):
            with self.assertRaises(SavedWorkError):queue.claim_reconnect(db,identity=identity)
        with self.assertRaises(ValueError):queue.claim_reconnect(db,identity=ID,limit=2)
        self.assertEqual(len(db.operations),1)
        # Candidate reads and claims share parent/clock/visibility predicates and fair order.
        self.assertIn(queue._ELIGIBLE,queue._CANDIDATES);self.assertIn(queue._ELIGIBLE,queue._CLAIM)
        self.assertIn(queue._FAIR_ORDER,queue._CANDIDATES);self.assertIn(queue._FAIR_ORDER,queue._CLAIM)

    def test_bind_fence_precedes_owner_and_rejects_expiry_edit_or_hidden(self):
        for update in ({'live':False},{'revision':2},{'reconnect_revision':2},{'is_hidden':True},{'lease_token':'different'}):
            db=Database();db.fence.update(update)
            with patch.object(queue,'bind_user_channel_restore_item') as bind:
                self.assertEqual(queue.bind_reconnect(db,ID,1,TOKEN,{'channel_id':CHANNEL}),{'outcome':'lease_lost'})
                bind.assert_not_called()

    def _bind_effect(self,db,**kwargs):
        self.assertIs(kwargs['transaction'],db)
        self.assertEqual(kwargs['reconnect_lease_token'],TOKEN)
        db.effects.append('channel_bound')
        return {'outcome':'ready','replayed':False}

    def test_bind_estimate_enqueue_share_one_transaction_and_ack_after_commit(self):
        db=Database()
        estimate=dict(estimated_items=20,estimated_pages=3,estimated_quota_units=4)
        with patch.object(queue,'bind_user_channel_restore_item',side_effect=self._bind_effect),\
             patch.object(queue,'estimate_channel_initial_inventory_job',return_value=estimate) as estimated,\
             patch.object(queue,'enqueue_channel_initial_inventory_job',return_value={'job':{'id':TOKEN}}) as enqueued:
            result=queue.bind_reconnect(db,ID,1,TOKEN,{'channel_id':CHANNEL})
        self.assertEqual(result['job_id'],TOKEN)
        self.assertIs(estimated.call_args.kwargs['transaction'],db)
        self.assertIs(enqueued.call_args.kwargs['transaction'],db)
        self.assertEqual(enqueued.call_args.kwargs['expected_epoch'],9)
        self.assertFalse(enqueued.call_args.kwargs['confirmed'])
        # Estimates remain observational; reconnect supplies no quota cap/reserve.
        self.assertEqual(enqueued.call_args.args[5:8], (None, None, None))
        self.assertEqual(len(db.operations),1);self.assertEqual(db.ends,['commit'])

    def test_enqueue_failure_rolls_back_bound_channel(self):
        db=Database()
        with patch.object(queue,'bind_user_channel_restore_item',side_effect=self._bind_effect),\
             patch.object(queue,'estimate_channel_initial_inventory_job',return_value=dict(estimated_items=20,estimated_pages=3,estimated_quota_units=4)),\
             patch.object(queue,'enqueue_channel_initial_inventory_job',side_effect=DatabaseServerError('operator_job_estimate_changed',code='P0001')):
            with self.assertRaises(DatabaseServerError):queue.bind_reconnect(db,ID,1,TOKEN,{'channel_id':CHANNEL})
        self.assertEqual(db.effects,[]);self.assertEqual(db.ends,['rollback'])

    def test_manual_and_merge_do_not_enqueue_hydration(self):
        for manual in (True,False):
            db=Database()
            if manual:db.control['owner_permission']='manual_required'
            with patch.object(queue,'bind_user_channel_restore_item',return_value={'outcome':'ready','merged':not manual}),\
                 patch.object(queue,'estimate_channel_initial_inventory_job') as estimate,\
                 patch.object(queue,'enqueue_channel_initial_inventory_job') as enqueue:
                result=queue.bind_reconnect(db,ID,1,TOKEN,{'channel_id':CHANNEL})
            estimate.assert_not_called();enqueue.assert_not_called()
            self.assertEqual(result['manual_required'],manual)

    def test_unknown_commit_never_repeats_bind(self):
        db=Database();db.unknown_commit=True;db.control['owner_permission']='manual_required'
        with patch.object(queue,'bind_user_channel_restore_item',side_effect=self._bind_effect) as bind:
            with self.assertRaises(DatabaseUnavailable) as caught:queue.bind_reconnect(db,ID,1,TOKEN,{'channel_id':CHANNEL})
        self.assertTrue(caught.exception.outcome_unknown);bind.assert_called_once()
        self.assertEqual(db.effects,['channel_bound'])

    def test_read_sql_and_driver_placeholders_are_valid_without_db(self):
        for statement,params,read in ((queue._FACTS,(),True),(queue._LOCKED_FACTS,(),True),(queue._LOCK,(),True),
            (queue._ENQUEUE,([ID],),False),(queue._RETRY_SINGLE,(ID,1),False),(queue._CLAIM,{'limit':2,'lease_seconds':180,'identity':None},False),(queue._CLAIM,{'limit':1,'lease_seconds':180,'identity':ID},False),(queue._CANDIDATES,{'limit':16},True),(queue._FENCE,(ID,),False),
            (queue._RELEASE,('waiting',60,60,'busy',ID,1,1,TOKEN),False),(queue._CONTROL,(CHANNEL,ID),True)):
            with self.subTest(statement=statement[:30]):
                _reviewed_statement(statement,read=True,max_rows=10001,returning=statement.startswith('UPDATE'))
                PostgresQuery(Transformer()).convert(statement,params)


class QueueHttpTests(unittest.TestCase):
    def setUp(self):
        self.repo=Mock();self.provider=Mock(side_effect=AssertionError('provider called'));self.wake=Mock()
        self.repo.enqueue_reconnect.return_value=dict(outcome='ready',snapshot='reconnect-v1:'+'a'*64,
            queued_count=1,blocked_count=0,already_requested_count=0,excluded_hidden=0)
        app=FastAPI();app.include_router(create_user_work_router(self.repo,resolve_channel=self.provider,on_reconnect=self.wake))
        self.client=TestClient(app);self.addCleanup(self.client.close)
        self.headers={'x-saved-work':'1'}

    def test_single_and_bulk_acknowledge_durable_queue_with_zero_provider_calls(self):
        for path,body in ((f'/user-work/pending/{ID}/resolve',{'expected_revision':1}),
            ('/user-work/pending/reconnect',{'expected_snapshot':'reconnect-v1:'+'a'*64})):
            response=self.client.post(path,json=body,headers=self.headers)
            self.assertEqual(response.status_code,202,response.text)
            self.assertEqual(response.json()['status'],'queued')
            self.assertEqual(response.headers['cache-control'],'no-store')
        self.provider.assert_not_called();self.assertEqual(self.wake.call_count,2)

    def test_unknown_ack_and_unavailable_have_structured_503_no_retry(self):
        for unknown in (False,True):
            self.repo.enqueue_reconnect.side_effect=DatabaseUnavailable('database_connection_failed',outcome_unknown=unknown)
            response=self.client.post(f'/user-work/pending/{ID}/resolve',json={'expected_revision':1},headers=self.headers)
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json(),{'code':'database_write_outcome_unknown' if unknown else 'database_unavailable','outcome_unknown':unknown})
            self.assertNotIn('retry-after',response.headers)
        self.assertEqual(self.repo.enqueue_reconnect.call_count,2);self.wake.assert_not_called();self.provider.assert_not_called()

    def test_stale_snapshot_is_conflict_and_failed_wake_does_not_undo_ack(self):
        self.repo.enqueue_reconnect.side_effect=UserWorkError('conflict',409)
        body={'expected_snapshot':'reconnect-v1:'+'a'*64}
        self.assertEqual(self.client.post('/user-work/pending/reconnect',json=body,headers=self.headers).status_code,409)
        self.wake.assert_not_called()
        self.repo.enqueue_reconnect.side_effect=None;self.wake.side_effect=RuntimeError('lost hint')
        self.assertEqual(self.client.post('/user-work/pending/reconnect',json=body,headers=self.headers).status_code,202)

    def test_empty_import_never_enqueues_or_calls_provider(self):
        self.repo.import_document.return_value={'outcome':'ready'}
        document={'format':'leti-cook-user-work','version':2,'created_at':'2026-09-20T00:00:00+00:00',
            'user_work':{'folders':[],'labels':[],'channels':[],'collections':[],'reviews':[]},
            'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]}}
        response=self.client.post('/user-work/restore',json=document,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['pending_count'],0)
        self.repo.enqueue_reconnect.assert_not_called();self.provider.assert_not_called();self.wake.assert_not_called()

    def test_status_database_unavailable_can_be_retried_without_writing(self):
        self.repo.status.side_effect=DatabaseUnavailable('database_connection_failed')
        response=self.client.get('/user-work/status')
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.headers['retry-after'],'5')
        self.assertFalse(response.json()['outcome_unknown'])
        self.repo.enqueue_reconnect.assert_not_called()

    def test_mutation_guard_refuses_before_enqueue(self):
        response=self.client.post(f'/user-work/pending/{ID}/resolve',json={'expected_revision':1})
        self.assertEqual(response.status_code,403);self.repo.enqueue_reconnect.assert_not_called()


if __name__=='__main__':unittest.main()
