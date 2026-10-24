"""The server clock is compared with Google's clock. No network, database or key use."""
from datetime import datetime, timezone
from contextlib import nullcontext
from email.utils import formatdate
import json
import math
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import requests  # noqa: E402
import hot_runtime  # noqa: E402
import time_trust  # noqa: E402
from time_trust import (PROBE_IDLE_SECONDS, PROBE_RETRY_MAX_SECONDS, PROBE_RETRY_SECONDS,  # noqa: E402
                        VERIFIED_FOR_SECONDS, TimeTrust, parse_http_date)
import youtube_client  # noqa: E402

KEY = 'AIza' + 'k' * 35
VIEW_KEYS = {'state', 'reason', 'offset_seconds', 'checked_at', 'source', 'max_offset_seconds'}


def http_date(seconds):
    return formatdate(seconds, usegmt=True)


class Clocks:
    """A wall clock that can be set, and an elapsed clock that only moves forward."""
    def __init__(self):
        self.wall, self.run = 1_791_000_000.0, 50_000.0

    def advance(self, seconds, *, wall=None):
        self.run += seconds
        self.wall += seconds if wall is None else wall


def trust(clocks, **options):
    return TimeTrust(clock=lambda: clocks.wall, elapsed=lambda: clocks.run, **options)


def answer(monitor, clocks, google_ahead, *, latency=0.0, source='youtube_api'):
    """Google stamps Date halfway through the request, `google_ahead` seconds from our clock."""
    started = monitor.request_started()
    clocks.advance(latency / 2)
    stamp = clocks.wall + google_ahead
    clocks.advance(latency / 2)
    return monitor.record_date(started, http_date(math.floor(stamp)), source=source)


class NoNetwork(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('network is forbidden'))
        guard.start()
        self.addCleanup(guard.stop)


class DateHeaderTests(unittest.TestCase):
    def test_only_the_imf_fixdate_form_is_read(self):
        self.assertEqual(parse_http_date('Thu, 08 Oct 2026 12:00:00 GMT'), 1791460800.0)
        self.assertEqual(parse_http_date('Thu, 01 Jan 1970 00:00:00 GMT'), 0.0)
        for value in (
            'Wed, 08 Oct 2026 12:00:00 GMT',      # wrong weekday
            'Thu, 8 Oct 2026 12:00:00 GMT',       # one-digit day
            'Thursday, 08-Oct-26 12:00:00 GMT',   # obsolete RFC 850 form
            'Thu Oct  8 12:00:00 2026',           # obsolete asctime form
            'Thu, 08 Oct 2026 12:00:00 UTC',      # other zone name
            'Thu, 08 Oct 2026 12:00:00 +0000',
            'Thu, 08 oct 2026 12:00:00 GMT',
            ' Thu, 08 Oct 2026 12:00:00 GMT',
            'Thu, 08 Oct 2026 12:00:00 GMT ',
            'Sat, 31 Feb 2026 12:00:00 GMT',      # impossible date
            'Thu, 08 Oct 2026 24:00:00 GMT',
            'Thu, 08 Oct 2026 12:00:60 GMT',      # leap second: skipped, not guessed
            'Thu, 08 Oct 2026 12:00:00 GMT' + ' ' * 20,
            '', None, b'Thu, 08 Oct 2026 12:00:00 GMT', 1791460800, Mock(),
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_http_date(value))


