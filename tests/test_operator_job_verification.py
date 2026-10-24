"""Verification replay, native snapshots and final transaction ownership."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import json,re,sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_verification as h
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL
TOKEN='f9600000-0000-4000-8000-000000000002'
RAW=json.dumps(dict(id=ID,channel_id=CHANNEL,summary_json={'opaque':0}))
NATIVE='{"id":"'+ID+'","summary_json":{"opaque":1e1000}}'


def prefix(*,version=0,all_types=False,replayed=False,epoch=17,accepted_epoch=17):
    return [dict(job_id=ID,lease_token=TOKEN),dict(channel_id=CHANNEL),dict(channel_id=CHANNEL),dict(value=RAW),
        dict(version=version,all_types=all_types,replayed=replayed,epoch=epoch,accepted_epoch=accepted_epoch)]
def counts(**changes):
    values=dict(total_count=1,pending_count=0,updated_count=1,skipped_count=0,unavailable_count=0,error_count=0)
    values.update(changes);return values
def tail(*,all_types=False,identity=False,ready=True,control=True):
    values=[counts(),dict(target_count_matches=True,ready=ready),dict(job=NATIVE)]
    if all_types:values.extend([dict(formats='{"short":{"opaque":1e1000}}'),dict(job=NATIVE,value={'lossy':'ignored'})])
    values.extend([1,dict(policy_facts=native_eligible_facts(control),control_epoch=17) if control is not None else None,0]);return values


class VerificationTests(unittest.TestCase):
    def setUp(self):
        for name in ('assert_full_initial_scope','assert_channel_scope'):
            p=patch.object(h,name);setattr(self,name,p.start());self.addCleanup(p.stop)
    def call(self,db,**changes):return h.verify_initial_hydration_job(db,ID,TOKEN,**changes)
    def test_native_casts_precede_missing_channel_job_and_replay(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as result:self.call(db)
        self.assertIs(result.exception,error);self.assertEqual([s for k,s,p in db.calls],[h._INPUT]);self.assertEqual(db.events,['rollback'])
    def test_missing_lookup_channel_or_held_job_fails_without_scope(self):
        for index in (1,2,3):
            rows=prefix()[:index]+[None];db=Database(*rows)
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call(db)
            self.assertEqual(len(db.calls),index+1)
        self.assert_full_initial_scope.assert_not_called()
    def test_replay_takes_channel_job_locks_then_skips_visibility_lease_and_fence(self):
        db=Database(*prefix(replayed=True,epoch=None,accepted_epoch=90));self.assertEqual(self.call(db),json.loads(RAW))
        self.assertEqual([s for k,s,p in db.calls],[h._INPUT,h._CHANNEL_ID,h._CHANNEL,h._JOB,h._STATE])
        self.assertIn('FOR UPDATE',h._CHANNEL);self.assertIn('FOR UPDATE',h._JOB)
        self.assert_full_initial_scope.assert_not_called();self.assert_channel_scope.assert_not_called()
    def test_replay_covers_exact_terminal_and_requeued_shapes_without_token_comparison(self):
        self.assertIn("j.phase='verify' AND j.status IN('completed','error') AND j.finished_at IS NOT NULL",h._STATE)
        self.assertIn("j.phase='hydrate_videos' AND j.status='queued'",h._STATE)
        self.assertIn('j.lease_token IS NULL AND j.lease_owner IS NULL AND j.lease_expires_at IS NULL',h._STATE)
        self.assertNotIn('%(lease_token)',h._STATE);self.assertNotIn('epoch IS NULL',h._STATE)
    def test_wrong_running_owner_rejects_before_scope_or_targets(self):
        db=Database(*prefix(),dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call(db)
        self.assert_full_initial_scope.assert_not_called();self.assertEqual(db.calls[-1][1],h._LEASE)
    def test_new_lease_uses_transaction_now_after_locks_not_wall_clock(self):
        self.assertIn('j.lease_expires_at<=now()',h._LEASE);self.assertNotIn('clock_timestamp',h._LEASE)
        self.assertIn('j.lease_token IS DISTINCT FROM %(lease_token)s::uuid',h._LEASE)
    def test_legacy_single_verification_preserves_statement_order_and_insert_only_discovery(self):
        db=Database(*prefix(),dict(invalid=False),*tail());result=self.call(db)
        self.assertEqual(result.encoded,NATIVE)
        self.assertEqual([s for k,s,p in db.calls][5:],[h._LEASE,h._COUNTS,h._READY,h._UPDATE,h._CHANNEL_UPDATE,h._DISCOVERY_CONTROL,h._DISCOVERY_INSERT])
        self.assert_full_initial_scope.assert_called_once_with(db,RAW,True);self.assert_channel_scope.assert_called_once_with(db,CHANNEL,17)
        self.assertEqual(db.calls[-1][2],(CHANNEL,CHANNEL,True,True));self.assertIn('DO NOTHING',h._DISCOVERY_INSERT)
    def test_full_identity_normalization_precedes_shared_identity_predicate(self):
        db=Database(*prefix(version=1,all_types=True),dict(invalid=False),0,dict(mismatch=True),*tail(all_types=True,ready=False))
        self.call(db);sql=[s for k,s,p in db.calls]
        self.assertIn(h._IDENTITY,sql);self.assertNotIn(h._LEGACY_IDENTITY,sql)
        self.assertIn('target.video_type)',h._IDENTITY)
        self.assertIn('IS DISTINCT FROM',h._IDENTITY)
        self.assertEqual(sql[6:8],[h._NORMALIZE_AUTOMATIC,h._IDENTITY])
        self.assertIn('NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships',h._IDENTITY)
        self.assertTrue(db.calls[sql.index(h._UPDATE)][2]['identity_mismatch'])
    def test_verified_reclassification_changes_only_type_under_current_automatic_proof(self):
        self.assertIn("target.status IN ('checked','updated','skipped','unavailable')",h._NORMALIZE_AUTOMATIC)
        self.assertIn("target.video_type IN ('long','short')",h._AUTOMATIC_TYPE)
        self.assertIn('automatic.channel_id=%(channel_id)s::text',h._AUTOMATIC_TYPE)
        self.assertIn('inventory.video_type=automatic.automatic_type',h._AUTOMATIC_TYPE)
        self.assertIn('candidate.video_id=target.video_id OFFSET 0',h._AUTOMATIC_TYPE)
        assignment=h._NORMALIZE_AUTOMATIC.split(' FROM ',1)[0]
        self.assertNotIn('job_id=',assignment)
        self.assertNotIn('video_id=',assignment)
        self.assertNotIn('status=',assignment)
        self.assertIn('changed.video_id',h._NORMALIZE_AUTOMATIC)
        self.assertIn(h._AUTOMATIC_TYPE,h._IDENTITY)
        self.assertIn(h._TARGET_TYPE,h._IDENTITY)

    def test_legacy_three_format_marks_identity_errors_before_count_snapshot(self):
        db=Database(*prefix(all_types=True),dict(invalid=False),2,*tail(all_types=True));self.call(db)
        sql=[s for k,s,p in db.calls];self.assertEqual(sql[6:8],[h._LEGACY_IDENTITY,h._COUNTS]);self.assertNotIn(h._IDENTITY,sql)
        self.assertNotIn("target.status='pending'",h._LEGACY_IDENTITY)
    def test_format_projection_is_separate_after_update_and_keeps_raw_json(self):
        db=Database(*prefix(all_types=True),dict(invalid=False),1,*tail(all_types=True));result=self.call(db)
        sql=[s for k,s,p in db.calls];index=sql.index(h._FORMATS)
        self.assertEqual(sql[index-1:index+2],[h._UPDATE,h._FORMATS,h._FORMAT_UPDATE]);self.assertEqual(db.calls[index][2]['job'],NATIVE)
        self.assertEqual(db.calls[index+1][2]['formats'],'{"short":{"opaque":1e1000}}');self.assertEqual(result.encoded,NATIVE)
        self.assertIn("'current_profile_ready',NOT %(identity_mismatch)s::boolean",h._FORMATS)
    def test_pending_status_precedes_identity_or_count_errors_in_native_update(self):
        self.assertLess(h._UPDATE.index("WHEN %(pending_count)s::integer>0 THEN 'queued'"),h._UPDATE.index("WHEN %(ready)s::boolean THEN 'completed'"))
        self.assertIn("WHEN %(pending_count)s::integer>0 THEN NULL\n WHEN %(identity_mismatch)",h._UPDATE)
    def test_checked_targets_do_not_change_top_level_ready_but_veto_format_ready(self):
        self.assertNotIn('checked',h._COUNTS);self.assertNotIn('checked',h._READY)
        self.assertIn('counts.checked_count=0',h._FORMATS)
    def test_discovery_missing_control_defaults_false_without_dropping_insert(self):
        db=Database(*prefix(),dict(invalid=False),*tail(control=None));self.call(db)
        self.assertEqual(db.calls[-1][2],(CHANNEL,CHANNEL,False,False))
    def test_scope_proof_failure_precedes_target_writes_and_counts(self):
        db=Database(*prefix(),dict(invalid=False));self.assert_full_initial_scope.side_effect=DatabaseServerError('full_initial_targets_not_frozen',code='P0001')
        with self.assertRaises(DatabaseError):self.call(db)
        self.assertEqual(db.calls[-1][1],h._LEASE);self.assert_channel_scope.assert_not_called()
    def test_count_error_precedes_late_epoch_validation(self):
        error=DatabaseServerError('database_operation_failed',code='22003');db=Database(*prefix(epoch=None),dict(invalid=False),error)
        with self.assertRaises(DatabaseError) as result:self.call(db)
        self.assertIs(result.exception,error);self.assert_channel_scope.assert_not_called()
    def test_bad_per_format_boolean_rolls_back_first_job_update_before_channel_write(self):
        error=DatabaseServerError('database_operation_failed',code='22P02')
        rows=tail(all_types=True)[:3]+[error];db=Database(*prefix(all_types=True),dict(invalid=False),1,*rows)
        with self.assertRaises(DatabaseError) as result:self.call(db)
        self.assertIs(result.exception,error);self.assertEqual(db.calls[-1][1],h._FORMATS);self.assertEqual(db.events,['rollback'])
    def test_epoch_is_checked_after_discovery_and_rolls_back_every_effect(self):
        for epoch,accepted in ((None,17),(17,18)):
            db=Database(*prefix(epoch=epoch,accepted_epoch=accepted),dict(invalid=False),*tail())
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call(db)
            self.assertEqual(db.calls[-1][1],h._DISCOVERY_INSERT);self.assertEqual(db.events,['rollback'])
        self.assert_channel_scope.assert_not_called()
    def test_final_generation_fence_failure_has_no_retry(self):
        db=Database(*prefix(),dict(invalid=False),*tail());self.assert_channel_scope.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(DatabaseError):self.call(db)
        self.assertEqual(db.events,['rollback']);self.assertEqual(len(db.operations),1)
    def test_borrowed_failure_poisons_owner_without_independent_transaction(self):
        db=Database(*prefix(),dict(invalid=True))
        with self.assertRaises(DatabaseError):self.call(db,transaction=db)
        self.assertEqual(db.events,['borrow','poison']);self.assertEqual(db.operations,[])
    def test_unknown_commit_does_not_implicitly_reconcile(self):
        class LostAck(Database):
            @contextmanager
            def transaction(self,operation):
                with super().transaction(operation) as tx:yield tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        db=LostAck(*prefix(),dict(invalid=False),*tail())
        with self.assertRaises(DatabaseError) as result:self.call(db)
        self.assertTrue(result.exception.outcome_unknown);self.assertEqual(len(db.operations),1);self.assertEqual(db.events,['commit'])
    def test_operation_preserves_total_retained_timeout_and_error_contract(self):
        db=Database(*prefix(replayed=True));self.call(db);operation=db.operations[0];spec=h.get_operation('verify_initial_hydration_job')
        self.assertEqual((operation.name,operation.statement_timeout_ms,operation.total_timeout_ms,operation.lock_timeout_ms,operation.error_codes),
            (spec.name,spec.statement_timeout_ms,spec.statement_timeout_ms,spec.lock_timeout_ms,spec.error_codes))
    def test_all_fixed_sql_binds_and_passes_real_direct_guard(self):
        returning={h._UPDATE,h._FORMAT_UPDATE}
        for name,sql in vars(h).items():
            if not(name.startswith('_') and isinstance(sql,str) and sql.lstrip().startswith(('SELECT','WITH','INSERT','UPDATE'))):continue
            names=re.findall(r'%\(([^)]+)\)s',sql);params={key:None for key in names} if names else (None,)*len(re.findall('%s',sql))
            PostgresQuery(Transformer()).convert(sql,params);is_returning=sql in returning
            read=is_returning or sql.lstrip().startswith(('SELECT','WITH'))
            _reviewed_statement(sql,read=read,max_rows=1 if read else 0,returning=is_returning)
        source=Path(h.__file__).read_text();self.assertNotIn('.call(',source);self.assertNotIn('operator_private.',source)


if __name__=='__main__':unittest.main()
