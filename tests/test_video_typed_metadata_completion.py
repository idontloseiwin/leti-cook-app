"""Offline typed receipt/restoration composition with the real finite driver."""
from pathlib import Path
import json,sys,unittest
from unittest.mock import patch
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_typed_metadata_completion as helper,video_source_invalidation as invalidation
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_collection_policy import rows as cadence_rows,NOW
from test_video_stream_completion import payload,one,CHANNEL
from test_video_missing_completion import incident_results
from db.videos import incident_statement_effects as effects

class TypedMetadataTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        automatic=patch("db.inventory.automatic_type_reconciliation.reconcile_automatic_video_types",return_value=[])
        self.automatic=automatic.start();self.addCleanup(automatic.stop)
        self.profile_patch=patch.object(helper.profile,'complete_video_profile_metadata_collection',side_effect=lambda d,rows,transaction,**kwargs:len(rows))
        self.borrowed=self.profile_patch.start()
        self.addCleanup(patch.stopall)
    def setup_success(self,count=1,owned_count=None,kind='short'):
        owned_count=count if owned_count is None else owned_count
        rows=[dict(payload(f'tm{i:09d}'),video_type=kind,published_at=NOW.isoformat(),extra={'exact':9007199254740993}) for i in range(count)]
        owned=rows[:owned_count];cadence=cadence_rows()
        matches=[i for i,r in enumerate(cadence) if r['video_type']==kind and r['min_age_seconds']==0]
        scopes=[dict(channel_id=CHANNEL,control_epoch=7)] if owned else []
        values=[one(count=count),one(valid=True),Result(tuple(dict(video_id=r['video_id']) for r in rows)),one(valid=True),
            one(mismatch=False),one(indices=list(range(1,count+1))),one(stale=False),Result(tuple(dict(video_id=r['video_id']) for r in owned)),
            one(regression=False),one(mismatch=False),one(ordinals=list(range(1,owned_count+1))),one(scopes=scopes),Result(()),one(rows='[]'),
            Result(),Result(has_columns=False,rowcount=0),
            one(sources=[dict(video_id=r['video_id'],fallback_at=None,matches=matches,as_of_valid=True,
                availability_now=False,availability_checked_at=NOW.isoformat()) for r in owned],cadence=cadence),
            Result(has_columns=False,rowcount=owned_count),one(valid=True),one(valid=True),one(valid=True)]
        if scopes:values.append(one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted'))
        self.connection.results.extend(values);return rows
    def run_helper(self,rows):return helper.complete_typed_video_metadata_collection(self.database,rows)
    def test_receipt_capture_incident_prelock_then_borrowed_profile_then_restoration(self):
        rows=self.setup_success();self.assertEqual(self.run_helper(rows),1)
        sql=[s for s,_ in self.connection.queries]
        for index,fragment in ((2,helper._LOCK),(7,helper._INSERT),(10,helper._OWNED_ORDINALS),(11,helper._SCOPES),
            (12,helper._INCIDENT_LOCK),(13,helper._RESTORED),(14,helper._INCIDENT_EFFECT_UPDATE),(15,helper._EVENTS),(17,helper._COMPLETE)):
            self.assertIn(fragment,sql[index])
        self.assertEqual(self.connection.queries[17][1]['max_interval_seconds'],2419200)
        self.assertEqual(len(sql),22);self.assertIn('FOR SHARE',sql[-1])
        self.borrowed.assert_called_once();args=self.borrowed.call_args
        self.assertIsNone(args.args[0]);self.assertIs(args.args[1][0],rows[0])
        self.assertIs(args.kwargs['transaction']._connection,self.connection)
        self.assertIs(args.kwargs['_reconcile_type'],False)
        self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
    def test_automatic_policy_runs_once_after_typed_settlement_and_before_scope(self):
        rows=self.setup_success();observed=[]
        def reconcile(tx,ids):
            observed.append(list(self.connection.queries))
            self.assertEqual(ids,[rows[0]['video_id']])
            self.assertIs(tx._connection,self.connection)
            self.assertEqual(self.connection.commits,0)
            return []
        self.automatic.side_effect=reconcile
        self.run_helper(rows)
        self.assertEqual(len(observed),1)
        self.assertIn(helper._COMPLETE,[sql for sql,_ in observed[0]])
        self.assertEqual(len(observed[0]),len(self.connection.queries)-1)
        self.assertIn('FOR SHARE',self.connection.queries[-1][0])

    def test_one_and_fifty_rows_have_constant_outer_statement_count(self):
        for count in (1,50):
            start=len(self.connection.queries);self.assertEqual(self.run_helper(self.setup_success(count)),count)
            self.assertEqual(len(self.connection.queries)-start,22)
            self.assertEqual(len(self.borrowed.call_args.args[1]),count)
    def test_real_profile_and_base_composition_are_constant_and_share_one_transaction(self):
        from test_video_metadata_completion import source
        self.profile_patch.stop()
        for count in (1,50):
            rows=self.setup_success(count);outer=list(self.connection.results);self.connection.results.clear()
            # Typed rows never enter the Long-only restoration owner set.
            inner=[one(valid=True),one(count=0),*(one(valid=True) for _ in range(3)),
                one(mask='0'*count,count=0),one(count=0),*(one(valid=True) for _ in range(3)),
                one(count=0,stale=False,rows='[]'),one(scopes=[])]
            inner += [one(valid=True),one(count=count),one(valid=True),one(valid=True),
                Result(tuple(dict(video_id=r['video_id']) for r in rows)),one(valid=True),one(valid=True),one(stale=False),
                one(count=count,rows=rows,scopes=[dict(channel_id=CHANNEL,control_epoch=7)]),
                one(count=count,snapshot_text=json.dumps([dict(video_id=r['video_id'],source_existed=True,checked_at=None,metadata_checked_at=None) for r in rows])),
                Result(has_columns=False,rowcount=count),one(present=count,required=False),Result(has_columns=False,rowcount=count),
                one(sources=[source(r['video_id']) for r in rows],cadence=cadence_rows()),
                Result(has_columns=False,rowcount=count),one(valid=True),one(valid=True),
                one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted')]
            inner += [one(valid=True) for _ in range(9)]
            self.connection.results.extend(outer[:14]+inner+outer[14:])
            start=len(self.connection.queries);borrows=self.pool.borrowed;commits=self.connection.commits
            self.assertEqual(self.run_helper(rows),count)
            sql=[q for q,_ in self.connection.queries[start:]]
            self.assertEqual(len(sql),61);self.assertEqual(self.pool.borrowed-borrows,1)
            self.assertEqual(self.connection.commits-commits,1)
            self.assertEqual(sql.count(helper.profile.metadata._UPSERT),1)
            source_index=sql.index(helper.profile.metadata._UPSERT)
            self.assertEqual(sql[source_index-1],f'SELECT * FROM ({invalidation._CAPTURE}) AS lic_bounded_result LIMIT 2')
            self.assertEqual(sql[source_index+1],f'SELECT * FROM ({invalidation._FACTS}) AS lic_bounded_result LIMIT 2')
            self.assertEqual(sql.count(helper.profile.metadata._SNAPSHOTS),1)
            self.assertLess(next(i for i,q in enumerate(sql) if helper._RESTORED in q),sql.index(helper.profile.metadata._UPSERT))
            self.assertLess(sql.index(helper.profile.metadata._COMPLETE),next(i for i,q in enumerate(sql) if helper._INCIDENT_EFFECT_UPDATE in q))
            self.assertLess(next(i for i,q in enumerate(sql) if helper._INCIDENT_EFFECT_UPDATE in q),sql.index(helper._COMPLETE))
            self.assertFalse(self.connection.results)
    def test_actual_short_and_stream_restoration_receipts_are_consumed_before_events(self):
        for kind in ('short','stream'):
            rows=self.setup_success(2,kind=kind)
            values=list(self.connection.results)
            values[14:15]=incident_results([r['video_id'] for r in rows],kind=kind,closed=True)
            self.connection.results.clear();self.connection.results.extend(values)
            start=len(self.connection.queries)
            self.assertEqual(self.run_helper(rows),2)
            sql=[q for q,_ in self.connection.queries[start:]]
            self.assertEqual(len(sql),23)
            self.assertIn(helper._INCIDENT_EFFECT_UPDATE,sql[14]);self.assertIn(effects._FACTS,sql[15])
            self.assertEqual(sql[16],helper._EVENTS)
            self.assertFalse(self.connection.results)

    def test_empty_and_nonarray_fail_before_mutation(self):
        self.connection.results.append(one(count=0));self.assertEqual(self.run_helper([]),0)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(None)
        self.assertEqual(caught.exception.message,'typed_video_metadata_completion_requires_array')
        self.assertEqual(len(self.connection.queries),1);self.borrowed.assert_not_called()
    def test_cast_error_precedes_fifty_cap(self):
        self.connection.results.append(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*51)
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
        self.connection.results.append(one(count=51))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload()]*51)
        self.borrowed.assert_not_called()
    def test_native_null_type_and_single_type_conditions_remain_exact(self):
        self.assertIn("video_type NOT IN ('short','stream')",helper._VALID)
        self.assertNotIn('video_type IS NULL',helper._VALID)
        self.assertIn('count(DISTINCT video_type)',helper._VALID)
        self.assertIn("video_type='stream' AND (stream_actual_end_at IS NULL",helper._VALID)
        self.assertIn("video_type='short' AND stream_actual_end_at IS NOT NULL",helper._VALID)
    def test_new_receipt_observation_regression_rolls_back_before_any_profile_call(self):
        rows=self.setup_success();self.connection.results[8]=one(regression=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'typed_completion_observation_clock_regression')
        self.assertEqual(len(self.connection.queries),9);self.borrowed.assert_not_called()
    def test_exact_replay_skips_borrowed_writer_and_keeps_all_three_final_clocks(self):
        rows=self.setup_success(owned_count=0);self.assertEqual(self.run_helper(rows),1)
        self.assertEqual(len(self.connection.queries),21);self.borrowed.assert_not_called()
        self.assertTrue(all(not call.args[1] for call in self.automatic.call_args_list))
        self.assertEqual(self.connection.queries[-4][1]['owned_ids'],[])
        self.assertEqual(self.connection.queries[-3][1][0].obj,rows)
        self.assertEqual(self.connection.queries[-2][1][0].obj,rows)
        self.assertEqual(self.connection.queries[-1][1][0].obj,rows)
    def test_owned_original_ordinals_preserve_numbers_and_sorted_borrow_order(self):
        rows=self.setup_success(2);self.connection.results[10]=one(ordinals=[2,1]);self.run_helper(rows)
        owned=self.borrowed.call_args.args[1]
        self.assertIs(owned[0],rows[1]);self.assertIs(owned[1],rows[0])
        self.assertEqual(owned[0]['extra']['exact'],9007199254740993)
        self.assertIn('ORDER BY input.video_id',helper._OWNED_ORDINALS)
    def test_restoration_is_captured_before_borrowed_profile_clears_leases(self):
        rows=self.setup_success();restored='[{"incident_id":9007199254740993}]';self.connection.results[13]=one(rows=restored)
        def writer(*args,**kwargs):
            self.assertEqual(len(self.connection.queries),14)
            self.assertIn(helper._RESTORED,self.connection.queries[-1][0]);return 1
        self.borrowed.side_effect=writer;self.run_helper(rows)
        self.assertEqual(self.connection.queries[14][1],(restored,));self.assertEqual(self.connection.queries[15][1],(restored,))
        self.assertIn('incident.last_seen_at<=coalesce(input.metadata_observed_at,input.recorded_at)',helper._RESTORED)
        self.assertIn("coalesce(restored.availability_status,'public')",helper._RESTORE)
        self.assertNotIn('nullif',helper._RESTORE+helper._EVENTS)
    def test_borrowed_count_mismatch_rolls_back_receipts_before_history_mutation(self):
        rows=self.setup_success();self.borrowed.return_value=0;self.borrowed.side_effect=None
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'typed_video_metadata_completion_count_mismatch')
        self.assertEqual(len(self.connection.queries),14);self.assertEqual(self.connection.commits,0)
    def test_settlement_count_guard_rolls_back_history_and_source(self):
        rows=self.setup_success();self.connection.results[17]=Result(has_columns=False,rowcount=0)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'typed_completion_owned_settlement_count_mismatch')
        self.assertEqual(len(self.connection.queries),18);self.assertEqual(self.connection.commits,0)
    def test_final_companion_clock_error_precedes_final_channel_lock(self):
        rows=self.setup_success();self.connection.results[19]=one(valid=False)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.code,'22023');self.assertEqual(len(self.connection.queries),20)
        self.assertEqual(self.connection.commits,0)
    def test_policy_clock_error_preserves_first_source_order(self):
        source=dict(video_id='x',fallback_at='nativeclock',matches=[],as_of_valid=False,availability_now=False,availability_checked_at=None)
        later=dict(source,fallback_at=None,as_of_valid=True)
        with self.assertRaises(DatabaseServerError) as caught:helper._schedules(dict(sources=[source,later],cadence=[]))
        self.assertEqual(caught.exception.message,'hot_video_refresh_as_of_invalid')
        with self.assertRaises(DatabaseServerError) as caught:helper._schedules(dict(sources=[later,source],cadence=[]))
        self.assertEqual(caught.exception.message,'typed_video_collection_cadence_missing')
    def test_native_typed_anchor_calendar_and_availability_ceiling_are_not_python_dates(self):
        self.assertIn("input.video_type='stream' THEN input.stream_actual_end_at ELSE input.published_at",helper._POLICY)
        self.assertIn('make_interval(days=>',helper._POLICY);self.assertIn('make_interval(secs=>%(max_interval_seconds)s::integer)',helper._COMPLETE)
        self.assertIn('state.lease_token IS NULL OR state.lease_token=input.lease_token',helper._COMPLETE)
    def test_typed_settlement_preserves_generic_counter_deadline_and_schedules_metadata(self):
        # The borrowed profile writer already schedules counters for this type.
        # Typed metadata owns only its separate metadata clock and final minimum.
        self.assertNotIn('stats_due_at=NULL',helper._COMPLETE)
        self.assertIn('metadata_due_at=least(due.metadata_due_at,state.availability_resume_observed_at)',helper._COMPLETE)
        self.assertIn('next_due_at=least(state.stats_due_at,due.metadata_due_at,state.availability_resume_observed_at)',helper._COMPLETE)
    def test_caught_borrowed_domain_error_poisons_outer_transaction(self):
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation('typed_metadata_test')) as tx:
                with self.assertRaises(DatabaseServerError):helper.complete_typed_video_metadata_collection(None,None,transaction=tx)
        self.assertEqual(caught.exception.message,'database_transaction_aborted')
    def test_unknown_commit_does_not_retry_borrowed_writer(self):
        rows=self.setup_success();self.connection.commit_error=psycopg.OperationalError('private')
        with self.assertRaises(DatabaseUnavailable) as caught:self.run_helper(rows)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1);self.borrowed.assert_called_once()

if __name__=='__main__':unittest.main()