class TimeTrustStateTests(unittest.TestCase):
    def setUp(self):
        self.clocks = Clocks()
        self.changes = []
        self.monitor = trust(self.clocks, on_change=lambda *change: self.changes.append(change))

    def status(self):
        value = self.monitor.status()
        self.assertEqual(set(value), VIEW_KEYS)
        json.dumps(value, allow_nan=False)
        return value

    def test_without_an_answer_the_clock_is_unverified(self):
        self.assertEqual(self.status(), {'state': 'unverified', 'reason': 'not_checked_yet', 'offset_seconds': None,
                                         'checked_at': None, 'source': None, 'max_offset_seconds': 60})

    def test_an_answer_within_sixty_seconds_is_trusted(self):
        self.assertTrue(answer(self.monitor, self.clocks, 5, latency=0.2))
        value = self.status()
        self.assertEqual((value['state'], value['reason'], value['source']),
                         ('trusted', 'agrees_with_google', 'youtube_api'))
        self.assertAlmostEqual(value['offset_seconds'], 5, delta=1)
        self.assertEqual(value['checked_at'], datetime.fromtimestamp(self.clocks.wall, timezone.utc).isoformat())

    def test_a_difference_over_sixty_seconds_in_either_direction_is_a_mismatch(self):
        for ahead in (120, -120, 3 * 86400, -86400):
            with self.subTest(google_ahead=ahead):
                monitor = trust(self.clocks)
                self.assertTrue(answer(monitor, self.clocks, ahead, latency=0.4))
                value = monitor.status()
                self.assertEqual((value['state'], value['reason']), ('mismatch', 'offset_too_large'))
                self.assertAlmostEqual(value['offset_seconds'], ahead, delta=1)

    def test_the_limit_allows_for_the_one_second_resolution_of_date(self):
        for ahead, state in ((59, 'trusted'), (-60, 'trusted'), (61, 'mismatch'), (-62, 'mismatch')):
            with self.subTest(google_ahead=ahead):
                monitor = trust(self.clocks)
                self.assertTrue(answer(monitor, self.clocks, ahead))
                self.assertEqual(monitor.status()['state'], state)
        # [60, 61] and [-61, -60] cross the limit: such an answer proves nothing either way.
        for ahead in (60, -61):
            with self.subTest(google_ahead=ahead):
                monitor = trust(self.clocks)
                self.assertFalse(answer(monitor, self.clocks, ahead))
                self.assertEqual(monitor.status()['reason'], 'not_checked_yet')

    def test_the_offset_is_measured_from_the_middle_of_the_request(self):
        # Ten seconds in flight; Google stamped its clock, equal to ours, halfway through.
        self.assertTrue(answer(self.monitor, self.clocks, 0, latency=10))
        value = self.status()
        self.assertEqual(value['state'], 'trusted')
        self.assertEqual(value['offset_seconds'], 0.5)  # not -5 (arrival) or +5 (departure)

    def test_a_slow_answer_that_cannot_decide_is_ignored(self):
        self.assertFalse(answer(self.monitor, self.clocks, 50, latency=30))  # [35, 66] crosses 60
        self.assertEqual(self.status()['state'], 'unverified')
        self.assertTrue(answer(self.monitor, self.clocks, 0, latency=30))    # [-15, 16] is inside
        self.assertEqual(self.status()['state'], 'trusted')

    def test_an_answer_is_ignored_when_our_clock_changed_during_the_request(self):
        started = self.monitor.request_started()
        self.clocks.advance(1, wall=11)
        self.assertFalse(self.monitor.record_date(started, http_date(math.floor(self.clocks.wall)),
                                                  source='youtube_api'))
        self.assertEqual(self.status()['reason'], 'not_checked_yet')

    def test_a_check_older_than_six_hours_no_longer_verifies(self):
        self.assertTrue(answer(self.monitor, self.clocks, 3))
        self.clocks.advance(VERIFIED_FOR_SECONDS)
        self.assertEqual(self.status()['state'], 'trusted')
        self.clocks.advance(1)
        value = self.status()
        self.assertEqual((value['state'], value['reason'], value['source']),
                         ('unverified', 'check_expired', 'youtube_api'))
        self.assertAlmostEqual(value['offset_seconds'], 3, delta=1)

    def test_a_proven_mismatch_does_not_expire(self):
        self.assertTrue(answer(self.monitor, self.clocks, 600))
        self.clocks.advance(2 * VERIFIED_FOR_SECONDS)
        self.assertEqual(self.status()['state'], 'mismatch')

    def test_a_clock_change_after_a_check_is_detected_without_a_new_answer(self):
        self.assertTrue(answer(self.monitor, self.clocks, 0))
        self.clocks.advance(10, wall=10 + 120)   # someone set the clock two minutes ahead
        value = self.status()
        self.assertEqual((value['state'], value['reason']), ('mismatch', 'clock_jumped'))
        self.assertAlmostEqual(value['offset_seconds'], -120, delta=1)
        self.clocks.advance(10, wall=10 - 120)   # and back
        self.assertEqual(self.status()['state'], 'trusted')
        self.clocks.advance(0, wall=-59.5)       # the interval [59.5, 60.5] crosses the limit
        self.assertEqual((self.status()['state'], self.status()['reason']), ('unverified', 'clock_jumped'))

    def test_a_clock_set_beyond_the_calendar_is_still_a_mismatch(self):
        self.assertTrue(answer(self.monitor, self.clocks, 0))
        self.clocks.advance(1, wall=1e13)   # past year 9999: no ISO time, still a state
        value = self.status()
        self.assertEqual((value['state'], value['reason'], value['checked_at']), ('mismatch', 'clock_jumped', None))

    def test_setting_a_wrong_clock_right_clears_the_mismatch(self):
        self.assertTrue(answer(self.monitor, self.clocks, 120))   # our clock is two minutes behind
        self.clocks.advance(30, wall=30 + 120)
        self.assertEqual(self.status()['state'], 'trusted')

    def test_a_new_agreeing_answer_clears_a_mismatch(self):
        self.assertTrue(answer(self.monitor, self.clocks, -300))
        self.assertTrue(answer(self.monitor, self.clocks, 1, source='google_probe'))
        value = self.status()
        self.assertEqual((value['state'], value['source']), ('trusted', 'google_probe'))

    def test_failed_checks_are_never_a_mismatch(self):
        failing = trust(self.clocks, fetch_date=Mock(side_effect=requests.ConnectionError('offline')))
        failing.probe_now()
        self.assertEqual((failing.status()['state'], failing.status()['reason']), ('unverified', 'check_failed'))
        # A recent agreeing check survives failures until it is six hours old.
        self.assertTrue(answer(failing, self.clocks, 2))
        failing.probe_now()
        self.assertEqual(failing.status()['state'], 'trusted')
        self.clocks.advance(VERIFIED_FOR_SECONDS + 1)
        failing.probe_now()
        self.assertEqual((failing.status()['state'], failing.status()['reason']), ('unverified', 'check_failed'))

    def test_bad_input_is_ignored_without_raising(self):
        stamp = http_date(math.floor(self.clocks.wall))
        for started, value, source in ((None, stamp, 'youtube_api'), ('x', stamp, 'youtube_api'),
                                       ((1.0,), stamp, 'youtube_api'), ((math.nan, 1.0), stamp, 'youtube_api'),
                                       (self.monitor.request_started(), stamp, 'elsewhere'),
                                       (self.monitor.request_started(), 'not a date', 'youtube_api'),
                                       (self.monitor.request_started(), None, 'youtube_api')):
            with self.subTest(started=started, value=value, source=source):
                self.assertFalse(self.monitor.record_date(started, value, source=source))
        broken = TimeTrust(clock=Mock(side_effect=OSError()), elapsed=Mock(side_effect=OSError()))
        self.assertIsNone(broken.request_started())
        self.assertFalse(broken.record_date((1.0, 1.0), stamp, source='youtube_api'))
        self.assertEqual(broken.status()['state'], 'unverified')

    def test_each_change_is_reported_once(self):
        self.status()
        answer(self.monitor, self.clocks, 0)
        answer(self.monitor, self.clocks, 0)
        self.status()
        answer(self.monitor, self.clocks, 500)
        self.assertEqual(self.changes, [('unverified', 'not_checked_yet'), ('trusted', 'agrees_with_google'),
                                        ('mismatch', 'offset_too_large')])
        failing_report = trust(self.clocks, on_change=Mock(side_effect=RuntimeError()))
        self.assertEqual(failing_report.status()['state'], 'unverified')


