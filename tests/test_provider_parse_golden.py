"""Golden parsing of synthetic documented-shape fixtures. No network.

Each fixture -> AcquisitionResult -> canonical JSON -> tests/fixtures/provider_sources/youtube/<method>/golden.json.
The existing collector normalizers parse the same fixtures and must agree (one rule, one owner).
Rewrite goldens after a reviewed change: python tests/test_provider_parse_golden.py --write
"""
import json
from datetime import datetime, timezone
import sys
import unittest

from provider_sources_support import FIXTURES, canonical, cases, load, run_case

from provider_sources.continuation import ContinuationCodec  # noqa: E402
from provider_sources.registry import load_registry  # noqa: E402

from youtube_source_fields import parse_dt, youtube_duration_seconds  # noqa: E402

METHODS = ('youtube.playlists.list', 'youtube.videos.list', 'youtube.videos.batchGetStats',
           'youtube.channels.list', 'youtube.playlistItems.list',
           # Public key families.
           'youtube.i18nLanguages.list', 'youtube.i18nRegions.list', 'youtube.videoCategories.list',
           'youtube.channelSections.list', 'youtube.activities.list', 'youtube.subscriptions.list',
           'youtube.commentThreads.list', 'youtube.comments.list', 'youtube.search.list',
           'youtube.liveChatMessages.list')
REQUIRED_CASES = {
    'youtube.playlists.list': {'full_two_pages', 'minimal', 'absent_parts', 'empty_list', 'omitted_id',
                               'unknown_field', 'unknown_enum', 'channel_not_found', 'budget_cut'},
    'youtube.videos.list': {'full', 'minimal', 'absent_parts', 'empty_list', 'omitted_id', 'unknown_field', 'unknown_enum'},
    'youtube.videos.batchGetStats': {'full', 'counters_only', 'no_summary', 'all_failed', 'invalid_counters', 'unknown_field'},
    'youtube.channels.list': {'full', 'for_handle', 'for_handle_not_found', 'omitted_id', 'unknown_enum_and_field',
                              'for_username', 'for_username_not_found'},
    'youtube.playlistItems.list': {'full_two_pages', 'playlist_not_found', 'not_accessible', 'empty_list',
                                   'unknown_enum_and_naive_date', 'by_id', 'video_in_playlist', 'video_not_found',
                                   'operation_unsupported'},
    'youtube.i18nLanguages.list': {'full', 'minimal', 'empty_list', 'items_missing', 'absent_empty_null', 'unknown_field',
                                   'bad_identity'},
    'youtube.i18nRegions.list': {'full', 'minimal', 'empty_list', 'absent_empty_null', 'unknown_field'},
    'youtube.videoCategories.list': {'full', 'by_id', 'by_id_complete', 'minimal', 'empty_list', 'absent_false',
                                     'unknown_field'},
    'youtube.channelSections.list': {'full', 'by_id', 'minimal', 'empty_list', 'unknown_enum_and_field', 'channel_not_found',
                                     'forbidden', 'section_not_found_batch'},
    'youtube.activities.list': {'full_two_pages', 'minimal', 'empty_list', 'absent_part', 'unknown_enum_and_field',
                                'channel_not_found'},
    'youtube.subscriptions.list': {'full_two_pages', 'default_selection', 'by_id', 'for_channel', 'private_subscriptions',
                                   'subscriber_not_found', 'account_closed', 'empty_list', 'unknown_enum_and_field'},
    'youtube.commentThreads.list': {'full_two_pages', 'minimal', 'default_selection', 'plain_text', 'channel_threads',
                                    'comments_disabled', 'video_not_found', 'channel_not_found', 'by_id', 'by_id_not_found',
                                    'by_id_google_plus_only', 'empty_list', 'null_and_unknown'},
    'youtube.comments.list': {'replies_two_pages', 'by_id', 'minimal', 'empty_list', 'comment_not_found', 'forbidden',
                              'google_plus_only', 'unknown_enum_and_field'},
    'youtube.search.list': {'mixed_two_pages', 'channel_videos', 'rejected_hits', 'empty_list', 'budget_cut',
                            'unknown_enum_and_field', 'invalid_filter', 'quota_exhausted'},
    'youtube.liveChatMessages.list': {'open_chat_poll', 'empty_poll', 'offline_chat', 'chat_ended', 'chat_disabled',
                                      'chat_not_found', 'forbidden', 'polled_too_early', 'unknown_enum_and_field'},
}


