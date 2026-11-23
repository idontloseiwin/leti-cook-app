"""Offline contract tests for the prepared pending-channel bind closure."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import sys
import json
import unittest
from uuid import UUID
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.user_work import user_work_restore_bind as binding
from db.channel import channel_workflow_inserts as inserts
from db.channel import channel_initial_error_fields as initial_error
from db.base.postgres import DatabaseServerError

ID=UUID('a0000000-0000-4000-8000-000000000001')
OWNER=UUID('a0000000-0000-4000-8000-000000000002')
FOLDER=UUID('f0000000-0000-4000-8000-000000000001')
CHANNEL='UC'+'a'*22
PARENT='UC'+'b'*22
STAMP=datetime(2026,1,1,tzinfo=timezone.utc)


class Transaction:
    def __init__(self,*results):self.results=deque(results);self.calls=[];self.savepoints=[];self.update_count=1
    def fetch_one(self,sql,params=()):
        self.calls.append(('read',sql,tuple(params)));value=self.results.popleft()
        if isinstance(value,BaseException):raise value
        return value
    def execute(self,sql,params=()):
        self.calls.append(('write',sql,tuple(params)))
        return self.update_count if sql.startswith('UPDATE public.channel_collection_state') else 1
    def insert_channel_with_effects(self,family,parameters):
        self.calls.append(('insert_effects',family,parameters))
        return {'channel_id':CHANNEL}
    @contextmanager
    def savepoint(self):
        self.savepoints.append('begin')
        try:yield self
        except BaseException:self.savepoints.append('rollback');raise
        else:self.savepoints.append('release')
    @contextmanager
    def borrow(self):
        self.calls.append(('borrow','begin',()))
        try:yield self
        except BaseException:self.calls.append(('borrow','failed',()));raise


class Database:
    def __init__(self,*results):self.tx=Transaction(*results);self.operations=[];self.ends=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        try:yield self.tx
        except BaseException:self.ends.append('rollback');raise
        else:self.ends.append('commit')


def args(**updates):
    value=dict(id=ID,revision=1,channel_id=CHANNEL);value.update(updates);return value


def item(**updates):
    value=dict(id=ID,revision=1,user_input='https://www.youtube.com/@synthetic',parent_ref=None,
        folder_id=None,label_keys=[],saved_views=[],is_hidden=False,hidden_at=None,added_at=STAMP)
    value.update(updates);return value


def tracked(**updates):
    value=dict(user_work_id=OWNER,folder_id=None,parent_channel_id=None);value.update(updates);return value


class BindTests(unittest.TestCase):
    def invoke(self,*rows,**options):
        self.db=Database(*rows)
        return binding.bind_user_channel_restore_item(self.db,identity=ID,expected_revision=1,channel_id=CHANNEL,**options)

    def new_rows(self,record=None):
        return [args(),{'locked':True},record or item(),None,{'kind':'object','bytes':100},{'invalid':False},
            {'value':STAMP},{'invalid':False},{'invalid':False},{'invalid':False},{'invalid':False},
            {'present':False},{'fresh':True},{'present':False},
            {'matches':True},{'columns_text':self.columns(record),'estimate_text':None},
            {'proposed_text':self.columns(record),'completed':False,'errored':False,'was_error':False,'previous_count':0},
            {'id':ID,'owner':ID},{'present':True}]

    def columns(self,record=None):
        record=record or item()
        native={name:None for name,_ in inserts.COLUMN_TYPES}
        native.update(channel_id=CHANNEL,user_work_id=str(ID),user_input=record['user_input'],
            is_hidden=record['is_hidden'],hidden_at=str(record['hidden_at']) if record['hidden_at'] else None,
            added_at=str(record['added_at']),initial_long_inventory_status='pending',initial_long_inventory_error_count=0)
        return json.dumps(native)

    def test_queued_bind_requires_the_live_authorized_token(self):
        record=item(reconnect_state='running',reconnect_revision=1,
                    reconnect_lease_token='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',reconnect_lease_live=True)
        self.assertEqual(self.invoke(args(),{'locked':True},record),{'outcome':'busy'})
        self.assertEqual(self.invoke(args(),{'locked':True},record,
            reconnect_lease_token='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),{'outcome':'lease_lost'})
        self.assertFalse(any(mode=='write' for mode,_,_ in self.db.tx.calls))

    def test_invalid_identity_revision_and_channel_rejected_before_lock(self):
        for row in (args(id=None),args(revision=0),args(revision=None),args(channel_id='invalid')):
            with self.subTest(row=row),self.assertRaises(DatabaseServerError) as caught:self.invoke(row)
            self.assertEqual(caught.exception.message,'user_work_bind_request_invalid')
            self.assertEqual(len(self.db.tx.calls),1);self.assertEqual(self.db.tx.savepoints,['begin','rollback'])

    def test_no_extra_upper_revision_limit(self):
        self.assertEqual(self.invoke(args(revision=9007199254740991),{'locked':False}),{'outcome':'busy'})

    def test_replay_and_not_found_have_exact_shapes(self):
        self.assertEqual(self.invoke(args(),{'locked':True},None,{'present':True}),dict(outcome='ready',id=str(ID),replayed=True))
        self.assertEqual(self.invoke(args(),{'locked':True},None,{'present':False}),{'outcome':'not_found'})

    def test_revision_input_parent_wait_exit_before_provider_data(self):
        vectors=[([item(revision=2)],'conflict'),([item(user_input=None)],'needs_input'),
            ([item(parent_ref=OWNER),None],'parent_pending')]
        for rows,outcome in vectors:
            self.assertEqual(self.invoke(args(),{'locked':True},*rows,channel_data={'never':'used'}),{'outcome':outcome})
            self.assertFalse(any(mode=='write' for mode,_,_ in self.db.tx.calls))
        self.assertIn('FOR SHARE NOWAIT',self.db.tx.calls[-1][1])

    def test_pending_user_owned_clocks_are_text_before_any_cas_or_input_outcome(self):
        record=item(user_input=None,added_at='10000-01-01T00:00:00+00:00',hidden_at='0001-01-01T00:00:00+00:00 BC')
        self.assertEqual(self.invoke(args(),{'locked':True},record),{'outcome':'needs_input'})
        query=self.db.tx.calls[-1][1]
        self.assertIn("to_jsonb(hidden_at)#>>'{}' AS hidden_at",query)
        self.assertIn("to_jsonb(added_at)#>>'{}' AS added_at",query)

    def test_existing_channel_merges_ownership_without_provider_or_visibility_update(self):
        result=self.invoke(args(),{'locked':True},item(),tracked(),{'id':ID,'owner':OWNER},{'present':True},channel_data={'invalid':True})
        self.assertEqual(result,dict(outcome='ready',id=str(ID),replayed=False,merged=True))
        writes=[(sql,params) for mode,sql,params in self.db.tx.calls if mode=='write']
        self.assertEqual(len(writes),5)
        self.assertIn('ON CONFLICT DO NOTHING',writes[0][0]);self.assertIn('revision=revision+1',writes[1][0])
        self.assertEqual(writes[2][1],(OWNER,ID));self.assertEqual(writes[3][1],(OWNER,ID))
        self.assertTrue(writes[-1][0].startswith('DELETE FROM public.user_channel_restore_items'))
        self.assertFalse(any('channel_collection_state' in sql or 'jsonb_typeof' in sql for _,sql,_ in self.db.tx.calls))
        self.assertFalse(any('public.merge_restored_' in sql or 'public.adopt_restored_' in sql for _,sql,_ in self.db.tx.calls))

    def test_existing_parent_cycle_is_dropped_but_folder_fills_if_absent(self):
        rows=[args(),{'locked':True},item(parent_ref=OWNER,folder_id=FOLDER),{'channel_id':PARENT},tracked(),{'cycle':True},
              {'id':ID,'owner':OWNER},{'present':True}]
        self.invoke(*rows)
        write=next(row for row in self.db.tx.calls if row[0]=='write')
        self.assertTrue(write[1].startswith('UPDATE public.channels'))
        self.assertEqual(write[2],(None,FOLDER,None,None,CHANNEL))
        self.assertNotIn('is_hidden',write[1])

    def test_existing_view_check_is_sequential_and_tombstone_is_skipped(self):
        views=[{'id':'already-present'},{'id':'new'}]
        self.invoke(args(),{'locked':True},item(saved_views=views),tracked(),{'present':True},{'present':False},
                    {'id':ID,'owner':OWNER},{'present':True})
        inserts=[row for row in self.db.tx.calls if row[1]==binding._VIEW_INSERT]
        self.assertEqual(len(inserts),1);self.assertEqual(inserts[0][2][1].obj,views[1])
        checks=[sql for _,sql,_ in self.db.tx.calls if "AND id=(%s::jsonb->>'id')::uuid" in sql]
        self.assertEqual(len(checks),2);self.assertFalse(any('deleted_at' in sql for sql in checks))

    def test_new_channel_inserts_then_adopts_then_deletes_pending_then_fences_control(self):
        record=item(is_hidden=True,hidden_at=STAMP,label_keys=['monetized'],saved_views=[{'id':'view'}])
        with patch.object(binding,'insert_restored_channel',wraps=binding.insert_restored_channel) as insert:
            self.assertEqual(self.invoke(*self.new_rows(record),channel_data={'synthetic':True}),dict(outcome='ready',id=str(ID),replayed=False))
            insert.assert_called_once()
            self.assertIs(insert.call_args.args[0],self.db.tx)
            self.assertEqual(len(insert.call_args.args[1]),11)
        writes=[row for row in self.db.tx.calls if row[0]=='write']
        proposal=next(row for row in self.db.tx.calls if row[1]==inserts._RESTORE_PROPOSAL)
        self.assertEqual(proposal[2][5:9],(True,True,STAMP,STAMP))
        effect=next(row for row in self.db.tx.calls if row[0]=='insert_effects')
        self.assertEqual(effect,('insert_effects','create',(None,self.columns(record))))
        statements=[row[1] for row in self.db.tx.calls]
        self.assertLess(statements.index(inserts._SCHEMA_PIN),statements.index(inserts._RESTORE_PROPOSAL))
        self.assertLess(statements.index(initial_error._FACTS),statements.index('create'))
        self.assertLess(statements.index('create'),self.db.tx.calls.index(writes[0]))
        self.assertEqual(len(writes),6)
        self.assertEqual(writes[1][1],binding._VIEW_INSERT)
        self.assertIn('user_channel_ref',writes[2][1]);self.assertIn('video_type_review_cases',writes[3][1])
        self.assertIn('DELETE FROM public.user_channel_restore_items',writes[4][1])
        self.assertIn("owner_permission='legacy_hold'",writes[5][1]);self.assertIn('nextval',writes[5][1])
        self.assertEqual(writes[5][2][1:4],(True,True,STAMP))
        self.assertEqual(len(self.db.operations),1);self.assertEqual(self.db.operations[0].total_timeout_ms,60000)
        self.assertEqual(self.db.tx.savepoints,['begin','release']);self.assertFalse(self.db.tx.results)

    def test_unavailable_shape_size_and_clock_validation(self):
        for info in ({'kind':None,'bytes':None},{'kind':'array','bytes':2},{'kind':'object','bytes':131073}):
            self.assertEqual(self.invoke(args(),{'locked':True},item(),None,info),{'outcome':'channel_unavailable'})
        base=self.new_rows()
        for index in (5,7,8,9,10):
            rows=base[:index]+[{'invalid':True}]
            with self.assertRaises(DatabaseServerError) as caught:self.invoke(*rows,channel_data={})
            self.assertEqual(caught.exception.message,'user_work_channel_data_invalid')
            self.assertFalse(any(mode=='write' for mode,_,_ in self.db.tx.calls))

    def test_native_wide_source_clock_remains_text_until_sql_domain_validation(self):
        for stamp in ('infinity','-infinity','10000-01-01T00:00:00+00:00'):
            rows=self.new_rows()[:6]+[{'value':stamp},{'invalid':True}]
            with self.assertRaises(DatabaseServerError) as caught:self.invoke(*rows,channel_data={})
            self.assertEqual(caught.exception.message,'user_work_channel_data_invalid')
            self.assertEqual(caught.exception.code,'P0001')
            self.assertIn("to_jsonb((%s::jsonb->>'channel_metadata_checked_at')::timestamptz)#>>'{}'",self.db.tx.calls[-2][1])
            self.assertEqual(self.db.tx.calls[-1][2][1],stamp)

    def test_native_timestamp_and_counter_errors_keep_sqlstate(self):
        for index,code in ((6,'22007'),(7,'22008'),(8,'22P02'),(9,'22003')):
            with self.assertRaises(DatabaseServerError) as caught:
                self.invoke(*(self.new_rows()[:index]+[DatabaseServerError('database_operation_failed',code=code)]),channel_data={})
            self.assertEqual(caught.exception.code,code);self.assertEqual(self.db.ends,['rollback'])

    def test_new_preflight_conflict_or_freshness_exit_without_writes(self):
        for index,row,outcome in ((11,{'present':True},'conflict'),(12,{'fresh':False},'channel_unavailable'),(13,{'present':True},'conflict')):
            self.assertEqual(self.invoke(*(self.new_rows()[:index]+[row]),channel_data={}),{'outcome':outcome})
            self.assertFalse(any(mode=='write' for mode,_,_ in self.db.tx.calls))

    def test_lost_final_control_fence_rolls_back_all_work(self):
        db=Database(*self.new_rows());db.tx.update_count=0
        with self.assertRaises(DatabaseServerError) as caught:
            binding.bind_user_channel_restore_item(db,identity=ID,expected_revision=1,channel_id=CHANNEL,channel_data={})
        self.assertEqual(caught.exception.message,'channel_collection_scope_lost');self.assertEqual(db.tx.savepoints,['begin','rollback'])
        self.assertEqual(db.ends,['rollback'])

    def test_nowait_failure_returns_busy_only_after_savepoint_rollback(self):
        error=DatabaseServerError('database_operation_failed',code='55P03')
        self.assertEqual(self.invoke(args(),{'locked':True},error),{'outcome':'busy'})
        self.assertEqual(self.db.tx.savepoints,['begin','rollback']);self.assertEqual(self.db.ends,['commit'])

    def test_sql_keeps_native_jsonb_clock_and_counter_boundaries(self):
        self.assertIn("IS DISTINCT FROM 'false'::jsonb",binding._DATA_SHAPE)
        self.assertIn("IS DISTINCT FROM '0'::jsonb",binding._DATA_SHAPE)
        self.assertIn("interval '5 minutes'",binding._DATA_CLOCKS)
        self.assertIn("interval '504 hours'",binding._DATA_CLOCKS)
        self.assertIn("coalesce(%s::timestamptz,clock_timestamp())",binding._CHANNEL_INSERT)


class OwnershipTests(unittest.TestCase):
    def test_adopt_requires_restored_channel_but_merge_accepts_tracked_channel(self):
        for adopt in (False,True):
            db=Database({'id':ID,'owner':ID if adopt else OWNER},{'present':True})
            if adopt:binding.adopt_restored_channel_user_work(db,ID)
            else:binding.merge_restored_channel_user_work(db,ID,OWNER)
            self.assertEqual(db.tx.calls[1][2][1],adopt)
            writes=[row for row in db.tx.calls if row[0]=='write'];self.assertEqual(len(writes),2)
            self.assertIn('restore_owner_ref=NULL',writes[0][1]);self.assertIn('restore_owner_ref=NULL',writes[1][1])
            self.assertEqual(db.ends,['commit'])

    def test_invalid_owner_domain_error_poison_borrowed_transaction(self):
        tx=Transaction({'id':ID,'owner':OWNER},{'present':False})
        with self.assertRaises(DatabaseServerError) as caught:binding.merge_restored_channel_user_work(None,ID,OWNER,transaction=tx)
        self.assertEqual(caught.exception.message,'user_work_adopt_invalid')
        self.assertEqual(tx.calls[0],('borrow','begin',()));self.assertEqual(tx.calls[-1],('borrow','failed',()))
        self.assertFalse(any(mode=='write' for mode,_,_ in tx.calls))

    def test_null_or_native_invalid_identity_does_not_write(self):
        for response in ({'id':None,'owner':ID},DatabaseServerError('database_operation_failed',code='22P02')):
            db=Database(response)
            with self.assertRaises(DatabaseServerError):binding.adopt_restored_channel_user_work(db,None)
            self.assertFalse(any(mode=='write' for mode,_,_ in db.tx.calls));self.assertEqual(db.ends,['rollback'])


if __name__=='__main__':unittest.main()
