"""Offline transaction ordering and boundary tests for prepared addressed claims."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_claims as claims
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement

IDS=['aaaaaaaaaaa','bbbbbbbbbbb']
RUN=UUID('ca000000-0000-4000-8000-000000000001')
SCOPES=[{'channel_id':'UC0000000000000000000001','control_epoch':17}]


def prepared(**changes):
    value=dict(video_ids=IDS,worker_id=' worker ',worker='worker',run_id=RUN,scopes=SCOPES,lease_seconds=300,id_count=2)
    value.update(changes);return value


class Database:
    def __init__(self,*results):self.results=deque(results);self.calls=[];self.events=[];self.operations=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        try:yield self
        except BaseException:self.events.append('rollback');raise
        else:self.events.append('commit')
    @contextmanager
    def borrow(self):
        self.events.append('borrow')
        try:yield self
        except BaseException:self.events.append('failed');raise
    def answer(self,kind,query,params,cap=None):
        self.calls.append((kind,query,params,cap))
        result=self.results.popleft()
        if isinstance(result,BaseException):raise result
        return result
    def fetch_one(self,query,params=()):return self.answer('one',query,params,1)
    def fetch_all(self,query,params=(),*,max_rows):return self.answer('all',query,params,max_rows)
    def returning_all(self,query,params=(),*,max_rows):return self.answer('returning',query,params,max_rows)


class AddressedClaimTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(claims,'assert_channel_scopes');self.fence=self.patch.start();self.addCleanup(self.patch.stop)
    def invoke(self,db,**kw):
        return claims.claim_video_collection_ids(db,IDS,' worker ',str(RUN),SCOPES,**kw)
    def test_ordered_child_locks_then_atomic_update_then_full_final_fence(self):
        db=Database(prepared(),[],[],{'matches':True},[{'position':2,'value':{'video_id':IDS[1]}},{'position':1,'value':{'video_id':IDS[0]}}])
        self.assertEqual(self.invoke(db),[{'video_id':IDS[0]},{'video_id':IDS[1]}])
        self.assertEqual([row[1] for row in db.calls],[claims._PREPARE,claims._LOCK_QUEUE,claims._LOCK_INVENTORY,claims._IDENTITY_MATCH,claims._CLAIM_DUE])
        self.assertEqual([row[3] for row in db.calls if row[0] in ('all','returning')],[1000,1000,1000])
        self.fence.assert_called_once_with(db,SCOPES);self.assertEqual(db.events,['commit'])
        self.assertEqual(db.operations[0].name,'claim_video_collection_ids')
        self.assertEqual(db.operations[0].total_timeout_ms,60000)
    def test_metadata_uses_same_protocol_with_fixed_alternative_predicate(self):
        db=Database(prepared(),[],[],{'matches':True},[])
        self.assertEqual(claims.claim_video_metadata_collection_ids(db,IDS,'worker',str(RUN),SCOPES),[])
        self.assertEqual(db.calls[-1][1],claims._CLAIM_METADATA)
        self.fence.assert_called_once_with(db,SCOPES)
    def test_empty_ids_fence_but_skip_domain_owner_and_lease_checks(self):
        db=Database(prepared(video_ids=[],id_count=0,scopes=[],worker=None,worker_id=None,run_id=None,lease_seconds=None),[],[],{'matches':True})
        self.assertEqual(claims.claim_video_collection_ids(db,[],None,None,[],None),[])
        self.assertEqual(len(db.calls),4);self.fence.assert_called_once_with(db,[])
    def test_native_dispatch_error_precedes_even_empty_short_circuit(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:claims.claim_video_collection_ids(db,[],None,'bad',[],None)
        self.assertIs(caught.exception,error);self.assertEqual(len(db.calls),1)
    def test_id_bound_rejection_precedes_scope_shape(self):
        for count in (None,1001):
            db=Database(prepared(id_count=count,scopes={}))
            with self.assertRaisesRegex(DatabaseServerError,'database_operation_failed'):self.invoke(db)
            self.assertEqual(len(db.calls),1)
    def test_scope_shape_and_bound_fail_before_child_locks(self):
        for scopes in (None,{},[None]*1001):
            db=Database(prepared(scopes=scopes))
            with self.assertRaisesRegex(DatabaseServerError,'channel_collection_scope_invalid'):self.invoke(db)
            self.assertEqual(len(db.calls),1)
    def test_identity_mismatch_precedes_invalid_worker_and_mutation(self):
        db=Database(prepared(worker=''),[],[],{'matches':False})
        with self.assertRaisesRegex(DatabaseServerError,'channel_collection_scope_invalid'):self.invoke(db)
        self.assertEqual(len(db.calls),4);self.fence.assert_not_called();self.assertEqual(db.events,['rollback'])
    def test_owner_lease_validation_occurs_after_identity_locks(self):
        for change in ({'worker':''},{'run_id':None},{'lease_seconds':None},{'lease_seconds':29},{'lease_seconds':3601}):
            db=Database(prepared(**change),[],[],{'matches':True})
            with self.assertRaisesRegex(DatabaseServerError,'database_operation_failed'):self.invoke(db)
            self.assertEqual(len(db.calls),4);self.assertEqual(db.events,['rollback'])
    def test_zero_claims_still_validate_all_captured_scopes(self):
        db=Database(prepared(),[],[],{'matches':True},[])
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):self.invoke(db)
        self.assertEqual(db.events,['rollback'])
    def test_final_fence_failure_rolls_back_claim_and_borrowed_failures_poison_owner(self):
        db=Database(prepared(),[],[],{'matches':True},[{'position':1,'value':{'video_id':IDS[0]}}])
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(DatabaseError):self.invoke(None,transaction=db)
        self.assertEqual(db.events,['borrow','failed']);self.assertEqual(db.operations,[])
    def test_ambiguous_ack_is_not_replayed(self):
        error=DatabaseError('database_connection_failed',outcome_unknown=True)
        db=Database(prepared(),[],[],{'matches':True},error)
        with self.assertRaises(DatabaseError) as caught:self.invoke(db)
        self.assertIs(caught.exception,error);self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len([c for c in db.calls if c[0]=='returning']),1)
    def test_parameterized_queries_pass_driver_and_native_placeholder_grammar(self):
        inputs={key:prepared()[key] for key in ('video_ids','worker_id','run_id','scopes','lease_seconds')}
        inputs['scopes']=Jsonb(inputs['scopes'])
        for query,params in ((claims._PREPARE,(IDS,'worker',RUN,None,300)),(claims._LOCK_QUEUE,(IDS,)),(claims._LOCK_INVENTORY,(IDS,)),(claims._IDENTITY_MATCH,inputs)):
            _reviewed_statement(query,read=True,max_rows=1000);PostgresQuery(Transformer()).convert(query,params)
        for query in (claims._CLAIM_DUE,claims._CLAIM_METADATA):
            _reviewed_statement(query,read=True,max_rows=1000,returning=True);PostgresQuery(Transformer()).convert(query,inputs)
            self.assertIn('FOR UPDATE OF state SKIP LOCKED',query)
            self.assertIn('state.last_claim_run_id IS DISTINCT FROM',query)
            self.assertNotIn('public.claim_',query)
        self.assertNotIn('SKIP LOCKED',claims._LOCK_QUEUE)
        self.assertNotIn('SKIP LOCKED',claims._LOCK_INVENTORY)
        self.assertIn('state.next_due_at<=now()',claims._CLAIM_DUE)
        self.assertNotIn('state.next_due_at<=now()',claims._CLAIM_METADATA)
        self.assertIn('coalesce(state.next_due_at,now())',claims._CLAIM_METADATA)



if __name__=='__main__':unittest.main()
