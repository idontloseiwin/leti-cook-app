"""Focused unwired self-FK effect protocol; native concurrency proof is separate."""
from contextlib import contextmanager
from pathlib import Path
import sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.quota import quota_retry_detachment as effect
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,_reviewed_statement
from test_operator_job_lifecycle import Database

PARENT='60000000-0000-4000-8000-000000000001'
CHILD='60000000-0000-4000-8000-000000000002'
CUTOFF='2026-01-02T03:04:05.123456+00:00'


def child(identity=CHILD,**updates):
    value=dict(id=identity,phase='private',endpoint='videos',quota_bucket=None,error_type=None)
    value.update(updates);return value


class Transaction(Database):
    def __init__(self,*answers,children=()):
        super().__init__(*answers);self.children=children;self.cursor_parameters=[]
    @contextmanager
    def quota_retry_children(self,*,parent_id):
        self.cursor_parameters.append(parent_id);self.events.append('cursor_open')
        try:yield iter(self.children)
        finally:self.events.append('cursor_closed')


class QuotaRetryDetachTests(unittest.TestCase):
    def invoke(self,tx):return effect.delete_expired_quota_attempt(tx,attempt_id=PARENT,cutoff=CUTOFF)
    def test_missing_or_rechecked_fresh_parent_stops_before_children(self):
        tx=Transaction(None)
        self.assertEqual(self.invoke(tx),0)
        self.assertEqual(tx.cursor_parameters,[]);self.assertEqual(len(tx.calls),1)
        self.assertEqual(tx.calls[0][2],(PARENT,CUTOFF))
        self.assertIn('started_at<%s::timestamptz',tx.calls[0][1])
        self.assertIn('FOR UPDATE',tx.calls[0][1]);self.assertNotIn('NOWAIT',tx.calls[0][1])
    def test_parent_fence_then_child_effect_then_same_parent_delete(self):
        tx=Transaction({'id':PARENT},1,1,children=[child()])
        self.assertEqual(self.invoke(tx),1)
        self.assertEqual([call[1] for call in tx.calls],[effect._PARENT,effect._DETACH,effect._DELETE])
        self.assertEqual(tx.calls[1][2],('other','videos','other',None,CHILD,PARENT))
        self.assertEqual(tx.calls[2][2],(PARENT,CUTOFF))
        self.assertEqual(tx.cursor_parameters,[PARENT]);self.assertEqual(tx.events,['borrow','cursor_open','cursor_closed'])
        self.assertEqual(tx.operations,[])
    def test_no_children_and_self_reference_delete_one_parent(self):
        tx=Transaction({'id':PARENT},1)
        self.assertEqual(self.invoke(tx),1)
        self.assertEqual([c[1] for c in tx.calls],[effect._PARENT,effect._DELETE])
        self.assertIn('request_attempt_id<>%s::uuid',effect._CHILDREN)
    def test_cursor_has_no_page_expiry_project_or_status_filter(self):
        self.assertIn('retry_of_request_attempt_id=%s::uuid',effect._CHILDREN)
        self.assertIn('FOR NO KEY UPDATE',effect._CHILDREN)
        for term in ('LIMIT','OFFSET','SKIP LOCKED','NOWAIT','started_at','project_id','status'):
            self.assertNotIn(term,effect._CHILDREN)
    def test_all_513_streamed_children_are_detached_without_total_cutoff(self):
        rows=(child(str(i)) for i in range(513))
        tx=Transaction({'id':PARENT},*([1]*514),children=rows)
        self.assertEqual(self.invoke(tx),1)
        self.assertEqual(sum(query==effect._DETACH for _,query,_ in tx.calls),513)
        self.assertEqual(len(tx.cursor_parameters),1)
    def test_safe_labels_remain_exact_and_only_five_child_columns_change(self):
        tx=Transaction({'id':PARENT},1,1,children=[child(phase='initial_inventory',quota_bucket='general',error_type='HttpError')])
        self.invoke(tx)
        self.assertEqual(tx.calls[1][2][:4],('initial_inventory','videos','general','HttpError'))
        for term in ('started_at','finished_at','quota_units','status','project_id','now(','gen_random_uuid'):
            self.assertNotIn(term,effect._DETACH)
    def test_malformed_parent_and_child_never_reach_parent_delete(self):
        for parent in ({},{'id':None},{'id':PARENT,'extra':1}):
            tx=Transaction(parent)
            with self.assertRaises(DatabaseError):self.invoke(tx)
            self.assertEqual(tx.cursor_parameters,[])
        for row in ({},child(PARENT),child(phase=2),dict(child(),extra=1)):
            tx=Transaction({'id':PARENT},children=[row])
            with self.assertRaises(DatabaseError):self.invoke(tx)
            self.assertEqual(len(tx.calls),1);self.assertIn('poison',tx.events)
    def test_zero_or_invalid_child_receipt_rolls_back_before_parent_delete(self):
        for count in (0,2,True,None):
            tx=Transaction({'id':PARENT},count,children=[child()])
            with self.assertRaises(DatabaseError):self.invoke(tx)
            self.assertEqual(len(tx.calls),2);self.assertIn('poison',tx.events)
    def test_locked_parent_delete_must_have_exact_single_row_receipt(self):
        for count in (0,2,True,None):
            tx=Transaction({'id':PARENT},count)
            with self.assertRaises(DatabaseError):self.invoke(tx)
            self.assertEqual(len(tx.calls),2);self.assertIn('poison',tx.events)
    def test_child_lock_error_poisoned_without_partial_delete_or_retry(self):
        failure=DatabaseServerError('database_operation_failed',code='55P03')
        tx=Transaction({'id':PARENT},1,failure,children=[child(),child('later')])
        with self.assertRaises(DatabaseServerError) as caught:self.invoke(tx)
        self.assertIs(caught.exception,failure);self.assertEqual(len(tx.calls),3)
        self.assertNotIn(effect._DELETE,[call[1] for call in tx.calls]);self.assertEqual(tx.cursor_parameters,[PARENT])
    def test_cursor_deadline_after_first_detach_closes_and_poisoned(self):
        failure=DatabaseServerError('database_operation_failed',code='57014')
        def rows():
            yield child()
            raise failure
        tx=Transaction({'id':PARENT},1,children=rows())
        with self.assertRaises(DatabaseServerError) as caught:self.invoke(tx)
        self.assertIs(caught.exception,failure);self.assertEqual(len(tx.calls),2)
        self.assertEqual(tx.events,['borrow','cursor_open','cursor_closed','poison'])
    def test_unknown_delete_ack_propagates_once_without_replay(self):
        failure=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        tx=Transaction({'id':PARENT},1,failure,children=[child()])
        with self.assertRaises(DatabaseUnavailable) as caught:self.invoke(tx)
        self.assertIs(caught.exception,failure);self.assertEqual(len(tx.calls),3)
        self.assertEqual(tx.cursor_parameters,[PARENT]);self.assertEqual(tx.operations,[])
    def test_statements_pass_real_guard_and_parameter_binding(self):
        for query,params,read in ((effect._PARENT,(PARENT,CUTOFF),True),(effect._CHILDREN,(PARENT,PARENT),True),
                                 (effect._DETACH,('other','videos','general',None,CHILD,PARENT),False),
                                 (effect._DELETE,(PARENT,CUTOFF),False)):
            _reviewed_statement(query,read=read,max_rows=1 if read else 0)
            PostgresQuery(Transformer()).convert(query,params)


if __name__=='__main__':unittest.main()
