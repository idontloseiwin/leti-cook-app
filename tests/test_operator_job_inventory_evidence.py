"""Borrowed evidence/restart ordering and rollback, without a database."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_inventory_evidence as evidence
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN

SCAN='f8500000-0000-4000-8000-000000000004';TRANSITION='f8500000-0000-4000-8000-000000000005'
def one(**values):return Result((values,))
def write():return Result(has_columns=False,rowcount=1)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        scope=patch.object(evidence,'assert_full_initial_scope');self.scope=scope.start();self.addCleanup(scope.stop)
        hot=patch.object(evidence,'_hot');self.hot=hot.start();self.addCleanup(hot.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def base(self,phase='bound',full=True):
        self.queue(one(job='{}',version=1 if full else 0,phase=phase),one(value='{"channel_id":"c"}'),one(value=SCAN),
            one(value='{}'),one(invalid=False),one(value=True),one(value=dict(kind='none',scan_id=SCAN,
                format_finished=True,format_absent=False,bound_live=True,retry_not_before=None),source_invalid=False))
    def receipts(self,page=None,quota=None):
        self.queue(one(value=TRANSITION),one(value=page) if page is not None else Result(),one(value=quota) if quota is not None else Result())
    def call(self):
        with self.database.transaction(Operation('evidence_test')) as tx:return evidence.initial_inventory_checkpoint_evidence(tx,{})
    def restart(self,finished=False):
        with self.database.transaction(Operation('restart_test')) as tx:return evidence.restart_initial_inventory_discovery(tx,{},finished)
    def restart_base(self,invalid=False,keep=False,live=False):
        self.queue(one(job='{}',finished=False,phase='prepared'),one(value='{}'),one(value=SCAN),one(value='{}'),
            one(invalid=False),one(invalid=invalid,keep=keep),one(value=live))
    def test_missing_playlist_returns_minimal_none_without_full_scope(self):
        self.queue(one(job='{}',version=1,phase=None),Result())
        self.assertEqual(self.call(),dict(kind='none',format_finished=False,format_absent=False,bound_live=False))
        self.scope.assert_not_called();self.hot.assert_not_called()
    def test_bound_missing_playlist_errors_before_full_scope(self):
        self.queue(one(job='{}',version=1,phase='bound'),Result())
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_state_missing'):self.call()
        self.scope.assert_not_called()
    def test_unbound_legacy_complete_scan_retains_finished_without_receipt_reads(self):
        self.base('prepared',False);result=self.call()
        self.assertTrue(result['format_finished']);self.scope.assert_not_called();self.assertEqual(len(self.connection.queries),7)
    def test_full_workset_never_adopts_mutable_completed_state_as_page_proof(self):
        self.base('prepared');result=self.call();self.assertFalse(result['format_finished']);self.assertFalse(result['format_absent'])
        self.scope.assert_called_once();self.assertEqual(len(self.connection.queries),7)
    def test_scan_lock_identity_precedes_wall_clock_projection(self):
        self.queue(one(job='{}',version=0,phase='prepared'),one(value='{}'),one(value=SCAN),one(value='{}'),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_scan_identity_invalid'):self.call()
        self.assertEqual(len(self.connection.queries),5);self.assertNotIn('clock_timestamp',evidence._SCAN)
    def test_no_receipts_keeps_live_bound_retry_evidence_without_hot_assertion(self):
        self.base();self.receipts();value=self.call()
        self.assertTrue(value['bound_live']);self.assertFalse(value['format_finished']);self.hot.assert_not_called()
        self.assertEqual([params[0] for sql,params in self.connection.queries if evidence._RECEIPT in sql],['scan_transition','quota_defer'])
    def test_conflicting_receipts_precede_expiry_and_clock_validation(self):
        self.base();self.receipts('{}','{}')
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_receipt_conflict'):self.call()
        self.hot.assert_not_called();self.assertEqual(len(self.connection.queries),10)
    def test_expired_receipt_precedes_clock_and_payload_checks(self):
        self.base();self.receipts('{}');self.queue(one(expired=True,source_checked_at='bad',kind='page'))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_receipt_expired'):self.call()
        self.hot.assert_not_called()
    def test_quota_clock_precedes_claims_check_and_native_json_result(self):
        self.base();self.receipts(quota='{}');self.queue(one(expired=False,source_checked_at='10000-01-01 UTC',kind=None),
            one(invalid=False),one(value='{"kind":"quota","retry_not_before":{"native":"json"},"provider_attempts":1}'))
        result=self.call();self.hot.assert_called_once();self.assertEqual(self.hot.call_args.args[1],'10000-01-01 UTC')
        self.assertEqual(result['retry_not_before'],{'native':'json'});self.assertEqual(result['provider_attempts'],1)
    def test_quota_claim_mismatch_is_atomic_public_error(self):
        self.base();self.receipts(quota='{}');self.queue(one(expired=False,source_checked_at='now',kind=None),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_quota_receipt_invalid'):self.call()
        self.assertEqual(self.connection.commits,0)
    def test_native_quota_result_keeps_large_json_numeric_text_for_composition(self):
        from copy import deepcopy
        raw='{"kind":"quota","retry_not_before":1e1000,"provider_attempts":1}'
        copied=deepcopy(evidence._NativeObject(raw))
        self.assertEqual(evidence._encoded(copied),raw)
        self.base();self.receipts(quota='{}');self.queue(one(expired=False,source_checked_at='now',kind=None),
            one(invalid=False),one(value=raw))
        result=self.call();self.assertEqual(result['retry_not_before'],float('inf'))
        self.assertEqual(evidence._encoded(result),raw)
        self.assertIn('captured.value::text',self.connection.queries[-1][0])
    def test_page_identity_precedes_failure_or_page_payload_validation(self):
        self.base();self.receipts('{}');self.queue(one(expired=False,source_checked_at='now',kind='failure'),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_receipt_identity_invalid'):self.call()
        self.assertEqual(len(self.connection.queries),12)
    def test_failure_clock_precedes_coverage_native_integer_cast(self):
        self.base();self.receipts('{}');self.queue(one(expired=False,source_checked_at='now',kind='failure'),one(invalid=False),
            one(invalid=False),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_source_clock_invalid'):self.call()
        self.assertTrue(evidence._FAILURE_CLOCK in self.connection.queries[-1][0])
    def test_failure_full_coverage_then_native_result_preserves_nullable_outcome(self):
        self.base();self.receipts('{}');self.queue(one(expired=False,source_checked_at='now',kind='failure'),one(invalid=False),
            one(invalid=None),one(invalid=False),one(invalid=False),one(value='{"kind":"failure","format_finished":null,"outcome":null}'))
        self.assertEqual(self.call(),{'kind':'failure','format_finished':None,'outcome':None})
    def test_page_payload_precedes_full_coverage_and_result(self):
        self.base();self.receipts('{}');self.queue(one(expired=False,source_checked_at='now',kind='page'),one(invalid=False),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_page_receipt_invalid'):self.call()
        self.assertTrue(evidence._PAGE_CHECK in self.connection.queries[-1][0])
    def test_page_native_full_coverage_error_is_not_relabelled(self):
        self.base();self.receipts('{}');self.queue(one(expired=False,source_checked_at='now',kind='page'),one(invalid=False),
            one(invalid=False),Result(execute_error=ServerFailure('22003')))
        with self.assertRaises(DatabaseError) as caught:self.call()
        self.assertEqual(caught.exception.code,'22003')
    def test_legacy_page_does_not_require_full_observed_id_array(self):
        self.base(full=False);self.receipts('{}');self.queue(one(expired=False,source_checked_at='now',kind='page'),one(invalid=False),
            one(invalid=False),one(value='{"kind":"page","items_processed":3}'))
        self.assertEqual(self.call()['items_processed'],3)
        self.assertFalse(any(evidence._PAGE_COVERAGE in sql for sql,params in self.connection.queries))
    def test_restart_finished_precedes_bound_checkpoint_gate(self):
        self.queue(one(job='{}',finished=True,phase='bound'));self.assertTrue(self.restart(True));self.assertEqual(len(self.connection.queries),1)
    def test_restart_bound_checkpoint_never_detaches_pending_receipt(self):
        self.queue(one(job='{}',finished=False,phase='bound'))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_receipt_reconciliation_required'):self.restart()
    def test_restart_missing_state_or_active_scan_is_noop(self):
        self.queue(one(job='{}',finished=None,phase=None),Result());self.assertTrue(self.restart(None))
        self.queue(one(job='{}',finished=False,phase=None),one(value='{}'),one(value=None));self.assertTrue(self.restart())
    def test_restart_live_discovery_returns_false_without_mutation(self):
        self.restart_base(live=True);self.assertFalse(self.restart());self.assertEqual(len(self.connection.queries),7)
    def test_restart_abandons_scan_then_detaches_playlist_then_exact_job_fk(self):
        self.restart_base();self.queue(write(),write(),write());self.assertTrue(self.restart())
        for actual,wanted in zip([sql for sql,params in self.connection.queries[-3:]],(evidence._ABANDON,evidence._DETACH_PLAYLIST,evidence._DETACH_JOB)):
            self.assertIn(wanted,actual)
    def test_borrowed_error_cannot_be_caught_to_commit_prior_write(self):
        self.queue(write(),one(job='{}',version=1,phase='bound'),Result())
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('evidence_owner')) as tx:
                tx.execute('UPDATE public.operator_jobs SET updated_at=now() WHERE id=%s::uuid',(SCAN,))
                try:evidence.initial_inventory_checkpoint_evidence(tx,{})
                except DatabaseServerError:pass
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(0,1,1))
    def test_fixed_sql_bindings_and_single_sample_absence(self):
        args=dict(job='{}',playlist='{}',scan='{}',scan_id=SCAN,receipt='{}',result=Jsonb({}),full=True,full_ready=None,transition=TRANSITION)
        writes={evidence._ABANDON,evidence._DETACH_PLAYLIST,evidence._DETACH_JOB}
        for name,sql in vars(evidence).items():
            if not name.startswith('_') or not isinstance(sql,str) or not sql.lstrip().startswith(('SELECT','WITH','UPDATE')):continue
            params=args if '%(' in sql else tuple('{}' for _ in range(sql.count('%s')))
            PostgresQuery(Transformer()).convert(sql,params);_reviewed_statement(sql,read=sql not in writes,max_rows=0 if sql in writes else 1)
        self.assertEqual(evidence._BASE.count("state.inventory_status='playlist_not_found'"),1)
        self.assertIn('MATERIALIZED',evidence._BASE)
        source=(ROOT/'application/db/jobs/operator_job_inventory_evidence.py').read_text()
        for forbidden in ('database.call(','.transaction(', 'datetime.'):self.assertNotIn(forbidden,source)


if __name__=='__main__':unittest.main()
