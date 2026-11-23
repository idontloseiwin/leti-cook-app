"""Frozen catalogue membership, pagination, facets and invalidation on PostgreSQL."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
sys.path.insert(0, str(ROOT/'tests'))
from research_query.validation import AccessContext, QueryRejected, canonical
from research_query.service import QueryService
from research_query.frozen import FrozenStore
from research_query.admission import ReadAdmission
from research_query.cursor import CursorCodec
from research_query.operations import OPERATIONS
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from research_query_pg import bootstrap, seed
from test_research_query_contract import FIXTURE, query

BIG = 'UC' + 'Z'*22          # 20,000 long videos, 1,000 Shorts, 400 Streams.
NEIGHBOUR = 'UC' + 'Y'*22    # A small channel in the same folder.
SMALL = 'UC' + 'X'*22        # Hand-made clocks, its own folder.
LONG, SHORT, STREAM = 20000, 1000, 400
LIBRARY = ['video.id', 'video.channel_id', 'video.type', 'video.title', 'video.thumbnail_url',
           'video.published_at', 'video.duration_seconds', 'video.views', 'video.likes', 'video.comments']
PREFIX = {'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}


def add_channel(admin, channel, folder, now, *, hidden=False):
    user = uuid.uuid4()
    admin.execute('INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at) VALUES(%s,%s,%s,%s)',
                  (channel, user, folder, now))
    admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch)"
                  " VALUES(%s,%s,'accepted',%s,1)", (channel, user, hidden))
    for kind, prefix in PREFIX.items():
        scan, playlist = uuid.uuid4(), prefix + channel[2:]
        admin.execute('''INSERT INTO public.channel_video_inventory_scans
            (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at)
            VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s)''',
            (scan, channel, kind, playlist, now-timedelta(hours=2), now-timedelta(hours=2), now-timedelta(hours=1), now-timedelta(hours=1)))
        admin.execute('''INSERT INTO public.channel_playlist_state
            (channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at)
            VALUES(%s,%s,%s,'completed',%s,%s,%s)''', (channel, kind, playlist, scan, now-timedelta(hours=1), now-timedelta(hours=1)))


def add_videos(admin, channel, kind, first, count, now, *, letter='z'):
    """`count` videos of one type; views repeat every 500 videos (ties)."""
    ids = f"'{letter}'||lpad(n::text,10,'0')"
    duration = {'long': "'PT'||(60+n%%3000)||'S'", 'short': "'PT'||(5+n%%55)||'S'", 'stream': "'PT2H'"}[kind]
    params = dict(channel=channel, kind=kind, first=first, last=first+count-1, now=now,
                  playlist=PREFIX[kind]+channel[2:])
    admin.execute(f"""INSERT INTO public.video_inventory(video_id,channel_id,video_type)
        SELECT {ids},%(channel)s,%(kind)s FROM generate_series(%(first)s::int,%(last)s::int) n""", params)
    admin.execute(f"""INSERT INTO public.video_inventory_memberships
        (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
        SELECT {ids},%(channel)s,%(kind)s,%(playlist)s,%(now)s::timestamptz-make_interval(mins=>n),
         %(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour','legacy_inventory_backfill'
        FROM generate_series(%(first)s::int,%(last)s::int) n""", params)
    admin.execute(f"""INSERT INTO public.videos
        (video_id,channel_id,title,views,like_count,comment_count,checked_at,metadata_checked_at,published_at,duration,
         availability_status,availability_checked_at,stream_actual_end_at)
        SELECT {ids},%(channel)s,'Synthetic video '||n,(n%%500)*10,n%%97,n%%13,%(now)s::timestamptz-interval '1 hour',
         %(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-make_interval(mins=>n),{duration},'public',
         %(now)s::timestamptz-interval '1 hour',CASE WHEN %(kind)s='stream' THEN %(now)s::timestamptz-interval '2 hours' END
        FROM generate_series(%(first)s::int,%(last)s::int) n""", params)


class NativeFrozenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        password = os.environ['LIC_QUERY_TEST_PASSWORD']
        port = int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres',
                                    password=password, autocommit=True)
        version = cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000 <= int(version) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        now = cls.seeded_now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        seed(cls.admin, now)
        cls.big_folder, cls.small_folder, cls.other_folder = (str(uuid.uuid4()) for _ in range(3))
        for folder in (cls.big_folder, cls.small_folder, cls.other_folder):
            cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Frozen fixture')", (folder,))
        started = time.perf_counter()
        add_channel(cls.admin, BIG, cls.big_folder, now)
        add_videos(cls.admin, BIG, 'long', 1, LONG, now)
        add_videos(cls.admin, BIG, 'short', LONG+1, SHORT, now)
        add_videos(cls.admin, BIG, 'stream', LONG+SHORT+1, STREAM, now)
        add_channel(cls.admin, NEIGHBOUR, cls.big_folder, now)
        add_videos(cls.admin, NEIGHBOUR, 'long', 1, 30, now, letter='y')
        add_channel(cls.admin, SMALL, cls.small_folder, now)
        add_videos(cls.admin, SMALL, 'long', 1, 6, now, letter='x')
        seeded = time.perf_counter()
        cls.context = AccessContext('synthetic-installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
        cls.db = PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=3),
                                  cadence_snapshot=__import__('collection_schedule').CollectionSchedules().cadence_snapshot(),
                                  admission=lambda _: None).open()
        cls.repo = ResearchQueryRepository(cls.db)
        cls.refresh()
        print('FROZEN_FIXTURE ' + json.dumps({'videos': LONG+SHORT+STREAM+36, 'seed_s': round(seeded-started, 2),
                                             'refresh_s': round(time.perf_counter()-seeded, 2),
                                             'server_version_num': version}), flush=True)

    @classmethod
    def refresh(cls):
        # The production refresh and batch size, as the backend role.
        from db.inventory.search_projection import SearchProjectionRepository
        projection = SearchProjectionRepository(cls.db)
        while projection.refresh():
            pass

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    def setUp(self):
        # Like one API process: one cursor key, one admission, one store.
        self.store, self.codec, self.admission = FrozenStore(auto_prune=False), CursorCodec(), ReadAdmission(concurrency=2)
        self.service = self.make_service()
        self.started = time.perf_counter()

    def tearDown(self):
        print(f'TIMING {self.id().rsplit(".", 1)[-1]} {time.perf_counter()-self.started:.1f}s', flush=True)

    def make_service(self, store=None, context=None):
        return QueryService(self.repo, lambda: context or self.context, frozen=store or self.store,
                            codec=self.codec, admission=self.admission)

    def big(self, kind='long', **changes):
        q = query(scope={'kind': 'channel', 'id': BIG}, video_types=[kind], select=LIBRARY,
                  order_by=[{'field': 'video.views', 'direction': 'desc'}], consistency='frozen', page={'size': 50})
        q.update(changes)
        return q

    def query(self, q, service=None):
        return (service or self.service).execute(canonical(q))

    def walk(self, q, first, service=None):
        ids, page = [row['id'] for row in first['rows']], first
        while page['page']['has_more']:
            page = self.query({**q, 'page': {**q['page'], 'after': page['page']['next_cursor']}}, service)
            ids.extend(row['id'] for row in page['rows'])
        return ids, page

    def ids_of(self, page, service=None):
        return (service or self.service).frozen_ids(canonical({'version': 1, 'frozen_ref': page['context']['frozen_ref']}))

    def current_ids(self, q):
        q = {**q, 'consistency': 'current', 'page': {'size': 200}}
        ids, page = [], self.query(q)
        while True:
            ids.extend(row['id'] for row in page['rows'])
            if not page['page']['has_more']:
                return ids
            page = self.query({**q, 'page': {'size': 200, 'after': page['page']['next_cursor']}})

    @contextmanager
    def clock_shift(self, video, **clocks):
        """Temporarily set clocks of one video (seconds relative to the DB clock)."""
        columns = ','.join(clocks)
        old = self.admin.execute(f'SELECT {columns} FROM public.videos WHERE video_id=%s', (video,)).fetchone()
        now = self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        try:
            self.admin.execute(f"UPDATE public.videos SET {','.join(c+'=%s' for c in clocks)} WHERE video_id=%s",
                               (*(now+timedelta(seconds=s) for s in clocks.values()), video))
            yield now
        finally:
            self.admin.execute(f"UPDATE public.videos SET {','.join(c+'=%s' for c in clocks)} WHERE video_id=%s", (*old, video))

    def timed_repository(self, timings):
        repo = self.repo
        class Timed:
            def read_frozen(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    return repo.read_frozen(*args, **kwargs)
                finally:
                    timings.append((time.perf_counter()-started)*1000)
            def __getattr__(self, name):
                return getattr(repo, name)
        return Timed()

    def test_01_twenty_thousand_rows_freeze_page_export_and_measure(self):
        q = self.big()
        reads = []
        self.service.repository = self.timed_repository(reads)
        started = time.perf_counter()
        first = self.query(q)
        build_ms = (time.perf_counter()-started)*1000
        page = first['page']
        self.assertEqual((page['start_index'], page['total_count'], page['previous_cursor']), (0, LONG, None))
        self.assertTrue(page['has_more'])
        self.assertEqual(len(first['rows']), 50)
        context = first['context']
        self.assertEqual(context['consistency'], 'frozen')
        self.assertEqual(context['time_zone'], 'UTC')
        lifetime = datetime.fromisoformat(context['valid_before']) - datetime.fromisoformat(context['evaluated_at'])
        self.assertEqual(lifetime, timedelta(seconds=600))
        self.assertEqual(first['coverage']['status'], 'complete')
        usage = self.store.usage()
        self.assertEqual(usage['sessions'], 1)
        self.assertLess(usage['bytes'], 64*1024*1024)
        started = time.perf_counter()
        ids, last = self.walk(q, first)
        walk_ms = (time.perf_counter()-started)*1000
        self.assertEqual((len(ids), len(set(ids))), (LONG, LONG))
        self.assertEqual(last['page']['start_index'], LONG-50)
        self.assertIsNone(last['page']['next_cursor'])
        started = time.perf_counter()
        exported = self.ids_of(first)
        export_ms = (time.perf_counter()-started)*1000
        self.assertEqual(exported['ids'], ids)
        self.assertEqual(exported['total_count'], LONG)
        self.assertEqual(exported['context']['valid_before'], context['valid_before'])
        self.assertLess(len(canonical(exported)), 2*1024*1024)
        # The same order as current pages over unchanged data: views desc, ties by id.
        expected = sorted(ids, key=lambda v: (-((int(v[1:]) % 500)*10), v))
        self.assertEqual(ids, expected)
        values = [int(r['cells']['video.views']['value']) for r in self.query({**q, 'page': {'size': 200, 'anchor_id': ids[10000]}})['rows']]
        self.assertEqual(values, sorted(values, reverse=True))
        # The same population and first page as current reads of unchanged data.
        from research_query.aggregate import SUMMARY
        current = {**q, 'consistency': 'current', 'page': {'size': 200}}
        summary = self.service.execute_operation(canonical({'version': 1, 'operation': SUMMARY, 'query': current, 'parameters': {}}))
        self.assertEqual(summary['result']['population'], LONG)
        self.assertEqual([r['id'] for r in self.query(current)['rows']], ids[:200])
        # Back from the last page.
        back = self.query({**q, 'page': {'size': 50, 'after': last['page']['previous_cursor']}})
        self.assertEqual(back['page']['start_index'], LONG-100)
        self.assertEqual([r['id'] for r in back['rows']], ids[LONG-100:LONG-50])
        sizes = [len(canonical(self.query({**q, 'page': {'size': 200}})))]
        print('FROZEN_SCALE ' + json.dumps({
            'rows': LONG, 'build_ms': round(build_ms), 'sql_read_ms': round(reads[0]), 'session_bytes': usage['bytes'],
            'pages_walked': LONG//50 - 1, 'page_ms_avg': round(walk_ms/(LONG//50 - 1), 2),
            'ids_export_ms': round(export_ms), 'ids_export_bytes': len(canonical(exported)),
            'page200_bytes': sizes[0]}), flush=True)
        self.assertLess(build_ms, 8000)

    def test_02_frozen_values_survive_concurrent_writes(self):
        q = self.big()
        first = self.query(q)
        second_ids = [row['id'] for row in self.query({**q, 'page': {'size': 50, 'after': first['page']['next_cursor']}})['rows']]
        target = second_ids[0]
        old = self.admin.execute('SELECT views,checked_at FROM public.videos WHERE video_id=%s', (target,)).fetchone()
        try:
            self.admin.execute('UPDATE public.videos SET views=987654321,checked_at=clock_timestamp() WHERE video_id=%s', (target,))
            again = self.query({**q, 'page': {'size': 50, 'after': first['page']['next_cursor']}})
            self.assertEqual([row['id'] for row in again['rows']], second_ids)
            frozen_views = again['rows'][0]['cells']['video.views']['value']
            self.assertNotEqual(frozen_views, '987654321')
            current = self.query({**q, 'consistency': 'current', 'where': {'field': 'video.id', 'op': 'eq', 'value': target}})
            self.assertEqual(current['rows'][0]['cells']['video.views']['value'], '987654321')
            # A new frozen result sees the write at the top.
            self.assertEqual(self.query(q)['rows'][0]['id'], target)
        finally:
            self.admin.execute('UPDATE public.videos SET views=%s,checked_at=%s WHERE video_id=%s', (*old, target))

    def test_03_anchor_previous_next_and_missing_anchor(self):
        q = self.big()
        ids = self.ids_of(self.query(q))['ids']
        anchored = self.query({**q, 'page': {'size': 50, 'anchor_id': ids[1234]}})
        self.assertEqual((anchored['page']['start_index'], anchored['page']['anchor_id']), (1200, ids[1234]))
        self.assertEqual([r['id'] for r in anchored['rows']], ids[1200:1250])
        before = self.query({**q, 'page': {'size': 50, 'after': anchored['page']['previous_cursor']}})
        after = self.query({**q, 'page': {'size': 50, 'after': anchored['page']['next_cursor']}})
        self.assertEqual((before['page']['start_index'], after['page']['start_index']), (1150, 1250))
        self.assertEqual([r['id'] for r in before['rows']], ids[1150:1200])
        missing = self.query({**q, 'page': {'size': 50, 'anchor_id': 'q0000000001'}})
        self.assertEqual((missing['page']['start_index'], missing['page']['anchor_id']), (0, None))
        # Sort change keeps the anchored video in the first page returned.
        resorted = self.query({**q, 'order_by': [{'field': 'video.published_at', 'direction': 'asc'}],
                             'page': {'size': 50, 'anchor_id': ids[1234]}})
        self.assertIn(ids[1234], [r['id'] for r in resorted['rows']])

    def test_04_sources_expiring_inside_the_session_do_not_shorten_it(self):
        q = query(scope={'kind': 'channel', 'id': SMALL}, select=LIBRARY, order_by=[{'field': 'video.id', 'direction': 'asc'}],
                  consistency='frozen', page={'size': 50})
        x = ['x%010d' % n for n in range(1, 7)]
        member = self.admin.execute('SELECT first_seen_at,last_seen_at,last_present_at FROM public.video_inventory_memberships WHERE video_id=%s', (x[4],)).fetchone()
        try:
            # x2 counters and x3 metadata expire in 5 minutes; x4 availability and
            # x5 identity proof too; x6 is private and its clock ends in 60 s.
            with self.clock_shift(x[1], checked_at=-696*3600+300), self.clock_shift(x[2], metadata_checked_at=-696*3600+300), \
                    self.clock_shift(x[3], availability_checked_at=-696*3600+300), \
                    self.clock_shift(x[5], availability_checked_at=-696*3600+60):
                self.admin.execute("UPDATE public.videos SET availability_status='private' WHERE video_id=%s", (x[5],))
                self.admin.execute("UPDATE public.video_inventory_memberships SET first_seen_at=clock_timestamp()-interval '696 hours'+interval '300 seconds',"
                                   " last_seen_at=clock_timestamp()-interval '696 hours'+interval '300 seconds',"
                                   " last_present_at=clock_timestamp()-interval '696 hours'+interval '300 seconds' WHERE video_id=%s", (x[4],))
                self.refresh()
                frozen = self.query(q)
                current = self.query({**q, 'consistency': 'current'})
                ids = [r['id'] for r in frozen['rows']]
                self.assertEqual(ids, [x[0], x[1], x[2]])
                self.assertEqual([r['id'] for r in current['rows']], [x[0], x[1], x[2], x[3], x[4]])
                cells = {r['id']: r['cells'] for r in frozen['rows']}
                self.assertEqual(cells[x[1]]['video.views']['state'], 'expired')
                self.assertEqual(cells[x[1]]['video.likes']['state'], 'expired')
                self.assertEqual(cells[x[1]]['video.title']['state'], 'known')
                self.assertEqual(cells[x[2]]['video.title']['state'], 'expired')
                self.assertEqual(cells[x[2]]['video.views']['state'], 'known')
                current_cells = {r['id']: r['cells'] for r in current['rows']}
                self.assertEqual(current_cells[x[1]]['video.views']['state'], 'known')
                self.assertEqual(frozen['coverage']['reasons'], {'source_rows_unavailable': 1, 'source_rows_expiring': 2,
                                                                 'rows_with_expired_values': 2})
                self.assertEqual(frozen['coverage']['status'], 'partial')
                evaluated = datetime.fromisoformat(frozen['context']['evaluated_at'])
                self.assertEqual(datetime.fromisoformat(frozen['context']['valid_before'])-evaluated, timedelta(seconds=600))
                # The current page is bounded by the private row's clock.
                self.assertLessEqual(datetime.fromisoformat(current['context']['valid_before'])
                                     - datetime.fromisoformat(current['context']['evaluated_at']), timedelta(seconds=61))
                # A filter on a withheld value cannot match.
                matching = self.query({**q, 'where': {'field': 'video.views', 'op': 'gte', 'value': '0'}})
                self.assertEqual([r['id'] for r in matching['rows']], [x[0], x[2]])
        finally:
            self.admin.execute("UPDATE public.videos SET availability_status='public' WHERE video_id=%s", (x[5],))
            self.admin.execute('UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s WHERE video_id=%s', (*member, x[4]))

    def test_05_hidden_channel_mid_session(self):
        q = self.big()
        first = self.query(q)
        try:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s', (BIG,))
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.query({**q, 'page': {'size': 50, 'after': first['page']['next_cursor']}})
            # The refused result is gone, not waiting for the channel to return.
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s', (BIG,))
            for attempt in (lambda: self.query({**q, 'page': {'size': 50, 'after': first['page']['next_cursor']}}),
                            lambda: self.ids_of(first)):
                with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
                    attempt()
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s', (BIG,))
            with self.assertRaisesRegex(QueryRejected, 'source_unavailable'):
                self.query(q)
        finally:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false WHERE channel_id=%s', (BIG,))

    def test_06_folder_membership_change_mid_session(self):
        q = self.big(scope={'kind': 'folder', 'id': self.big_folder, 'include_descendants': False})
        first = self.query(q)
        self.assertEqual(first['page']['total_count'], LONG + 30)
        try:
            self.admin.execute('UPDATE public.channels SET folder_id=%s WHERE channel_id=%s', (self.other_folder, NEIGHBOUR))
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.query({**q, 'page': {'size': 50, 'after': first['page']['next_cursor']}})
            self.assertEqual(self.query(q)['page']['total_count'], LONG)
        finally:
            self.admin.execute('UPDATE public.channels SET folder_id=%s WHERE channel_id=%s', (self.big_folder, NEIGHBOUR))

    def test_07_channel_invalidation_and_runtime_clear(self):
        big = self.query(self.big())
        small_q = query(scope={'kind': 'channel', 'id': SMALL}, select=LIBRARY, consistency='frozen', page={'size': 2})
        small = self.query(small_q)
        folder_q = self.big(scope={'kind': 'folder', 'id': self.big_folder, 'include_descendants': False})
        folder = self.query(folder_q)
        # A type decision or trash change on NEIGHBOUR: its folder result expires.
        self.store.invalidate_channel(NEIGHBOUR)
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
            self.query({**folder_q, 'page': {'size': 50, 'after': folder['page']['next_cursor']}})
        self.assertEqual(self.query({**self.big(), 'page': {'size': 50, 'after': big['page']['next_cursor']}})['page']['start_index'], 50)
        self.store.invalidate_channel(BIG)
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
            self.query({**self.big(), 'page': {'size': 50, 'after': big['page']['next_cursor']}})
        self.assertEqual(self.query({**small_q, 'page': {'size': 2, 'after': small['page']['next_cursor']}})['page']['start_index'], 2)
        self.store.invalidate_all()
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
            self.query({**small_q, 'page': {'size': 2, 'after': small['page']['next_cursor']}})
        self.assertEqual(self.store.usage(), {'sessions': 0, 'bytes': 0})

    def test_08_shorts_and_streams_have_their_own_frozen_populations(self):
        for kind, count in (('short', SHORT), ('stream', STREAM)):
            q = self.big(kind)
            first = self.query(q)
            self.assertEqual(first['page']['total_count'], count)
            exported = self.ids_of(first)['ids']
            self.assertEqual(exported, self.current_ids(q))
            self.assertTrue(all(row['cells']['video.type']['value'] == kind for row in first['rows']))

    def test_09_over_limit_is_refused_never_truncated(self):
        with patch('db.reads.research_sql.MAX_FROZEN_ROWS', LONG - 1):
            with self.assertRaises(QueryRejected) as caught:
                self.query(self.big())
        self.assertEqual((caught.exception.code, caught.exception.path), ('query_budget_exceeded', 'consistency'))
        self.assertEqual(self.store.usage()['sessions'], 0)
        small = FrozenStore(max_session_bytes=1024*1024, auto_prune=False)
        with self.assertRaises(QueryRejected) as caught:
            self.query(self.big(), self.make_service(small))
        self.assertEqual((caught.exception.code, caught.exception.path), ('query_budget_exceeded', 'consistency'))
        self.assertEqual(small.usage()['sessions'], 0)

    def test_10_owner_http_frozen_pages_and_matching_ids(self):
        import tempfile
        from fastapi.testclient import TestClient
        from research_query_preview import create_preview_app
        with tempfile.TemporaryDirectory() as folder, \
                patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None):
            app = create_preview_app(self.db, Path(folder)/'owner.sqlite3', 'http://127.0.0.1:8002', FIXTURE['folders'])
            with TestClient(app, base_url='http://127.0.0.1:8002', headers={'Origin': 'http://127.0.0.1:8002'}) as client:
                deadline = time.monotonic()+15
                while not app.state.research_projection.initial_fill_complete:
                    self.assertLess(time.monotonic(), deadline, 'initial preparation stalled')
                    time.sleep(.02)
                q = self.big(page={'size': 200})
                response = client.post('/research/queries/execute', json=q)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data['context']['valid_before'], data['source_expires_at'])
                self.assertEqual(response.headers['cache-control'], 'no-store')
                ids = [r['id'] for r in data['rows']]
                while data['page']['has_more']:
                    data = client.post('/research/queries/execute', json={**q, 'page': {'size': 200, 'after': data['page']['next_cursor']}}).json()
                    ids.extend(r['id'] for r in data['rows'])
                exported = client.post('/research/queries/frozen-ids', json={'version': 1, 'frozen_ref': data['context']['frozen_ref']})
                self.assertEqual(exported.status_code, 200, exported.text)
                self.assertEqual(exported.json()['ids'], ids)
                self.assertEqual(exported.json()['source_expires_at'], exported.json()['context']['valid_before'])
                tampered = client.post('/research/queries/execute', json={**q, 'page': {'size': 200, 'after': data['page']['previous_cursor'][:-4]+'AAAA'}})
                self.assertEqual((tampered.status_code, tampered.json()['code']), (409, 'cursor_expired'))
                foreign = client.post('/research/queries/frozen-ids', json={'version': 1, 'frozen_ref': data['page']['previous_cursor']})
                self.assertEqual((foreign.status_code, foreign.json()['code']), (409, 'cursor_expired'))
                malformed = client.post('/research/queries/frozen-ids', json={'version': 1})
                self.assertEqual((malformed.status_code, malformed.json()['code']), (422, 'invalid_query'))

    def test_11_collection_membership_change_mid_session(self):
        identity = str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.saved_objects(kind,id,name,create_fingerprint) VALUES('collection',%s,'Frozen',%s)", (identity, 'b'*64))
        try:
            for n in range(1, 4):
                self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                                   (identity, 'z%010d' % n))
            q = self.big(scope={'kind': 'collection', 'id': identity}, page={'size': 2})
            first = self.query(q)
            self.assertEqual(first['page']['total_count'], 3)
            self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                               (identity, 'z%010d' % 4))
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.query({**q, 'page': {'size': 2, 'after': first['page']['next_cursor']}})
            self.assertEqual(self.query(q)['page']['total_count'], 4)
        finally:
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s', (identity,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s", (identity,))

    def test_12_channel_source_frozen_result(self):
        q = query(source={'id': 'public.channels.current.v1'}, scope={'kind': 'folder', 'id': self.big_folder, 'include_descendants': False},
                  select=['channel.id', 'channel.title'], order_by=[{'field': 'channel.id', 'direction': 'asc'}],
                  consistency='frozen', page={'size': 1})
        q.pop('video_types'); q.pop('observations', None)
        first = self.query(q)
        self.assertEqual(first['page']['total_count'], 2)
        self.assertEqual(self.ids_of(first)['ids'], sorted([BIG, NEIGHBOUR]))
        second = self.query({**q, 'page': {'size': 1, 'after': first['page']['next_cursor']}})
        self.assertEqual(second['rows'][0]['id'], max(BIG, NEIGHBOUR))
        self.assertEqual(datetime.fromisoformat(second['context']['valid_before'])
                         - datetime.fromisoformat(second['context']['evaluated_at']), timedelta(seconds=600))

    def test_13_dependent_read_reference_and_access_change(self):
        first = self.query(self.big())
        reference = self.service.frozen_reference(first['context']['frozen_ref'])
        target = first['rows'][7]['id']
        member = reference.member(target)
        self.assertEqual(member.row, first['rows'][7])
        self.assertEqual(member.position, 7)
        self.assertEqual(member.evaluated_at.isoformat().replace('+00:00', 'Z'), first['context']['evaluated_at'])
        with self.assertRaisesRegex(QueryRejected, 'source_unavailable'):
            reference.member('x0000000001')
        reference.require_current()
        from dataclasses import replace
        revoked = self.make_service(context=replace(self.context, access_revision='owner-2'))
        with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
            revoked.frozen_member(first['context']['frozen_ref'], target)
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
            reference.require_current()

    def test_14_parallel_builds_and_page_reads(self):
        errors, results = [], []
        def build():
            try:
                results.append(self.query(self.big()))
            except QueryRejected as error:
                errors.append(error.code)
        threads = [threading.Thread(target=build) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(30)
        self.assertEqual(len(results) + len(errors), 3)
        self.assertTrue(set(errors) <= {'query_busy'}, errors)
        self.assertGreaterEqual(len(results), 1)
        print('FROZEN_PARALLEL ' + json.dumps({'built': len(results), 'busy': len(errors)}), flush=True)

    def test_facets_on_a_large_filtered_population(self):
        from research_query.aggregate_service import DEADLINE_SECONDS, MAX_OUTPUT_BYTES
        q = query(scope={'kind': 'channel', 'id': BIG}, video_types=['long'], select=['video.id'],
                  time={'mode': 'current', 'timezone': 'Europe/Berlin'})
        cases = {'all': {},
                 'conditions': {'views': {'all': [{'field': 'video.views', 'op': 'gte', 'value': '1000'},
                                                   {'field': 'video.views', 'op': 'lt', 'value': '10000'}]},
                                'duration': {'field': 'video.duration_seconds', 'op': 'lte', 'value': '1800'},
                                'title_length': {'field': 'video.title_length', 'op': 'lt', 'value': '40'}}}
        measured = {}
        for name, conditions in cases.items():
            runs = []
            for _ in range(3):
                started = time.perf_counter()
                out = self.service.execute_operation(canonical(dict(version=1, operation='video.facets.v1', query=q,
                    parameters={'population': {'kind': 'query'}, 'conditions': conditions})))
                runs.append(time.perf_counter() - started)
            size = len(canonical(out))
            self.assertLess(max(runs), DEADLINE_SECONDS)
            self.assertLess(size, MAX_OUTPUT_BYTES)
            result = out['result']
            expected = sum(not conditions or (1000 <= (n % 500) * 10 < 10000 and 60 + n % 3000 <= 1800)
                           for n in range(1, LONG + 1))
            self.assertEqual(result['population']['matched_count'], expected)
            measured[name] = {'facets_ms': [round(r * 1000) for r in runs], 'bytes': size,
                              'matched': result['population']['matched_count']}

    def test_14c_facets_over_the_frozen_list_keep_its_rows_and_values(self):
        """Video.facets.v1 over a frozen Library list (operation bind hook). Unchanged
        data: the same counts as the current facets. Writes during
        the list's life: the listed rows keep their frozen values; other rows count only where
        their own group's condition fails. Another list or a field the list did not freeze is refused."""
        from research_query.aggregate_service import DEADLINE_SECONDS
        from research_query.service import instant
        zone = {'mode': 'current', 'timezone': 'Europe/Berlin'}
        base = {'field': 'video.title', 'op': 'contains_casefold_v1', 'value': 'synthetic'}
        conditions = {'views': {'all': [{'field': 'video.views', 'op': 'gte', 'value': '1000'},
                                        {'field': 'video.views', 'op': 'lt', 'value': '2000'}]},
                      'duration': {'field': 'video.duration_seconds', 'op': 'lte', 'value': '1800'}}
        # The list as Library reads it: its own condition order, frozen, with the facet fields.
        select = [*LIBRARY, 'video.title_length', 'video.tag_count', 'video.category_id']
        listed = self.big(select=select, time=zone, where={'all': [conditions['duration'], base, *conditions['views']['all']]})
        page = self.query(listed)
        reference, total = page['context']['frozen_ref'], page['page']['total_count']
        q = query(scope={'kind': 'channel', 'id': BIG}, video_types=['long'], select=['video.id'], where=base, time=zone)

        def facets(population, given=conditions):
            started = time.perf_counter()
            out = self.service.execute_operation(canonical(dict(version=1, operation='video.facets.v1', query=q,
                parameters={'population': population, 'conditions': given})))
            return out, time.perf_counter() - started

        def views(out):
            return {b['key']: b['count'] for b in out['result']['groups']['views']['buckets']}
        frozen_ref = {'kind': 'frozen', 'frozen_ref': reference}
        current, _ = facets({'kind': 'query'})
        frozen, frozen_s = facets(frozen_ref)
        self.assertEqual(frozen['result']['groups'], current['result']['groups'])
        self.assertEqual((frozen['result']['population']['kind'], frozen['result']['population']['matched_count']), ('frozen', total))
        self.assertEqual(current['result']['population']['matched_count'], total)
        self.assertLessEqual(instant(frozen['context']['valid_before']), instant(page['context']['valid_before']))
        self.assertLess(frozen_s, DEADLINE_SECONDS)
        ids = self.ids_of(page)['ids']
        leaving = ids[:2]
        entering = self.admin.execute("""SELECT v.video_id FROM public.videos v WHERE v.channel_id=%s AND v.views=2500
            AND substring(v.duration FROM 'PT([0-9]+)S')::int<=1800 AND NOT (v.video_id=ANY(%s)) ORDER BY v.video_id LIMIT 1""", (BIG, ids)).fetchone()[0]
        old = self.admin.execute('SELECT video_id,views,checked_at FROM public.videos WHERE video_id=ANY(%s)', (leaving + [entering],)).fetchall()
        try:
            self.admin.execute('UPDATE public.videos SET views=50000,checked_at=clock_timestamp() WHERE video_id=ANY(%s)', (leaving,))
            self.admin.execute('UPDATE public.videos SET views=1500,checked_at=clock_timestamp() WHERE video_id=%s', (entering,))
            moved, _ = facets({'kind': 'query'})
            kept, kept_s = facets(frozen_ref)
            # Current: the two leave the range for 10K-100K, the third enters it.
            self.assertEqual(moved['result']['population']['matched_count'], total - 1)
            self.assertEqual((views(moved)['1k_10k'], views(moved)['10k_100k']),
                             (views(current)['1k_10k'] - 2, views(current)['10k_100k'] + 2))
            # Frozen: the list's rows keep their frozen bucket; the entering row matches every
            # condition now but is not a listed row, so no group counts it (the old snapshot did, at 2,500).
            self.assertEqual(kept['result']['population']['matched_count'], total)
            self.assertEqual((views(kept)['1k_10k'], views(kept)['10k_100k']),
                             (views(current)['1k_10k'] - 1, views(current)['10k_100k']))
            self.assertEqual(kept['result']['groups']['duration'], frozen['result']['groups']['duration'])
        finally:
            for video_id, views_value, checked in old:
                self.admin.execute('UPDATE public.videos SET views=%s,checked_at=%s WHERE video_id=%s', (views_value, checked, video_id))
        for population, given, code in ((frozen_ref, {'views': conditions['views']}, 'cursor_context_changed'),
                                        ({'kind': 'frozen', 'frozen_ref': self.query(self.big())['context']['frozen_ref']}, conditions,
                                         'cursor_context_changed'),
                                        ({'kind': 'frozen', 'frozen_ref': self.query({**listed, 'select': LIBRARY})['context']['frozen_ref']},
                                         conditions, 'invalid_query')):
            with self.subTest(code=code), self.assertRaisesRegex(QueryRejected, code):
                facets(population, given)
        print('FROZEN_FACETS ' + json.dumps({'rows': total, 'frozen_ms': round(frozen_s * 1000), 'after_writes_ms': round(kept_s * 1000)}),
              flush=True)

    def test_15_lagging_read_model_is_complete_or_refused_never_truncated(self):
        # Worst case: every row of the channel waits for the background refresh,
        # so the statement reads the protected views directly. Runs last: the
        # read model is left lagging.
        self.admin.execute('UPDATE public.video_search_projection SET valid_until=clock_timestamp() WHERE channel_id=%s', (BIG,))
        reads = []
        self.service.repository = self.timed_repository(reads)
        started = time.perf_counter()
        try:
            first = self.query(self.big())
            outcome = first['page']['total_count']
            self.assertEqual(outcome, LONG)
        except QueryRejected as error:
            outcome = error.code
            self.assertEqual(outcome, 'query_budget_exceeded')
            self.assertEqual(self.store.usage()['sessions'], 0)
        print('FROZEN_LAGGING ' + json.dumps({'outcome': outcome, 'build_ms': round((time.perf_counter()-started)*1000),
                                             'sql_read_ms': round(reads[0]) if reads else None}), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
