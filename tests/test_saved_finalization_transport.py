"""Finite Saved finalization keeps the existing transaction and failure rules."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.base import postgres as pg
from db.user_work.saved_mutations import _FINALIZE_CHANGED_MEMBERS, _FINALIZE_CHANGED_MEMBERS_KEYS
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN

VALUES = dict(collection_id='c0000000-0000-4000-8000-000000000001',
    request_id='d0000000-0000-4000-8000-000000000001',fingerprint='a'*64,
    action='add',requested_count=1,changed_count=1,delta=1,changed_at='2026-10-06T00:00:00Z')

class SavedFinalizationTransportTests(unittest.TestCase):
    def setUp(self):
        self.trace=[]
        self.connection=Connection(self.trace)
        self.pool=Pool(self.connection,self.trace)
        self.admissions=[]
        self.db=pg.PostgresDatabase(pg.PostgresConfig(DSN),
            admission=lambda name:self.admissions.append(name),
            pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.db.close)

    def test_single_fixed_statement_and_bound_arguments_under_existing_owner(self):
        self.connection.results.append(Result(rows=[{'object':{'revision':3}}]))
        with self.db.transaction(pg.Operation('saved',total_timeout_ms=1000)) as tx:
            result=tx.finalize_saved_collection_members(**VALUES)
        self.assertEqual(result,{'object':{'revision':3}})
        self.assertEqual(len(self.connection.queries),1)
        statement,parameters=self.connection.queries[0]
        self.assertEqual(statement,_FINALIZE_CHANGED_MEMBERS)
        self.assertEqual(set(parameters),_FINALIZE_CHANGED_MEMBERS_KEYS)
        self.assertEqual(parameters['now'],VALUES['changed_at'])
        self.assertEqual(parameters['id'],VALUES['collection_id'])
        self.assertNotIn(VALUES['collection_id'],statement)
        self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
        self.assertGreaterEqual(len(self.admissions),3)
        self.assertTrue(statement.startswith('WITH changed_object AS'))
        self.assertTrue(statement.endswith('LIMIT 2'))

    def test_readonly_and_registered_rpc_contexts_do_not_dispatch(self):
        for readonly,rpc in ((True,None),(False,'abort_hot_collection_claims')):
            tx=pg.Transaction(self.connection,lambda name:None,read_only=readonly,rpc_name=rpc)
            with self.assertRaisesRegex(pg.DatabaseError,'database_saved_finalize_capability_invalid'):
                tx.finalize_saved_collection_members(**VALUES)
            with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):
                tx.fetch_one('SELECT 1')
        self.assertEqual(self.connection.queries,[])

    def test_arbitrary_sql_keys_and_return_limit_are_rejected(self):
        params=dict(id='id',request_id='request',fingerprint='fp',action='add',
                    requested_count=1,changed_count=1,delta=1,now='clock')
        for statement,parameters,limit in (('SELECT 1',params,1),(None,{},1),
            (None,dict(params,extra='x'),1),(None,tuple(params.values()),1),(None,params,2)):
            tx=pg.Transaction(self.connection,lambda name:None)
            with self.assertRaisesRegex(pg.DatabaseError,'database_saved_finalize_capability_invalid'):
                tx._run(statement,parameters,'saved_membership_finalize',limit)
        self.assertEqual(self.connection.queries,[])

    def test_result_overflow_poisoning_rolls_back_even_when_caught(self):
        self.connection.results.append(Result(rows=[{'object':{}},{'object':{}}]))
        with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):
            with self.db.transaction(pg.Operation('saved')) as tx:
                with self.assertRaisesRegex(pg.DatabaseError,'database_result_limit_exceeded'):
                    tx.finalize_saved_collection_members(**VALUES)
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))

    def test_receipt_constraint_error_rolls_back_without_another_dispatch(self):
        self.connection.results.append(Result(execute_error=ServerFailure('23514')))
        with self.assertRaises(pg.DatabaseServerError) as caught:
            with self.db.transaction(pg.Operation('saved')) as tx:
                tx.finalize_saved_collection_members(**VALUES)
        self.assertEqual(caught.exception.code,'23514')
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
        self.assertEqual(len(self.connection.queries),1)

    def test_lost_commit_ack_marks_unknown_and_does_not_replay(self):
        self.connection.results.append(Result(rows=[{'object':{'revision':3}}]))
        self.connection.commit_error=pg.psycopg.OperationalError('private test transport')
        with self.assertRaises(pg.DatabaseUnavailable) as caught:
            with self.db.transaction(pg.Operation('saved')) as tx:
                tx.finalize_saved_collection_members(**VALUES)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertNotIn('private test transport',str(caught.exception))
        self.assertEqual(len(self.connection.queries),1)
        self.assertEqual(self.pool.borrowed,1)

    def test_admission_rechecked_before_closed_statement(self):
        def deny(name):raise pg.DatabaseError('blocked_test_owner')
        tx=pg.Transaction(self.connection,deny)
        with self.assertRaisesRegex(pg.DatabaseError,'blocked_test_owner'):
            tx.finalize_saved_collection_members(**VALUES)
        self.assertEqual(self.connection.queries,[])

if __name__=='__main__':unittest.main()
