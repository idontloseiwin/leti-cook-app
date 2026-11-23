"""Fault matrix of one-off acquisition through the real shared transport.

Only requests.get, the admission permit, HOT readiness and the clock check are replaced; youtube_client,
classification, the quota attempt journal calls and the source layer run as in production. No network.
"""
from datetime import datetime, timedelta, timezone
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import requests  # noqa: E402
import provider_runtime  # noqa: E402
import youtube_client  # noqa: E402
from provider_admission import AdmissionFull  # noqa: E402
from provider_credentials import OwnerBearer  # noqa: E402
from provider_sources.acquire import acquire, refusal_outcome  # noqa: E402
from provider_sources.continuation import ContinuationCodec  # noqa: E402
from provider_sources.credentials import Credential, OwnerBinding, OwnerContext, PublicKeyContext  # noqa: E402
from provider_sources.daily_cap import CapSessions, CapUnavailable, DailyCap  # noqa: E402
from provider_sources.errors import PlanRejected  # noqa: E402
from provider_sources.plan import AcquisitionRequest, Budget, plan_request  # noqa: E402
from provider_sources.registry import load_registry  # noqa: E402
from provider_sources.results import Completeness, EntityState, ValueState  # noqa: E402
from provider_sources.selection import FieldSelection  # noqa: E402
from self_hosted_configuration import ConfigurationStore  # noqa: E402

CHANNEL = 'UC' + 'a' * 22
PLAYLIST_KIND = 'youtube#playlistListResponse'
KEY = 'synthetic_installation_key_no_requests'
TOKEN = 'synthetic-owner-token-' + 'z' * 20
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
OWNER = OwnerContext(OwnerBinding('binding-1', CHANNEL, frozenset({'https://www.googleapis.com/auth/youtube.readonly'}),
                                  'synthetic-project'))


class Hot(Exception):
    pass


class Response(requests.Response):
    def __init__(self, status=200, body=None, *, raw=None, headers=None, chunks=None):
        super().__init__()
        self.status_code = status
        self.headers.update(headers or {'Date': 'Thu, 29 Oct 2026 10:00:00 GMT'})
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode('utf-8')
        self.chunks = chunks if chunks is not None else (data,)

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            yield chunk() if callable(chunk) else chunk

    def close(self):
        pass


def playlist(index):
    return {'kind': 'youtube#playlist', 'id': f'PLsyntheticPlaylist{index:015d}',
            'snippet': {'title': f'Series {index}', 'publishedAt': '2026-01-02T03:04:05Z'},
            'status': {'privacyStatus': 'public'}, 'contentDetails': {'itemCount': index}}


def page(items, token=None, *, kind=PLAYLIST_KIND, total=None):
    body = {'kind': kind, 'pageInfo': {'totalResults': len(items) if total is None else total,
                                       'resultsPerPage': len(items)}, 'items': items}
    if token is not None:
        body['nextPageToken'] = token
    return Response(200, body)


def refusal(status, reason, headers=None):
    return Response(status, {'error': {'code': status, 'errors': [{'reason': reason}]}}, headers=headers)


class Recorder:
    def __init__(self):
        self.started, self.finished = [], []

    def start_attempt(self, **values):
        self.started.append(values)
        return f'00000000-0000-4000-8000-{len(self.started):012d}'

    def finish_attempt(self, attempt_id, **values):
        self.finished.append((attempt_id, values))
        return True


