"""Bounded bootstrap and initial conflict settlement SQL contracts.

Runtime transition behavior is proved by native_initial_type_conflict on PG17;
these guards cover the borrowed write boundary and unchanged SQL parameter use.
"""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_queue_initialization as queues
from db.videos import video_initial_claims as claims
from db.jobs import operator_job_scope as scope
from db.jobs import operator_job_chunks as prepare
from db.jobs import operator_job_chunk_settlement as settle
from db.base.postgres import _reviewed_statement


class Borrowed:
    def __init__(self, *, fail=False):
        self.calls=[]
        self.borrowed=0
        self.fail=fail
        self.poisoned=False

    @contextmanager
    def borrow(self):
        self.borrowed+=1
        try:
            yield self
        except BaseException:
            self.poisoned=True
            raise

    def returning_all(self,sql,params,*,max_rows):
        self.calls.append((sql,params,max_rows))
        if self.fail:
            raise RuntimeError('synthetic_transaction_failure')
        return [dict(video_id=identity) for identity in params[0]]


class AutomaticInitialBootstrapTests(unittest.TestCase):
    def test_typed_only_initializer_is_one_borrowed_insert_and_deduplicates(self):
        tx=Borrowed()
        self.assertEqual(queues.ensure_typed_collection_state_in_transaction(tx,['a','b','a']),2)
        self.assertEqual(tx.borrowed,1)
        self.assertEqual(tx.calls,[(queues._TYPED_INSERT,(['a','b'],),2)])

    def test_empty_initializer_never_acquires_or_writes(self):
        tx=Borrowed()
        self.assertEqual(queues.ensure_typed_collection_state_in_transaction(tx,[]),0)
        self.assertEqual((tx.borrowed,tx.calls),(0,[]))

    def test_invalid_or_unbounded_input_rejects_before_borrow(self):
        for values in (None,'a',set(),[''],[' a'],[1],['a']*1001):
            tx=Borrowed()
            with self.subTest(values=type(values).__name__),self.assertRaisesRegex(ValueError,'typed_collection_ids_invalid'):
                queues.ensure_typed_collection_state_in_transaction(tx,values)
            self.assertEqual((tx.borrowed,tx.calls),(0,[]))

    def test_boundary_1000_stays_one_statement(self):
        tx=Borrowed();values=[f'v{i:010d}' for i in range(1000)]
        self.assertEqual(queues.ensure_typed_collection_state_in_transaction(tx,values),1000)
        self.assertEqual(len(tx.calls),1)
        self.assertEqual(tx.calls[0][2],1000)

    def test_failure_propagates_and_poisons_borrowed_transaction(self):
        tx=Borrowed(fail=True)
        with self.assertRaisesRegex(RuntimeError,'synthetic_transaction_failure'):
            queues.ensure_typed_collection_state_in_transaction(tx,['a'])
        self.assertTrue(tx.poisoned)
        self.assertEqual(len(tx.calls),1)

    def test_new_insert_passes_driver_and_psycopg_binding(self):
        _reviewed_statement(queues._TYPED_INSERT,read=True,max_rows=1,returning=True)
        query=PostgresQuery(Transformer())
        query.convert(queues._TYPED_INSERT,(['aaaaaaaaaaa'],))
        self.assertEqual(len(query.params),1)

    def test_current_v1_conflict_scope_and_initial_claim_bind_without_extra_parameters(self):
        _reviewed_statement(scope._TYPE,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(scope._TYPE,dict(video_id='aaaaaaaaaaa',channel_id='UCexample'))
        values=dict(video_ids=['aaaaaaaaaaa'],channel_id='UCexample',job_id='00000000-0000-4000-8000-000000000001',
            job='{}',worker_id='native',epoch=1,expires='2026-10-15T00:00:00Z',lease_seconds=900,
            run_id='00000000-0000-4000-8000-000000000002')
        for sql in (claims._INSERT,claims._CLAIM):
            PostgresQuery(Transformer()).convert(sql,values)

    def test_initial_type_rebind_and_receipt_classification_keep_one_bounded_write(self):
        values=dict(job='{}',ids=['aaaaaaaaaaa'],chunk=['aaaaaaaaaaa'])
        for sql in (prepare._IDENTITY,prepare._AWAITING,settle._IDENTITY,settle._CHECKED):
            _reviewed_statement(sql,read=False,max_rows=None)
            PostgresQuery(Transformer()).convert(sql,values)
        for sql in (prepare._IDENTITY,settle._IDENTITY):
            self.assertIn('automatic.automatic_type=inventory.video_type',sql)
            self.assertIn('j.initial_scope_version=1',sql)
            self.assertIn("'frozen_identity_changed'",sql)
        for sql in (prepare._AWAITING,settle._CHECKED):
            self.assertIn("observed.receipt_kind='identity_refresh'",sql)
            self.assertIn('observed.recorded_at=state.last_success_at',sql)
            self.assertIn('state.last_success_at>=j.requested_at',sql)


if __name__=='__main__':
    unittest.main()
