"""Pure planning = the preview. No network, no quota, no transport import."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from provider_sources.credentials import OwnerBinding, OwnerContext, PublicKeyContext  # noqa: E402
from provider_sources.errors import PlanRejected  # noqa: E402
from provider_sources.families import policy  # noqa: E402
from provider_sources.families.hooks import HOOKS  # noqa: E402
from provider_sources.plan import AcquisitionRequest, Budget, plan_request  # noqa: E402
from provider_sources.registry import load_registry  # noqa: E402
from provider_sources.selection import FieldSelection  # noqa: E402

CHANNEL = 'UC' + 'a' * 22
VIDEO = 'abcdefghijk'
OWNER = OwnerContext(OwnerBinding('binding-1', CHANNEL, frozenset({'https://www.googleapis.com/auth/youtube.readonly'}),
                                  'synthetic-project'))


def plan(method, parameters, parts=('snippet',), fields=(), auth=PublicKeyContext(), budget=Budget()):
    return plan_request(AcquisitionRequest(method, parameters, FieldSelection(frozenset(parts), frozenset(fields)),
                                           auth, budget), load_registry())


class RefusalTests(unittest.TestCase):
    def refused(self, code, *args, **kwargs):
        with self.assertRaises(PlanRejected) as raised:
            plan(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_unknown_control_and_discovery_only_methods(self):
        self.refused('method_unknown', 'youtube.nothing.list', {})
        self.refused('method_control_not_readable', 'youtube.playlists.insert', {})
        self.refused('method_discovery_only', 'youtube.videoTrainability.get', {'id': VIDEO})
        self.refused('context_not_supported', 'youtubeAnalytics.groups.list', {'mine': True})   # owner OAuth only

    def test_reserved_unknown_deprecated_and_discovery_only_parameters(self):
        for name in ('key', 'fields', 'part', 'pageToken', 'quotaUser', 'access_token'):
            error = self.refused('parameter_reserved', 'youtube.playlists.list', {'channelId': CHANNEL, name: 'x'})
            self.assertEqual(error.subject, name)
        error = self.refused('parameter_unknown', 'youtube.playlists.list', {'channelId': CHANNEL, 'raw<script>': 'x'})
        self.assertIsNone(error.subject)   # an unknown client name is never echoed
        self.refused('parameter_deprecated', 'youtube.channels.list', {'categoryId': 'GCQmVzdCBvZiBZb3VUdWJl'})
        self.refused('parameter_discovery_only', 'youtube.videos.list', {'id': VIDEO, 'locale': 'en'})

    def test_contexts_owner_partner_and_developer(self):
        self.refused('parameter_owner_only', 'youtube.playlists.list', {'mine': True})
        self.refused('context_not_supported', 'youtube.playlists.list',
                     {'channelId': CHANNEL, 'onBehalfOfContentOwner': 'x'})
        self.refused('context_not_supported', 'youtube.captions.list', {'videoId': VIDEO}, parts=('snippet',))
        self.refused('part_owner_only', 'youtube.videos.list', {'id': VIDEO}, parts=('fileDetails',))
        self.refused('context_not_supported', 'youtube.channels.list', {'id': CHANNEL}, parts=('auditDetails',))
        self.refused('field_owner_only', 'youtube.channels.list', {'id': CHANNEL}, parts=('status',),
                     fields=('status.longUploadsStatus',))
        owner = plan('youtube.playlists.list', {'mine': True}, auth=OWNER)
        self.assertEqual((owner.context, owner.owner, owner.filter_param), ('owner:binding-1', True, 'mine'))

    def test_filter_rules_bounds_values_and_identities(self):
        self.refused('filter_exactly_one_required', 'youtube.playlists.list', {})
        self.refused('filter_exactly_one_required', 'youtube.playlists.list', {'channelId': CHANNEL, 'id': 'PL123456789'})
        self.refused('parameter_needs_filter', 'youtube.videos.list', {'id': VIDEO, 'maxResults': 5})
        self.refused('parameter_invalid', 'youtube.playlists.list', {'channelId': CHANNEL, 'maxResults': 51})
        self.refused('parameter_invalid', 'youtube.playlists.list', {'channelId': CHANNEL, 'maxResults': True})
        self.refused('parameter_value_unknown', 'youtube.videos.list', {'chart': 'leastPopular'})
        self.refused('parameter_invalid', 'youtube.playlists.list', {'channelId': 'not-a-channel'})
        self.refused('parameter_invalid', 'youtube.videos.list', {'id': ['abc']})
        self.refused('parameter_invalid', 'youtube.videos.list', {'id': [VIDEO, ' ']})
        self.refused('parameter_invalid', 'youtube.channels.list', {'forHandle': 'https://evil.example/@x'})
        self.refused('parameter_invalid', 'youtube.playlists.list', {'channelId': CHANNEL + '\n'})
        self.refused('parameter_required', 'youtube.videos.batchGetStats', {}, parts=('statistics',))

    def test_selection_rules(self):
        self.refused('parts_required', 'youtube.playlists.list', {'channelId': CHANNEL}, parts=())
        self.refused('part_unknown', 'youtube.playlists.list', {'channelId': CHANNEL}, parts=('statistics',))
        self.refused('statistics_from_batch_get_stats', 'youtube.videos.list', {'id': VIDEO}, parts=('statistics',))
        self.refused('field_part_not_requested', 'youtube.playlists.list', {'channelId': CHANNEL},
                     fields=('status.privacyStatus',))
        self.refused('field_not_selectable', 'youtube.playlists.list', {'channelId': CHANNEL}, fields=('snippet.tags[]',))
        self.refused('field_unknown', 'youtube.playlists.list', {'channelId': CHANNEL}, fields=('snippet.secretSauce',))
        # Arrays of objects are parsed element by element; only raw bytes and untyped
        # objects stay described (all of them are never-exposed today).
        nested = plan('youtube.videos.list', {'id': VIDEO}, parts=('fileDetails',),
                      fields=('fileDetails.audioStreams[].codec',), auth=OWNER)
        self.assertIn('fileDetails(audioStreams(codec))', nested.fields_mask)

    def test_budgets_cannot_be_raised_and_bound_the_worst_case(self):
        self.refused('budget_invalid', 'youtube.playlists.list', {'channelId': CHANNEL}, budget=Budget(max_requests=11))
        self.refused('budget_invalid', 'youtube.playlists.list', {'channelId': CHANNEL}, budget=Budget(max_rows=5001))
        self.refused('budget_invalid', 'youtube.playlists.list', {'channelId': CHANNEL},
                     budget=Budget(max_units={'general': 11}))
        self.refused('budget_invalid', 'youtube.playlists.list', {'channelId': CHANNEL},
                     budget=Budget(max_units={'no_such_bucket': 1}))
        self.refused('budget_invalid', 'youtube.search.list', {'q': 'x'},
                     budget=Budget(max_units={'search_queries': 6}))   # 5 search calls per acquisition
        self.refused('budget_invalid', 'youtube.playlists.list', {'channelId': CHANNEL}, budget=Budget(deadline_seconds=61))
        self.refused('budget_units_exceeded', 'youtube.playlists.list', {'channelId': CHANNEL},
                     budget=Budget(max_units={'general': 0}))
        ids = [f'{index:011d}' for index in range(101)]
        self.refused('budget_units_exceeded', 'youtube.videos.batchGetStats', {'id': ids}, parts=('statistics',),
                     budget=Budget(max_units={'video_batch_get_stats': 2}))
        self.refused('budget_requests_exceeded', 'youtube.videos.list', {'id': ids},
                     budget=Budget(max_requests=2, max_units={'general': 10}))
        many = [f'{index:011d}' for index in range(600)]
        self.refused('budget_requests_exceeded', 'youtube.videos.batchGetStats', {'id': many}, parts=('statistics',))
        self.refused('budget_units_exceeded', 'youtube.search.list', {'q': 'x'},
                     budget=Budget(max_units={'general': 10}))   # the search bucket is not in this budget


class CostTests(unittest.TestCase):
    def test_page_token_cost_is_pages_times_units(self):
        value = plan('youtube.playlists.list', {'channelId': CHANNEL, 'maxResults': 20},
                     budget=Budget(max_requests=4, max_units={'general': 10}, max_rows=60))
        self.assertEqual((value.strategy, value.page_size, value.max_pages, dict(value.worst_cost)),
                         ('page_token', 20, 3, {'general': 3}))
        value = plan('youtube.playlists.list', {'channelId': CHANNEL}, budget=Budget(max_units={'general': 2}))
        self.assertEqual((value.page_size, value.max_pages, dict(value.worst_cost)), (50, 2, {'general': 2}))

    def test_id_batches_count_every_batch_in_their_own_bucket(self):
        ids = [f'{index:011d}' for index in range(120)] + ['00000000001']   # a repeat is dropped
        value = plan('youtube.videos.batchGetStats', {'id': ids}, parts=('statistics',))
        self.assertEqual((value.strategy, len(value.requests), dict(value.worst_cost)),
                         ('id_batch', 3, {'video_batch_get_stats': 3}))
        self.assertEqual([len(item.targets) for item in value.requests], [50, 50, 20])
        self.assertEqual(value.quota_bucket, 'video_batch_get_stats')

    def test_public_playlists_surface_plans_every_filter_and_part(self):
        parts = ('contentDetails', 'id', 'localizations', 'player', 'snippet', 'status')
        by_channel = plan('youtube.playlists.list', {'channelId': CHANNEL, 'hl': 'de', 'maxResults': 7}, parts=parts)
        self.assertEqual((by_channel.requests[0].params, by_channel.page_size, by_channel.parts),
                         ((('channelId', CHANNEL), ('hl', 'de')), 7, parts))
        by_id = plan('youtube.playlists.list', {'id': ['PL' + 'a' * 32, 'PL' + 'b' * 32]}, parts=parts)
        self.assertTrue(by_id.tracks_requested_ids)
        self.assertIn('player(embedHtml)', plan('youtube.playlists.list', {'channelId': CHANNEL}, parts=('player',),
                                                fields=('player.embedHtml',)).fields_mask)
        self.assertNotIn('player', plan('youtube.playlists.list', {'channelId': CHANNEL}, parts=('player', 'snippet')).fields_mask)

    def test_single_result_filter_is_one_request(self):
        value = plan('youtube.channels.list', {'forHandle': 'synthetic'}, parts=('snippet',))
        self.assertEqual((value.strategy, value.max_pages, dict(value.worst_cost), value.filter_values),
                         ('single', 1, {'general': 1}, ('@synthetic',)))

    def test_fingerprint_is_stable_and_binds_every_input(self):
        base = plan('youtube.playlists.list', {'channelId': CHANNEL})
        self.assertEqual(base.fingerprint, plan('youtube.playlists.list', {'channelId': CHANNEL}).fingerprint)
        others = [plan('youtube.playlists.list', {'channelId': 'UC' + 'b' * 22}),
                  plan('youtube.playlists.list', {'channelId': CHANNEL}, parts=('snippet', 'status')),
                  plan('youtube.playlists.list', {'channelId': CHANNEL}, budget=Budget(max_requests=9)),
                  plan('youtube.playlists.list', {'channelId': CHANNEL}, fields=('snippet.title',))]
        self.assertEqual(len({base.fingerprint, *(item.fingerprint for item in others)}), 5)

    def test_preview_projection_has_no_secret_or_provider_data(self):
        preview = plan('youtube.playlists.list', {'channelId': CHANNEL}).preview()
        self.assertEqual(preview['worst_cost'], {'general': 10})
        self.assertEqual(set(preview), {'method', 'registry_revision', 'executable', 'plan_fingerprint', 'strategy',
                                        'requests_planned', 'page_size', 'worst_cost', 'quota_bucket', 'quota_units',
                                        'ledger_endpoint_label', 'parts', 'fields', 'fields_mask', 'context', 'order',
                                        'row_unit', 'budget'})
        self.assertNotIn('AIza', str(preview))
        self.assertNotIn(CHANNEL, str(preview))   # parameters stay inside the fingerprint


class PublicFamilyPlanTests(unittest.TestCase):
    """Request builders of the public-key families; refusals are decided before any network use."""

    def refused(self, code, *args, **kwargs):
        with self.assertRaises(PlanRejected) as raised:
            plan(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_search_budget_bucket_and_documented_combinations(self):
        value = plan('youtube.search.list', {'q': 'synthetic', 'type': 'video', 'videoDuration': 'short'})
        self.assertEqual((value.strategy, value.quota_bucket, dict(value.worst_cost), value.max_pages, value.page_size),
                         ('page_token', 'search_queries', {'search_queries': 5}, 5, 50))
        self.assertIn('items(id(channelId,kind,playlistId,videoId)', value.fields_mask)
        error = self.refused('parameter_needs_type_video', 'youtube.search.list', {'q': 'x', 'videoDuration': 'short'})
        self.assertEqual(error.subject, 'videoDuration')
        self.refused('parameter_needs_type_video', 'youtube.search.list', {'q': 'x', 'type': ['video', 'channel'],
                                                                           'eventType': 'live'})
        self.refused('parameter_needs_pair', 'youtube.search.list', {'type': 'video', 'location': '37.42307,-122.08427'})
        self.refused('parameter_invalid', 'youtube.search.list', {'type': 'video', 'location': '91.0,10.0',
                                                                  'locationRadius': '10km'})
        self.refused('parameter_invalid', 'youtube.search.list', {'type': 'video', 'location': '37.4,-122.0',
                                                                  'locationRadius': '1001km'})
        located = plan('youtube.search.list', {'type': 'video', 'location': '37.42307,-122.08427', 'locationRadius': '0.75mi'})
        self.assertEqual(dict(located.requests[0].params)['locationRadius'], '0.75mi')
        self.refused('parameter_value_unknown', 'youtube.search.list', {'q': 'x', 'order': 'searchSortUnspecified'})
        self.refused('parameter_value_unknown', 'youtube.search.list', {'q': 'x', 'type': 'short'})
        self.refused('parameter_invalid', 'youtube.search.list', {'q': 'x', 'publishedAfter': '2026-01-01'})
        self.refused('parameter_invalid', 'youtube.search.list', {'q': 'x', 'relevanceLanguage': 'english please'})
        self.refused('parameter_owner_only', 'youtube.search.list', {'forMine': True, 'type': 'video'})
        self.refused('context_not_supported', 'youtube.search.list', {'forDeveloper': True})
        ordered = plan('youtube.search.list', {'channelId': CHANNEL, 'type': 'video', 'order': 'date'})
        self.assertEqual(ordered.order, 'provider_param:date')   # the provider's order, never a local sort

    def test_reference_lists_are_single_requests_with_checked_codes(self):
        for method in ('youtube.i18nLanguages.list', 'youtube.i18nRegions.list'):
            value = plan(method, {'hl': 'zh-Hant'})
            self.assertEqual((value.strategy, dict(value.worst_cost), value.executable), ('single', {'general': 1}, True))
            self.refused('parameter_invalid', method, {'hl': 'x y'})
        by_region = plan('youtube.videoCategories.list', {'regionCode': 'US', 'hl': 'en_US'})
        self.assertEqual(by_region.strategy, 'single')
        by_id = plan('youtube.videoCategories.list', {'id': ['1', '10', '1']})
        self.assertEqual((by_id.strategy, by_id.requests[0].targets), ('id_batch', ('1', '10')))
        self.refused('parameter_invalid', 'youtube.videoCategories.list', {'regionCode': 'USA'})
        self.refused('parameter_invalid', 'youtube.videoCategories.list', {'id': ['music']})
        self.refused('filter_exactly_one_required', 'youtube.videoCategories.list', {'id': ['1'], 'regionCode': 'US'})

    def test_channel_rows_filters_and_contexts(self):
        sections = plan('youtube.channelSections.list', {'channelId': CHANNEL}, parts=('contentDetails', 'snippet'))
        self.assertEqual((sections.strategy, sections.row_unit), ('single', 'channel_section'))
        self.refused('parameter_deprecated', 'youtube.channelSections.list', {'channelId': CHANNEL, 'hl': 'de'})
        self.refused('parameter_owner_only', 'youtube.channelSections.list', {'mine': True})
        activities = plan('youtube.activities.list', {'channelId': CHANNEL, 'publishedAfter': '2026-10-01',
                                                      'publishedBefore': '2026-10-28T00:00:00Z', 'regionCode': 'DE'})
        self.assertEqual((activities.strategy, activities.row_unit), ('page_token', 'activity'))
        self.refused('parameter_invalid', 'youtube.activities.list', {'channelId': CHANNEL, 'publishedAfter': 'yesterday'})
        self.refused('parameter_deprecated', 'youtube.activities.list', {'home': True})
        subscriptions = plan('youtube.subscriptions.list', {'channelId': CHANNEL, 'forChannelId': [CHANNEL, 'UC' + 'b' * 22]},
                             fields=('snippet.title',))
        self.assertEqual((subscriptions.strategy, subscriptions.row_unit), ('page_token', 'subscription'))
        self.assertEqual(subscriptions.fields, ('id', 'snippet.title'))
        self.refused('parameter_too_many_values', 'youtube.subscriptions.list',
                     {'channelId': CHANNEL, 'forChannelId': ['UC' + f'{index:022d}' for index in range(51)]})
        self.refused('parameter_invalid', 'youtube.subscriptions.list', {'channelId': CHANNEL, 'forChannelId': 'not-a-channel'})
        for name in ('mine', 'mySubscribers', 'myRecentSubscribers'):
            self.refused('parameter_owner_only', 'youtube.subscriptions.list', {name: True})
        default = plan('youtube.subscriptions.list', {'channelId': CHANNEL}, parts=('snippet', 'contentDetails'))
        self.assertEqual(default.fields, ('id',))   # personal data is never selected by default

    def test_comment_filters_pages_and_personal_fields(self):
        threads = plan('youtube.commentThreads.list', {'videoId': VIDEO, 'maxResults': 100, 'order': 'relevance',
                                                       'searchTerms': 'synthetic', 'textFormat': 'plainText'},
                       parts=('snippet', 'replies'), fields=('replies.comments[].snippet.textDisplay',))
        self.assertEqual((threads.strategy, threads.page_size, threads.order), ('page_token', 100, 'provider_param:relevance'))
        self.assertIn('replies(comments(snippet(textDisplay)))', threads.fields_mask)
        by_id = plan('youtube.commentThreads.list', {'id': ['Ugx' + 'a' * 20, 'Ugx' + 'b' * 20]})
        self.assertEqual((by_id.strategy, len(by_id.requests), by_id.tracks_requested_ids), ('id_batch', 1, True))
        self.refused('parameter_conflict', 'youtube.commentThreads.list', {'id': ['Ugx' + 'a' * 20], 'maxResults': 5})
        self.refused('parameter_conflict', 'youtube.commentThreads.list', {'id': ['Ugx' + 'a' * 20], 'order': 'time'})
        self.refused('parameter_owner_only', 'youtube.commentThreads.list', {'videoId': VIDEO, 'moderationStatus': 'heldForReview'})
        self.refused('parameter_discovery_only', 'youtube.commentThreads.list', {'channelId': CHANNEL})
        self.refused('parameter_value_unknown', 'youtube.commentThreads.list', {'videoId': VIDEO, 'textFormat': 'markdown'})
        self.refused('field_owner_only', 'youtube.commentThreads.list', {'videoId': VIDEO}, parts=('replies',),
                     fields=('replies.comments[].snippet.textOriginal',))
        replies = plan('youtube.comments.list', {'parentId': 'Ugx' + 'a' * 20}, fields=('snippet.textDisplay',))
        self.assertEqual((replies.strategy, replies.tracks_requested_ids), ('page_token', False))   # parentId names the parent
        reply_ids = plan('youtube.comments.list', {'id': ['Ugx' + 'a' * 20 + '.reply1']})
        self.assertEqual(reply_ids.strategy, 'id_batch')
        self.refused('parameter_invalid', 'youtube.comments.list', {'parentId': 'not a comment id'})

    def test_live_chat_is_one_poll_and_the_stream_is_refused(self):
        poll = plan('youtube.liveChatMessages.list', {'liveChatId': 'Cg0KC2hHUFVsRmJ6Nm1F', 'maxResults': 2000,
                                                      'profileImageSize': 16}, parts=('snippet', 'authorDetails'))
        self.assertEqual((poll.strategy, poll.page_size, dict(poll.worst_cost), poll.max_pages),
                         ('poll', 2000, {'general': 5}, 1))
        self.assertIn('pollingIntervalMillis', poll.fields_mask)
        self.assertIn('offlineAt', poll.fields_mask)
        self.refused('parameter_invalid', 'youtube.liveChatMessages.list', {'liveChatId': 'x', 'maxResults': 199})
        self.refused('parameter_required', 'youtube.liveChatMessages.list', {})
        self.refused('budget_units_exceeded', 'youtube.liveChatMessages.list', {'liveChatId': 'x'},
                     budget=Budget(max_units={'general': 4}))
        self.refused('stream_reader_not_available', 'youtube.youtube.v3.liveChat.messages.stream', {'liveChatId': 'x'})

    def test_remaining_public_surface_of_videos_and_channels(self):
        chart = plan('youtube.videos.list', {'chart': 'mostPopular', 'regionCode': 'DE', 'videoCategoryId': '10',
                                             'hl': 'de', 'maxHeight': 720},
                     parts=('brandPartner', 'localizations', 'player', 'recordingDetails', 'topicDetails'))
        self.assertEqual((chart.strategy, chart.order), ('page_token', 'provider'))
        self.refused('parameter_value_unknown', 'youtube.videos.list', {'chart': 'chartUnspecified'})
        self.refused('parameter_invalid', 'youtube.videos.list', {'chart': 'mostPopular', 'videoCategoryId': 'music'})
        self.refused('parameter_invalid', 'youtube.videos.list', {'chart': 'mostPopular', 'maxWidth': 71})
        legacy = plan('youtube.channels.list', {'forUsername': 'SyntheticLegacyName', 'hl': 'de'},
                      parts=('localizations', 'status', 'topicDetails'))
        self.assertEqual((legacy.strategy, legacy.filter_values), ('single', ('SyntheticLegacyName',)))


class PreviewIsolationTests(unittest.TestCase):
    def test_planning_never_imports_the_transport(self):
        code = ("import sys; sys.path.insert(0, 'application'); import provider_sources.plan, provider_sources.wire, "
                "provider_sources.selection, provider_sources.registry; "
                "loaded = sorted(m for m in sys.modules if m in ('youtube_client', 'provider_runtime', 'requests', "
                "'provider_sources.transport', 'provider_sources.acquire', 'core')); print(loaded)")
        environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP') if key in os.environ}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], cwd=ROOT, capture_output=True, text=True,
                                timeout=60, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_planning_sends_nothing(self):
        import youtube_client
        with patch.object(youtube_client.requests, 'get', side_effect=AssertionError('network')), \
                patch('provider_runtime.admit_request', side_effect=AssertionError('admitted')):
            for method, parameters, parts in (('youtube.playlists.list', {'channelId': CHANNEL}, ('snippet',)),
                                              ('youtube.videos.batchGetStats', {'id': VIDEO}, ('statistics',)),
                                              ('youtube.search.list', {'q': 'x'}, ('snippet',))):
                try:
                    plan(method, parameters, parts=parts)
                except PlanRejected:
                    pass   # a refusal is also decided without the network

    def test_every_executable_method_has_a_parse_hook_and_a_public_context(self):
        self.assertEqual(set(HOOKS), set(policy.EXECUTABLE))
        loaded = load_registry()
        for method_id in policy.EXECUTABLE:
            method = loaded.methods[method_id]
            if method_id in policy.OWNER_ONLY_FAMILIES:
                # Owner families: owner OAuth only, never the installation key.
                self.assertNotIn('public_key', method.auth_contexts)
                self.assertIn('owner_oauth', method.auth_contexts)
            else:
                self.assertEqual(method.api, 'data')
                self.assertIn('public_key', method.auth_contexts)

    def test_described_but_unconnected_methods_plan_but_do_not_execute(self):
        unconnected = policy.EXECUTABLE - {'youtube.channelSections.list'}
        with patch.object(policy, 'EXECUTABLE', unconnected):
            value = plan('youtube.channelSections.list', {'channelId': CHANNEL})
        self.assertFalse(value.executable)
        self.assertEqual(dict(value.worst_cost), {'general': 1})


if __name__ == '__main__':
    unittest.main()
