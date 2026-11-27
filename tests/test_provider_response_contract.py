"""Provider response and selection boundaries. All responses are synthetic."""
from datetime import timedelta
import unittest
from unittest.mock import patch

import provider_sources_support as support
from provider_sources.errors import PlanRejected
from provider_sources.registry import load_registry
from provider_sources_support import canonical, load, plan_for, run_case
from youtube_batch_stats import YouTubeBatchStatsError, normalize_batch_stats_response


class ProviderResponseContractTests(unittest.TestCase):
    def test_chat_poll_delay_matches_admission_and_keeps_a_resume_window(self):
        for delay_ms in (0, 90_001, 900_000, 3_600_000):
            with self.subTest(delay_ms=delay_ms):
                fixture = load('youtube.liveChatMessages.list', 'open_chat_poll')
                fixture['responses'][0]['body']['pollingIntervalMillis'] = delay_ms
                codec = support.ContinuationCodec()
                _, first, _ = run_case(fixture, codec=codec)
                self.assertEqual(first.retry_after_seconds, (delay_ms + 999) // 1000)
                due = support.NOW + timedelta(milliseconds=delay_ms)
                if delay_ms:
                    with patch.object(support, 'NOW', due - timedelta(milliseconds=1)):
                        with self.assertRaisesRegex(PlanRejected, 'continuation_too_early'):
                            run_case(fixture, codec=codec, continuation=first.continuation)
                for moment in (due, due + timedelta(minutes=9, seconds=59)):
                    with patch.object(support, 'NOW', moment):
                        _, _, sender = run_case(fixture, codec=codec, continuation=first.continuation)
                        self.assertEqual(sender.calls[0]['pageToken'], fixture['responses'][0]['body']['nextPageToken'])
                with patch.object(support, 'NOW', due + timedelta(minutes=10)):
                    with self.assertRaisesRegex(PlanRejected, 'continuation_expired'):
                        run_case(fixture, codec=codec, continuation=first.continuation)

    def test_zero_result_search_makes_one_request_and_reports_the_provider_estimate(self):
        for estimate in (0, 400, None):
            fixture = load('youtube.search.list', 'empty_list')
            fixture['request']['parameters']['maxResults'] = 0
            payload = fixture['responses'][0]['body']
            payload['nextPageToken'] = 'opaque-token'
            if estimate is None:
                del payload['pageInfo']['totalResults']
            else:
                payload['pageInfo']['totalResults'] = estimate
            plan, result, sender = run_case(fixture)
            self.assertEqual((plan.page_size, plan.max_pages, dict(plan.worst_cost)), (0, 1, {'search_queries': 1}))
            self.assertEqual([p['maxResults'] for p in sender.calls], ['0'])
            wire = canonical(result)
            self.assertEqual((wire['rows'], wire['completeness'], wire['continuation']), ([], 'complete', None))
            page = wire['pages'][0]
            self.assertEqual('estimated_total_results' in page, estimate is not None)
            self.assertEqual(page.get('estimated_total_results'), estimate)
        unexpected = load('youtube.search.list', 'mixed_two_pages')
        unexpected['request']['parameters']['maxResults'] = 0
        _, result, sender = run_case(unexpected)
        self.assertEqual((len(sender.calls), result.stop_reason, result.rows), (1, 'response_invalid', ()))

    def test_batch_summary_types_and_failed_ids_are_validated_before_conversion(self):
        video = 'abcdefghijk'
        stats = {'items': [], 'summary': {'requestedVideoCount': '1', 'failedVideoCount': 1,
                                           'succeededVideoCount': '0', 'failedVideoIds': [video]}}
        self.assertEqual(normalize_batch_stats_response(stats, [video])['summary']['failed_ids'], [video])
        for bad in (False, 0.9, 0.0, ' 0', '0.0', '-0', '+0', '0_0', '9' * 100):
            with self.subTest(count=bad):
                stats['summary']['succeededVideoCount'] = bad
                with self.assertRaises(YouTubeBatchStatsError) as caught:
                    normalize_batch_stats_response(stats, [video])
                self.assertEqual(caught.exception.code, 'summary_count_mismatch')
        stats['summary']['succeededVideoCount'] = 0
        for bad in ([video, video], [None, video], [' ' + video], [123], [{}]):
            with self.subTest(failures=bad):
                stats['summary']['failedVideoIds'] = bad
                with self.assertRaises(YouTubeBatchStatsError) as caught:
                    normalize_batch_stats_response(stats, [video])
                self.assertEqual(caught.exception.code, 'invalid_failed_ids_shape')

    def test_duration_milliseconds_agrees_between_registry_and_collector(self):
        for raw, expected in (('12534', 12534), (0, 0), (2**64 - 1, 2**64 - 1),
                              (False, None), (1.3, None), ('-100', None), (2**64, None)):
            fixture = load('youtube.videos.batchGetStats', 'full')
            selection = fixture['request']['selection']
            selection['fields'] = ['contentDetails.durationMillis']
            payload = fixture['responses'][0]['body']
            payload['items'][0]['contentDetails']['durationMillis'] = raw
            normalized = normalize_batch_stats_response(payload, fixture['request']['parameters']['id'])
            self.assertEqual(normalized['items'][0]['duration_ms'], expected)
            plan, result, _ = run_case(fixture)
            self.assertIn('durationMillis', plan.fields_mask)
            value = result.rows[0].fields['contentDetails.durationMillis']
            self.assertEqual(value.state.value, 'known' if expected is not None else 'invalid')
            self.assertEqual(value.value, expected)

    def test_vertical_thumbnails_are_explicit_selectable_fields_of_the_official_resources(self):
        registry = load_registry()
        names = ('activities', 'channels', 'liveBroadcasts', 'playlistItems', 'playlists', 'search',
                 'subscriptions', 'videos')
        expected = {(f'youtube.{name}.list', f'{part}.thumbnails.shortsAspectRatio.{leaf}')
                    for name in names for part in (('snippet', 'subscriberSnippet') if name == 'subscriptions'
                                                  else ('snippet',)) for leaf in ('url', 'width', 'height')}
        actual = {(method.id, field.path) for method in registry.methods.values() for field in method.fields.values()
                  if '.shortsAspectRatio.' in field.path}
        self.assertEqual(actual, expected)
        fixture = load('youtube.videos.list', 'minimal')
        prefix = 'snippet.thumbnails.shortsAspectRatio.'
        fixture['request']['selection'] = {'parts': ['snippet'], 'fields': [prefix + s for s in ('url','width','height')]}
        thumbnail = {'url': 'https://example.invalid/sample', 'width': 720, 'height': 1280}
        fixture['responses'][0]['body']['items'][0]['snippet'] = {'thumbnails': {'shortsAspectRatio': thumbnail}}
        plan, result, _ = run_case(fixture)
        self.assertIn('shortsAspectRatio(height,url,width)', plan.fields_mask)
        for name, value in thumbnail.items():
            self.assertEqual(result.rows[0].fields[prefix + name].value, value)

    def test_subscription_response_accepts_only_its_documented_and_observed_kinds(self):
        for kind in ('youtube#subscriptionListResponse', 'youtube#SubscriptionListResponse'):
            for case, expected_rows in (('by_id', 1), ('empty_list', 0)):
                with self.subTest(kind=kind, case=case):
                    fixture = load('youtube.subscriptions.list', case)
                    fixture['responses'][0]['body']['kind'] = kind
                    _, result, _ = run_case(fixture)
                    self.assertEqual(result.completeness, 'complete')
                    self.assertEqual(len(result.rows), expected_rows)
        for method, case, kind in (
                ('youtube.subscriptions.list', 'by_id', 'youtube#channelListResponse'),
                ('youtube.subscriptions.list', 'by_id', 'youtube#SUBSCRIPTIONLISTRESPONSE'),
                ('youtube.videos.list', 'minimal', 'youtube#SubscriptionListResponse')):
            with self.subTest(method=method, kind=kind):
                fixture = load(method, case)
                fixture['responses'][0]['body']['kind'] = kind
                _, result, _ = run_case(fixture)
                self.assertEqual((result.stop_reason, result.rows), ('response_invalid', ()))

    def test_registry_separates_specialized_search_ui_and_the_closed_posts_filter(self):
        registry = load_registry()
        self.assertEqual(registry.methods['youtube.search.list'].states['ui_supported'], 'partial')
        req = load('youtube.commentThreads.list', 'by_id')['request']
        req['parameters'] = {'postId': 'synthetic-post'}
        with self.assertRaisesRegex(PlanRejected, 'context_not_supported'):
            plan_for(req)
