"""On a disposable PostgreSQL 17 instance: a passing failure or an interrupted run keeps a confirmed profile.

Never auto-discovered; the same disposable-cluster contract as research_query_pg.
Synthetic channels only, no YouTube. The actual collector owners compute the
values; the actual cleanup, HOT release, projection and readable view decide.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import requests
from research_query_pg import bootstrap
from collection import channel_metadata as metadata
from db.channel.channel_sync import ChannelSyncRepository
from db.channel.channel_workflow_updates import update_channel_fields
from db.hot.hot_metadata_termination import drain_hot_channel_metadata
from db.retention.current_source_retention import cleanup_hot_current_source


class Transaction:
    """The owner's transaction surface over one autocommit admin connection."""
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def borrow(self):
        yield

    @contextmanager
    def savepoint(self):
        with self.connection.transaction():
            yield

    def fetch_one(self, query, params=(), **_):
        return self.connection.execute(query, params).fetchone()

    def fetch_all(self, query, params=(), max_rows=None, **_):
        return self.connection.execute(query, params).fetchall()

    returning_one = fetch_one

    def execute(self, query, params=()):
        return self.connection.execute(query, params).rowcount


class Database:
    """Enough of the gateway for the real ChannelSyncRepository read and write owners."""
    def __init__(self, connection):
        self.tx = Transaction(connection)

    def read_rows(self, name, query, parameters, *, max_rows):
        return self.tx.fetch_all(query, parameters)

    @contextmanager
    def transaction(self, operation):
        with self.tx.connection.transaction():
            yield self.tx


class ProfileStatusPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        cls.admin = psycopg.connect(host='127.0.0.1', port=int(os.environ['LIC_QUERY_TEST_PORT']), dbname='leti-cook',
            user='postgres', password=os.environ['LIC_QUERY_TEST_PASSWORD'], autocommit=True)
        version = cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000 <= int(version) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        cls.admin.row_factory = dict_row
        cls.tx = Transaction(cls.admin)

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def channel(self, status, *, error=None, checked_hours=1):
        identity = 'UC' + uuid.uuid4().hex[:22]
        owner = uuid.uuid4()
        self.admin.execute('INSERT INTO public.channels(channel_id,user_work_id) VALUES(%s,%s)', (identity, owner))
        self.admin.execute("INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,control_epoch)"
                           " VALUES(%s,%s,'accepted',1)", (identity, owner))
        self.admin.execute("""UPDATE public.channels SET title='Profile',avatar_url='https://example.test/a.png',
            subscribers=10,views=20,video_count=3,uploads_playlist_id='UU'||substr(channel_id,3),
            channel_metadata_status=%s,channel_metadata_error=%s,channel_metadata_error_count=0,
            channel_metadata_checked_at=clock_timestamp()-make_interval(hours=>%s) WHERE channel_id=%s""",
            (status, error, checked_hours, identity))
        self.addCleanup(self.admin.execute, 'DELETE FROM public.channels WHERE channel_id=%s', (identity,))
        return identity

    def stored(self, identity):
        return self.admin.execute('SELECT * FROM public.channels WHERE channel_id=%s', (identity,)).fetchone()

    def readable_title(self, identity):
        row = self.admin.execute('SELECT title FROM readable.channels WHERE channel_id=%s', (identity,)).fetchone()
        return row['title'] if row else None

    def cleanup(self):
        scrubbed, after = 0, None
        while True:
            page = cleanup_hot_current_source(self.tx, target='channel_profile', after_id=after, limit=100)
            scrubbed += page['scrubbed']
            if page['complete']:
                return scrubbed
            after = page['next_cursor']

    def mark_running(self, identity):
        started = datetime.now(timezone.utc).replace(microsecond=0)
        self.admin.execute("UPDATE public.channels SET channel_metadata_status='running',channel_metadata_started_at=%s"
                           " WHERE channel_id=%s", (started, identity))
        return started

    def collector_write(self, identity, error):
        """mark_channel_metadata_error's values through the actual channel writer."""
        captured = dict(self.stored(identity), collection_control_epoch=1)
        self.mark_running(identity)
        writes = []
        repository = SimpleNamespace(update_process_state=lambda channel_id, values, *, expected_epoch: writes.append(values) or True)
        context = SimpleNamespace(sources=SimpleNamespace(is_youtube_daily_quota_exhausted=lambda error: False))
        metadata.mark_channel_metadata_error(captured, error, datetime.now(timezone.utc), repository=repository, context=context)
        with self.admin.transaction():
            update_channel_fields(self.tx, identity, writes[0])

    def metadata_write(self, identity, values):
        with self.admin.transaction():
            update_channel_fields(self.tx, identity, values)

    def recheck_missing(self, identity, observed):
        """A recheck that YouTube answers without the channel: the running mark, then the collector's values."""
        captured = dict(self.stored(identity))
        self.metadata_write(identity, {'channel_metadata_status': 'running', 'channel_metadata_started_at': observed.isoformat()})
        self.metadata_write(identity, metadata.channel_not_found_values(captured, observed))

    def since(self, identity):
        return self.stored(identity)['channel_metadata_not_found_since']

    def test_first_missing_day_is_kept_until_youtube_returns_the_channel(self):
        # docs/DATA.md: the channel writer derives the day from the row before each write.
        channel = self.channel('ok')
        first = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3)
        self.recheck_missing(channel, first)
        self.assertEqual((self.stored(channel)['channel_metadata_status'], self.since(channel)), ('channel_not_found', first))
        self.recheck_missing(channel, first + timedelta(days=1))
        self.assertEqual((self.since(channel), self.stored(channel)['channel_metadata_checked_at']), (first, first + timedelta(days=1)))
        # A passing failure keeps the verdict and the day; another failure keeps the day.
        self.collector_write(channel, requests.Timeout('synthetic'))
        self.assertEqual((self.stored(channel)['channel_metadata_status'], self.since(channel)), ('channel_not_found', first))
        self.collector_write(channel, RuntimeError('synthetic'))
        self.assertEqual((self.stored(channel)['channel_metadata_status'], self.since(channel)), ('error', first))
        self.recheck_missing(channel, first + timedelta(days=2))
        self.assertEqual(self.since(channel), first)
        # A released run (HOT) restores the verdict and keeps the day; the reader view shows it.
        started = self.mark_running(channel)
        with self.admin.transaction():
            drain_hot_channel_metadata(self.tx, [{'channel_id': channel, 'control_epoch': 1, 'started_at': started.isoformat(),
                                                  'restore_status': 'channel_not_found'}])
        self.assertEqual(self.since(channel), first)
        readable = self.admin.execute('SELECT channel_metadata_not_found_since AS since FROM readable.channels WHERE channel_id=%s',
                                      (channel,)).fetchone()
        self.assertEqual(readable['since'], first)
        # YouTube returns it: the day is cleared. Missing again later starts a new day.
        returned = first + timedelta(days=2, hours=1)
        self.metadata_write(channel, metadata.channel_metadata_update_values({'snippet': {'title': 'Back'}, 'statistics': {}}, returned))
        self.assertEqual((self.stored(channel)['channel_metadata_status'], self.since(channel)), ('ok', None))
        later = returned + timedelta(hours=5)
        self.recheck_missing(channel, later)
        self.assertEqual(self.since(channel), later)

    def test_a_channel_missing_before_the_day_was_recorded_stays_unknown(self):
        # A row with the status and its code but no day: a recheck cannot invent one.
        channel = self.channel('channel_not_found', error='youtube_channel_not_found')
        self.recheck_missing(channel, datetime.now(timezone.utc).replace(microsecond=0))
        self.assertEqual((self.stored(channel)['channel_metadata_status'], self.since(channel)), ('channel_not_found', None))
        self.collector_write(channel, requests.Timeout('synthetic'))
        self.assertEqual((self.stored(channel)['channel_metadata_status'], self.since(channel)), ('channel_not_found', None))

    def test_passing_failure_keeps_profile_through_cleanup_and_reads(self):
        kept, failed = self.channel('ok'), self.channel('ok')
        self.collector_write(kept, requests.Timeout('synthetic'))
        self.collector_write(failed, RuntimeError('synthetic'))
        self.assertEqual(self.cleanup(), 1)
        row = self.stored(kept)
        self.assertEqual((row['channel_metadata_status'], row['channel_metadata_error'], row['title']),
                         ('ok', 'provider_unavailable', 'Profile'))
        self.assertEqual(self.readable_title(kept), 'Profile')
        # Contrast: a real failure still hides and removes the profile.
        self.assertEqual((self.stored(failed)['channel_metadata_status'], self.stored(failed)['title']), ('error', None))
        self.assertIsNone(self.readable_title(failed))
        snapshots = self.admin.execute('SELECT count(*) AS n FROM public.channel_snapshots WHERE channel_id=%s', (kept,)).fetchone()
        self.assertLessEqual(snapshots['n'], 1)

    def test_passing_failure_keeps_the_no_longer_on_youtube_verdict(self):
        gone = self.channel('channel_not_found', error='youtube_channel_not_found')
        self.collector_write(gone, requests.Timeout('synthetic'))
        row = self.stored(gone)
        self.assertEqual((row['channel_metadata_status'], row['channel_metadata_error'], row['channel_metadata_started_at']),
                         ('channel_not_found', 'youtube_channel_not_found', None))
        # Its profile is withdrawn at once. Only the label remains.
        self.assertEqual(self.cleanup(), 1)
        self.assertIsNone(self.stored(gone)['title'])

    def test_kept_profile_still_expires_after_29_days(self):
        old = self.channel('ok', checked_hours=700)
        self.collector_write(old, requests.ConnectionError('synthetic'))
        self.assertEqual(self.cleanup(), 1)
        self.assertIsNone(self.stored(old)['title'])

    def test_hot_release_restores_only_a_kept_verdict(self):
        confirmed, gone = self.channel('ok'), self.channel('channel_not_found', error='youtube_channel_not_found')
        failed = self.channel('error', error='channel_metadata_failed')
        markers = []
        for identity, restore in ((confirmed, 'ok'), (gone, 'channel_not_found'), (failed, None)):
            started = self.mark_running(identity)
            markers.append({'channel_id': identity, 'control_epoch': 1, 'started_at': started.isoformat(),
                            'restore_status': restore})
        with self.admin.transaction():
            self.assertEqual(drain_hot_channel_metadata(self.tx, markers)['released'], 3)
        self.assertEqual(self.stored(confirmed)['channel_metadata_status'], 'ok')
        self.assertEqual(self.stored(gone)['channel_metadata_status'], 'channel_not_found')
        self.assertIsNone(self.stored(failed)['channel_metadata_status'])
        self.assertEqual(self.cleanup(), 2)
        self.assertEqual((self.stored(confirmed)['title'], self.readable_title(confirmed)), ('Profile', 'Profile'))
        self.assertIsNone(self.stored(gone)['title'])

    def test_stale_run_recovery_reads_projection_and_keeps_confirmed_profile(self):
        confirmed = self.channel('ok', error='provider_unavailable')
        gone = self.channel('channel_not_found', error='youtube_channel_not_found')
        for identity in (confirmed, gone):
            self.admin.execute("UPDATE public.channels SET channel_metadata_status='running',"
                               "channel_metadata_started_at=clock_timestamp()-interval '1 hour' WHERE channel_id=%s", (identity,))
        database = Database(self.admin)
        context = SimpleNamespace(database=database, sources=SimpleNamespace(parse_dt=lambda value: value))
        for identity in (confirmed, gone):
            result = metadata.recover_stale_running_statuses(channel_id=identity, context=context)
            self.assertEqual((result['channel_metadata'], result['errors']), (1, 0), result)
        self.assertEqual((self.stored(confirmed)['channel_metadata_status'], self.stored(confirmed)['channel_metadata_error']),
                         ('ok', 'provider_unavailable'))
        self.assertEqual((self.stored(gone)['channel_metadata_status'], self.stored(gone)['channel_metadata_error']),
                         ('channel_not_found', 'youtube_channel_not_found'))
        self.assertEqual(self.cleanup(), 1)
        self.assertEqual(self.readable_title(confirmed), 'Profile')


if __name__ == '__main__':
    unittest.main()
