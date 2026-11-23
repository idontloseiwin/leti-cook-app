"""PostgreSQL owners in one dispatch window; every provider is synthetic.

Explicit disposable runner only. The manual lane uses bounded descriptors,
not manual-job state; canonical video/source ownership uses the actual DB.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from functools import partial
from hashlib import sha256
from pathlib import Path
from threading import Event, get_ident
from types import ModuleType
from unittest.mock import patch
from uuid import uuid4
import sys
import unittest
import json
import os
import time
from tempfile import TemporaryDirectory

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).parent)]
import research_query_pg as native
import collection_shutdown
from collection.budgets import PageBudget
from collection.cycle import AutomaticDispatchWindow
from collection.discovery_feeder import DiscoveryFeeder
from collection.inventory import run_typed_inventory_claim
from collection.inventory_admission import InventoryDiscoveryAdmission
from collection.inventory_feeder import AutoInventoryCursor, AutoInventoryFeeder
from collection.provider_steps import ProviderStep
from collection.video_payloads import build_video_collection_metadata_payload, build_video_counter_payload
from db.base.postgres import Operation
from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_budget import DiscoveryBudgetRepository
from db.discovery_dispatch import release_claims
from db.discovery_runtime import runtime_status
from db.inventory.search_projection import SearchProjectionRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_collection import VideoCollectionRepository
from db.videos.video_queue_initialization import ensure_video_collection_state_in_transaction
from discovery_policy import DiscoverySettings
from discovery_service import DiscoveryService
from research_query.service import QueryService
from research_query.validation import canonical
from test_discovery_dispatch import Clock, ManualTurn
from test_research_query_contract import query
from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_source_fields import parse_dt


class OwnerDatabase:
    """Assert domain DB access before delegating to the real gateway."""
    def __init__(self, database):
        self.database, self.owner, self.calls = database, get_ident(), []

    def __getattr__(self, name):
        value = getattr(self.database, name)
        if not callable(value):
            return value
        def owned(*args, **kwargs):
            if get_ident() != self.owner:
                raise AssertionError('domain_database_called_in_provider_thread')
            self.calls.append(name)
            return value(*args, **kwargs)
        return owned


class DiscoveryDispatchPostgres(unittest.TestCase):
    def test_current_schema_passes_actual_hot_runtime_contract(self):
        from db.readiness.capabilities import get_hot_runtime_contract
        contract=get_hot_runtime_contract(self.db)
        self.assertTrue(contract['schema_ready'])

    @classmethod
    def setUpClass(cls):
        native.NativeQueryTests.setUpClass.__func__(cls)
        from schema_release import load_schema_release
        release = load_schema_release()
        manifest = json.loads((Path(__file__).resolve().parents[1]/'schema/manifest.json').read_text())
        if manifest['schema_version'] != release.schema_version or manifest['baseline_id'] != release.baseline_id:
            raise RuntimeError('native_release_metadata_mismatch')
        # bootstrap has applied every manifest DDL step; complete the updater's
        # release bookkeeping before exercising the real runtime readiness gates.
        cls.admin.execute('UPDATE operator_private.schema_state SET current_version=%s',
                          (release.schema_version,))
    tearDownClass = classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh = classmethod(native.NativeQueryTests.refresh.__func__)

    def scalar(self, statement, parameters=()):
        row = self.admin.execute(statement, parameters).fetchone()
        return None if row is None else row[0]

    def now(self):
        return self.scalar('SELECT clock_timestamp()')

    def channel(self, *, permission="accepted"):
        identity = 'UC' + uuid4().hex[:22]
        ChannelSyncRepository(self.owned).insert_channel(dict(channel_id=identity,
            title='Discovery native synthetic', user_input='https://www.youtube.com/channel/' + identity,
            initial_long_inventory_status='completed', initial_28d_stats_status='completed',
            video_count=10, channel_metadata_status='ok',
            channel_metadata_checked_at=(self.now()-timedelta(hours=1)).isoformat()), permission=permission)
        job = str(uuid4())
        self.admin.execute("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,initial_scope_version,
          initial_video_types,max_items,max_pages,max_quota_units,accepted_epoch,error_count)
          VALUES(%s,'channel_initial_long_inventory',%s,'completed','verify',0,ARRAY['long'],2500,50,100,1,0)""", (job, identity))
        self.admin.execute('UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s', (job, identity))
        return identity

    def event(self, identity):
        self.admin.execute("""INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind)
          VALUES(%s,%s,%s,'entry')""", (sha256((identity+str(uuid4())).encode()).hexdigest(), self.target, identity))

    def cover(self, identity, kind):
        epoch = self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s', (identity,))
        claims = self.typed.claim_channels([identity], video_types=[kind], worker_id='discovery-full-fixture',
            run_id=str(uuid4()), expected_channel_scopes=[dict(channel_id=identity, control_epoch=epoch)])
        self.assertEqual(len(claims), 1)
        result = run_typed_inventory_claim(repository=self.typed, claim=claims[0], fetch_page=lambda *a, **kw: {'items': []})
        self.assertEqual(result['status'], 'completed')

    def test_actual_window_writer_query_and_late_wake(self):
        self.owned = OwnerDatabase(self.db)
        self.target, ordinary = self.channel(), self.channel()
        self.typed = TypedInventoryRepository(self.owned)
        self.typed.prepare_runtime(limit=1000, enable=True)
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        # Real complete source receipts grant the existing format readiness.
        self.cover(self.target, 'long'); self.cover(self.target, 'short')
        self.admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now()+interval '1 day',source_next_after=now()+interval '1 day'")
        self.admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now(),source_next_after=now() WHERE channel_id=%s AND video_type='long'", (ordinary,))
        # An ordinary due stats row is deliberately outside initial demand.
        unrelated = 'a0000000001'
        with self.owned.transaction(Operation('ensure_video_collection_state')) as tx:
            ensure_video_collection_state_in_transaction(tx, [unrelated])
        self.admin.execute("UPDATE public.video_collection_state SET discovery_hydration_requested_at=NULL")
        self.admin.execute("UPDATE public.video_collection_state SET next_due_at=now(),stats_due_at=now(),metadata_due_at=now() WHERE video_id=%s", (unrelated,))
        before = self.admin.execute('SELECT next_due_at,stats_due_at,metadata_due_at,last_attempt_at,lease_token FROM public.video_collection_state WHERE video_id=%s', (unrelated,)).fetchone()
        ids = ['fxevent000A', 'fxevent000E']
        events, observed = [], {}
        owner = get_ident()
        published = (self.now()-timedelta(minutes=5)).isoformat()
        def forbidden(*args, **kwargs):
            raise AssertionError('unrelated_provider_request')
        def metadata(requested, reason):
            self.assertNotEqual(get_ident(), owner)
            self.assertTrue(set(requested) <= set(ids))
            events.append(('metadata', tuple(requested)))
            return [dict(id=identity, snippet=dict(channelId=self.target, title='Discovery Short '+identity, publishedAt=published),
                contentDetails=dict(duration='PT1M'), status=dict(privacyStatus='public', uploadStatus='processed'),
                statistics=dict(viewCount='12', likeCount='1', commentCount='0')) for identity in requested]
        def membership(playlist, identity):
            self.assertNotEqual(get_ident(), owner)
            self.assertEqual(playlist, 'UUSH'+self.target[2:])
            self.assertIsNone(identity)
            listed = next(group for kind, group in reversed(events) if kind == 'metadata')
            events.append(('membership', playlist))
            return {'pageInfo': {'totalResults': len(listed)}, 'items': [
                {'contentDetails': {'videoId': item, 'videoPublishedAt': published}} for item in listed]}
        def ordinary_page(playlist, **options):
            self.assertNotEqual(get_ident(), owner)
            self.assertEqual(playlist, 'UULF'+ordinary[2:])
            events.append(('ordinary', playlist))
            return {'items': []}
        video = VideoCollectionService(VideoCollectionRepository(self.owned), fetch_full=forbidden,
            fetch_metadata=metadata, fetch_counters=forbidden, fetch_initial_profiles=forbidden,
            build_full_payload=partial(build_video_collection_metadata_payload, batch_stats_enabled=True,
                parse_dt=parse_dt, view_count_absent_error=YouTubeViewCountAbsent,
                view_count_invalid_error=YouTubeViewCountInvalid),
            build_counter_payload=build_video_counter_payload, is_quota_exhausted=lambda error:False)
        settings, project = DiscoverySettings(), 'discovery-native-'+str(uuid4())
        budget = DiscoveryBudgetRepository(self.owned, project_id=project, settings=settings, interval_minutes=15)
        repository = DiscoveryRepository(self.owned)
        collector = DiscoveryService(repository, video, budget, settings=settings, fetch_membership=membership,
            worker_id='Discovery native dispatch', run_id=str(uuid4()))
        clock, revision = Clock(), [0]
        def ready(): self.assertEqual(get_ident(), owner)
        def manual(**kwargs): return ManualTurn(**kwargs, count=3, events=events)
        window = AutomaticDispatchWindow(concurrency=1, require_ready=ready, should_stop=lambda:False,
            operator_factory=manual, clock=clock, wake_revision=lambda:revision[0])
        self.addCleanup(window.close)
        feeder = DiscoveryFeeder(collector, deadline=100, require_ready=ready, should_stop=window.pass_stopping,
            clock=clock, on_settled=window.operator_stopped, release_claims=partial(release_claims, self.owned))
        window.discovery = feeder
        inventory = AutoInventoryFeeder(self.typed, page_budget=PageBudget(1), cursor=AutoInventoryCursor(),
            candidate_scan_limit=1000, run_id=str(uuid4()), worker_id='Discovery ordinary page', fetch_page=ordinary_page,
            require_ready=ready, concurrency=1, clock=clock, formats=('long','short'),
            discovery_admission=InventoryDiscoveryAdmission(budget), on_settled=window.stop_all)
        execute = ProviderStep.execute
        def captured(step):
            response = execute(step)
            if step.reason == 'discovery_metadata':
                for identity in step.identities: observed[identity] = response.observed_at
            return response
        self.event(ids[0])
        with patch.object(collection_shutdown, '_requested', Event()), patch.object(ProviderStep, 'execute', captured):
            window.run_phase(inventory)
            first = window.run_phase(feeder, wait_operator=True)
            self.assertEqual(first['settled'], 1, first)
            self.assertTrue(feeder.exhausted)
            executor = window.executor
            self.event(ids[1]); revision[0] += 1; clock.now += 3
            second = window.run_phase(feeder)
            self.assertEqual(second['settled'], 2, second)
            self.assertIs(window.executor, executor)
            self.assertEqual(feeder.deadline, 100)
            window.close()
        self.assertTrue(window.checkpoint_ready)
        kinds = [event[0] for event in events]
        self.assertIn('ordinary', kinds)
        self.assertEqual(kinds.count('manual'), 3)
        self.assertLess(kinds.index('manual'), kinds.index('membership'))
        for identity in ids:
            self.assertEqual(self.admin.execute('SELECT status,outcome FROM public.websub_events WHERE video_id=%s', (identity,)).fetchone(), ('done','writer_receipt'))
            self.assertEqual(self.scalar('SELECT metadata_checked_at FROM public.videos WHERE video_id=%s', (identity,)), observed[identity])
            self.assertEqual(self.scalar('SELECT count(*) FROM readable.videos WHERE video_id=%s', (identity,)), 1)
        self.assertEqual(self.admin.execute('SELECT next_due_at,stats_due_at,metadata_due_at,last_attempt_at,lease_token FROM public.video_collection_state WHERE video_id=%s', (unrelated,)).fetchone(), before)
        # Explicit projection refresh, followed by the actual shared query API.
        projection = SearchProjectionRepository(self.owned)
        for _ in range(20):
            if not projection.refresh(): break
        result = QueryService(self.repo, lambda:self.context).execute(canonical(query(
            scope={'kind':'channel','id':self.target}, video_types=['short'], select=['video.id','video.title'],
            order_by=[{'field':'video.id','direction':'asc'}])))
        self.assertEqual([row['id'] for row in result['rows']], ids)
        self.assertEqual(kinds.count('membership'), 2)
        epoch = self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s', (self.target,))
        def observed_count(count):
            at = self.now().isoformat()
            ChannelSyncRepository(self.owned).update_metadata_batch([dict(channel_id=self.target, control_epoch=epoch,
                values=dict(video_count=count, channel_metadata_checked_at=at, channel_metadata_status='ok',
                            channel_video_count_changed_at=at))])
            status = runtime_status(self.owned, project_id=project, settings=settings, interval_minutes=15)
            self.assertTrue(status['enabled'])
            self.assertFalse(status['budget_wait'])
            return status['runnable']

        tokens = '''SELECT count_reconcile_token, count_reconcile_settled_token FROM public.channels
                    WHERE channel_id=%s'''
        self.assertFalse(observed_count(12))  # Both new IDs already have writer receipts.
        explained, settled = self.admin.execute(tokens, (self.target,)).fetchone()
        self.assertIsNotNone(explained)
        self.assertEqual(explained, settled)
        self.assertTrue(observed_count(13))  # A third video has no notification or membership receipt.
        unexplained, settled = self.admin.execute(tokens, (self.target,)).fetchone()
        self.assertIsNotNone(unexplained)
        self.assertNotEqual(unexplained, settled)
        self.assertTrue(self.scalar('''SELECT EXISTS(SELECT 1 FROM public.channel_playlist_state
            WHERE channel_id=%s AND head_signal_token=%s)''', (self.target, unexplained)))
        self.assertTrue(self.owned.calls)

    def test_process_scope_narrow_composition_reaches_real_transport_boundary(self):
        """Actual composition, while HOT/shared-permit dependencies are fixtures.

        This is deliberately not an import/bootstrap latency benchmark. The
        PostgreSQL domain owners, process binding, provider adapters, transport
        validation, discovery grants and common quota guard are all real.
        """
        from collection_schedule import CollectionSchedules
        from runtime.collection_schedules import schedule_environment
        from runtime.process_collection import (process_collection_configuration,
            process_collection_scope, bind_process_common_dispatch)
        from self_hosted_settings import collection_environment
        import quota_buckets
        import requests

        self.owned = OwnerDatabase(self.db)
        self.target = self.channel()
        self.typed = TypedInventoryRepository(self.owned)
        self.typed.prepare_runtime(limit=1000, enable=True)
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
        self.cover(self.target, 'long'); self.cover(self.target, 'short')
        # This scenario starts without another scenario's count/source demand.
        self.admin.execute('UPDATE public.channels SET count_reconcile_settled_token=count_reconcile_token')
        self.admin.execute("""UPDATE public.channel_playlist_state p SET
          count_reconcile_served_token=c.count_reconcile_token,count_reconcile_served_epoch=s.control_epoch,
          full_reconcile_served_token=p.full_reconcile_required_token,
          head_signal_token=NULL,head_next_due_at=now()+interval '1 day',
          next_due_at=now()+interval '1 day',source_next_after=now()+interval '1 day'
          FROM public.channels c JOIN public.channel_collection_state s USING(channel_id)
          WHERE p.channel_id=c.channel_id""")
        self.admin.execute("""UPDATE public.video_collection_state SET discovery_hydration_requested_at=NULL,
            next_due_at=CASE WHEN next_due_at IS NOT NULL THEN now()+interval '2 days' END""")
        identity, project = 'fxbound000A', 'discovery-composition-'+str(uuid4())
        self.event(identity)
        schedules = CollectionSchedules(discovery=DiscoverySettings())
        core = ModuleType('core')
        core.database, core.COLLECTION_SCHEDULES = self.db, schedules
        core.API_KEY, core.YOUTUBE_QUOTA_ATTEMPT_RECORDER = 'synthetic-key', None
        core.YOUTUBE_API_TIMEOUT_SECONDS = 10
        owner, calls, gates = get_ident(), [], []
        published = (self.now()-timedelta(minutes=5)).isoformat()

        def http(url, *, params, **kwargs):
            self.assertNotEqual(get_ident(), owner)
            self.assertTrue(quota_buckets.control_active())
            endpoint = url.rsplit('/', 1)[-1]
            calls.append(endpoint)
            if endpoint == 'videos':
                self.assertEqual(params['id'], identity)
                self.assertIn('statistics', params['part'])
                payload = {'items': [dict(id=identity, snippet=dict(channelId=self.target,
                    title='Discovery bound Short', publishedAt=published), contentDetails=dict(duration='PT1M'),
                    status=dict(privacyStatus='public', uploadStatus='processed'), statistics=dict(viewCount='17'))]}
            elif endpoint == 'playlistItems':
                self.assertEqual(params['playlistId'], 'UUSH'+self.target[2:])
                self.assertNotIn('videoId', params)
                self.assertNotIn('pageToken', params)
                self.assertEqual(params['maxResults'], 50)
                payload = {'items': [{'contentDetails': {'videoId': identity, 'videoPublishedAt': published}}]}
            else:
                raise AssertionError('ordinary_queue_or_unrelated_provider_called')
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(payload).encode()
            response._content_consumed = True
            return response

        def ready(**options): gates.append((get_ident(), options['operation']))
        with TemporaryDirectory(prefix='discovery-composition-') as directory:
            env = {**collection_environment('test', {'inventory_page_budget': 2}),
                **schedule_environment(schedules), 'LIC_COLLECTION_INTERVAL_MINUTES': '15',
                'LIC_COLLECTION_FORMATS': 'long,short', 'LIC_YOUTUBE_REQUEST_CONCURRENCY': '1',
                'YOUTUBE_QUOTA_PROJECT_ID': project, 'OPERATOR_JOB_WAKE_PATH': str(Path(directory)/'wake')}
            with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {'core':core}), \
                 patch.object(collection_shutdown, '_requested', Event()), \
                 patch('hot_runtime.require_hot_runtime_ready', side_effect=ready), \
                 patch('provider_runtime.admit_request', side_effect=lambda **kw:nullcontext(None)), \
                 patch('youtube_client.requests.get', side_effect=http):
                settings, policy = process_collection_configuration()
                with process_collection_scope(settings=settings, policy=policy) as context:
                    self.assertTrue(quota_buckets.control_active())
                    dispatch = bind_process_common_dispatch(context, whole_cycle=True,
                        discovery_deadline=time.monotonic()+5)
                    try:
                        summary = dispatch.narrow()
                        dispatch.finish(summary)
                        self.assertIsNotNone(dispatch.checkpoint())
                    finally:
                        dispatch.close()
                self.assertFalse(quota_buckets.control_active())
        self.assertEqual(summary['errors'], 0, summary)
        self.assertEqual(summary['discovery']['settled'], 1, summary)
        self.assertEqual(calls, ['videos', 'playlistItems'])
        self.assertTrue(any(thread == owner and operation == 'collection' for thread, operation in gates))
        self.assertTrue(any(thread != owner and operation == 'provider' for thread, operation in gates))
        self.assertEqual(self.scalar('SELECT count(*) FROM readable.videos WHERE video_id=%s', (identity,)), 1)
        self.assertEqual(self.admin.execute('SELECT event_used,full_used FROM public.discovery_budget_days WHERE project_id=%s',
            (project,)).fetchone(), (2,0))

    def test_count_growth_preserves_absent_playlist_retry_without_spending_quota(self):
        from dataclasses import replace
        from collection.inventory import run_typed_inventory_batch
        self.owned = OwnerDatabase(self.db)
        self.target = self.channel()
        self.typed = TypedInventoryRepository(self.owned)
        self.typed.prepare_runtime(limit=1000, enable=True)
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
        epoch=self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(self.target,))
        options=dict(video_types=['short'],worker_id='Discovery denied new token',run_id=str(uuid4()),
            expected_channel_scopes=[dict(channel_id=self.target,control_epoch=epoch)])
        old=self.typed.claim_channels([self.target],**options)[0]
        scan=str(uuid4())
        self.typed.begin_scan(old,scan_id=scan,scan_boundary_at=self.now())
        self.typed.fail_scan(old,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
            outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=self.now())
        at=self.now().isoformat()
        ChannelSyncRepository(self.owned).update_metadata_batch([dict(channel_id=self.target,control_epoch=epoch,
            values=dict(video_count=11,channel_metadata_checked_at=at,channel_metadata_status='ok',channel_video_count_changed_at=at))])
        budget=DiscoveryBudgetRepository(self.db,project_id='denied-'+str(uuid4()),interval_minutes=15,
            settings=replace(DiscoverySettings(),full_general_window_limit=1))
        self.assertTrue(budget.reserve(str(uuid4()),bucket='general',full=True,event=False)['allowed'])
        calls=[]
        def head(*args,**kwargs):calls.append(kwargs['page_token']);return {'items':[]}
        summary=run_typed_inventory_batch(repository=self.typed,fetch_page=head,limit=1,
            worker_id='Discovery denied new token',explicit_channel_ids=[self.target],video_types=('short',),
            expected_channel_scopes=options['expected_channel_scopes'],prepare=False,
            discovery_admission=InventoryDiscoveryAdmission(budget))
        self.assertEqual(calls, [])
        self.assertEqual(summary.get('head_observed', 0), 0)
        self.assertEqual(self.admin.execute('SELECT event_used,full_used FROM public.discovery_budget_days WHERE project_id=%s',
            (budget.project,)).fetchone(), (0,1))
        self.assertTrue(self.scalar('''SELECT head_next_due_at>now() FROM public.channel_playlist_state
            WHERE channel_id=%s AND video_type='short' ''', (self.target,)))
        self.assertEqual(self.typed.claim_channels([self.target], only_due=True, **options), [])
        self.admin.execute('''UPDATE public.channel_playlist_state SET head_next_due_at=now(),next_due_at=now(),
            source_next_after=now() WHERE channel_id=%s AND video_type='short' ''', (self.target,))
        subsequent = self.typed.claim_channels([self.target], only_due=True, **options)
        self.assertEqual(len(subsequent), 1)
        self.assertTrue(self.typed.automatic_page_plan(subsequent[0])['head_due'])

    def test_fill_respects_channel_controls_formats_and_publication_in_both_paths(self):
        from db.videos.video_queue_maintenance import backfill_video_collection_state

        self.owned = OwnerDatabase(self.db)
        normal, hidden = self.channel(), self.channel()
        held = self.channel(permission='manual_required')
        self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s', (hidden,))
        repository = VideoCollectionRepository(self.owned)
        self.admin.execute("UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days' WHERE next_due_at IS NOT NULL")
        fixtures = dict(eligible=(normal, 'long'), hidden=(hidden, 'long'), held=(held, 'long'),
                        disabled=(normal, 'short'), paused=(normal, 'long'), waiting=(normal, 'long'))
        scopes = [dict(channel_id=normal, video_type='long')]

        def seed(channel, kind, seconds):
            vid = uuid4().hex[:11]
            now = self.now()
            previous = now-timedelta(days=1)
            due = now+timedelta(seconds=seconds)
            self.admin.execute('''INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at)
                VALUES(%s,%s,%s,%s,%s)''', (vid,channel,kind,previous,previous))
            self.admin.execute('''INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,metadata_checked_at,
                availability_checked_at,availability_status,privacy_status,upload_status,views,like_count,comment_count)
                VALUES(%s,%s,%s,%s,%s,%s,'public','public','processed',1,1,0)''', (vid,channel,previous,previous,previous,previous))
            self.admin.execute('''INSERT INTO public.video_collection_state(video_id,channel_id,video_type,hydration_needed,
                profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,stats_source_checked_at,metadata_source_checked_at)
                VALUES(%s,%s,%s,false,1,%s,%s,%s,%s,%s)''', (vid,channel,kind,due,now+timedelta(days=2),due,previous,previous))
            return vid

        def batch(fill):
            return repository.claim_due_batch(limit=50, worker_id='scheduled fill', run_id=str(uuid4()),
                scopes=scopes, operation='counters', video_types=('long',), **({'fill_final': True} if fill else {}))

        resume_ids = []
        for urgent in (True, False):
            with self.subTest(urgent=urgent):
                # Previous fixtures cannot occupy a place in the next request.
                self.admin.execute("UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days' WHERE next_due_at IS NOT NULL")
                ids = {role:seed(channel,kind,-15 if urgent else 300) for role,(channel,kind) in fixtures.items()}
                self.admin.execute("UPDATE public.video_collection_state SET paused_reason='manual_hold' WHERE video_id=%s",(ids['paused'],))
                self.admin.execute("UPDATE public.videos SET publication_status='upcoming' WHERE video_id=%s",(ids['waiting'],))
                if urgent:
                    rows = repository.claim_due_fill(limit=50, worker_id='urgent fill', run_id=str(uuid4()),
                                                     formats=('long',), excluded=())
                    self.assertEqual({r.video_id:r.collection_mode for r in rows},
                                     {ids['eligible']:'counters',ids['waiting']:'metadata'})
                else:
                    self.assertEqual(batch(False)['rows'], [])
                    early = seed(normal, 'long', 45)
                    first = batch(False)['rows']
                    self.assertEqual([r.video_id for r in first], [early])
                    self.assertEqual(batch(True)['rows'], [])
                    repository.release_discovery_admission(first)
                    result = batch(True)
                    rows = result['rows']
                    self.assertEqual({r.video_id:r.collection_mode for r in rows},
                                     {early:'counters',ids['eligible']:'counters'})
                    self.assertEqual(result['fill_ids'], {ids['eligible']})
                repository.release_discovery_admission(rows)
                for role, reason in [('hidden','channel_hidden'), ('held','channel_inventory_manual_required'),
                                     ('disabled',None), ('paused','manual_hold')]:
                    self.assertEqual(self.admin.execute('''SELECT paused_reason,lease_token FROM public.video_collection_state
                        WHERE video_id=%s''', (ids[role],)).fetchone(), (reason,None))
                resume_ids.extend([ids['hidden'],ids['held']])

        self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s', (hidden,))
        self.admin.execute("UPDATE public.channel_collection_state SET owner_permission='accepted' WHERE channel_id=%s", (held,))
        backfill_video_collection_state(self.owned, limit=1000)
        self.assertEqual(self.scalar('''SELECT count(*) FROM public.video_collection_state WHERE video_id=ANY(%s)
            AND paused_reason IS NULL AND next_due_at IS NOT NULL AND lease_token IS NULL''', (resume_ids,)), len(resume_ids))

    def test_unsent_learned_quota_ban_defers_exact_scan_without_source_failure(self):
        from concurrent.futures import ThreadPoolExecutor
        from collection.dispatch import Completion
        from collection.inventory_dispatch import InventoryPageReservations, InventoryPageUnit
        import quota_buckets
        self.owned=OwnerDatabase(self.db)
        self.target=self.channel()
        self.typed=TypedInventoryRepository(self.owned)
        self.typed.prepare_runtime(limit=1000,enable=True)
        epoch=self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(self.target,))
        budget=DiscoveryBudgetRepository(self.db,project_id='late-ban-'+str(uuid4()),
            settings=DiscoverySettings(),interval_minutes=15)
        calls=[]
        def provider(*args,**kwargs):
            quota_buckets.before_request('general')
            calls.append('HTTP')
            raise AssertionError('banned_request_reached_http')
        def claim_source():
            return self.typed.claim_channels([self.target],video_types=['short'],worker_id='Discovery late ban',
                run_id=str(uuid4()),expected_channel_scopes=[dict(channel_id=self.target,control_epoch=epoch)])[0]
        pages=PageBudget(1)
        unit=InventoryPageUnit(self.typed,key='late-ban',lane='auto',channel=self.target,video_type='short',
            claim_page=claim_source,fetch_page=provider,reservations=InventoryPageReservations(pages),
            require_ready=lambda:None,on_claimed=lambda value:None,on_settled=lambda value:None,
            discovery_admission=InventoryDiscoveryAdmission(budget))
        with patch.object(collection_shutdown,'_requested',Event()), quota_buckets.controlled(self.db,
                project_id=budget.project,settings=budget.settings,interval_minutes=budget.interval):
            unit.prepare()
            scan=unit.prepared.scan_id
            before=self.admin.execute("SELECT source_checked_at,resume_page_token FROM public.channel_video_inventory_scans WHERE scan_id=%s",(scan,)).fetchone()
            budget.note_exhausted('general')
            with ThreadPoolExecutor(max_workers=1) as pool:
                response=pool.submit(unit.fetch,Event()).result(timeout=5)
            unit.settle(Completion('response',value=response,fetch_started=True))
        self.assertEqual(calls,[])
        self.assertEqual((pages.attempts,unit.reservations.reserved),(0,0))
        self.assertEqual(unit.summary['status'],'quota_deferred')
        self.assertEqual(unit.summary['provider_attempts'],0)
        self.assertFalse(unit.summary.get('settlement_unknown'))
        self.assertEqual(self.admin.execute("SELECT source_checked_at,resume_page_token FROM public.channel_video_inventory_scans WHERE scan_id=%s",(scan,)).fetchone(),before)
        self.assertIsNone(self.scalar("SELECT lease_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.typed_inventory_receipts WHERE scan_id=%s AND receipt_scope='scan_transition'",(scan,)),0)
        self.assertEqual(self.scalar("SELECT count(*) FROM public.typed_inventory_receipts WHERE transition_id=%s AND receipt_scope='quota_defer'",(unit.prepared.identity['transition_id'],)),1)


if __name__ == '__main__': unittest.main(verbosity=2)
