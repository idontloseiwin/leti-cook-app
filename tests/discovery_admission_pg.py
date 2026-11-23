"""Durable admission on an isolated PostgreSQL 17 fixture, never YouTube."""
from pathlib import Path
import sys
import unittest
from dataclasses import replace
from uuid import uuid4

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).resolve().parent)]
import research_query_pg as native
from db.discovery_budget import DiscoveryBudgetRepository
from discovery_policy import DiscoverySettings


class DiscoveryBudgetPostgres(unittest.TestCase):
    setUpClass=classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass=classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh=classmethod(native.NativeQueryTests.refresh.__func__)

    def setUp(self):
        self.project='discovery-test-'+str(uuid4())
        self.settings=replace(DiscoverySettings(),events_general_daily_limit=4,
                              events_general_window_limit=3,full_general_daily_limit=2,full_general_window_limit=1)
        self.budget=DiscoveryBudgetRepository(self.db,project_id=self.project,settings=self.settings,interval_minutes=10)

    def token(self):return str(uuid4())

    def test_01_full_signal_consumes_two_limits_one_grant(self):
        token=self.token()
        self.assertTrue(self.budget.reserve(token,bucket='general',event=True,full=True)['allowed'])
        row=self.admin.execute('SELECT event_used,full_used,window_event_used,window_full_used FROM public.discovery_budget_days WHERE project_id=%s',(self.project,)).fetchone()
        self.assertEqual(row,(1,1,1,1))
        self.assertTrue(self.budget.begin_dispatch(token))
        self.assertFalse(self.budget.begin_dispatch(token))
        self.assertEqual(self.budget.reserve(self.token(),bucket='general',full=True)['reason'],'window_budget')
        self.assertTrue(self.budget.reserve(self.token(),bucket='general')['allowed'])

    def test_02_restart_and_unknown_reservation_ack_never_refill(self):
        token=self.token()
        self.assertTrue(self.budget.reserve(token,bucket='general')['allowed'])
        other=DiscoveryBudgetRepository(self.db,project_id=self.project,settings=self.settings,interval_minutes=30)
        self.assertTrue(other.reserve(token,bucket='general')['allowed'])
        self.assertEqual(self.admin.execute('SELECT event_used,extract(epoch FROM window_end-window_start) FROM public.discovery_budget_days WHERE project_id=%s',(self.project,)).fetchone()[0],1)
        with self.assertRaises(ValueError):other.reserve(token,bucket='general',full=True)
        self.assertTrue(other.begin_dispatch(token))
        self.assertFalse(other.reserve(token,bucket='general')['allowed'])

    def test_03_window_settings_do_not_change_open_window(self):
        self.budget.reserve(self.token(),bucket='general')
        more=DiscoveryBudgetRepository(self.db,project_id=self.project,settings=DiscoverySettings(),interval_minutes=30)
        self.assertTrue(more.reserve(self.token(),bucket='general')['allowed'])
        self.assertTrue(more.reserve(self.token(),bucket='general')['allowed'])
        self.assertEqual(more.reserve(self.token(),bucket='general')['reason'],'window_budget')
        self.assertEqual(self.admin.execute('SELECT event_limit,window_event_limit FROM public.discovery_budget_days WHERE project_id=%s',(self.project,)).fetchone(),(4,3))

    def test_04_exhaustion_blocks_preissued_and_future_grants_only_same_bucket(self):
        token=self.token();self.budget.reserve(token,bucket='general')
        self.budget.note_exhausted('general')
        self.assertFalse(self.budget.begin_dispatch(token))
        self.assertEqual(self.budget.reserve(self.token(),bucket='general')['reason'],'provider_quota')
        self.assertTrue(self.budget.blocked('general'))
        self.assertFalse(self.budget.blocked('stats'))
        self.assertTrue(self.budget.reserve(self.token(),bucket='stats')['allowed'])

    def test_05_expired_grant_cannot_dispatch(self):
        token=self.token();self.budget.reserve(token,bucket='general')
        self.admin.execute("UPDATE public.discovery_budget_grants SET expires_at=now()-interval '1 second' WHERE grant_id=%s",(token,))
        self.assertFalse(self.budget.begin_dispatch(token))
        self.assertEqual(self.admin.execute('SELECT event_used FROM public.discovery_budget_days WHERE project_id=%s',(self.project,)).fetchone()[0],1)

    def test_06_campaign_limit_spans_windows_and_days(self):
        campaign=self.token()
        self.admin.execute("""INSERT INTO public.discovery_repair_campaigns
          (campaign_id,definition_digest,definition_version,window_start,window_end,target_limit,general_limit,statistics_limit)
          VALUES(%s,%s,1,now()-interval '7 days',now(),300,1,1)""",(campaign,uuid4().hex+uuid4().hex))
        self.assertTrue(self.budget.reserve(self.token(),bucket='general',campaign_id=campaign)['allowed'])
        self.assertEqual(self.budget.reserve(self.token(),bucket='general',campaign_id=campaign)['reason'],'campaign_budget')
        self.assertTrue(self.budget.reserve(self.token(),bucket='stats',campaign_id=campaign)['allowed'])

    def test_08_targeted_hydration_owner_and_original_source_clocks(self):
        from discovery_hydration_pg import run_checks
        result=run_checks(self.admin,self.db)
        self.assertGreaterEqual(result['checks'],12)

    def test_07_background_full_does_not_debit_events(self):
        self.assertTrue(self.budget.reserve(self.token(),bucket='general',event=False,full=True)['allowed'])
        self.assertEqual(self.admin.execute('SELECT event_used,full_used FROM public.discovery_budget_days WHERE project_id=%s',(self.project,)).fetchone(),(0,1))

    def test_09_storage_generation_repair_and_crash_fences(self):
        from discovery_storage_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),20)

    def test_10_new_short_end_to_end_no_real_provider(self):
        from functools import partial
        from hashlib import sha256
        from datetime import timedelta
        from discovery_hydration_pg import CHANNEL
        from discovery_service import DiscoveryService
        from db.discovery import DiscoveryRepository
        from db.inventory.typed_inventory import TypedInventoryRepository
        from db.videos.video_collection import VideoCollectionRepository
        from video_collection_service import VideoCollectionService,YouTubeViewCountAbsent,YouTubeViewCountInvalid
        from collection.video_payloads import build_video_collection_metadata_payload,build_video_counter_payload
        from youtube_source_fields import parse_dt
        self.admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending'")
        identity='fxendtoend1'
        self.admin.execute("INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind) VALUES(%s,%s,%s,'entry')",(sha256(identity.encode()).hexdigest(),CHANNEL,identity))
        now=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        item=dict(id=identity,snippet=dict(channelId=CHANNEL,title='Synthetic Short',publishedAt=(now-timedelta(minutes=5)).isoformat()),
                  contentDetails=dict(duration='PT1M'),status=dict(privacyStatus='public',uploadStatus='processed'),
                  statistics=dict(viewCount='12',likeCount='1',commentCount='0'))
        calls=[]
        def metadata(ids,phase):
            self.assertEqual(ids,[identity]);calls.append(('metadata',tuple(ids)));return [item]
        def membership(playlist,video):
            self.assertEqual((playlist,video),('UUSH'+CHANNEL[2:],None))
            calls.append(('membership',video));return {'items':[{'contentDetails':{'videoId':identity}}]}
        def forbidden(*args,**kwargs):raise AssertionError('unexpected_provider_request')
        video=VideoCollectionService(VideoCollectionRepository(self.db),fetch_full=forbidden,fetch_metadata=metadata,
            fetch_counters=forbidden,fetch_initial_profiles=forbidden,
            build_full_payload=partial(build_video_collection_metadata_payload,batch_stats_enabled=True,parse_dt=parse_dt,
                view_count_absent_error=YouTubeViewCountAbsent,view_count_invalid_error=YouTubeViewCountInvalid),
            build_counter_payload=build_video_counter_payload,is_quota_exhausted=lambda e:False)
        discovery=DiscoveryRepository(self.db)
        # The parent fixture is a newly created disposable cluster. Activation
        # readiness has its own real-gate tests; this case tests ID hydration.
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,clock_timestamp())")
        TypedInventoryRepository(self.db).prepare_runtime(limit=1000,enable=True)
        service=DiscoveryService(discovery,video,self.budget,settings=self.settings,fetch_membership=membership,
                                 worker_id='Discovery end-to-end',run_id=self.token())
        result=service.run_once()
        self.assertEqual(result['settled'],1,result)
        self.assertEqual(len(calls),2,calls)
        self.assertEqual(self.admin.execute('SELECT status,outcome FROM public.websub_events WHERE video_id=%s',(identity,)).fetchone(),('done','writer_receipt'))
        self.assertEqual(self.admin.execute('SELECT video_type FROM public.video_inventory WHERE video_id=%s',(identity,)).fetchone(),('short',))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.discovery_budget_grants WHERE project_id=%s',(self.project,)).fetchone()[0],2)

    def test_11_concurrent_reservations_cannot_overdraw_window(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes=list(pool.map(lambda _:self.budget.reserve(self.token(),bucket='general'),range(8)))
        self.assertEqual(sum(result['allowed'] for result in outcomes),3)
        self.assertEqual(self.admin.execute('SELECT event_used,window_event_used FROM public.discovery_budget_days WHERE project_id=%s',(self.project,)).fetchone(),(3,3))

    def test_12_channel_delete_cleans_addressed_receipts_in_bounded_batches(self):
        from discovery_retention_pg import run_checks
        result=run_checks(self.admin,self.db)
        self.assertGreaterEqual(len(result),6)

    def test_13_count_heads_respect_enabled_formats_and_failure_backoff(self):
        from discovery_counts_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),18)

    def test_14_cancelled_campaign_cannot_use_preissued_grant(self):
        campaign=self.token()
        self.admin.execute("""INSERT INTO public.discovery_repair_campaigns
          (campaign_id,definition_digest,definition_version,window_start,window_end,target_limit,general_limit,statistics_limit)
          VALUES(%s,%s,1,now()-interval '7 days',now(),300,1,1)""",(campaign,uuid4().hex+uuid4().hex))
        token=self.token()
        self.assertTrue(self.budget.reserve(token,bucket='general',campaign_id=campaign)['allowed'])
        self.admin.execute("UPDATE public.discovery_repair_campaigns SET state='cancelled' WHERE campaign_id=%s",(campaign,))
        self.assertFalse(self.budget.begin_dispatch(token))

    def test_15_shared_head_and_deep_id_preserve_exact_events(self):
        from discovery_coalescing_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),14)

    def test_16_operational_cleanup_preserves_pending_work_and_spend(self):
        from discovery_operational_retention_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),12)

    def test_17_counter_changes_do_not_replace_complete_coverage(self):
        from discovery_coverage_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),20)

    def test_18_fresh_delivery_matches_migrated_public_schema(self):
        import psycopg
        from psycopg import sql
        name='discovery_fresh_'+uuid4().hex
        self.admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            with psycopg.connect(self.admin.info.dsn,dbname=name,
                    password=native.os.environ['LIC_QUERY_TEST_PASSWORD'],autocommit=True) as fresh:
                fresh.execute('CREATE SCHEMA extensions; CREATE EXTENSION pgcrypto WITH SCHEMA extensions',prepare=False)
                fresh.execute((native.ROOT/'schema/schema.sql').read_text(),prepare=False)
                state=fresh.execute('SELECT initial_version,current_version FROM operator_private.schema_state').fetchone()
                from schema_release import load_schema_release
                current = load_schema_release().schema_version
                self.assertEqual(state,(current,current))
                for view in ('readable.library_format_coverage','readable.library_manual_list_check'):
                    view_sql = 'SELECT pg_get_viewdef(%s::regclass)'
                    self.assertEqual(fresh.execute(view_sql,(view,)).fetchone(),self.admin.execute(view_sql,(view,)).fetchone())
                self.assertEqual(fresh.execute('SELECT enabled,first_activated_at FROM public.discovery_runtime_control').fetchone(),(False,None))
                # Independent delivery paths must agree on column shape, defaults,
                # RLS and effective runtime permissions; OIDs and row data differ.
                query="""SELECT c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),
                    a.attnotnull,pg_get_expr(d.adbin,d.adrelid),c.relrowsecurity,
                    has_column_privilege('service_role',c.oid,a.attnum,'SELECT'),
                    has_column_privilege('service_role',c.oid,a.attnum,'INSERT'),
                    has_column_privilege('service_role',c.oid,a.attnum,'UPDATE'),
                    has_column_privilege('lic_reader',c.oid,a.attnum,'SELECT')
                  FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
                  LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
                  WHERE c.relnamespace='public'::regnamespace AND c.relkind IN('r','v')
                    AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"""
                self.assertEqual(fresh.execute(query).fetchall(),self.admin.execute(query).fetchall())
                from db.base.postgres import PostgresDatabase,PostgresConfig
                from psycopg.conninfo import make_conninfo
                from db.readiness.video_failure_capabilities import assert_video_failure_ready
                from db.readiness.capabilities import assert_typed_inventory_canonical_repair_schema
                dsn=make_conninfo(host='127.0.0.1',port=native.os.environ['LIC_QUERY_TEST_PORT'],
                                 dbname=name,user='authenticator',
                                 password=native.os.environ['LIC_QUERY_TEST_PASSWORD'])
                runtime=PostgresDatabase(PostgresConfig(dsn,max_size=1),admission=lambda _:None).open()
                try:
                    assert_video_failure_ready(runtime)
                    assert_typed_inventory_canonical_repair_schema(runtime)
                finally:
                    runtime.close()
        finally:
            self.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))

    def test_19_existing_readiness_accepts_new_exact_queue_permissions(self):
        from db.readiness.video_failure_capabilities import assert_video_failure_ready
        assert_video_failure_ready(self.db)


    def test_20_obligations_stay_isolated(self):
        from discovery_reconciliation_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),25)

    def test_21_activation_requires_real_current_coverage(self):
        from discovery_activation_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),18)

    def test_22_late_counter_and_disabled_stream_credit(self):
        from discovery_count_settlement_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),7)

    def test_23_paused_campaigns_keep_debt_and_resume_budget(self):
        from discovery_recovery_campaign_pg import run_checks
        self.assertGreaterEqual(len(run_checks(self.admin,self.db)),25)


if __name__=='__main__':unittest.main(verbosity=2)