class ProbeTests(NoNetwork):
    def setUp(self):
        super().setUp()
        self.clocks = Clocks()
        self.ahead = 0
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        return http_date(math.floor(self.clocks.wall + self.ahead))

    def monitor(self, **options):
        options.setdefault('fetch_date', self.fetch)
        options.setdefault('spawn', lambda target: target())
        return trust(self.clocks, **options)

    def test_the_request_has_no_key_no_parameters_and_short_timeouts(self):
        response = Mock()
        response.headers = {'Date': 'Thu, 08 Oct 2026 12:00:00 GMT'}
        with patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'YOUTUBE_API_KEY': KEY}), \
                patch('requests.head', return_value=response) as head:
            self.assertEqual(time_trust.fetch_google_date(), 'Thu, 08 Oct 2026 12:00:00 GMT')
        self.assertEqual(head.call_args.args, ('https://www.googleapis.com/',))
        self.assertEqual(head.call_args.kwargs, {'headers': {'Cache-Control': 'no-cache'},
                                                 'timeout': (3.05, 5), 'allow_redirects': False})
        self.assertNotIn(KEY, repr(head.call_args))
        response.close.assert_called_once_with()
        response.headers = {}
        with patch('requests.head', return_value=response):
            self.assertIsNone(time_trust.fetch_google_date())

    def test_construction_and_status_are_quiet_but_first_explicit_probe_is_immediate(self):
        monitor = self.monitor()
        for _ in range(3):
            self.assertEqual(monitor.status()['reason'], 'not_checked_yet')
        self.assertEqual(self.fetches, 0)
        self.assertTrue(monitor.probe_if_due())
        self.assertEqual(self.fetches, 1)
        self.assertEqual(monitor.status()['state'], 'trusted')
        self.assertFalse(monitor.probe_if_due())
        self.assertEqual(self.fetches, 1)

    def test_the_probe_runs_after_an_hour_without_an_answer(self):
        monitor = self.monitor()
        self.assertTrue(monitor.probe_if_due())
        self.assertEqual((monitor.status()['state'], monitor.status()['source']), ('trusted', 'google_probe'))
        self.assertFalse(monitor.probe_if_due())
        self.clocks.advance(PROBE_IDLE_SECONDS - 1)
        self.assertFalse(monitor.probe_if_due())
        self.clocks.advance(1)
        self.assertTrue(monitor.probe_if_due())
        self.assertEqual(self.fetches, 2)

    def test_youtube_answers_make_the_probe_unnecessary(self):
        monitor = self.monitor()
        self.clocks.advance(PROBE_IDLE_SECONDS / 2)
        answer(monitor, self.clocks, 0)
        self.assertFalse(monitor.probe_if_due())
        self.clocks.advance(PROBE_IDLE_SECONDS - 1)
        self.assertFalse(monitor.probe_if_due())
        self.assertEqual(self.fetches, 0)

    def test_a_failed_probe_leaves_the_clock_unverified_and_retries_with_growing_pauses(self):
        fetch = Mock(side_effect=requests.Timeout('slow'))
        monitor = self.monitor(fetch_date=fetch)
        self.assertTrue(monitor.probe_if_due())
        pauses = []
        for _ in range(6):
            self.assertEqual((monitor.status()['state'], monitor.status()['reason']), ('unverified', 'check_failed'))
            waited = 0
            while not monitor.probe_if_due():
                self.clocks.advance(60)
                waited += 60
            pauses.append(waited)
        self.assertEqual(pauses, [PROBE_RETRY_SECONDS, 2 * PROBE_RETRY_SECONDS, 4 * PROBE_RETRY_SECONDS,
                                  8 * PROBE_RETRY_SECONDS, PROBE_RETRY_MAX_SECONDS, PROBE_RETRY_MAX_SECONDS])
        self.assertEqual(fetch.call_count, 7)

    def test_an_answer_without_date_counts_as_a_failure(self):
        monitor = self.monitor(fetch_date=lambda: None)
        monitor.probe_if_due()
        self.assertEqual(monitor.status()['reason'], 'check_failed')

    def test_a_mismatch_or_clock_change_is_checked_again_after_five_minutes(self):
        self.ahead = 300
        monitor = self.monitor()
        monitor.probe_if_due()
        self.assertEqual(monitor.status()['state'], 'mismatch')
        self.clocks.advance(PROBE_RETRY_SECONDS - 1)
        self.assertFalse(monitor.probe_if_due())
        self.clocks.advance(1)
        self.ahead = 0
        self.assertTrue(monitor.probe_if_due())
        self.assertEqual(monitor.status()['state'], 'trusted')
        self.clocks.advance(PROBE_RETRY_SECONDS, wall=PROBE_RETRY_SECONDS + 30)  # a 30-second change
        self.assertEqual(monitor.status()['state'], 'trusted')
        self.assertTrue(monitor.probe_if_due())

    def test_one_probe_at_a_time_and_a_thread_failure_is_contained(self):
        pending = []
        monitor = self.monitor(spawn=pending.append)
        self.assertTrue(monitor.probe_if_due())
        self.assertFalse(monitor.probe_if_due())
        pending.pop()()
        self.assertEqual(monitor.status()['state'], 'trusted')
        refused = self.monitor(spawn=Mock(side_effect=RuntimeError('no threads')))
        self.assertFalse(refused.probe_if_due())
        self.assertEqual(refused.status()['reason'], 'check_failed')

    def test_the_default_probe_runs_in_its_own_thread(self):
        done = threading.Event()
        def fetch():
            self.assertNotEqual(threading.current_thread(), threading.main_thread())
            done.set()
            return None
        monitor = trust(self.clocks, fetch_date=fetch)
        self.assertTrue(monitor.probe_if_due())
        self.assertTrue(done.wait(5))


