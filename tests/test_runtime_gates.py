"""R08 separate database, API reading and collection gates. Offline."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
os.environ.setdefault('LIC_DEPLOYMENT_PROFILE', 'self-hosted')
from hot_runtime import HotRuntimeController, HotRuntimeUnavailable, RemoteAdmission, HotAdmissionConfig, signed_status
from db.hot.hot_retention import RetentionError
from hot_storage import HOT_DATA_WINDOW, utc_instant
DAY = 86400
def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
class Clocks:
    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 500.0
    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds
class Time:
    def __init__(self, state='unverified'):
        self.state = state
    def status(self):
        return {'state': self.state}
    def probe_if_due(self):
        pass
class Repository:
    def __init__(self, clocks, expired=False):
        self.clocks, self.expired, self.error = clocks, bool(expired), None
        self.contract_calls = self.expiry_calls = 0
        self.sources = None
        self.cutoffs = []
    def contract(self):
        self.contract_calls += 1
        if self.error:
            raise RetentionError(self.error)
        return {'version': 1, 'schema_ready': True, 'as_of': iso(self.clocks.wall)}
    def startup_expiry(self, *, cutoff, strict_entry=False):
        self.expiry_calls += 1
        cutoff = utc_instant(cutoff)
        self.cutoffs.append((cutoff, strict_entry))
        expired = self.expired if self.sources is None else any(stamp < cutoff for stamp in self.sources)
        return {'as_of': iso(self.clocks.wall), 'expired': expired}
    def close(self):
        pass
class Cycle:
    def __init__(self, clocks, pages=3, **findings):
        self.clocks, self.pages, self.findings, self.done = clocks, pages, findings, 0
        self.error = False
    def summary(self):
        return {'status': 'error' if self.error else 'in_progress', 'batches': self.done}
    def reset(self):
        self.done = 0
    def retry_failed(self):
        self.error = False
    def run_slice(self, **kw):
        self.done += 1
        if self.findings.get('failure'):
            self.error = True
            return {'status': 'error', 'pages': [], 'failed_target': 'video_snapshots'}
        return {
            'status': 'scan_exhausted' if self.done >= self.pages else 'in_progress', 'batches': self.done,
            'counts': {'scanned': self.done, 'deleted': self.findings.get('deleted', 0), 'scrubbed': 0,
                       'deferred': self.findings.get('deferred', 0)},
            'targets_not_visited': [], 'targets_pending': [], 'targets_with_observed_deferrals': [],
            'invalid_clocks_observed': self.findings.get('invalid_clocks', 0),
            'unknown_expiry_observed': self.findings.get('unknown', 0),
            'oldest_observed_expired_source_at': self.findings.get('expired'),
            'pages': [{'as_of': iso(self.clocks.wall)}]}
def runtime(*, expired=False, pages=3, **findings):
    clocks = Clocks()
    controller = HotRuntimeController(Repository(clocks, expired), local_cleanup=lambda: {'status': 'current'},
        clear_caches=lambda: None, clock=lambda: clocks.wall, monotonic=lambda: clocks.mono, time_trust=Time())
    controller.cycle = Cycle(clocks, pages, **findings)
    return controller, clocks
def step(controller, clocks, times=1):
    for _ in range(times):
        clocks.advance(1)
        controller.step()
    return controller.status()
class NarrowGateTests(unittest.TestCase):
    def test_normal_start_without_cleanup_or_database_age(self):
        c, _ = runtime()
        c.initialize()
        self.assertTrue(all(c.status()[k]['allowed'] for k in ('api_read','collection','user_work')))
        self.assertEqual(c.cycle.done, 0)
        self.assertNotIn('young_database', c.status())
    def test_expired_record_only_closes_api_until_confirmed_clean(self):
        c, clocks = runtime(expired=True, deleted=1)
        c.initialize()
        self.assertEqual(c.status()['api_read'], {'allowed':False,'reason':'startup_cleanup'})
        c.require_collection_ready()
        c.require_database_ready()
        step(c, clocks, 2)
        c.repository.expired = False
        s = step(c, clocks)
        self.assertTrue(s['api_read']['allowed'])
        self.assertEqual(s['startup_notice']['records'], 1)
        self.assertTrue(s['startup_notice']['complete'])
        self.assertEqual(c.status()['startup_notice']['id'], s['startup_notice']['id'])
    def test_startup_grace_ignores_normal_lag_and_exactly_one_hour(self):
        for expired_seconds, expected in ((30, False), (3600, False), (3600.001, True)):
            with self.subTest(expired_seconds=expired_seconds):
                c, clocks = runtime()
                at = utc_instant(iso(clocks.wall))
                c.repository.sources = [at - HOT_DATA_WINDOW - timedelta(seconds=expired_seconds)]
                c.initialize()
                self.assertEqual(c.status()['api_read']['allowed'], not expected)
                self.assertEqual(c.repository.cutoffs, [(at - HOT_DATA_WINDOW - timedelta(hours=1), True)])
                self.assertEqual(c.cycle.done, 0)

    def test_startup_cutoff_uses_database_time_not_backend_wall_clock(self):
        c, clocks = runtime()
        database_at = utc_instant(iso(clocks.wall - 5))
        c.repository.contract = lambda: {'version': 1, 'schema_ready': True, 'as_of': database_at.isoformat()}
        c.repository.sources = [database_at - HOT_DATA_WINDOW - timedelta(hours=2)]
        c.initialize()
        self.assertEqual(c.repository.cutoffs, [(database_at - HOT_DATA_WINDOW - timedelta(hours=1), True)])
        self.assertEqual(c._catchup_cutoff, database_at - HOT_DATA_WINDOW)

    def test_new_expiry_during_first_pass_does_not_hold_catchup(self):
        c, clocks = runtime(pages=3, deleted=1)
        at = utc_instant(iso(clocks.wall))
        old = at - HOT_DATA_WINDOW - timedelta(hours=2)
        newly_expiring = at - HOT_DATA_WINDOW + timedelta(seconds=1)
        c.repository.sources = [old, newly_expiring]
        c.initialize()
        self.assertFalse(c.status()['api_read']['allowed'])
        c.repository.sources.remove(old)  # the original backlog was cleaned
        result = step(c, clocks, 3)
        self.assertLess(newly_expiring, utc_instant(iso(clocks.wall)) - HOT_DATA_WINDOW)
        self.assertTrue(result['api_read']['allowed'])
        self.assertTrue(result['startup_notice']['complete'])
        self.assertEqual(c.repository.cutoffs, [
            (at - HOT_DATA_WINDOW - timedelta(hours=1), True), (at - HOT_DATA_WINDOW, False)])

    def test_held_original_expiry_keeps_catchup_until_released(self):
        c, clocks = runtime(pages=3, deferred=1)
        at = utc_instant(iso(clocks.wall))
        old = at - HOT_DATA_WINDOW - timedelta(hours=2)
        c.repository.sources = [old]
        c.cycle.findings['expired'] = old.isoformat()
        c.initialize()
        self.assertFalse(step(c, clocks, 3)['api_read']['allowed'])
        self.assertFalse(c.status()['startup_notice']['complete'])
        self.assertEqual(c.repository.cutoffs[-1], (at - HOT_DATA_WINDOW, False))
        c.repository.sources.clear()
        c.cycle.findings.clear()
        self.assertTrue(step(c, clocks, 7)['api_read']['allowed'])
        self.assertEqual(c.repository.cutoffs[-1], (at - HOT_DATA_WINDOW, False))

    def test_cleanup_retry_keeps_the_original_completion_cutoff(self):
        c, clocks = runtime(pages=1, failure=True)
        at = utc_instant(iso(clocks.wall))
        old = at - HOT_DATA_WINDOW - timedelta(hours=2)
        c.repository.sources = [old]
        c.initialize()
        frozen = c._catchup_cutoff
        step(c, clocks)
        self.assertTrue(c.status()['maintenance_failed'])
        c.cycle.findings.clear()
        c.repository.sources = [at - HOT_DATA_WINDOW + timedelta(seconds=1)]
        result = step(c, clocks, 15)
        self.assertTrue(result['startup_notice']['complete'])
        self.assertEqual(c.repository.cutoffs[-1], (frozen, False))

    def test_recheck_during_catchup_does_not_move_original_cutoff(self):
        c, clocks = runtime(pages=1)
        at = utc_instant(iso(clocks.wall))
        c.repository.sources = [at - HOT_DATA_WINDOW - timedelta(hours=2)]
        c.initialize()
        frozen = c._catchup_cutoff
        clocks.advance(21)
        self.assertFalse(step(c, clocks)['api_read']['allowed'])
        self.assertEqual(c._catchup_cutoff, frozen)
        self.assertEqual(c.repository.cutoffs[-1], (frozen, False))
    def test_db_unavailable_backoff_and_recovery(self):
        c, clocks = runtime()
        c.repository.error = 'runtime_contract_unavailable'
        c.initialize()
        self.assertEqual(c.status()['reason'], 'database_unavailable')
        with self.assertRaises(HotRuntimeUnavailable):
            c.require_database_ready()
        calls = c.repository.contract_calls
        step(c, clocks, 14)
        self.assertEqual(c.repository.contract_calls, calls)
        c.repository.error = None
        self.assertTrue(step(c, clocks)['user_work']['allowed'])
    def test_schema_failure_is_distinct(self):
        c, _ = runtime()
        c.repository.error = 'runtime_contract_schema_invalid'
        c.initialize()
        self.assertEqual(c.status()['gate'], {'open':False,'reason':'schema_invalid'})
    def test_time_mismatch_keeps_user_work(self):
        c, _ = runtime()
        c.initialize()
        c.time_trust.state = 'mismatch'
        self.assertEqual(c.status()['reason'], 'time_mismatch')
        with self.assertRaises(HotRuntimeUnavailable):
            c.require_collection_ready()
        c.require_database_ready()
        c.time_trust.state = 'trusted'
        c.require_ready()
    def test_unverified_is_warning(self):
        c, _ = runtime()
        c.initialize()
        self.assertEqual(c.status()['time']['state'], 'unverified')
        c.require_ready()
        c.require_collection_ready()
    def test_broken_cleaner_warns_retries_and_keeps_working(self):
        c, clocks = runtime(failure=True)
        c.initialize()
        self.assertTrue(step(c, clocks)['maintenance_failed'])
        c.require_ready()
        c.require_collection_ready()
        c.require_database_ready()
        done = c.cycle.done
        step(c, clocks, 14)
        self.assertEqual(c.cycle.done, done)
        step(c, clocks)
        self.assertGreater(c.cycle.done, done)
    def test_failure_during_catchup_does_not_claim_clean(self):
        c, clocks = runtime(expired=True, failure=True)
        c.initialize()
        s = step(c, clocks)
        self.assertTrue(s['api_read']['allowed'])
        self.assertFalse(s['startup_notice']['complete'])
        self.assertFalse(s['whole_database_clean_verified'])
    def test_cleanup_failure_cannot_hide_db_outage(self):
        c, clocks = runtime(failure=True)
        c.initialize()
        c.repository.error = 'runtime_contract_unavailable'
        self.assertFalse(step(c, clocks)['user_work']['allowed'])
    def test_future_or_unknown_row_does_not_close_all_reads(self):
        for f in ({'invalid_clocks':1}, {'unknown':1}):
            c, clocks = runtime(**f)
            c.initialize()
            self.assertTrue(step(c, clocks)['api_read']['allowed'])
    def test_clock_jump_closes_api_collection_until_recheck(self):
        c, clocks = runtime()
        c.initialize()
        clocks.wall += 100
        self.assertEqual(c.status()['reason'], 'clock_changed')
        c.require_database_ready()
        with self.assertRaises(HotRuntimeUnavailable):
            c.require_collection_ready()
        self.assertTrue(step(c, clocks)['api_read']['allowed'])
    def test_suspend_rechecks_expiry(self):
        c, clocks = runtime()
        c.initialize()
        c.repository.expired = True
        clocks.advance(10 * DAY)
        self.assertEqual(step(c, clocks)['reason'], 'startup_cleanup')
    def test_heartbeat_stall_alone_not_connection_failure(self):
        c, clocks = runtime()
        c.initialize()
        clocks.advance(21)
        c.require_ready()
        c.require_collection_ready()
    def test_clock_jump_is_latched_during_cleanup_retry_backoff(self):
        c, clocks = runtime(failure=True)
        c.initialize()
        step(c, clocks)
        calls = c.repository.contract_calls
        clocks.wall += 100
        self.assertEqual(c.status()['reason'], 'clock_changed')
        self.assertEqual(step(c, clocks)['reason'], 'clock_changed')
        self.assertEqual(c.repository.contract_calls, calls)
        c.require_database_ready()
        observed = []
        original = c.repository.contract
        def contract_during_retry():
            observed.append(c.status()['api_read']['allowed'])
            return original()
        c.repository.contract = contract_during_retry
        step(c, clocks, 14)
        self.assertGreater(c.repository.contract_calls, calls)
        self.assertFalse(observed[0])
        c.require_ready()
    def test_wrong_time_never_runs_destructive_cleanup(self):
        c, clocks = runtime()
        c.time_trust.state = 'mismatch'
        c.initialize()
        step(c, clocks, 30)
        self.assertEqual(c.cycle.done, 0)
        c.require_database_ready()
    def test_mismatch_recovery_takes_fresh_database_startup_time_without_wall_jump(self):
        c, clocks = runtime()
        at = utc_instant(iso(clocks.wall))
        c.repository.sources = [at - HOT_DATA_WINDOW - timedelta(seconds=30)]
        c.time_trust.state = 'mismatch'
        c.initialize()
        for _ in range(180):
            clocks.advance(20)  # no wall-clock drift or watchdog stall
            c.step()
        self.assertEqual(c.repository.contract_calls, 1)
        self.assertEqual(c.repository.expiry_calls, 0)
        c.time_trust.state = 'trusted'
        self.assertEqual(step(c, clocks)['reason'], 'startup_cleanup')
        recovered_at = utc_instant(iso(clocks.wall))
        self.assertEqual(c.repository.contract_calls, 2)
        self.assertEqual(c.repository.cutoffs, [
            (recovered_at - HOT_DATA_WINDOW - timedelta(hours=1), True)])
        self.assertEqual(c._catchup_cutoff, recovered_at - HOT_DATA_WINDOW)

    def test_status_never_waits_for_end_of_sweep_database_check(self):
        import threading
        c, clocks = runtime(expired=True, pages=1)
        c.initialize()
        entered, release, status_done = threading.Event(), threading.Event(), threading.Event()
        def expiry(*, cutoff, strict_entry=False):
            entered.set()
            if not release.wait(3):
                raise AssertionError('probe_timeout')
            return {'as_of':iso(clocks.wall),'expired':False}
        c.repository.startup_expiry = expiry
        worker = threading.Thread(target=lambda:step(c, clocks))
        reader = threading.Thread(target=lambda:(c.status(),status_done.set()))
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(status_done.wait(0.3))
        finally:
            release.set()
            worker.join(3)
            if reader.ident:
                reader.join(3)
    def test_reconnect_cannot_publish_source_access_before_clock_validation(self):
        c, _ = runtime()
        c.initialize()
        c._database_reason = 'database_unavailable'
        observed = []
        def bad_clock(value):
            observed.append(c.status()['api_read']['allowed'])
            raise HotRuntimeUnavailable('hot_runtime_clock_invalid')
        c._check_db_clock = bad_clock
        with self.assertRaises(HotRuntimeUnavailable):
            c._validate()
        self.assertEqual(observed, [False])
        self.assertEqual(c.status()['reason'], 'time_mismatch')
        c.require_database_ready()
    def test_proof_cache_never_confuses_collection_and_api(self):
        c, clocks = runtime(expired=True)
        c.initialize()
        config = HotAdmissionConfig('f9800000-0000-4000-8000-000000000001', 'synthetic_hot_probe_secret_000000000001')
        fetch = Mock(side_effect=lambda:signed_status(c,admission=config,now=clocks.wall))
        remote = RemoteAdmission('http://127.0.0.1:8009/internal/hot-runtime',admission=config,
            fetch=fetch,clock=lambda:clocks.wall,monotonic=lambda:clocks.mono)
        remote.require_collection_ready()
        with self.assertRaises(HotRuntimeUnavailable):
            remote.require_api_ready()
        remote.require_database_ready()
        self.assertEqual(fetch.call_count, 1)
if __name__ == '__main__':
    unittest.main()
