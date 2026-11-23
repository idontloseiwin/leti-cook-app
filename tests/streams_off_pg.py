"""On a disposable PG17: a stream-off pass sends YouTube nothing for stream work.

Never auto-discovered; the same disposable-cluster contract as research_query_pg.
One synthetic active channel with due Long-form, Shorts and stream videos (one
stream still live) and due playlists of all three formats. The real collector
owners run as the collector's role (authenticator -> service_role) with FAKE
providers that only record their requests: the unscoped common drain, the dispatched auto feeder, the typed stream
drain, the dispatched and the unaddressed playlist passes, and the
first-format count demand for a missing playlist. Each runs with formats long, short
(the pass snapshot LIC_COLLECTION_FORMATS, as worker_environment writes it)
and, as the positive control, with every format: the same fixture is asked for
streams only then. No network, no key, no YouTube.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from functools import partial
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query_pg import bootstrap
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.budgets import PageBudget
from collection.common_feeder import CommonAutoFeeder
from collection.dispatch import Coordinator, DispatchBlocked
from collection.inventory import run_typed_inventory_batch
from collection.inventory_feeder import AutoInventoryCursor, AutoInventoryFeeder
from collection.video_payloads import build_video_collection_metadata_payload, build_video_counter_payload
from collection_schedule import CollectionSchedules
from db.base.postgres import PostgresConfig, PostgresDatabase
from db.channel.channel_read_policy import CHANNEL_CONTROL_SQL
from db.channel.channel_sync import ChannelSyncRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_collection import VideoCollectionRepository
from video_collection_service import (VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid,
                                      run_claimed_common_video_collection, run_claimed_typed_video_collection)
from youtube_errors import YouTubePlaylistNotFoundError
from youtube_source_fields import parse_dt

CHANNEL = 'UC' + 'streamsoff'.ljust(22, '0')
PREFIX = {'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}
OFF, ALL = 'long,short', 'long,short,stream'
# The pass snapshot worker_environment writes (collection_formats.FORMATS_ENV). Spelled out so that
# this suite also runs where the setting is absent and every format is collected.
FORMATS_ENV = 'LIC_COLLECTION_FORMATS'


class Provider:
    """Records every video and playlist request; answers like an empty, healthy YouTube."""
    def __init__(self):
        self.videos, self.playlists = [], []

    def general(self, ids, phase=None, **_):
        ids = list(ids)
        self.videos.extend(('general', video) for video in ids)
        return [dict(id=video, snippet=dict(channelId=CHANNEL, title='streams-off synthetic',
                                             publishedAt='2026-10-01T03:04:05+00:00'),
                     contentDetails=dict(duration='PT5M'),
                     status=dict(privacyStatus='public', uploadStatus='processed'),
                     statistics=dict(viewCount='23', likeCount='2', commentCount='1')) for video in ids]

    def counters(self, ids, phase=None, **_):
        ids = list(ids)
        self.videos.extend(('stats', video) for video in ids)
        return dict(items=[dict(id=video, views=100, like_count=2, comment_count=0) for video in ids],
                    summary=dict(failed_ids=[]))

    def page(self, playlist_id, page_token=None, *args, **_):
        self.playlists.append(playlist_id)
        return {'items': [], 'pageInfo': {'totalResults': 0, 'resultsPerPage': 50}}


class StreamsOffPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        port, password = int(os.environ['LIC_QUERY_TEST_PORT']), os.environ['LIC_QUERY_TEST_PASSWORD']
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres',
                                    password=password, autocommit=True)
        if not 170000 <= int(cls.admin.execute('SHOW server_version_num').fetchone()[0]) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        cls.admin.row_factory = dict_row
        cls.db = PostgresDatabase(PostgresConfig(
            f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=4),
            cadence_snapshot=CollectionSchedules().cadence_snapshot(), admission=lambda _: None).open()
        result = ChannelSyncRepository(cls.db).insert_channel({
            'channel_id': CHANNEL, 'title': 'Streams off synthetic',
            'user_input': 'https://www.youtube.com/channel/' + CHANNEL,
            'initial_long_inventory_status': 'completed', 'initial_28d_stats_status': 'completed'},
            permission='accepted')
        assert isinstance(result, dict), result
        # Past its initial load (collection mode 'active').
        job = str(uuid.uuid4())
        cls.admin.execute("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,initial_scope_version,
            initial_video_types,max_items,max_pages,max_quota_units,accepted_epoch,error_count)
            VALUES(%s,'channel_initial_long_inventory',%s,'completed','verify',0,ARRAY['long'],2500,50,100,1,0)""",
                          (job, CHANNEL))
        cls.admin.execute('UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s', (job, CHANNEL))
        mode = cls.admin.execute('SELECT collection_mode,typed_runtime_eligible FROM (' + CHANNEL_CONTROL_SQL
                                 + ') p WHERE channel_id=%s', (CHANNEL,)).fetchone()
        assert mode == {'collection_mode': 'active', 'typed_runtime_eligible': True}, mode
        cls.epoch = cls.admin.execute('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',
                                      (CHANNEL,)).fetchone()['control_epoch']

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    # ------------------------------------------------------------------ fixture
    def setUp(self):
        self.ids = {kind: [] for kind in PREFIX}
        with self.admin.transaction():
            for table in ('video_collection_state', 'video_inventory_memberships', 'videos', 'video_inventory'):
                self.admin.execute(f'DELETE FROM public.{table} WHERE channel_id=%s', (CHANNEL,))
            now = self.admin.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            old = now - timedelta(days=2)
            for kind, prefix in PREFIX.items():
                scan = uuid.uuid4()
                playlist = prefix + CHANNEL[2:]
                self.admin.execute("""INSERT INTO public.channel_video_inventory_scans
                    (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,
                     source_checked_at,completed_at)
                    VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s)""",
                    (scan, CHANNEL, kind, playlist, old, old, old, old))
                # Every playlist due now (as after a WebSub notification or its schedule).
                self.admin.execute("""INSERT INTO public.channel_playlist_state
                    (channel_id,video_type,playlist_id,runtime_enabled,status,inventory_status,last_completed_scan_id,
                     source_checked_at,inventory_completed_at,next_due_at,source_next_after,priority)
                    VALUES(%s,%s,%s,true,'ready','completed',%s,%s,%s,%s,%s,0)
                    ON CONFLICT(channel_id,video_type) DO UPDATE SET playlist_id=excluded.playlist_id,runtime_enabled=true,
                    status='ready',inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id,
                    source_checked_at=excluded.source_checked_at,inventory_completed_at=excluded.inventory_completed_at,
                    next_due_at=excluded.next_due_at,source_next_after=excluded.source_next_after,priority=0,
                    active_scan_id=NULL,lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,
                    lease_channel_epoch=NULL,last_error_code=NULL""",
                    (CHANNEL, kind, playlist, scan, old, old, now - timedelta(hours=1), now - timedelta(hours=1)))
                for index in range(3):
                    video = f'so{kind[:2]}{index}{uuid.uuid4().hex[:6]}'
                    self.ids[kind].append(video)
                    published = old - timedelta(days=10 + index)
                    # The last stream is still live: its next check is a lifecycle probe.
                    ended = published + timedelta(hours=2) if kind == 'stream' and index < 2 else None
                    self.admin.execute("""INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,
                        published_acquired_at) VALUES(%s,%s,%s,%s,%s)""", (video, CHANNEL, kind, published, old))
                    self.admin.execute("""INSERT INTO public.video_inventory_memberships
                        (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,
                         last_present_at,evidence_source)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')""",
                        (video, CHANNEL, kind, playlist, published, old, now - timedelta(hours=1), now - timedelta(hours=1)))
                    self.admin.execute("""INSERT INTO public.videos(video_id,channel_id,title,published_at,duration,
                        checked_at,metadata_checked_at,availability_checked_at,availability_status,privacy_status,
                        upload_status,views,like_count,comment_count,stream_actual_end_at)
                        VALUES(%s,%s,'synthetic',%s,'PT5M',%s,%s,%s,'public','public','processed',1,1,0,%s)""",
                        (video, CHANNEL, published, old, old, old, ended))
                    self.admin.execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
                        hydration_needed,profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,
                        stats_source_checked_at,metadata_source_checked_at,typed_source_published_at,
                        typed_source_published_acquired_at,typed_stream_actual_end_at,typed_source_stream_end_acquired_at,
                        typed_collection_status)
                        VALUES(%s,%s,%s,false,1,now()-interval '1 hour',now()-interval '2 hours',now()-interval '2 hours',
                        %s,%s,%s,%s,%s,%s,%s)""",
                        (video, CHANNEL, kind, old, old,
                         published if kind != 'long' else None, old if kind != 'long' else None,
                         ended if kind == 'stream' else None, old if kind == 'stream' and ended else None,
                         'pending' if kind != 'long' else None))

    def stream_ids(self):
        return set(self.ids['stream'])

    @contextmanager
    def formats(self, value):
        with patch.dict(os.environ, {FORMATS_ENV: value}):
            yield

    def service(self, provider):
        repository = VideoCollectionRepository(self.db)
        service = VideoCollectionService(repository, fetch_full=provider.general, fetch_metadata=provider.general,
            fetch_initial_profiles=provider.general, fetch_counters=provider.counters,
            build_full_payload=partial(build_video_collection_metadata_payload, batch_stats_enabled=True, parse_dt=parse_dt,
                                       view_count_absent_error=YouTubeViewCountAbsent,
                                       view_count_invalid_error=YouTubeViewCountInvalid),
            build_counter_payload=build_video_counter_payload, is_quota_exhausted=lambda error: False)
        return repository, service

    def drive(self, feeder):
        with ThreadPoolExecutor(max_workers=2) as executor:
            coordinator = Coordinator(executor, 2)
            for _ in range(2000):
                feeder.offer_to(coordinator)
                try:
                    coordinator.step()
                except DispatchBlocked:
                    self.fail('dispatch blocked')
                snap = coordinator.snapshot()
                if feeder.exhausted and not snap.ready and not snap.held:
                    break
                coordinator.wait(0.01)
            feeder.finish_pass(coordinator)

    def requested(self, provider):
        return {video for _, video in provider.videos}

    def leases(self, kind):
        return self.admin.execute("""SELECT count(*) AS n FROM public.video_collection_state WHERE channel_id=%s
            AND video_type=%s AND (lease_token IS NOT NULL OR paused_reason IS NOT NULL)""", (CHANNEL, kind)).fetchone()['n']

    # ------------------------------------------------------------------ video paths
    def check_videos(self, run):
        for value, streams in ((OFF, False), (ALL, True)):
            with self.subTest(formats=value):
                self.setUp()
                provider = Provider()
                before = self.admin.execute("""SELECT video_id,to_jsonb(s)::text AS row FROM public.video_collection_state s
                    WHERE channel_id=%s AND video_type='stream' ORDER BY 1""", (CHANNEL,)).fetchall()
                with self.formats(value):
                    run(provider)
                asked = self.requested(provider)
                self.assertTrue(set(self.ids['long']) & asked, 'positive control: Long-form is collected')
                self.assertEqual(bool(self.stream_ids() & asked), streams)
                if not streams:
                    # No lease, pause or schedule write either: the rows wait unchanged.
                    after = self.admin.execute("""SELECT video_id,to_jsonb(s)::text AS row FROM public.video_collection_state s
                        WHERE channel_id=%s AND video_type='stream' ORDER BY 1""", (CHANNEL,)).fetchall()
                    self.assertEqual(after, before)

    def test_unscoped_common_drain(self):
        def run(provider):
            repository, service = self.service(provider)
            run_claimed_common_video_collection(repository, service, limit=200, worker_id='streams-off:common')
        self.check_videos(run)

    def test_dispatched_auto_feeder(self):
        def run(provider):
            repository, service = self.service(provider)
            state = AutoPassState(200, concurrency=2)
            self.drive(CommonAutoFeeder(service, state=state, cursor=CommonCandidateCursor(),
                                        worker_id='streams-off:auto', require_ready=lambda: None))
        self.check_videos(run)

    def test_typed_stream_drain(self):
        # The retired typed runner is off on every installation; switch it on here
        # so that only the formats gate can keep it from streams.
        self.admin.execute("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type='stream'")
        self.addCleanup(self.admin.execute,
                        "UPDATE public.typed_video_collection_runtime SET runtime_enabled=false WHERE video_type='stream'")
        for value, streams in ((OFF, False), (ALL, True)):
            with self.subTest(formats=value):
                self.setUp()
                provider = Provider()
                repository, service = self.service(provider)
                with self.formats(value):
                    run_claimed_typed_video_collection(repository, service, video_type='stream', limit=50,
                                                       worker_id='streams-off:typed')
                self.assertEqual(bool(self.stream_ids() & self.requested(provider)), streams)

    # ------------------------------------------------------------------ playlist paths
    def check_playlists(self, run):
        stream_playlist = PREFIX['stream'] + CHANNEL[2:]
        for value, streams in ((OFF, False), (ALL, True)):
            with self.subTest(formats=value):
                self.setUp()
                provider = Provider()
                with self.formats(value):
                    run(provider)
                self.assertIn(PREFIX['long'] + CHANNEL[2:], provider.playlists, 'positive control')
                self.assertIn(PREFIX['short'] + CHANNEL[2:], provider.playlists)
                self.assertEqual(stream_playlist in provider.playlists, streams)
                if not streams:
                    row = self.admin.execute("""SELECT status,lease_token,active_scan_id FROM public.channel_playlist_state
                        WHERE channel_id=%s AND video_type='stream'""", (CHANNEL,)).fetchone()
                    self.assertEqual(row, {'status': 'ready', 'lease_token': None, 'active_scan_id': None})

    def test_dispatched_playlist_pass(self):
        def run(provider):
            feeder = AutoInventoryFeeder(TypedInventoryRepository(self.db), page_budget=PageBudget(20),
                cursor=AutoInventoryCursor(), candidate_scan_limit=1000, run_id=str(uuid.uuid4()),
                worker_id='streams-off:inventory', fetch_page=provider.page, require_ready=lambda: None,
                concurrency=2, playlist_not_found_errors=(YouTubePlaylistNotFoundError,))
            self.drive(feeder)
        self.check_playlists(run)

    def test_unaddressed_playlist_pass(self):
        def run(provider):
            run_typed_inventory_batch(repository=TypedInventoryRepository(self.db), fetch_page=provider.page,
                limit=20, worker_id='streams-off:inventory', prepare=False,
                playlist_not_found_errors=(YouTubePlaylistNotFoundError,))
        self.check_playlists(run)

    def test_first_format_count_demand_leaves_a_turned_off_list_unclaimed(self):
        for value, expected in ((OFF, False), (ALL, True)):
            with self.subTest(formats=value):
                self.setUp()
                self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
                self.admin.execute("""UPDATE public.channel_playlist_state SET status='ready',inventory_status='playlist_not_found',
                    last_error_code='typed_playlist_not_found',source_checked_at=clock_timestamp()-interval '2 days',
                    next_due_at=clock_timestamp()+interval '19 days',source_next_after=clock_timestamp()+interval '19 days'
                    WHERE channel_id=%s AND video_type='stream'""",(CHANNEL,))
                self.admin.execute('UPDATE public.channels SET count_reconcile_token=gen_random_uuid() WHERE channel_id=%s',(CHANNEL,))
                with self.formats(value):
                    claims=TypedInventoryRepository(self.db).claim_due(limit=10,worker_id='discovery-streams-off',run_id=str(uuid.uuid4()))
                self.assertEqual(any(c.video_type=='stream' for c in claims),expected)
                # Admission is an independent token; no metadata/arm write moves this clock.
                due=self.admin.execute("""SELECT next_due_at>clock_timestamp()+interval '18 days' AS retained
                    FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='stream'""",(CHANNEL,)).fetchone()
                self.assertTrue(due['retained'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
