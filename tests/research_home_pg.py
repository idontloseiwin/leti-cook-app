"""Home feed scope, pagination, format windows and view pace on PostgreSQL."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query.validation import AccessContext, QueryRejected, canonical
from research_query.service import QueryService
from research_query.operations import OPERATIONS
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from research_query_pg import bootstrap
from research_frozen_pg import add_channel

A, B, C = 'UC' + 'A' * 22, 'UC' + 'B' * 22, 'UC' + 'C' * 22
INC = 'UC' + 'I' * 22    # visible, Long inventory not complete
HID = 'UC' + 'H' * 22    # hidden
TRASH = 'UC' + 'T' * 22  # in the channel trash
PER_CHANNEL, SPACING = 400, 107  # minutes between a channel's uploads: about 29.7 days
WINDOWS = {'24h': '24', '7d': '168', '28d': '672'}
SELECT = ['video.id', 'video.channel_id', 'video.title', 'video.thumbnail_url', 'video.published_at',
          'video.views', 'video.likes', 'video.comments', 'video.lifetime_views_per_day']
PACE_ID = 'video.view_pace_median.v1'
# The views definition every fixture counter was recorded with: each
# counter has its real snapshot, as the collector writes them.
EPOCH = 'youtube.views.play_start.20260824'
UPCOMING, LIVE, DATE_DIFFERS = 'supcoming01', 'slivenow001', 'sdatediffer'
YOUNG, ZERO, PRIVATE, PRECHECK, NO_VIEWS, LOST = 'syoung00001', 'szeroviews1', 'sprivate001', 'sprecheck01', 'snoviews001', 'sidentity01'
# Home summary's typed count with an independent query: the protected view over every membership.
MEMBERSHIP_COUNT_SQL = ("SELECT video_type,count(*) AS count,min(source_checked_at) AS source_checked_at,"
                     "min(source_expires_at) AS source_expires_at FROM readable.hot_video_inventory "
                     "WHERE channel_id = ANY(%(channels)s::text[]) AND video_type = ANY(%(types)s::text[]) GROUP BY video_type")


def bulk(admin, channel, letter, now, count, *, spacing=SPACING, offset_seconds=0, kind='long'):
    """`count` uploads every `spacing` minutes back from `now`; the counter and
    metadata were refreshed with a cadence that slows with age (up to a day)."""
    ids = f"'{letter}'||lpad(n::text,10,'0')"
    params = dict(channel=channel, kind=kind, count=count, now=now, spacing=spacing, offset=offset_seconds,
                  playlist={'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}[kind] + channel[2:])
    published = "%(now)s::timestamptz-make_interval(mins=>n*%(spacing)s)-make_interval(secs=>%(offset)s)"
    checked = "%(now)s::timestamptz-make_interval(mins=>(n*7)%%least(60+n,1440))"
    admin.execute(f"""INSERT INTO public.video_inventory(video_id,channel_id,video_type)
        SELECT {ids},%(channel)s,%(kind)s FROM generate_series(1,%(count)s::int) n""", params)
    admin.execute(f"""INSERT INTO public.video_inventory_memberships
        (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
        SELECT {ids},%(channel)s,%(kind)s,%(playlist)s,{published},%(now)s::timestamptz-interval '1 hour',
         %(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour','legacy_inventory_backfill'
        FROM generate_series(1,%(count)s::int) n""", params)
    admin.execute(f"""INSERT INTO public.videos
        (video_id,channel_id,title,views,like_count,comment_count,checked_at,metadata_checked_at,published_at,duration,
         availability_status,availability_checked_at)
        SELECT {ids},%(channel)s,'Home video '||n,(n*7919)%%100000+n,n%%97,n%%13,{checked},{checked},{published},
         'PT'||(60+n%%3000)||'S','public',%(now)s::timestamptz-interval '1 hour'
        FROM generate_series(1,%(count)s::int) n""", params)
    admin.execute(f"""INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch)
        SELECT {ids},{checked},(n*7919)%%100000+n,%(epoch)s FROM generate_series(1,%(count)s::int) n""", {**params, 'epoch': EPOCH})


def video(admin, channel, video_id, now, *, published, inventory_published=None, checked=None, views=1000,
          availability='public', publication=None, kind='long', end=None, present=None):
    """One hand-made video; clocks are offsets from `now` (timedelta) or None."""
    checked = now - (checked if checked is not None else timedelta(hours=1))
    present = now - (present if present is not None else timedelta(hours=1))
    admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)', (video_id, channel, kind))
    admin.execute('''INSERT INTO public.video_inventory_memberships
        (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')''',
        (video_id, channel, kind, {'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}[kind] + channel[2:],
         now - (inventory_published if inventory_published is not None else published), present, present, present))
    admin.execute('''INSERT INTO public.videos
        (video_id,channel_id,title,views,like_count,comment_count,checked_at,metadata_checked_at,published_at,duration,
         availability_status,availability_checked_at,publication_status,stream_actual_end_at)
        VALUES(%s,%s,%s,%s,7,3,%s,%s,%s,'PT600S',%s,%s,%s,%s)''',
        (video_id, channel, 'Special ' + video_id, views, checked, checked, now - published, availability,
         now - timedelta(hours=1), publication, None if end is None else now - end))
    if views is not None:
        admin.execute('INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) VALUES(%s,%s,%s,%s)',
                      (video_id, checked, views, EPOCH))


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))




def median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def half_even(value):
    scaled = value * 1_000_000
    whole, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest > scaled.denominator or (2 * rest == scaled.denominator and whole % 2):
        whole += 1
    return f'{whole // 1_000_000}.{whole % 1_000_000:06d}'.rstrip('0').rstrip('.')


def home_query(hours, *, kind='long', size=20, where=None, after=None):
    window = {'field': 'video.published_at', 'op': 'within_last_hours_v1', 'value': hours}
    return dict(query_version=1, source={'id': 'public.videos.current.v1'}, scope={'kind': 'library'},
                video_types=[kind], select=list(SELECT), where=where or window,
                order_by=[{'field': 'video.published_at', 'direction': 'desc'}, {'field': 'video.id', 'direction': 'desc'}],
                page={'size': size, **({'after': after} if after else {})})


def continuation(hours, rows):
    """The Home adapter's "Load 20 more": the window, then everything at or
    before the last row's publication except the rows already shown at it."""
    last = rows[-1]['cells']['video.published_at']['value']
    tied = sorted(row['id'] for row in rows if row['cells']['video.published_at']['value'] == last)
    return {'all': [{'field': 'video.published_at', 'op': 'within_last_hours_v1', 'value': hours},
                    {'field': 'video.published_at', 'op': 'lte', 'value': last},
                    {'not': {'field': 'video.id', 'op': 'in', 'value': tied}}]}


class Harness:
    @classmethod
    def connect(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        password = os.environ['LIC_QUERY_TEST_PASSWORD']
        cls.port = int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=cls.port, dbname='leti-cook', user='postgres',
                                    password=password, autocommit=True)
        version = cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000 <= int(version) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        cls.context = AccessContext('synthetic-installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
        cls.db = PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={cls.port} dbname=leti-cook user=authenticator password={password}', max_size=3),
                                  cadence_snapshot=__import__('collection_schedule').CollectionSchedules().cadence_snapshot(),
                                  admission=lambda _: None).open()
        cls.repo = ResearchQueryRepository(cls.db)
        cls.service = QueryService(cls.repo, lambda: cls.context)

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

    def now(self):
        return self.admin.execute('SELECT clock_timestamp()').fetchone()[0]


    def new_pages(self, hours, kind='long'):
        """First page, then "Load 20 more" by key until the end."""
        pages = [self.service.execute(canonical(home_query(hours, kind=kind)))]
        loaded = list(pages[0]['rows'])
        while pages[-1]['page']['has_more']:
            pages.append(self.service.execute(canonical(home_query(hours, kind=kind, where=continuation(hours, loaded)))))
            loaded.extend(pages[-1]['rows'])
        return pages

    def cursor_rows(self, hours, kind='long'):
        page = self.service.execute(canonical(home_query(hours, kind=kind, size=200)))
        rows = list(page['rows'])
        while page['page']['has_more']:
            page = self.service.execute(canonical(home_query(hours, kind=kind, size=200, after=page['page']['next_cursor'])))
            rows.extend(page['rows'])
        return rows

    def pace_median(self, hours, kind='long'):
        query = home_query(hours, kind=kind)
        return self.service.execute_operation(canonical(dict(version=1, operation=PACE_ID, query=query, parameters={})))


class HomePostgres(Harness, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connect()
        now = cls.seeded_at = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        for folder in range(2):
            cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Home fixture')", (str(uuid.uuid4()),))
        folders = [row[0] for row in cls.admin.execute('SELECT id::text FROM public.folders ORDER BY name,id').fetchall()]
        for channel, folder, hidden in ((A, 0, False), (B, 0, False), (C, 1, False), (INC, 1, False), (HID, 0, True), (TRASH, 1, True)):
            add_channel(cls.admin, channel, folders[folder], now, hidden=hidden)
        cls.admin.execute('UPDATE public.channel_collection_state SET hidden_at=%s WHERE channel_id=%s', (now, TRASH))
        cls.admin.execute("UPDATE public.channel_playlist_state SET inventory_status='not_started',last_completed_scan_id=NULL,"
                          "inventory_completed_at=NULL WHERE channel_id=%s AND video_type='long'", (INC,))
        # A and B upload at the same instants (ties ordered by video id), C 17 s apart.
        bulk(cls.admin, A, 'a', now, PER_CHANNEL)
        bulk(cls.admin, B, 'b', now, PER_CHANNEL)
        bulk(cls.admin, C, 'c', now, PER_CHANNEL, offset_seconds=17)
        bulk(cls.admin, INC, 'i', now, 30, spacing=300)
        bulk(cls.admin, HID, 'h', now, 40)
        bulk(cls.admin, TRASH, 't', now, 40)
        hours, minutes = (lambda value: timedelta(hours=value)), (lambda value: timedelta(minutes=value))
        video(cls.admin, A, UPCOMING, now, published=-hours(2), checked=minutes(5), publication='upcoming')
        video(cls.admin, A, LIVE, now, published=minutes(30), checked=minutes(5), publication='live')
        video(cls.admin, A, DATE_DIFFERS, now, published=timedelta(days=5, minutes=3), inventory_published=timedelta(days=10, minutes=3))
        video(cls.admin, A, YOUNG, now, published=minutes(20), checked=minutes(10), views=1000)
        video(cls.admin, B, ZERO, now, published=timedelta(days=2, minutes=1), views=0)
        video(cls.admin, B, PRIVATE, now, published=timedelta(days=1, minutes=1), availability='private')
        video(cls.admin, C, PRECHECK, now, published=minutes(90), checked=minutes(100), views=50)
        video(cls.admin, C, NO_VIEWS, now, published=timedelta(days=3, minutes=1), views=None)
        video(cls.admin, C, LOST, now, published=timedelta(days=1, minutes=2), present=hours(700))
        video(cls.admin, HID, 'shidden0001', now, published=hours(5))
        video(cls.admin, TRASH, 'strash00001', now, published=hours(5))
        for index in range(5):
            video(cls.admin, A, f'hshort0000{index}', now, published=hours(3 + index), kind='short')
        video(cls.admin, A, 'hstream0001', now, published=hours(5), checked=minutes(30), end=hours(3), kind='stream')
        video(cls.admin, A, 'hstream0002', now, published=hours(4), checked=minutes(30), kind='stream')
        started = time.perf_counter()
        cls.refresh()
        print('HOME_FIXTURE ' + json.dumps({'videos': cls.admin.execute('SELECT count(*) FROM public.videos').fetchone()[0],
                                           'refresh_s': round(time.perf_counter() - started, 2)}), flush=True)

    def db_rows(self, ids):
        rows = self.admin.execute('SELECT video_id,views,published_at,checked_at FROM public.videos WHERE video_id=ANY(%s)', (list(ids),)).fetchall()
        return {row[0]: row[1:] for row in rows}

    def test_feed_paging_scope_and_median(self):
        for window, hours in WINDOWS.items():
            with self.subTest(window=window):
                new = self.new_pages(hours)
                new_rows = [row for page in new for row in page['rows']]
                new_ids = [row['id'] for row in new_rows]
                self.assertEqual(len(new_ids), len(set(new_ids)))
                self.assertEqual(new_ids, [row['id'] for row in self.cursor_rows(hours)])
                self.assertTrue(all(len(page['rows']) == 20 for page in new[:-1]))
                self.assertFalse({UPCOMING, LIVE} & set(new_ids))
                self.assertNotIn(PRIVATE, new_ids)
                self.assertNotIn(LOST, new_ids)
                self.assertFalse({'shidden0001', 'strash00001'} & set(new_ids))
                self.assertFalse(any(video_id.startswith(('h', 't')) for video_id in new_ids))
                keys = [(row['cells']['video.published_at']['value'], row['id']) for row in new_rows]
                self.assertEqual(keys, sorted(keys, reverse=True))
                # The pace of every row: views*86400/max(counter age, 1 h), half-even to 6 places.
                stored = self.db_rows(new_ids)
                states = {}
                for row in new_rows:
                    views, published, checked = stored[row['id']]
                    cell = row['cells']['video.lifetime_views_per_day']
                    states[cell['state']] = states.get(cell['state'], 0) + 1
                    if views is None:
                        self.assertEqual(cell['state'], 'unknown')
                    elif checked < published:
                        self.assertEqual(cell['state'], 'unavailable')
                    else:
                        age = max((checked - published) // timedelta(microseconds=1), 3_600_000_000)
                        self.assertEqual(cell['value'], half_even(Fraction(views * 86_400_000_000, age)), row['id'])
                result = self.pace_median(hours)
                known = [Fraction(row['cells']['video.lifetime_views_per_day']['value']) for row in new_rows
                         if row['cells']['video.lifetime_views_per_day']['state'] == 'known']
                expected = median(known)
                self.assertEqual(result['result']['median'], half_even(expected))
                self.assertEqual(result['result']['population_count'], len(new_rows))
                self.assertEqual(result['result']['known_count'], len(known))
                self.assertEqual(result['result']['missing'], {'unknown': states.get('unknown', 0), 'expired': states.get('expired', 0),
                                                              'unavailable': states.get('unavailable', 0)})

    def test_library_scope_coverage_and_hidden_channels(self):
        page = self.service.execute(canonical(home_query('168')))
        coverage = page['coverage']
        # A, B, C and INC are visible; the hidden and the trashed channel are not in scope.
        self.assertEqual((coverage['channels_in_scope'], coverage['channels_complete']), (4, 3))
        self.assertEqual(coverage['reasons'].get('inventory_incomplete'), 1)
        self.assertEqual(coverage['status'], 'partial')
        evaluated, valid = instant(page['context']['evaluated_at']), instant(page['context']['valid_before'])
        self.assertLessEqual(valid - evaluated, timedelta(seconds=120))
        cursor = self.service.execute(canonical(home_query('168', size=5)))['page']['next_cursor']
        try:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=clock_timestamp() WHERE channel_id=%s', (B,))
            with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
                self.service.execute(canonical(home_query('168', size=5, after=cursor)))
            rows = self.cursor_rows('168')
            self.assertTrue(rows and not any(row['cells']['video.channel_id']['value'] == B for row in rows))
            self.assertEqual(self.service.execute(canonical(home_query('168')))['coverage']['channels_in_scope'], 3)
        finally:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false,hidden_at=NULL WHERE channel_id=%s', (B,))
        self.assertTrue(any(row['cells']['video.channel_id']['value'] == B for row in self.cursor_rows('168')))
        # Restored from the trash: back in the library.
        try:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=false,hidden_at=NULL WHERE channel_id=%s', (TRASH,))
            self.assertIn('strash00001', [row['id'] for row in self.cursor_rows('24')])
        finally:
            self.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=clock_timestamp() WHERE channel_id=%s', (TRASH,))

    def test_the_scan_bound_never_changes_the_rows(self):
        for hours in WINDOWS.values():
            window = {'field': 'video.published_at', 'op': 'within_last_hours_v1', 'value': hours}
            bounded = [row['id'] for row in self.cursor_rows(hours)]
            unbounded_query = home_query(hours, size=200, where={'any': [window, window]})
            page = self.service.execute(canonical(unbounded_query))
            rows = list(page['rows'])
            while page['page']['has_more']:
                page = self.service.execute(canonical({**unbounded_query, 'page': {'size': 200, 'after': page['page']['next_cursor']}}))
                rows.extend(page['rows'])
            self.assertEqual(bounded, [row['id'] for row in rows])

    def test_every_type_reads_its_own_window(self):
        shorts = [row['id'] for row in self.cursor_rows('24', 'short')]
        self.assertEqual(shorts, [f'hshort0000{index}' for index in range(5)])
        self.assertEqual([row['id'] for row in self.cursor_rows('24', 'stream')], ['hstream0001'])
        longs = [row['id'] for row in self.cursor_rows('24')]
        self.assertFalse(any(video_id.startswith('hs') for video_id in longs))
        for kind in ('short', 'stream'):
            result = self.pace_median('24', kind)['result']
            self.assertEqual(result['population_count'], len(self.cursor_rows('24', kind)))

    def test_the_median_lives_until_its_oldest_row_leaves_the_window(self):
        for hours in WINDOWS.values():
            with self.subTest(hours=hours):
                response = self.pace_median(hours)
                rows = self.cursor_rows(hours)
                evaluated, valid = instant(response['context']['evaluated_at']), instant(response['context']['valid_before'])
                oldest = min(instant(row['cells']['video.published_at']['value']) for row in rows)
                # Data clocks and the window edge, not the 120 s of a page.
                self.assertEqual(valid, oldest + timedelta(hours=int(hours)))
                self.assertGreater(valid - evaluated, timedelta(seconds=120))
                self.assertEqual(response['operation'], PACE_ID)
                self.assertEqual(response['coverage']['channels_in_scope'], 4)

    def test_young_and_odd_rows(self):
        rows = {row['id']: row['cells'] for row in self.cursor_rows('672')}
        # 20 minutes old, counter at 10 minutes: one hour minimum age.
        self.assertEqual(rows[YOUNG]['video.lifetime_views_per_day']['value'], '24000')
        self.assertEqual(rows[ZERO]['video.lifetime_views_per_day']['value'], '0')
        self.assertEqual(rows[PRECHECK]['video.lifetime_views_per_day']['state'], 'unavailable')
        self.assertEqual(rows[NO_VIEWS]['video.lifetime_views_per_day']['state'], 'unknown')
        self.assertEqual(rows[NO_VIEWS]['video.views']['state'], 'unknown')
        # The pace cell carries the counter observation.
        self.assertEqual(rows[YOUNG]['video.lifetime_views_per_day']['observed_at'], rows[YOUNG]['video.views']['observed_at'])

    def test_the_median_follows_the_views_definition_rule(self):
        """(the population statistics query): a value whose counter has no recorded views
        definition is left out on its own and counted; two recorded definitions withhold the median."""
        hours = '672'
        rows = self.cursor_rows(hours)
        known = {row['id']: Fraction(row['cells']['video.lifetime_views_per_day']['value']) for row in rows
                 if row['cells']['video.lifetime_views_per_day']['state'] == 'known'}
        first = self.pace_median(hours)['result']
        self.assertEqual((first['definition'], first['known_count'], first['median']),
                         ({'state': 'known', 'epoch': EPOCH, 'excluded_count': 0}, len(known), half_even(median(known.values()))))
        target = sorted(known)[len(known) // 2]
        stored = self.admin.execute('SELECT recorded_at,views FROM public.video_snapshots WHERE video_id=%s', (target,)).fetchall()
        self.assertEqual(len(stored), 1)
        try:
            # No snapshot recorded exactly this counter: that one value is left out.
            self.admin.execute('DELETE FROM public.video_snapshots WHERE video_id=%s', (target,))
            result = self.pace_median(hours)['result']
            rest = [value for video_id, value in known.items() if video_id != target]
            self.assertEqual((result['definition'], result['known_count'], result['median']),
                             ({'state': 'known', 'epoch': EPOCH, 'excluded_count': 1}, len(rest), half_even(median(rest))))
            self.assertEqual(result['population_count'], first['population_count'])
            # The row keeps its own pace; only the median leaves it out.
            cells = {row['id']: row['cells'] for row in self.cursor_rows(hours)}
            self.assertEqual(cells[target]['video.lifetime_views_per_day']['state'], 'known')
            # Another recorded definition in the same window: the median is withheld, every value counted out.
            self.admin.execute('INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) VALUES(%s,%s,%s,%s)',
                               (target, stored[0][0], stored[0][1], 'fixture.other.v1'))
            mixed = self.pace_median(hours)['result']
            self.assertEqual((mixed['definition'], mixed['known_count'], mixed['median']),
                             ({'state': 'mixed_definition_epochs', 'epoch': None, 'excluded_count': len(known)}, 0, None))
        finally:
            self.admin.execute('DELETE FROM public.video_snapshots WHERE video_id=%s', (target,))
            self.admin.execute('INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) VALUES(%s,%s,%s,%s)',
                               (target, stored[0][0], stored[0][1], EPOCH))
        self.assertEqual(self.pace_median(hours)['result']['median'], first['median'])

    def test_summary_counts_read_the_model_with_the_live_numbers(self):
        # Home summary's typed counts read the catalogue read model. The
        # counts and both earliest clocks equal the protected view's whatever each
        # row's state: current, queued, past its deadline, never refreshed, without
        # a proof, conflicting, moved, future or old evidence, hidden channel,
        # automatic or manual type proof. Every state runs in a rolled-back
        # transaction as lic_reader, the live-view statement right next to the read-model one.
        # In every state the narrow view the count reads has the
        # full view's rows and values.
        from db.reads.frontend import FrontendRepository, INVENTORY_COUNT_SQL
        from db.base.read_context import http_method_scope, http_read_scope
        visible, kinds = [A, B, C, INC], ['long', 'short', 'stream']
        scopes = (visible, visible + [HID, TRASH], [A], [HID], ['UC' + 'Z' * 22])
        membership = ("INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,first_seen_at,"
                      "last_seen_at,last_present_at,evidence_source) VALUES(%s,%s,%s,%s,clock_timestamp(),clock_timestamp(),"
                      "clock_timestamp(),'legacy_inventory_backfill')")
        states = {
            'refreshed': (),
            'queued': (("INSERT INTO public.video_search_projection_queue(video_id) SELECT video_id FROM public.video_search_projection"
                        " WHERE right(video_id,1) IN ('1','2','3') ON CONFLICT DO NOTHING", ()),),
            'all_queued': (('INSERT INTO public.video_search_projection_queue(video_id) SELECT video_id FROM public.video_search_projection'
                            ' ON CONFLICT DO NOTHING', ()),),
            'past_deadline': (("UPDATE public.video_search_projection SET valid_until=statement_timestamp()-interval '1 second'"
                               " WHERE right(video_id,1) IN ('4','7')", ()),),
            'never_refreshed': (("UPDATE public.video_search_projection SET refreshed_at=NULL,valid_until='-infinity'"
                                 " WHERE right(video_id,1)='5'", ()),),
            # Writes queue their videos through the triggers, as the collector's do: a lost
            # proof, a move, a second channel, a second type, a manual decision, future and old evidence.
            'writes': (
                ("DELETE FROM public.video_inventory_memberships WHERE video_id IN ('a0000000011','b0000000012')", ()),
                ('UPDATE public.video_inventory SET channel_id=%s WHERE video_id=%s', (B, 'a0000000021')),
                (membership, ('a0000000031', B, 'long', 'UULF' + B[2:])),
                (membership, ('a0000000041', A, 'short', 'UUSH' + A[2:])),
                ("INSERT INTO public.video_type_review_cases(video_id,channel_id,state,revision,decision_type,decision_reason,"
                 "decision_at,decision_revision) VALUES(%s,%s,'resolved',2,'long','fixture',clock_timestamp(),2)", ('c0000000051', C)),
                ("UPDATE public.video_inventory_memberships SET last_seen_at=clock_timestamp()+interval '1 hour',"
                 "last_present_at=clock_timestamp()+interval '1 hour' WHERE video_id=%s", ('c0000000061',)),
                ("UPDATE public.video_inventory_memberships SET first_seen_at=clock_timestamp()-interval '697 hours',"
                 "last_seen_at=clock_timestamp()-interval '697 hours',last_present_at=clock_timestamp()-interval '697 hours'"
                 " WHERE video_id=%s", ('b0000000071',))),
        }

        def counts(statement, channels, types):
            rows = self.admin.execute(statement, dict(channels=channels, types=types), prepare=False).fetchall()
            return sorted(rows)

        # The narrow view and the full one, in one statement (one statement_timestamp).
        own = 'video_id,channel_id,proof_current,inventory_type,hot_present,identity_clock,identity_expiry'
        both = ('SELECT (SELECT array_agg(t::text ORDER BY t.video_id) FROM (SELECT ' + own
                + ' FROM readable.video_search_projection_types) t),(SELECT array_agg(t::text ORDER BY t.video_id) FROM (SELECT '
                + own + ' FROM readable.video_search_projection) t)')

        for name, writes in states.items():
            with self.subTest(state=name), self.admin.transaction(force_rollback=True):
                for write, args in writes:
                    self.admin.execute(write, args or None)
                self.admin.execute('SET LOCAL ROLE lic_reader')
                self.admin.execute("SELECT set_config('TimeZone','UTC',true)")
                live = self.admin.execute('SELECT count(*) FROM readable.video_search_projection WHERE proof_current IS NOT TRUE').fetchone()[0]
                # A seed deadline may pass while the suite runs, so only the other states assert their live rows.
                self.assertTrue(name == 'refreshed' or live > 0, live)
                # Row for row, the narrow view is the full view's own read-model columns.
                narrow, full = self.admin.execute(both).fetchone()
                self.assertGreater(len(full), 1000)
                self.assertEqual(narrow, full)
                for channels in scopes:
                    for types in (kinds, ['short'], ['stream', 'long']):
                        expected_counts = counts(MEMBERSHIP_COUNT_SQL, channels, types)
                        self.assertEqual(counts(INVENTORY_COUNT_SQL, channels, types), expected_counts, (channels, types))
                totals = {row[0]: row[1] for row in counts(MEMBERSHIP_COUNT_SQL, visible, kinds)}
                self.assertGreater(totals['long'], 1000)
                self.assertEqual(totals['stream'], 2)
        # A current row is read from the model, not live: a changed stored clock shows,
        # and once the row is queued it is read live again.
        with self.admin.transaction(force_rollback=True):
            self.admin.execute("UPDATE public.video_search_projection SET identity_clock='2000-01-01Z' WHERE video_id='hstream0001'")
            self.admin.execute('SET LOCAL ROLE lic_reader')
            model = counts(INVENTORY_COUNT_SQL, visible, ['stream'])
            self.assertEqual(model[0][2], datetime(2000, 1, 1, tzinfo=timezone.utc))
            self.assertNotEqual(counts(MEMBERSHIP_COUNT_SQL, visible, ['stream'])[0][2], model[0][2])
            self.admin.execute('RESET ROLE')
            self.admin.execute("INSERT INTO public.video_search_projection_queue(video_id) VALUES('hstream0001')")
            self.admin.execute('SET LOCAL ROLE lic_reader')
            self.assertEqual(counts(INVENTORY_COUNT_SQL, visible, ['stream']), counts(MEMBERSHIP_COUNT_SQL, visible, ['stream']))
        # The repository outside a request and inside one: the same numbers, read as lic_reader.
        expected = {row[0]: row[1] for row in counts(MEMBERSHIP_COUNT_SQL, visible, kinds)}
        self.assertEqual(FrontendRepository(self.db).count_inventory_by_type(visible, kinds), expected)
        with http_method_scope('GET'), http_read_scope():
            self.assertEqual(FrontendRepository(self.db).count_inventory_by_type(visible + visible, kinds + ['long']), expected)

    def test_the_window_index_reads_the_page_the_full_scope_reads(self):
        # A windowed page of several channels starts from the videos whose shown
        # publication time is in the window (readable.video_publication_times). In every state
        # below, as lic_reader, its whole answer (rows, coverage counts, deadline) and its raw rows equal
        # those of the same statement reading the full scope; both in one statement (one statement_timestamp).
        from unittest.mock import patch as patched
        import db.reads.research_sql as research_sql
        from research_query.validation import validate
        last = "right(video_id,1)"
        states = {
            'as_seeded': (),
            # Masked or not yet shown: metadata past the window, a future metadata clock, just inside its edge.
            'metadata': (f"UPDATE public.videos SET metadata_checked_at=clock_timestamp()-interval '697 hours' WHERE {last}='3'",
                         f"UPDATE public.videos SET metadata_checked_at=clock_timestamp()+interval '1 hour' WHERE {last}='6'",
                         f"UPDATE public.videos SET metadata_checked_at=clock_timestamp()-interval '696 hours'+interval '1 minute'"
                         f" WHERE {last}='8'"),
            # Publication times moved into the window, out of it and into the future (each queues its video).
            'published': (f"UPDATE public.videos SET published_at=clock_timestamp()-interval '2 hours' WHERE {last}='2'",
                          f"UPDATE public.videos SET published_at=clock_timestamp()-interval '30 days' WHERE {last}='5'",
                          f"UPDATE public.videos SET published_at=clock_timestamp()+interval '2 hours' WHERE {last}='9'"),
            'queued_and_past_deadline': (
                f"INSERT INTO public.video_search_projection_queue(video_id) SELECT video_id FROM public.video_search_projection"
                f" WHERE {last} IN ('1','4') ON CONFLICT DO NOTHING",
                f"UPDATE public.video_search_projection SET valid_until=statement_timestamp()-interval '1 second' WHERE {last}='7'"),
            'hidden_channel': (f"UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=clock_timestamp() WHERE channel_id='{B}'",),
        }

        def statements(hours, kind):
            plan = validate(canonical(home_query(hours, kind=kind, size=200)), self.context)
            indexed, params = research_sql.compile_query(plan)
            with patched.object(research_sql, 'WINDOW_SCOPES', frozenset()):
                full, same_params = research_sql.compile_query(plan)
            self.assertEqual(params, same_params)
            self.assertIn(research_sql._WINDOW_FROM, indexed)
            self.assertNotIn('video_publication_times', full)
            return indexed, full, params

        def raw(statement):
            return statement.partition('\npage AS MATERIALIZED (')[0].rstrip().removesuffix(',') + '\nSELECT * FROM raw'

        for name, writes in states.items():
            with self.subTest(state=name), self.admin.transaction(force_rollback=True):
                for write in writes:
                    self.admin.execute(write)
                self.admin.execute('SET LOCAL ROLE lic_reader')
                self.admin.execute("SELECT set_config('TimeZone','UTC',true)")
                for hours in WINDOWS.values():
                    for kind in ('long', 'short'):
                        indexed, full, params = statements(hours, kind)
                        new, old = self.admin.execute('SELECT to_jsonb(n),to_jsonb(o) FROM (' + indexed + ') n CROSS JOIN ('
                                                      + full + ') o', params, prepare=False).fetchone()
                        self.assertEqual(new, old, (name, hours, kind))
                        only = self.admin.execute(
                            'WITH n AS MATERIALIZED (SELECT * FROM (' + raw(indexed) + ') z), o AS MATERIALIZED (SELECT * FROM ('
                            + raw(full) + ') z) SELECT (SELECT count(*) FROM (SELECT * FROM n EXCEPT ALL SELECT * FROM o) a),'
                            '(SELECT count(*) FROM (SELECT * FROM o EXCEPT ALL SELECT * FROM n) b),(SELECT count(*) FROM o)',
                            params, prepare=False).fetchone()
                        self.assertEqual(only[:2], (0, 0), (name, hours, kind))
                        if name == 'published' and kind == 'long':
                            self.assertGreater(only[2], 0)

    def test_frozen_reads_keep_the_window(self):
        query = {**home_query('168', size=50), 'consistency': 'frozen'}
        page = self.service.execute(canonical(query))
        ids = list(self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': page['context']['frozen_ref']}))['ids'])
        self.assertEqual(ids, [row['id'] for row in self.cursor_rows('168')])




@unittest.skipUnless(os.environ.get('LIC_TEST_LARGE_HOME') == '1', 'enable the 250,200-video Home fixture')
class HomeBudget(Harness, unittest.TestCase):
    """900 visible channels, 250,200 Long videos over three years (synthetic)."""
    CHANNELS, PER = 900, 278

    @classmethod
    def setUpClass(cls):
        cls.connect()
        now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        started = time.perf_counter()
        folder = str(uuid.uuid4())
        cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Budget')", (folder,))
        cls.admin.execute("""INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at)
            SELECT 'UC'||lpad(c::text,22,'0'),gen_random_uuid(),%s::uuid,%s FROM generate_series(1,%s) c""", (folder, now, cls.CHANNELS))
        cls.admin.execute("""INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch)
            SELECT channel_id,user_work_id,'accepted',false,1 FROM public.channels""")
        for kind, prefix in (('long', 'UULF'), ('short', 'UUSH'), ('stream', 'UULV')):
            cls.admin.execute("""INSERT INTO public.channel_video_inventory_scans
                (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at)
                SELECT gen_random_uuid(),channel_id,%(kind)s,%(prefix)s||substr(channel_id,3),'initial_inventory','completed','complete',
                 %(now)s::timestamptz-interval '2 hours',%(now)s::timestamptz-interval '2 hours',%(now)s::timestamptz-interval '1 hour',
                 %(now)s::timestamptz-interval '1 hour' FROM public.channels""", dict(kind=kind, prefix=prefix, now=now))
            cls.admin.execute("""INSERT INTO public.channel_playlist_state
                (channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at)
                SELECT channel_id,video_type,playlist_id,'completed',scan_id,%(now)s::timestamptz-interval '1 hour',
                 %(now)s::timestamptz-interval '1 hour' FROM public.channel_video_inventory_scans WHERE video_type=%(kind)s""",
                dict(kind=kind, now=now))
        # Each channel uploads every ~3.9 days, shifted by channel: about 230
        # videos in 24 h, 1,600 in 7 days and 6,400 in 28 days across the library.
        series = f"""FROM generate_series(1,{cls.CHANNELS}) c CROSS JOIN generate_series(1,{cls.PER}) n"""
        ids = "'v'||lpad((c*1000+n)::text,10,'0')"
        age = "((n-1)*5671+c*6+1)"
        published = f"%(now)s::timestamptz-make_interval(mins=>{age})"
        # Counters refreshed after publication, at most a day ago.
        checked = f"%(now)s::timestamptz-make_interval(mins=>least({age}/10,(n*7+c)%%1440))"
        channel = "'UC'||lpad(c::text,22,'0')"
        cls.admin.execute(f"INSERT INTO public.video_inventory(video_id,channel_id,video_type) SELECT {ids},{channel},'long' {series}")
        cls.admin.execute(f"""INSERT INTO public.video_inventory_memberships
            (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
            SELECT {ids},{channel},'long','UULF'||lpad(c::text,22,'0'),{published},%(now)s::timestamptz-interval '1 hour',
             %(now)s::timestamptz-interval '1 hour',%(now)s::timestamptz-interval '1 hour','legacy_inventory_backfill' {series}""", dict(now=now))
        cls.admin.execute(f"""INSERT INTO public.videos
            (video_id,channel_id,title,views,like_count,comment_count,checked_at,metadata_checked_at,published_at,duration,
             availability_status,availability_checked_at)
            SELECT {ids},{channel},'Budget video '||n,(n*7919+c)%%1000000,n%%97,n%%13,{checked},{checked},{published},
             'PT'||(60+n%%3000)||'S','public',%(now)s::timestamptz-interval '1 hour' {series}""", dict(now=now))
        cls.admin.execute(f"""INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch)
            SELECT {ids},{checked},(n*7919+c)%%1000000,%(epoch)s {series}""", dict(now=now, epoch=EPOCH))
        cls.admin.execute('ANALYZE')
        cls.timings = {'seed_s': round(time.perf_counter() - started, 1),
                       'videos': cls.admin.execute('SELECT count(*) FROM public.videos').fetchone()[0]}

    def measure(self, label, call, repeat=3):
        values = []
        for _ in range(repeat):
            started = time.perf_counter()
            result = call()
            values.append(time.perf_counter() - started)
        self.timings[label] = {'min_s': round(min(values), 3), 'max_s': round(max(values), 3),
                               'bytes': len(json.dumps(result, default=str))}
        return result

    def run_all(self, phase):
        for window, hours in WINDOWS.items():
            first = self.measure(f'{phase}.new_first_page.{window}', lambda: self.service.execute(canonical(home_query(hours))))
            self.measure(f'{phase}.new_load_more.{window}', lambda: self.service.execute(
                canonical(home_query(hours, where=continuation(hours, first['rows'])))))
            result = self.measure(f'{phase}.pace_median.{window}', lambda: self.pace_median(hours))
            self.timings[f'{phase}.population.{window}'] = result['result']['population_count']
            self.assertLess(self.timings[f'{phase}.new_load_more.{window}']['max_s'], 8)
            self.assertLess(self.timings[f'{phase}.new_first_page.{window}']['max_s'], 8)
            self.assertLess(self.timings[f'{phase}.pace_median.{window}']['max_s'], 8)
            self.assertLess(self.timings[f'{phase}.pace_median.{window}']['bytes'], 512 * 1024)

    def test_budget(self):
        # Before the read model refresh every row is read from the protected
        # views (the worst case after a restore), then on the refreshed model.
        self.assertEqual(self.timings['videos'], self.CHANNELS * self.PER)
        self.run_all('views')
        started = time.perf_counter()
        self.refresh()
        self.timings['refresh_s'] = round(time.perf_counter() - started, 1)
        self.admin.execute('ANALYZE')
        self.run_all('model')
        print('HOME_BUDGET ' + json.dumps(self.timings, sort_keys=True), flush=True)


if __name__ == '__main__':
    suite = 'HomeBudget' if os.environ.get('LIC_TEST_LARGE_HOME') == '1' else None
    unittest.main(defaultTest=suite, verbosity=2)
