"""Saved filters, portable backups and frozen catalogue membership on a disposable PostgreSQL database."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid
from zoneinfo import ZoneInfo

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query.validation import AccessContext, QueryRejected, canonical  # noqa: E402
from research_query.service import QueryService  # noqa: E402
from research_query.frozen import FrozenStore  # noqa: E402
from research_query.admission import ReadAdmission  # noqa: E402
from research_query.cursor import CursorCodec  # noqa: E402
from research_query.operations import OPERATIONS  # noqa: E402
from db.reads.research_query import ResearchQueryRepository  # noqa: E402
from db.base.postgres import PostgresConfig, PostgresDatabase  # noqa: E402
from research_query_pg import bootstrap, seed  # noqa: E402
from research_frozen_pg import LIBRARY, PREFIX, add_channel, add_videos  # noqa: E402
from test_research_query_contract import query  # noqa: E402

EDGE = 'UC' + 'E' * 21 + 'Q'      # 2,000 Long videos of add_videos plus the hand-made ones below.
ZONES = 'UC' + 'W' * 22           # Publication instants around zone transitions only.
COUNT = 2000
SELECT = ['video.id', 'video.title', 'video.views', 'video.published_at']
# Instants in the past (a future publication is not admitted): DST changes, offsets and the date line.
INSTANTS = ['2026-03-08T04:59:59Z', '2026-03-08T06:59:59Z', '2026-03-08T07:00:00Z',     # New York spring
            '2025-11-02T05:30:00Z', '2025-11-02T06:30:00Z', '2025-11-02T03:59:59Z',     # New York autumn
            '2026-03-29T00:59:59Z', '2026-03-29T01:00:00Z', '2025-10-26T00:30:00Z',     # Berlin
            '2025-10-26T01:30:00Z', '2025-12-31T09:59:59Z', '2025-12-31T10:00:00Z',     # Kiritimati new year
            '2026-01-01T10:59:59Z', '2026-01-01T11:00:00Z', '2026-04-04T15:29:59Z',     # Pago Pago, Lord Howe
            '2026-04-04T15:30:00Z', '2026-05-31T18:14:59Z', '2026-05-31T18:15:00Z']     # Kathmandu +05:45
TIME_ZONES = ['America/New_York', 'Europe/Berlin', 'Pacific/Kiritimati', 'Pacific/Pago_Pago',
              'Australia/Lord_Howe', 'Asia/Kathmandu', 'America/Argentina/Buenos_Aires', 'UTC']
TITLES = {'u0000000001': 'Σίσυφος and ß', 'u0000000002': 'ΣΊΣΥΦΟΣ AND SS', 'u0000000003': 'Kyiv Київ 🎬',
          'u0000000004': 'İstanbul ı', 'u0000000005': 'é combining'}


def add_video(admin, video, channel, kind, now, *, published, title='Edge video', views=1, counter_age=None,
              metadata_age=None):
    playlist = PREFIX[kind] + channel[2:]
    seen = now - timedelta(hours=1)
    admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)', (video, channel, kind))
    admin.execute('''INSERT INTO public.video_inventory_memberships
        (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')''', (video, channel, kind, playlist, published, seen, seen, seen))
    admin.execute('''INSERT INTO public.videos(video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,
        availability_status,availability_checked_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'PT300S','public',%s)''',
                  (video, channel, title, views, now - (counter_age or timedelta(hours=1)),
                   now - (metadata_age or timedelta(hours=1)), published, seen))


class ResearchEdges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        password, port = os.environ['LIC_QUERY_TEST_PASSWORD'], int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres', password=password,
                                    autocommit=True)
        if not 170000 <= int(cls.admin.execute('SHOW server_version_num').fetchone()[0]) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        seed(cls.admin, now)
        cls.folder = str(uuid.uuid4())
        cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Edges')", (cls.folder,))
        add_channel(cls.admin, EDGE, cls.folder, now)
        add_videos(cls.admin, EDGE, 'long', 1, COUNT, now, letter='q')
        for video, title in TITLES.items():
            add_video(cls.admin, video, EDGE, 'long', now, published=now - timedelta(days=3), title=title, views=7)
        add_channel(cls.admin, ZONES, cls.folder, now)
        cls.instants = {}
        for index, value in enumerate(INSTANTS):
            video = f'w{index:010d}'
            cls.instants[video] = datetime.fromisoformat(value.replace('Z', '+00:00'))
            add_video(cls.admin, video, ZONES, 'long', now, published=cls.instants[video])
        cls.context = AccessContext('synthetic-installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
        cls.db = PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator '
                                                 f'password={password}', max_size=4),
                                  cadence_snapshot=__import__('collection_schedule').CollectionSchedules().cadence_snapshot(),
                                  admission=lambda _: None).open()
        cls.repo = ResearchQueryRepository(cls.db)
        cls.refresh()

    @classmethod
    def refresh(cls):
        from db.inventory.search_projection import SearchProjectionRepository
        projection = SearchProjectionRepository(cls.db)
        while projection.refresh():
            pass

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    def setUp(self):
        self.service = QueryService(self.repo, lambda: self.context, frozen=FrozenStore(auto_prune=False),
                                    codec=CursorCodec(), admission=ReadAdmission(concurrency=2))

    def run_query(self, value):
        return self.service.execute(canonical(value))

    def q(self, channel=EDGE, **changes):
        value = query(scope={'kind': 'channel', 'id': channel}, video_types=['long'], select=SELECT,
                      order_by=[{'field': 'video.views', 'direction': 'desc'}], page={'size': 200})
        value.update(changes)
        return value

    def all_ids(self, value):
        page = self.run_query(value)
        ids = [row['id'] for row in page['rows']]
        while page['page']['has_more']:
            page = self.run_query({**value, 'page': {**value['page'], 'after': page['page']['next_cursor']}})
            ids.extend(row['id'] for row in page['rows'])
        return ids

    def summary(self, value):
        from research_query.aggregate import SUMMARY
        return self.service.execute_operation(canonical(dict(version=1, operation=SUMMARY, query=value, parameters={})))

    # ------------------------------------------------------------------ 1
    def test_01_typical_and_edge_filters(self):
        everything = self.all_ids(self.q())
        self.assertEqual(len(everything), COUNT + len(TITLES))
        self.assertEqual(len(set(everything)), len(everything))
        match_all = self.all_ids(self.q(where={'field': 'video.views', 'op': 'gte', 'value': '0'}))
        self.assertEqual(sorted(match_all), sorted(everything))
        frozen = self.run_query(self.q(where={'field': 'video.views', 'op': 'gte', 'value': '0'}, consistency='frozen'))
        self.assertEqual(frozen['page']['total_count'], len(everything))
        none = self.run_query(self.q(where={'field': 'video.views', 'op': 'gt', 'value': str(10**15)}))
        self.assertEqual((none['rows'], none['page']['has_more']), ([], False))
        known = [f'q{n:010d}' for n in range(1, 501)]
        unknown = [f'n{n:010d}' for n in range(1, 501)]
        listed = self.q(where={'field': 'video.id', 'op': 'in', 'value': known + unknown})
        self.assertEqual(sorted(self.all_ids(listed)), known)
        self.assertEqual(self.run_query({**listed, 'consistency': 'frozen'})['page']['total_count'], 500)
        for values, code in (([], 'query_budget_exceeded'), (known + unknown + ['x0000000001'], 'query_budget_exceeded')):
            with self.subTest(values=len(values)), self.assertRaisesRegex(QueryRejected, code):
                self.run_query(self.q(where={'field': 'video.id', 'op': 'in', 'value': values}))
        for needle, expected in (('σίσυφος', {'u0000000001', 'u0000000002'}), ('київ', {'u0000000003'}),
                                 ('🎬', {'u0000000003'}), ('ss', {'u0000000001', 'u0000000002'})):
            with self.subTest(needle=needle):
                where = {'field': 'video.title', 'op': 'contains_casefold_v1', 'value': needle}
                current = set(self.all_ids(self.q(where=where)))
                frozen_ids = self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': self.run_query(
                    self.q(where=where, consistency='frozen'))['context']['frozen_ref']}))['ids']
                self.assertEqual((current, set(frozen_ids)), (expected, expected))
        for zone in ('America/Argentina', 'America', '+03:00', 'Mars/Phobos'):
            with self.subTest(zone=zone), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                self.run_query(self.q(time={'mode': 'current', 'timezone': zone}))
        self.assertTrue(self.run_query(self.q(time={'mode': 'current', 'timezone': 'America/Argentina/Buenos_Aires'}))['rows'])

    # ------------------------------------------------------------------ 2
    def test_02_local_dates_across_dst_offsets_and_the_date_line(self):
        select = ['video.id', 'video.published_at', 'video.published_date', 'video.published_year']
        for zone in TIME_ZONES:
            with self.subTest(zone=zone):
                local = {video: moment.astimezone(ZoneInfo(zone)) for video, moment in self.instants.items()}
                value = self.q(ZONES, select=select, time={'mode': 'current', 'timezone': zone},
                               order_by=[{'field': 'video.id', 'direction': 'asc'}])
                rows = self.run_query(value)['rows']
                self.assertEqual(len(rows), len(INSTANTS))
                for row in rows:
                    cells = row['cells']
                    self.assertEqual(cells['video.published_date']['value'], local[row['id']].date().isoformat(), row['id'])
                    self.assertEqual(str(cells['video.published_year']['value']), str(local[row['id']].year), row['id'])
                for day in sorted({moment.date().isoformat() for moment in local.values()}):
                    expected = {video for video, moment in local.items() if moment.date().isoformat() == day}
                    where = {'field': 'video.published_date', 'op': 'eq', 'value': day}
                    self.assertEqual(set(self.all_ids({**value, 'where': where})), expected, (zone, day))
                frozen = self.run_query({**value, 'consistency': 'frozen', 'page': {'size': 50}})
                self.assertEqual({row['id']: row['cells']['video.published_date']['value'] for row in frozen['rows']},
                                 {video: moment.date().isoformat() for video, moment in local.items()})

    # ------------------------------------------------------------------ 3
    def test_03_the_696_hour_window_to_the_minute(self):
        now = self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        ages = {'v0000000001': timedelta(hours=695, minutes=59), 'v0000000002': timedelta(hours=696, minutes=1),
                'v0000000003': timedelta(hours=695, minutes=55)}
        channel = 'UC' + 'V' * 22
        add_channel(self.admin, channel, self.folder, now)
        for video, age in ages.items():
            add_video(self.admin, video, channel, 'long', now, published=now - timedelta(days=40), title='Window ' + video,
                      views=5, counter_age=age, metadata_age=age)
        self.refresh()
        value = self.q(channel, order_by=[{'field': 'video.id', 'direction': 'asc'}])
        current = {row['id']: (row['cells']['video.views']['state'], row['cells']['video.title']['state'])
                   for row in self.run_query(value)['rows']}
        self.assertEqual(current, {'v0000000001': ('known', 'known'), 'v0000000002': ('expired', 'expired'),
                                   'v0000000003': ('known', 'known')})
        frozen = self.run_query({**value, 'consistency': 'frozen'})
        cells = {row['id']: (row['cells']['video.views']['state'], row['cells']['video.title']['state']) for row in frozen['rows']}
        # A value that would expire inside the 600-second session is expired at once; the row stays.
        self.assertEqual(cells['v0000000003'], ('expired', 'expired'))
        self.assertEqual(cells['v0000000002'], ('expired', 'expired'))
        self.assertGreaterEqual(frozen['coverage']['reasons'].get('rows_with_expired_values', 0), 2)
        known = self.run_query({**value, 'where': {'field': 'video.views', 'op': 'is_known'}})
        self.assertEqual([row['id'] for row in known['rows']], ['v0000000001', 'v0000000003'])

    # ------------------------------------------------------------------ 4
    def test_04_a_video_that_changes_type(self):
        video = 'q0000000777'
        frozen = self.run_query(self.q(consistency='frozen', page={'size': 50}))
        reference = frozen['context']['frozen_ref']
        long_before = self.summary(self.q())['result']['population']
        short_before = self.summary(self.q(video_types=['short']))['result']['population']
        now = self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        try:
            # As the collector records a type correction: the video is now in the Shorts list only.
            self.admin.execute('DELETE FROM public.video_inventory_memberships WHERE video_id=%s', (video,))
            self.admin.execute("UPDATE public.video_inventory SET video_type='short' WHERE video_id=%s", (video,))
            self.admin.execute('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                SELECT %s,%s,'short',%s,published_at,%s,%s,%s,'legacy_inventory_backfill' FROM public.videos WHERE video_id=%s''',
                               (video, EDGE, PREFIX['short'] + EDGE[2:], now, now, now, video))
            self.admin.execute("UPDATE public.videos SET duration='PT40S' WHERE video_id=%s", (video,))
            self.refresh()
            self.assertNotIn(video, self.all_ids(self.q()))
            self.assertIn(video, self.all_ids(self.q(video_types=['short'])))
            self.assertEqual(self.summary(self.q())['result']['population'], long_before - 1)
            self.assertEqual(self.summary(self.q(video_types=['short']))['result']['population'], short_before + 1)
            kept = self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': reference}))['ids']
            self.assertIn(video, kept)             # the frozen list keeps its rows until it is rebuilt
            rebuilt = self.run_query(self.q(consistency='frozen', page={'size': 50}))
            self.assertNotIn(video, self.service.frozen_ids(canonical(
                {'version': 1, 'frozen_ref': rebuilt['context']['frozen_ref']}))['ids'])
        finally:
            self.admin.execute('DELETE FROM public.video_inventory_memberships WHERE video_id=%s', (video,))
            self.admin.execute("UPDATE public.video_inventory SET video_type='long' WHERE video_id=%s", (video,))
            self.admin.execute('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                SELECT %s,%s,'long',%s,published_at,%s,%s,%s,'legacy_inventory_backfill' FROM public.videos WHERE video_id=%s''',
                               (video, EDGE, PREFIX['long'] + EDGE[2:], now, now, now, video))
            self.admin.execute("UPDATE public.videos SET duration='PT837S' WHERE video_id=%s", (video,))
            self.refresh()

    # ------------------------------------------------------------------ 5
    def test_05_collector_writes_while_frozen_pages_facets_and_overview_read(self):
        # The list as Library reads it (the facet fields frozen), and facets over it.
        views = {'field': 'video.views', 'op': 'gte', 'value': '100'}
        listed = self.q(select=[*LIBRARY, 'video.title_length', 'video.tag_count', 'video.category_id'], where=views,
                        consistency='frozen', page={'size': 100})
        first = self.run_query(listed)
        reference, total = first['context']['frozen_ref'], first['page']['total_count']
        conditions = {'views': views}
        facet_query = self.q(select=['video.id'])
        population = self.summary(self.q())['result']['population']

        def walk():
            ids, cells, page = [], [], first
            while True:
                ids.extend(row['id'] for row in page['rows'])
                cells.extend(json.dumps(row['cells'], sort_keys=True) for row in page['rows'])
                if not page['page']['has_more']:
                    return ids, cells
                page = self.run_query({**listed, 'page': {**listed['page'], 'after': page['page']['next_cursor']}})

        def facets():
            result = self.service.execute_operation(canonical(dict(
                version=1, operation='video.facets.v1', query=facet_query,
                parameters={'population': {'kind': 'frozen', 'frozen_ref': reference}, 'conditions': conditions})))['result']
            return result['groups'], result['population']['matched_count']
        baseline_ids, baseline_cells = walk()
        baseline_facets = facets()
        self.assertEqual((len(baseline_ids), len(set(baseline_ids))), (total, total))
        stop, failures, rounds = threading.Event(), [], {'write': 0, 'pages': 0, 'facets': 0, 'summary': 0}
        port, password = int(os.environ['LIC_QUERY_TEST_PORT']), os.environ['LIC_QUERY_TEST_PASSWORD']

        def writer():
            with psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres', password=password,
                                 autocommit=True) as connection:
                while not stop.is_set():
                    try:
                        with connection.transaction():
                            # Only rows already in the list change: the matching set stays the same, so the
                            # frozen pages and the facets over them must not move while values do.
                            connection.execute("""UPDATE public.videos SET views=views+3,checked_at=clock_timestamp(),
                                metadata_checked_at=clock_timestamp() WHERE channel_id=%s AND video_id<=%s AND views>=100""",
                                               (EDGE, f'q{(rounds["write"] % 20 + 1) * 100:010d}'))
                            connection.execute("""INSERT INTO public.video_snapshots(video_id,views,recorded_at,views_definition_epoch)
                                SELECT video_id,views,clock_timestamp(),'fixture.current.v1' FROM public.videos
                                WHERE channel_id=%s AND video_id<=%s""", (EDGE, 'q0000000050'))
                        rounds['write'] += 1
                    except Exception as error:      # a collector write must never fail because of the readers
                        failures.append(('write', type(error).__name__))
                        return

        def reader(name, action, check):
            while not stop.is_set():
                try:
                    check(action())
                    rounds[name] += 1
                except QueryRejected as error:
                    # Busy and time-budget refusals are the bounded answers under load: the reader
                    # retries, and each reader must still finish two rounds while the writer runs.
                    if error.code not in {'query_busy', 'query_budget_exceeded'}:
                        failures.append((name, error.code))
                        return
                except AssertionError as error:
                    failures.append((name, 'changed: ' + str(error)[:120]))
                    return
                except Exception as error:
                    failures.append((name, type(error).__name__))
                    return

        def same(expected):
            def check(value):
                assert value == expected, 'frozen value changed'
            return check
        threads = [threading.Thread(target=writer),
                   threading.Thread(target=reader, args=('pages', walk, same((baseline_ids, baseline_cells)))),
                   threading.Thread(target=reader, args=('facets', facets, same(baseline_facets))),
                   threading.Thread(target=reader, args=('summary', lambda: self.summary(self.q())['result']['population'],
                                                         same(population)))]
        for thread in threads:
            thread.start()
        # At least 8 s of overlap, then until every reader and the writer finished two rounds: a slow CI
        # container (1.5 CPU) gives one full frozen walk in 8 s. Bounded well below the suite's 360 s.
        started = time.monotonic()
        while time.monotonic() - started < 90 and not failures:
            if time.monotonic() - started >= 8 and all(count >= 2 for count in rounds.values()):
                break
            time.sleep(0.2)
        stop.set()
        for thread in threads:
            thread.join(60)
        self.assertEqual(failures, [])
        self.assertTrue(all(count >= 2 for count in rounds.values()), rounds)
        after = self.run_query({**listed, 'page': {'size': 100}})
        self.assertNotEqual(after['context']['frozen_ref'], reference)   # a new list sees the written values
        print('EDGES_CONCURRENCY ' + json.dumps(rounds), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
