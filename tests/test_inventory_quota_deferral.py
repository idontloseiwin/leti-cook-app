"""Offline quota-deferral receipt, lock ordering and no-retry transaction tests."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys,unittest
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.inventory import inventory_quota_deferral as helper
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_inventory_scan_steps import A,LEASE,TRANSITION,STAMP,one,changed,control


def claims(count=1):return [dict(channel_id=A if count==1 else f'UC{i:022d}',video_type='long',lease_token=LEASE) for i in range(count)]
def result(**changes):return dict(status='deferred',reason='youtube_daily_quota_exceeded',transition_id=TRANSITION,
    requested=1,released=1,rejected=0,retry_not_before='2026-10-07T07:00:00+00:00',active_scan_state_preserved=True,replayed=False,**changes)

class QuotaDeferralTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*rows):self.connection.results.extend(rows)
    def start(self,values=None,receipt=None,*,matches=True,at=STAMP):
        values=claims() if values is None else values
        self.queue(one(value=dict(transition_id=TRANSITION,source_checked_at=at),claims_type='array',claims_count=len(values)),
            one(valid=True),one(valid=True),one(present=False),one(claims=values),one(lock=None),
            Result() if receipt is None else one(value=receipt,matches=matches))
        return values
    def finish(self,values=None,*,released=True):
        values=claims() if values is None else values
        for claim in values:self.queue(one(lease_channel_epoch=7),one(valid=True),changed(1 if released else 0))
        output=dict(result(),requested=len(values),released=len(values) if released else 0,rejected=0 if released else len(values))
        scopes=[dict(channel_id=v['channel_id'],control_epoch=7) for v in values] if released else []
        self.queue(one(value=output),changed(),one(valid=True),one(scopes=scopes))
        if scopes:self.queue(Result(tuple(control(channel_id=v['channel_id']) for v in values)))
        return output
    def invoke(self,values=None,**changes):return helper.defer_typed_inventory_claims_after_daily_quota(self.database,
        changes.get('transition_id',TRANSITION),claims() if values is None else values,changes.get('at',STAMP))
    def test_native_header_clock_validation_canonical_advisory_and_row_lock_order(self):
        values=self.start();expected=self.finish();self.assertEqual(self.invoke(values),expected)
        sql=[q for q,_ in self.connection.queries]
        for index,statement in ((0,helper._INPUT),(1,helper._HOT),(4,helper._CANONICAL),(5,helper._TRANSITION_LOCK),(6,helper._RECEIPT),(7,helper._LOCK),(8,helper._HOT),(10,helper._RESULT),(12,helper._HOT),(13,helper._SCOPES)):
            self.assertIn(statement,sql[index])
        self.assertEqual(sql[9],helper._RELEASE);self.assertEqual(sql[11],helper._INSERT);self.assertIn('FOR SHARE',sql[-1])
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(1,0,1))
    def test_bound_five_hundred_retains_exact_three_statements_per_claim(self):
        values=claims(500);self.start(values);expected=self.finish(values);self.assertEqual(self.invoke(values),expected)
        self.assertEqual(len(self.connection.queries),1512)
        self.assertEqual(sum(helper._LOCK in q for q,_ in self.connection.queries),500)
    def test_header_native_cast_error_precedes_hot_and_domain_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.invoke(transition_id='bad',at='infinity')
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
    def test_hot_clock_precedes_missing_transition_and_bad_claim_shape(self):
        self.queue(one(value=dict(transition_id=None,source_checked_at=None),claims_type='object',claims_count=None),one(valid=False))
        with self.assertRaises(DatabaseServerError) as caught:self.invoke({})
        self.assertEqual((caught.exception.code,caught.exception.message),('22023','hot_source_clock_out_of_window'))
    def test_transition_then_count_then_claim_then_duplicate_error_precedence(self):
        for fault,code,length in (('transition','typed_inventory_quota_defer_transition_id_invalid',2),
                ('count','typed_inventory_quota_defer_claims_invalid',2),('claim','typed_inventory_quota_defer_claim_invalid',3),
                ('duplicate','typed_inventory_quota_defer_claim_duplicate',4)):
            self.queue(one(value=dict(transition_id=None if fault=='transition' else TRANSITION,source_checked_at=STAMP),
                claims_type='array',claims_count=501 if fault=='count' else 1),one(valid=True))
            if fault not in ('transition','count'):self.queue(one(valid=fault!='claim'))
            if fault=='duplicate':self.queue(one(present=True))
            start=len(self.connection.queries)
            with self.assertRaises(DatabaseServerError) as caught:self.invoke()
            self.assertEqual(caught.exception.message,code);self.assertEqual(len(self.connection.queries)-start,length)
    def test_canonicalized_native_claims_determine_lock_order_and_receipt_request(self):
        canonical=[dict(channel_id=A,video_type='long',lease_token=LEASE),dict(channel_id=A,video_type='short',lease_token=LEASE)]
        self.start(canonical)
        for value in canonical:self.queue(one(lease_channel_epoch=7),one(valid=True),changed())
        expected=dict(result(),requested=2,released=2)
        self.queue(one(value=expected),changed(),one(valid=True),one(scopes=[dict(channel_id=A,control_epoch=7)]),one(**control()))
        self.invoke([dict(canonical[1],channel_id=' '+A+' ',ignored='extra'),dict(canonical[0],lease_token=LEASE.upper())])
        self.assertEqual(self.connection.queries[7][1],canonical[0]);self.assertEqual(self.connection.queries[10][1],canonical[1])
        self.assertEqual(self.connection.queries[14][1]['claims'].obj,canonical)
    def test_retry_uses_pacific_midnight_and_at_least_one_hour(self):
        for at,expected in (('2026-10-06T12:00:00+00:00','2026-10-07T07:00:00+00:00'),
                ('2026-10-07T06:30:00+00:00','2026-10-07T07:30:00+00:00'),
                ('2026-11-01T08:30:00+00:00','2026-11-02T08:00:00+00:00')):
            self.start(at=at);self.finish();start=len(self.connection.queries);self.invoke(at=at)
            self.assertEqual(self.connection.queries[start+9][1]['retry'].isoformat(),expected)
    def test_release_only_exact_live_enabled_owner_and_preserves_scan_fields(self):
        self.assertIn('AND runtime_enabled',helper._RELEASE);self.assertIn("AND status='leased'",helper._RELEASE)
        self.assertIn('lease_token=%(lease_token)s::uuid AND lease_expires_at>clock_timestamp()',helper._RELEASE)
        for field in ('active_scan_id=','resume_page_token=','scan_boundary_at=','attempt_count=','inventory_status='):
            self.assertNotIn(field,helper._RELEASE)
        self.assertIn('greatest(coalesce(next_due_at',helper._RELEASE)
    def test_no_owned_rows_still_get_a_durable_receipt_without_final_control_lock(self):
        self.start();expected=self.finish(released=False);self.assertEqual(self.invoke(),expected)
        self.assertEqual(len(self.connection.queries),14);self.assertNotIn('FOR SHARE',' '.join(q for q,_ in self.connection.queries))
    def test_exact_replay_precedes_current_row_locks_with_repeated_clock_passes(self):
        saved=dict(source_checked_at=STAMP,result=result());self.start(receipt=saved)
        self.queue(one(valid=True),one(valid=True),one(valid=True))
        self.assertEqual(self.invoke(),dict(result(),replayed=True))
        sql=[q for q,_ in self.connection.queries];self.assertEqual(len(sql),10)
        self.assertFalse(any(helper._LOCK in q or q.startswith(('UPDATE','INSERT')) for q in sql))
    def test_receipt_expired_flag_precedes_clock_and_request_mismatch(self):
        self.start(receipt=dict(source_checked_at='infinity',result={'source_expired':False}),matches=False)
        with self.assertRaises(DatabaseServerError) as caught:self.invoke()
        self.assertEqual(caught.exception.message,'hot_inventory_receipt_source_expired');self.assertEqual(len(self.connection.queries),7)
    def test_receipt_clock_precedes_mismatch_and_request_clock_recheck(self):
        self.start(receipt=dict(source_checked_at=STAMP,result=result()),matches=False);self.queue(one(valid=False))
        with self.assertRaises(DatabaseServerError) as caught:self.invoke()
        self.assertEqual(caught.exception.code,'22023')
        self.start(receipt=dict(source_checked_at=STAMP,result=result()),matches=False);self.queue(one(valid=True))
        with self.assertRaises(DatabaseServerError) as caught:self.invoke()
        self.assertEqual(caught.exception.message,'typed_inventory_quota_defer_transition_id_reused')
    def test_post_wait_clock_failure_prevents_release(self):
        self.start();self.queue(one(lease_channel_epoch=7),one(valid=False))
        with self.assertRaises(DatabaseServerError):self.invoke()
        self.assertEqual(len(self.connection.queries),9);self.assertEqual(self.connection.rollbacks,1)
    def test_receipt_insert_or_final_scope_failure_rolls_back_releases(self):
        for index,value in ((11,Result(execute_error=ServerFailure('23505'))),(14,one(**control(control_epoch=8)))):
            self.start();self.finish();self.connection.results[index]=value
            with self.assertRaises(DatabaseServerError):self.invoke()
            self.connection.results.clear()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,2))
    def test_borrowed_domain_failure_poisons_outer_transaction(self):
        self.queue(one(value=dict(transition_id=None,source_checked_at=STAMP),claims_type='array',claims_count=1),one(valid=True))
        with self.assertRaises(DatabaseError):
            with self.database.transaction(Operation('quota_deferral_test')) as tx:
                with self.assertRaises(DatabaseServerError):helper.defer_typed_inventory_claims_after_daily_quota(None,None,claims(),STAMP,transaction=tx)
        self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_does_not_replay(self):
        self.start();self.finish();self.connection.commit_error=psycopg.OperationalError('private payload')
        with self.assertRaises(DatabaseUnavailable) as caught:self.invoke()
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
    def test_no_business_rpc_and_fixed_advisory_namespace(self):
        code=(ROOT/'application/db/inventory/inventory_quota_deferral.py').read_text(encoding='utf-8-sig')
        self.assertNotIn('.call(',code);self.assertIn('781631247',helper._TRANSITION_LOCK)

if __name__=='__main__':unittest.main()
