"""Cleanup keeps progressing under failures; reads stay protected.

Offline: the real HotRuntimeController and HotCleanupCycle over a fake maintenance
repository with controllable clocks. No database, provider or files.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
os.environ.setdefault('LIC_DEPLOYMENT_PROFILE', 'self-hosted')
import hot_retention  # noqa: E402
import hot_runtime  # noqa: E402
from db.base.postgres import DatabaseServerError, DatabaseUnavailable  # noqa: E402
from db.hot import hot_retention as retention_db  # noqa: E402
from db.hot.hot_retention import CleanupPage, HotRetentionRepository, RetentionError  # noqa: E402
from hot_retention import HotCleanupCycle  # noqa: E402
from hot_runtime import HotRuntimeController  # noqa: E402

FAULT_AFTER = hot_runtime.FAULT_AFTER_FAILURES
PAGE_MIN = hot_retention.PAGE_LIMIT_MIN
GROW_AFTER = hot_retention.PAGE_GROW_AFTER
TARGETS = ('video_snapshots', 'video_counters', 'channel_snapshots')


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def error(cause, code='cleanup_outcome_unknown'):
    return RetentionError(code, cause=cause)


class Clocks:
    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 500.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class Time:
    def __init__(self):
        self.state = 'trusted'

    def status(self):
        return {'state': self.state}

    def probe_if_due(self):
        pass


class Store:
    """Expired rows per target, removed oldest first; failures on demand."""

    def __init__(self, clocks, rows=None, walk=0):
        self.clocks = clocks
        self.rows = dict(rows or {target: 0 for target in TARGETS})
        self.walk = walk            # rows of a target that is walked by cursor
        self.fail = {}              # target -> cause, raised on every page
        self.page_hook = None
        self.calls = []
        self.contract_error = None
        self.expiry_error = None
        self.expiry_calls = []

    def contract(self):
        if self.contract_error:
            raise self.contract_error
        return {'version': 1, 'schema_ready': True, 'as_of': iso(self.clocks.wall)}

    def startup_expiry(self, *, cutoff, strict_entry=False):
        self.expiry_calls.append(strict_entry)
        if self.expiry_error:
            raise self.expiry_error
        return {'as_of': iso(self.clocks.wall), 'expired': any(self.rows.values())}

    def cleanup_page(self, target, *, after_id=None, limit=100):
        self.calls.append((target, after_id, limit))
        if self.page_hook:
            self.page_hook(target)
        if target in self.fail:
            raise error(self.fail[target])
        now = self.clocks.wall
        if target == 'inventory':
            position = int(after_id or 0) + limit
            complete = position >= self.walk
            value = dict(scanned=limit, deleted=0, complete=complete, next_cursor=None if complete else str(position))
        else:
            taken = min(limit, self.rows.get(target, 0))
            self.rows[target] = self.rows.get(target, 0) - taken
            value = dict(scanned=taken, deleted=taken, complete=taken < limit, next_cursor=None)
        return CleanupPage.parse(target, dict(value, as_of=iso(now), cutoff=iso(now - 696 * 3600), scrubbed=0,
                                              deferred=0), limit)

    def close(self):
        pass


def controller(rows=None, targets=TARGETS, walk=0):
    clocks = Clocks()
    store = Store(clocks, rows, walk)
    runtime = HotRuntimeController(store, local_cleanup=lambda: {'status': 'current'}, clear_caches=lambda: None,
                                   clock=lambda: clocks.wall, monotonic=lambda: clocks.mono, time_trust=Time())
    runtime.cycle = HotCleanupCycle(store, targets=targets, clock=lambda: clocks.mono)
    return runtime, store, clocks


def run(runtime, clocks, seconds, *, every=1.0, record=None):
    """Heartbeats short enough not to look like a suspended process."""
    for _ in range(int(seconds / every)):
        clocks.advance(every)
        runtime.step()
        if record is not None:
            record.append(runtime.status()['api_read']['allowed'])
    return runtime.status()


def flips(record):
    """Open-to-closed transitions: reads that opened early and closed again."""
    return sum(1 for before, after in zip(record, record[1:]) if before and not after)


class CauseTests(unittest.TestCase):
    def test_database_errors_become_fixed_causes_without_text(self):
        classify = getattr(retention_db, 'failure_cause', None)
        self.assertIsNotNone(classify, 'failure causes are named')
        cases = [
            (DatabaseServerError('database_operation_failed', code='57014'), 'statement_timeout'),
            (DatabaseServerError('database_operation_failed', code='55P03'), 'lock_timeout'),
            (DatabaseServerError('database_operation_failed', code='40P01'), 'transaction_conflict'),
            (DatabaseServerError('database_operation_failed', code='40001'), 'transaction_conflict'),
            (DatabaseServerError('database_operation_failed', code='53300'), 'insufficient_resources'),
            (DatabaseServerError('database_operation_failed', code='XX000'), 'query_failed'),
            (DatabaseUnavailable('database_pool_unavailable', reason='pool_timeout'), 'pool_unavailable'),
            (DatabaseUnavailable('database_connection_failed', code='57P01', reason='connection_failed'),
             'connection_terminated'),
            (DatabaseUnavailable('database_connection_failed', code='57P03', reason='connection_failed'),
             'server_restarting'),
            (DatabaseUnavailable('database_connection_failed', reason='connection_failed'), 'connection_lost'),
            (ValueError('row 42 value secret'), 'unknown'),
        ]
        for raised, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify(raised), expected)

    def test_a_failed_page_keeps_its_cause_and_no_database_text(self):
        class Database:
            def cleanup_hot_evidence(self, **kwargs):
                raise DatabaseServerError('canceling statement for video Abc123', code='57014')
        with self.assertRaises(RetentionError) as caught:
            HotRetentionRepository(Database()).cleanup_page('video_snapshots')
        self.assertEqual(caught.exception.code, 'cleanup_outcome_unknown')
        self.assertEqual(getattr(caught.exception, 'cause', None), 'statement_timeout')
        self.assertNotIn('Abc123', str(caught.exception))

    def test_a_contract_check_that_times_out_is_not_a_schema_error(self):
        from db.hot.hot_runtime import MaintenanceRepository
        import db.hot.hot_runtime as maintenance

        def slow(database):
            raise DatabaseServerError('database_operation_failed', code='57014')

        def broken(database):
            raise DatabaseServerError('schema_release_mismatch', code='P0001')
        original = maintenance.get_hot_runtime_contract
        try:
            for function, code, cause in ((slow, 'runtime_contract_unavailable', 'statement_timeout'),
                                          (broken, 'runtime_contract_schema_invalid', 'query_failed')):
                maintenance.get_hot_runtime_contract = function
                with self.subTest(code=code), self.assertRaises(RetentionError) as caught:
                    MaintenanceRepository(object()).contract()
                self.assertEqual((caught.exception.code, getattr(caught.exception, 'cause', None)), (code, cause))
        finally:
            maintenance.get_hot_runtime_contract = original


def failing(store, target, times, cause):
    """The next `times` pages of `target` fail with `cause`."""
    def hook(name):
        if name == target and hook.left:
            hook.left -= 1
            raise error(cause)
    hook.left = times
    store.page_hook = hook
    return hook


class BatchSizeTests(unittest.TestCase):
    def cycle(self, targets, times, cause='statement_timeout'):
        clocks = Clocks()
        store = Store(clocks, {target: 1000 for target in targets})
        failing(store, 'video_snapshots', times, cause)
        return HotCleanupCycle(store, targets=targets, clock=lambda: clocks.mono), store, clocks

    def slice(self, cycle, clocks):
        clocks.mono += 1000                     # past any hold of a failing target
        return cycle.run_slice(max_batches=1, max_seconds=60, clock=lambda: 0)

    def test_a_page_cancelled_for_time_is_retried_smaller_with_the_same_cursor(self):
        cycle, store, clocks = self.cycle(('video_snapshots',), 5)
        for _ in range(5):
            self.assertEqual(self.slice(cycle, clocks)['status'], 'error')
            cycle.retry_failed()
        self.assertEqual([limit for _, _, limit in store.calls], [100, 50, 25, 12, max(PAGE_MIN, 6)])
        self.assertTrue(all(after is None for _, after, _ in store.calls), 'the same page is read again')
        self.assertEqual(cycle.summary().get('page_limits'), {'video_snapshots': PAGE_MIN})
        self.slice(cycle, clocks)
        self.assertEqual(store.calls[-1][2], PAGE_MIN)
        self.assertEqual(store.rows['video_snapshots'], 1000 - PAGE_MIN)

    def test_only_the_cancelled_target_shrinks_and_it_grows_back(self):
        cycle, store, clocks = self.cycle(('video_snapshots', 'video_counters'), 1)
        self.slice(cycle, clocks)
        cycle.retry_failed()
        for _ in range(2 * GROW_AFTER + 2):
            self.slice(cycle, clocks)
        snapshots = [limit for target, _, limit in store.calls if target == 'video_snapshots']
        counters = {limit for target, _, limit in store.calls if target == 'video_counters'}
        self.assertEqual(snapshots[:GROW_AFTER + 1], [100] + [50] * GROW_AFTER)
        self.assertEqual(snapshots[-1], 100, 'it grows back after quick pages')
        self.assertEqual(counters, {100})

    def test_a_lock_wait_does_not_shrink_the_page(self):
        cycle, store, clocks = self.cycle(('video_snapshots',), 1, 'lock_timeout')
        self.slice(cycle, clocks)
        cycle.retry_failed()
        self.slice(cycle, clocks)
        self.assertEqual([limit for _, _, limit in store.calls], [100, 100])


class ResumeTests(unittest.TestCase):
    def test_a_pause_keeps_the_cursor_instead_of_restarting_the_pass(self):
        runtime, store, clocks = controller(targets=('inventory',), walk=10_000)
        runtime.initialize()
        run(runtime, clocks, 5)
        cursor = store.calls[-1][1]
        self.assertIsNotNone(cursor)
        clocks.advance(30)                       # a real stall, past the watchdog
        runtime.step()
        run(runtime, clocks, 1)
        resumed = [after for _, after, _ in store.calls[-2:]]
        self.assertNotIn(None, resumed, 'the walk resumed at its cursor')

    def test_a_slow_failing_step_is_not_taken_for_a_suspended_process(self):
        runtime, store, clocks = controller()
        runtime.initialize()
        store.rows['video_snapshots'] = 10_000   # expired while running: no catch-up
        run(runtime, clocks, 61)

        def slow(target):
            if target == 'video_snapshots' and slow.left:
                slow.left -= 1
                clocks.advance(25)               # the database spent 25 s on this page
                raise error('statement_timeout')
        slow.left = 1
        store.page_hook = slow
        record = []
        run(runtime, clocks, 15, record=record)
        self.assertNotEqual(runtime.status()['reason'], 'clock_changed')
        self.assertTrue(all(record), 'reads stayed open: no false watchdog')


class RetryAndFaultTests(unittest.TestCase):
    def test_retry_pauses_are_short_then_slower_and_bounded(self):
        delay = getattr(hot_runtime, 'retry_delay', None)
        self.assertIsNotNone(delay)
        self.assertEqual([delay(n) for n in range(1, 10)], [5, 10, 20, 40, 120, 240, 480, 600, 600])

    def test_a_target_that_keeps_failing_becomes_a_fault_with_its_cause(self):
        runtime, store, clocks = controller({'video_snapshots': 50, 'video_counters': 5_000, 'channel_snapshots': 0})
        runtime.initialize()
        store.fail['video_snapshots'] = 'lock_timeout'
        before = store.rows['video_counters']
        seen = []
        for _ in range(900):
            clocks.advance(1)
            runtime.step()
            seen.append(bool(runtime.status().get('cleanup_faults')))
        status = runtime.status()
        faults = status.get('cleanup_faults') or []
        self.assertIn({'stage': 'page', 'target': 'video_snapshots', 'cause': 'lock_timeout'},
                      [{key: item[key] for key in ('stage', 'target', 'cause')} for item in faults])
        self.assertTrue(status['maintenance_failed'])
        self.assertLess(store.rows['video_counters'], before, 'other targets keep being cleaned')
        self.assertEqual(set(faults[0]), {'stage', 'target', 'cause', 'failures', 'since'})
        self.assertGreater(seen.index(True), 60, 'not a fault at the first failure')
        attempts = [call for call in store.calls if call[0] == 'video_snapshots']
        self.assertLess(len(attempts), 20, 'it keeps being retried, at a slower pace')
        del store.fail['video_snapshots']
        status = run(runtime, clocks, 700)
        self.assertEqual(status['cleanup_faults'], [])
        self.assertFalse(status['maintenance_failed'])
        self.assertEqual(store.rows['video_snapshots'], 0)

    def test_a_step_whose_cause_keeps_changing_is_a_fault_too(self):
        runtime, store, clocks = controller({'video_snapshots': 50, 'video_counters': 5_000, 'channel_snapshots': 0})
        runtime.initialize()
        causes = iter(['lock_timeout', 'statement_timeout'] * 50)

        def hook(target):
            if target == 'video_snapshots':
                raise error(next(causes))
        store.page_hook = hook
        status = run(runtime, clocks, 3000)
        faults = [item for item in status.get('cleanup_faults') or [] if item['target'] == 'video_snapshots']
        self.assertEqual(len(faults), 1, 'no streak of one cause, still a fault')
        self.assertIn(faults[0]['cause'], ('lock_timeout', 'statement_timeout'))
        self.assertGreaterEqual(faults[0]['failures'], 2 * FAULT_AFTER)
        self.assertTrue(status['api_read']['allowed'])

    def test_every_page_failing_is_a_fault_too(self):
        runtime, store, clocks = controller({'video_snapshots': 500, 'video_counters': 500, 'channel_snapshots': 500})
        runtime.initialize()
        for target in TARGETS:
            store.fail[target] = 'statement_timeout'
        status = run(runtime, clocks, 400)
        causes = {(item['stage'], item['cause']) for item in status.get('cleanup_faults') or []}
        self.assertIn(('page', 'statement_timeout'), causes)


class GateTests(unittest.TestCase):
    def test_a_transient_failure_during_catchup_never_opens_reads_early(self):
        runtime, store, clocks = controller({'video_snapshots': 3_000, 'video_counters': 0, 'channel_snapshots': 0})
        runtime.initialize()
        self.assertEqual(runtime.status()['reason'], 'startup_cleanup')
        failing(store, 'video_snapshots', 3, 'connection_terminated')
        record = []
        status = run(runtime, clocks, 200, record=record)
        self.assertTrue(status['api_read']['allowed'])
        self.assertTrue(status['startup_notice']['complete'])
        self.assertEqual(store.rows['video_snapshots'], 0)
        self.assertEqual(flips(record), 0, 'no open-close-open while catch-up retries')
        self.assertFalse(record[0], 'reads waited for catch-up after the failure')

    def test_a_persistent_failure_opens_reads_only_under_verified_protection(self):
        runtime, store, clocks = controller({'video_snapshots': 3_000, 'video_counters': 2_000, 'channel_snapshots': 0})
        runtime.initialize()
        store.fail['video_snapshots'] = 'lock_timeout'
        record = []
        status = run(runtime, clocks, 600, record=record)
        self.assertTrue(status['api_read']['allowed'])
        self.assertFalse(status['startup_notice']['complete'])
        self.assertTrue(status['cleanup_faults'])
        self.assertEqual(flips(record), 0)
        self.assertFalse(any(record[:60]), 'not opened by the first failures')
        # Protection that cannot be verified closes YouTube data again; user work stays.
        runtime._clock_invalid = True
        self.assertEqual(runtime.status()['api_read'], {'allowed': False, 'reason': 'time_mismatch'})
        self.assertTrue(runtime.status()['user_work']['allowed'])
        runtime._clock_invalid = False
        store.contract_error = RetentionError('runtime_contract_schema_invalid')
        status = run(runtime, clocks, 90)        # verified again while cleanup runs
        self.assertEqual(status['api_read']['reason'], 'schema_invalid')

    def test_cleanup_runs_when_the_startup_check_keeps_timing_out(self):
        runtime, store, clocks = controller({'video_snapshots': 2_000, 'video_counters': 0, 'channel_snapshots': 0})
        store.expiry_error = error('statement_timeout', 'startup_expiry_failed')
        runtime.initialize()
        self.assertEqual(runtime.status()['api_read']['reason'], 'validation_pending')
        record = []
        status = run(runtime, clocks, 300, record=record)
        self.assertEqual(store.rows['video_snapshots'], 0, 'cleanup did not wait for the startup check')
        self.assertIn(('startup_check', 'statement_timeout'),
                      {(item['stage'], item['cause']) for item in status.get('cleanup_faults') or []})
        self.assertTrue(status['api_read']['allowed'])
        self.assertFalse(any(record[:30]), 'reads waited while the check was retried')
        self.assertEqual(flips(record), 0)
        checks = len(store.expiry_calls)
        self.assertGreater(checks, 3)
        self.assertLess(checks, 15, 'the check is retried on its own, spaced schedule')
        store.expiry_error = None
        status = run(runtime, clocks, 300)
        self.assertEqual(status['cleanup_faults'], [])
        self.assertTrue(status['api_read']['allowed'])

    def test_a_failing_end_of_catchup_check_does_not_stop_cleanup(self):
        runtime, store, clocks = controller({'video_snapshots': 500, 'video_counters': 0, 'channel_snapshots': 0})
        runtime.initialize()
        self.assertEqual(runtime.status()['reason'], 'startup_cleanup')
        store.expiry_error = error('statement_timeout', 'startup_expiry_failed')
        store.rows['video_counters'] = 300
        status = run(runtime, clocks, 120)
        self.assertEqual(store.rows['video_counters'], 0)
        self.assertEqual(status['api_read']['reason'], 'startup_cleanup')
        self.assertFalse(status['maintenance_failed'] and not status.get('cleanup_faults'),
                         'a failing recheck is not reported as a failed cleanup step')
        store.expiry_error = None
        status = run(runtime, clocks, 200)
        self.assertTrue(status['api_read']['allowed'])
        self.assertTrue(status['startup_notice']['complete'])

    def test_a_database_outage_closes_reads_and_cleanup_resumes(self):
        runtime, store, clocks = controller({'video_snapshots': 200_000, 'video_counters': 0, 'channel_snapshots': 0})
        runtime.initialize()
        run(runtime, clocks, 2)
        left = store.rows['video_snapshots']
        store.fail['video_snapshots'] = 'connection_lost'
        store.contract_error = RetentionError('runtime_contract_unavailable')
        status = run(runtime, clocks, 40)
        self.assertEqual(status['api_read']['reason'], 'database_unavailable')
        del store.fail['video_snapshots']
        store.contract_error = None
        status = run(runtime, clocks, 200)
        self.assertEqual(store.rows['video_snapshots'], 0)
        self.assertGreater(left, 0)
        self.assertTrue(status['api_read']['allowed'])
        self.assertTrue(status['startup_notice']['complete'])


if __name__ == '__main__':
    unittest.main()