class AcquireCase(unittest.TestCase):
    def setUp(self):
        self.queue, self.calls = [], []
        self.recorder = Recorder()
        self.admit = lambda **kw: contextlib.nullcontext(provider_runtime._UnregulatedRequest(kw.get('cancel')))
        hot = types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None, HotRuntimeUnavailable=Hot)
        for item in (patch.object(youtube_client.requests, 'get', self.http),
                     patch('provider_runtime.admit_request', side_effect=lambda **kw: self.admit(**kw)),
                     patch.dict(sys.modules, {'hot_runtime': hot}),
                     patch.object(youtube_client.time_trust, 'record_date')):
            item.start()
            self.addCleanup(item.stop)

    def http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def plan(self, method='youtube.playlists.list', parameters=None, parts=('snippet', 'status', 'contentDetails'),
             fields=(), budget=Budget(), auth=PublicKeyContext()):
        return plan_request(AcquisitionRequest(method, parameters or {'channelId': CHANNEL},
                                               FieldSelection(frozenset(parts), frozenset(fields)), auth, budget),
                            load_registry())

    def go(self, plan=None, *, credential=None, cancelled=None, deadline=60, cap=None, codec=None, continuation=None):
        plan = plan or self.plan()
        return acquire(plan, registry=load_registry(), credential=credential or Credential('public_key', None),
                       cancelled=cancelled or threading.Event(), deadline=time.monotonic() + deadline,
                       attempt_recorder=self.recorder, cap=cap, codec=codec or ContinuationCodec(),
                       continuation=continuation, clock=lambda: NOW)


class TransportFailureTests(AcquireCase):
    def test_timeout_and_connection_errors_are_transient_and_spent(self):
        for error in (requests.ReadTimeout('synthetic'), requests.ConnectionError('synthetic')):
            with self.subTest(error=type(error).__name__):
                self.queue[:] = [error]
                result = self.go()
                self.assertEqual((result.completeness, result.stop_reason), (Completeness.PARTIAL, 'transient'))
                self.assertEqual(dict(result.cost), {'general': 1})
                self.assertIsNotNone(result.continuation)   # the same page may be asked again later

    def test_cancel_before_any_request_sends_nothing(self):
        event = threading.Event()
        event.set()
        result = self.go(cancelled=event)
        self.assertEqual((result.stop_reason, result.requests_sent, self.calls), ('cancelled', 0, []))

    def test_cancel_while_reading_the_body(self):
        event = threading.Event()
        self.queue[:] = [Response(chunks=(lambda: event.set() or b'{"kind":',))]
        result = self.go(cancelled=event)
        self.assertEqual((result.stop_reason, dict(result.cost)), ('cancelled', {'general': 1}))
        self.assertEqual(result.rows, ())

    def test_expired_deadline_sends_nothing(self):
        result = self.go(deadline=-1)
        self.assertEqual((result.stop_reason, self.calls, self.recorder.started), ('deadline', [], []))

    def test_full_admission_queue_sends_nothing(self):
        def full(**kwargs):
            raise AdmissionFull('provider_admission_full')
        self.admit = full
        result = self.go()
        self.assertEqual((result.stop_reason, dict(result.cost), self.calls), ('provider_busy', {}, []))

    def test_oversized_body_stops_without_values(self):
        plan = self.plan(budget=Budget(max_requests=1, max_units={'general': 1}, max_rows=50, max_response_bytes=32))
        big = b'{"kind":"' + b'x' * 64 + b'"}'
        self.queue[:] = [Response(raw=big, headers={'Content-Length': str(len(big))})]
        result = self.go(plan)
        self.assertEqual((result.stop_reason, result.rows, dict(result.cost)), ('response_too_large', (), {'general': 1}))

    def test_server_errors_are_transient(self):
        for status in (500, 503):
            self.queue[:] = [Response(status, {'error': {'code': status, 'errors': [{'reason': 'backendError'}]}})]
            self.assertEqual(self.go().stop_reason, 'transient')


