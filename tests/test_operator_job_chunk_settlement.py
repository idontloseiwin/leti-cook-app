"""Ordered settlement protocol, opaque native JSON and borrowed ownership."""
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import sys,unittest
from psycopg.types.json import Jsonb
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_chunk_settlement as helper
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL
TOKEN='f8100000-0000-4000-8000-000000000002'
NOW=datetime(2026,3,8,9,30,tzinfo=timezone.utc)
RAW='{"opaque":123456789012345678901234567890.0123456789}'


def arguments(**changes):
    return dict(job_id=ID,lease_token=TOKEN,observed_quota_units=2,error_code=None,summary=RAW,**changes)
def held(**changes):
    value=dict(job=RAW,job_type='channel_current_stats_backfill',phase='video_targets',version=0,all_types=False,
        channel_id=CHANNEL,epoch=17,accepted_epoch=17,valid=True);value.update(changes);return value
def result(status='queued',raw=RAW):return dict(job=raw,value=dict(id=ID,channel_id=CHANNEL,status=status))
def invoke(db,hydration=False,**changes):
    args=dict(job_id=ID,lease_token=TOKEN,observed_quota_units=2,error_code=None,summary={'opaque':'native'})
    args.update(changes)
    return (helper.settle_initial_hydration_job_chunk if hydration else helper.settle_operator_job_chunk)(db,**args)


