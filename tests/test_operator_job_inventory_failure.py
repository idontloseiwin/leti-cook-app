"""Failure acknowledgement ordering, evidence choices and transaction ownership."""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import json,re,sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_inventory_failure as h
from db.jobs.operator_job_inventory_evidence import _NativeObject
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL
TOKEN='f8800000-0000-4000-8000-000000000002'
RAW=json.dumps(dict(id=ID,channel_id=CHANNEL,summary_json={'opaque':0}))
NOW=datetime(2026,10,6,12,tzinfo=timezone.utc)


def inputs(**changes):
    args=dict(job_id=ID,lease_token=TOKEN,error_code='provider_failed',playlist_not_found=False);args.update(changes);return args
def prefix(*,phase='inventory',version=0,**changes):
    return [inputs(**changes),{'invalid':False},{'value':'hash'},None,{'channel_id':CHANNEL},{'channel_id':CHANNEL},{'value':RAW},
        dict(phase=phase,version=version,epoch=17,accepted_epoch=17,invalid=False)]
def evidence(*,prepared=False,**changes):
    flags=dict(deferred=False,uncertain=False,quota_deferred=False,kind='failure',outcome='error',evidence_error='provider_failed');flags.update(changes)
    return [{'invalid':False},dict(reserved=1,checkpoint_phase='prepared' if prepared else 'bound',summary='{"transition_id":"native"}'),flags]
def finish(*,count=1,status='queued',retry=None,keep=False):
    return [{'value':count},{'value':status},{'value':retry},{'value':'{"native":"ack"}'},{'value':RAW},{'value':keep},1]


