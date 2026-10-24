"""Offline profile wrapper ownership, batch bounds and atomic restoration order."""
from pathlib import Path
import json,sys,unittest
from unittest.mock import patch
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_profile_completion as helper,video_source_invalidation as invalidation
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_video_counter_completion import CHANNEL,one
import test_video_metadata_completion as base_tests
from test_video_metadata_completion import payload
from test_video_missing_completion import incident_results
from db.videos import incident_statement_effects as effects, video_queue_initialization as initialization

class ProfileCompletionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        automatic=patch("db.inventory.automatic_type_reconciliation.reconcile_automatic_video_types",return_value=[])
        self.automatic=automatic.start();self.addCleanup(automatic.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def prefix(self,count=1,*,restore=True,owned=True,stale=False):
        scopes=[dict(channel_id=CHANNEL,control_epoch=7)] if owned else []
        captured=json.dumps([dict(video_id=f'vm{i:09d}',incident_id=i+1,restored_at='2026-10-06T00:00:00+00:00',latest_status='private') for i in range(min(count,1000))]) if count<=1000 else None
        self.queue(one(valid=True),one(count=count),*(one(valid=True) for _ in range(3)),
            one(mask=('1' if owned else '0')*count,count=count if owned else 0),one(count=count if restore else 0),
            *(one(valid=True) for _ in range(3)),one(count=count if restore else 0,stale=stale,rows=captured),one(scopes=scopes))
        return scopes
    def setup_success(self,count=1,*,restore=True,replay=False):
        scopes=self.prefix(count,restore=restore,owned=not replay)
        rows=base_tests.MetadataCompletionTests.setup_success(self,count,owned_count=0 if replay else count)
        self.queue(*(one(valid=True) for _ in range(3)))
        if restore:self.queue(*incident_results([r['video_id'] for r in rows],kind='long',closed=True),Result(has_columns=False,rowcount=count),*(one(valid=True) for _ in range(3)))
        else:self.queue(*(one(valid=True) for _ in range(6)))
        if scopes:self.queue(one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted'))
        return rows
    def run_helper(self,rows):return helper.complete_video_profile_metadata_collection(self.database,rows)
    def test_atomic_borrowed_base_then_restoration_event_and_final_fence(self):
        rows=self.setup_success();self.assertEqual(self.run_helper(rows),1)
        sql=[q for q,_ in self.connection.queries]
        self.assertEqual(len(sql),40);self.assertIn(helper._LOCK,sql[1])
        self.assertIn(helper._OWNED,sql[5]);self.assertIn(helper._INCIDENT_LOCK,sql[6]);self.assertIn(helper._CAPTURE,sql[10]);self.assertIn(helper._SCOPES,sql[11])
        self.assertEqual(sql[21:25],[f'SELECT * FROM ({invalidation._CAPTURE}) AS lic_bounded_result LIMIT 2',helper.metadata._UPSERT,f'SELECT * FROM ({invalidation._FACTS}) AS lic_bounded_result LIMIT 2',helper.metadata._SNAPSHOTS])
        self.assertIn(helper._INCIDENT_EFFECT_UPDATE,sql[33]);self.assertIn(effects._FACTS,sql[34])
        self.assertNotIn(initialization._LONG_UPSERT,sql)
        self.assertEqual(sql[35],helper._EVENTS);self.assertIn('FOR SHARE',sql[-1])
        self.assertFalse(self.connection.results)
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(1,0,1))
    def test_automatic_policy_runs_after_source_settlement_before_final_fence(self):
        rows=self.setup_success();observed=[]
        def reconcile(tx,ids):
            observed.append(list(self.connection.queries))
            self.assertIs(tx._connection,self.connection)
            self.assertEqual(ids,[row['video_id'] for row in rows])
            self.assertEqual(self.connection.commits,0)
            return []
        self.automatic.side_effect=reconcile
        self.run_helper(rows)
        self.assertEqual(len(observed),1)
        self.assertEqual(len(observed[0]),len(self.connection.queries)-1)
        self.assertIn(helper.metadata._COMPLETE,[sql for sql,_ in observed[0]])
        self.assertIn('FOR SHARE',self.connection.queries[-1][0])

    def test_one_and_thousand_rows_use_the_same_forty_queries(self):
        for count in (1,1000):
            start=len(self.connection.queries);rows=self.setup_success(count)
            self.assertEqual(self.run_helper(rows),count);self.assertEqual(len(self.connection.queries)-start,40)
    def test_profile_presence_and_array_shape_precede_native_identity_casts(self):
        self.queue(one(valid=False))
        with self.assertRaises(DatabaseServerError):self.run_helper([dict(payload(),youtube_topic_categories=None,lease_token='bad')])
        self.assertEqual(len(self.connection.queries),1)
        self.assertIn("NOT(raw.value ? 'youtube_topic_categories')",helper._PROFILE_ARRAY)
    def test_queue_and_inventory_locks_precede_clock_validation(self):
        self.queue(one(valid=True),Result(execute_error=ServerFailure('55P03')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(recorded_at='infinity')])
        self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(self.connection.queries),2)
        self.assertIn('FOR UPDATE OF state,inventory',helper._LOCK);self.assertNotIn('SKIP LOCKED',helper._LOCK)
    def test_all_three_clock_passes_repeat_after_incident_locks(self):
        rows=self.setup_success();self.run_helper(rows);sql=[q for q,_ in self.connection.queries]
        self.assertEqual(sql[2:5],sql[7:10]);self.assertIn(helper._RESTORED_CLOCKS,sql[4])
        self.assertIn('FOR UPDATE OF incident',helper._INCIDENT_LOCK)
    def test_mask_reconstructs_original_owned_rows_without_receipt_reread(self):
        rows=self.setup_success(2);self.run_helper(rows)
        for index in (6,10,11):self.assertEqual(self.connection.queries[index][1]['mask'],'11')
        self.assertNotIn('video_snapshots',helper._RESTORE_INPUT);self.assertNotIn('video_collection_state',helper._RESTORE_INPUT)
        self.assertIn('WITH ORDINALITY',helper._OWNED);self.assertIn('input.ordinality',helper._OWNED)
    def test_stale_restoration_fails_before_base_casts_and_writes(self):
        self.prefix(stale=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(caption_available='invalid')])
        self.assertEqual(caught.exception.message,'typed_long_availability_restore_stale_observation')
        self.assertEqual(len(self.connection.queries),11);self.assertEqual(self.connection.commits,0)
    def test_oversized_transfers_no_restoration_rows_and_reaches_original_base_cap(self):
        self.prefix(1001);self.queue(one(valid=True),one(count=1001))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*1001)
        self.assertEqual(caught.exception.code,'P0001');self.assertEqual(len(self.connection.queries),14)
        self.assertIn('jsonb_array_length(%(rows)s::jsonb)<=1000',helper._CAPTURE)
        self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q,_ in self.connection.queries))
    def test_oversized_stale_and_native_cast_failures_still_precede_count_rejection(self):
        self.prefix(1001,stale=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*1001)
        self.assertEqual(caught.exception.message,'typed_long_availability_restore_stale_observation');self.connection.results.clear()
        self.prefix(1001);self.queue(one(valid=True),Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(caption_available='invalid')]*1001)
        self.assertEqual(caught.exception.code,'22P02')
    def test_no_restoration_repeats_two_additional_final_clock_triples(self):
        rows=self.setup_success(restore=False);self.assertEqual(self.run_helper(rows),1)
        sql=[q for q,_ in self.connection.queries];self.assertEqual(len(sql),40)
        self.assertEqual(sum(helper._RESTORED_CLOCKS in q for q in sql),5)
        self.assertNotIn(helper._RESTORE,sql);self.assertNotIn(helper._EVENTS,sql)
    def test_receipt_replay_never_restores_incidents_or_releases_replacement_owner(self):
        rows=self.setup_success(restore=False,replay=True);self.assertEqual(self.run_helper(rows),1)
        self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q,_ in self.connection.queries))
        self.assertEqual(len(self.connection.queries),34)
        self.automatic.assert_not_called()
    def test_native_history_defaults_preserve_existing_channel_and_exact_status(self):
        self.assertIn('coalesce(incident.channel_id,restored.channel_id)',helper._RESTORE)
        self.assertIn("coalesce(nullif(input.availability_status,''),'public')",helper._RESTORE_INPUT)
        self.assertIn("coalesce(nullif(input.availability_source,''),'video_stats_sync')",helper._RESTORE_INPUT)
        self.assertIn("coalesce(nullif(restored.latest_status,''),'unknown_unavailable')",helper._EVENTS)
        self.assertNotIn('last_seen_at=',helper._RESTORE);self.assertNotIn('attempt_count=',helper._RESTORE)
    def test_restoration_or_final_clock_failure_rolls_back_base_writes(self):
        for index,result in ((35,Result(execute_error=ServerFailure('23514'))),(38,one(valid=False))):
            rows=self.setup_success();self.connection.results[index]=result
            with self.assertRaises(DatabaseServerError):self.run_helper(rows)
            self.connection.results.clear()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,2))
    def test_borrowed_error_poison_and_unknown_commit_are_not_replayed(self):
        with self.assertRaises(DatabaseError):
            with self.database.transaction(Operation('profile_completion_test')) as tx:
                with self.assertRaises(DatabaseServerError):helper.complete_video_profile_metadata_collection(None,None,transaction=tx)
        rows=self.setup_success();self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseUnavailable) as caught:self.run_helper(rows)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,2)
    def test_empty_array_keeps_native_wrapper_stages_without_writes(self):
        self.prefix(0,restore=False,owned=False)
        self.queue(one(valid=True),one(count=0),*(one(valid=True) for _ in range(9)))
        self.assertEqual(self.run_helper([]),0);self.assertEqual(len(self.connection.queries),23)
        self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q,_ in self.connection.queries))
    def test_no_business_rpc_temp_table_or_additional_owner(self):
        code=(ROOT/'application/db/videos/video_profile_completion.py').read_text(encoding='utf-8')
        self.assertNotIn('.call(',code);self.assertNotIn('CREATE TEMP',code)
        self.assertIn('metadata.complete_video_metadata_collection(None,rows,transaction=tx)',code)

if __name__=='__main__':unittest.main()