class ChunkSettlementTests(unittest.TestCase):
    def setUp(self):
        self.fence_patch=patch.object(helper,'assert_channel_scope');self.fence=self.fence_patch.start();self.addCleanup(self.fence_patch.stop)
        self.scope_patch=patch.object(helper,'assert_full_initial_scope');self.scope=self.scope_patch.start();self.addCleanup(self.scope_patch.stop)
    def database(self,*,hydration=False,version=0,all_types=False,quota=False,status='queued',held_changes=None):
        owner=held(version=version,all_types=all_types)
        if hydration:owner.update(job_type='channel_initial_long_inventory',phase='hydrate_videos')
        owner.update(held_changes or {})
        answers=[arguments()]
        if hydration:answers.extend([dict(owner),dict(invalid_units=False,invalid_error=False)])
        answers.append(owner)
        if version==1:answers.extend([dict(invalid=False),dict(ids=['one',None,'two'])])
        if all_types:answers.append(0)
        answers.extend([0,0])
        if all_types:answers.append(0)
        answers.append(dict(quota_error=quota))
        if quota:answers.append(0)
        answers.extend([0,dict(pending_count=1,updated_count=2,skipped_count=3,unavailable_count=4,error_count=5)])
        if version==1:answers.append(dict(summary=RAW))
        answers.extend([dict(next_status=status,as_of=NOW),result(status)])
        if all_types:answers.extend([dict(formats='{"long":{"native":1}}'),result(status,'{"after_formats":true}')])
        if hydration and status=='completed':answers.append(result('queued'))
        return Database(*answers)
    def test_single_type_settlement_keeps_exact_order_and_one_total_budget(self):
        db=self.database();value=invoke(db)
        self.assertEqual(value,result()['value'])
        self.assertEqual([call[1] for call in db.calls],[helper._INPUT,helper._JOB,helper._REFRESHED,helper._UNAVAILABLE,
            helper._QUOTA,helper._EXHAUSTED,helper._COUNTS,helper._DECIDE,helper._UPDATE])
        self.fence.assert_called_once_with(db,CHANNEL,17);self.scope.assert_not_called();self.assertEqual(db.events,['commit'])
        spec=db.operations[0];self.assertEqual((spec.statement_timeout_ms,spec.lock_timeout_ms,spec.total_timeout_ms),(60000,0,60000))
    def test_all_format_full_chunk_order_and_scope_are_bounded_set_statements(self):
        db=self.database(hydration=True,version=1,all_types=True);invoke(db,True)
        sql=[call[1] for call in db.calls]
        self.assertEqual(sql,[helper._INPUT,helper._JOB,helper._HYDRATION_VALID,helper._JOB,helper._CHUNK_VALID,helper._CHUNK,
            helper._IDENTITY,helper._REFRESHED,helper._UNAVAILABLE,helper._CHECKED,helper._QUOTA,helper._EXHAUSTED,
            helper._COUNTS,helper._FULL_SUMMARY,helper._DECIDE,helper._UPDATE,helper._FORMATS,helper._FORMAT_UPDATE])
        self.scope.assert_called_once_with(db,RAW,True);self.assertEqual(db.calls[6][2]['chunk'],['one',None,'two'])
        self.assertEqual(len(sql),18);self.assertIn('jsonb_array_length',helper._CHUNK_VALID);self.assertIn("CASE WHEN j.job_type='channel_initial_long_inventory' THEN 400 ELSE 250 END",helper._CHUNK_VALID)
    def test_unstarted_initial_targets_refund_before_exhaustion_and_are_not_persisted(self):
        db=self.database(hydration=True,version=1,all_types=True,quota=True)
        # Refund is the only extra statement when an explicit unstarted list is present.
        db.answers.insert(12,0)
        invoke(db,True,summary={'hydration_unstarted_video_ids':['one','two'],'kept':7},error_code='youtube_quota_exhausted')
        sql=[call[1] for call in db.calls]
        self.assertLess(sql.index(helper._UNSTARTED_REFUND),sql.index(helper._EXHAUSTED))
        self.assertEqual(db.calls[0][2][-1].obj,{'kept':7})
        args=db.calls[sql.index(helper._UNSTARTED_REFUND)][2]
        self.assertEqual(args['unstarted'],['one','two'])
        self.assertIn('NOT target.video_id=ANY(%(unstarted)s::text[])',helper._REFUND)
        self.assertEqual(db.events,['commit'])

    def test_unstarted_scope_rejects_foreign_or_duplicate_ids_without_target_writes(self):
        for ids in (['one','one'],['foreign'],[None],'one',list(map(str,range(401)))):
            db=self.database(hydration=True,version=1,all_types=True)
            with self.assertRaisesRegex(DatabaseError,'operator_job_unstarted_scope_invalid'):
                invoke(db,True,summary={'hydration_unstarted_video_ids':ids})
            self.assertNotIn(helper._IDENTITY,[call[1] for call in db.calls])
            self.assertEqual(db.events,['rollback'])
        db=self.database()
        with self.assertRaisesRegex(DatabaseError,'operator_job_unstarted_scope_invalid'):
            invoke(db,summary={'hydration_unstarted_video_ids':['one']})
        self.assertEqual(db.events,['rollback'])

    def test_rate_refund_requires_durable_target_failure_and_does_not_use_daily_reset(self):
        db=self.database()
        db.answers[0]['error_code']='video_collection_rate_limited'
        # Only a known rate adds the addressed durable-state refund statement.
        db.answers.insert(5,0)
        with patch.object(helper,'youtube_quota_reset_at') as reset:
            invoke(db,error_code='video_collection_rate_limited')
        reset.assert_not_called()
        sqls=[s for _,s,_ in db.calls]
        self.assertLess(sqls.index(helper._RATE_REFUND),sqls.index(helper._EXHAUSTED))
        self.assertNotIn(helper._REFUND,sqls)
        self.assertTrue(db.calls[-1][2]['rate_limited'])
        self.assertFalse(db.calls[-1][2]['quota_error'])
        for proof in ("state.last_error_type='provider_rate_limited'",
                      "state.updated_at>=target.updated_at","settlement_unknown"):
            self.assertIn(proof,helper._RATE_REFUND)
        self.assertIn("THEN now()+interval '1 minute'",helper._UPDATE)

    def test_hydration_completed_routes_to_verify_before_final_scope(self):
        db=self.database(hydration=True,status='completed');value=invoke(db,True)
        self.assertEqual(value['status'],'queued');self.assertEqual(db.calls[-1][1],helper._VERIFY)
        self.assertIn("phase='verify'",helper._VERIFY);self.assertIn("'current_profile_ready',false",helper._VERIFY)
        self.fence.assert_called_once_with(db,CHANNEL,17)
    def test_settlement_never_checks_lease_deadline_and_null_token_is_native(self):
        self.assertNotIn('lease_expires_at',helper._JOB)
        self.assertIn('IS NOT DISTINCT FROM %(lease_token)s::uuid',helper._JOB)
        db=self.database();db.answers[0]['lease_token']=None;invoke(db,lease_token=None)
        self.assertIsNone(db.calls[1][2]['lease_token']);self.assertEqual(db.events,['commit'])
    def test_native_argument_error_precedes_even_missing_job(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:invoke(db,job_id='bad',observed_quota_units=-1)
        self.assertIs(caught.exception,error);self.assertEqual(len(db.calls),1);self.assertEqual(db.events,['rollback'])
    def test_lease_and_hydration_route_validation_precede_units(self):
        for owner in (None,held(valid=False),held(job_type='channel_current_stats_backfill'),held(job_type='channel_initial_long_inventory',phase='verify')):
            db=Database(arguments(),owner)
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):invoke(db,True,observed_quota_units=-1)
            self.assertEqual(len(db.calls),2)
    def test_hydration_counter_error_order_precedes_second_native_job_read(self):
        for invalid_units,invalid_error,message in ((True,True,'operator_job_hydration_settlement_invalid'),(False,True,'operator_job_error_code_invalid')):
            db=Database(arguments(),held(job_type='channel_initial_long_inventory',phase='hydrate_videos'),dict(invalid_units=invalid_units,invalid_error=invalid_error))
            with self.assertRaisesRegex(DatabaseError,message):invoke(db,True)
            self.assertEqual(len(db.calls),3)
    def test_general_initial_wrong_phase_differs_from_hydration_wrapper(self):
        db=Database(arguments(),held(job_type='channel_initial_long_inventory',phase='inventory'))
        with self.assertRaisesRegex(DatabaseError,'operator_job_initial_phase_invalid'):invoke(db)
        self.scope.assert_not_called()
    def test_full_scope_and_protected_json_fail_before_target_mutation(self):
        self.scope.side_effect=DatabaseServerError('full_initial_targets_not_frozen',code='P0001')
        db=Database(arguments(),held(job_type='channel_initial_long_inventory',phase='hydrate_videos',version=1,all_types=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_targets_not_frozen'):invoke(db)
        self.assertEqual(len(db.calls),2)
        self.scope.side_effect=None
        db=Database(arguments(),held(version=1),dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_chunk_invalid'):invoke(db)
        self.assertEqual(len(db.calls),3)
    def test_quota_refund_uses_native_suffix_and_captured_transaction_pacific_reset(self):
        db=self.database(quota=True)
        with patch.object(helper,'youtube_quota_reset_at',wraps=helper.youtube_quota_reset_at) as reset:invoke(db,error_code='x_quota_exhausted')
        reset.assert_called_once_with(NOW)
        self.assertEqual(db.calls[5][1],helper._REFUND)
        self.assertEqual(db.calls[-1][2]['reset_at'],datetime(2026,3,9,7,tzinfo=timezone.utc))
        self.assertIn('state.updated_at>=target.updated_at',helper._REFUND)
        self.assertIn('greatest(target.job_attempts-1,0)',helper._REFUND)
    def test_completed_or_blocked_quota_does_not_calculate_reset(self):
        for status in ('completed','blocked'):
            db=self.database(quota=True,status=status)
            with patch.object(helper,'youtube_quota_reset_at') as reset:invoke(db)
            reset.assert_not_called();self.assertIsNone(db.calls[-1][2]['reset_at'])
    def test_opaque_job_and_summary_roundtrip_only_as_native_text(self):
        db=self.database(version=1,all_types=True);invoke(db)
        for kind,sql,args in db.calls[2:]:
            if sql==helper._FORMATS:self.assertEqual(args,{'job':RAW})
            elif sql==helper._FORMAT_UPDATE:self.assertEqual(args,{'job':RAW,'formats':'{"long":{"native":1}}'})
            else:
                self.assertEqual(args['job'],RAW);self.assertEqual(args['summary'],RAW)
        self.assertIn('::jsonb::text AS summary',helper._INPUT)
        self.assertIn('to_jsonb(job)::text AS job',helper._JOB)
    def test_optional_omission_and_explicit_sql_or_json_null_remain_distinct(self):
        for value in ('omitted',None,Jsonb(None),{},['array'],7):
            db=self.database()
            if value=='omitted':helper.settle_operator_job_chunk(db,ID,TOKEN)
            else:helper.settle_operator_job_chunk(db,ID,TOKEN,summary=value)
            args=db.calls[0][2];self.assertEqual(args[2:4],(0,None))
            if value is None:self.assertIsNone(args[-1])
            elif value=='omitted':self.assertEqual(args[-1].obj,{})
            elif isinstance(value,Jsonb):self.assertIs(args[-1],value)
            else:self.assertEqual(args[-1].obj,value)
    def test_legacy_scalar_and_json_null_never_dispatch_full_summary_subtraction(self):
        for summary in ('7','null','true','"text"'):
            db=self.database();db.answers[0]['summary']=summary;invoke(db)
            self.assertNotIn(helper._FULL_SUMMARY,[sql for kind,sql,args in db.calls])
            self.assertEqual(db.calls[-1][2]['summary'],summary)
        # The incoming scalar must not occur in a planner-foldable JSONB minus
        # expression, including an untaken CASE arm in the job UPDATE.
        self.assertNotIn("%(summary)s::jsonb-",helper._UPDATE)
        self.assertEqual(helper._FULL_SUMMARY,"SELECT (%(summary)s::jsonb-'full_hydration_chunk')::text AS summary")
    def test_full_summary_native_failure_stays_after_counts_before_decision(self):
        db=self.database(version=1,all_types=True);failure=DatabaseServerError('database_operation_failed',code='22023')
        # Input/job, shape/chunk, four target updates, quota, exhausted, counts.
        db.answers[11]=failure
        with self.assertRaises(DatabaseError) as caught:invoke(db)
        self.assertIs(caught.exception,failure)
        self.assertEqual([sql for kind,sql,args in db.calls][-2:],[helper._COUNTS,helper._FULL_SUMMARY])
        self.fence.assert_not_called();self.assertEqual(db.events,['rollback'])
    def test_native_failure_after_target_updates_precedes_late_epoch_check(self):
        for code,at in (('22P02',5),('22003',8)):
            db=self.database(held_changes={'epoch':None});failure=DatabaseServerError('database_operation_failed',code=code)
            db.answers[at]=failure
            with self.assertRaises(DatabaseError) as caught:invoke(db)
            self.assertIs(caught.exception,failure);self.assertEqual(db.events,['rollback'])
        self.fence.assert_not_called()
    def test_late_epoch_and_final_scope_roll_back_already_settled_targets(self):
        db=self.database(held_changes={'accepted_epoch':18})
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):invoke(db)
        self.assertEqual(db.calls[-1][1],helper._UPDATE);self.fence.assert_not_called()
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001');db=self.database()
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):invoke(db)
        self.assertEqual(db.events,['rollback']);self.assertEqual(len(db.operations),1)
    def test_borrowed_failure_poisons_parent_without_opening_nested_transaction(self):
        db=Database(arguments(),None)
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):invoke(None,transaction=db)
        self.assertEqual(db.events,['borrow','poison']);self.assertEqual(db.operations,[])
    def test_native_result_json_and_all_format_casts_are_left_to_postgresql(self):
        self.assertIn("(entry.value->>'current_profile_ready')::boolean",helper._FORMAT_UPDATE)
        self.assertIn("(%(summary)s::jsonb->>'settlement_unknown')::boolean",helper._EXHAUSTED)
        self.assertIn("j.summary_json ? 'full_retry_after'",helper._DECIDE)
        self.assertIn('quota_units_observed_estimate+greatest(coalesce',helper._UPDATE)
        self.assertIn("THEN 'full_hydration_chunk' ELSE '__unused__' END",helper._UPDATE)
    def test_fixed_statements_bind_and_no_business_rpc_is_retained(self):
        args=dict(job_id=ID,lease_token=TOKEN,observed_quota_units=2,error_code=None,summary=RAW,job=RAW,chunk=[],unstarted=[],quota_error=False,rate_limited=False,
            next_status='queued',reset_at=None,pending_count=1,updated_count=0,skipped_count=0,unavailable_count=0,error_count=0,formats='{}')
        reads={'_INPUT','_JOB','_HYDRATION_VALID','_CHUNK_VALID','_CHUNK','_QUOTA','_COUNTS','_FULL_SUMMARY','_DECIDE','_FORMATS'}
        returning={'_UPDATE','_FORMAT_UPDATE','_VERIFY'}
        for name in reads|returning|{'_IDENTITY','_REFRESHED','_UNAVAILABLE','_CHECKED','_REFUND','_RATE_REFUND','_UNSTARTED_REFUND','_EXHAUSTED'}:
            sql=getattr(helper,name);read=name in reads or name in returning
            _reviewed_statement(sql,read=read,max_rows=1 if read else 0,returning=name in returning)
            PostgresQuery(Transformer()).convert(sql,(ID,TOKEN,2,None,Jsonb({})) if name=='_INPUT' else args)
        source=Path(helper.__file__).read_text()
        for forbidden in ('.call(', 'public.youtube_quota_reset_at(', 'operator_private.settle_operator_job_chunk_provisional('):self.assertNotIn(forbidden,source)


if __name__=='__main__':unittest.main()
