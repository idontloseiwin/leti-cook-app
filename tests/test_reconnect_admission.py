"""Real temporary SQLite tests; no PostgreSQL, Docker, network or provider calls."""
from contextlib import closing
import multiprocessing
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.user_work.reconnect_admission import ChannelAddAdmission, ChannelAddAdmissionUnavailable
from quota_guards import CHANNEL_ADD_WINDOWS, RateLimiter

INSTALLATION = 'cccccccc-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


class Clock:
    def __init__(self, now=100000.0):
        self.now = now

    def __call__(self):
        return self.now


def contender(path, ready, start, result):
    owner = ChannelAddAdmission(path, INSTALLATION, clock=Clock())
    ready.put(True)
    if not start.wait(10):
        result.put(('error', 'start_timeout'))
        return
    try:
        result.put(('ok', [owner.acquire() for _ in range(10)]))
    except ChannelAddAdmissionUnavailable:
        result.put(('error', 'unavailable'))


class ReconnectAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'channel-add.sqlite3'
        self.clock = Clock()
        self.owner = ChannelAddAdmission.initialize(self.path, INSTALLATION, clock=self.clock)

    def count(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT count(*) FROM channel_add_admissions').fetchone()[0]

    def test_existing_rolling_policy_matches_at_boundaries_and_across_instances(self):
        reference = RateLimiter(CHANNEL_ADD_WINDOWS, clock=self.clock)
        second = ChannelAddAdmission(self.path, INSTALLATION, clock=self.clock)
        generator = random.Random(42)
        for i in range(400):
            self.clock.now += generator.choice((0, 0, 0, 0.125, 1, 5, 60, 600))
            self.assertEqual((self.owner if i % 2 else second).acquire(), reference.acquire(), i)
        self.assertLessEqual(self.count(), 300)

    def test_twenty_per_minute_and_refusal_does_not_append(self):
        self.assertEqual([self.owner.acquire() for _ in range(20)], [0] * 20)
        for _ in range(3):
            self.assertEqual(self.owner.acquire(), 60)
        self.assertEqual(self.count(), 20)
        self.clock.now += 59.5
        self.assertEqual(self.owner.acquire(), 1)
        self.clock.now += 0.5
        self.assertEqual(self.owner.acquire(), 0)

    def test_daily_window_persists_restart_and_exact_expiry(self):
        for minute in range(15):
            self.clock.now = 100000 + minute * 60
            self.assertEqual([self.owner.acquire() for _ in range(20)], [0] * 20)
        self.clock.now = 100000 + 15 * 60
        restarted = ChannelAddAdmission(self.path, INSTALLATION, clock=self.clock)
        self.assertEqual(restarted.acquire(), 86400 - 900)
        self.assertEqual(self.count(), 300)
        self.clock.now = 100000 + 86400
        self.assertEqual(restarted.acquire(), 0)
        self.assertEqual(self.count(), 281)

    def test_clock_rollback_does_not_erase_admission_history(self):
        for _ in range(20):
            self.owner.acquire()
        self.clock.now -= 3600
        self.assertEqual(ChannelAddAdmission(self.path, INSTALLATION, clock=self.clock).acquire(), 60)
        self.clock.now = 100060
        self.assertEqual(self.owner.acquire(), 0)

    def test_missing_or_foreign_or_corrupt_store_never_resets(self):
        other = ChannelAddAdmission(self.path, 'bbbbbbbb-aaaa-4aaa-8aaa-aaaaaaaaaaaa', clock=self.clock)
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            other.acquire()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            self.owner.acquire()
        self.path.unlink()
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            self.owner.acquire()
        self.assertFalse(self.path.exists())

    def test_double_initialization_cannot_reset_limits(self):
        self.owner.acquire()
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            ChannelAddAdmission.initialize(self.path, INSTALLATION, clock=self.clock)
        self.assertEqual(self.count(), 1)

    def test_database_busy_is_bounded_and_fails_closed(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with self.assertRaises(ChannelAddAdmissionUnavailable):
                self.owner.acquire()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
        self.assertEqual(self.count(), 0)

    def test_unexpected_trigger_and_wal_mode_fail_closed(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TRIGGER unexpected AFTER INSERT ON channel_add_admissions BEGIN DELETE FROM channel_add_admissions; END')
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            self.owner.acquire()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TRIGGER unexpected')
            db.execute('PRAGMA journal_mode=WAL')
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            self.owner.acquire()

    def test_invalid_clock_never_admits(self):
        for value in (True, float('nan'), float('inf'), -1, '100000'):
            with self.subTest(value=value):
                self.clock.now = value
                with self.assertRaises(ChannelAddAdmissionUnavailable):
                    self.owner.acquire()
        self.assertEqual(self.count(), 0)

    def test_physical_and_row_bounds(self):
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO channel_add_admissions VALUES(300,100000)')
        self.assertLessEqual(self.path.stat().st_size, 256 * 1024)
        self.assertFalse(Path(str(self.path) + '-wal').exists())
        self.assertFalse(Path(str(self.path) + '-journal').exists())

    def test_hardlinked_database_is_refused_without_changing_original(self):
        alias = self.path.with_name('alias.sqlite3')
        os.link(self.path, alias)
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            self.owner.acquire()
        self.assertEqual(self.count(), 0)
        alias.unlink()
        self.assertEqual(self.owner.acquire(), 0)

    def test_sidecar_symlink_is_refused(self):
        sidecar = self.path.with_name(self.path.name + '-journal')
        try:
            sidecar.symlink_to(self.path.with_name('absent-target'))
        except OSError:
            self.skipTest('Symlink creation privilege is unavailable on this host')
        with self.assertRaises(ChannelAddAdmissionUnavailable):
            self.owner.acquire()
        self.assertFalse(sidecar.resolve().exists())
        sidecar.unlink()

    def test_four_processes_share_one_admission_window(self):
        context = multiprocessing.get_context('spawn')
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        children = [context.Process(target=contender, args=(str(self.path), ready, start, results)) for _ in range(4)]
        try:
            for child in children:
                child.start()
            for _ in children:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            batches = [results.get(timeout=10) for _ in children]
            for child in children:
                child.join(10)
                self.assertFalse(child.is_alive())
                self.assertEqual(child.exitcode, 0)
            self.assertTrue(all(kind == 'ok' for kind, _ in batches), batches)
            values = [value for _, batch in batches for value in batch]
            self.assertEqual(values.count(0), 20)
            self.assertEqual(values.count(60), 20)
            self.assertEqual(self.count(), 20)
        finally:
            # Only child processes created by this test are ever terminated.
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            ready.close()
            results.close()


if __name__ == '__main__':
    unittest.main()