class InventoryFailureTests(unittest.TestCase):
    def setUp(self):
        for name in ('initial_inventory_checkpoint_evidence','settle_initial_inventory_job_chunk','_fence','youtube_quota_reset_at'):
            p=patch.object(h,name);setattr(self,name,p.start());self.addCleanup(p.stop)
        self.initial_inventory_checkpoint_evidence.return_value=_NativeObject('{"opaque":1e1000}')
        self.settle_initial_inventory_job_chunk.return_value=_NativeObject(RAW)
        self.youtube_quota_reset_at.return_value=NOW
    def call(self,db,**changes):
        args=dict(job_id=ID,lease_token=TOKEN,error_code='provider_failed');args.update(changes)
        return h.fail_initial_inventory_job_chunk(db,**args)
    def test_exact_failed_ack_uses_native_hash_and_has_no_domain_lock(self):
        db=Database(inputs(),{'invalid':False},{'value':'hash'},dict(job=RAW,settled=False,settled_conflict=True,failed=True,failed_conflict=False),{'value':'{"inventory_ack_replayed":true}'})
        self.assertEqual(self.call(db),{'inventory_ack_replayed':True})
        self.assertEqual([sql for _,sql,_ in db.calls],[h._INPUT,h._VALIDATE,h._HASH,h._REPLAY,h._REPLAY_RESULT])
        self.initial_inventory_checkpoint_evidence.assert_not_called();self._fence.assert_not_called();self.assertEqual(db.events,['commit'])
        self.assertIn('coalesce(%(playlist_not_found)s::boolean,false)',h._HASH)
    def test_settled_absence_replay_compares_error_before_failure_hash(self):
        db=Database(inputs(playlist_not_found=True),{'invalid':False},{'value':'different'},dict(job=RAW,settled=True,settled_conflict=False,failed=True,failed_conflict=True),{'value':RAW})
        self.assertEqual(self.call(db,playlist_not_found=True),json.loads(RAW));self.assertEqual(len(db.calls),5)
    def test_replay_conflicts_fail_before_locks_and_fence(self):
        for settled in (False,True):
            db=Database(inputs(),{'invalid':False},{'value':'hash'},dict(job=RAW,settled=settled,settled_conflict=True,failed=not settled,failed_conflict=True))
            with self.assertRaisesRegex(DatabaseError,'initial_inventory_job_ack_conflict'):self.call(db)
            self.assertEqual(len(db.calls),4);self.assertEqual(db.events,['rollback'])
    def test_native_argument_casts_precede_error_code_and_lookup(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:self.call(db,job_id='bad',error_code=None)
        self.assertIs(caught.exception,error);self.assertEqual(len(db.calls),1)
    def test_error_code_validation_precedes_hash_or_replay(self):
        db=Database(inputs(error_code=None),{'invalid':True})
        with self.assertRaisesRegex(DatabaseError,'operator_job_error_code_invalid'):self.call(db,error_code=None)
        self.assertEqual(len(db.calls),2)
    def test_missing_or_wrong_owner_rejects_without_evidence(self):
        for index,replacement in ((4,None),(5,None),(6,None),(7,dict(invalid=True,phase='inventory',version=0,epoch=17,accepted_epoch=17))):
            values=prefix();values[index]=replacement;db=Database(*values[:index+1])
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call(db)
        self.initial_inventory_checkpoint_evidence.assert_not_called()
    def test_inventory_wall_clock_and_checkpoint_guard_before_evidence(self):
        db=Database(*prefix(),{'invalid':True})
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_job_checkpoint_mismatch'):self.call(db)
        self.initial_inventory_checkpoint_evidence.assert_not_called();self.assertIn('clock_timestamp()',h._CHECKPOINT)
        self.assertNotIn('lease_expires_at',h._LEASE);self.assertNotIn('accepted_epoch IS DISTINCT',h._LEASE)
    def test_absence_evidence_then_borrowed_page_writer_with_exact_native_transition(self):
        values=evidence();db=Database(*prefix(playlist_not_found=True),*values[:2],{'invalid':False})
        result=self.call(db,playlist_not_found=True)
        self.assertEqual(result,json.loads(RAW))
        self.settle_initial_inventory_job_chunk.assert_called_once_with(db,ID,TOKEN,1,0,0,0,0,None,'{"transition_id":"native"}',transaction=db)
        self._fence.assert_not_called();self.assertEqual(db.calls[-1][1],h._ABSENCE_CHECK)
    def test_absence_requires_exact_receipt_before_delegating(self):
        db=Database(*prefix(playlist_not_found=True),*evidence()[:2],{'invalid':True})
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_absence_receipt_required'):self.call(db,playlist_not_found=True)
        self.settle_initial_inventory_job_chunk.assert_not_called()
    def test_prepared_deferred_refunds_unused_reservation_and_native_retry(self):
        db=Database(*prefix(error_code='initial_inventory_deferred'),*evidence(prepared=True,deferred=True,kind='none',outcome=None,evidence_error=None),
            *finish(count=0,retry='infinity'))
        self.call(db,error_code='initial_inventory_deferred');args=next(p for _,sql,p in db.calls if sql==h._UPDATE_INVENTORY)
        self.assertEqual(args['attempts'],0);self.assertEqual(args['retry'],'infinity');self.assertIsNone(args['message'])
        self.youtube_quota_reset_at.assert_not_called();self.assertEqual(db.calls[-1][1],h._CHANNEL_PENDING)
    def test_prepared_nondeferred_fails_before_count_or_ack(self):
        db=Database(*prefix(),*evidence(prepared=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_unbound_failure_invalid'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._FLAGS)
    def test_bound_page_failure_must_be_settled_before_counts(self):
        db=Database(*prefix(),*evidence(kind='page'))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_page_requires_job_settlement'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._FLAGS)
    def test_quota_receipt_requires_exact_error_and_owns_retry_clock(self):
        db=Database(*prefix(),*evidence(kind='quota',quota_deferred=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_quota_failure_mismatch'):self.call(db)
        db=Database(*prefix(error_code='initial_inventory_quota_exhausted'),*evidence(kind='quota',quota_deferred=True),*finish(count=0,retry='10000-01-01T00:00:00Z'))
        self.call(db,error_code='initial_inventory_quota_exhausted');args=db.calls[-3][2]
        self.assertEqual((args['attempts'],args['message'],args['retry']),(1,h._QUOTA,'10000-01-01T00:00:00Z'))
        self.youtube_quota_reset_at.assert_not_called()
    def test_failure_code_mismatch_precedes_native_counter(self):
        db=Database(*prefix(),*evidence(evidence_error='different'))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_failure_code_mismatch'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._FLAGS)
    def test_unknown_requires_absent_live_child_and_counts_reserved_conservatively(self):
        db=Database(*prefix(error_code='initial_inventory_outcome_unknown'),*evidence(kind='none',uncertain=True),{'invalid':True})
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_outcome_unresolved'):self.call(db,error_code='initial_inventory_outcome_unknown')
        db=Database(*prefix(error_code='initial_inventory_outcome_unknown'),*evidence(kind='none',uncertain=True),{'invalid':False},*finish())
        self.call(db,error_code='initial_inventory_outcome_unknown');args=next(p for _,sql,p in db.calls if sql==h._UPDATE_INVENTORY)
        self.assertEqual(args['attempts'],args['reserved']);self.assertEqual(args['message'],h._UNKNOWN)
    def test_receipt_proven_rate_preserves_attempt_count_and_waits_source_clock(self):
        self.initial_inventory_checkpoint_evidence.return_value=_NativeObject(
            '{"scan_retained":true,"retry_not_before":"2026-10-06T12:01:00Z"}')
        db=Database(*prefix(error_code='provider_rate_limited'),
            *evidence(evidence_error='provider_rate_limited'),*finish(count=3,retry='2026-10-06T12:01:00Z'))
        self.call(db,error_code='provider_rate_limited')
        args=next(p for _,sql,p in db.calls if sql==h._UPDATE_INVENTORY)
        self.assertTrue(args['rate_limited'])
        self.assertEqual((args['error_count'],args['attempts'],args['next_status']),(3,1,'queued'))
        self.assertFalse(args['quota_deferred'])
        self.youtube_quota_reset_at.assert_not_called()
        self.assertIn("OR %(rate_limited)s::boolean THEN 0",h._ERROR_COUNT)
        self.assertIn("isfinite(",h._RETRY)

    def test_rate_without_retained_scan_or_finite_retry_cannot_ack(self):
        for retained in (False,True):
            self.initial_inventory_checkpoint_evidence.return_value=_NativeObject(
                '{"scan_retained":'+str(retained).lower()+'}')
            answers=[*prefix(error_code='provider_rate_limited'),*evidence(evidence_error='provider_rate_limited')]
            if retained:answers.extend([{'value':3},{'value':'queued'},{'value':None}])
            db=Database(*answers)
            with self.assertRaisesRegex(DatabaseError,'initial_inventory_outcome_unresolved'):
                self.call(db,error_code='provider_rate_limited')
            self.assertNotIn(h._ACK,[sql for _,sql,_ in db.calls])
            self.assertEqual(db.events,['rollback'])

    def test_legacy_rate_keeps_existing_count_and_queues_one_minute(self):
        db=Database(*prefix(phase='initial_stats',error_code='provider_rate_limited'),{'value':False},
            dict(quota_deferred=False,error_count=3,max_target_attempts=3,as_of=NOW),
            {'value':RAW},{'value':True})
        self.call(db,error_code='provider_rate_limited')
        args=next(p for _,sql,p in db.calls if sql==h._UPDATE_LEGACY)
        self.assertEqual((args['next_status'],args['error_count']),('queued',3))
        self.youtube_quota_reset_at.assert_not_called()
        self.assertIn("OR %(error_code)s::text='provider_rate_limited' THEN 0",h._LEGACY_COUNT)
        self.assertIn("THEN now()+interval '1 minute'",h._UPDATE_LEGACY)

    def test_native_counter_overflow_precedes_restart_snapshot_and_fence(self):
        error=DatabaseServerError('database_operation_failed',code='22003');db=Database(*prefix(version=1),*evidence(),error)
        with self.assertRaises(DatabaseError) as caught:self.call(db)
        self.assertIs(caught.exception,error);self.assertEqual(db.calls[-1][1],h._ERROR_COUNT);self._fence.assert_not_called()
    def test_full_failed_scan_forces_error_after_counts_then_records_restart(self):
        db=Database(*prefix(version=1),*evidence(),{'value':1},{'value':'queued'},{'value':True},
            {'value':None},{'value':'{}'},{'value':RAW},{'value':False},{'value':{}})
        self.call(db);args=next(p for _,sql,p in db.calls if sql==h._UPDATE_INVENTORY)
        self.assertEqual(args['next_status'],'error');self.assertEqual(args['message'],h._RESTART_MESSAGE)
        self.assertEqual(db.calls[-1][2],('initial_inventory_restart_required',CHANNEL))
        ordered=[s for _,s,_ in db.calls];self.assertLess(ordered.index(h._NEXT),ordered.index(h._RESTART));self.assertLess(ordered.index(h._RESTART),ordered.index(h._RETRY))
    def test_retry_native_clock_error_occurs_after_restart_before_ack_write(self):
        error=DatabaseServerError('database_operation_failed',code='22007');db=Database(*prefix(),*evidence(),{'value':1},{'value':'queued'},error)
        with self.assertRaises(DatabaseError) as caught:self.call(db)
        self.assertIs(caught.exception,error);self.assertEqual(db.calls[-1][1],h._RETRY)
    def test_inventory_unknown_keep_long_predicate_skips_channel_like_if_not_null(self):
        answers=finish(keep=None);db=Database(*prefix(),*evidence(),*answers[:-1]);self.call(db)
        self.assertEqual(db.calls[-1][1],h._KEEP_LONG);self._fence.assert_called_once()
    def test_noninventory_has_no_expiry_and_preserves_unknown_keep_long_branch(self):
        db=Database(*prefix(phase='hydrate_videos'),{'value':False},dict(quota_deferred=False,error_count=1,max_target_attempts=3,as_of=NOW),{'value':RAW},{'value':None},1)
        self.call(db);sqls=[s for _,s,_ in db.calls]
        self.assertNotIn(h._CHECKPOINT,sqls);self.assertEqual(sqls[-1],h._CHANNEL_PENDING)
        self.initial_inventory_checkpoint_evidence.assert_not_called();self.youtube_quota_reset_at.assert_not_called()
    def test_noninventory_all_format_absence_rejects_before_legacy_write(self):
        db=Database(*prefix(phase='hydrate_videos',playlist_not_found=True),{'value':True})
        with self.assertRaisesRegex(DatabaseError,'all_format_playlist_absence_requires_inventory_settlement'):self.call(db,playlist_not_found=True)
        self.assertEqual(db.calls[-1][1],h._ALL_TYPES)
    def test_noninventory_long_absence_keeps_special_clock_and_no_receipt(self):
        db=Database(*prefix(phase='verify',playlist_not_found=True),{'value':False},{'value':RAW},1)
        self.call(db,playlist_not_found=True);self.assertEqual(db.calls[-2][1],h._LEGACY_ABSENT);self.assertEqual(db.calls[-2][2],(h._ABSENT_MESSAGE,ID))
        self.assertNotIn('heartbeat_at',h._LEGACY_ABSENT);self.assertNotIn('inventory_ack',h._LEGACY_ABSENT)
    def test_legacy_quota_still_gets_native_now_reset_when_error_limit_reached(self):
        db=Database(*prefix(phase='initial_stats',error_code='phase_quota_exhausted'),{'value':False},
            dict(quota_deferred=True,error_count=3,max_target_attempts=3,as_of=NOW),{'value':RAW},{'value':False},{'value':{}})
        self.call(db,error_code='phase_quota_exhausted');self.youtube_quota_reset_at.assert_called_once_with(NOW)
        args=next(p for _,sql,p in db.calls if sql==h._UPDATE_LEGACY)
        self.assertEqual((args['next_status'],args['message'],args['reset']),('error',h._EXHAUSTED,NOW))
        self.assertIn("greatest(now()+interval '1 hour',%(reset)s::timestamptz)",h._UPDATE_LEGACY)
    def test_legacy_completed_long_is_kept_without_channel_write(self):
        db=Database(*prefix(phase='hydrate_videos'),{'value':True},dict(quota_deferred=False,error_count=1,max_target_attempts=3,as_of=NOW),{'value':RAW},{'value':True})
        self.call(db);self.assertEqual(db.calls[-1][1],h._KEEP_LONG)
    def test_final_fence_remains_after_native_write_and_rolls_back(self):
        self._fence.side_effect=DatabaseServerError('operator_job_lease_lost',code='P0001')
        db=Database(*prefix(),*evidence(),*finish())
        with self.assertRaises(DatabaseError):self.call(db)
        self.assertEqual(db.calls[-1][1],h._CHANNEL_PENDING);self.assertEqual(db.events,['rollback'])
    def test_borrowed_transaction_no_second_owner_and_unknown_commit_no_retry(self):
        db=Database(*prefix(),*evidence(),*finish());self.call(db,transaction=db);self.assertEqual(db.events,['borrow']);self.assertEqual(db.operations,[])
        class LostAck(Database):
            @contextmanager
            def transaction(self,operation):
                with super().transaction(operation) as tx:yield tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        db=LostAck(*prefix(),*evidence(),*finish())
        with self.assertRaises(DatabaseError) as caught:self.call(db)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(len(db.operations),1);self.assertEqual(db.events,['commit'])
    def test_raw_json_snapshots_and_retained_timeout_are_preserved(self):
        db=Database(*prefix(),*evidence(),*finish());self.call(db)
        args=next(p for _,sql,p in db.calls if sql==h._UPDATE_INVENTORY);self.assertEqual(args['evidence'],'{"opaque":1e1000}')
        spec=h.get_operation('fail_initial_inventory_job_chunk');op=db.operations[0]
        self.assertEqual((op.statement_timeout_ms,op.total_timeout_ms,op.lock_timeout_ms,op.error_codes),(spec.statement_timeout_ms,spec.statement_timeout_ms,spec.lock_timeout_ms,spec.error_codes))
    def test_all_fixed_sql_binds_through_actual_driver_guard(self):
        returning={h._UPDATE_INVENTORY,h._UPDATE_LEGACY,h._LEGACY_ABSENT,h._RECORD_ERROR}
        for name,sql in vars(h).items():
            if not(name.startswith('_') and isinstance(sql,str) and sql.lstrip().startswith(('SELECT','WITH','INSERT','UPDATE'))):continue
            names=re.findall(r'%\(([^)]+)\)s',sql);params={key:None for key in names} if names else (None,)*len(re.findall('%s',sql))
            PostgresQuery(Transformer()).convert(sql,params);ret=sql in returning;read=ret or sql.lstrip().startswith(('SELECT','WITH'))
            _reviewed_statement(sql,read=read,max_rows=1 if read else 0,returning=ret)
        source=Path(h.__file__).read_text();self.assertNotIn('.call(',source);self.assertNotIn('operator_private.',source)


if __name__=='__main__':unittest.main()
