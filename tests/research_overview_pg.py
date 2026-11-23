"""Channel overview and profile operations on PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
import os
from pathlib import Path
import random
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
import psycopg
from research_query_pg import bootstrap
from research_query.validation import AccessContext, QueryRejected, canonical
from research_query.service import QueryService
from research_query.operations import OPERATIONS
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from fractions import Fraction
from statistics import median

FOLDER = '99999999-9999-4999-8999-999999999999'
MAIN = 'UCMainOverviewChannel000'
BIG = 'UCBigTwentyThousandVids0'
EPOCH = 'UCEpochDefinitionChan000'
# Channel-counter scenarios (header 7D/28D and the public video count notice).
CC_FULL = 'UCCounterFullDailyChan00'
CC_FIVE = 'UCCounterFiveDayIntrvl00'
CC_ZERO = 'UCCounterZeroBaseline000'
CC_STALE = 'UCCounterStaleCurrent000'
CC_NONE = 'UCCounterNoSnapshots0000'
CC_DROP = 'UCCounterVideoCountDrop0'
CC_TYPED = 'UCTypedStatesAbsentNot00'
COUNTER_CHANNELS = (CC_FULL, CC_FIVE, CC_ZERO, CC_STALE, CC_NONE, CC_DROP, CC_TYPED)
TOTALS = 'UCCleanHeaderTotals00000'  # Every listed video public and current, plus one stream with a pre-end counter.
# A private Long video, a stream without an availability check, and Long/Short playlist overlaps typed automatically.
EXCLUSIONS = 'UCHeaderTotalsExclusion0'
AGE_STREAM = 'mav00002000' # a stream that ended 50 hours ago, hourly snapshots after its end.
PREFIX = {'long': 'UULF', 'short': 'UUSH', 'stream': 'UULV'}
TOPICS = ('https://en.wikipedia.org/wiki/Music', 'https://en.wikipedia.org/wiki/Video_game_culture',
          'https://en.wikipedia.org/wiki/Lifestyle_(sociology)', 'https://en.wikipedia.org/wiki/Technology',
          'https://en.wikipedia.org/wiki/Entertainment', 'https://en.wikipedia.org/wiki/Caf%C3%A9_culture',
          'https://en.wikipedia.org/wiki/Hip_hop_music', 'https://en.wikipedia.org/wiki/Knowledge',
          'https://en.wikipedia.org/wiki/Sport', 'https://en.wikipedia.org/wiki/Food')
CATEGORIES = ('10', '20', '22', '24', '27', '28')
LANGUAGES = ('en', 'en-US', 'ru', 'de', 'zxx', 'uk')
TAGS = tuple(f'tag {n}' for n in range(40)) + ('Straße', '測試')
TITLES = ('Weekly review #{n}', 'Straße 測試 {n}', 'Обзор недели №{n}',
          'A rather long video title that keeps going past sixty characters number {n}', 'Short {n}', '\U0001F600 {n}')
EPOCH_SECOND = 'fixture.synthetic.v2'


def video_id(channel, kind, index):
    return channel[2:4].lower() + {'long': 'l', 'short': 's', 'stream': 'v'}[kind] + f'{index:08d}'


def hours(value):
    return timedelta(hours=value)


class Catalogue:
    """Deterministic realistic rows; the generator never reads the database."""

    def __init__(self, now):
        self.now = now
        self.channels, self.videos, self.inventory, self.snapshots, self.channel_snapshots = [], [], [], [], []

    def channel(self, identity, *, title, subscribers, views, video_count, country='UA', profile_age=2,
                status='ok', inventory=None):
        self.channels.append(dict(channel_id=identity, title=title, subscribers=subscribers, views=views,
                                  video_count=video_count, country_code=country,
                                  country_collected_at=self.now - hours(profile_age),
                                  channel_created_at=self.now - timedelta(days=2900),
                                  channel_metadata_checked_at=self.now - hours(profile_age),
                                  channel_metadata_status=status,
                                  avatar_url='https://yt3.ggpht.example/' + identity,
                                  inventory=inventory or {kind: 'completed' for kind in PREFIX}))

    def published(self, rng, distribution):
        if distribution == 'uniform15y':
            return self.now - timedelta(seconds=rng.randint(3600, 15*365*86400))
        bucket = rng.random()
        if bucket < .12:
            low, high = 2, 7*24
        elif bucket < .27:
            low, high = 7*24, 28*24
        elif bucket < .42:
            low, high = 28*24, 90*24
        elif bucket < .62:
            low, high = 90*24, 365*24
        else:
            low, high = 365*24, 6*365*24
        return self.now - timedelta(seconds=rng.randint(low*3600, high*3600))

    def video(self, channel, kind, index, rng, *, distribution='mixed', **override):
        identity = video_id(channel, kind, index)
        published = self.published(rng, distribution)
        seconds = {'long': rng.randint(240, 5400), 'short': rng.randint(3, 180), 'stream': rng.randint(1200, 20000)}[kind]
        roll = rng.random()
        duration = None if roll < .01 else 'PT1.5S' if roll < .015 else f'PT{seconds}S'
        views = None if rng.random() < .01 else int(10 ** rng.uniform(1, 6.5))
        likes = None if views is None or rng.random() < .03 else int(views * rng.uniform(.005, .06))
        comments = None if views is None or rng.random() < .03 else int(views * rng.uniform(.0005, .01))
        roll = rng.random()
        tags = None if roll < .05 else [] if roll < .15 else rng.sample(TAGS, rng.randint(1, 25))
        roll = rng.random()
        topics = (None if roll < .10 else [] if roll < .20 else ['  '] if roll < .21
                  else rng.sample(TOPICS, rng.randint(1, 3)))
        roll = rng.random()
        category = None if roll < .04 else '' if roll < .06 else ' 24 ' if roll < .07 else rng.choice(CATEGORIES)
        roll = rng.random()
        audio = None if roll < .25 else '' if roll < .28 else rng.choice(LANGUAGES)
        roll = rng.random()
        language = None if roll < .20 else ' ' if roll < .22 else rng.choice(LANGUAGES)
        roll = rng.random()
        caption = None if roll < .15 else roll < .55
        roll = rng.random()
        availability, availability_at = 'public', self.now - hours(rng.randint(1, 200))
        if roll < .03:
            availability = 'private'
        elif roll < .04:
            availability = 'unlisted'
        elif roll < .05:
            availability_at = None
        elif roll < .06:
            availability_at = self.now - hours(700)
        roll = rng.random()
        checked = None if roll < .01 else self.now - hours(700) if roll < .04 else self.now - timedelta(minutes=rng.randint(30, 6000))
        if checked is not None and checked > self.now - hours(600) and checked < published:
            checked = min(self.now - timedelta(minutes=5), published + timedelta(minutes=30))  # Counted after publication.
        metadata = self.now - hours(700) if rng.random() < .03 else self.now - timedelta(minutes=rng.randint(30, 36000))
        title = None if rng.random() < .02 else rng.choice(TITLES).format(n=index)
        row = dict(video_id=identity, channel_id=channel, video_type=kind, title=title, views=views,
                   checked_at=checked, metadata_checked_at=metadata, published_at=published, duration=duration,
                   like_count=likes, comment_count=comments, tags=tags, youtube_topic_categories=topics,
                   category_id=category, default_audio_language=audio, default_language=language,
                   caption_available=caption, availability_status=availability,
                   availability_checked_at=availability_at, stream_actual_end_at=None, publication_status=None,
                   row=True, epoch=None, snapshot=True)
        if kind == 'stream':
            end = published + timedelta(seconds=seconds)
            if end > self.now - hours(1):
                end = self.now - hours(2)
                row['published_at'] = end - timedelta(seconds=seconds)
            roll = rng.random()
            fresh_counter = checked is not None and checked > self.now - hours(600)
            if roll < .06:
                end = None  # Ended, but the end time is not observed yet.
            elif roll < .09 and fresh_counter:
                # The current counter was observed before the stream ended.
                end = min(self.now - timedelta(minutes=20), checked + hours(2))
                row['published_at'] = min(row['published_at'], end - timedelta(seconds=seconds))
            elif fresh_counter and checked < end:
                row['checked_at'] = min(self.now - timedelta(minutes=5), end + timedelta(minutes=30))
            row['stream_actual_end_at'] = end
            if end is not None and row['metadata_checked_at'] < end:
                row['metadata_checked_at'] = min(self.now - timedelta(minutes=10), end + hours(1))
        roll = rng.random()
        if roll < .015:
            row['row'] = False  # Deleted: an inventory identity without a video row.
        elif roll < .025:
            row.update(publication_status='upcoming', published_at=self.now + hours(30))
        row.update(override)
        self.videos.append(row)
        return row

    def snapshots_for(self, row, cap):
        if not row['row'] or not row['snapshot'] or row['checked_at'] is None or row['views'] is None or row['publication_status']:
            return
        start = row['stream_actual_end_at'] if row['video_type'] == 'stream' else row['published_at']
        if start is None:
            return
        floor = max(start, self.now - hours(690))
        at, count, views = row['checked_at'], 0, row['views']
        end_age = max((row['checked_at'] - start).total_seconds(), 1.0)
        while at >= floor and count < cap:
            age = (at - start).total_seconds()
            value = row['views'] if count == 0 else int(row['views'] * (max(age, 0) / end_age) ** .5)
            self.snapshots.append((row['video_id'], value, at, None if count else row['epoch'], count == 0))
            count += 1
            step = 3600 if age < 86400 else 7200 if age < 2*86400 else 6*3600 if age < 7*86400 \
                else 12*3600 if age < 28*86400 else 86400 if age < 90*86400 else 7*86400
            at -= timedelta(seconds=step)

    def daily_channel_snapshots(self, channel, *, days=29, subscribers=100000, views=50000000, video_count=300,
                                offset_minutes=30, growth=1.002):
        for k in range(days):
            at = self.now - timedelta(days=k, minutes=offset_minutes)
            self.channel_snapshots.append((channel, at, int(views / growth ** k), int(subscribers / growth ** k), video_count))


def build_catalogue(now):
    data = Catalogue(now)
    data.channel(MAIN, title='Main overview — Straße', subscribers=1250000, views=987654321, video_count=420)
    data.channel(BIG, title='Twenty thousand uploads', subscribers=9007199254740993, views=19000000000, video_count=20600)
    data.channel(EPOCH, title='Definition epochs', subscribers=10, views=2000, video_count=20)
    data.channel(TOTALS, title='Clean totals', subscribers=10, views=2000, video_count=13)
    for identity in COUNTER_CHANNELS:
        inventory = {'long': 'completed', 'short': 'playlist_not_found', 'stream': 'not_started'} if identity == CC_TYPED else None
        data.channel(identity, title='Counter ' + identity[2:10], subscribers=5000, views=800000, video_count=12,
                     inventory=inventory)
    matched = 'UCLibraryProofCase000000'
    data.channel(matched, title='Shorts only', subscribers=100, views=7500, video_count=75,
                 inventory={'short': 'completed', 'long': 'playlist_not_found', 'stream': 'not_started'})
    sample = random.Random(75)
    for i in range(75):
        data.video(matched, 'short', i, sample, views=100, row=True, snapshot=False,
                   availability_status='public', availability_checked_at=now-hours(2), metadata_checked_at=now-hours(3),
                   published_at=now-timedelta(days=3), checked_at=now-hours(1), publication_status=None)
    rng = random.Random(20261029)
    for kind, count in (('long', 240), ('short', 90), ('stream', 45)):
        for i in range(count):
            data.video(MAIN, kind, i, rng)
    # Period edges (rolling windows are inclusive at both ends) and catalogue ages.
    edge = 1000
    for days in (7, 28, 90, 365, 3*365, 5*365):
        for side in (-1, 1):
            data.video(MAIN, 'long', edge, rng, published_at=now - timedelta(days=days) + side*hours(6),
                       availability_status='public', availability_checked_at=now - hours(3), checked_at=now - hours(2),
                       metadata_checked_at=now - hours(4), row=True, publication_status=None)
            edge += 1
    rng = random.Random(20000)
    for kind, count in (('long', 20000), ('short', 400), ('stream', 120)):
        for i in range(count):
            data.video(BIG, kind, i, rng, distribution='uniform15y')
    clean = dict(availability_status='public', availability_checked_at=now - hours(2), metadata_checked_at=now - hours(3),
                 row=True, publication_status=None, views=1000)
    rng = random.Random(13)
    for i in range(6):
        data.video(TOTALS, 'long', i, rng, checked_at=now - hours(1), published_at=now - timedelta(days=10 + i), **clean)
    for i in range(4):
        data.video(TOTALS, 'short', i, rng, checked_at=now - hours(1), published_at=now - timedelta(days=20 + i), **clean)
    for i in range(3):
        end = now - hours(5 + i)
        data.video(TOTALS, 'stream', i, rng, published_at=end - hours(2), stream_actual_end_at=end,
                   # Stream 2: its counter was observed one hour before the end.
                   checked_at=end - hours(1) if i == 2 else now - hours(1), **clean)
    end = now - hours(50)
    # A 30-hour stream: publication + 24 hours falls before its end.
    data.video(MAIN, 'stream', 2000, rng, published_at=end - hours(30), stream_actual_end_at=end, checked_at=now - timedelta(minutes=30),
               metadata_checked_at=now - hours(1), availability_status='public', availability_checked_at=now - hours(1), row=True,
               publication_status=None, views=5000, snapshot=False)
    for k in range(51):  # Hourly from the end (k=50) to the current counter (k=0).
        at = now - timedelta(minutes=30) - hours(k) if k else now - timedelta(minutes=30)
        if at >= end:
            data.snapshots.append((AGE_STREAM, 5000 - 90 * k, at, None, k == 0))
    rng = random.Random(27)
    for i in range(20):
        data.video(EPOCH, 'long', i, rng, availability_status='public', availability_checked_at=now - hours(2),
                   checked_at=now - hours(1), metadata_checked_at=now - hours(3), views=1000 + i, row=True,
                   publication_status=None, duration='PT600S')
    data.channel(EXCLUSIONS, title='Totals exclusions', subscribers=10, views=2000, video_count=11)
    listed = dict(availability_status='public', availability_checked_at=now - hours(2), checked_at=now - hours(1),
                  metadata_checked_at=now - hours(3), row=True, publication_status=None, snapshot=False)
    rng = random.Random(31)
    for i, views in enumerate((1000, 2000, 3000)):
        data.video(EXCLUSIONS, 'long', i, rng, views=views, duration='PT600S', published_at=now - timedelta(days=10 + i), **listed)
    data.video(EXCLUSIONS, 'long', 3, rng, views=500000, duration='PT600S', published_at=now - timedelta(days=13),
               **dict(listed, availability_status='private'))
    # Listed in both the Long and the Shorts playlist: 15 minutes type it Long automatically.
    data.video(EXCLUSIONS, 'long', 4, rng, views=40000, duration='PT15M', published_at=now - timedelta(days=14),
               also_listed_as=('short',), **listed)
    for i, views in enumerate((100, 200)):
        data.video(EXCLUSIONS, 'short', i, rng, views=views, duration='PT40S', published_at=now - timedelta(days=20 + i), **listed)
    # Listed in both playlists: 59 seconds type it a Short automatically.
    data.video(EXCLUSIONS, 'short', 2, rng, views=7000, duration='PT59S', published_at=now - timedelta(days=22),
               also_listed_as=('long',), **listed)
    for i, views in enumerate((10000, 20000, 30000)):
        end = now - hours(5 + i)
        # Stream 2: its availability was never checked.
        data.video(EXCLUSIONS, 'stream', i, rng, views=views, duration='PT7200S', published_at=end - hours(2),
                   stream_actual_end_at=end, **(dict(listed, availability_checked_at=None) if i == 2 else listed))
    for row in data.videos:
        data.snapshots_for(row, cap=48 if row['channel_id'] != BIG else 6)
    data.daily_channel_snapshots(MAIN, subscribers=1250000, views=987654321, video_count=420)
    data.daily_channel_snapshots(BIG, subscribers=9007199254740993, views=19000000000, video_count=20600)
    data.daily_channel_snapshots(CC_FULL, subscribers=5000, views=800000, video_count=12)
    # Two observations 5.04 days apart, the start one day after the 7D boundary.
    data.channel_snapshots += [(CC_FIVE, now - timedelta(days=6) - timedelta(minutes=10), 700000, 4000, 12),
                               (CC_FIVE, now - timedelta(days=.96), 770000, 4400, 12)]
    data.channel_snapshots += [(CC_ZERO, now - timedelta(days=8), 0, 0, 12), (CC_ZERO, now - timedelta(hours=3), 500, 7, 12)]
    data.channel_snapshots += [(CC_STALE, now - timedelta(hours=26), 800000, 5000, 12),
                               (CC_STALE, now - timedelta(days=9), 790000, 4900, 12)]
    # The latest check (at the profile clock) reports fewer public videos.
    data.channel_snapshots += [(CC_DROP, now - hours(26), 799000, 5000, 14), (CC_DROP, now - hours(2), 800000, 5000, 12)]
    return data


def head_absence(admin, channel, kind, checked):
    from psycopg.types.json import Jsonb
    attempt = str(uuid.uuid4())
    lease = str(uuid.uuid4())
    playlist = PREFIX[kind] + channel[2:]
    head = dict(transition_id=attempt, epoch=1, outcome='playlist_not_found',
                source_checked_at=checked.isoformat(), lease_token=lease)
    evidence = dict(transition_id=attempt, lease_token=lease, observation_kind='head_page',
                    outcome='playlist_not_found', provider_http_status=404, error_code='typed_playlist_not_found',
                    channel_id=channel, video_type=kind, playlist_id=playlist, control_epoch=1)
    admin.execute("""INSERT INTO public.typed_inventory_receipts
        (receipt_scope,transition_id,source_checked_at,result) VALUES('addressed_membership',%s,%s,%s)""",
        (attempt, checked, Jsonb(evidence)))
    admin.execute("UPDATE public.channel_playlist_state SET head_last_attempt=%s WHERE channel_id=%s AND video_type=%s",
                  (Jsonb(head), channel, kind))


def seed(admin, data):
    now = data.now
    admin.execute('INSERT INTO public.folders(id,name) VALUES(%s,%s)', (FOLDER, 'Overview fixture'))
    for c in data.channels:
        owner = uuid.uuid4()
        admin.execute('''INSERT INTO public.channels(channel_id,user_work_id,folder_id,added_at,title,subscribers,views,video_count,
            country_code,country_collected_at,channel_created_at,channel_metadata_checked_at,channel_metadata_status,avatar_url)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (c['channel_id'], owner, FOLDER, now - timedelta(days=60), c['title'], c['subscribers'], c['views'], c['video_count'],
             c['country_code'], c['country_collected_at'], c['channel_created_at'], c['channel_metadata_checked_at'],
             c['channel_metadata_status'], c['avatar_url']))
        admin.execute('''INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,control_epoch)
            VALUES(%s,%s,'accepted',false,1)''', (c['channel_id'], owner))
        for kind, prefix in PREFIX.items():
            status = c['inventory'][kind]
            playlist = prefix + c['channel_id'][2:]
            scan = uuid.uuid4()
            if status == 'completed':
                admin.execute('''INSERT INTO public.channel_video_inventory_scans
                    (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at,discovery_channel_epoch)
                    VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s,1)''',
                    (scan, c['channel_id'], kind, playlist, now - hours(3), now - hours(3), now - hours(2), now - hours(2)))
            admin.execute('''INSERT INTO public.channel_playlist_state
                (channel_id,video_type,playlist_id,inventory_status,last_completed_scan_id,source_checked_at,inventory_completed_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s)''',
                (c['channel_id'], kind, playlist, status, scan if status == 'completed' else None,
                 now - hours(2), now - hours(2) if status == 'completed' else None))
            if status == 'playlist_not_found':
                head_absence(admin, c['channel_id'], kind, now - hours(1))
    for start in range(0, len(data.videos), 5000):
        rows = data.videos[start:start + 5000]
        with admin.transaction():
            with admin.cursor().copy('COPY public.video_inventory(video_id,channel_id,video_type) FROM STDIN') as cp:
                for r in rows:
                    cp.write_row((r['video_id'], r['channel_id'], r['video_type']))
            with admin.cursor().copy('''COPY public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
                    source_published_at,first_seen_at,last_seen_at,last_present_at,evidence_source) FROM STDIN''') as cp:
                for r in rows:
                    seen = now - hours(2)
                    # A video also listed in another typed playlist has one membership per playlist.
                    for kind in (r['video_type'], *r.get('also_listed_as', ())):
                        cp.write_row((r['video_id'], r['channel_id'], kind, PREFIX[kind] + r['channel_id'][2:],
                                      r['published_at'], seen, seen, seen, 'legacy_inventory_backfill'))
            with admin.cursor().copy('''COPY public.videos(video_id,channel_id,title,views,checked_at,metadata_checked_at,published_at,
                    duration,like_count,comment_count,tags,youtube_topic_categories,category_id,default_audio_language,default_language,
                    caption_available,availability_status,availability_checked_at,stream_actual_end_at,publication_status) FROM STDIN''') as cp:
                for r in rows:
                    if r['row']:
                        cp.write_row(tuple(r[k] for k in ('video_id', 'channel_id', 'title', 'views', 'checked_at', 'metadata_checked_at',
                            'published_at', 'duration', 'like_count', 'comment_count', 'tags', 'youtube_topic_categories', 'category_id',
                            'default_audio_language', 'default_language', 'caption_available', 'availability_status',
                            'availability_checked_at', 'stream_actual_end_at', 'publication_status')))
    for start in range(0, len(data.snapshots), 20000):
        with admin.transaction():
            with admin.cursor().copy('COPY public.video_snapshots(video_id,views,recorded_at,views_definition_epoch) FROM STDIN') as cp:
                for video, views, at, epoch, _endpoint in data.snapshots[start:start + 20000]:
                    cp.write_row((video, views, at, epoch))
    with admin.transaction():
        with admin.cursor().copy('COPY public.channel_snapshots(channel_id,recorded_at,views,subscribers,video_count,source) FROM STDIN') as cp:
            for channel, at, views, subscribers, count in data.channel_snapshots:
                cp.write_row((channel, at, views, subscribers, count, 'youtube_channel_metadata_refresh'))
    admin.execute('ANALYZE')


def query(channel, kind='long', select=('video.id',), **extra):
    return dict(query_version=1, source={'id': 'public.videos.current.v1'}, scope={'kind': 'channel', 'id': channel},
                video_types=[kind], select=list(select), order_by=[{'field': 'video.id', 'direction': 'asc'}],
                page={'size': 1}, **extra)


def channel_query(channel, select=('channel.id',)):
    return dict(query_version=1, source={'id': 'public.channels.current.v1'}, scope={'kind': 'channel', 'id': channel},
                select=list(select), order_by=[{'field': 'channel.id', 'direction': 'asc'}], page={'size': 1})


def operation(q, name, parameters=None):
    return canonical(dict(version=1, operation=name, query=q, parameters=parameters or {}))


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def edges(preset, kind):
    """Bucket edges of the shared preset registry, per video type."""
    from research_query.registry import preset_buckets
    return tuple((b.key, b.label, b.min, b.max) for b in preset_buckets(preset, kind))


class OverviewPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        password = os.environ['LIC_QUERY_TEST_PASSWORD']
        port = int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres', password=password, autocommit=True)
        version = cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000 <= int(version) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        started = time.monotonic()
        cls.now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        cls.data = build_catalogue(cls.now)
        seed(cls.admin, cls.data)
        cls.endpoints = {video for video, _views, _at, _epoch, endpoint in cls.data.snapshots if endpoint}
        cls.context = AccessContext('synthetic-installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
        from collection_schedule import CollectionSchedules
        cls.db = PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=2),
                                  cadence_snapshot=CollectionSchedules().cadence_snapshot(), admission=lambda _: None).open()
        cls.repo = ResearchQueryRepository(cls.db)
        cls.service = QueryService(cls.repo, lambda: cls.context)
        from db.inventory.search_projection import SearchProjectionRepository
        projection = SearchProjectionRepository(cls.db)
        while projection.refresh():
            pass
        print(f'Overview fixture ready: {len(cls.data.videos)} videos, {len(cls.data.snapshots)} snapshots, '
              f'{time.monotonic()-started:.1f}s; server_version_num={version}', flush=True)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    def run_operation(self, q, name, parameters=None):
        return self.service.execute_operation(operation(q, name, parameters))

    # ---- helpers ---------------------------------------------------------------
    def population(self, channel, kind, select, zone='UTC'):
        """The shared query population of one type, every row, with cell states."""
        q = dict(query(channel, kind, select), page={'size': 200}, time={'mode': 'current', 'timezone': zone})
        rows, result = {}, self.service.execute(canonical(q))
        while True:
            for row in result['rows']:
                rows[row['id']] = {f: (c['state'], c['value']) for f, c in row['cells'].items()}
            if not result['page']['has_more']:
                return rows
            q['page'] = {'size': 200, 'after': result['page']['next_cursor']}
            result = self.service.execute(canonical(q))

    def fixture_row(self, identity):
        return next(r for r in self.data.videos if r['video_id'] == identity)


    @staticmethod
    def in_period(cells, period, evaluated_at):
        days = {'7d': 7, '28d': 28, '3m': 90, '1y': 365, 'max': None}[period]
        if days is None:
            return True
        state, value = cells['video.published_at']
        return state == 'known' and evaluated_at - timedelta(days=days) <= instant(value) <= evaluated_at


    def test_a_value_without_a_definition_is_excluded_not_the_metric(self):
        first, second = video_id(EPOCH, 'long', 1), video_id(EPOCH, 'long', 2)
        saved = self.admin.execute('SELECT video_id,views,recorded_at,views_definition_epoch FROM public.video_snapshots WHERE video_id=%s',
                                   (first,)).fetchall()
        try:
            self.admin.execute('DELETE FROM public.video_snapshots WHERE video_id=%s', (first,))
            summary = self.run_operation(query(EPOCH), 'video.summary.v1')['result']
            views = summary['metrics']['views']
            self.assertEqual((views['state'], views['known_count'], views['missing_count'], views['unknown_definition_epoch_count']),
                             ('known', 19, 1, 1))
            self.assertEqual(views['total'], str(sum(1000 + i for i in range(20) if i != 1)))
            self.assertEqual(views['definition_epoch'], 'youtube.views.play_start.20260824')
            fingerprint = self.run_operation(query(EPOCH), 'video.fingerprint.v1', {'period': 'max'})
            views = fingerprint['result']['metrics']['views']
            self.assertEqual((views['known_count'], views['missing_count'], views['excluded_count']), (19, 0, 1))
            # Two different recorded definitions still withhold the whole metric.
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=%s WHERE video_id=%s', (EPOCH_SECOND, second))
            views = self.run_operation(query(EPOCH), 'video.summary.v1')['result']['metrics']['views']
            self.assertEqual((views['state'], views['known_count'], views['total']), ('mixed_definition_epochs', 0, None))
            views = self.run_operation(query(EPOCH), 'video.fingerprint.v1', {'period': 'max'})['result']['metrics']['views']
            self.assertEqual((views['state'], views['known_count'], views['excluded_count']), ('mixed_definition_epochs', 0, 20))
        finally:
            self.admin.execute('UPDATE public.video_snapshots SET views_definition_epoch=NULL WHERE video_id=%s', (second,))
            for row in saved:
                self.admin.execute('INSERT INTO public.video_snapshots(video_id,views,recorded_at,views_definition_epoch) VALUES(%s,%s,%s,%s)', row)

    FIELDS = ('video.views', 'video.duration_seconds', 'video.likes', 'video.comments', 'video.title', 'video.tags')
    METRICS = ('views', 'duration_seconds', 'likes', 'comments', 'title_length', 'tag_count')
    PRESETS = dict(views='views', duration_seconds='duration', likes='likes', comments='comments',
                   title_length='title_length', tag_count='tag_count')

    @staticmethod
    def values(rows, field):
        out = []
        for cells in rows.values():
            state, value = cells[field]
            if state == 'known':
                out.append(len(value) if field in ('video.title', 'video.tags') else int(value))
        return out

    def test_fingerprint_summarizes_the_selected_rows(self):
        for kind in ('long', 'short', 'stream'):
            cells = self.population(MAIN, kind, ('video.id', 'video.published_at') + self.FIELDS)
            for period in ('7d', '28d', '3m', '1y', 'max'):
                with self.subTest(kind=kind, period=period):
                    response = self.run_operation(query(MAIN, kind), 'video.fingerprint.v1', {'period': period})
                    result, evaluated = response['result'], instant(response['context']['evaluated_at'])
                    rows = {i: c for i, c in cells.items() if self.in_period(c, period, evaluated)}
                    self.assertEqual(result['population']['matched'], len(rows))
                    # A current counter without its real snapshot has no recorded definition.
                    undefined = {i for i, c in rows.items() if c['video.views'][0] == 'known' and i not in self.endpoints}
                    self.assertEqual(result['metrics']['views']['excluded_count'], len(undefined))
                    self.assertEqual(result['uploads']['known_count'], sum(c['video.published_at'][0] == 'known' for c in rows.values()))
                    for field, metric in zip(self.FIELDS, self.METRICS):
                        new = result['metrics'][metric]
                        defined = {i: c for i, c in rows.items() if field != 'video.views' or i not in undefined}
                        values = self.values(defined, field)
                        self.assertEqual(new['known_count'], len(values))
                        if values:
                            self.assertEqual(int(new['total']), sum(values))
                            self.assertEqual(Fraction(new['median']), median([Fraction(v) for v in values]))
                        self.assertEqual(new['known_count'] + new['missing_count'] + new['excluded_count'], len(rows))
                        self.assertEqual(sum(b['count'] for b in new['buckets']), len(values))

    def test_fingerprint_coverage_counts_rejections_and_stale_values(self):
        response = self.run_operation(query(MAIN), 'video.fingerprint.v1', {'period': 'max'})
        population, coverage = response['result']['population'], response['coverage']
        self.assertEqual(sum(population['rejected'].values()), coverage['reasons'].get('source_rows_unavailable', 0))
        self.assertEqual(population['admitted'] + sum(population['rejected'].values()), population['scanned'])
        for reason in ('source_unavailable', 'source_missing', 'source_stale', 'source_unpublished'):
            self.assertGreater(population['rejected'][reason], 0, reason)
        self.assertGreater(population['stale_count'], 0)
        streams = self.run_operation(query(MAIN, 'stream'), 'video.fingerprint.v1', {'period': 'max'})['result']['population']
        self.assertGreater(streams['rejected']['stream_incomplete'], 0)

    def test_overview_results_live_as_long_as_their_data(self):
        # Without a time-driven change the data clocks decide: the earliest is a
        # waiting publication about 30 hours ahead (the fixture's upcoming rows).
        for name, parameters in (('video.fingerprint.v1', {'period': 'max'}), ('video.publication_rhythm.v1', {'period': 'max'}),
                                 ('video.publication_years.v1', {})):
            context = self.run_operation(query(MAIN), name, parameters)['context']
            lifetime = instant(context['valid_before']) - instant(context['evaluated_at'])
            self.assertTrue(hours(20) < lifetime <= hours(30), (name, lifetime))
        cells = self.population(MAIN, 'long', ('video.id', 'video.published_at'))
        published = [instant(c['video.published_at'][1]) for c in cells.values() if c['video.published_at'][0] == 'known']
        for name, parameters in (('video.fingerprint.v1', {'period': '28d'}), ('video.publication_rhythm.v1', {'period': '28d'}),
                                 ('video.content_breakdown.v1', {'period': '28d'})):
            context = self.run_operation(query(MAIN), name, parameters)['context']
            evaluated, until = instant(context['evaluated_at']), instant(context['valid_before'])
            # The result ends at the latest when its oldest video leaves the rolling 28 days.
            oldest = min(p for p in published if p >= evaluated - timedelta(days=28))
            self.assertEqual(min(until, oldest + timedelta(days=28, microseconds=1)), until, name)
            self.assertGreater(until, evaluated)
        breakdown = self.run_operation(query(MAIN), 'video.content_breakdown.v1', {'period': 'max'})['context']
        evaluated = instant(breakdown['evaluated_at'])
        anniversary = min(p + timedelta(days=years * 365) for p in published for years in (1, 3, 5)
                          if p + timedelta(days=years * 365) > evaluated)
        self.assertLessEqual(instant(breakdown['valid_before']), anniversary)
        context = self.run_operation(query(MAIN), 'video.summary.v1')['context']
        self.assertLessEqual(instant(context['valid_before']) - instant(context['evaluated_at']), timedelta(seconds=120))

    # ---- Rhythm and calendar ------------------
    def test_rhythm_and_years_use_the_fingerprint_population_in_the_viewer_zone(self):
        from zoneinfo import ZoneInfo
        zone = 'America/New_York'
        cells = self.population(MAIN, 'long', ('video.id', 'video.published_at'), zone)
        zoned = dict(query(MAIN), time={'mode': 'current', 'timezone': zone})
        for period in ('28d', 'max'):
            response = self.run_operation(zoned, 'video.publication_rhythm.v1', {'period': period})
            result, evaluated = response['result'], instant(response['context']['evaluated_at'])
            moments = sorted(instant(c['video.published_at'][1]) for c in cells.values()
                             if c['video.published_at'][0] == 'known' and self.in_period(c, period, evaluated))
            local = [m.astimezone(ZoneInfo(zone)) for m in moments]
            self.assertEqual((result['known_count'], result['time_zone']), (len(moments), zone))
            self.assertEqual([h['count'] for h in result['hours']], [sum(m.hour == h for m in local) for h in range(24)])
            self.assertEqual([d['count'] for d in result['weekdays']], [sum(m.isoweekday() == d for m in local) for d in range(1, 8)])
            self.assertEqual([row['hours'] for row in result['grid']],
                             [[sum(m.isoweekday() == d and m.hour == h for m in local) for h in range(24)] for d in range(1, 8)])
            counts = [h['count'] for h in result['hours']]
            self.assertEqual(result['peak_hour'], {'hour': counts.index(max(counts)), 'count': max(counts)})
            self.assertEqual((instant(result['first_published_at']), instant(result['last_published_at'])), (moments[0], moments[-1]))
        years = self.run_operation(zoned, 'video.publication_years.v1')
        result, evaluated = years['result'], instant(years['context']['evaluated_at'])
        local = [instant(c['video.published_at'][1]).astimezone(ZoneInfo(zone)) for c in cells.values()
                 if c['video.published_at'][0] == 'known']
        current = evaluated.astimezone(ZoneInfo(zone)).year
        self.assertEqual((result['default_year'], result['year'], result['time_zone']), (current, current, zone))
        self.assertEqual(result['available_years'], sorted({current, *(m.year for m in local)}, reverse=True))
        expected = {}
        for m in local:
            if m.year == current:
                expected[m.date().isoformat()] = expected.get(m.date().isoformat(), 0) + 1
        self.assertEqual({d['date']: d['count'] for d in result['days']}, expected)
        other = result['available_years'][-1]
        chosen = self.run_operation(zoned, 'video.publication_years.v1', {'year': other})['result']
        self.assertEqual((chosen['year'], sum(d['count'] for d in chosen['days'])), (other, sum(m.year == other for m in local)))
        self.assertEqual(sum(expected.values()), sum(m.year == current for m in local))

    def test_publication_years_and_calendar_follow_the_canonical_published_year(self):
        """The years and days are the rows' own video.published_year / video.published_date
        the calendar block takes time.timezone, a saved study's own zone still counts;
        a local year outside 1..9999 is not known in all three."""
        from db.inventory.search_projection import SearchProjectionRepository
        zone = 'America/New_York'
        zoned = dict(query(MAIN), time={'mode': 'current', 'timezone': zone})
        cells = self.population(MAIN, 'long', ('video.id', 'video.published_date', 'video.published_year'), zone)
        known = [c for c in cells.values() if c['video.published_year'][0] == 'known']
        result = self.run_operation(zoned, 'video.publication_years.v1')['result']
        self.assertEqual((result['population'], result['known_count']), (len(cells), len(known)))
        self.assertEqual(set(result['available_years']), {result['default_year'], *(int(c['video.published_year'][1]) for c in known)})
        days = {}
        for c in known:
            if int(c['video.published_year'][1]) == result['year']:
                days[c['video.published_date'][1]] = days.get(c['video.published_date'][1], 0) + 1
        self.assertEqual({d['date']: d['count'] for d in result['days']}, days)
        # The calendar block: without its own zone it reads time.timezone; with one, that zone.
        year = int(known[0]['video.published_year'][1])
        bare = self.run_operation(zoned, 'video.publication_calendar.v1', {'year': year})['result']
        named = self.run_operation(zoned, 'video.publication_calendar.v1', {'year': year, 'time_zone': zone})['result']
        utc = self.run_operation(zoned, 'video.publication_calendar.v1', {'year': year, 'time_zone': 'UTC'})['result']
        self.assertEqual(bare, named)
        self.assertEqual((bare['time_zone'], utc['time_zone']), (zone, 'UTC'))
        chosen = [c['video.published_date'][1] for c in known if int(c['video.published_year'][1]) == year]
        self.assertEqual({d['date']: d['count'] for d in bare['days']}, {d: chosen.count(d) for d in chosen})
        self.assertEqual(bare['known_count'], len(known))
        # Year 0 in New York: a publication at 0001-01-01T03:00Z is 31 Dec 1 BC there.
        target = next(i for i, c in cells.items() if c['video.published_year'][0] == 'known')
        original = self.admin.execute('SELECT published_at FROM public.videos WHERE video_id=%s', (target,)).fetchone()[0]
        projection = SearchProjectionRepository(self.db)
        try:
            self.admin.execute("UPDATE public.videos SET published_at='0001-01-01T03:00:00Z' WHERE video_id=%s", (target,))
            while projection.refresh():
                pass
            edge = self.population(MAIN, 'long', ('video.id', 'video.published_at', 'video.published_year'), zone)[target]
            self.assertEqual((edge['video.published_at'][0], edge['video.published_year'][0]), ('known', 'unavailable'))
            years = self.run_operation(zoned, 'video.publication_years.v1')['result']
            self.assertEqual(years['known_count'], len(known) - 1)
            self.assertTrue(all(1 <= y <= 9999 for y in years['available_years']))
            calendar = self.run_operation(zoned, 'video.publication_calendar.v1', {'year': 1})['result']
            self.assertEqual((calendar['days'], calendar['known_count']), ([], len(known) - 1))
            self.assertEqual(self.run_operation(zoned, 'video.publication_calendar.v1', {'year': 1, 'time_zone': 'UTC'})['result']['days'],
                             [{'date': '0001-01-01', 'count': 1}])
        finally:
            self.admin.execute('UPDATE public.videos SET published_at=%s WHERE video_id=%s', (original, target))
            while projection.refresh():
                pass

    # ---- Content breakdown ---------------------------------
    BREAKDOWN_FIELDS = {'video.category_id': 'category_id', 'video.topic_urls': 'youtube_topic_categories',
                        'video.audio_language': 'default_audio_language', 'video.metadata_language': 'default_language',
                        'video.caption_available': 'caption_available', 'video.published_at': 'published_at'}

    def test_content_breakdown_counts_the_selected_sample(self):
        names = self.BREAKDOWN_FIELDS
        for kind in ('long', 'short', 'stream'):
            cells = self.population(MAIN, kind, ('video.id',) + tuple(names))
            for period in ('28d', '1y', 'max'):
                with self.subTest(kind=kind, period=period):
                    response = self.run_operation(query(MAIN, kind), 'video.content_breakdown.v1', {'period': period})
                    result, evaluated = response['result'], instant(response['context']['evaluated_at'])
                    chosen = {i: c for i, c in cells.items() if self.in_period(c, period, evaluated)}
                    for section in ('youtube_topics', 'official_categories'):
                        group = result[section]
                        self.assertEqual(group['sample_count'], len(chosen))
                        self.assertEqual(group['known_count'] + group['missing_count'], len(chosen))
                        if section == 'youtube_topics':
                            self.assertEqual(group['assigned_count'] + group['empty_count'], group['known_count'])

    # ---- Header: 7D/28D change ---------------------------------
    def test_counter_change_uses_the_window_tolerance_rule(self):
        for channel in (CC_FULL, CC_FIVE, CC_ZERO, CC_STALE, CC_NONE, MAIN):
            response = self.run_operation(channel_query(channel), 'channel.counter_change.v1')
            new = response['result']
            self.assertIsNone(new['definition_epoch'])
            for period in ('7d', '28d'):
                for metric in ('subscribers', 'views'):
                    b = new['windows'][period]['metrics'][metric]
                    if b['status'] == 'ready':
                        window = 7 if period == '7d' else 28
                        self.assertTrue(window * 86400 <= b['actual_interval_seconds'] <= (window + 1) * 86400)
                        self.assertEqual(b['percent_change'], format(round(int(b['absolute_change']) * 100 / int(b['start_value']), 6), 'f')
                                         .rstrip('0').rstrip('.'))
            if channel == CC_FIVE:
                b = new['windows']['7d']['metrics']['views']
                self.assertEqual((b['status'], b['coverage']), ('partial','partial'))
                self.assertTrue(5 < b['actual_interval_seconds'] / 86400 < 5.1)
            if channel == CC_FULL:
                self.assertEqual(new['windows']['7d']['metrics']['views']['actual_interval_seconds'], 7 * 86400)
            if channel == CC_STALE:
                self.assertEqual(new['windows']['7d']['metrics']['views']['status'], 'stale_current')
            if channel == CC_NONE:
                self.assertEqual(new['windows']['28d']['metrics']['views']['status'], 'not_reported')

    # ---- Header: per-format totals --------------------------
    def test_format_totals_respect_stream_completion_and_collection_settings(self):
        totals = self.run_operation(channel_query(TOTALS), 'channel.format_totals.v1')['result']['formats']
        self.assertEqual((totals['long']['status'], totals['long']['views']), ('ready', '6000'))
        self.assertEqual((totals['stream']['status'], totals['stream']['excluded']['stream_completion_withheld']), ('incomplete', 1))
        typed = self.run_operation(channel_query(CC_TYPED), 'channel.format_totals.v1')['result']['formats']
        self.assertEqual((typed['short']['status'], typed['stream']['status']), ('absent', 'not_collected'))

    def test_format_totals_exclude_private_withhold_unconfirmed_and_count_automatic_types_once(self):
        rows = [r for r in self.data.videos if r['channel_id'] == EXCLUSIONS]
        listed = {kind: [r for r in rows if r['video_type'] == kind] for kind in PREFIX}
        private = [r for r in rows if r['availability_status'] == 'private']
        unconfirmed = [r for r in rows if r['availability_checked_at'] is None]
        overlaps = {r['video_id']: r['video_type'] for r in rows if r.get('also_listed_as')}
        self.assertEqual(([r['video_type'] for r in private], [r['video_type'] for r in unconfirmed], sorted(overlaps.values())),
                         (['long'], ['stream'], ['long', 'short']))
        # The automatic Long/Short rule types each overlap as its stored type.
        automatic = self.admin.execute('SELECT video_id,automatic_type FROM public.video_type_automatic_candidates WHERE channel_id=%s',
                                       (EXCLUSIONS,)).fetchall()
        self.assertEqual(dict(automatic), overlaps)

        def public_views(kind):
            return str(sum(r['views'] for r in listed[kind] if r not in private and r not in unconfirmed))
        totals = self.run_operation(channel_query(EXCLUSIONS), 'channel.format_totals.v1')['result']['formats']
        for kind, total in totals.items():
            # Every listed video is scanned once: an overlap only in the format of its stored type.
            self.assertEqual((total['scanned_inventory_count'], total['canonical_inventory_count'], total['reason_code']),
                             (len(listed[kind]), len(listed[kind]), None), kind)
        # The private video leaves the Long sum, which stays ready.
        long = totals['long']
        self.assertEqual((long['status'], long['views'], long['excluded']['unavailable_excluded_count'], long['missing_view_count'],
                          long['known_view_count']), ('ready', public_views('long'), len(private), 0, len(listed['long']) - len(private)))
        # The Shorts sum holds the overlap typed as a Short and not the one typed as Long.
        short = totals['short']
        self.assertEqual((short['status'], short['views'], short['missing_view_count'], short['known_view_count']),
                         ('ready', public_views('short'), 0, len(listed['short'])))
        # The stream without an availability check withholds the Streams total.
        stream = totals['stream']
        self.assertEqual((stream['excluded']['availability_withheld_count'], stream['excluded']['unavailable_excluded_count'],
                          stream['missing_view_count'], stream['known_view_count']),
                         (len(unconfirmed), 0, len(unconfirmed), len(listed['stream']) - len(unconfirmed)))
        self.assertEqual((stream['status'], stream['views']), ('incomplete', None))

    def test_library_current_coverage_survives_expired_initial_report(self):
        from unittest.mock import patch
        from channel_hydration import with_format_readiness
        from channel_explorer import build_channel_list_inventory
        from db.reads.channel_explorer import ChannelExplorerRepository

        channel = 'UCLibraryProofCase000000'
        historical = with_format_readiness({'job': dict(error_code='source_scope_expired', status='completed',
                                                          phase='verify', errors=0)})
        self.assertTrue(historical['all_initial_checks_complete'])
        self.assertFalse(historical['current_profile_ready'])
        repo = ChannelExplorerRepository(self.db)
        def library(enabled=('long', 'short')):
            with patch('channel_explorer.shown_formats', return_value=enabled):
                return build_channel_list_inventory(repo, channel)['library']
        current = library()
        self.assertEqual(current['status'], 'ready')
        formats = current['formats']
        self.assertEqual([formats[k]['status'] for k in ('long','short','stream')], ['absent','complete','turned_off'])
        self.assertIsNone(formats['long']['full_completed_at'])
        self.assertTrue(formats['long']['absent_checked_at'])
        completed = formats['short']['full_completed_at']
        self.assertTrue(completed)
        with_streams = library(('long', 'short', 'stream'))
        self.assertEqual(with_streams['status'], 'partial')
        self.assertEqual(with_streams['formats']['stream']['status'], 'not_checked')
        self.assertEqual(with_streams['formats']['short'], formats['short'])

        def assert_surfaces():
            page = self.service.execute(canonical(dict(query(channel, 'short'), consistency='frozen', page={'size':100})))
            self.assertEqual(len(page['rows']), 75)
            overview = self.run_operation(query(channel,'short'), 'video.fingerprint.v1', {'period':'max'})
            self.assertEqual(overview['result']['population']['matched'], 75)
        assert_surfaces()
        totals = self.run_operation(channel_query(channel),'channel.format_totals.v1')['result']['formats']
        self.assertEqual((totals['long']['status'],totals['short']['views']), ('absent','7500'))
        # A failed first-page check leaves the last completed full walk intact.
        self.admin.execute("""UPDATE public.channel_playlist_state
            SET head_last_attempt=jsonb_build_object('epoch',1,'outcome','error','source_checked_at',clock_timestamp())
            WHERE channel_id=%s AND video_type='short'""", (channel,))
        try:
            after_head = library()['formats']['short']
            self.assertEqual((after_head['status'], after_head['full_completed_at']), ('check_failed', completed))
        finally:
            self.admin.execute("UPDATE public.channel_playlist_state SET head_last_attempt=NULL WHERE channel_id=%s AND video_type='short'", (channel,))
        for code, expected in [(None, 'checking'), ('youtube_daily_quota_exceeded_error', 'waiting_for_quota')]:
            with self.subTest(status=expected):
                self.admin.execute("""UPDATE public.channel_playlist_state SET inventory_status='running',
                    last_error_code=%s,next_due_at=clock_timestamp()+interval '1 hour'
                    WHERE channel_id=%s AND video_type='short'""", (code, channel))
                try:
                    waiting = library()['formats']['short']
                    self.assertEqual((waiting['status'], waiting['full_completed_at']), (expected, completed))
                    assert_surfaces()
                finally:
                    self.admin.execute("""UPDATE public.channel_playlist_state SET inventory_status='completed',
                        last_error_code=NULL,next_due_at=NULL WHERE channel_id=%s AND video_type='short'""", (channel,))
        # A later attempt and a changed control generation cannot invent a full completion.
        self.admin.execute("UPDATE public.channel_playlist_state SET inventory_status='error' WHERE channel_id=%s AND video_type='short'", (channel,))
        try:
            pending = library()
            self.assertEqual(pending['status'], 'partial')
            self.assertEqual(pending['formats']['short']['status'], 'check_failed')
            self.assertEqual(pending['formats']['short']['full_completed_at'], completed)
            assert_surfaces()
            self.admin.execute("UPDATE public.channel_collection_state SET control_epoch=2 WHERE channel_id=%s",(channel,))
            moved = library()['formats']
            self.assertIsNone(moved['short']['full_completed_at'])
            self.assertIsNone(moved['long']['absent_checked_at'])
        finally:
            self.admin.execute("UPDATE public.channel_collection_state SET control_epoch=1 WHERE channel_id=%s",(channel,))
            self.admin.execute("UPDATE public.channel_playlist_state SET inventory_status='completed' WHERE channel_id=%s AND video_type='short'", (channel,))

    def test_streams_off_keeps_long_and_short_byte_identical(self):
        """With Streams turned off, Long-form and Shorts answers do not change by a byte."""
        from dataclasses import replace
        off = QueryService(self.repo, lambda: replace(self.context, disabled_video_types=frozenset({'stream'})),
                           codec=self.service.codec)

        def same(call):
            # Two reads differ only in instants taken at evaluation (evaluated_at, valid_before,
            # a period's end): every instant at or after this comparison began is one placeholder.
            # Stored values were all written before it; everything else is compared byte for byte.
            started = self.admin.execute('SELECT clock_timestamp()').fetchone()[0] - timedelta(seconds=1)

            def strip(value):
                def clean(node):
                    if isinstance(node, dict):
                        return {key: clean(item) for key, item in node.items() if key != 'page'}
                    if isinstance(node, list):
                        return [clean(item) for item in node]
                    if isinstance(node, str) and len(node) >= 20 and node[4:5] == '-' and node[10:11] == 'T':
                        try:
                            if instant(node) >= started:
                                return '<evaluation instant>'
                        except ValueError:
                            pass
                    return node
                return canonical(clean(json.loads(canonical(value))))
            self.assertEqual(strip(call(self.service)), strip(call(off)))

        for channel in (MAIN, TOTALS, BIG):
            for kind in ('long', 'short'):
                with self.subTest(channel=channel, kind=kind):
                    q = dict(query(channel, kind, ('video.id', 'video.views', 'video.title', 'video.published_at')),
                             page={'size': 200})
                    same(lambda service: service.execute(canonical(q)))
                    for name, parameters in (('video.fingerprint.v1', {'period': 'max'}),
                                             ('video.publication_rhythm.v1', {'period': 'max'}),
                                             ('video.publication_years.v1', {}),
                                             ('video.content_breakdown.v1', {'period': 'max'}),
                                             ('video.summary.v1', {})):
                        same(lambda service: service.execute_operation(operation(query(channel, kind), name, parameters)))
        with self.assertRaises(QueryRejected) as caught:
            off.execute(canonical(query(MAIN, 'stream')))
        self.assertEqual(caught.exception.code, 'format_turned_off')
        with self.assertRaises(QueryRejected) as caught:
            off.execute_operation(operation(query(MAIN, 'stream'), 'video.fingerprint.v1', {'period': 'max'}))
        self.assertEqual(caught.exception.code, 'format_turned_off')
        totals = off.execute_operation(operation(channel_query(TOTALS), 'channel.format_totals.v1'))['result']['formats']
        before = self.run_operation(channel_query(TOTALS), 'channel.format_totals.v1')['result']['formats']
        self.assertEqual((totals['long'], totals['short']), (before['long'], before['short']))
        self.assertEqual((totals['stream']['status'], totals['stream']['reason_code'], totals['stream']['views']),
                         ('turned_off', 'format_turned_off', None))

    # ---- Header profile fields: channel and profile agree
    def test_channel_source_matches_profile_fields(self):
        from channel_explorer import build_channel_profile
        from db.reads.channel_explorer import ChannelExplorerRepository
        select = ('channel.id', 'channel.title', 'channel.subscribers', 'channel.views', 'channel.video_count',
                  'channel.country_code', 'channel.created_at')
        for channel in (MAIN, BIG, CC_DROP):
            result = self.service.execute(canonical(channel_query(channel, select)))
            cells = result['rows'][0]['cells']
            old = build_channel_profile(ChannelExplorerRepository(self.db), channel)
            for field, key in (('channel.title', 'title'), ('channel.subscribers', 'subscribers'), ('channel.views', 'views'),
                               ('channel.video_count', 'video_count'), ('channel.country_code', 'country_code')):
                value = old['channel'][key]
                self.assertEqual(cells[field]['value'], None if value is None else str(value), (channel, field))
            self.assertEqual(instant(cells['channel.created_at']['value']), instant(str(old['channel']['channel_created_at'])))
            self.assertEqual(result['coverage']['status'] == 'complete', old['freshness']['channel_data_fresh'])

    # ---- Streams ------------------------------------------------------
    def test_stream_age_is_counted_from_its_actual_end(self):
        q = dict(query(MAIN, 'stream', ('video.id', 'observation.views_at_age'), where={'field': 'video.id', 'op': 'eq', 'value': AGE_STREAM}),
                 page={'size': 5})
        cell = self.service.execute(canonical(q))['rows'][0]['cells']['observation.views_at_age']
        row = self.fixture_row(AGE_STREAM)
        self.assertEqual(cell['state'], 'known')
        self.assertEqual(instant(cell['observation']['from_at']), row['stream_actual_end_at'])
        self.assertEqual(int(cell['observation']['actual_seconds'].split('.')[0]), 23 * 3600 + 1800)
        self.assertEqual(cell['value'], str(5000 - 90 * 26))
        # A 24-hour observation for this stream is measured from its end.
        self.assertLess(row['published_at'] + hours(24), row['stream_actual_end_at'])

    # ---- Budgets: 20,000 videos ----------------------------------------------------
    def test_zz_budget_twenty_thousand_videos(self):
        timings = {}
        zoned = dict(query(BIG), time={'mode': 'current', 'timezone': 'Europe/Kyiv'})
        cases = [('video.summary.v1', query(BIG), {}), ('video.fingerprint.v1', query(BIG), {'period': 'max'}),
                 ('video.fingerprint.v1', query(BIG), {'period': '1y'}), ('video.publication_rhythm.v1', zoned, {'period': 'max'}),
                 ('video.publication_years.v1', zoned, {}), ('video.content_breakdown.v1', query(BIG), {'period': 'max'}),
                 ('channel.counter_change.v1', channel_query(BIG), {}), ('channel.format_totals.v1', channel_query(BIG), {})]
        for name, q, parameters in cases:
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                output = self.run_operation(q, name, parameters)
                samples.append(round((time.perf_counter() - started) * 1000))
            size = len(canonical(output))
            timings[name + ' ' + json.dumps(parameters)] = dict(ms=samples, bytes=size)
            self.assertLess(max(samples), 8000)
            self.assertLess(size, 512 * 1024)
        fingerprint = self.run_operation(query(BIG), 'video.fingerprint.v1', {'period': 'max'})
        self.assertGreater(fingerprint['result']['population']['matched'], 18000)
        # The benchmark below exercises the current overview on the same channel.
        print('Budget (20,000 long videos):', json.dumps(timings), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
