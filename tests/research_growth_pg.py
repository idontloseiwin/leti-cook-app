"""Window, coverage, filters and ordering of observed video growth on PostgreSQL."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from zoneinfo import ZoneInfo

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'application'), str(Path(__file__).resolve().parent)]
import research_query_pg as native
from research_query import growth
from research_query.validation import QueryRejected, canonical

H, D = timedelta(hours=1), timedelta(days=1)
WINDOW = timedelta(hours=672)
EPOCH = 'youtube.views.play_start.20260824'
PREFIX = {'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}
CHANNEL = 'UCgrowthAAAAAAAAAAAAAAAA'
LARGE = 'UCgrowthLARGEAAAAAAAAAAA'
# CI keeps the suite inside its time limit; the 20,000-video budget run sets this to 20000.
LARGE_COUNT = int(os.environ.get('LIC_GROWTH_LARGE_COUNT', '2000'))
# The test cadence: hourly the first day, then 2 h, 6 h, 12 h, daily, weekly after a year.
BANDS = ((86400, 3600), (3 * 86400, 7200), (7 * 86400, 21600), (14 * 86400, 43200), (365 * 86400, 86400), (None, 604800))
# Statuses depend on the UTC date, so the whole run keeps its seed's date (test_12 checks it). Longer than the run
# after the seed clock (about 15 s with CI's 2,000 large videos), and short enough that waiting for the next
# date keeps the suite within CI's 360 s per suite (tools/check_research_query_postgres.sh).
MIDNIGHT_MARGIN = timedelta(seconds=90) * max(1, LARGE_COUNT / 2000)


def wait_before_seeding(T):
    """Seconds until 1 s after midnight when T is less than MIDNIGHT_MARGIN before it, or less than 1 s after it (a
    "today" without length); else 0. Midnight in T's zone, UTC here, as the seed's "today"."""
    today = T.replace(hour=0, minute=0, second=0, microsecond=0)
    for midnight in (today, today + D):
        if midnight - MIDNIGHT_MARGIN <= T < midnight + timedelta(seconds=1):
            return (midnight + timedelta(seconds=1) - T).total_seconds()
    return 0


def cadence(published, until, first_delay=timedelta(minutes=10)):
    points, at = [], published + first_delay
    while at <= until:
        points.append(at)
        age = (at - published).total_seconds()
        at += timedelta(seconds=next(step for limit, step in BANDS if limit is None or age < limit))
    return points


def video(identity, kind, published, points, *, checked=None, metadata=None, membership=None, views=None,
          end=None, availability='public', epochs=None):
    """points: [(at, views)], the last one is the current counter unless overridden."""
    checked = checked or points[-1][0]
    current = views if views is not None or not points else dict(points).get(checked)
    return dict(id=identity, kind=kind, published=published, points=points, checked=checked,
                metadata=metadata, membership=membership, views=current, end=end, availability=availability,
                epochs=epochs or {})


def scenarios(T):
    """Explicit input cases per format, then realistic cadence for all three."""
    today = T.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for kind, tag in (('long', 'L'), ('short', 'S'), ('stream', 'T')):
        end = (lambda at: at) if kind == 'stream' else (lambda at: None)
        def vid(name):
            return ('g' + tag + name + '0' * 11)[:11]
        E = T - 2 * H
        # Full window: a point exactly 28 days before the endpoint; boundary 0.
        out.append(video(vid('readyexa'), kind, T - 60 * D,
                         [(E - k * D, 10000 - 100 * k) for k in range(28, -1, -1)], end=end(T - 59 * D)))
        # Counter decrease is a correction, not clamped.
        out.append(video(vid('decrease'), kind, T - 60 * D, [(E - WINDOW, 1000), (E, 900)], end=end(T - 59 * D)))
        # Young video with the realistic cadence: partial over its own history.
        published = T - 10 * D - 30 * timedelta(minutes=1)
        points = [(at, 50 + int(40 * math.sqrt((at - published).total_seconds() / 3600))) for at in cadence(published, T - 20 * timedelta(minutes=1))]
        out.append(video(vid('young'), kind, published, points, end=end(published)))
        # Tracking started today (UTC) and waiting for the next snapshot. The points follow UTC midnight. The
        # publication (never read by growth; a Stream's actual end here) stays at or before the first point and
        # between bg00 (T - 5 h), bg01 (T - 30 h 07 min) and bg02 (T - 70 h 14 min), so the recorded order by
        # publication holds at any time of day. From 10:00 UTC on these are the old instants.
        span = T - today
        published = min(today + span / 4, T - 6 * H)
        out.append(video(vid('today'), kind, published, [(today + span / 3, 10), (today + 2 * span / 3, 15)], end=end(published)))
        published = min(today - 3 * H, T - 7 * H)
        out.append(video(vid('waitone'), kind, published, [(today - 2 * H, 7)], end=end(published)))
        # Old clocks: counter or metadata beyond 672 h (still inside 696 h): stale for growth only.
        out.append(video(vid('stalecnt'), kind, T - 90 * D, [(T - 700 * H, 10), (T - 680 * H, 20)], end=end(T - 89 * D),
                         metadata=T - 3 * H))
        # An old video with daily counters whose 28-day metadata refresh is 8 h late.
        out.append(video(vid('latemeta'), kind, T - 200 * D, [(E - k * D, 5000 + 40 * (28 - k)) for k in range(28, -1, -1)],
                         metadata=T - 680 * H, end=end(T - 199 * D)))
        # Current counter without its matching snapshot.
        out.append(video(vid('nosnap'), kind, T - 40 * D, [(E - WINDOW, 100), (E, 699)], views=700, end=end(T - 39 * D)))
        if kind == 'long':
            # Window start edges: early (+20 h), late (-2 h), exactly 3 h late, 3 h + 1 s late (partial).
            out.append(video(vid('early'), kind, T - 90 * D, [(T - 1 * H - 692 * H, 5000), (T - 1 * H - 21 * D, 6000),
                             (T - 1 * H - 14 * D, 7000), (T - 1 * H - 7 * D, 8000), (T - 1 * H, 9000)]))
            out.append(video(vid('late'), kind, T - 90 * D, [(T - 3 * H - 670 * H, 3000), (T - 3 * H - 10 * D, 4000), (T - 3 * H, 5000)]))
            out.append(video(vid('edge3h'), kind, T - 90 * D, [(T - 4 * H - 669 * H, 100), (T - 4 * H, 300)]))
            out.append(video(vid('edge3h1s'), kind, T - 90 * D, [(T - 5 * H - 669 * H + timedelta(seconds=1), 100), (T - 5 * H, 500)]))
            # Half-even ties: exactly 8 days, delta 1 / 3 / -1 gives 12.5 / 37.5 / -12.5 hundredths.
            for name, delta in (('tie1', 1), ('tie3', 3), ('tieneg', -1)):
                S = T - 9 * D
                out.append(video(vid(name), kind, S - H, [(S, 1000), (S + 8 * D, 1000 + delta)]))
            out.append(video(vid('zero'), kind, T - 60 * D, [(E - WINDOW, 500), (E, 500)]))
            # Exact decimal arithmetic far beyond 2**53.
            out.append(video(vid('huge'), kind, T - 60 * D, [(E - WINDOW, 0), (E, 9223372036854775000)]))
            out.append(video(vid('waittwo'), kind, min(today - 21 * H, T - 31 * H), [(today - 20 * H, 1), (today - H, 2)]))
            out.append(video(vid('stalemeta'), kind, T - 90 * D, [(E - WINDOW, 10), (E, 20)], metadata=T - 680 * H))
            out.append(video(vid('oldproof'), kind, T - 90 * D, [(E - WINDOW, 10), (E, 20)], membership=T - 680 * H))
            out.append(video(vid('noviews'), kind, T - 90 * D, [(E - WINDOW, 10)], checked=E))
            # Points older than 696 h that cleanup has not removed yet stay invisible.
            out.append(video(vid('expired'), kind, T - 90 * D, [(T - H - 700 * H, 1), (T - H - 600 * H, 50), (T - H, 80)]))
            out.append(video(vid('private'), kind, T - 60 * D, [(E - WINDOW, 1), (E, 2)], availability='private'))
        if kind == 'stream':
            # Points before the actual end (the live broadcast) never start a window.
            ended = T - 10 * D
            live = [(ended - 5 * H, 100), (ended - 3 * H, 400), (ended - H, 900)]
            after = [(at, 1000 + int((at - ended).total_seconds() // 3600)) for at in cadence(ended, T - H)]
            out.append(video(vid('afterend'), kind, ended - 6 * H, live + after, end=ended))
            # An end 1 h after the window start: the first point after it is a full window.
            E2 = T - 2 * H
            ws = E2 - WINDOW
            out.append(video(vid('endnearw'), kind, ws - 6 * H, [(ws - 5 * H, 50), (ws + H + 10 * timedelta(minutes=1), 60)]
                             + [(E2 - k * D, 200 - k) for k in range(27, -1, -1)], end=ws + H))
            # Not ended, and counters read before the end: not admitted by either.
            out.append(video(vid('live'), kind, T - 3 * H, [(T - 2 * H, 10)], end=None))
            out.append(video(vid('cntbefore'), kind, T - 5 * H, [(T - 4 * H, 10)], end=T - 3 * H))
        # Realistic channel history: ages from a few hours to over a year.
        for i, age_hours in enumerate((5, 30, 70, 160, 300, 500, 671, 673, 700, 800, 1500, 3000, 6000, 9000, 12000)):
            published = T - age_hours * H - i * timedelta(minutes=7)
            rate = 3 + (i * 37) % 50
            points = [(at, 100 + int(rate * math.sqrt((at - published).total_seconds() / 3600)))
                      for at in cadence(published, T - (15 + i) * timedelta(minutes=1)) if at >= T - 740 * H]
            # Clean-up band around the retention edge stays empty: the run takes minutes.
            points = [(at, value) for at, value in points if not T - 698 * H <= at <= T - 694 * H]
            if points:
                out.append(video(vid('bg%02d' % i), kind, published, points, end=end(published)))
    return out




def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def shared_view(cells):
    """The shared cells in the view_growth response shape (the adapter does the same)."""
    evidence = cells[growth.STATUS]['observation']
    status = evidence['status']
    integer = lambda value: None if value is None else int(value)
    delta = integer(cells[growth.GAINED]['value'])
    return {'status': status,
            'delta_views': None if delta is None else (str(delta) if abs(delta) > 2**53 - 1 else delta),
            'average_views_per_day': cells[growth.PER_DAY]['value'],
            'tracked_days': integer(cells[growth.TRACKED_DAYS]['value']),
            'observed_start_at': evidence['from_at'] if status != 'unavailable' else None,
            'observed_end_at': evidence['to_at'],
            'actual_interval_seconds': integer(cells[growth.INTERVAL]['value']),
            'boundary_distance_seconds': integer(evidence['boundary_distance_seconds']),
            'reason': evidence['reason']}


class PublicationClockTests(unittest.TestCase):
    def test_snapshots_follow_publication_at_every_seed_time(self):
        day = datetime(2026, 11, 5, tzinfo=timezone.utc)
        for minute in range(0, 24 * 60, 10):
            videos = scenarios(day + timedelta(minutes=minute, seconds=7))
            for v in videos:  # No point before the publication (a Stream's end here).
                self.assertLessEqual(v['published'], min(at for at, _ in v['points']), (minute, v['id']))


class GrowthPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.NativeQueryTests.setUpClass.__func__(cls)
        T = cls.clock()
        wait = wait_before_seeding(T)
        if wait:
            time.sleep(wait)
            T = cls.clock()
        cls.T = T
        folder = str(uuid.uuid4())
        cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Growth fixture')", (folder,))
        cls.channel(CHANNEL, folder, T)
        cls.videos = scenarios(T)
        cls.seed_videos(CHANNEL, cls.videos)
        started = time.monotonic()
        cls.seed_large(folder, T)
        cls.large_seed_seconds = time.monotonic() - started
        started = time.monotonic()
        cls.refresh()
        cls.large_refresh_seconds = time.monotonic() - started
        cls.admin.execute('ANALYZE')

    tearDownClass = classmethod(native.NativeQueryTests.tearDownClass.__func__)

    refresh = classmethod(native.NativeQueryTests.refresh.__func__)

    @classmethod
    def clock(cls):
        return cls.admin.execute('SELECT statement_timestamp()').fetchone()[0]

    @classmethod
    def channel(cls, channel_id, folder, T):
        user = uuid.uuid4()
        cls.admin.execute('INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at) VALUES(%s,%s,%s,%s)', (channel_id, user, folder, T))
        cls.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch) VALUES(%s,%s,'accepted',false,1)",
                          (channel_id, user))
        for kind, prefix in PREFIX.items():
            scan, playlist = uuid.uuid4(), prefix + channel_id[2:]
            cls.admin.execute('''INSERT INTO public.channel_video_inventory_scans
                (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at)
                VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s)''',
                (scan, channel_id, kind, playlist, T - 2 * H, T - 2 * H, T - H, T - H))
            cls.admin.execute('''INSERT INTO public.channel_playlist_state
                (channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at)
                VALUES(%s,%s,%s,'completed',%s,%s,%s)''', (channel_id, kind, playlist, scan, T - H, T - H))

    @classmethod
    def seed_videos(cls, channel_id, videos):
        T = cls.T
        with cls.admin.cursor() as cursor:
            cursor.executemany('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)',
                               [(v['id'], channel_id, v['kind']) for v in videos])
            cursor.executemany('''INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'legacy_inventory_backfill')''',
                [(v['id'], channel_id, v['kind'], PREFIX[v['kind']] + channel_id[2:], v['published'],
                  v['membership'] or T - H, v['membership'] or T - H, v['membership'] or T - H) for v in videos])
            cursor.executemany('''INSERT INTO public.videos
                (video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,
                 availability_status,availability_checked_at,stream_actual_end_at,publication_status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'PT10M',%s,%s,%s,NULL)''',
                [(v['id'], channel_id, 'Growth ' + v['id'], v['views'], v['checked'], v['metadata'] or max(v['checked'], T - 3 * H),
                  v['published'], v['availability'], T - H, v['end']) for v in videos])
            # Half of the history carries the stamp, half relies on the view's date rule: one epoch.
            cursor.executemany('INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) VALUES(%s,%s,%s,%s)',
                               [(v['id'], at, value, v['epochs'].get(at, EPOCH if n % 2 else None))
                                for v in videos for n, (at, value) in enumerate(v['points'])])

    @classmethod
    def seed_large(cls, folder, T):
        """A 20,000-video Long channel with the test cadence over five years, set-based."""
        cls.channel(LARGE, folder, T)
        ids = "'gZ'||lpad(i::text,9,'0')"
        cls.admin.execute(f"""CREATE TEMP TABLE growth_large AS SELECT i,{ids} AS video_id,
            %(T)s::timestamptz-make_interval(secs=>i*7884.0+600) AS published_at FROM generate_series(1,%(n)s) i""", dict(T=T, n=LARGE_COUNT))
        cls.admin.execute("""INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch)
            SELECT g.video_id,at,(100+(g.i*7919)%%5000*sqrt(extract(epoch FROM at-g.published_at)/3600))::bigint,
            CASE WHEN k%%2=0 THEN %(epoch)s END
            FROM growth_large g CROSS JOIN (VALUES(0,86400,3600),(86400,259200,7200),(259200,604800,21600),
            (604800,1209600,43200),(1209600,31536000,86400),(31536000,1000000000,604800)) b(lo,hi,step)
            CROSS JOIN LATERAL generate_series(ceil(greatest(b.lo,extract(epoch FROM %(T)s::timestamptz-interval '720 hours'-g.published_at))/b.step)::bigint,
            least(ceil(b.hi::numeric/b.step)-1,floor(extract(epoch FROM %(T)s::timestamptz-interval '20 minutes'-g.published_at)/b.step))::bigint) k
            CROSS JOIN LATERAL (SELECT g.published_at+make_interval(secs=>k*b.step) AS at) p
            WHERE at NOT BETWEEN %(T)s::timestamptz-interval '698 hours' AND %(T)s::timestamptz-interval '694 hours'""",
            dict(T=T, epoch=EPOCH))
        cls.admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) SELECT video_id,%s,'long' FROM growth_large", (LARGE,))
        cls.admin.execute("""INSERT INTO public.video_inventory_memberships
            (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source)
            SELECT video_id,%(c)s,'long',%(p)s,published_at,%(seen)s,%(seen)s,%(seen)s,'legacy_inventory_backfill' FROM growth_large""",
            dict(c=LARGE, p='UULF' + LARGE[2:], seen=T - H))
        # Metadata every 28 days (2 % refreshed late), 1 % private: realistic synthetic proportions.
        cls.admin.execute("""INSERT INTO public.videos(video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,duration,
            availability_status,availability_checked_at,stream_actual_end_at,publication_status)
            SELECT g.video_id,%(c)s,'Large '||g.i,last.views,last.recorded_at,
            CASE WHEN g.i%%50=0 THEN %(T)s::timestamptz-interval '680 hours' ELSE greatest(g.published_at,
            %(T)s::timestamptz-make_interval(secs=>(g.i*3607)%%2332800+1800)) END,g.published_at,'PT10M',
            CASE WHEN g.i%%101=0 THEN 'private' ELSE 'public' END,%(T)s::timestamptz-interval '1 hour',NULL,NULL
            FROM growth_large g CROSS JOIN LATERAL (SELECT s.recorded_at,s.views FROM public.video_snapshots s
            WHERE s.video_id=g.video_id ORDER BY s.recorded_at DESC LIMIT 1) last""", dict(c=LARGE, T=T))
        cls.large_snapshots = cls.admin.execute("SELECT count(*) FROM public.video_snapshots WHERE video_id LIKE 'gZ%%'").fetchone()[0]


    def query(self, channel_id, kind, *, where=None, order=None, zone='UTC', select=None, size=200, scope=None,
              consistency='current'):
        return {'query_version': 1, 'source': {'id': 'public.videos.current.v1'},
                'scope': scope or {'kind': 'channel', 'id': channel_id}, 'video_types': [kind],
                'select': select or ['video.id', 'video.published_at', *growth.FIELDS],
                **({'where': where} if where else {}),
                'order_by': order or [{'field': 'video.published_at', 'direction': 'desc'}],
                'time': {'mode': 'current', 'timezone': zone}, 'consistency': consistency, 'page': {'size': size}}

    def shared(self, channel_id, kind, **options):
        """Every page of the shared query, in order."""
        request, rows, pages = self.query(channel_id, kind, **options), [], []
        while True:
            page = self.service.execute(canonical(request))
            pages.append(page)
            rows.extend(page['rows'])
            if not page['page']['has_more']:
                return rows, pages
            request['page']['after'] = page['page']['next_cursor']

    @contextmanager
    def same_utc_day(self):
        before = self.clock().date()
        yield
        if self.clock().date() != before:
            self.skipTest('crossed UTC midnight during a started_today comparison')

    # --- differential checks -----------------------------------------------------------------

    def test_observed_growth_and_frozen_values_for_every_format(self):
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind), self.same_utc_day():
                rows, pages = self.shared(CHANNEL, kind)
                actual = {r['id']: shared_view(r['cells']) for r in rows}
                # The Library's path: one frozen result gives the same rows and values.
                frozen_rows, frozen_pages = self.shared(CHANNEL, kind, consistency='frozen')
                self.assertEqual([r['id'] for r in frozen_rows], [r['id'] for r in rows])
                self.assertEqual({r['id']: shared_view(r['cells']) for r in frozen_rows}, actual)
                context = frozen_pages[0]['context']
                self.assertEqual(context['consistency'], 'frozen')
                # Growth never shortens a frozen result: every point outlives fresh_until.
                self.assertEqual(datetime.fromisoformat(context['valid_before']) - datetime.fromisoformat(context['evaluated_at']),
                                 timedelta(seconds=600))
                statuses = {value['status'] for value in actual.values()}
                self.assertTrue({'ready', 'partial', 'started_today', 'waiting_next_snapshot', 'unavailable'} <= statuses, (kind, statuses))
                for page in pages:
                    self.assertIsNone(page['context']['observation_age_limit_seconds'])  # Growth never narrows the rows.
        cells = {r['id']: r['cells'] for r in self.shared(CHANNEL, 'long')[0]}
        expect = {'gLreadyexa0': ('ready', '2800', '100', '2419200', None, '0'),
                  'gLdecrease0': ('ready', '-100', '-3.57', '2419200', None, '0'),
                  'gLearly0000': ('ready', '4000', '138.73', '2491200', None, '72000'),
                  'gLlate00000': ('ready', '2000', '71.64', '2412000', None, '-7200'),
                  'gLedge3h000': ('ready', '200', '7.17', '2408400', None, '-10800'),
                  'gLedge3h1s0': ('partial', '400', '14.35', '2408399', '27', None),
                  'gLtie100000': ('partial', '1', '0.12', '691200', '8', None),
                  'gLtie300000': ('partial', '3', '0.38', '691200', '8', None),
                  'gLtieneg000': ('partial', '-1', '-0.12', '691200', '8', None),
                  'gLzero00000': ('ready', '0', '0', '2419200', None, '0'),
                  'gLhuge00000': ('ready', '9223372036854775000', '329406144173384821.43', '2419200', None, '0'),
                  'gLexpired00': ('partial', '30', '1.2', '2160000', '25', None),
                  # Metadata 680 h old (a late 672 h refresh) does not hide growth.
                  'gLstalemeta': ('ready', '10', '0.36', '2419200', None, '0'),
                  'gLlatemeta0': ('ready', '1120', '40', '2419200', None, '0')}
        for identity, (status, gained, per_day, interval, tracked, boundary) in expect.items():
            with self.subTest(video=identity):
                c = cells[identity]
                self.assertEqual((c[growth.STATUS]['value'], c[growth.GAINED]['value'], c[growth.PER_DAY]['value'],
                                  c[growth.INTERVAL]['value'], c[growth.TRACKED_DAYS]['value'],
                                  c[growth.STATUS]['observation']['boundary_distance_seconds']),
                                 (status, gained, per_day, interval, tracked, boundary))
        reasons = {identity: cells[identity][growth.STATUS]['observation']['reason'] for identity in cells}
        self.assertEqual({k: reasons[k] for k in ('gLstalecnt0', 'gLoldproof0', 'gLnoviews00', 'gLnosnap000')},
                         {'gLstalecnt0': 'current_data_stale', 'gLoldproof0': 'video_not_in_scope',
                          'gLnoviews00': 'current_counter_unavailable', 'gLnosnap000': 'current_snapshot_missing'})
        self.assertNotIn('gLprivate00', cells)

    def test_channel_page_growth_matches_the_complete_query(self):
        with self.same_utc_day():
            expected, _ = self.shared(CHANNEL, 'long')
            request = self.query(CHANNEL, 'long', size=7)
            request['page']['growth_lifetime'] = 'page'
            actual = []
            while True:
                page = self.service.execute(canonical(request))
                through = datetime.fromisoformat(page['context']['valid_before'])
                for row in page['rows']:
                    for cell in row['cells'].values():
                        if cell['state'] == 'known' and cell['expires_at']:
                            self.assertGreaterEqual(datetime.fromisoformat(cell['expires_at']), through)
                actual.extend(page['rows'])
                if not page['page']['has_more']:
                    break
                request['page']['after'] = page['page']['next_cursor']
            self.assertGreater(len(actual), 7)
            self.assertEqual([(r['id'], shared_view(r['cells'])) for r in actual],
                             [(r['id'], shared_view(r['cells'])) for r in expected])

    def test_growth_ranked_pages_match_frozen_values_for_filters_sort_and_missing_values(self):
        for kind in ("long", "short", "stream"):
            for field in growth.FIELDS:
                options = {"order": [{"field": field, "direction": "desc"}], "size": 7}
                if field == growth.STATUS:
                    options["order"] = [{"field": "video.id", "direction": "asc"}]
                    options["where"] = {"field": growth.STATUS, "op": "eq", "value": "partial"}
                elif field == growth.GAINED:
                    options["where"] = {"field": growth.GAINED, "op": "gte", "value": "0"}
                with self.subTest(kind=kind, field=field), self.same_utc_day():
                    expected, _ = self.shared(CHANNEL, kind, consistency="frozen", **options)
                    request = self.query(CHANNEL, kind, **options)
                    request["page"]["growth_lifetime"] = "page"
                    actual = self.service.execute(canonical(request))["rows"]
                    comparison = lambda rows: [(r["id"], shared_view(r["cells"])) for r in rows]
                    self.assertEqual(comparison(actual), comparison(expected[:7]))

    def test_channel_list_is_the_complete_growth_order(self):
        """Library's ordered list for 28-day growth sorts: the order of every page of the shared query,
        unknown growth last; its slices read exactly their videos in list order."""
        for kind in ('long', 'short', 'stream'):
            for field in (growth.GAINED, growth.PER_DAY):
                for direction in ('desc', 'asc'):
                    with self.subTest(kind=kind, field=field, direction=direction), self.same_utc_day():
                        order = [{'field': field, 'direction': direction}]
                        rows, _ = self.shared(CHANNEL, kind, order=order)
                        request = self.query(CHANNEL, kind, order=order, size=7)
                        request['page'] = {'size': 7, 'growth_lifetime': 'page', 'order': 'ids'}
                        reply = self.service.execute(canonical(request))
                        joined = reply['page']['order']['ids']
                        listed = [joined[at:at + 11] for at in range(0, len(joined), 11)]
                        self.assertEqual(listed, [row['id'] for row in rows])
                        self.assertEqual(reply['page']['order']['total_count'], len(rows))
                        self.assertEqual([(r['id'], shared_view(r['cells'])) for r in reply['rows']],
                                         [(r['id'], shared_view(r['cells'])) for r in rows[:7]])
                        request['page'] = {'size': 7, 'growth_lifetime': 'page', 'ids': listed[7:14]}
                        self.assertEqual([r['id'] for r in self.service.execute(canonical(request))['rows']], listed[7:14])

    def test_current_channel_anchor_restarts_inside_the_filtered_order(self):
        for ordering in ([{'field': 'video.published_at', 'direction': 'asc'}],
                         [{'field': growth.GAINED, 'direction': 'desc'}]):
            with self.subTest(ordering=ordering), self.same_utc_day():
                rows, _ = self.shared(CHANNEL, 'long', order=ordering)
                ids = [row['id'] for row in rows]
                query = self.query(CHANNEL, 'long', size=2, order=ordering)
                def anchored(anchor):
                    query['page'] = {'size': 2, 'growth_lifetime': 'page', 'anchor_id': anchor}
                    return self.service.execute(canonical(query))
                # Include a middle row and a near-end row with a missing sort value.
                for i in (5, len(ids) - 3):
                    reply = anchored(ids[i])
                    self.assertEqual([r['id'] for r in reply['rows']], ids[i:i + 2])
                    query['page'] = {'size': 2, 'growth_lifetime': 'page', 'after': reply['page']['next_cursor']}
                    next_page = self.service.execute(canonical(query))
                    self.assertEqual([r['id'] for r in next_page['rows']], ids[i + 2:i + 4])
                for missing in ('Missing0000', 'gLprivate00'):
                    self.assertEqual([r['id'] for r in anchored(missing)['rows']], ids[:2])
                query['where'] = {'field': 'video.id', 'op': 'eq', 'value': ids[-1]}
                self.assertEqual([r['id'] for r in anchored(ids[1])['rows']], ids[-1:])

    def test_numeric_growth_order_across_pages(self):
        for kind in ('long', 'short', 'stream'):
            for direction in ('desc', 'asc'):
                with self.subTest(kind=kind, direction=direction), self.same_utc_day():
                    # Two rows per page: the keyset continuation crosses ties and the unknown tail.
                    rows, pages = self.shared(CHANNEL, kind, order=[{'field': growth.GAINED, 'direction': direction}], size=2)
                    self.assertGreater(len(pages), 5)
                    values = [int(r['cells'][growth.GAINED]['value']) for r in rows if r['cells'][growth.GAINED]['state'] == 'known']
                    self.assertEqual(values, sorted(values, reverse=direction == 'desc'))
                    self.assertEqual(len({r['id'] for r in rows}), len(rows))

    def test_growth_conditions_select_rows_by_interval_and_gain(self):
        cases = ({'min_views_gained': '100'}, {'max_views_gained': '0'}, {'min_views_gained': '-50', 'max_views_gained': '3000'},
                 {'min_tracked_days': '0'}, {'min_tracked_days': '9'}, {'min_tracked_days': '27'}, {'min_tracked_days': '28'},
                 {'min_tracked_days': '1000000000000000'}, {'full_history': True},
                 {'min_views_gained': '1', 'min_tracked_days': '5', 'full_history': True})
        for kind in ('long', 'short', 'stream'):
            for case in cases:
                for sort in ('published_at', 'views_gained'):
                    with self.subTest(kind=kind, case=case, sort=sort), self.same_utc_day():
                        order = [{'field': growth.LEGACY_SORT_FIELDS.get(sort, 'video.published_at'), 'direction': 'desc'}]
                        all_rows, _ = self.shared(CHANNEL, kind, order=order)
                        expected = []
                        for row in all_rows:
                            view = shared_view(row['cells'])
                            gained, elapsed = view['delta_views'], view['actual_interval_seconds']
                            if (gained is None or 'min_views_gained' in case and (gained is None or int(gained)<int(case['min_views_gained']))
                                or 'max_views_gained' in case and (gained is None or int(gained)>int(case['max_views_gained']))
                                or 'min_tracked_days' in case and (elapsed is None or elapsed<86400*int(case['min_tracked_days']))
                                or case.get('full_history') and view['status'] != 'ready'):
                                continue
                            expected.append(row['id'])
                        rows, _ = self.shared(CHANNEL, kind, where=growth.legacy_conditions(case), order=order)
                        self.assertEqual([row['id'] for row in rows], expected)

    def test_summary_counts_each_growth_status(self):
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind), self.same_utc_day():
                answer = self.service.execute_operation(canonical(dict(version=1, operation=growth.SUMMARY_ID,
                    query=self.query(CHANNEL, kind, select=['video.id']), parameters={})))
                result = answer['result']
                rows, _ = self.shared(CHANNEL,kind)
                self.assertEqual(result['population'], len(rows))
                for status in growth.STATUSES:
                    self.assertEqual(result[status + '_count'], sum(r['cells'][growth.STATUS]['value'] == status for r in rows))
                self.assertEqual(result['window']['requested_days'], 28)
                self.assertEqual(result['methods']['average_views_per_day'], 'signed_delta_per_actual_second_times_86400_rounded_2dp_half_even')
                self.assertEqual(sum(result['reasons'].values()), result['population'] - result['ready_count'])
                self.assertIsNone(answer['context']['observation_age_limit_seconds'])
                frozen_ready = self.service.execute_operation(canonical(dict(version=1, operation=growth.SUMMARY_ID,
                    query=self.query(CHANNEL, kind, select=['video.id']), parameters={'population': {'kind': 'query'}})))
                self.assertEqual(frozen_ready['result'], result)
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            self.service.execute_operation(canonical(dict(version=1, operation=growth.SUMMARY_ID,
                query=self.query(CHANNEL, 'long', select=['video.id']), parameters={'population': {'kind': 'frozen'}})))

    def test_05_inspector_reads_one_video_with_the_same_evidence(self):
        rows = {r['id']: r for r in self.shared(CHANNEL, 'long')[0]}
        for identity in ('gLreadyexa0', 'gLedge3h1s0', 'gLwaittwo00', 'gLnosnap000'):
            with self.subTest(video=identity):
                one, _ = self.shared(CHANNEL, 'long', where={'field': 'video.id', 'op': 'eq', 'value': identity})
                self.assertEqual(len(one), 1)
                for field in growth.FIELDS:
                    self.assertEqual(one[0]['cells'][field], rows[identity]['cells'][field])
        # One evidence object per row: on the status cell, the other growth cells carry none.
        cells = rows['gLreadyexa0']['cells']
        evidence = cells[growth.STATUS]['observation']
        self.assertEqual((evidence['from_views'], evidence['to_views'], evidence['definition_epoch']), ('7200', '10000', EPOCH))
        self.assertEqual([cells[f]['observation'] for f in growth.FIELDS[1:]], [None] * 4)
        # Without the status, the first selected growth field carries it (the Inspector's points).
        one, _ = self.shared(CHANNEL, 'long', where={'field': 'video.id', 'op': 'eq', 'value': 'gLreadyexa0'},
                             select=['video.id', growth.PER_DAY, growth.GAINED])
        self.assertEqual(one[0]['cells'][growth.GAINED]['observation'], evidence)
        self.assertIsNone(one[0]['cells'][growth.PER_DAY]['observation'])

    def test_06_started_today_uses_the_viewer_time_zone(self):
        zones = ('UTC', 'Pacific/Kiritimati', 'Pacific/Pago_Pago', 'Asia/Kathmandu', 'America/St_Johns', 'Europe/Berlin')
        tracking = ('gLtoday0000', 'gLwaitone00', 'gLwaittwo00', 'gStoday0000', 'gTtoday0000')
        flips = 0
        for zone in zones:
            with self.subTest(zone=zone):
                rows, pages = self.shared(CHANNEL, 'long', zone=zone)
                rows += self.shared(CHANNEL, 'short', zone=zone)[0] + self.shared(CHANNEL, 'stream', zone=zone)[0]
                now = datetime.fromisoformat(pages[0]['context']['evaluated_at'].replace('Z', '+00:00'))
                local = ZoneInfo(zone)
                for row in rows:
                    if row['id'] not in tracking:
                        continue
                    evidence = row['cells'][growth.STATUS]['observation']
                    start = datetime.fromisoformat(evidence['from_at'].replace('Z', '+00:00'))
                    expected = 'started_today' if start.astimezone(local).date() == now.astimezone(local).date() else 'waiting_next_snapshot'
                    self.assertEqual((row['id'], row['cells'][growth.STATUS]['value'], evidence['time_zone']), (row['id'], expected, zone))
                    flips += expected != {'gLtoday0000': 'started_today', 'gStoday0000': 'started_today', 'gTtoday0000': 'started_today'}.get(row['id'], 'waiting_next_snapshot')
        self.assertGreater(flips, 0)  # At least one zone puts a UTC "today" on another day.
        for zone in ('Mars/Olympus', 'Europe/../etc', 'utc', 'X' * 65):
            with self.subTest(zone=zone), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                self.service.execute(canonical(self.query(CHANNEL, 'long', zone=zone)))

    def test_07_a_second_views_definition_epoch_is_explicit(self):
        start, end = self.T - 2 * H - WINDOW, self.T - 2 * H
        try:
            self.admin.execute("UPDATE public.video_snapshots SET views_definition_epoch='fixture.views.epoch2' WHERE video_id='gLdecrease0' AND recorded_at=%s", (start,))
            self.admin.execute("UPDATE public.video_snapshots SET views_definition_epoch='fixture.views.epoch2' WHERE video_id IN('gLzero00000','gSdecrease0')")
            rows, pages = self.shared(CHANNEL, 'long')
            cells = {r['id']: r['cells'] for r in rows}
            evidence = cells['gLdecrease0'][growth.STATUS]['observation']
            self.assertEqual((cells['gLdecrease0'][growth.STATUS]['value'], evidence['reason'], evidence['from_definition_epoch'], evidence['to_definition_epoch']),
                             ('unavailable', 'incompatible_definition', 'fixture.views.epoch2', EPOCH))
            for field in (growth.GAINED, growth.PER_DAY, growth.INTERVAL):
                self.assertEqual((cells['gLdecrease0'][field]['state'], cells['gLdecrease0'][field]['value']), ('unavailable', None))
            # One epoch for both points, whichever it is, compares.
            self.assertEqual((cells['gLzero00000'][growth.STATUS]['value'], cells['gLzero00000'][growth.STATUS]['observation']['definition_epoch']),
                             ('ready', 'fixture.views.epoch2'))
            short = {r['id']: r['cells'] for r in self.shared(CHANNEL, 'short')[0]}
            self.assertEqual(short['gSdecrease0'][growth.STATUS]['value'], 'ready')
            # Excluded from growth conditions and sorted with the unknown tail.
            rows, _ = self.shared(CHANNEL, 'long', where=growth.legacy_conditions({'min_views_gained': '-1000'}))
            self.assertNotIn('gLdecrease0', [r['id'] for r in rows])
            summary = self.service.execute_operation(canonical(dict(version=1, operation=growth.SUMMARY_ID,
                query=self.query(CHANNEL, 'long', select=['video.id']), parameters={})))['result']
            self.assertEqual(summary['reasons'].get('incompatible_definition'), 1)
        finally:
            self.admin.execute("UPDATE public.video_snapshots SET views_definition_epoch=%s WHERE views_definition_epoch='fixture.views.epoch2'", (EPOCH,))

    def test_08_points_stay_readable_through_the_result_lifetime(self):
        identity, now = 'gLhorizon00', self.clock()
        E = now - 23 * H
        edge = now - timedelta(hours=696) + timedelta(seconds=300)  # Leaves retention in 5 minutes.
        points = [(edge, 100), (E - WINDOW + 10 * H, 200), (E, 300)]
        self.seed_videos(CHANNEL, [video(identity, 'long', now - 90 * D, points)])
        try:
            def one():
                rows, pages = self.shared(CHANNEL, 'long', where={'field': 'video.id', 'op': 'eq', 'value': identity})
                return rows[0]['cells'], pages[0]['context']
            current, context = one()
            self.assertEqual((current[growth.STATUS]['value'], current[growth.GAINED]['value']), ('ready', '200'))
            # That point outlives the whole current result; it never shortens a page.
            evaluated = datetime.fromisoformat(context['evaluated_at'].replace('Z', '+00:00'))
            self.assertGreaterEqual(datetime.fromisoformat(current[growth.GAINED]['expires_at'].replace('Z', '+00:00')),
                                    evaluated + timedelta(seconds=growth.CURRENT_RESULT_SECONDS))
            frozen, _ = self.shared(CHANNEL, 'long', where={'field': 'video.id', 'op': 'eq', 'value': identity}, consistency='frozen')
            self.assertEqual((frozen[0]['cells'][growth.STATUS]['value'], frozen[0]['cells'][growth.GAINED]['value']), ('partial', '100'))
        finally:
            for table in ('video_snapshots', 'videos', 'video_inventory_memberships', 'video_inventory'):
                self.admin.execute(f'DELETE FROM public.{table} WHERE video_id=%s', (identity,))

    def test_09_streams_compare_only_after_their_actual_end(self):
        cells = {r['id']: r['cells'] for r in self.shared(CHANNEL, 'stream')[0]}
        after = cells['gTafterend0'][growth.STATUS]['observation']
        self.assertEqual(cells['gTafterend0'][growth.STATUS]['value'], 'partial')
        self.assertEqual(after['from_at'], (self.T - 10 * D + timedelta(minutes=10)).isoformat().replace('+00:00', 'Z'))
        near = cells['gTendnearw0']
        self.assertEqual((near[growth.STATUS]['value'], near[growth.STATUS]['observation']['boundary_distance_seconds']), ('ready', '-4200'))
        self.assertNotIn('gTlive00000', cells)
        self.assertNotIn('gTcntbefore', cells)

    def test_10_large_channel_sort_by_gained_within_budget(self):
        """20,000 Long videos, realistic cadence: whole-population order before paging, 8 s budget."""
        timings = {}
        def timed(name, call):
            started = time.perf_counter()
            value = call()
            timings[name] = round(time.perf_counter() - started, 3)
            return value
        request = lambda **options: canonical(self.query(LARGE, 'long', **options))
        first = timed('sort_gained_desc_page50', lambda: self.service.execute(request(order=[{'field': growth.GAINED, 'direction': 'desc'}], size=50)))
        timed('sort_gained_desc_page50_warm', lambda: self.service.execute(request(order=[{'field': growth.GAINED, 'direction': 'desc'}], size=50)))
        desc = timed('sort_gained_desc_page200', lambda: self.service.execute(request(order=[{'field': growth.GAINED, 'direction': 'desc'}], size=200)))
        asc = timed('sort_gained_asc_page200', lambda: self.service.execute(request(order=[{'field': growth.GAINED, 'direction': 'asc'}], size=200)))
        following = timed('sort_gained_desc_next_page', lambda: self.service.execute(canonical({**self.query(LARGE, 'long', order=[{'field': growth.GAINED, 'direction': 'desc'}], size=200),
            'page': {'size': 200, 'after': desc['page']['next_cursor']}}))) if desc['page']['has_more'] else {'rows': []}
        filtered = timed('filter_full_history_min_gained', lambda: self.service.execute(request(
            where=growth.legacy_conditions({'min_views_gained': '1000', 'full_history': True}), order=[{'field': growth.GAINED, 'direction': 'desc'}], size=50)))
        summary = timed('population_summary', lambda: self.service.execute_operation(canonical(dict(version=1, operation=growth.SUMMARY_ID,
            query=self.query(LARGE, 'long', select=['video.id']), parameters={}))))
        plain = timed('published_order_without_growth', lambda: self.service.execute(request(select=['video.id', 'video.published_at'], size=50)))
        # The Library's own path: one frozen result of the whole population, cards' columns.
        library = ['video.id', 'video.title', 'video.thumbnail_url', 'video.published_at', 'video.duration_seconds',
                   'video.views', 'video.likes', 'video.comments', *growth.FIELDS]
        frozen_request = self.query(LARGE, 'long', select=library, order=[{'field': growth.GAINED, 'direction': 'desc'}], size=50, consistency='frozen')
        frozen = timed('frozen_build_library_columns_sort_gained', lambda: self.service.execute(canonical(frozen_request)))
        reference = self.service.frozen_reference(frozen['context']['frozen_ref'])
        frozen_ids = timed('frozen_ids_all', lambda: self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': frozen['context']['frozen_ref']})))['ids']
        frozen_rows, request_page = list(frozen['rows']), dict(frozen_request)
        started = time.perf_counter()
        page = frozen
        while page['page']['has_more']:
            request_page['page'] = {'size': 50, 'after': page['page']['next_cursor']}
            page = self.service.execute(canonical(request_page))
            frozen_rows.extend(page['rows'])
        all_pages = round(time.perf_counter() - started, 3)  # Hundreds of requests: not one budget.
        self.assertEqual(datetime.fromisoformat(frozen['context']['valid_before']) - datetime.fromisoformat(frozen['context']['evaluated_at']),
                         timedelta(seconds=600))
        for name, seconds in timings.items():
            self.assertLess(seconds, 8.0, name)
        self.assertEqual(len(first['rows']), 50)
        self.assertTrue(plain['rows'])
        population = summary['result']['population']
        self.assertGreater(population, 0.95 * LARGE_COUNT)
        self.assertLess(len(canonical(summary)), 512 * 1024)
        self.assertLess(len(canonical(desc)), 2 * 1024 * 1024)
        self.assertEqual(frozen_ids, [row['id'] for row in frozen_rows])
        self.assertEqual(frozen_ids[:len(desc['rows'] + following['rows'])], [row['id'] for row in desc['rows'] + following['rows']])
        self.assertEqual(len(set(frozen_ids)), population)
        for row in filtered['rows']:
            self.assertEqual(row['cells'][growth.STATUS]['value'], 'ready')
            self.assertGreaterEqual(int(row['cells'][growth.GAINED]['value']), 1000)
        print(json.dumps({'large_channel': {'videos': LARGE_COUNT, 'snapshots': self.large_snapshots, 'admitted': population,
                          'statuses': {s: summary['result'][s + '_count'] for s in growth.STATUSES}, 'reasons': summary['result']['reasons'],
                          'seed_seconds': round(self.large_seed_seconds, 3), 'refresh_seconds': round(self.large_refresh_seconds, 3),
                          'response_bytes_page200': len(canonical(desc)), 'summary_bytes': len(canonical(summary)),
                          'frozen_session_bytes': reference.session.size, 'frozen_rows': len(frozen_rows),
                          'frozen_pages_seconds_total': all_pages, 'frozen_pages': math.ceil(len(frozen_rows) / 50), 'seconds': timings}}), flush=True)

    def test_11_late_metadata_refresh_keeps_growth_available(self):
        late = {'long': ('gLstalemeta', 'gLlatemeta0'), 'short': ('gSlatemeta0',), 'stream': ('gTlatemeta0',)}
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind), self.same_utc_day():
                for consistency in ('current', 'frozen'):
                    cells = {r['id']: r['cells'] for r in self.shared(CHANNEL, kind, consistency=consistency)[0]}
                    for identity in late[kind]:
                        self.assertEqual(cells[identity][growth.STATUS]['value'], 'ready', (consistency, identity))
                    # The counter and the inventory proof keep the 28-day floor.
                    stale = {'long': {'gLstalecnt0', 'gLoldproof0'}, 'short': {'gSstalecnt0'}, 'stream': {'gTstalecnt0'}}[kind]
                    self.assertEqual({identity: cells[identity][growth.STATUS]['value'] for identity in stale},
                                     dict.fromkeys(stale, 'unavailable'))
        # The 696 h edge is the shared source's own metadata rule: frozen results end 600 s later.
        identity = 'gLlatemeta0'
        previous = self.admin.execute('SELECT metadata_checked_at FROM public.videos WHERE video_id=%s', (identity,)).fetchone()[0]
        one = lambda consistency: self.shared(CHANNEL, 'long', where={'field': 'video.id', 'op': 'eq', 'value': identity},
                                              select=['video.id', 'video.title', growth.STATUS], consistency=consistency)[0][0]['cells']
        try:
            self.admin.execute("UPDATE public.videos SET metadata_checked_at=statement_timestamp()-interval '696 hours'+interval '300 seconds' WHERE video_id=%s", (identity,))
            current, frozen = one('current'), one('frozen')
            self.assertEqual((current[growth.STATUS]['value'], current['video.title']['state']), ('ready', 'known'))
            self.assertEqual((frozen[growth.STATUS]['value'], frozen[growth.STATUS]['observation']['reason'], frozen['video.title']['state']),
                             ('unavailable', 'current_data_stale', 'expired'))
            self.admin.execute("UPDATE public.videos SET metadata_checked_at=statement_timestamp()-interval '696 hours'-interval '60 seconds' WHERE video_id=%s", (identity,))
            current = one('current')
            self.assertEqual((current[growth.STATUS]['value'], current[growth.STATUS]['observation']['reason'], current['video.title']['state']),
                             ('unavailable', 'current_data_stale', 'expired'))
        finally:
            self.admin.execute('UPDATE public.videos SET metadata_checked_at=%s WHERE video_id=%s', (previous, identity))
        after = self.service.execute_operation(canonical(dict(version=1, operation=growth.SUMMARY_ID,
            query=self.query(LARGE, 'long', select=['video.id']), parameters={})))['result']
        late_rows = self.admin.execute("""SELECT count(*) FROM public.videos WHERE video_id LIKE 'gZ%%' AND availability_status='public'
            AND metadata_checked_at<statement_timestamp()-interval '672 hours'""").fetchone()[0]
        self.assertGreater(late_rows, 0)
        self.assertNotIn('current_data_stale', after['reasons'])

    def test_12_the_whole_run_kept_the_seed_date(self):
        """Runs last: the UTC date the statuses depend on did not change after the seed clock (wait_before_seeding)."""
        self.assertEqual(self.clock().date(), self.T.date(), 'the run crossed midnight after seeding: raise MIDNIGHT_MARGIN')


if __name__ == '__main__':
    unittest.main(verbosity=2)