class RefusalTableTests(AcquireCase):
    def test_daily_quota_in_each_bucket(self):
        self.queue[:] = [refusal(403, 'quotaExceeded')]
        self.assertEqual(self.go().stop_reason, 'quota_exhausted:general')
        batch = self.plan('youtube.videos.batchGetStats', {'id': ['abcdefghijk']}, parts=('statistics',))
        self.queue[:] = [refusal(403, 'quotaExceeded')]
        self.assertEqual(self.go(batch).stop_reason, 'quota_exhausted:video_batch_get_stats')
        search = types.SimpleNamespace(quota_bucket='search_queries', owner=False, api='data')
        self.assertEqual(refusal_outcome(403, {'error': {'errors': [{'reason': 'dailyLimitExceeded'}]}}, plan=search,
                                         retry_after=None, now=NOW)[0], 'quota_exhausted:search_queries')

    def test_rate_limit_keeps_a_bounded_retry_after_without_retrying(self):
        self.queue[:] = [refusal(429, 'rateLimitExceeded', {'Retry-After': '17', 'Date': 'x'})]
        result = self.go()
        self.assertEqual((result.stop_reason, result.retry_after_seconds, len(self.calls)), ('rate_limited', 17, 1))
        self.queue[:] = [refusal(403, 'userRateLimitExceeded', {'Retry-After': 'Thu, 29 Oct 2026 10:00:30 GMT'})]
        result = self.go()
        self.assertEqual((result.stop_reason, result.retry_after_seconds), ('rate_limited', 30))

    def test_access_and_target_refusals(self):
        self.queue[:] = [refusal(403, 'accessNotConfigured')]
        self.assertEqual(self.go().stop_reason, 'credential_access_denied')
        items = self.plan('youtube.playlistItems.list', {'playlistId': 'PLsyntheticPrivate0000001'}, parts=('contentDetails',))
        self.queue[:] = [refusal(403, 'playlistItemsNotAccessible')]
        result = self.go(items)
        self.assertEqual((result.completeness, dict(result.entities)),
                         (Completeness.COMPLETE, {'PLsyntheticPrivate0000001': EntityState.ACCESS_DENIED}))
        other = types.SimpleNamespace(quota_bucket='general', owner=False, api='data')
        for reason, state in (('commentsDisabled', EntityState.DISABLED), ('subscriptionForbidden', EntityState.ACCESS_DENIED),
                              ('watchHistoryNotAccessible', EntityState.ACCESS_DENIED), ('forbidden', EntityState.ACCESS_DENIED)):
            self.assertEqual(refusal_outcome(403, {'error': {'errors': [{'reason': reason}]}}, plan=other,
                                             retry_after=None, now=NOW), (None, state, None))

    def test_not_found_with_and_without_a_reason(self):
        self.queue[:] = [refusal(404, 'channelNotFound')]
        result = self.go()
        self.assertEqual((result.completeness, dict(result.entities)), (Completeness.COMPLETE, {CHANNEL: EntityState.NOT_FOUND}))
        self.queue[:] = [Response(404, {'error': {'code': 404}})]
        result = self.go()
        self.assertEqual((result.completeness, result.stop_reason), (Completeness.PARTIAL, 'not_found_unclassified'))

    def test_plain_bad_request_is_a_plan_error_signal(self):
        self.queue[:] = [refusal(400, 'invalidParameter')]
        self.assertEqual(self.go().stop_reason, 'request_invalid')
        analytics = types.SimpleNamespace(quota_bucket='youtube_analytics', owner=True, api='analytics')
        self.assertEqual(refusal_outcome(400, {'error': {'message': 'The query is not supported.'}}, plan=analytics,
                                         retry_after=None, now=NOW)[0], 'combination_unsupported')


