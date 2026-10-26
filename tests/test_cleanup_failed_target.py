"""A cleanup page that keeps failing does not block the other targets (audit D-03). No database."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
os.environ.setdefault('LIC_DEPLOYMENT_PROFILE', 'self-hosted')
from db.hot.hot_retention import CleanupPage, RetentionError  # noqa: E402
from hot_retention import FAILED_HOLD_FIRST_SECONDS, HotCleanupCycle  # noqa: E402

AT = '2026-10-06T00:00:00+00:00'
FLOOR = '2026-09-07T00:00:00+00:00'  # 29 days
TARGETS = ('video_counters', 'video_metadata', 'video_snapshots')


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Repository:
    """Every target finishes in one page except `broken`, which fails `failures` times."""

    def __init__(self, broken, failures):
        self.broken, self.failures, self.calls = broken, failures, []

    def cleanup_page(self, target, *, after_id=None, limit=100):
        self.calls.append((target, after_id))
        if target == self.broken and self.failures:
            self.failures -= 1
            raise RetentionError('cleanup_failed')
        return CleanupPage.parse(target, dict(as_of=AT, cutoff=FLOOR, scanned=0, deleted=0, scrubbed=0, deferred=0,
                                              complete=True, next_cursor=None), limit)


def cycle(failures):
    clock = Clock()
    repository = Repository('video_counters', failures)
    return HotCleanupCycle(repository, targets=TARGETS, clock=clock), repository, clock


def run(cycle_):
    return cycle_.run_slice(max_batches=10, max_seconds=60, clock=lambda: 0)


class FailedTargetTests(unittest.TestCase):
    def test_a_single_failure_is_retried_first_as_before(self):
        cycle_, repository, _ = cycle(failures=1)
        self.assertEqual(run(cycle_)['status'], 'error')
        cycle_.retry_failed()
        self.assertEqual(run(cycle_)['status'], 'scan_exhausted')
        self.assertEqual([target for target, _ in repository.calls], ['video_counters', *TARGETS])

    def test_a_repeated_failure_lets_the_other_targets_run_and_waits(self):
        cycle_, repository, clock = cycle(failures=10)
        run(cycle_); cycle_.retry_failed()
        run(cycle_); cycle_.retry_failed()            # second failure in a row: rotate
        summary = run(cycle_)
        self.assertEqual([target for target, _ in repository.calls][2:], ['video_metadata', 'video_snapshots'])
        self.assertEqual((summary['status'], summary['stop_reason']), ('in_progress', 'deferred_or_no_progress'))
        self.assertEqual(summary['targets_failing'], ['video_counters'])
        self.assertEqual(summary['targets_pending'], ['video_counters'])
        # Nothing is retried during the hold, then the page is read again with its cursor.
        clock.now += FAILED_HOLD_FIRST_SECONDS - 1
        run(cycle_)
        self.assertEqual(len(repository.calls), 4)
        clock.now += 1
        self.assertEqual(run(cycle_)['status'], 'error')
        self.assertEqual(repository.calls[-1], ('video_counters', None))

    def test_each_further_failure_gives_the_others_a_fresh_round(self):
        cycle_, repository, clock = cycle(failures=10)
        for _ in range(2):
            run(cycle_); cycle_.retry_failed()
        run(cycle_)
        clock.now += FAILED_HOLD_FIRST_SECONDS
        run(cycle_); cycle_.retry_failed()            # third failure: others again, longer hold
        run(cycle_)
        names = [target for target, _ in repository.calls]
        self.assertEqual(names[-2:], ['video_metadata', 'video_snapshots'])
        clock.now += 2 * FAILED_HOLD_FIRST_SECONDS - 1
        run(cycle_)
        self.assertEqual(len(repository.calls), len(names))

    def test_recovery_clears_the_failure_and_finishes_the_pass(self):
        cycle_, repository, clock = cycle(failures=2)
        for _ in range(2):
            run(cycle_); cycle_.retry_failed()
        run(cycle_)
        clock.now += FAILED_HOLD_FIRST_SECONDS
        summary = run(cycle_)
        self.assertEqual((summary['status'], summary['targets_failing']), ('scan_exhausted', []))

    def test_reset_forgets_failures_and_holds(self):
        cycle_, _, _ = cycle(failures=10)
        for _ in range(2):
            run(cycle_); cycle_.retry_failed()
        cycle_.reset()
        self.assertEqual(cycle_.summary()['targets_failing'], [])


class RuntimeRepository:
    def __init__(self, clock):
        self.clock = clock

    def contract(self):
        return {'version': 1, 'schema_ready': True, 'as_of': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()}

    def startup_expiry(self, *, cutoff, strict_entry=False):
        return {'as_of': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), 'expired': False}


class ClockRepository(Repository):
    """Pages carry the controller's clock, as the database's would."""

    def __init__(self, clock, broken, failures):
        super().__init__(broken, failures)
        self.clock = clock

    def cleanup_page(self, target, *, after_id=None, limit=100):
        self.calls.append((target, after_id))
        if target == self.broken and self.failures:
            self.failures -= 1
            raise RetentionError('cleanup_failed')
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        return CleanupPage.parse(target, dict(as_of=now.isoformat(), cutoff=(now - timedelta(hours=696)).isoformat(),
                                              scanned=0, deleted=0, scrubbed=0, deferred=0, complete=True,
                                              next_cursor=None), limit)


class ControllerTests(unittest.TestCase):
    def test_other_targets_keep_being_cleaned_while_one_keeps_failing(self):
        from hot_runtime import HotRuntimeController
        clock = Clock()
        runtime = HotRuntimeController(RuntimeRepository(clock), local_cleanup=lambda: {'status': 'current'},
                                       clear_caches=lambda: None, clock=clock, monotonic=clock)
        repository = ClockRepository(clock, 'video_counters', failures=10 ** 6)
        runtime.cycle = HotCleanupCycle(repository, targets=TARGETS, clock=clock)
        failing_seen = False
        for _ in range(1800):                      # half an hour in one-second heartbeats
            clock.now += 1
            runtime.step()
            failing_seen = failing_seen or 'video_counters' in runtime.status()['scan'].get('targets_failing', ())
        names = [target for target, _ in repository.calls]
        # Before the fix only the broken target was ever called.
        self.assertGreater(names.count('video_metadata'), 3)
        self.assertGreater(names.count('video_snapshots'), 3)
        # Its retries are spaced by growing holds, not repeated every round.
        self.assertLess(names.count('video_counters'), 15)
        self.assertTrue(failing_seen)


if __name__ == '__main__':
    unittest.main()
