"""A collection pause survives clean pages before the held target is revisited."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.hot.hot_retention import CleanupPage
from hot_retention import HotCleanupCycle
from hot_runtime import HotRuntimeController, HotRuntimeUnavailable, PAUSE_AFTER_PASSES

DAY = 86400


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Pages:
    def __init__(self, finding='expired', *, exhausted=True):
        self.wall, self.mono = 1_800_000_000.0, 500.0
        self.finding, self.exhausted, self.held = finding, exhausted, True
        self.calls = []

    def contract(self):
        return {'version': 1, 'schema_ready': True, 'as_of': iso(self.wall)}

    def startup_expiry(self, *, cutoff, strict_entry=False):
        # This lock appears during normal operation, after startup admission.
        return {'as_of': iso(self.wall), 'expired': False}

    def cleanup_page(self, target, *, after_id=None, limit=100):
        self.calls.append(target)
        blocked = target == 'video_metadata' and self.held
        raw = {'as_of': iso(self.wall), 'cutoff': iso(self.wall - 29 * DAY),
               'scanned': 1, 'deleted': 0, 'scrubbed': int(target == 'video_metadata' and not self.held),
               'deferred': 1 if target == 'video_counters' or blocked else 0,
               # An exhausted candidate page may still contain leased/skipped rows.
               'complete': self.exhausted or not blocked, 'next_cursor': None,
               'oldest_remaining_expired_source_at': iso(self.wall - 29 * DAY - 60)
                   if blocked and self.finding == 'expired' else None,
               'unknown_expiry_remaining': int(blocked and self.finding == 'unknown'),
               'invalid_clock_remaining': int(blocked and self.finding == 'invalid')}
        return CleanupPage.parse(target, raw, limit)


class Time:
    def status(self):
        return {'state': 'unverified'}


def make_runtime(finding='expired', *, exhausted=True):
    repository = Pages(finding, exhausted=exhausted)
    runtime = HotRuntimeController(repository, local_cleanup=lambda: {'status': 'current'},
        clear_caches=lambda: None, clock=lambda: repository.wall, monotonic=lambda: repository.mono,
        time_trust=Time())
    runtime.cycle = HotCleanupCycle(repository, targets=('video_counters', 'video_metadata'))
    runtime.initialize()
    return runtime, repository


def next_page(runtime, repository):
    before = len(repository.calls)
    for _ in range(90):
        repository.wall += 1
        repository.mono += 1
        runtime.step()
        if len(repository.calls) > before:
            return repository.calls[-1], runtime.status()
    raise AssertionError('No cleanup page was attempted within the scheduled interval.')


class PriorityLatchTests(unittest.TestCase):
    def test_complete_pages_keep_pause_until_a_whole_pass_clears_the_held_target(self):
        for finding in ('expired', 'invalid'):
            with self.subTest(finding=finding):
                runtime, repository = make_runtime(finding)
                for number in range(PAUSE_AFTER_PASSES):
                    self.assertEqual(next_page(runtime, repository)[0], 'video_counters')
                    target, status = next_page(runtime, repository)
                    self.assertEqual(target, 'video_metadata')
                    self.assertEqual(status['collection_pause_requested'], number + 1 >= PAUSE_AFTER_PASSES)
                runtime.require_ready()  # A held row never closes API reads globally.
                runtime.require_database_ready()
                with self.assertRaises(HotRuntimeUnavailable):
                    runtime.require_collection_ready()
                target, status = next_page(runtime, repository)
                self.assertEqual(target, 'video_counters')
                self.assertTrue(status['collection_pause_requested'], 'A fresh target cannot clear a different target\'s held row.')
                self.assertTrue(next_page(runtime, repository)[1]['collection_pause_requested'])
                repository.held = False
                self.assertTrue(next_page(runtime, repository)[1]['collection_pause_requested'], 'Keep the pause until the formerly held target is checked.')
                target, status = next_page(runtime, repository)
                self.assertEqual(target, 'video_metadata')
                self.assertFalse(status['collection_pause_requested'])
                runtime.require_collection_ready()

    def test_fresh_deferred_rows_do_not_request_a_pause(self):
        for finding in ('fresh', 'unknown'):
            with self.subTest(finding=finding):
                runtime, repository = make_runtime(finding)
                for _ in range(2 * (PAUSE_AFTER_PASSES + 1)):
                    _, status = next_page(runtime, repository)
                    self.assertFalse(status['collection_pause_requested'])
                    self.assertTrue(status['api_read']['allowed'])

    def test_stalled_cursor_rounds_also_request_and_eventually_release_pause(self):
        runtime, repository = make_runtime(exhausted=False)
        for _ in range(PAUSE_AFTER_PASSES + 1):
            next_page(runtime, repository)
        self.assertTrue(runtime.status()['collection_pause_requested'])
        repository.held = False
        next_page(runtime, repository)
        # The cycle remembers previously held rows; the next clean full pass proves release.
        self.assertTrue(runtime.status()['collection_pause_requested'])
        next_page(runtime, repository)
        self.assertTrue(runtime.status()['collection_pause_requested'])
        next_page(runtime, repository)
        self.assertFalse(runtime.status()['collection_pause_requested'])


if __name__ == '__main__':
    unittest.main()
