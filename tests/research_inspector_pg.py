"""Video static details, observation history, launch and lifetime statistics on PostgreSQL."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import random
import sys
import time
import unittest
import uuid

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
import research_query_pg as native
from research_query.validation import AccessContext, QueryRejected, canonical
from research_query.service import QueryService
from research_query.operations import OPERATIONS
from research_query.aggregate_service import MAX_OUTPUT_BYTES
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from statistics import quantiles

MAIN = 'UCWPDMAINAAAAAAAAAAAAAAA'
CONFLICT = 'UCWPDCONFLICTCCCCCCCCCCC'
BIG = 'UCWPDBIGBBBBBBBBBBBBBBBB'
FOLDER = '22222222-2222-4222-8222-222222222222'
EPOCH = 'youtube.views.play_start.20260824'
SECOND_EPOCH = 'fixture.views.v2'
HOUR, DAY = 3600, 86400
# Brief cadence ladder: (age limit seconds, interval seconds); None: no limit.
LADDER = ((DAY, HOUR), (3 * DAY, 2 * HOUR), (7 * DAY, 6 * HOUR), (14 * DAY, 12 * HOUR), (90 * DAY, DAY), (None, 7 * DAY))


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value


def schedule(origin, now, start, jitter_seed):
    """Collection-like observation times from origin, ladder intervals, minute jitter, within [start, now]."""
    jitter = random.Random(jitter_seed)
    times, t, k = [], origin, 0
    while t <= now:
        age = (t - origin).total_seconds()
        step = next(step for limit, step in LADDER if limit is None or age < limit)
        stamp = t + timedelta(seconds=jitter.randint(-150, 150)) if k else t + timedelta(seconds=jitter.randint(0, 150))
        if start <= stamp <= now:
            times.append(stamp)
        t += timedelta(seconds=step)
        k += 1
    return times


def curve(seed, origin, stamp):
    """Monotone lifetime views, likes and comments of a synthetic video."""
    rng = random.Random(seed)
    base, slope = rng.randint(200, 90_000), rng.randint(0, 40)
    hours = max(0.0, (stamp - origin).total_seconds() / HOUR)
    views = int(base * (1 - math.exp(-hours / 30)) + slope * hours)
    return views, views * rng.randint(5, 60) // 1000, views * rng.randint(1, 9) // 1000


def video_rows(channel, prefix, kind, count, spacing_hours, now, rng, offset_hours=5):
    """Plain catalogue videos of one type published every spacing_hours (newest first)."""
    rows = []
    for index in range(count):
        published = now - timedelta(hours=offset_hours + index * spacing_hours, minutes=rng.randint(0, 50))
        video = dict(id=f'{prefix}{index:0{11 - len(prefix)}d}', channel=channel, type=kind, published=published,
                     end=published + timedelta(hours=rng.randint(1, 5)) if kind == 'stream' else None,
                     title=rng.choice(['Plain', 'Straße guide', 'Ünïcode — title', 'Emoji \U0001F600 test', 'x' * 120]),
                     duration=f'PT{rng.randint(5, 3500)}S' if kind != 'stream' else rng.choice(['PT2H3M', 'P1DT2H', 'PT45M']),
                     category=rng.choice(['10', '22', '27', '']), tags=rng.choice([None, [], ['a'], ['Straße', 'b', 'c']]),
                     availability='public', counters_age=None, metadata_age=None, likes_hidden=rng.random() < 0.15,
                     comments_disabled=rng.random() < 0.1, edits=[])
        rows.append(video)
    return rows


def materialize(video, now):
    """Snapshot rows and the current row of one synthetic video."""
    window_start = now - timedelta(hours=720)  # Also writes rows older than the 696-hour view.
    times = schedule(video['published'], now - timedelta(minutes=10), window_start, video['id'])
    snaps = []
    for stamp in times:
        views, likes, comments = curve(video['id'], video['published'], stamp)
        snaps.append([stamp, views, None if video['likes_hidden'] else likes,
                      None if video['comments_disabled'] else comments])
    for edit in video['edits']:
        edit(snaps)
    return snaps


class InspectorPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        password, port = os.environ['LIC_QUERY_TEST_PASSWORD'], int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres', password=password,
                                    autocommit=True)
        version = cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000 <= int(version) < 180000:
            raise RuntimeError('pg17_required')
        native.bootstrap(cls.admin)
        cls.now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        started = time.monotonic()
        cls.videos = cls.seed_detailed(cls.now)
        cls.big_rows = cls.seed_big(cls.now)
        cls.context = AccessContext('synthetic-installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
        cls.db = PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=2),
                                  cadence_snapshot=__import__('collection_schedule').CollectionSchedules().cadence_snapshot(),
                                  admission=lambda _: None).open()
        cls.service = QueryService(ResearchQueryRepository(cls.db), lambda: cls.context)
        cls.admin.execute('ANALYZE')
        # The production refresh, as the backend role through the gateway, in the installed batch size.
        from db.inventory.search_projection import SearchProjectionRepository
        repository = SearchProjectionRepository(cls.db)
        while repository.refresh():
            pass
        cls.admin.execute('ANALYZE')
        print(f'Inspector fixture ready in {time.monotonic() - started:.1f} s; big channel snapshots {cls.big_rows}', flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    # ---------- fixture ----------

    @classmethod
    def channel(cls, channel_id, now):
        user_id = uuid.uuid4()
        cls.admin.execute('INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at) VALUES(%s,%s,%s,%s)',
                          (channel_id, user_id, FOLDER, now))
        cls.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch)"
                          " VALUES(%s,%s,'accepted',false,1)", (channel_id, user_id))
        for kind, prefix in (('long', 'UULF'), ('short', 'UUSH'), ('stream', 'UULV')):
            scan, playlist = uuid.uuid4(), prefix + channel_id[2:]
            cls.admin.execute('''INSERT INTO public.channel_video_inventory_scans
                (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at)
                VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s)''',
                (scan, channel_id, kind, playlist, now - timedelta(hours=2), now - timedelta(hours=2), now - timedelta(hours=1), now - timedelta(hours=1)))
            cls.admin.execute('''INSERT INTO public.channel_playlist_state
                (channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at)
                VALUES(%s,%s,%s,'completed',%s,%s,%s)''', (channel_id, kind, playlist, scan, now - timedelta(hours=1), now - timedelta(hours=1)))

    @classmethod
    def insert_videos(cls, videos, now):
        clock = now - timedelta(hours=1)
        snapshots = []
        for video in videos:
            playlist = {'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}[video['type']] + video['channel'][2:]
            cls.admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)',
                              (video['id'], video['channel'], video['type']))
            cls.admin.execute('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')''',
                (video['id'], video['channel'], video['type'], playlist, video['published'], clock, clock, clock))
            snaps = materialize(video, now)
            video['snapshots'] = snaps
            current = video.get('current') or (snaps[-1] if snaps else None)
            checked = now - timedelta(hours=video['counters_age']) if video['counters_age'] else (current[0] if current else None)
            metadata = now - timedelta(hours=video['metadata_age'] or 2)
            values = current[1:] if current else (None, None, None)
            if video.get('views_offset'):
                values = (values[0] + video['views_offset'], *values[1:])
            video.update(checked=checked, current_values=values)
            end = video['end']
            cls.admin.execute('''INSERT INTO public.videos
                (video_id,channel_id,title,views,like_count,comment_count,checked_at,metadata_checked_at,published_at,duration,
                 availability_status,availability_checked_at,stream_actual_end_at,publication_status,category_id,tags)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (video['id'], video['channel'], video['title'], *values, checked, metadata, video['published'], video['duration'],
                 video['availability'], now - timedelta(hours=1), end, video.get('publication'), video['category'], video['tags']))
            for stamp, views, likes, comments in snaps:
                if video.get('drop_endpoint') and stamp == snaps[-1][0]:
                    continue
                snapshots.append((video['id'], stamp, views, likes, comments))
                if video.get('duplicate_at') is not None and stamp == snaps[video['duplicate_at']][0]:
                    duplicate = video.get('duplicate_views', views)
                    snapshots.append((video['id'], stamp, duplicate, likes, comments))
        with cls.admin.cursor().copy('COPY public.video_snapshots(video_id,recorded_at,views,like_count,comment_count) FROM STDIN') as copy:
            for row in snapshots:
                copy.write_row(row)
        return videos

    @classmethod
    def seed_detailed(cls, now):
        cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Inspector fixture')", (FOLDER,))
        rng = random.Random(20261029)
        cls.channel(MAIN, now)
        cls.numeric_channel = 'UC' + 'NumericExamples'.ljust(22, '0')
        cls.channel(cls.numeric_channel, now)
        anchor = now - timedelta(days=28, hours=1)
        cls.numeric_anchor = anchor
        examples = []
        for kind, prefix in (('long', 'N'), ('short', 'Q'), ('stream', 'T')):
            for rank in range(1, 6):
                stamps = [[anchor + timedelta(days=day), rank * day * 10, rank * day * 2, rank * day]
                          for day in (1, 7, 28)]
                examples.append(dict(id=f'{prefix}{rank:010d}', type=kind, channel=cls.numeric_channel,
                                     published=anchor - timedelta(hours=1) if kind == 'stream' else anchor,
                                     end=anchor if kind == 'stream' else None, title='Sample title', duration='PT92S',
                                     category='22', tags=['one', 'two'], availability='public', counters_age=None,
                                     metadata_age=None, likes_hidden=False, comments_disabled=False,
                                     edits=[lambda rows, stamps=stamps: rows.__setitem__(slice(None), stamps)]))
        cls.insert_videos(examples, now)
        cls.channel(CONFLICT, now)
        videos = (video_rows(MAIN, 'L', 'long', 140, 11, now, rng) + video_rows(MAIN, 'H', 'short', 70, 21, now, rng)
                  + video_rows(MAIN, 'S', 'stream', 40, 37, now, rng, offset_hours=9))
        by = {video['id']: video for video in videos}
        # Long: included and excluded rows.
        by['L0000000003'].update(counters_age=700)              # stale counters: expired counters
        by['L0000000004'].update(metadata_age=700)              # stale metadata: unknown publication time
        by['L0000000005'].update(availability='private')
        by['L0000000006'].update(availability='unknown_unavailable')
        by['L0000000009'].update(duplicate_at=3)                # identical duplicate rows: collapsed
        by['L0000000010'].update(likes_hidden=True, comments_disabled=True)
        def null_views(snaps):
            snaps[len(snaps) // 2][1] = None
        def negative_views(snaps):
            snaps[len(snaps) // 3][1] = -1
        by['L0000000011']['edits'].append(null_views)
        by['L0000000012']['edits'].append(negative_views)
        def gap_24h(snaps, published=by['L0000000013']['published']):
            snaps[:] = [s for s in snaps if not published + timedelta(hours=21) <= s[0] <= published + timedelta(hours=25)]
        by['L0000000013']['edits'].append(gap_24h)
        # Streams: one still live (excluded), one ended after its counter (excluded), the rest ended.
        by['S0000000001'].update(end=None, publication='live')
        by['S0000000002'].update(end=now - timedelta(minutes=1))
        # Anomaly channel: conflicting snapshots and broken endpoints (they also void the shared views epoch state).
        conflict = video_rows(CONFLICT, 'C', 'long', 30, 23, now, rng)
        for video in conflict:
            video.update(likes_hidden=False, comments_disabled=False)
        cbs = {video['id']: video for video in conflict}
        cbs['C0000000007'].update(drop_endpoint=True)           # no snapshot at the counter clock
        cbs['C0000000008'].update(views_offset=5)               # current counter differs from its snapshot
        cbs['C0000000020'].update(conflict_near='24h')          # different values at the 24 h pick
        cbs['C0000000025'].update(conflict_far=True)            # different values far from any target
        cls.insert_videos(videos + conflict, now)
        for video_id, row in cbs.items():
            if row.get('conflict_near') or row.get('conflict_far'):
                target = row['published'] + (timedelta(hours=24) if row.get('conflict_near') else timedelta(days=10, hours=3))
                stamp = max(s[0] for s in row['snapshots'] if s[0] <= target)
                cls.admin.execute('INSERT INTO public.video_snapshots(video_id,recorded_at,views,like_count,comment_count)'
                                  ' VALUES(%s,%s,%s,0,0)', (video_id, stamp, 7))
        return by | cbs

    @classmethod
    def seed_big(cls, now):
        """20,000 long videos, five a day for 4,000 days, default schedule (hourly to 7 d, daily to 1 y, weekly)."""
        cls.channel(BIG, now)
        cls.admin.execute("""