class Ready:
    def require_ready(self):
        pass

    require_collection_ready = require_ready
    require_database_ready = require_ready


class Response:
    def __init__(self, status_code=200, headers=None):
        self.status_code, self.headers = status_code, headers or {}

    def json(self):
        return {}

    def iter_content(self, chunk_size): return iter((b'{}',))
    def close(self): pass


def youtube_get():
    return youtube_client.youtube_api_get('videos', phase='test', quota_bucket='general', attempt_recorder=None,
                                          params={'part': 'snippet', 'id': 'abc'}, api_key=KEY)


class YouTubeClientHookTests(NoNetwork):
    def setUp(self):
        super().setUp()
        environ = {k: v for k, v in os.environ.items() if k != 'LIC_SETUP_DIRECTORY'}
        environ.update({'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
                        'LIC_DATABASE_ID': '91f5d9f3-9230-4ee6-b170-155d4c3c1719',
                        'LIC_HOT_RUNTIME_SECRET': 'synthetic-internal-admission-secret-32'})
        for patcher in (patch.dict(os.environ, environ, clear=True),
                        patch('provider_runtime.admit_request', side_effect=lambda **kw: nullcontext(kw.get('cancel'))),
                        patch.object(hot_runtime, '_local_controller', Ready())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clocks = Clocks()
        self.monitor = trust(self.clocks)
        patcher = patch.object(time_trust, '_MONITOR', self.monitor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, response=None, error=None):
        sent = []
        def get(url, **options):
            sent.append(options)
            self.clocks.advance(0.3)
            if error is not None:
                raise error
            return response
        recorded = []
        original = time_trust.record_date
        def record(*args, **options):
            recorded.append((args, options))
            return original(*args, **options)
        with patch.object(youtube_client.requests, 'get', get), patch.object(time_trust, 'record_date', record):
            result = youtube_get()
        return result, sent, recorded

    def test_every_answer_records_only_its_date_header(self):
        date = http_date(math.floor(self.clocks.wall))
        for status_code in (200, 304, 403, 500):
            with self.subTest(status_code=status_code):
                response = Response(status_code, {'Date': date, 'Content-Type': 'application/json'})
                result, sent, recorded = self.request(response)
                self.assertIs(result, response)
                self.assertEqual(sent[0]['headers']['X-Goog-Api-Key'], KEY)
                self.assertEqual(len(recorded), 1)
                (started, value), options = recorded[0]
                self.assertEqual((value, options), (date, {'source': 'youtube_api'}))
                self.assertEqual(len(started), 2)
                self.assertNotIn(KEY, repr(recorded))
                self.assertNotIn('googleapis', repr(recorded))
        self.assertEqual((self.monitor.status()['state'], self.monitor.status()['source']),
                         ('trusted', 'youtube_api'))

    def test_a_transport_failure_records_nothing(self):
        with self.assertRaises(requests.ConnectionError):
            self.request(error=requests.ConnectionError('offline'))
        self.assertEqual(self.monitor.status()['reason'], 'not_checked_yet')

    def test_an_answer_without_a_readable_date_changes_nothing(self):
        for response in (Response(200, {}), SimpleNamespace(status_code=200,
                iter_content=lambda **kw: iter((b'{}',)), close=lambda: None), Response(200, Mock())):
            with self.subTest(response=response):
                result, _, recorded = self.request(response)
                self.assertIs(result, response)
                self.assertIsNone(recorded[0][0][1])
        self.assertEqual(self.monitor.status()['reason'], 'not_checked_yet')

    def test_a_mismatch_does_not_stop_requests_yet(self):
        self.request(Response(200, {'Date': http_date(math.floor(self.clocks.wall + 3600))}))
        self.assertEqual(self.monitor.status()['state'], 'mismatch')
        result, sent, _ = self.request(Response(200, {}))
        self.assertEqual((result.status_code, len(sent)), (200, 1))


class HotRuntimeStatusTests(NoNetwork):
    def controller(self, **options):
        return hot_runtime.HotRuntimeController(Mock(), local_cleanup=Mock(), clear_caches=Mock(), **options)

    def test_status_shows_the_clock_check_read_only(self):
        clocks = Clocks()
        monitor = trust(clocks)
        answer(monitor, clocks, 400)
        controller = self.controller(time_trust=monitor)
        with patch('telemetry.emit_event'):
            value = controller.status()
        self.assertEqual(value['time'], monitor.status())
        self.assertEqual(value['time']['state'], 'mismatch')
        self.assertEqual((value['status'], value['reason']), ('blocked', 'validation_pending'))
        config = hot_runtime.HotAdmissionConfig('91f5d9f3-9230-4ee6-b170-155d4c3c1719', 'synthetic_secret_' + 'x' * 20)
        with patch('telemetry.emit_event'):
            proof = hot_runtime.signed_status(controller, admission=config, diagnostics=True)
        self.assertEqual(proof['diagnostics']['time']['state'], 'mismatch')
        self.assertNotIn('time', set(proof) - {'diagnostics'})

    def test_the_process_check_is_the_default(self):
        self.assertIs(self.controller().time_trust, time_trust.monitor())

    def test_time_mismatch_blocks_api_and_collection_but_not_user_work(self):
        from test_readiness_after_stall import ready_runtime
        runtime, _ = ready_runtime()
        clocks = Clocks()
        mismatched = trust(clocks)
        answer(mismatched, clocks, -900)
        runtime.time_trust = mismatched
        value = runtime.status()
        self.assertEqual((value['status'], value['time']['state']), ('blocked', 'mismatch'))
        self.assertFalse(value['collection']['allowed'])
        self.assertTrue(value['user_work']['allowed'])

    def test_the_maintenance_loop_asks_for_a_due_probe_before_database_work(self):
        asked, stepped = threading.Event(), threading.Event()
        observed = []
        monitor = SimpleNamespace(status=lambda: {}, probe_if_due=asked.set)
        controller = self.controller(time_trust=monitor)
        def step():
            observed.append(asked.is_set())
            controller._stop.set()
            stepped.set()
        with patch.object(controller, 'step', side_effect=step):
            controller.start()
            try:
                self.assertTrue(stepped.wait(5))
                self.assertEqual(observed, [True])
            finally:
                controller.close()


if __name__ == '__main__':
    unittest.main()
