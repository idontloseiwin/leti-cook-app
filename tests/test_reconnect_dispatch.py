"""Opt-in reconnect adapter with actual coordinator; no PostgreSQL/provider I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import sys
import sqlite3
import tempfile
from threading import Event, get_ident
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
sys.path.insert(0, str(ROOT / 'tests'))
from collection.dispatch import Ack, Completion, Coordinator, DispatchBlocked, Skipped
from collection.reconnect import ReconnectUnit
from channel_reconnect_outcome import ReconnectResponse, reconnect_refusal
import collection_shutdown
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied, HotRuntimeUnavailable
from db.user_work.reconnect_admission import ChannelAddAdmission, ChannelAddAdmissionUnavailable
from provider_admission import AdmissionUnavailable, AdmissionCancelled, AdmissionFull, AdmissionTimeout
from youtube_errors import ProviderOutcome, YouTubeDailyQuotaExceededError, YouTubeRateLimitError, classify_youtube_outcome

ID = 'a0000000-0000-4000-8000-000000000001'
OTHER = 'a0000000-0000-4000-8000-000000000002'
TOKEN = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
CHANNEL = 'UC' + 'a' * 22
IN_TRANSACTION = ContextVar('reconnect_test_tx', default=False)


class Repository:
    def __init__(self):
        self.owner = get_ident()
        self.events = []
        self.claim = dict(id=ID, revision=7, lease_token=TOKEN,
                          user_input='https://www.youtube.com/@synthetic')
        self.unknown = None
        self.binding = {'outcome': 'ready', 'job_id': 'atomic-initial-job'}
        self.release = {'outcome': 'ready'}

    def call(self, name, args, kwargs, result):
        assert get_ident() == self.owner
        assert not IN_TRANSACTION.get()
        token = IN_TRANSACTION.set(True)
        try:
            self.events.append((name, args, kwargs))
            if self.unknown == name:
                raise DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
            return result
        finally:
            IN_TRANSACTION.reset(token)

    def claim_reconnect(self, **kwargs):
        return self.call('claim', (), kwargs, [] if self.claim is None else [dict(self.claim)])

    def defer_reconnect(self, *args, **kwargs):
        return self.call('defer', args, kwargs, dict(self.release))

    def bind_reconnect(self, *args, **kwargs):
        return self.call('bind', args, kwargs, dict(self.binding))

    def abort_reconnect(self, *args, **kwargs):
        return self.call('abort', args, kwargs, dict(self.release))


class ReconnectDispatchTests(unittest.TestCase):
    def setUp(self):
        handoff = patch.object(collection_shutdown, '_requested', Event())
        handoff.start()
        self.addCleanup(handoff.stop)
        self.repo = Repository()
        self.provider_calls = []
        self.response = ReconnectResponse(channel={'channel_id': CHANNEL, 'private_source': 'secret title'})
        self.ready = Mock()
        self.source = Mock()

    def resolver(self, locator, *, cancelled, deadline):
        self.assertNotEqual(get_ident(), self.repo.owner)
        self.assertFalse(IN_TRANSACTION.get())
        self.assertFalse(cancelled.is_set())
        self.assertGreater(deadline, time.monotonic())
        self.provider_calls.append(locator)
        return self.response

    def unit(self, **kwargs):
        return ReconnectUnit(self.repo, identity=kwargs.pop('identity', ID),
            resolve_channel=kwargs.pop('resolve_channel', self.resolver), require_ready=self.ready,
            require_source=self.source, **kwargs)

    def run_unit(self, unit):
        with ThreadPoolExecutor(max_workers=1) as executor:
            coordinator = Coordinator(executor, concurrency=1)
            coordinator.offer(unit.work)
            coordinator.step()
            end = time.monotonic() + 5
            while coordinator.snapshot().held:
                self.assertLess(time.monotonic(), end)
                coordinator.wait(0.1)
                coordinator.step()
            return coordinator.snapshot()

    def test_offer_no_claim_exact_prepare_and_atomic_bind_on_owner(self):
        unit = self.unit()
        self.assertEqual(self.repo.events, [])
        self.ready.assert_not_called()
        self.assertEqual(unit.work.exclusion_key, 'reconnect:' + ID)
        self.assertEqual(unit.work.lane, 'operator')
        self.assertEqual(self.run_unit(unit).held, 0)
        self.assertEqual([row[0] for row in self.repo.events], ['claim', 'bind'])
        self.assertEqual(self.repo.events[0][2], dict(identity=ID, limit=1, lease_seconds=180))
        self.assertEqual(self.repo.events[1][1][:3], (ID, 7, TOKEN))
        self.assertEqual(self.repo.events[1][2], dict(max_attempts=3))
        self.assertEqual(unit.result['job_id'], 'atomic-initial-job')
        self.assertEqual(self.ready.call_count, 2)
        self.source.assert_called_once_with(self.response.channel)
        self.assertNotIn('secret title', repr(self.response))
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.prepare()
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('response', value=self.response))

    def test_missing_hidden_deleted_ineligible_are_skipped_without_provider(self):
        # The real exact SQL owner supplies no claim for all these predicates.
        self.repo.claim = None
        unit = self.unit()
        self.assertEqual(self.run_unit(unit).ready, 0)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual([row[0] for row in self.repo.events], ['claim'])

    def test_wrong_identity_never_substitutes_candidate(self):
        self.repo.claim['id'] = OTHER
        with self.assertRaises(DispatchBlocked):
            self.run_unit(self.unit())
        self.assertEqual(self.provider_calls, [])

    def test_handoff_before_claim_skips(self):
        collection_shutdown.request_handoff()
        self.assertIsInstance(self.unit().prepare(), Skipped)
        self.assertEqual(self.repo.events, [])

    def test_exhausted_manual_minute_and_day_do_not_limit_or_charge_reconnect(self):
        for batches in (1, 15):
            with self.subTest(window='minute' if batches == 1 else 'day'), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'channel-add.sqlite3'
                path.parent.chmod(0o700)
                now = [100000]
                manual = ChannelAddAdmission.initialize(path, ID, clock=lambda: now[0])
                for minute in range(batches):
                    now[0] = 100000 + minute * 60
                    self.assertEqual([manual.acquire() for _ in range(20)], [0] * 20)
                if batches == 15:
                    now[0] += 60
                wait = manual.acquire()
                self.assertEqual(wait, 60 if batches == 1 else 85500)
                before = path.read_bytes()
                self.repo = Repository()
                self.provider_calls.clear()
                with patch('db.user_work.reconnect_admission.configured_channel_add_admission',
                           side_effect=AssertionError('manual limiter must not load for reconnect')):
                    self.assertEqual(self.run_unit(self.unit()).held, 0)
                self.assertEqual([event[0] for event in self.repo.events], ['claim', 'bind'])
                self.assertEqual(len(self.provider_calls), 1)
                self.assertEqual(path.read_bytes(), before)
                restarted = ChannelAddAdmission(path, ID, clock=lambda: now[0])
                self.assertEqual(restarted.acquire(), wait)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM channel_add_admissions').fetchone()[0], batches * 20)

    def test_missing_manual_store_does_not_block_reconnect_or_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'channel-add.sqlite3'
            manual = ChannelAddAdmission(path, ID)
            with self.assertRaises(ChannelAddAdmissionUnavailable):
                manual.acquire()
            with patch('db.user_work.reconnect_admission.configured_channel_add_admission',
                       side_effect=ChannelAddAdmissionUnavailable()):
                self.assertEqual(self.run_unit(self.unit()).held, 0)
            self.assertFalse(path.exists())
            self.assertEqual([event[0] for event in self.repo.events], ['claim', 'bind'])

    def test_cancel_or_expire_before_send_releases_durably(self):
        for phase in ('cancelled', 'expired'):
            unit = self.unit()
            unit.prepare()
            self.assertIsInstance(unit.settle(Completion(phase)), Ack)
            self.assertEqual(self.repo.events[-1][2], dict(reason='busy', retry_seconds=1))
        self.assertEqual(self.provider_calls, [])

    def test_handoff_between_prepare_and_fetch_never_resolves(self):
        unit = self.unit()
        prepared = unit.prepare()
        collection_shutdown.request_handoff()
        response = prepared.fetch(Event())
        self.assertFalse(response.dispatched)
        self.assertIsInstance(unit.settle(Completion('response', value=response)), Ack)
        self.assertEqual(self.provider_calls, [])

    def test_provider_refusals_have_distinct_durable_states(self):
        for code, delay in (('provider_key_rejected', None), ('provider_access_denied', None),
                            ('channel_not_found', None), ('needs_input', None),
                            ('unsupported_legacy_channel_link', None), ('provider_unavailable', 60),
                            ('initial_inventory_count_unavailable', 60), ('provider_rate_limited', 11)):
            with self.subTest(code=code):
                self.response = ReconnectResponse(code=code, retry_seconds=11 if code=='provider_rate_limited' else None)
                self.run_unit(self.unit())
                self.assertEqual(self.repo.events[-1][2], dict(reason=code, retry_seconds=delay))
        self.source.assert_not_called()
        self.assertFalse(any(row[0]=='bind' for row in self.repo.events))

    def test_source_outcomes_reach_existing_durable_owner(self):
        from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout
        from channels.reconnect_source import ReconnectSource
        from collection.channel_metadata import channel_metadata_update_values
        from db.user_work import user_work_reconnect as queue
        from test_reconnect_source import NOW, PROFILE, response, refusal
        from test_user_work_reconnect_queue import Database
        zero = {'kind': 'youtube#channelListResponse', 'etag': 'synthetic',
                'pageInfo': {'totalResults': 0, 'resultsPerPage': 5}}
        found = response({'kind': 'youtube#channelListResponse', 'items': [PROFILE],
                          'pageInfo': {'totalResults': 1, 'resultsPerPage': 5}})
        cases = [
            ('zero_omitted', [response(zero)], 'channel_not_found', None),
            ('zero_items', [response(dict(zero, items=[]))], 'channel_not_found', None),
            ('malformed', [response({})], 'channel_lookup_unavailable', 60),
            ('quota', [refusal(403, 'quotaExceeded')], 'provider_quota_exceeded', 68400),
            ('timeout', [Timeout('private provider URL')], 'provider_unavailable', 60),
            ('partial_connection', [found, RequestsConnectionError('private provider URL')],
             'provider_unavailable', 60),
            ('found', [found] + [response({'pageInfo': {'totalResults': 1}})] * 3, None, None),
        ]
        for name, answers, code, delay in cases:
            with self.subTest(case=name):
                self.repo = Repository()
                database = Database()
                def durable_defer(*args, **kwargs):
                    self.repo.call('defer', args, kwargs, None)
                    return queue.defer_reconnect(database, *args, **kwargs)
                self.repo.defer_reconnect = durable_defer
                request = Mock(side_effect=answers)
                source = ReconnectSource(request, metadata_update_values=channel_metadata_update_values,
                                         utc_clock=lambda: NOW)
                unit = self.unit(resolve_channel=source, utc_clock=lambda: NOW)
                self.assertEqual(self.run_unit(unit).held, 0)
                self.assertEqual(request.call_count, len(answers))
                self.assertEqual(unit.phase, 'acknowledged')
                self.assertEqual([row[0] for row in self.repo.events], ['claim', 'defer' if code else 'bind'])
                if code:
                    self.assertEqual(self.repo.events[-1][2], dict(reason=code, retry_seconds=delay))
                    self.assertEqual(database.calls[-1][1],
                        ('blocked' if delay is None else 'waiting', delay, delay, code, ID, 7, 7, TOKEN))
                    self.assertEqual(database.ends, ['commit'])
                else:
                    self.assertEqual([call.args[0] for call in request.call_args_list],
                                     ['channels', 'playlistItems', 'playlistItems', 'playlistItems'])
                self.assertNotIn('private', repr(self.repo.events))

    def test_source_transport_deferral_unknown_ack_never_retries_or_publishes(self):
        from requests.exceptions import Timeout
        from channels.reconnect_source import ReconnectSource
        from collection.channel_metadata import channel_metadata_update_values
        request = Mock(side_effect=Timeout('private provider URL'))
        source = ReconnectSource(request, metadata_update_values=channel_metadata_update_values)
        observer = Mock()
        self.repo.unknown = 'defer'
        unit = self.unit(resolve_channel=source, on_settled=observer)
        with self.assertRaises(DispatchBlocked):
            self.run_unit(unit)
        request.assert_called_once()
        observer.assert_not_called()
        self.assertEqual(unit.phase, 'deferring')
        self.assertEqual([row[0] for row in self.repo.events], ['claim', 'defer'])
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('response', value=ReconnectResponse(code='provider_unavailable')))

    def test_explicit_daily_quota_uses_25_hour_pacific_fallback(self):
        self.response = reconnect_refusal(ProviderOutcome('daily_quota', 'general'))
        unit = self.unit(utc_clock=lambda: datetime(2026, 11, 1, 7, tzinfo=timezone.utc))
        self.run_unit(unit)
        self.assertEqual(self.repo.events[-1][2], dict(reason='provider_quota_exceeded', retry_seconds=90000))

    def test_confirmed_quota_is_reported_after_durable_defer_on_owner_thread(self):
        self.response = ReconnectResponse(code='provider_quota_exceeded')
        seen = []
        def settled(summary):
            self.assertEqual(get_ident(), self.repo.owner)
            self.assertEqual(self.repo.events[-1][0], 'defer')
            self.assertFalse(IN_TRANSACTION.get())
            seen.append(summary)
        self.run_unit(self.unit(on_settled=settled))
        self.assertEqual(seen, [dict(outcome='ready', reason='provider_quota_exceeded',
                                     quota_exhausted=True, rate_limited=False)])

    def test_unknown_quota_defer_does_not_publish_ack(self):
        self.response = ReconnectResponse(code='provider_quota_exceeded')
        self.repo.unknown = 'defer'
        observer = Mock()
        with self.assertRaises(DispatchBlocked):
            self.run_unit(self.unit(on_settled=observer))
        observer.assert_not_called()

    def test_slice_stop_before_claim_skips_without_mutation(self):
        observer = Mock()
        unit = self.unit(on_settled=observer, should_stop=lambda: True)
        self.assertIsInstance(unit.prepare(), Skipped)
        self.assertEqual(self.repo.events, [])
        observer.assert_not_called()

    def test_slice_stop_after_prepare_releases_without_provider(self):
        stopped = Event()
        observer = Mock()
        unit = self.unit(on_settled=observer, should_stop=stopped.is_set)
        unit.prepare()
        stopped.set()
        response = unit.fetch(Event())
        self.assertFalse(response.dispatched)
        self.assertIsInstance(unit.settle(Completion('response', value=response)), Ack)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(self.repo.events[-1][0], 'defer')
        self.assertEqual(observer.call_args.args[0], dict(outcome='ready', reason='busy',
                                                        quota_exhausted=False, rate_limited=False))

    def test_daily_exception_has_same_reset_without_raw_message(self):
        def quota(*args, **kwargs):
            raise YouTubeDailyQuotaExceededError('private provider URL')
        unit = self.unit(resolve_channel=quota, utc_clock=lambda: datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
        self.run_unit(unit)
        self.assertEqual(self.repo.events[-1][2], dict(reason='provider_quota_exceeded', retry_seconds=68400))
        self.assertNotIn('private provider', repr(self.repo.events))


    def test_typed_rate_exception_defers_once_with_its_short_delay_not_daily_reset(self):
        for delay in (1, 17, 60):
            with self.subTest(delay=delay):
                resolver = Mock(side_effect=YouTubeRateLimitError(retry_after_seconds=delay))
                before = len(self.repo.events)
                unit = self.unit(resolve_channel=resolver)
                self.assertEqual(self.run_unit(unit).held, 0)
                resolver.assert_called_once()
                self.assertEqual([event[0] for event in self.repo.events[before:]], ['claim', 'defer'])
                self.assertEqual(self.repo.events[-1][2], dict(reason='provider_rate_limited', retry_seconds=delay))
                self.assertEqual(unit.phase, 'acknowledged')
        self.source.assert_not_called()

    def test_typed_rate_deferral_unknown_commit_never_acks_or_retries(self):
        resolver = Mock(side_effect=YouTubeRateLimitError(retry_after_seconds=7))
        self.repo.unknown = 'defer'
        unit = self.unit(resolve_channel=resolver)
        with self.assertRaises(DispatchBlocked):
            self.run_unit(unit)
        resolver.assert_called_once()
        self.assertEqual([event[0] for event in self.repo.events], ['claim', 'defer'])
        self.assertEqual(unit.phase, 'deferring')
        self.assertIsNone(unit.result)
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('fetch_error', error=YouTubeRateLimitError(retry_after_seconds=7)))

    def test_generic_resource_exhausted_rate_is_not_daily(self):
        outcome = classify_youtube_outcome(403, {'error': {'status':'RESOURCE_EXHAUSTED'}}, quota_bucket='general')
        self.response = reconnect_refusal(outcome)
        self.run_unit(self.unit())
        self.assertEqual(self.repo.events[-1][2], dict(reason='provider_rate_limited', retry_seconds=60))
        self.assertEqual(reconnect_refusal(ProviderOutcome('resource_missing','general')).code, 'channel_lookup_unavailable')
        self.assertEqual(reconnect_refusal(ProviderOutcome('resource_missing','general',resource='channel')).code, 'channel_not_found')

    def test_source_or_readiness_guard_prevents_bind_and_ack(self):
        for guard in ('source', 'ready'):
            with self.subTest(guard=guard):
                self.ready.reset_mock(side_effect=True)
                self.source.reset_mock(side_effect=True)
                if guard == 'source': self.source.side_effect = RuntimeError('source_stale')
                else: self.ready.side_effect = [None, RuntimeError('not_ready')]
                with self.assertRaises(DispatchBlocked): self.run_unit(self.unit())
        self.assertFalse(any(row[0]=='bind' for row in self.repo.events))

    def test_known_hot_refusal_after_fetch_aborts_only_original_pending_token(self):
        self.ready.side_effect = [None, HotRuntimeUnavailable()]
        unit = self.unit()
        self.run_unit(unit)
        self.assertEqual(unit.phase, 'acknowledged')
        self.assertEqual([row[0] for row in self.repo.events], ['claim', 'abort'])
        self.assertEqual(self.repo.events[-1][1], (ID, 7, TOKEN))

    def test_unknown_abort_never_acks_or_replays(self):
        self.ready.side_effect = [None, HotRuntimeUnavailable()]
        self.repo.unknown = 'abort'
        unit = self.unit()
        with self.assertRaises(DispatchBlocked):
            self.run_unit(unit)
        self.assertEqual(unit.phase, 'aborting')
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('response', value=self.response))
        self.assertEqual([row[0] for row in self.repo.events], ['claim', 'abort'])

    def test_definite_defer_admission_denial_uses_closed_abort_but_unknown_does_not(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                self.repo = Repository()
                error = HotDatabaseAdmissionDenied()
                error.outcome_may_be_committed = unknown
                self.repo.defer_reconnect = Mock(side_effect=error)
                self.response = ReconnectResponse(code='provider_unavailable')
                unit = self.unit()
                if unknown:
                    with self.assertRaises(DispatchBlocked):
                        self.run_unit(unit)
                    self.assertFalse(any(row[0]=='abort' for row in self.repo.events))
                else:
                    self.run_unit(unit)
                    self.assertEqual(self.repo.events[-1][0], 'abort')
                self.repo.defer_reconnect.assert_called_once()

    def test_merge_manual_and_lost_lease_only_use_existing_atomic_owner(self):
        for result in ({'outcome':'ready','merged':True}, {'outcome':'ready','manual_required':True},
                       {'outcome':'lease_lost'}):
            self.repo.binding = result
            unit = self.unit()
            self.run_unit(unit)
            self.assertEqual(unit.result, result)
            self.assertEqual(self.repo.events[-1][0], 'bind')

    def test_known_bind_rejection_releases_instead_of_acknowledging_held_lease(self):
        for outcome, reason, delay in (('busy','busy',60), ('conflict','busy',60),
                                      ('parent_pending','parent_pending',60),
                                      ('channel_unavailable','channel_lookup_unavailable',60),
                                      ('needs_input','needs_input',None)):
            self.repo.binding = {'outcome':outcome}
            self.run_unit(self.unit())
            self.assertEqual(self.repo.events[-1][2], dict(reason=reason,retry_seconds=delay))

    def test_unknown_claim_defer_bind_keep_credit_and_never_retry(self):
        for operation in ('claim', 'defer', 'bind'):
            with self.subTest(operation=operation), ThreadPoolExecutor(max_workers=1) as executor:
                self.repo = Repository()
                self.repo.unknown = operation
                self.response = (ReconnectResponse(code='provider_unavailable') if operation == 'defer'
                                 else ReconnectResponse(channel={'channel_id': CHANNEL}))
                unit = self.unit()
                coordinator = Coordinator(executor, concurrency=1)
                coordinator.offer(unit.work)
                with self.assertRaises(DispatchBlocked):
                    coordinator.step()
                    end = time.monotonic()+5
                    while True:
                        self.assertLess(time.monotonic(),end)
                        coordinator.wait(.1)
                        coordinator.step()
                before = list(self.repo.events)
                self.assertEqual(coordinator.snapshot().held,1)
                with self.assertRaises(DispatchBlocked): coordinator.step()
                self.assertEqual(self.repo.events,before)
                with self.assertRaisesRegex(RuntimeError,'not_repeatable'): unit.prepare()
                with self.assertRaisesRegex(RuntimeError,'not_repeatable'):
                    unit.settle(Completion('response',value=self.response))
                self.assertEqual(sum(row[0]==operation for row in before),1)

    def test_provider_admission_refusals_defer_once_without_unsent_or_bind(self):
        for error_type in (AdmissionUnavailable, AdmissionCancelled, AdmissionFull, AdmissionTimeout):
            with self.subTest(error_type=error_type.__name__):
                self.repo = Repository()
                resolver = Mock(side_effect=error_type('private admission detail'))
                observer = Mock()
                unit = self.unit(resolve_channel=resolver, on_settled=observer)
                self.assertEqual(self.run_unit(unit).held, 0)
                resolver.assert_called_once()
                self.assertEqual([name for name, _, _ in self.repo.events], ['claim', 'defer'])
                self.assertEqual(self.repo.events[-1][2],
                                 dict(reason='provider_unavailable', retry_seconds=60))
                self.assertEqual(observer.call_args.args[0], dict(outcome='ready',
                    reason='provider_unavailable', quota_exhausted=False, rate_limited=False))
                self.assertNotIn('private admission', repr(self.repo.events))
        self.source.assert_not_called()

    def test_admission_failure_after_partial_source_reads_never_binds_partial_row(self):
        from channels.reconnect_source import ReconnectSource
        from collection.channel_metadata import channel_metadata_update_values
        from test_reconnect_source import PROFILE, response
        request = Mock(side_effect=[response({'items': [PROFILE]}),
                                   AdmissionUnavailable('provider_admission_unavailable')])
        resolver = ReconnectSource(request, metadata_update_values=channel_metadata_update_values)
        self.assertEqual(self.run_unit(self.unit(resolve_channel=resolver)).held, 0)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['channels', 'playlistItems'])
        self.assertEqual([name for name, _, _ in self.repo.events], ['claim', 'defer'])
        self.assertEqual(self.repo.events[-1][2], dict(reason='provider_unavailable', retry_seconds=60))
        self.source.assert_not_called()

    def test_real_get_observe_failure_defers_without_repeating_http(self):
        from functools import partial
        from types import SimpleNamespace
        import os
        import provider_runtime
        import hot_runtime
        import youtube_client
        from channels.reconnect_source import ReconnectSource
        from collection.channel_metadata import channel_metadata_update_values
        from test_provider_transport_integration import Gate, Response
        gate = Gate()
        gate.on_outcome = Mock(side_effect=AdmissionUnavailable('uncertain local gate observation'))
        response = Response(gate)
        http, recorder = Mock(return_value=response), Mock()
        runtime = SimpleNamespace(pid=os.getpid(), stop=Event(), gate=gate)
        resolver = ReconnectSource(partial(youtube_client.youtube_api_get,
            api_key='synthetic', attempt_recorder=recorder),
            metadata_update_values=channel_metadata_update_values)
        with patch.object(provider_runtime, '_runtime', runtime), \
                patch.object(hot_runtime, 'require_hot_runtime_ready'), \
                patch.object(youtube_client.time_trust, 'request_started', return_value=100), \
                patch.object(youtube_client.time_trust, 'record_date'), \
                patch.object(youtube_client.requests, 'get', http):
            self.assertEqual(self.run_unit(self.unit(resolve_channel=resolver)).held, 0)
        http.assert_called_once()
        gate.on_outcome.assert_called_once()
        self.assertEqual(response.closes, 1)
        self.assertEqual(gate.active, 0)
        recorder.start_attempt.assert_called_once()
        recorder.finish_attempt.assert_called_once()
        self.assertEqual([name for name, _, _ in self.repo.events], ['claim', 'defer'])
        self.assertEqual(self.repo.events[-1][2], dict(reason='provider_unavailable', retry_seconds=60))
        self.source.assert_not_called()

    def test_provider_admission_deferral_unknown_commit_stays_blocked_without_retry(self):
        resolver = Mock(side_effect=AdmissionCancelled('provider_request_cancelled'))
        observer = Mock()
        self.repo.unknown = 'defer'
        unit = self.unit(resolve_channel=resolver, on_settled=observer)
        with self.assertRaises(DispatchBlocked):
            self.run_unit(unit)
        resolver.assert_called_once()
        observer.assert_not_called()
        self.assertEqual([name for name, _, _ in self.repo.events], ['claim', 'defer'])
        self.assertEqual(unit.phase, 'deferring')
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('fetch_error', error=AdmissionCancelled()))

    def test_unexpected_provider_exception_blocks_without_persisting_error_text(self):
        for error_type in (ValueError, RuntimeError):
            with self.subTest(error_type=error_type.__name__):
                self.repo = Repository()
                resolver = Mock(side_effect=error_type('secret provider response'))
                with self.assertRaisesRegex(DispatchBlocked, 'requires_reconciliation'):
                    self.run_unit(self.unit(resolve_channel=resolver))
                resolver.assert_called_once()
                self.assertEqual([row[0] for row in self.repo.events], ['claim'])

    def test_readiness_before_claim_never_claims_or_fetches(self):
        self.ready.side_effect = RuntimeError('not_ready')
        with self.assertRaises(DispatchBlocked): self.run_unit(self.unit())
        self.assertEqual(self.repo.events,[])
        self.assertEqual(self.provider_calls, [])

    def test_lease_bound_matches_existing_claim_owner(self):
        for seconds in (30, 600):
            self.assertEqual(self.unit(lease_seconds=seconds, clock=lambda: 100).prepare().deadline,
                             100 + seconds - 1)
        before = len(self.repo.events)
        for invalid in (29, 601, 3600, True):
            with self.assertRaises(ValueError): self.unit(lease_seconds=invalid)
        self.assertEqual(len(self.repo.events), before)

    def test_claim_elapsed_time_counts_towards_provider_deadline(self):
        unit = self.unit(clock=lambda:100,lease_seconds=30)
        self.assertEqual(unit.prepare().deadline,129)

    def test_invalid_or_unconfirmed_domain_results_never_ack(self):
        for phase in ('defer','bind'):
            self.repo = Repository()
            self.response = (ReconnectResponse(code='provider_unavailable') if phase == 'defer'
                             else ReconnectResponse(channel={'channel_id': CHANNEL}))
            if phase=='defer': self.repo.release={'outcome':'unknown'}
            else: self.repo.binding={'outcome':'not_found'}
            with self.assertRaises(DispatchBlocked): self.run_unit(self.unit())

    def test_defer_owner_accepts_25h_but_rejects_unbounded_delays(self):
        from test_user_work_reconnect_queue import Database
        from db.user_work import user_work_reconnect as queue
        database = Database()
        self.assertEqual(queue.defer_reconnect(database,ID,7,TOKEN,
            reason='provider_quota_exceeded',retry_seconds=90000),{'outcome':'ready'})
        self.assertEqual(database.calls[-1][1][1:3],(90000,90000))
        for bad in (0,90001,True,1.5):
            with self.assertRaises(ValueError):
                queue.defer_reconnect(database,ID,7,TOKEN,reason='provider_quota_exceeded',retry_seconds=bad)


if __name__ == '__main__':
    unittest.main()