class CredentialTests(AcquireCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETUP_DIRECTORY': directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        store = ConfigurationStore.initialize(directory.name)
        store.update(store.read()['revision'], lambda v: v.update(
            api_key=KEY, prepared=True, phase='ready', workspace={'phase': 'complete', 'channels': {}}))
        self.directory = directory.name

    def stored_error(self):
        return ConfigurationStore(self.directory).read()['error']

    def test_owner_token_401_stops_and_never_marks_the_installation_key(self):
        plan = self.plan(parameters={'mine': True}, auth=OWNER)
        self.queue[:] = [Response(401, {'error': {'code': 401, 'status': 'UNAUTHENTICATED'}})]
        result = self.go(plan, credential=Credential(OWNER.label, OwnerBearer(TOKEN)))
        self.assertEqual(result.stop_reason, 'owner_auth_expired')
        self.assertEqual(self.calls[0][1]['headers'], {'Authorization': 'Bearer ' + TOKEN})
        self.assertIsNone(self.stored_error())
        self.assertEqual(result.pages[0].context, 'owner:binding-1')

    def test_installation_key_refusal_stops_and_marks_the_key(self):
        self.queue[:] = [Response(400, {'error': {'code': 400, 'details': [{'reason': 'API_KEY_INVALID'}]}})]
        result = self.go()
        self.assertEqual(result.stop_reason, 'credential_rejected')
        self.assertEqual(self.stored_error(), 'provider_key_rejected')
        self.queue[:] = [page([playlist(1)])]
        self.assertEqual(self.go().stop_reason, 'credential_rejected')   # nothing more is sent with that key
        self.assertEqual(len(self.calls), 1)


class PageContractTests(AcquireCase):
    def test_short_page_with_a_token_continues(self):
        plan = self.plan(parameters={'channelId': CHANNEL, 'maxResults': 3}, budget=Budget(max_units={'general': 3}))
        self.queue[:] = [page([playlist(1)], 'NEXT-1', total=4), page([playlist(2), playlist(3), playlist(4)])]
        result = self.go(plan)
        self.assertEqual((result.completeness, len(result.rows), [call[1]['params'].get('pageToken') for call in self.calls]),
                         (Completeness.COMPLETE, 4, [None, 'NEXT-1']))

    def test_broken_continuations_reject_the_page(self):
        for name, pages in (('empty page with a token', [page([], 'NEXT')]),
                            ('repeated token', [page([playlist(1)], 'SAME'), page([playlist(2)], 'SAME')]),
                            ('token longer than 2048', [page([playlist(1)], 'x' * 2049)])):
            with self.subTest(name=name):
                self.queue[:] = pages
                result = self.go(self.plan(budget=Budget(max_units={'general': 3})))
                self.assertEqual((result.completeness, result.stop_reason), (Completeness.PARTIAL, 'response_invalid'))

    def test_budget_cut_and_continuation_resume_the_exact_next_page(self):
        codec = ContinuationCodec()
        plan = self.plan(parameters={'channelId': CHANNEL, 'maxResults': 1}, budget=Budget(max_requests=2, max_units={'general': 2}))
        self.queue[:] = [page([playlist(1)], 'TOKEN-A', total=3), page([playlist(2)], 'TOKEN-B', total=3)]
        first = self.go(plan, codec=codec)
        self.assertEqual((first.stop_reason, len(first.rows), dict(first.cost)), ('budget_requests', 2, {'general': 2}))
        self.assertNotIn('TOKEN-B', first.continuation)
        self.queue[:] = [page([playlist(3)])]
        second = self.go(plan, codec=codec, continuation=first.continuation)
        self.assertEqual((second.completeness, second.rows[0].identity[1]), (Completeness.COMPLETE, playlist(3)['id']))
        self.assertEqual(self.calls[-1][1]['params']['pageToken'], 'TOKEN-B')
        self.assertEqual(second.pages[0].page_index, 2)
        for token, code in ((first.continuation[:-2] + 'xx', 'continuation_invalid'),):
            with self.assertRaisesRegex(PlanRejected, code):
                self.go(plan, codec=codec, continuation=token)
        other = self.plan(parameters={'channelId': CHANNEL, 'maxResults': 2}, budget=Budget(max_requests=2, max_units={'general': 2}))
        with self.assertRaisesRegex(PlanRejected, 'continuation_plan_changed'):
            self.go(other, codec=codec, continuation=first.continuation)
        with self.assertRaisesRegex(PlanRejected, 'continuation_expired'):
            acquire(plan, registry=load_registry(), credential=Credential('public_key', None), cancelled=threading.Event(),
                    deadline=time.monotonic() + 5, attempt_recorder=self.recorder, codec=codec,
                    continuation=first.continuation, clock=lambda: NOW + timedelta(minutes=11))

    def test_contract_violations_reject_the_page(self):
        batch = self.plan('youtube.videos.batchGetStats', {'id': ['abcdefghijk']}, parts=('statistics',))
        video = {'kind': 'youtube#videoStat', 'id': 'abcdefghijk', 'statistics': {'viewCount': '1'}}
        cases = {
            'invalid JSON': (self.plan(), Response(raw=b'{"kind": ')),
            'duplicate JSON keys': (self.plan(), Response(raw=b'{"kind":"youtube#playlistListResponse","kind":"x","items":[]}')),
            'foreign kind': (self.plan(), page([playlist(1)], kind='youtube#videoListResponse')),
            'repeated row in one page': (self.plan(), page([playlist(1), playlist(1)])),
            'foreign id in a batch': (batch, Response(200, {'kind': 'youtube#batchGetStatsResponse', 'items': [
                dict(video, id='zzzzzzzzzzz')]})),
            'repeated id in a batch': (batch, Response(200, {'kind': 'youtube#batchGetStatsResponse', 'items': [video, video]})),
            'NaN constant': (self.plan(), Response(raw=b'{"kind":"youtube#playlistListResponse","items":[],"x":NaN}')),
        }
        for name, (plan, response) in cases.items():
            with self.subTest(name=name):
                self.queue[:] = [response]
                result = self.go(plan)
                self.assertEqual((result.stop_reason, result.rows), ('response_invalid', ()))

    def test_value_states_for_bad_values(self):
        item = playlist(1)
        item['status']['privacyStatus'] = 'secretish'
        item['contentDetails']['itemCount'] = -3
        item['snippet']['publishedAt'] = '2026-01-02T03:04:05'
        item['snippet']['newField'] = 'dropped'
        self.queue[:] = [page([item])]
        result = self.go()
        fields = result.rows[0].fields
        self.assertEqual(fields['status.privacyStatus'].state, ValueState.UNKNOWN_ENUM)
        self.assertEqual(fields['contentDetails.itemCount'].state, ValueState.INVALID)
        self.assertEqual(fields['snippet.publishedAt'].state, ValueState.INVALID)
        self.assertEqual(result.pages[0].unknown_field_paths, ('snippet.newField',))
        batch = self.plan('youtube.videos.batchGetStats', {'id': ['abcdefghijk']}, parts=('statistics',))
        self.queue[:] = [Response(200, {'kind': 'youtube#batchGetStatsResponse', 'items': [
            {'id': 'abcdefghijk', 'statistics': {'viewCount': 12.5, 'likeCount': '-1', 'commentCount': '07'}}]})]
        fields = self.go(batch).rows[0].fields
        self.assertEqual(fields['statistics.viewCount'].state, ValueState.INVALID)       # float instead of a string
        self.assertEqual(fields['statistics.likeCount'].state, ValueState.INVALID)       # negative counter
        self.assertEqual(fields['statistics.commentCount'], fields['statistics.commentCount'].__class__(ValueState.KNOWN, 7))


class DailyCapTests(AcquireCase):
    def test_the_cap_counts_the_ledger_and_undelivered_local_attempts(self):
        ledger = [('00000000-0000-4000-8000-000000000099', 495)]   # an earlier acquisition today
        cap = DailyCap(lambda day: (list(ledger), False), clock=lambda: NOW)
        self.assertEqual(cap.reading()['remaining_units'], 5)
        session = cap.session()
        self.queue[:] = [page([playlist(i)], f'T{i}', total=9) for i in range(1, 8)]
        result = self.go(self.plan(parameters={'channelId': CHANNEL, 'maxResults': 1}), cap=session)
        self.assertEqual((result.stop_reason, dict(result.cost)), ('daily_cap', {'general': 5}))
        reading = cap.reading()
        self.assertEqual((reading['used_units'], reading['local_unconfirmed_units'], reading['remaining_units']), (500, 5, 0))
        # Once the journal delivers the rows, they are counted once.
        ledger.extend((f'00000000-0000-4000-8000-{index:012d}', 1) for index in range(1, 6))
        self.assertEqual(cap.reading()['local_unconfirmed_units'], 0)
        self.assertEqual(cap.reading()['used_units'], 500)

    def test_unreadable_or_truncated_ledger_fails_closed(self):
        def broken(day):
            raise RuntimeError('database unavailable')
        self.assertEqual(DailyCap(broken, clock=lambda: NOW).reading()['read_status'], 'unavailable')
        with self.assertRaises(CapUnavailable):
            DailyCap(broken, clock=lambda: NOW).session()
        with self.assertRaises(CapUnavailable):
            DailyCap(lambda day: ([], True), clock=lambda: NOW).session()

    def test_the_cap_applies_to_the_general_bucket_and_the_pacific_day(self):
        days = []
        cap = DailyCap(lambda day: days.append(day) or ([], False), clock=lambda: datetime(2026, 10, 29, 6, 59, tzinfo=timezone.utc))
        session = cap.session()
        self.assertEqual(days, ['2026-10-28'])                     # still the 28th in Los Angeles
        self.assertTrue(session.allows('video_batch_get_stats', 10_000))
        self.assertTrue(session.allows('general', 500))
        self.assertFalse(session.allows('general', 501))


def search_page(items, token=None):
    body = {'kind': 'youtube#searchListResponse', 'regionCode': 'US',
            'pageInfo': {'totalResults': 1000, 'resultsPerPage': len(items)}, 'items': items}
    if token is not None:
        body['nextPageToken'] = token
    return Response(200, body)


def search_hit(index):
    return {'kind': 'youtube#searchResult', 'id': {'kind': 'youtube#video', 'videoId': f'vid{index:08d}'},
            'snippet': {'title': f'hit {index}'}}


class PublicFamilyFaultTests(AcquireCase):
    """Documented refusals of the public families through the real shared transport."""

    def test_search_spends_and_journals_its_own_bucket(self):
        plan = self.plan('youtube.search.list', {'q': 'synthetic', 'maxResults': 1}, parts=('snippet',))
        self.queue[:] = [search_page([search_hit(1)], 'T2'), search_page([search_hit(2)])]
        result = self.go(plan)
        self.assertEqual((result.completeness, dict(result.cost)), (Completeness.COMPLETE, {'search_queries': 2}))
        self.assertEqual({(item['endpoint'], item['quota_bucket'], item['quota_units'], item['phase'])
                          for item in self.recorder.started}, {('search', 'search_queries', 1, 'explicit_acquisition')})
        self.assertTrue(self.calls[0][0].endswith('/youtube/v3/search'))
        self.queue[:] = [refusal(403, 'quotaExceeded')]    # Classified in its bucket, no ValueError
        self.assertEqual(self.go(plan).stop_reason, 'quota_exhausted:search_queries')

    def test_target_states_of_comments_subscriptions_and_chats(self):
        cases = (
            ('youtube.commentThreads.list', {'videoId': 'abcdefghijk'}, 403, 'commentsDisabled', 'abcdefghijk', EntityState.DISABLED),
            ('youtube.commentThreads.list', {'allThreadsRelatedToChannelId': CHANNEL}, 404, 'channelNotFound', CHANNEL,
             EntityState.NOT_FOUND),
            ('youtube.subscriptions.list', {'channelId': CHANNEL}, 403, 'subscriptionForbidden', CHANNEL, EntityState.ACCESS_DENIED),
            ('youtube.subscriptions.list', {'channelId': CHANNEL}, 404, 'subscriberNotFound', CHANNEL, EntityState.NOT_FOUND),
            ('youtube.channelSections.list', {'channelId': CHANNEL}, 403, 'channelSectionForbidden', CHANNEL,
             EntityState.ACCESS_DENIED),
            ('youtube.liveChatMessages.list', {'liveChatId': 'Cg0KC2hHUFVs'}, 403, 'liveChatEnded', 'Cg0KC2hHUFVs', EntityState.ENDED),
            ('youtube.liveChatMessages.list', {'liveChatId': 'Cg0KC2hHUFVs'}, 403, 'liveChatDisabled', 'Cg0KC2hHUFVs',
             EntityState.DISABLED),
            ('youtube.liveChatMessages.list', {'liveChatId': 'Cg0KC2hHUFVs'}, 404, 'liveChatNotFound', 'Cg0KC2hHUFVs',
             EntityState.NOT_FOUND),
            ('youtube.playlistItems.list', {'playlistId': 'PLsyntheticList001', 'videoId': 'abcdefghijk'}, 404, 'videoNotFound',
             'abcdefghijk', EntityState.NOT_FOUND),
        )
        for method, parameters, status, reason, target, state in cases:
            with self.subTest(method=method, reason=reason):
                self.queue[:] = [refusal(status, reason)]
                result = self.go(self.plan(method, parameters, parts=('snippet',)))
                self.assertEqual((result.completeness, dict(result.entities)), (Completeness.COMPLETE, {target: state}))

    def test_request_refusals_with_their_own_stop_codes(self):
        for method, parameters, reason, code in (
                ('youtube.comments.list', {'id': ['Ugxsynthetic01']}, 'operationNotSupported', 'operation_not_supported'),
                ('youtube.search.list', {'q': 'x'}, 'invalidSearchFilter', 'search_filter_invalid'),
                ('youtube.videos.list', {'chart': 'mostPopular', 'regionCode': 'AQ'}, 'videoChartNotFound', 'chart_not_available')):
            with self.subTest(reason=reason):
                self.queue[:] = [refusal(400, reason)]
                plan = self.plan(method, parameters, parts=('snippet',))
                result = self.go(plan)
                self.assertEqual((result.completeness, result.stop_reason, result.continuation),
                                 (Completeness.PARTIAL, code, None))

    def test_a_chat_polled_too_early_is_rate_limited_without_a_retry(self):
        plan = self.plan('youtube.liveChatMessages.list', {'liveChatId': 'Cg0KC2hHUFVs'}, parts=('snippet',))
        self.queue[:] = [refusal(403, 'rateLimitExceeded', {'Retry-After': '5', 'Date': 'x'})]
        result = self.go(plan)
        self.assertEqual((result.stop_reason, result.retry_after_seconds, len(self.calls), dict(result.cost)),
                         ('rate_limited', 5, 1, {'general': 5}))

    def test_a_poll_with_a_broken_interval_or_token_is_rejected(self):
        plan = self.plan('youtube.liveChatMessages.list', {'liveChatId': 'Cg0KC2hHUFVs'}, parts=('snippet',))
        for body in ({'kind': 'youtube#liveChatMessageListResponse', 'items': [], 'pollingIntervalMillis': '5000'},
                     {'kind': 'youtube#liveChatMessageListResponse', 'items': [], 'nextPageToken': 'x' * 2049}):
            with self.subTest(body=sorted(body)):
                self.queue[:] = [Response(200, body)]
                self.assertEqual(self.go(plan).stop_reason, 'response_invalid')


class SearchDailyCapTests(AcquireCase):
    def test_the_search_cap_counts_its_own_bucket_and_day(self):
        ledger = [('00000000-0000-4000-8000-000000000099', 99)]
        search = DailyCap(lambda day: (list(ledger), False), limit=100, clock=lambda: NOW, bucket='search_queries')
        general = DailyCap(lambda day: ([], False), clock=lambda: NOW)
        sessions = CapSessions([general.session(), search.session()])
        self.assertTrue(sessions.allows('general', 500) and sessions.allows('search_queries', 1))
        self.assertFalse(sessions.allows('search_queries', 2))
        plan = self.plan('youtube.search.list', {'q': 'synthetic', 'maxResults': 1}, parts=('snippet',))
        self.queue[:] = [search_page([search_hit(index)], f'T{index}') for index in range(1, 4)]
        result = self.go(plan, cap=sessions)
        self.assertEqual((result.stop_reason, dict(result.cost), len(self.calls)), ('daily_cap', {'search_queries': 1}, 1))
        self.assertEqual((search.reading()['used_units'], search.reading()['remaining_units'], search.reading()['bucket']),
                         (100, 0, 'search_queries'))
        self.assertEqual(general.reading()['used_units'], 0)     # a search never counts against the general cap


class MalformedShapeTests(AcquireCase):
    """Valid JSON of the wrong shape rejects a row or the page; nothing raises after the quota was spent."""

    def test_a_search_hit_whose_kind_is_not_text_rejects_only_that_row(self):
        plan = self.plan('youtube.search.list', {'q': 'synthetic', 'maxResults': 2}, parts=('snippet',))
        for kind in ([], {}, ['youtube#video'], 7, None):
            with self.subTest(kind=kind):
                odd = search_hit(1)
                odd['id']['kind'] = kind
                self.queue[:] = [search_page([odd, search_hit(2)])]
                result = self.go(plan)
                self.assertEqual((result.completeness, [row.identity for row in result.rows], result.pages[0].rejected_items),
                                 (Completeness.COMPLETE, [('video', 'vid00000002')], 1))

    def test_a_batch_item_part_that_is_not_an_object_rejects_the_page(self):
        plan = self.plan('youtube.videos.batchGetStats', {'id': ['abcdefghijk']}, parts=('statistics',))
        for part in ('statistics', 'snippet', 'contentDetails'):
            for value in ('x', 7, [{}], True):
                with self.subTest(part=part, value=value):
                    item = {'kind': 'youtube#videoStat', 'id': 'abcdefghijk', 'statistics': {'viewCount': '1'}, part: value}
                    self.queue[:] = [Response(200, {'kind': 'youtube#batchGetStatsResponse', 'items': [item]})]
                    result = self.go(plan)
                    self.assertEqual((result.completeness, result.stop_reason, result.rows, result.notes, dict(result.cost)),
                                     (Completeness.PARTIAL, 'response_invalid', (), ('batch_invalid_items_shape',),
                                      {'video_batch_get_stats': 1}))

    def test_server_error_and_rate_limit_keep_a_bounded_retry_after_and_a_continuation(self):
        for status, reason, header, seconds in ((503, 'backendError', '7', 7), (500, 'internalError', None, None),
                                                (429, 'rateLimitExceeded', '3600', 60),
                                                (429, 'rateLimitExceeded', 'soon', None),
                                                (503, 'backendError', 'Thu, 29 Oct 2026 10:00:12 GMT', 12)):
            with self.subTest(status=status, header=header):
                headers = {'Date': 'Thu, 29 Oct 2026 10:00:00 GMT', **({'Retry-After': header} if header else {})}
                self.queue[:] = [refusal(status, reason, headers)]
                result = self.go()
                self.assertEqual((result.completeness, result.stop_reason, result.retry_after_seconds, len(self.calls)),
                                 (Completeness.PARTIAL, 'rate_limited' if status == 429 else 'transient', seconds, 1))
                self.assertIsNotNone(result.continuation)     # the same page may be asked again later
                self.calls.clear()

    def test_deadline_while_reading_the_body_stops_spent_and_resumable(self):
        def slow():
            time.sleep(0.3)
            return b'{"kind":'
        self.queue[:] = [Response(chunks=(slow, b'"x"}'))]
        result = self.go(deadline=0.2)
        self.assertEqual((result.stop_reason, result.rows, dict(result.cost)), ('deadline', (), {'general': 1}))
        self.assertEqual(self.recorder.finished[0][1]['status'], 'transport_error')
        self.assertIsNotNone(result.continuation)

    def test_oversized_chunked_body_without_a_length_stops_without_values(self):
        plan = self.plan(budget=Budget(max_requests=1, max_units={'general': 1}, max_rows=50, max_response_bytes=64))
        self.queue[:] = [Response(chunks=(b'{"kind":"youtube#playlistListResponse",', b'"items":[' + b' ' * 64, b']}'),
                                  headers={'Date': 'Thu, 29 Oct 2026 10:00:00 GMT'})]
        result = self.go(plan)
        self.assertEqual((result.stop_reason, result.rows, dict(result.cost)), ('response_too_large', (), {'general': 1}))

    def test_owner_401_leaves_the_installation_key_usable(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETUP_DIRECTORY': directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        store = ConfigurationStore.initialize(directory.name)
        store.update(store.read()['revision'], lambda v: v.update(
            api_key=KEY, prepared=True, phase='ready', workspace={'phase': 'complete', 'channels': {}}))
        owner = self.plan(parameters={'mine': True}, auth=OWNER)
        self.queue[:] = [Response(401, {'error': {'code': 401, 'status': 'UNAUTHENTICATED'}}), page([playlist(1)])]
        self.assertEqual(self.go(owner, credential=Credential(OWNER.label, OwnerBearer(TOKEN))).stop_reason,
                         'owner_auth_expired')
        public = self.go()
        self.assertEqual((public.completeness, len(public.rows)), (Completeness.COMPLETE, 1))
        self.assertEqual(self.calls[1][1]['headers'], {'X-Goog-Api-Key': KEY})
        self.assertIsNone(ConfigurationStore(directory.name).read()['error'])


if __name__ == '__main__':
    unittest.main()