def results_by_method():
    found = {}
    for method, name, fixture in cases():
        found.setdefault(method, {})[name] = canonical(run_case(fixture)[1])
    return found


def golden_path(method):
    return FIXTURES / method / 'golden.json'


def write_goldens():
    for method, values in results_by_method().items():
        golden_path(method).write_bytes((json.dumps(values, indent=1, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8'))


def cell(row, path):
    return row['fields'][path]


class FixtureContractTests(unittest.TestCase):
    def test_every_fixture_is_synthetic_documented_and_named(self):
        seen = {}
        for method, name, fixture in cases():
            seen.setdefault(method, set()).add(name)
            with self.subTest(method=method, case=name):
                self.assertIs(fixture['synthetic'], True)
                self.assertTrue(fixture['source_url'].startswith(('https://developers.google.com/youtube/v3/docs/',
                                                                  'https://developers.google.com/youtube/v3/live/docs/')))
                self.assertTrue(fixture['case'].strip())
        for method in METHODS:
            self.assertLessEqual(REQUIRED_CASES[method], seen.get(method, set()), method)


class GoldenTests(unittest.TestCase):
    maxDiff = None

    def test_results_equal_the_reviewed_goldens(self):
        for method, values in results_by_method().items():
            with self.subTest(method=method):
                expected = json.loads(golden_path(method).read_text(encoding='utf-8'))
                self.assertEqual(values, expected)

    def test_masked_provenance_matches_the_plan_and_the_registry(self):
        revision = load_registry().revision
        for method, name, fixture in cases():
            with self.subTest(method=method, case=name):
                plan, result, _ = run_case(fixture)
                self.assertEqual(result.plan_fingerprint, plan.fingerprint)
                self.assertTrue(all(page.plan_fingerprint == plan.fingerprint and page.registry_revision == revision
                                    for page in result.pages))

    def test_new_family_playlists_end_to_end(self):
        plan, result, sender = run_case(load('youtube.playlists.list', 'full_two_pages'))
        value = canonical(result)
        self.assertEqual((value['completeness'], value['cost'], value['unit']), ('complete', {'general': 2}, 'playlist'))
        self.assertEqual([call.get('pageToken') for call in sender.calls], [None, 'MARKER-TOKEN-PAGE-2'])
        self.assertTrue(all(call['part'] == plan.part_param and call['fields'] == plan.fields_mask for call in sender.calls))
        first = value['rows'][0]
        self.assertEqual(cell(first, 'contentDetails.itemCount'), {'state': 'known', 'value': 3})
        self.assertEqual(cell(first, 'snippet.publishedAt')['value'], '2026-02-11T08:30:01Z')
        self.assertEqual(cell(first, 'localizations.{key}.title')['value'], {'de': 'Reihe 1', 'en': 'Series 1'})
        self.assertTrue(cell(first, 'player.embedHtml')['value'].startswith('<iframe'))   # raw text, never rendered
        self.assertEqual(cell(first, 'status.podcastStatus')['value'], 'disabled')
        self.assertEqual(len({page['token_chain_sha256'] for page in value['pages']}), 2)

    def test_states_absent_empty_omitted_unknown(self):
        absent = canonical(run_case(load('youtube.playlists.list', 'absent_parts'))[1])['rows'][0]
        self.assertEqual(cell(absent, 'contentDetails.itemCount'), {'state': 'absent', 'absence': 'absent_unknown'})
        self.assertEqual(cell(absent, 'snippet.defaultLanguage')['state'], 'absent')
        self.assertEqual(cell(absent, 'snippet.description')['state'], 'known')
        empty = canonical(run_case(load('youtube.playlists.list', 'empty_list'))[1])
        self.assertEqual((empty['rows'], empty['completeness']), ([], 'complete'))
        omitted = canonical(run_case(load('youtube.playlists.list', 'omitted_id'))[1])
        self.assertEqual(omitted['entities']['PLsyntheticMissing000000000000099'], 'omitted')
        unknown = canonical(run_case(load('youtube.playlists.list', 'unknown_field'))[1])
        self.assertEqual(unknown['pages'][0]['unknown_field_paths'], ['newEnvelopeKey', 'newPart', 'snippet.brandNewField'])
        self.assertNotIn('MARKER-UNKNOWN', json.dumps(unknown))
        enum = canonical(run_case(load('youtube.playlists.list', 'unknown_enum'))[1])['rows'][0]
        self.assertEqual(cell(enum, 'status.privacyStatus'), {'state': 'unknown_enum', 'value': 'membersOnly'})
        missing = canonical(run_case(load('youtube.playlists.list', 'channel_not_found'))[1])
        self.assertEqual((missing['completeness'], missing['entities']), ('complete', {'UCCCCCCCCCCCCCCCCCCCCCC3': 'not_found'}))
        cut = canonical(run_case(load('youtube.playlists.list', 'budget_cut'))[1])
        self.assertEqual((cut['completeness'], cut['stop_reason'], cut['continuation']),
                         ('partial', 'budget_requests', '<encrypted continuation>'))


def rows_of(method, name):
    return canonical(run_case(load(method, name))[1])


class PublicFamilyTests(unittest.TestCase):
    """Value and entity states of the public-key families on documented shapes."""

    def test_reference_lists_keep_absent_empty_null_and_false_apart(self):
        names = [cell(row, 'snippet.name') for row in rows_of('youtube.i18nLanguages.list', 'absent_empty_null')['rows']]
        self.assertEqual(names, [{'state': 'absent', 'absence': 'absent_unknown'}, {'state': 'known_empty', 'value': ''},
                                 {'state': 'null_reported'}])
        flags = [cell(row, 'snippet.assignable') for row in rows_of('youtube.videoCategories.list', 'absent_false')['rows']]
        self.assertEqual(flags, [{'state': 'absent', 'absence': 'absent_unknown'}, {'state': 'known', 'value': False}])
        self.assertEqual(rows_of('youtube.videoCategories.list', 'by_id')['entities'],
                         {'1': 'present', '10': 'present', '99': 'omitted'})
        self.assertEqual(rows_of('youtube.i18nLanguages.list', 'items_missing')['completeness'], 'complete')
        bad = rows_of('youtube.i18nLanguages.list', 'bad_identity')
        self.assertEqual((len(bad['rows']), bad['pages'][0]['rejected_items']), (1, 1))
        plan, result, sender = run_case(load('youtube.i18nLanguages.list', 'full'))
        self.assertEqual((plan.strategy, sender.calls[0]['hl'], result.rows[2].identity),
                         ('single', 'de', ('i18n_language', 'zh-Hant')))

    def test_channel_sections_and_activities_follow_their_type(self):
        sections = rows_of('youtube.channelSections.list', 'full')['rows']
        self.assertEqual(cell(sections[0], 'contentDetails.playlists[]'),
                         {'state': 'absent', 'absence': 'absent_means_not_applicable'})
        self.assertEqual(cell(sections[2], 'contentDetails.channels[]')['value'],
                         ['UCBBBBBBBBBBBBBBBBBBBBB2', 'UCCCCCCCCCCCCCCCCCCCCCC3'])
        self.assertEqual(rows_of('youtube.channelSections.list', 'section_not_found_batch')['entities'],
                         {'UCAAAAAAAAAAAAAAAAAAAAA1.SectionSynth01': 'omitted',
                          'UCAAAAAAAAAAAAAAAAAAAAA1.SectionSynth02': 'omitted'})
        self.assertEqual(rows_of('youtube.channelSections.list', 'forbidden')['entities'],
                         {'UCBBBBBBBBBBBBBBBBBBBBB2': 'access_denied'})
        activities = rows_of('youtube.activities.list', 'full_two_pages')
        self.assertEqual((activities['completeness'], activities['cost'], activities['unit']),
                         ('complete', {'general': 2}, 'activity'))
        upload, added = activities['rows'][:2]
        self.assertEqual(cell(upload, 'contentDetails.upload.videoId')['value'], 'vidxxxxxxx1')
        self.assertEqual(cell(upload, 'contentDetails.playlistItem.playlistId')['state'], 'absent')
        self.assertEqual(cell(added, 'contentDetails.playlistItem.resourceId.videoId')['value'], 'vidxxxxxxx2')
        self.assertEqual(cell(rows_of('youtube.activities.list', 'unknown_enum_and_field')['rows'][0], 'snippet.type'),
                         {'state': 'unknown_enum', 'value': 'shortsRemix'})

    def test_subscriptions_are_relations_and_private_lists_are_a_state(self):
        value = rows_of('youtube.subscriptions.list', 'full_two_pages')
        self.assertEqual((value['unit'], len(value['rows']), value['order']),
                         ('subscription', 3, 'provider_param:alphabetical'))
        first = value['rows'][0]
        self.assertEqual(first['identity']['kind'], 'subscription')
        self.assertEqual(cell(first, 'snippet.resourceId.channelId')['value'], 'UCBBBBBBBBBBBBBBBBBBBBB2')
        self.assertEqual(cell(value['rows'][1], 'contentDetails.totalItemCount'), {'state': 'known', 'value': 0})
        self.assertEqual(set(rows_of('youtube.subscriptions.list', 'default_selection')['rows'][0]['fields']), {'id'})
        private = rows_of('youtube.subscriptions.list', 'private_subscriptions')
        self.assertEqual((private['completeness'], private['entities']),
                         ('complete', {'UCBBBBBBBBBBBBBBBBBBBBB2': 'access_denied'}))
        _, _, sender = run_case(load('youtube.subscriptions.list', 'for_channel'))
        self.assertEqual(sender.calls[0]['forChannelId'], 'UCBBBBBBBBBBBBBBBBBBBBB2,UCCCCCCCCCCCCCCCCCCCCCC3')

    def test_comment_threads_keep_reply_states_per_reply(self):
        value = rows_of('youtube.commentThreads.list', 'full_two_pages')
        first, second, third = value['rows']
        self.assertEqual(cell(first, 'snippet.totalReplyCount')['value'], 3)   # 2 of 3 replies returned: never reconciled
        authors = cell(first, 'replies.comments[].snippet.authorChannelId.value')
        self.assertEqual(authors, {'state': 'known', 'value': [{'state': 'known', 'value': 'UCBBBBBBBBBBBBBBBBBBBBB2'},
                                                               {'state': 'absent', 'absence': 'absent_unknown'}]})
        self.assertEqual(cell(first, 'snippet.topLevelComment.snippet.parentId')['absence'], 'absent_means_not_applicable')
        self.assertTrue(cell(first, 'snippet.topLevelComment.snippet.textDisplay')['value'].startswith('<b>'))   # raw HTML
        self.assertEqual(cell(second, 'replies.comments[].id')['state'], 'absent')
        self.assertEqual(cell(third, 'replies.comments[].id'), {'state': 'known_empty', 'value': []})
        self.assertEqual(cell(third, 'snippet.canReply'), {'state': 'known', 'value': False})
        self.assertEqual(rows_of('youtube.commentThreads.list', 'comments_disabled')['entities'], {'vidxxxxxxx2': 'disabled'})
        self.assertEqual(rows_of('youtube.commentThreads.list', 'by_id_not_found')['entities'],
                         {'UgxSyntheticThread000001': 'omitted', 'UgxSyntheticThread000002': 'omitted'})
        self.assertEqual(rows_of('youtube.commentThreads.list', 'by_id_google_plus_only')['stop_reason'],
                         'operation_not_supported')
        plan, _, sender = run_case(load('youtube.commentThreads.list', 'by_id'))
        self.assertEqual((plan.strategy, 'maxResults' in sender.calls[0], 'pageToken' in sender.calls[0]),
                         ('id_batch', False, False))
        nulls = rows_of('youtube.commentThreads.list', 'null_and_unknown')['rows'][0]
        self.assertEqual(cell(nulls, 'snippet.topLevelComment.snippet.likeCount'), {'state': 'null_reported'})
        self.assertEqual(cell(nulls, 'snippet.topLevelComment.snippet.viewerRating'), {'state': 'unknown_enum', 'value': 'love'})

    def test_comments_by_parent_are_rows_of_the_parent_not_an_id_list(self):
        value = rows_of('youtube.comments.list', 'replies_two_pages')
        self.assertEqual((value['completeness'], len(value['rows']), value['entities']), ('complete', 3, {}))
        self.assertEqual(rows_of('youtube.comments.list', 'comment_not_found')['entities'],
                         {'UgxSyntheticMissing0099': 'not_found'})
        by_id = rows_of('youtube.comments.list', 'by_id')
        self.assertEqual(by_id['entities']['UgxSyntheticMissing0099'], 'omitted')
        self.assertEqual(cell(by_id['rows'][0], 'snippet.parentId')['absence'], 'absent_means_not_applicable')

    def test_search_cycle_longer_than_recent_history_survives_confirmed_continuations(self):
        fixture = load('youtube.search.list', 'mixed_two_pages')
        fixture['request']['budget'] = {'max_requests': 1, 'max_units': {'search_queries': 1}}
        empty = {**fixture['responses'][0]['body'], 'items': []}
        codec, cursor = ContinuationCodec(), None
        for page in range(100):
            token = str(page % 33).zfill(2) + '\u96ea' * 745 + 'X' * 1301
            fixture['responses'] = [{'status': 200, 'body': {**empty, 'nextPageToken': token}}]
            _, result, sender = run_case(fixture, codec=codec, continuation=cursor)
            self.assertEqual((len(sender.calls), dict(result.cost)), (1, {'search_queries': 1}))
            cursor = result.continuation
            if cursor is None:
                self.assertGreaterEqual(page, 33)
                self.assertEqual((result.stop_reason, result.notes), ('response_invalid', ('repeated_page_token',)))
                break
            self.assertEqual(result.stop_reason, 'budget_requests')
        else:
            self.fail('a repeated search cycle kept returning a continuation')

    def test_search_resumes_large_escaped_tokens_accepted_by_previous_cursor(self):
        from provider_sources_support import NOW, plan_for

        fixture = load('youtube.search.list', 'mixed_two_pages')
        fixture['request']['budget'] = {'max_requests': 1, 'max_units': {'search_queries': 1}}
        plan = plan_for(fixture['request'])
        codec = ContinuationCodec()
        # Each token has 2048 characters and fits the encoded cursor limit.
        # The Chinese glyph uses three UTF-8 bytes. The control byte is JSON escaped.
        for special in ('\u96ea', '\x01'):
            with self.subTest(special=repr(special)):
                token = 'A' + special * 745 + 'Z' * 1302
                cursor_without_history = codec.encode(
                    fingerprint=plan.fingerprint, context=plan.context, now=NOW,
                    position={'page': 9, 'token': token, 'chain': 'a' * 64})
                fixture['responses'] = [{'status': 200, 'body': {'kind': 'youtube#searchListResponse',
                                         'items': [], 'nextPageToken': token[::-1]}}]
                _, page, sent = run_case(fixture, codec=codec, continuation=cursor_without_history)
                self.assertEqual(sent.calls[0]['pageToken'], token)
                self.assertIsNotNone(page.continuation)
                fixture['responses'][0]['body'].pop('nextPageToken')
                _, last, sent = run_case(fixture, codec=codec, continuation=page.continuation)
                self.assertEqual(sent.calls[0]['pageToken'], token[::-1])
                self.assertIsNone(last.continuation)

    def test_search_hits_have_kinds_and_are_not_exhaustive(self):
        value = rows_of('youtube.search.list', 'mixed_two_pages')
        self.assertEqual([row['identity']['kind'] for row in value['rows']], ['video', 'channel', 'playlist', 'video'])
        self.assertEqual((value['duplicate_rows_dropped'], value['cost'], value['notes']),
                         (1, {'search_queries': 2}, ['region:US']))
        self.assertEqual(cell(value['rows'][1], 'id.videoId'), {'state': 'absent', 'absence': 'absent_means_not_applicable'})
        rejected = rows_of('youtube.search.list', 'rejected_hits')
        self.assertEqual((len(rejected['rows']), rejected['pages'][0]['rejected_items']), (1, 2))
        self.assertEqual(rows_of('youtube.search.list', 'quota_exhausted')['stop_reason'], 'quota_exhausted:search_queries')
        self.assertEqual(rows_of('youtube.search.list', 'channel_videos')['order'], 'provider_param:date')
        _, _, sender = run_case(load('youtube.search.list', 'mixed_two_pages'))
        self.assertIn('items(id(channelId,kind,playlistId,videoId)', sender.calls[0]['fields'])

    def test_live_chat_is_polled_and_its_states_are_the_chat(self):
        plan, result, sender = run_case(load('youtube.liveChatMessages.list', 'open_chat_poll'))
        value = canonical(result)
        self.assertEqual((plan.strategy, len(sender.calls), value['cost']), ('poll', 1, {'general': 5}))
        self.assertEqual((value['completeness'], value['stop_reason'], value['retry_after_seconds'], value['continuation']),
                         ('partial', 'live_chat_open', 6, '<encrypted continuation>'))
        self.assertEqual(cell(value['rows'][1], 'snippet.superChatDetails.amountMicros'), {'state': 'known', 'value': 5000000})
        self.assertEqual(cell(value['rows'][0], 'snippet.superChatDetails.amountMicros')['absence'],
                         'absent_means_not_applicable')
        offline = rows_of('youtube.liveChatMessages.list', 'offline_chat')
        self.assertEqual((offline['completeness'], offline['continuation']), ('complete', None))
        self.assertIn('chat_offline_at:2026-10-29T09:59:00Z', offline['notes'])
        self.assertEqual(cell(offline['rows'][0], 'snippet.displayMessage')['absence'], 'absent_means_not_applicable')
        for name, state in (('chat_ended', 'ended'), ('chat_disabled', 'disabled'), ('forbidden', 'access_denied')):
            self.assertEqual(rows_of('youtube.liveChatMessages.list', name)['entities'], {'LiveChatSynthetic000001': state})
        self.assertEqual(rows_of('youtube.liveChatMessages.list', 'polled_too_early')['stop_reason'], 'rate_limited')

    def test_live_chat_continuation_waits_for_the_polling_interval(self):
        from datetime import timedelta
        from provider_sources.continuation import ContinuationCodec
        from provider_sources.errors import PlanRejected
        import provider_sources_support as support
        codec = ContinuationCodec()
        _, first, _ = run_case(load('youtube.liveChatMessages.list', 'open_chat_poll'), codec=codec)
        fixture = load('youtube.liveChatMessages.list', 'empty_poll')
        fixture['request'] = load('youtube.liveChatMessages.list', 'open_chat_poll')['request']
        with self.assertRaisesRegex(PlanRejected, 'continuation_too_early'):
            run_case(fixture, codec=codec, continuation=first.continuation)   # same clock: 5.1 s not elapsed
        original = support.NOW
        support.NOW = original + timedelta(seconds=6)
        try:
            _, second, sender = run_case(fixture, codec=codec, continuation=first.continuation)
        finally:
            support.NOW = original
        self.assertEqual(sender.calls[0]['pageToken'], 'MARKER-TOKEN-POLL-2')
        self.assertEqual((second.stop_reason, second.pages[0].page_index), ('live_chat_open', 1))

    def test_remaining_public_surface_of_videos_channels_and_playlist_items(self):
        chart = rows_of('youtube.videos.list', 'chart_two_pages')
        self.assertEqual((chart['completeness'], len(chart['rows']), chart['entities']), ('complete', 3, {}))
        self.assertEqual(cell(chart['rows'][0], 'brandPartner.channelId')['value'], 'UCBBBBBBBBBBBBBBBBBBBBB2')
        self.assertEqual(cell(chart['rows'][1], 'brandPartner.channelId')['state'], 'absent')
        self.assertEqual(cell(chart['rows'][0], 'player.embedWidth'), {'state': 'known', 'value': 640})
        self.assertEqual(rows_of('youtube.videos.list', 'chart_not_available')['stop_reason'], 'chart_not_available')
        user = rows_of('youtube.channels.list', 'for_username')
        self.assertEqual(user['entities'], {'SyntheticLegacyName': 'present'})
        self.assertEqual(cell(user['rows'][0], 'status.madeForKids'), {'state': 'known', 'value': False})
        self.assertEqual(rows_of('youtube.channels.list', 'for_username_not_found')['entities'], {'NobodySynthetic': 'not_found'})
        items = rows_of('youtube.playlistItems.list', 'by_id')
        self.assertEqual(items['entities']['UExzeW50aGV0aWNNaXNzaW5n'], 'omitted')
        self.assertEqual(cell(items['rows'][0], 'snippet.thumbnails.high.width'), {'state': 'known', 'value': 480})
        same_video = rows_of('youtube.playlistItems.list', 'video_in_playlist')['rows']
        self.assertEqual([cell(row, 'contentDetails.videoId')['value'] for row in same_video], ['vidxxxxxxx1', 'vidxxxxxxx1'])
        self.assertEqual(rows_of('youtube.playlistItems.list', 'video_not_found')['entities'], {'vidxxxxxxx4': 'not_found'})


class SameRuleOwnerTests(unittest.TestCase):
    """The new parser and the collector's normalizers agree on the same fixtures."""

    def test_videos_list_matches_build_video_metadata_payload(self):
        from collection.video_payloads import build_video_metadata_payload
        fixture = load('youtube.videos.list', 'full')
        rows = canonical(run_case(fixture)[1])['rows']
        for item, row in zip(fixture['responses'][0]['body']['items'], rows):
            with self.subTest(video=item['id']):
                old = build_video_metadata_payload(item['snippet']['channelId'], item, '2026-10-29T10:00:00+00:00',
                                                   parse_dt=parse_dt)
                self.assertEqual(cell(row, 'snippet.title')['value'], old['title'])
                self.assertEqual(datetime.fromisoformat(cell(row, 'snippet.publishedAt')['value'].replace('Z', '+00:00')),
                                 parse_dt(old['published_at']))
                self.assertEqual(cell(row, 'contentDetails.duration')['value'], youtube_duration_seconds(old['duration']))
                self.assertEqual(cell(row, 'snippet.tags[]')['value'], old['tags'])
                self.assertEqual(cell(row, 'snippet.categoryId')['value'], old['category_id'])
                self.assertEqual(cell(row, 'snippet.defaultLanguage')['value'], old['default_language'])
                self.assertEqual(cell(row, 'snippet.defaultAudioLanguage')['value'], old['default_audio_language'])
                self.assertEqual(cell(row, 'contentDetails.caption')['value'], old['caption_available'])
                self.assertEqual(cell(row, 'topicDetails.topicCategories[]')['value'], old['youtube_topic_categories'])
                end = cell(row, 'liveStreamingDetails.actualEndTime')
                self.assertEqual(None if end['state'] != 'known' else
                                 datetime.fromisoformat(end['value'].replace('Z', '+00:00')),
                                 parse_dt(old['stream_actual_end_at']) if old['stream_actual_end_at'] else None)
                self.assertEqual(cell(row, 'status.privacyStatus')['value'], item['status']['privacyStatus'])

    def test_channels_list_matches_channel_metadata_update_values(self):
        from collection.channel_metadata import channel_metadata_update_values
        fixture = load('youtube.channels.list', 'full')
        rows = canonical(run_case(fixture)[1])['rows']
        for item, row in zip(fixture['responses'][0]['body']['items'], rows):
            with self.subTest(channel=item['id']):
                old = channel_metadata_update_values(item, datetime(2026, 10, 29, tzinfo=timezone.utc))
                self.assertEqual(cell(row, 'snippet.title')['value'], old['title'])
                self.assertEqual(cell(row, 'snippet.thumbnails.default.url')['value'], old['avatar_url'])
                self.assertEqual(cell(row, 'contentDetails.relatedPlaylists.uploads')['value'], old['uploads_playlist_id'])
                self.assertEqual(cell(row, 'snippet.country')['value'].upper(), old['country_code'])
                self.assertEqual(parse_dt(cell(row, 'snippet.publishedAt')['value']), parse_dt(old['channel_created_at']))
                self.assertEqual(cell(row, 'statistics.viewCount')['value'], old['views'])
                self.assertEqual(cell(row, 'statistics.videoCount')['value'], old['video_count'])
                subscribers = cell(row, 'statistics.subscriberCount')
                self.assertEqual(subscribers.get('value'), old['subscribers'])
                self.assertEqual(subscribers['state'], 'known' if old['subscribers'] is not None else 'owner_only')

    def test_batch_get_stats_matches_normalize_batch_stats_response(self):
        from youtube_batch_stats import normalize_batch_stats_response
        fixture = load('youtube.videos.batchGetStats', 'full')
        value = canonical(run_case(fixture)[1])
        old = normalize_batch_stats_response(fixture['responses'][0]['body'], fixture['request']['parameters']['id'])
        by_id = {row['identity']['id']: row for row in value['rows']}
        for item in old['items']:
            row = by_id[item['id']]
            for path, key in (('statistics.viewCount', 'views'), ('statistics.likeCount', 'like_count'),
                              ('statistics.commentCount', 'comment_count')):
                expected = item[key]
                actual = cell(row, path)
                if expected is None:
                    # Discovery types these counters int64: both owners refuse 2**64 - 1.
                    self.assertEqual(actual, {'state': 'invalid'})
                else:
                    self.assertEqual(actual['value'], expected)
        self.assertEqual({key for key, state in value['entities'].items() if state == 'failed'},
                         set(old['summary']['failed_ids']))

    def test_playlist_items_match_playlist_video_item(self):
        from typed_playlist import playlist_video_item
        fixture = load('youtube.playlistItems.list', 'full_two_pages')
        rows = canonical(run_case(fixture)[1])['rows']
        items = [item for response in fixture['responses'] for item in response['body']['items']]
        for item, row in zip(items, rows):
            old = playlist_video_item(item)
            self.assertEqual(cell(row, 'contentDetails.videoId')['value'], old['video_id'])
            self.assertEqual(parse_dt(cell(row, 'contentDetails.videoPublishedAt')['value']), parse_dt(old['published_at']))
        naive = canonical(run_case(load('youtube.playlistItems.list', 'unknown_enum_and_naive_date'))[1])['rows'][0]
        self.assertIsNone(playlist_video_item(load('youtube.playlistItems.list', 'unknown_enum_and_naive_date')
                                              ['responses'][0]['body']['items'][0]))
        self.assertEqual(cell(naive, 'contentDetails.videoPublishedAt')['state'], 'invalid')
        self.assertEqual(cell(naive, 'contentDetails.videoId')['state'], 'invalid')


    def test_reference_lists_match_the_category_exporter(self):
        """scripts/export_youtube_category_reference.py owns the stored labels; both read the same rows."""
        from scripts.export_youtube_category_reference import export_reference
        languages = load('youtube.i18nLanguages.list', 'full')['responses'][0]['body']
        categories = load('youtube.videoCategories.list', 'by_id_complete')['responses'][0]['body']

        def get(endpoint, params, **kwargs):
            return languages if endpoint == 'i18nLanguages' else categories
        old = export_reference(get, ['1', '10'], now=datetime(2026, 10, 29, tzinfo=timezone.utc))
        new_languages = {cell(row, 'snippet.hl')['value']: cell(row, 'snippet.name')['value']
                         for row in canonical(run_case(load('youtube.i18nLanguages.list', 'full'))[1])['rows']}
        new_categories = {row['identity']['id']: cell(row, 'snippet.title')['value']
                          for row in canonical(run_case(load('youtube.videoCategories.list', 'by_id_complete'))[1])['rows']}
        self.assertEqual(old['languages'], new_languages)
        self.assertEqual(old['categories']['en'], new_categories)


class ShapeSweepTests(unittest.TestCase):
    """Every subtree of the richest recorded answer of each method, replaced by another JSON shape.

    The result is always an AcquisitionResult that fits the wire (a kept, an invalid or a rejected value,
    row or page); an exception would surface as HTTP 500 after the quota was spent.
    """
    SHAPES = (None, [], {}, 'x', 1, True, [{}])

    @staticmethod
    def paths(value, prefix=()):
        yield prefix
        if isinstance(value, dict):
            for key, item in value.items():
                yield from ShapeSweepTests.paths(item, prefix + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value[:1]):
                yield from ShapeSweepTests.paths(item, prefix + (index,))

    @staticmethod
    def replaced(body, path, shape):
        if not path:
            return shape
        body = json.loads(json.dumps(body))
        node = body
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = shape
        return body

    def test_no_shape_of_a_recorded_answer_raises(self):
        import threading
        import time
        from provider_sources.acquire import acquire
        from provider_sources.continuation import ContinuationCodec
        from provider_sources.results import to_wire
        from provider_sources.transport import Exchange
        from provider_sources_support import NOW, OWNER_FIXTURES, body_bytes, credential_for, plan_for
        registry = load_registry()
        richest = {}
        for root in (FIXTURES, OWNER_FIXTURES):
            for method, name, fixture in cases(root=root):
                for index, response in enumerate(fixture['responses']):
                    if 'body' in response and 200 <= response['status'] < 300:
                        size = sum(1 for _ in self.paths(response['body']))
                        if size > richest.get(method, (0,))[0]:
                            richest[method] = (size, name, fixture, index)
        self.assertGreaterEqual(len(richest), 30)
        failures = []
        for method, (_, name, fixture, index) in sorted(richest.items()):
            plan = plan_for(fixture['request'], registry)
            for path in self.paths(fixture['responses'][index]['body']):
                for shape in self.SHAPES:
                    responses = list(fixture['responses'])
                    responses[index] = dict(responses[index], body=self.replaced(responses[index]['body'], path, shape))
                    sent = []

                    def send(plan, params, **kwargs):
                        response = responses[min(len(sent), len(responses) - 1)]
                        sent.append(1)
                        return Exchange(response['status'], body_bytes(response), None, None, None)
                    try:
                        result = acquire(plan, registry=registry, credential=credential_for(plan),
                                         cancelled=threading.Event(), deadline=time.monotonic() + 60, attempt_recorder=None,
                                         codec=ContinuationCodec(), send=send, clock=lambda: NOW)
                        json.dumps(to_wire(result, max_bytes=2 * 1024 * 1024), allow_nan=False)
                    except Exception as error:
                        failures.append((method, name, path, shape, type(error).__name__))
        self.assertEqual(failures[:10], [])


if __name__ == '__main__':
    if '--write' in sys.argv:
        write_goldens()
        print('goldens written')
    else:
        unittest.main()