CREATE TEMP TABLE big AS SELECT 'B'||lpad(g::text,10,'0') AS video_id,
 date_trunc('second',%(now)s::timestamptz)-make_interval(hours=>3)-(g*interval '288 minutes') AS published FROM generate_series(0,19999) g""",
                          {'now': now})
        cls.admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) SELECT video_id,%s,'long' FROM big", (BIG,))
        cls.admin.execute("""INSERT INTO public.video_inventory_memberships
 (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
 SELECT video_id,%(c)s,'long','UULF'||substr(%(c)s,3),published,%(t)s,%(t)s,%(t)s,'legacy_inventory_backfill' FROM big""",
                          {'c': BIG, 't': now - timedelta(hours=1)})
        cls.admin.execute("""
CREATE TEMP TABLE big_snaps AS SELECT b.video_id,s.at AS recorded_at,
 (1000*ln(1+extract(epoch FROM s.at-b.published)/3600))::bigint+length(b.video_id) AS views
FROM big b CROSS JOIN LATERAL (
 SELECT generate_series(greatest(b.published,%(start)s::timestamptz),least(b.published+interval '7 days',%(now)s::timestamptz),interval '1 hour') AS at
 UNION ALL SELECT generate_series(greatest(b.published+interval '7 days',%(start)s::timestamptz),least(b.published+interval '365 days',%(now)s::timestamptz),interval '1 day')
 UNION ALL SELECT generate_series(greatest(b.published+interval '365 days',%(start)s::timestamptz),%(now)s::timestamptz,interval '7 days')) s
WHERE s.at>%(start)s::timestamptz""", {'start': now - timedelta(hours=690), 'now': now - timedelta(minutes=5)})
        rows = cls.admin.execute("INSERT INTO public.video_snapshots(video_id,recorded_at,views,like_count,comment_count)"
                                 " SELECT video_id,recorded_at,views,views/40,views/400 FROM big_snaps").rowcount
        # The current row is the latest snapshot, as the collector writes them together.
        cls.admin.execute("""INSERT INTO public.videos(video_id,channel_id,title,views,like_count,comment_count,checked_at,
 metadata_checked_at,published_at,duration,availability_status,availability_checked_at,category_id,tags)
 SELECT b.video_id,%(c)s,'Big '||b.video_id,l.views,l.views/40,l.views/400,l.recorded_at,%(m)s,b.published,'PT10M','public',%(m)s,'22',ARRAY['big']
 FROM big b JOIN (SELECT DISTINCT ON(video_id) video_id,recorded_at,views FROM big_snaps ORDER BY video_id,recorded_at DESC) l
 USING(video_id)""", {'c': BIG, 'm': now - timedelta(hours=1)})
        return rows

    # Read a live or frozen population through the same operation.

    def query(self, channel, kind, **changes):
        return {'query_version': 1, 'source': {'id': 'public.videos.current.v1'}, 'scope': {'kind': 'channel', 'id': channel},
                'video_types': [kind], 'select': ['video.id'], 'order_by': [{'field': 'video.id', 'direction': 'asc'}], **changes}

    def operation(self, name, channel, kind, video, frozen=None, **extra):
        q = self.query(channel, kind, **extra.pop('query', {}))
        reference = {'kind': 'frozen', 'frozen_ref': frozen} if frozen else {'kind': 'query'}
        raw = canonical(dict(version=1, operation=name, query=q, parameters={'video_id': video, 'population': reference, **extra}))
        started = time.monotonic()
        output = self.service.execute_operation(raw)
        return output, time.monotonic() - started

    def freeze(self, channel, kind, **changes):
        """A frozen Library browse: its reference and clock."""
        q = self.query(channel, kind, select=list(INSPECTED), order_by=[{'field': 'video.published_at', 'direction': 'desc'}],
                       consistency='frozen', **changes)
        page = self.service.execute(canonical(q))
        return page['context']['frozen_ref'], at(page['context']['evaluated_at'])


    # ---------- tests ----------

    def test_static_details_current_and_frozen_for_every_format(self):
        for kind in ('long', 'short', 'stream'):
            frozen, now = self.freeze(MAIN, kind)
            rows = self.service.execute(canonical(self.query(MAIN, kind, select=['video.id'], page={'size': 200})))['rows']
            self.assertTrue(rows)
            for video_id in [row['id'] for row in rows[:5]]:
                with self.subTest(kind=kind, video=video_id):
                    current = self.operation('video.static_details.v1', MAIN, kind, video_id)[0]['result']
                    bound = self.operation('video.static_details.v1', MAIN, kind, video_id, frozen=frozen)[0]['result']
                    self.assertEqual((current['inspection'], bound['inspection']['mode']), ({'mode': 'query'}, 'frozen'))
                    self.assertEqual(bound['inspection']['frozen_fields'], sorted(INSPECTED))
                    self.assertEqual(bound['cells'], current['cells'])  # Unchanged data: frozen = current values.
        stale = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000003')[0]['result']
        self.assertEqual((stale['membership'], stale['cells']['video.views']['state'], stale['derived']['video.like_rate']['state']),
                         ('member', 'expired', 'expired'))
        metadata = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000004')[0]['result']
        self.assertEqual((metadata['cells']['video.title']['state'], metadata['derived']['video.title_length']['state'],
                          metadata['cells']['video.views']['state']), ('expired', 'expired', 'known'))
        for excluded in ('L0000000005', 'L0000000006', 'S0000000001', 'S0000000002'):
            kind = 'stream' if excluded.startswith('S') else 'long'
            self.assertEqual(self.operation('video.static_details.v1', MAIN, kind, excluded)[0]['result']['membership'],
                             'not_in_population')
        described = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000020', description=True)[0]['result']
        self.assertEqual(described['cells']['video.description']['state'], 'unknown')

    def test_history_current_and_frozen_for_every_format(self):
        for kind in ('long', 'short', 'stream'):
            frozen, _ = self.freeze(MAIN,kind)
            rows = self.service.execute(canonical(self.query(MAIN, kind, select=['video.id'])))['rows']
            for row in rows[:3]:
                current = self.operation('video.observed_history.v1', MAIN, kind, row['id'])[0]['result']
                bound = self.operation('video.observed_history.v1', MAIN, kind, row['id'], frozen=frozen)[0]['result']
                self.assertEqual(current['status'], bound['status'])
        hidden = self.operation('video.observed_history.v1', MAIN, 'long', 'L0000000010')[0]['result']
        self.assertEqual((hidden['series']['likes']['status'], hidden['series']['likes']['coverage']['valid_count']), ('ready', 0))
        # The window is evaluation minus 28 days through the counter observation, and is named so.
        output = self.operation('video.observed_history.v1', MAIN, 'long', 'L0000000100')[0]
        window = output['result']['window']
        self.assertEqual(at(output['context']['evaluated_at']) - at(window['from_at']), timedelta(hours=672))
        self.assertEqual(at(window['to_at']), self.videos['L0000000100']['checked'])
        self.assertEqual((window['rule'], window['endpoint']), ('evaluated_at_minus_28_days_through_counter_observation', 'counter_observation'))
        stale = self.operation('video.observed_history.v1', MAIN, 'long', 'L0000000003')[0]['result']
        self.assertEqual(stale['reason'], 'catalogue_endpoint_outside_hot_window')

    def test_card_observations_and_launch_values_match_the_input(self):
        for kind, prefix in (('long', 'N'), ('short', 'Q'), ('stream', 'T')):
            channel = self.numeric_channel
            video_id = prefix + '0000000003'
            frozen, _ = self.freeze(channel, kind)
            for ref in (None, frozen):
                with self.subTest(kind=kind, mode='frozen' if ref else 'current'):
                    card = self.operation('video.static_details.v1', channel, kind, video_id, frozen=ref)[0]['result']
                    for key, value in {'title': 'Sample title', 'duration_seconds': '92', 'views': '840',
                                       'likes': '168', 'comments': '84', 'tags': ['one', 'two'],
                                       'category_id': '22'}.items():
                        self.assertEqual(card['cells']['video.' + key]['value'], value, key)
                    for key, value in {'title_length': 12, 'tag_count': 2, 'like_rate': 20, 'comment_rate': 10}.items():
                        self.assertEqual(Decimal(card['derived']['video.' + key]['value']), value, key)
                    times = [self.numeric_anchor + timedelta(days=day) for day in (1, 7, 28)]
                    history = self.operation('video.observed_history.v1', channel, kind, video_id, frozen=ref)[0]['result']
                    self.assertEqual([at(point['recorded_at']) for point in history['points']], times)
                    for metric, daily in (('views', 30), ('likes', 6), ('comments', 3)):
                        self.assertEqual([int(point[metric]) for point in history['points']], [daily, 7*daily, 28*daily])
                        series = history['series'][metric]
                        self.assertEqual(series['coverage'], dict(point_count=3, valid_count=3, missing_count=0,
                                                                  duplicate_timestamp_count=0))
                        expected = [(times[i], times[i+1], span*DAY, span*daily, daily) for i, span in enumerate((6, 21))]
                        actual = [(at(x['from_at']), at(x['to_at']), Decimal(x['actual_interval_seconds']),
                                   int(x['delta']), Decimal(x['per_day'])) for x in series['intervals']]
                        self.assertEqual(actual, expected)
                        self.assertEqual(series['latest_interval'], series['intervals'][-1])
                    launch = self.operation('video.launch_comparison.v1', channel, kind, video_id, frozen=ref)[0]['result']
                    self.assertEqual(len(launch['horizons']), 3)
                    for horizon, day, stamp in zip(launch['horizons'], (1, 7, 28), times, strict=True):
                        self.assertEqual((horizon['ready_count'], horizon['eligible_count']), (5, 5))
                        self.assertEqual(horizon['counts'], {'below_typical': 2, 'typical': 1, 'above_typical': 2})
                        target = horizon['target']
                        self.assertEqual((at(target['observed_at']), int(target['views'])), (stamp, 30*day))
                        cmp = target['comparison']
                        self.assertEqual((cmp['reference_count'], cmp['classification']), (4, 'typical'))
                        # Four peers have 10, 20, 40 and 50 views per day of age.
                        expected = [Decimal('17.5')*day, 30*day, Decimal('42.5')*day]
                        self.assertEqual([Decimal(cmp[k]) for k in ('p25', 'median', 'p75')], expected)

    def test_launch_horizon_statuses(self):
        statuses = {key: [h['target']['status'] for h in self.operation('video.launch_comparison.v1', MAIN, 'long', key)[0]['result']['horizons']]
                    for key in ('L0000000000', 'L0000000013', 'L0000000139', 'L0000000004')}
        self.assertEqual(statuses['L0000000000'], ['not_reached'] * 3)
        self.assertEqual(statuses['L0000000013'][0], 'missing_snapshot')
        self.assertEqual(statuses['L0000000139'], ['outside_observation_window'] * 3)
        self.assertEqual(statuses['L0000000004'], ['missing_publication'] * 3)

    def test_04_anomalies_conflicts_and_endpoints(self):
        frozen, _ = self.freeze(CONFLICT, 'long')
        ids = ('C0000000020', 'C0000000025', 'C0000000010')
        for video in ids[:2]:
            result = self.operation('video.observed_history.v1', CONFLICT, 'long', video, frozen=frozen)[0]['result']
            self.assertEqual((result['status'], result['reason']), ('unavailable', 'conflicting_simultaneous_observations'))
        reasons = {video: self.operation('video.observed_history.v1', CONFLICT, 'long', video)[0]['result']['reason']
                   for video in ('C0000000007', 'C0000000008')}
        self.assertEqual(reasons, {'C0000000007': 'endpoint_snapshot_missing', 'C0000000008': 'endpoint_counter_mismatch'})
        new = {v: self.operation('video.launch_comparison.v1', CONFLICT, 'long', v, frozen=frozen)[0]['result'] for v in ids}
        near_statuses = [h['target']['status'] for h in new['C0000000020']['horizons']]
        self.assertEqual(near_statuses[0], 'conflicting_snapshots')
        far_statuses = [h['target']['status'] for h in new['C0000000025']['horizons']]
        self.assertNotIn('conflicting_snapshots', far_statuses)
        baseline = self.operation('video.lifetime_baseline.v1', CONFLICT, 'long', 'C0000000010')[0]['result']
        rows = self.service.execute(canonical(self.query(CONFLICT, 'long', select=['video.id', 'video.views'], page={'size': 200})))['rows']
        known = sum(r['cells']['video.views']['state'] == 'known' for r in rows)
        unknown = baseline['definition']['unknown_definition_epoch_count']
        self.assertEqual(baseline['definition']['state'], 'known')
        self.assertGreaterEqual(unknown, 2)
        self.assertEqual(baseline['sample_size'], known - unknown)

    def test_05_lifetime_baseline_is_the_shared_summary_over_the_population(self):
        for kind in ('long', 'short', 'stream'):
            q = self.query(MAIN, kind, select=['video.id', 'video.views'], page={'size': 200})
            rows = self.service.execute(canonical(q))['rows']
            values = [int(r['cells']['video.views']['value']) for r in rows if r['cells']['video.views']['state'] == 'known']
            q1, mid, q3 = quantiles(values, method='inclusive')
            expected = dict(known_count=len(values), missing_count=len(rows)-len(values), median=mid, q1=q1, q3=q3)
            probe = rows[0]['id']
            result = self.operation('video.lifetime_baseline.v1', MAIN, kind, probe)[0]['result']
            with self.subTest(kind=kind):
                self.assertEqual((result['sample_size'], result['missing_views'], result['population_count']),
                                 (expected['known_count'], expected['missing_count'], len(rows)))
                self.assertEqual(result['status'], 'ready')
                for name, key in (('median', 'median'), ('p25', 'q1'), ('p75', 'q3')):
                    self.assertEqual(Decimal(result[name]), Decimal(str(expected[key])), name)
                self.assertEqual(result['definition'], {'state': 'known', 'epoch': EPOCH, 'unknown_definition_epoch_count': 0})
                self.assertEqual(result['target']['views']['value'], rows[0]['cells']['video.views']['value'])

    def test_06_a_second_views_definition_is_never_mixed(self):
        try:
            # A definition change 72 hours ago: every later snapshot of the channel carries the new stamp.
            self.admin.execute("UPDATE public.video_snapshots SET views_definition_epoch=%s WHERE recorded_at>=now()-interval '72 hours'"
                               " AND video_id IN (SELECT video_id FROM public.videos WHERE channel_id=%s)", (SECOND_EPOCH, MAIN))
            history = self.operation('video.observed_history.v1', MAIN, 'long', 'L0000000020')[0]['result']
            self.assertEqual(history['status'], 'ready')
            definitions = [i['definition'] for i in history['series']['views']['intervals']]
            self.assertEqual(definitions.count('incompatible_definition'), 1)
            crossing = next(i for i in history['series']['views']['intervals'] if i['definition'] != 'known')
            self.assertEqual((crossing['delta'], crossing['per_day']), (None, None))
            self.assertEqual({p['views_definition_epoch'] for p in history['points']}, {EPOCH, SECOND_EPOCH})
            launch = self.operation('video.launch_comparison.v1', MAIN, 'long', 'L0000000010')[0]['result']
            day = launch['horizons'][0]
            self.assertEqual(day['definition']['state'], 'mixed_definition_epochs')
            self.assertEqual((day['eligible_count'], day['counts'], day['target']['comparison']['status']),
                             (0, {'below_typical': 0, 'typical': 0, 'above_typical': 0}, 'mixed_definition_epochs'))
            self.assertGreater(day['ready_count'], 12)
            # One video reported under another definition: the shared summary rule withholds the views statistics.
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=NULL')
            self.admin.execute("UPDATE public.video_snapshots SET views_definition_epoch=%s WHERE video_id='L0000000020'", (SECOND_EPOCH,))
            baseline = self.operation('video.lifetime_baseline.v1', MAIN, 'long', 'L0000000020')[0]['result']
            self.assertEqual((baseline['definition']['state'], baseline['sample_size'], baseline['median'], baseline['status']),
                             ('mixed_definition_epochs', 0, None, 'insufficient_data'))
            self.assertEqual(baseline['missing_views'], baseline['population_count'])
        finally:
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=NULL')

    def test_07_five_long_videos_a_day_within_budget(self):
        probe = 'B0000000050'  # published 10 days ago
        frozen, now = self.freeze(BIG, 'long')
        timings, sizes = {}, {}
        for name in ('video.static_details.v1', 'video.observed_history.v1', 'video.launch_comparison.v1', 'video.lifetime_baseline.v1'):
            for mode in ('query', 'frozen'):
                output, seconds = self.operation(name, BIG, 'long', probe, frozen=frozen if mode == 'frozen' else None)
                timings[name + ':' + mode], sizes[name + ':' + mode] = round(seconds, 3), len(canonical(output))
                self.assertLess(seconds, 8)
                self.assertLess(sizes[name + ':' + mode], MAX_OUTPUT_BYTES)
                self.assertEqual(output['result']['membership'], 'member')
        launch = self.operation('video.launch_comparison.v1', BIG, 'long', probe, frozen=frozen)[0]['result']
        self.assertEqual([h['target']['status'] for h in launch['horizons']], ['ready', 'ready', 'not_reached'])
        self.assertEqual(launch['coverage']['population_count'], 20000)
        self.assertGreaterEqual(launch['horizons'][0]['eligible_count'], 12)
        old_rows = self.admin.execute("""SELECT count(*) FROM public.video_snapshots s JOIN public.videos v USING(video_id)
 WHERE v.channel_id=%s AND s.recorded_at>=%s AND s.recorded_at<=%s AND v.published_at>=%s""",
                                      (BIG, now - timedelta(days=28), now, now - timedelta(days=56))).fetchone()[0]
        self.assertGreater(old_rows, 20000)

    def test_08_saved_collection_without_session(self):
        """the Inspector of a saved collection member needs no browse session (current reference)."""
        identity = str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.saved_objects(kind,id,name,create_fingerprint) VALUES('collection',%s,'Inspector',%s)",
                           (identity, 'b' * 64))
        try:
            for video in ('L0000000020', 'H0000000001', 'S0000000005'):
                self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                                   (identity, video))
            self.admin.execute("UPDATE public.saved_objects SET member_count=3 WHERE id=%s AND kind='collection'", (identity,))
            scope = {'scope': {'kind': 'collection', 'id': identity}}
            for kind, video in (('long', 'L0000000020'), ('short', 'H0000000001'), ('stream', 'S0000000005')):
                with self.subTest(kind=kind):
                    collection = self.operation('video.static_details.v1', MAIN, kind, video, query=scope)[0]['result']
                    channel = self.operation('video.static_details.v1', MAIN, kind, video)[0]['result']
                    self.assertEqual(collection['cells'], channel['cells'])
                    history = self.operation('video.observed_history.v1', MAIN, kind, video, query=scope)[0]['result']
                    same = self.operation('video.observed_history.v1', MAIN, kind, video)[0]['result']
                    self.assertEqual((history['membership'], history['series']), ('member', same['series']))
                    # The channel baseline and launch of a member: the channel query of the member, no session.
                    self.assertEqual(self.operation('video.lifetime_baseline.v1', MAIN, kind, video)[0]['result']['membership'], 'member')
                    self.assertEqual(self.operation('video.launch_comparison.v1', MAIN, kind, video)[0]['result']['membership'], 'member')
            outside = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000021', query=scope)[0]['result']
            self.assertEqual(outside['membership'], 'not_in_population')
        finally:
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s', (identity,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s", (identity,))

    def test_09_frozen_reference_rules(self):
        """membership, scope binding, lifetime and fencing of the frozen reference (frozen-query contract)."""
        names = ('video.static_details.v1', 'video.observed_history.v1', 'video.launch_comparison.v1', 'video.lifetime_baseline.v1')
        filtered, _ = self.freeze(MAIN, 'long', where={'field': 'video.id', 'op': 'in', 'value': ['L0000000020', 'L0000000021']})
        for name in names:
            with self.subTest(operation=name):
                output = self.operation(name, MAIN, 'long', 'L0000000021', frozen=filtered)[0]
                self.assertEqual(output['result']['inspection']['total_count'], 2)
                self.assertLessEqual(at(output['context']['valid_before']), at(output['result']['inspection']['valid_before']))
                with self.assertRaisesRegex(QueryRejected, 'source_unavailable'):  # not one of its rows
                    self.operation(name, MAIN, 'long', 'L0000000022', frozen=filtered)
                with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):  # another scope
                    self.operation(name, CONFLICT, 'long', 'C0000000010', frozen=filtered)
                with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):  # another video type
                    self.operation(name, MAIN, 'short', 'L0000000021', frozen=filtered)
                with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
                    self.operation(name, MAIN, 'long', 'L0000000021', frozen='A' * 300)
        # A frozen result that lapses during the read voids the answer (require_current after the read).
        from unittest.mock import patch
        from research_query.frozen import FrozenReference
        with patch.object(FrozenReference, 'require_current', side_effect=QueryRejected('cursor_expired')):
            with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
                self.operation('video.static_details.v1', MAIN, 'long', 'L0000000021', frozen=filtered)
        self.service.frozen.invalidate_all()
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
            self.operation('video.static_details.v1', MAIN, 'long', 'L0000000021', frozen=filtered)
        # Frozen values win over newer collected ones (the old frozen row); current mode sees the new counter.
        frozen, _ = self.freeze(MAIN, 'long')
        before = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000030', frozen=frozen)[0]['result']
        self.admin.execute("UPDATE public.videos SET views=views+1000 WHERE video_id='L0000000030'")
        try:
            after = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000030', frozen=frozen)[0]['result']
            now = self.operation('video.static_details.v1', MAIN, 'long', 'L0000000030')[0]['result']
            self.assertEqual(after['cells']['video.views'], before['cells']['video.views'])
            self.assertEqual(int(now['cells']['video.views']['value']), int(before['cells']['video.views']['value']) + 1000)
            history = self.operation('video.observed_history.v1', MAIN, 'long', 'L0000000030', frozen=frozen)[0]['result']
            self.assertEqual(history['status'], 'ready')  # The frozen counter still has its snapshot.
            changed = self.operation('video.observed_history.v1', MAIN, 'long', 'L0000000030')[0]['result']
            self.assertEqual(changed['reason'], 'endpoint_counter_mismatch')
        finally:
            self.admin.execute("UPDATE public.videos SET views=views-1000 WHERE video_id='L0000000030'")


# Fields a Library browse selects so that the frozen row carries every Inspector value.
INSPECTED = ('video.id', 'video.title', 'video.thumbnail_url', 'video.duration_seconds', 'video.views', 'video.likes',
             'video.comments', 'video.published_at', 'video.category_id', 'video.tags',
             # The static calculations are registry fields: frozen with their inputs.
             'video.like_rate', 'video.comment_rate', 'video.title_length', 'video.tag_count')


if __name__ == '__main__':
    unittest.main(verbosity=2)
