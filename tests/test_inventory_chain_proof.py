"""Source-copy races and replay proof; bounded offline transaction doubles."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from db.jobs import operator_job_inventory_chain_proof as proof
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_operator_job_lifecycle import Database as Base
from test_operator_job_inventory_chains import JOB,LEASE,SCAN,TRANSITION,PLAYLIST_LEASE,CHANNEL,checkpoint,args

class Database(Base):
    def fetch_all(self,sql,parameters=(),*,max_rows):
        answer=self.answer('all',sql,parameters)
        if len(answer)>max_rows:raise AssertionError('unbounded fixture')
        return answer

def job(marked=True,source=False,**changes):
    summary={'full_inventory_sources':{}}
    if marked:summary['inventory_chain_version']=1
    if source:summary['full_inventory_sources']['long']={'scan_id':SCAN,'terminal_outcome':None}
    value=dict(id=JOB,channel_id=CHANNEL,requested_at='2026-10-10T00:00:00Z',
        initial_scope_version=1,phase='inventory',inventory_video_type='long',
        inventory_checkpoint={},inventory_scan_id=None,summary_json=summary)
    value.update(changes)
    return {'value':json.dumps(value)}

def copies(ack=None,cp=None,source=False):
    return [{'value':json.dumps(dict(channel_id=CHANNEL,video_type=kind,
        scan_id=SCAN if source and kind=='long' else None,terminal_outcome=None,
        checkpoint=(cp or {}) if kind=='long' else {},last_ack=(ack or {}) if kind=='long' else {}))}
        for kind in ('long','short','stream')]

def ack(outcome='page'):
    return dict(transition_id=TRANSITION,job_lease_token=LEASE,scan_id=SCAN,
        playlist_lease_token=PLAYLIST_LEASE,outcome=outcome,error_code=None)

class ProofTests(unittest.TestCase):
    def setUp(self):
        self.scope=patch.object(proof,'assert_full_initial_scope').start()
        self.hot=patch.object(proof,'_hot').start()
        self.addCleanup(patch.stopall)

    def test_scalar_initializer_locks_and_rereads_before_copy(self):
        original=job(False)
        tx=Database(original,[],[],[],original)
        got,children=proof.validate_inventory_chain_copies(tx,JOB,scalar=True,lock_sources=True)
        self.assertEqual([sql for _,sql,_ in tx.calls],
            [proof._JOB,proof._PLAYLIST_LOCKS,proof._SCAN_LOCKS,proof._RECEIPT_LOCKS,proof._JOB])
        self.assertEqual(len(children),3)
        self.assertEqual(self.scope.call_count,2)
        self.hot.assert_called_once_with(tx,got['requested_at'])
        self.assertFalse(any(kind=='execute' for kind,_,_ in tx.calls))

    def test_existing_children_lock_before_source_rows(self):
        original=job()
        tx=Database(original,copies(),[],[],[],original,copies())
        proof.validate_inventory_chain_copies(tx,JOB,lock_sources=True)
        self.assertEqual(tx.calls[1][1],proof._CHAINS+' FOR UPDATE')
        self.assertEqual(tx.calls[2][1],proof._PLAYLIST_LOCKS)

    def test_changed_copy_after_lock_is_not_accepted(self):
        old=job(False)
        new=job(False,source=True)
        tx=Database(old,[],[],[],new)
        with self.assertRaisesRegex(DatabaseError,'chain_state_invalid'):
            proof.validate_inventory_chain_copies(tx,JOB,scalar=True,lock_sources=True)
        self.hot.assert_not_called()

    def test_ack_missing_exact_receipt_fails_before_return(self):
        tx=Database(job(source=True),copies(ack(),source=True),{'valid':True},None,None)
        with self.assertRaisesRegex(DatabaseError,'outcome_unresolved'):
            proof.validate_inventory_chain_replay(tx,args('long',TRANSITION),ack=True)
        self.assertEqual(tx.events[-1],'poison')
        self.hot.assert_not_called()

    def test_unknown_ack_is_historical_only_and_does_not_require_live_parent_lease(self):
        tx=Database(job(source=True,phase='verify'),copies(ack('unknown'),source=True),
            {'valid':True},None,None)
        got=proof.validate_inventory_chain_replay(tx,args('long',TRANSITION),ack=True)
        self.assertEqual(got['outcome'],'unknown')
        self.assertTrue(all(kind in ('one','all') for kind,_,_ in tx.calls))
        self.assertNotIn('lease_expires_at',proof._JOB)
        self.assertNotIn("phase='inventory'",proof._JOB)

    def test_native_expired_scan_rejects_unknown_and_success_equally(self):
        for outcome in ('unknown','page'):
            tx=Database(job(source=True),copies(ack(outcome),source=True),{'valid':False})
            with self.subTest(outcome=outcome),self.assertRaisesRegex(DatabaseError,'receipt_expired'):
                proof.validate_inventory_chain_replay(tx,args('long',TRANSITION),ack=True)
            self.assertFalse(any(sql==proof._RECEIPT for _,sql,_ in tx.calls))

    def test_invalid_receipt_marker_or_identity_is_not_replayed(self):
        tx=Database(job(source=True),copies(ack(),source=True),{'valid':True},
            {'value':'{}'},{'valid':False})
        with self.assertRaisesRegex(DatabaseError,'receipt_expired'):
            proof.validate_inventory_chain_replay(tx,args('long',TRANSITION),ack=True)

    def test_exact_page_ack_reads_source_then_returns_only_saved_ack(self):
        tx=Database(job(source=True),copies(ack(),source=True),{'valid':True},
            {'value':'{}'},{'valid':True},None)
        got=proof.validate_inventory_chain_replay(tx,args('long',TRANSITION),ack=True)
        self.assertEqual(got,ack())
        self.hot.assert_called_once()
        self.assertEqual(tx.calls[-2][2]['outcome'],'page')

    def test_receipt_scope_conflict_fails_closed(self):
        tx=Database(job(source=True),copies(ack('unknown'),source=True),{'valid':True},
            {'value':'{}'},{'valid':True},{'value':'{}'},{'valid':True})
        with self.assertRaisesRegex(DatabaseError,'outcome_unresolved'):
            proof.validate_inventory_chain_replay(tx,args('long',TRANSITION),ack=True)

    def test_bound_replay_needs_hot_source_but_missing_receipt_does_not_grant_dispatch(self):
        tx=Database(job(source=True),copies(cp=checkpoint('bound'),source=True),
            {'valid':True},None,None)
        result=proof.validate_inventory_chain_replay(tx,args('long',TRANSITION,SCAN,PLAYLIST_LEASE),ack=False)
        self.assertIsNone(result)
        self.assertTrue(all(kind in ('one','all') for kind,_,_ in tx.calls))

    def test_stale_replay_identity_does_not_fall_through_to_fresh_work(self):
        tx=Database(job(source=True),copies(ack('unknown'),source=True),{'valid':True},None,None)
        with self.assertRaisesRegex(DatabaseError,'outcome_unresolved'):
            proof.validate_inventory_chain_replay(tx,args('long',JOB),ack=True)

    def test_three_row_shape_and_terminal_direction_fail_closed(self):
        tx=Database(job(),copies()[:2])
        with self.assertRaisesRegex(DatabaseError,'chain_state_invalid'):
            proof.validate_inventory_chain_copies(tx,JOB)
        source=json.loads(job(source=True)['value'])
        source['summary_json']['full_inventory_sources']['long']['terminal_outcome']='completed'
        tx=Database({'value':json.dumps(source)},copies(source=True))
        with self.assertRaisesRegex(DatabaseError,'chain_state_invalid'):
            proof.validate_inventory_chain_copies(tx,JOB)

    def test_sql_is_fixed_bounded_and_native_clocked(self):
        mapping=dict(scan=SCAN,kind='long',channel=CHANNEL,requested_at='2026-10-10T00:00:00Z',
            receipt='{}',playlist_lease=PLAYLIST_LEASE,outcome='page',transition=TRANSITION,error_code=None)
        cases=((proof._JOB,(JOB,),1),(proof._CHAINS,(JOB,),3),
            (proof._CHAINS+' FOR UPDATE',(JOB,),3),(proof._PLAYLIST_LOCKS,(CHANNEL,),3),
            (proof._SCAN_LOCKS,([SCAN],),6),(proof._RECEIPT_LOCKS,('[]',),12),
            (proof._SCAN,mapping,1),(proof._RECEIPT,('scan_transition',TRANSITION),1),
            (proof._RECEIPT_VALID,mapping,1))
        for sql,params,limit in cases:
            with self.subTest(sql=sql[:45]):
                PostgresQuery(Transformer()).convert(sql,params)
                _reviewed_statement(sql,read=True,max_rows=limit)
        for sql in (proof._SCAN,proof._RECEIPT_VALID):
            self.assertIn('clock_timestamp()',sql)
            self.assertIn('isfinite',sql)
        self.assertIn("r.result->>'lease_token'=%(playlist_lease)s::text",proof._RECEIPT_VALID)

if __name__=='__main__':unittest.main()
