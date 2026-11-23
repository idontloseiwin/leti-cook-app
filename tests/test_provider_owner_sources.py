"""Owner OAuth / Analytics / Reporting through the source layer. No network, no sign-in.

* Planning: owner contexts only, scopes, path IDs, the Analytics report-table allow-list (no
  cartesian products), the Reporting media chain inputs, jobs.create stays a control operation.
* Golden parse of synthetic documented-shape fixtures under tests/fixtures/provider_sources/youtube_owner/.
  Rewrite after a reviewed change: python tests/test_provider_owner_sources.py --write
* Fault matrix: owner_auth_expired, insufficient scope, not the owner's object, quota, provider
  refusals per family; media bounds; secrets never leave.
* Transport: three fixed bases; the Analytics and Reporting hosts accept only an owner token.
* Over HTTP (no owner credential provider exists) every owner context answers 409 owner_auth_unavailable.
"""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from provider_sources_support import (NOW, OWNER_CHANNEL, OWNER_CONTEXT, OWNER_FIXTURES, OWNER_TOKEN, ROOT, SCOPE,
                                      FakeSender, canonical, cases, load, plan_for, run_case)

import provider_runtime  # noqa: E402
import youtube_client  # noqa: E402
from provider_credentials import OwnerBearer  # noqa: E402
from provider_sources import owner_sources as analytics  # noqa: E402
from provider_sources.acquire import acquire  # noqa: E402
from provider_sources.continuation import ContinuationCodec  # noqa: E402
from provider_sources.credentials import (Credential, OwnerAuthExpired, OwnerAuthRevoked, OwnerBinding, OwnerContext,  # noqa: E402
                                          PublicKeyContext)
from provider_sources.errors import PlanRejected, ResponseInvalid  # noqa: E402
from provider_sources.families import policy  # noqa: E402
from provider_sources.plan import LIMITS, OWNER_LIMITS, AcquisitionRequest, Budget, plan_request  # noqa: E402
from provider_sources.registry import load_registry  # noqa: E402
from provider_sources.selection import FieldSelection  # noqa: E402
from provider_sources.service import AcquisitionService, ServiceRefused  # noqa: E402
from self_hosted_configuration import ConfigurationStore  # noqa: E402

NOW_TOKEN = 'synthetic-owner-token-' + 'z' * 24
KEY = 'synthetic_installation_key_no_requests'
REQUIRED_CASES = {
    'youtube.captions.list': {'full', 'ids_only', 'empty_list', 'unknown_enum_and_field', 'not_owner', 'quota'},
    'youtube.captions.download': {'vtt', 'srt_translated', 'original_sbv', 'original_unrecognized', 'format_mismatch',
                                  'binary_body', 'not_editable', 'not_found', 'could_not_convert'},
    'youtube.liveBroadcasts.list': {'mine_two_pages', 'by_id_omitted', 'empty_list', 'not_enabled', 'auth_expired',
                                    'scope_insufficient'},
    'youtube.liveChatModerators.list': {'full', 'chat_not_found', 'not_chat_owner'},
    'youtube.liveStreams.list': {'mine_cdn_secrets_dropped', 'empty_list'},
    'youtube.members.list': {'all_current_two_pages', 'updates_first_poll', 'filter_by_member', 'memberships_not_enabled'},
    'youtube.membershipsLevels.list': {'full', 'no_levels', 'memberships_not_enabled'},
    'youtube.playlistImages.list': {'by_playlist', 'empty_list'},
    'youtube.superChatEvents.list': {'full', 'empty_list'},
    'youtube.videoAbuseReportReasons.list': {'full', 'default_selection'},
    'youtube.videos.getRating': {'full', 'omitted_and_unknown_enum'},
    'youtube.subscriptions.list': {'my_subscribers', 'mine'},
    'youtube.channels.list': {'mine_owner_status'},
    'youtube.videos.list': {'my_rating_owner_parts'},
    'youtube.playlists.list': {'mine_private'},
    'youtubeAnalytics.reports.query': {'daily_two_pages', 'top_videos', 'monetary', 'no_data', 'columns_mismatch',
                                       'query_not_supported', 'not_owner', 'quota', 'auth_expired'},
    'youtubeAnalytics.groups.list': {'mine_two_pages', 'empty'},
    'youtubeAnalytics.groupItems.list': {'full', 'group_not_found'},
    'youtubereporting.reportTypes.list': {'full', 'empty'},
    'youtubereporting.jobs.list': {'full', 'no_jobs'},
    'youtubereporting.jobs.get': {'full', 'not_found'},
    'youtubereporting.jobs.reports.list': {'backfill', 'job_not_found'},
    'youtubereporting.jobs.reports.get': {'full'},
    'youtubereporting.media.download': {'csv_full', 'header_only', 'download_url_foreign_host', 'wrong_report_type',
                                        'row_budget', 'report_not_found'},
}
EXPECTED = {   # (completeness, stop_reason) of every fault case: the 02 design table 3.5 for owner contexts
    ('youtube.captions.list', 'quota'): ('partial', 'quota_exhausted:general'),
    ('youtube.liveBroadcasts.list', 'auth_expired'): ('partial', 'owner_auth_expired'),
    ('youtube.liveBroadcasts.list', 'scope_insufficient'): ('partial', 'owner_scope_insufficient'),
    ('youtubeAnalytics.reports.query', 'auth_expired'): ('partial', 'owner_auth_expired'),
    ('youtubeAnalytics.reports.query', 'quota'): ('partial', 'quota_exhausted:youtube_analytics'),
    ('youtubeAnalytics.reports.query', 'query_not_supported'): ('partial', 'combination_unsupported'),
    ('youtube.captions.download', 'could_not_convert'): ('partial', 'caption_not_convertible'),
    ('youtube.captions.download', 'format_mismatch'): ('partial', 'response_invalid'),
    ('youtube.captions.download', 'binary_body'): ('partial', 'response_invalid'),
    ('youtubeAnalytics.reports.query', 'columns_mismatch'): ('partial', 'response_invalid'),
    ('youtubereporting.media.download', 'download_url_foreign_host'): ('partial', 'response_invalid'),
    ('youtubereporting.media.download', 'wrong_report_type'): ('partial', 'response_invalid'),
    ('youtubereporting.media.download', 'row_budget'): ('partial', 'budget_rows'),
    ('youtube.members.list', 'updates_first_poll'): ('partial', 'updates_poll'),
    ('youtube.subscriptions.list', 'my_subscribers'): ('partial', 'budget_requests'),
}
ENTITIES = {   # documented refusals about one object: the target's state, the acquisition is complete
    ('youtube.captions.list', 'not_owner'): {'vidxxxxxxx2': 'access_denied'},
    ('youtube.captions.download', 'not_editable'): {'AUieDaSyntheticTrack2': 'access_denied'},
    ('youtube.captions.download', 'not_found'): {'AUieDaSyntheticTrack9': 'not_found'},
    ('youtube.liveBroadcasts.list', 'not_enabled'): {'mine': 'access_denied'},
    ('youtube.liveChatModerators.list', 'chat_not_found'): {'Cg0KC2xpdmVjaGF0aWQ': 'not_found'},
    ('youtube.liveChatModerators.list', 'not_chat_owner'): {'Cg0KC2xpdmVjaGF0aWQ': 'access_denied'},
    ('youtube.members.list', 'memberships_not_enabled'): {'youtube.members.list': 'disabled'},
    ('youtube.membershipsLevels.list', 'memberships_not_enabled'): {'youtube.membershipsLevels.list': 'disabled'},
    ('youtubeAnalytics.reports.query', 'not_owner'): {'youtubeAnalytics.reports.query': 'access_denied'},
    ('youtubeAnalytics.groupItems.list', 'group_not_found'): {'group-synthetic-9': 'not_found'},
    ('youtubereporting.jobs.get', 'not_found'): {'job-synthetic-9': 'not_found'},
    ('youtubereporting.jobs.reports.list', 'job_not_found'): {'job-synthetic-9': 'not_found'},
    ('youtubereporting.media.download', 'report_not_found'): {'report-2': 'not_found'},
}


def owner_plan(method, parameters, parts=(), fields=(), *, auth=OWNER_CONTEXT, budget=None):
    return plan_request(AcquisitionRequest(method, parameters, FieldSelection(frozenset(parts), frozenset(fields)),
                                           auth, budget), load_registry())


def results_by_method():
    found = {}
    for method, name, fixture in cases(root=OWNER_FIXTURES):
        found.setdefault(method, {})[name] = canonical(run_case(fixture)[1])
    return found


def golden_path(method):
    return OWNER_FIXTURES / method / 'golden.json'


def write_goldens():
    for method, values in results_by_method().items():
        golden_path(method).write_bytes((json.dumps(values, indent=1, sort_keys=True, ensure_ascii=False) + '\n')
                                        .encode('utf-8'))


def cell(row, path):
    return row['fields'][path]


DAY = {'ids': 'channel==MINE', 'startDate': '2026-09-01', 'endDate': '2026-09-30'}


class FixtureContractTests(unittest.TestCase):
    def test_every_owner_fixture_is_synthetic_documented_and_owner(self):
        seen = {}
        for method, name, fixture in cases(root=OWNER_FIXTURES):
            seen.setdefault(method, set()).add(name)
            with self.subTest(method=method, case=name):
                self.assertIs(fixture['synthetic'], True)
                self.assertTrue(fixture['source_url'].startswith('https://developers.google.com/youtube/'))
                self.assertEqual(fixture['request']['context'], 'owner')
                self.assertTrue(fixture['case'].strip())
        self.assertEqual(set(REQUIRED_CASES), set(seen))
        for method, names in REQUIRED_CASES.items():
            self.assertLessEqual(names, seen[method], method)
        self.assertLessEqual(policy.OWNER_ONLY_FAMILIES, set(seen))

    def test_results_equal_the_reviewed_goldens(self):
        self.maxDiff = None
        for method, values in results_by_method().items():
            with self.subTest(method=method):
                self.assertEqual(values, json.loads(golden_path(method).read_text(encoding='utf-8')))

    def test_fault_and_target_outcomes(self):
        for method, name, fixture in cases(root=OWNER_FIXTURES):
            value = canonical(run_case(fixture)[1])
            with self.subTest(method=method, case=name):
                if (method, name) in EXPECTED:
                    self.assertEqual((value['completeness'], value['stop_reason']), EXPECTED[(method, name)])
                else:
                    self.assertEqual((value['completeness'], value['stop_reason']), ('complete', None))
                if (method, name) in ENTITIES:
                    self.assertEqual(value['entities'], ENTITIES[(method, name)])
                    self.assertEqual(value['rows'], [])
                for page in value['pages']:
                    self.assertTrue(page['context'].startswith('owner:'))   # owner rows carry their binding

    def test_no_marker_secret_token_or_capability_leaves(self):
        for method, name, fixture in cases(root=OWNER_FIXTURES):
            text = json.dumps(canonical(run_case(fixture)[1]), ensure_ascii=False)
            with self.subTest(method=method, case=name):
                for marker in ('MARKER-SECRET', 'MARKER-ERROR', 'MARKER-TOKEN', 'MARKER-UNKNOWN',
                               'youtubereporting.googleapis.com', 'MARKER-CHANNEL-REF', OWNER_TOKEN, 'rtmp://'):
                    self.assertNotIn(marker, text)


class OwnerFamilySemanticsTests(unittest.TestCase):
    def test_caption_files_are_bounded_text(self):
        value = canonical(run_case(load('youtube.captions.download', 'vtt', OWNER_FIXTURES))[1])
        row = value['rows'][0]
        self.assertEqual((row['identity'], value['unit']), ({'kind': 'caption', 'id': 'AUieDaSyntheticTrack1'},
                                                            'caption_file'))
        self.assertEqual((cell(row, 'format')['value'], cell(row, 'cue_count')['value'], cell(row, 'line_count')['value']),
                         ('vtt', 3, 10))
        self.assertTrue(cell(row, 'text')['value'].startswith('WEBVTT'))
        srt = canonical(run_case(load('youtube.captions.download', 'srt_translated', OWNER_FIXTURES))[1])['rows'][0]
        self.assertEqual(set(srt['fields']), {'format', 'cue_count', 'byte_length'})   # BOM stripped, srt detected
        self.assertEqual(cell(srt, 'cue_count')['value'], 2)
        plain = canonical(run_case(load('youtube.captions.download', 'original_unrecognized', OWNER_FIXTURES))[1])
        self.assertEqual(cell(plain['rows'][0], 'format'), {'state': 'unknown_enum', 'value': 'unrecognized'})
        self.assertEqual(cell(plain['rows'][0], 'cue_count'), {'state': 'absent', 'absence': 'absent_means_not_applicable'})
        plan, _, sender = run_case(load('youtube.captions.download', 'srt_translated', OWNER_FIXTURES))
        self.assertEqual((sender.calls[0], sender.targets[0]), ({'tfmt': 'srt', 'tlang': 'de'},
                                                                ('captions/AUieDaSyntheticTrack1', 'captions.download')))
        self.assertEqual((plan.strategy, dict(plan.worst_cost)), ('media', {'general': 200}))

    def test_live_stream_ingestion_secrets_are_never_requested_or_returned(self):
        plan, result, sender = run_case(load('youtube.liveStreams.list', 'mine_cdn_secrets_dropped', OWNER_FIXTURES))
        self.assertNotIn('ingestionInfo', plan.fields_mask)
        self.assertIn('cdn(frameRate,ingestionType,resolution)', plan.fields_mask)
        row = canonical(result)['rows'][0]
        self.assertEqual(cell(row, 'status.healthStatus.configurationIssues[].type')['value'],
                         [{'state': 'known', 'value': 'gopSizeLong'}])
        self.assertNotIn('ingestionInfo', json.dumps(row))
        for path in ('cdn.ingestionInfo.streamName', 'cdn.ingestionInfo.ingestionAddress'):
            with self.assertRaises(PlanRejected) as raised:
                owner_plan('youtube.liveStreams.list', {'mine': True}, ('cdn',), (path,))
            self.assertEqual(raised.exception.code, 'field_not_selectable')

    def test_members_updates_mode_is_a_poll_never_complete(self):
        fixture = load('youtube.members.list', 'updates_first_poll', OWNER_FIXTURES)
        codec = ContinuationCodec()
        plan, first, _ = run_case(fixture, codec=codec)
        self.assertEqual((plan.max_pages, dict(plan.worst_cost)), (1, {'general': 2}))   # members.list: 2 units
        self.assertEqual((first.completeness.value, first.stop_reason, first.rows), ('partial', 'updates_poll', ()))
        self.assertIsNotNone(first.continuation)
        polled = copy.deepcopy(fixture)
        polled['responses'] = [{'status': 200, 'headers': {}, 'body': {
            'kind': 'youtube#memberListResponse', 'items': [], 'nextPageToken': 'MARKER-TOKEN-POLL-2',
            'pageInfo': {'totalResults': 0, 'resultsPerPage': 0}}}]
        _, second, sender = run_case(polled, codec=codec, continuation=first.continuation)
        self.assertEqual(sender.calls[0]['pageToken'], 'MARKER-TOKEN-POLL-1')    # the poll position is reused
        self.assertEqual(second.stop_reason, 'updates_poll')
        all_current = owner_plan('youtube.members.list', {}, ('snippet',), ('snippet.memberDetails.channelId',))
        with self.assertRaises(PlanRejected) as raised:   # a token is bound to its mode: another plan, another key
            run_case(dict(fixture, request=dict(fixture['request'], parameters={'mode': 'all_current'})),
                     codec=codec, continuation=first.continuation)
        self.assertEqual(raised.exception.code, 'continuation_plan_changed')
        self.assertNotEqual(all_current.fingerprint, plan.fingerprint)

    def test_reporting_media_chain_keeps_the_download_url_inside(self):
        plan, result, sender = run_case(load('youtubereporting.media.download', 'csv_full', OWNER_FIXTURES))
        self.assertEqual(sender.targets, [('jobs/job-synthetic-1/reports/report-2', 'reporting.reports'),
                                          ('media/CHANNEL/MARKER-CHANNEL-REF/jobs/job-synthetic-1/reports/report-2',
                                           'reporting.media')])
        self.assertEqual(sender.calls, [{}, {'alt': 'media'}])
        value = canonical(result)
        self.assertEqual((value['cost'], value['unit']), ({'youtube_reporting': 2}, 'report_csv_row'))
        self.assertEqual(value['pages'][1]['unknown_field_paths'], ['new_column_2027'])
        first, second = value['rows']
        self.assertEqual((cell(first, 'date')['value'], cell(first, 'views')['value'],
                          cell(first, 'watch_time_minutes')['value']), ('2026-10-01', 12, '30.5'))
        self.assertEqual(cell(second, 'video_id'), {'state': 'known_empty', 'value': ''})       # channel-level row
        self.assertEqual(cell(second, 'average_view_duration_percentage'), {'state': 'null_reported'})
        foreign = run_case(load('youtubereporting.media.download', 'download_url_foreign_host', OWNER_FIXTURES))
        self.assertEqual((len(foreign[2].calls), foreign[1].notes), (1, ('report_download_url_invalid',)))

    def test_download_urls_are_checked_exactly(self):
        good = 'https://youtubereporting.googleapis.com/v1/media/CHANNEL/abc_DEF-1/jobs/j-1/reports/r-1?alt=media'
        self.assertEqual(analytics.report_resource_name(good), 'CHANNEL/abc_DEF-1/jobs/j-1/reports/r-1')
        for bad in ('http://youtubereporting.googleapis.com/v1/media/x', 'https://youtubereporting.googleapis.com.evil/v1/media/x',
                    'https://user@youtubereporting.googleapis.com/v1/media/x',
                    'https://youtubereporting.googleapis.com:8443/v1/media/x',
                    'https://youtubereporting.googleapis.com/v1/other/x', 'https://youtubereporting.googleapis.com/v1/media/',
                    'https://youtubereporting.googleapis.com/v1/media/a/../b', 'https://youtubereporting.googleapis.com/v1/media/a%2Fb',
                    'https://youtubereporting.googleapis.com/v1/media/a?alt=media&x=1',
                    'https://youtubereporting.googleapis.com/v1/media/a#f', 'https://youtubereporting.googleapis.com/v1/media/' + 'a' * 1000,
                    None, 7):
            with self.subTest(url=bad), self.assertRaises(ResponseInvalid):
                analytics.report_resource_name(bad)

    def test_reporting_backfill_and_pst_days(self):
        value = canonical(run_case(load('youtubereporting.jobs.reports.list', 'backfill', OWNER_FIXTURES))[1])
        self.assertEqual(value['notes'], ['superseded_reports:1'])
        result = run_case(load('youtubereporting.jobs.reports.list', 'backfill', OWNER_FIXTURES))[1]
        self.assertEqual(analytics.superseded_reports(result.rows), ['report-1'])
        self.assertEqual([analytics.pst_report_day(row) for row in result.rows], ['2026-10-01', '2026-10-01', '2026-10-02'])
        self.assertNotIn('downloadUrl', json.dumps(value))

    def test_analytics_rows_are_typed_by_column_headers_and_paged_by_start_index(self):
        plan, result, sender = run_case(load('youtubeAnalytics.reports.query', 'daily_two_pages', OWNER_FIXTURES))
        self.assertEqual([(call['startIndex'], call['maxResults']) for call in sender.calls], [('1', '2'), ('3', '2')])
        self.assertTrue(all('part' not in call and 'fields' not in call for call in sender.calls))
        self.assertEqual(plan.strategy, 'start_index')
        value = canonical(result)
        self.assertEqual([row['identity'] for row in value['rows']],
                         [{'kind': 'report_row', 'id': f'day=2026-09-0{day}'} for day in (1, 2, 3)])
        self.assertEqual(cell(value['rows'][0], 'averageViewPercentage'), {'state': 'known', 'value': '41.5'})
        self.assertEqual(cell(value['rows'][2], 'views'), {'state': 'known', 'value': 0})   # zero is a known value
        top_plan, _, top = run_case(load('youtubeAnalytics.reports.query', 'top_videos', OWNER_FIXTURES))
        self.assertEqual(top_plan.strategy, 'single')
        self.assertEqual(top.calls[0]['sort'], '-views')
        self.assertEqual(top.calls[0]['maxResults'], '10')
        self.assertEqual(top.calls[0]['ids'], 'channel==' + OWNER_CHANNEL)
        money = canonical(run_case(load('youtubeAnalytics.reports.query', 'monetary', OWNER_FIXTURES))[1])
        self.assertEqual((money['rows'][0]['identity'], cell(money['rows'][0], 'estimatedRevenue')['value']),
                         (None, '12.345678'))

    def test_array_of_objects_keeps_one_state_per_element(self):
        rows = canonical(run_case(load('youtube.liveBroadcasts.list', 'mine_two_pages', OWNER_FIXTURES))[1])['rows']
        self.assertEqual(cell(rows[0], 'contentDetails.availabilityConfig.regionsConfig.regionIntervals[].interval.endTime'),
                         {'state': 'known', 'value': [{'state': 'absent', 'absence': 'absent_unknown'},
                                                      {'state': 'known', 'value': '2026-10-03T18:00:00Z'}]})
        default = canonical(run_case(load('youtube.videoAbuseReportReasons.list', 'default_selection', OWNER_FIXTURES))[1])
        self.assertEqual(set(default['rows'][0]['fields']), {'id', 'snippet.label', 'snippet.secondaryReasons[].id',
                                                             'snippet.secondaryReasons[].label'})
        named = canonical(run_case(load('youtube.videoAbuseReportReasons.list', 'full', OWNER_FIXTURES))[1])
        self.assertEqual(cell(named['rows'][0], 'snippet.secondaryReasons[].id')['value'],
                         [{'state': 'known', 'value': '32'}, {'state': 'known', 'value': '33'}])
        self.assertEqual(cell(named['rows'][1], 'snippet.secondaryReasons[].id'), {'state': 'absent', 'absence': 'absent_unknown'})

    def test_memberships_states_differ_from_an_empty_list(self):
        empty = canonical(run_case(load('youtube.membershipsLevels.list', 'no_levels', OWNER_FIXTURES))[1])
        disabled = canonical(run_case(load('youtube.membershipsLevels.list', 'memberships_not_enabled', OWNER_FIXTURES))[1])
        self.assertEqual((empty['entities'], disabled['entities']), ({}, {'youtube.membershipsLevels.list': 'disabled'}))


class OwnerPlanTests(unittest.TestCase):
    def refused(self, code, *args, **kwargs):
        with self.assertRaises(PlanRejected) as raised:
            owner_plan(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_owner_families_refuse_the_installation_key(self):
        for method in sorted(policy.OWNER_ONLY_FAMILIES):
            with self.subTest(method=method):
                self.refused('context_not_supported', method, {}, auth=PublicKeyContext())

    def test_scopes_are_checked_before_any_network(self):
        readonly = OwnerContext(OwnerBinding('b-2', OWNER_CHANNEL, frozenset({SCOPE + 'youtube.readonly'}), 'p'))
        self.refused('owner_scope_missing', 'youtube.members.list', {}, ('snippet',),
                     ('snippet.memberDetails.channelId',), auth=readonly)
        self.refused('owner_scope_missing', 'youtube.captions.list', {'videoId': 'vidxxxxxxx1'}, ('id',), auth=readonly)
        analytics_only = OwnerContext(OwnerBinding('b-3', OWNER_CHANNEL, frozenset({SCOPE + 'yt-analytics.readonly'}), 'p'))
        self.refused('owner_scope_missing', 'youtubeAnalytics.reports.query', dict(DAY, metrics='views'), auth=analytics_only)
        plain = OwnerContext(OwnerBinding('b-4', OWNER_CHANNEL, frozenset({SCOPE + 'youtube.readonly',
                                                                          SCOPE + 'yt-analytics.readonly'}), 'p'))
        self.assertEqual(owner_plan('youtubeAnalytics.reports.query', dict(DAY, metrics='views'), auth=plain).owner_scopes,
                         (SCOPE + 'youtube.readonly', SCOPE + 'yt-analytics.readonly'))
        self.refused('owner_scope_missing', 'youtubeAnalytics.reports.query', dict(DAY, metrics='views,estimatedRevenue'),
                     auth=plain)   # monetary metrics need yt-analytics-monetary.readonly
        money = owner_plan('youtubeAnalytics.reports.query', dict(DAY, metrics='views,estimatedRevenue'))
        self.assertIn(SCOPE + 'yt-analytics-monetary.readonly', money.owner_scopes)

    def test_path_ids_are_checked_and_never_raw(self):
        for value in ('a/b', '..', 'x?y', 'x#y', 'track id'):
            with self.subTest(value=value):
                self.refused('parameter_invalid', 'youtube.captions.download', {'id': value})
        self.refused('parameter_invalid', 'youtubereporting.jobs.get', {'jobId': 'j' * 41})
        self.refused('parameter_layer_managed', 'youtubereporting.media.download', {'resourceName': 'CHANNEL/x'})
        self.refused('parameter_layer_managed', 'youtubeAnalytics.reports.query', dict(DAY, metrics='views', startIndex=3))
        self.refused('parameter_required', 'youtubereporting.media.download', {'jobId': 'j-1', 'reportId': 'r-1'})
        self.refused('report_type_not_listed', 'youtubereporting.media.download',
                     {'jobId': 'j-1', 'reportId': 'r-1', 'reportTypeId': 'content_owner_basic_a3'})
        self.refused('field_unknown', 'youtubereporting.media.download',
                     {'jobId': 'j-1', 'reportId': 'r-1', 'reportTypeId': 'channel_basic_a3'}, (),
                     ('end_screen_element_clicks',))
        plan = owner_plan('youtubereporting.jobs.reports.get', {'jobId': 'j-1', 'reportId': 'r-1'})
        self.assertEqual((plan.endpoint, plan.requests[0].params, plan.filter_values), ('jobs/j-1/reports/r-1', (), ('r-1',)))

    def test_family_parameter_rules(self):
        self.refused('parameter_discovery_only', 'youtube.playlistImages.list', {'parent': 'PL' + 'a' * 32}, ('snippet',))
        self.refused('filter_exactly_one_required', 'youtube.playlistImages.list',
                     {'id': 'image-1', 'playlistId': 'PL' + 'a' * 32}, ('snippet',))
        self.refused('parameter_needs_filter', 'youtube.liveBroadcasts.list',
                     {'id': 'brdxxxxxxx1', 'broadcastType': 'all'}, ('snippet',))
        self.refused('parameter_value_unknown', 'youtube.liveBroadcasts.list', {'broadcastStatus': 'broadcastStatusFilterUnspecified'},
                     ('snippet',))
        self.refused('parameter_too_many_values', 'youtube.members.list',
                     {'filterByMemberChannelId': [f'UCmember{index:016d}' for index in range(101)]}, ('snippet',),
                     ('snippet.memberDetails.channelId',))
        self.refused('parameter_value_unknown', 'youtube.members.list', {'mode': 'listMembersModeUnknown'}, ('snippet',),
                     ('snippet.memberDetails.channelId',))
        self.refused('parameter_invalid', 'youtube.liveBroadcasts.list', {'mine': False}, ('snippet',))
        self.refused('parameter_invalid', 'youtubereporting.jobs.reports.list',
                     {'jobId': 'j-1', 'createdAfter': '2026-10-01 00:00:00'})
        self.refused('parameter_invalid', 'youtubereporting.reportTypes.list', {'pageSize': 51})
        self.refused('context_not_supported', 'youtubereporting.jobs.list', {'onBehalfOfContentOwner': 'x'})
        self.refused('parts_not_supported', 'youtube.videos.getRating', {'id': 'vidxxxxxxx1'}, ('snippet',))
        # The budget: 50/200-unit owner methods fit the owner ceiling, never the key ceiling.
        self.assertEqual(dict(OWNER_LIMITS.max_units)['general'], 200)
        self.refused('budget_invalid', 'youtube.captions.download', {'id': 'track-1'},
                     budget=Budget(max_units={'general': 201}))

    def test_owner_and_public_plans_never_share_a_continuation(self):
        public = plan_request(AcquisitionRequest('youtube.playlists.list', {'channelId': OWNER_CHANNEL},
                                                 FieldSelection(frozenset({'snippet'}))), load_registry())
        owner = owner_plan('youtube.playlists.list', {'channelId': OWNER_CHANNEL}, ('snippet',),
                           budget=LIMITS)
        self.assertNotEqual(public.fingerprint, owner.fingerprint)
        self.assertEqual((public.context, owner.context), ('public_key', 'owner:synthetic-binding'))
        codec = ContinuationCodec()
        token = codec.encode(fingerprint=owner.fingerprint, context=owner.context, position={'token': 'x'}, now=NOW)
        with self.assertRaises(PlanRejected) as raised:
            codec.decode(token, fingerprint=owner.fingerprint, context=public.context, now=NOW)
        self.assertEqual(raised.exception.code, 'continuation_plan_changed')

    def test_reporting_jobs_create_stays_a_control_operation(self):
        self.refused('method_control_not_readable', 'youtubereporting.jobs.create', {})
        self.refused('method_control_not_readable', 'youtubereporting.jobs.delete', {})
        sources = ROOT / 'application/provider_sources'
        for path in sources.rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            with self.subTest(path=path.name):
                self.assertNotIn('requests.post', text)
                self.assertNotIn('jobs.create(', text)
        self.assertEqual(load_registry().methods['youtubereporting.jobs.list'].http_method, 'GET')


class AnalyticsAllowListTests(unittest.TestCase):
    def plan(self, **parameters):
        return owner_plan('youtubeAnalytics.reports.query', dict(DAY, **parameters))

    def refused(self, code, **parameters):
        with self.assertRaises(PlanRejected) as raised:
            self.plan(**parameters)
        self.assertEqual(raised.exception.code, code)

    def test_documented_combinations_plan_with_their_tables(self):
        value = self.plan(metrics='views', dimensions='country', filters='continent==150')
        self.assertIn('video.geo_country', value.options['tables'])
        self.assertIn('video.geo_country', value.preview()['report_tables'])
        self.assertEqual(self.plan(metrics='views', dimensions='province', filters='country==US').options['tables'][:1],
                         ['video.geo_province'])
        self.assertIn('video.audience_retention',
                      self.plan(metrics='audienceWatchRatio', dimensions='elapsedVideoTimeRatio',
                                filters='video==vidxxxxxxx1').options['tables'])
        self.assertEqual(self.plan(metrics='views', dimensions='insightPlaybackLocationDetail',
                                   filters='insightPlaybackLocationType==EMBEDDED', sort='-views', maxResults=25)
                         .options['tables'], ['video.playback_location_detail'])

    def test_combinations_outside_the_tables_are_refused_before_sending(self):
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='day,country')   # no such table
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='video')          # top: needs sort
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='video', sort='-views', maxResults=201)
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='video', sort='-likes', maxResults=5)
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='province', filters='country==FR')
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='insightTrafficSourceDetail',
                     filters='insightTrafficSourceType==NOTIFICATION', sort='-views', maxResults=10)
        self.refused('analytics_combination_not_listed', metrics='audienceWatchRatio', dimensions='elapsedVideoTimeRatio',
                     filters='video==vidxxxxxxx1,vidxxxxxxx2')
        self.refused('analytics_combination_not_listed', metrics='views,notAMetric')
        self.refused('analytics_combination_not_listed', metrics='views', dimensions='city', sort='-views', maxResults=10)
        self.refused('analytics_combination_not_listed', metrics='views', sort='likes')
        self.refused('analytics_filter_single_value', metrics='views', filters='group==g-1,g-2')
        self.refused('analytics_filter_single_value', metrics='views', filters='country==DE,FR')
        self.refused('parameter_invalid', metrics='views', filters='country==germany')
        self.refused('analytics_filter_repeated', metrics='views', filters='country==DE;country==FR')
        self.refused('analytics_names_repeated', metrics='views,views')
        self.refused('analytics_dates_invalid', metrics='views', startDate='2026-10-01', endDate='2026-09-01')
        self.refused('parameter_invalid', metrics='views', startDate='2026-02-30')
        self.refused('parameter_needs_filter', metrics='views', currency='EUR')
        self.refused('analytics_ids_not_owner', metrics='views', ids='channel==UC' + 'b' * 22)
        self.refused('parameter_invalid', metrics='views', ids='contentOwner==partner')
        self.refused('context_not_supported', metrics='views', includeHistoricalChannelData=True)

    def test_every_listed_table_accepts_a_minimal_request_and_refuses_an_extra_dimension(self):
        tables = analytics.owner_reports()['analytics']['tables']
        self.assertEqual(len(tables), 44)
        samples = {'province': 'US-CA', 'country': 'US', 'video': 'vidxxxxxxx1', 'playlist': 'PL' + 'a' * 32,
                   'group': 'group-1', 'insightTrafficSourceType': 'YT_SEARCH', 'insightPlaybackLocationType': 'EMBEDDED'}
        for key, table in sorted(tables.items()):
            def first(groups):
                names = []
                for group in groups:
                    if group['rule'] == 'required':
                        names.extend(group['values'])
                    elif group['rule'] in ('exactly_one', 'one_or_more'):
                        names.append(group['values'][0])
                return names
            dimensions, metrics = first(table['dimensions']), first(table['metrics'])
            filters = {name: table['filter_values'].get(name, [samples[name]])[0] for name in first(table['filters'])}
            parameters = {'metrics': ','.join(metrics)}
            if table['sort']['required']:
                metrics = metrics if table['sort']['allowed'][0][1:] in metrics else metrics + [table['sort']['allowed'][0][1:]]
                parameters = {'metrics': ','.join(metrics), 'sort': table['sort']['allowed'][0]}
            if dimensions:
                parameters['dimensions'] = ','.join(dimensions)
            if filters:
                parameters['filters'] = ';'.join(f'{name}=={value}' for name, value in filters.items())
            if table['max_results']:
                parameters['maxResults'] = table['max_results']
            with self.subTest(table=key):
                self.assertIn(key, self.plan(**parameters).options['tables'])
                with self.assertRaises(PlanRejected):
                    self.plan(**dict(parameters, dimensions=','.join(dimensions + ['notADimension'])))



class Response:
    def __init__(self, status, payload, headers=None, raw=None):
        self.status_code, self.payload = status, payload
        self.headers = dict(headers or {'Date': 'Thu, 29 Oct 2026 10:00:00 GMT'})
        self.raw_body = raw

    def json(self):
        return self.payload

    @property
    def content(self):
        return getattr(self, '_content', b'')   # the transport caches the bounded body here

    def iter_content(self, chunk_size):
        return iter((self.raw_body if self.raw_body is not None else json.dumps(self.payload).encode(),))

    def close(self):
        pass


class Recorder:
    def __init__(self):
        self.started = []

    def start_attempt(self, **values):
        self.started.append(values)
        return f'00000000-0000-4000-8000-{len(self.started):012d}'

    def finish_attempt(self, attempt_id, **values):
        return True


class TransportFamilyTests(unittest.TestCase):
    """Through the real shared transport (requests.get, admission and HOT replaced)."""

    def setUp(self):
        self.sent, self.answers = [], []
        self.recorder = Recorder()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        hot = types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None, HotRuntimeUnavailable=RuntimeError)
        for item in (patch.dict(sys.modules, {'hot_runtime': hot}),
                     patch('provider_runtime.admit_request', side_effect=lambda **kw: contextlib.nullcontext(
                         provider_runtime._UnregulatedRequest(kw.get('cancel')))),
                     patch.object(youtube_client.requests, 'get', self.http),
                     patch.object(youtube_client.time_trust, 'record_date'),
                     patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETUP_DIRECTORY': directory.name})):
            item.start()
            self.addCleanup(item.stop)
        self.store = ConfigurationStore.initialize(directory.name)
        self.store.update(self.store.read()['revision'], lambda v: v.update(
            api_key=KEY, prepared=True, phase='ready', workspace={'phase': 'complete', 'channels': {}}))

    def http(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return self.answers.pop(0)

    def run_plan(self, plan):
        return acquire(plan, registry=load_registry(), credential=Credential(plan.context, OwnerBearer(NOW_TOKEN)),
                       cancelled=threading.Event(), deadline=time.monotonic() + 30, attempt_recorder=self.recorder,
                       codec=ContinuationCodec())

    def test_each_family_reaches_only_its_fixed_base_with_the_owner_token(self):
        cases_ = (
            (owner_plan('youtubeAnalytics.reports.query', dict(DAY, metrics='views')),
             'https://youtubeanalytics.googleapis.com/v2/reports',
             {'kind': 'youtubeAnalytics#resultTable', 'columnHeaders': [
                 {'name': 'views', 'columnType': 'METRIC', 'dataType': 'INTEGER'}], 'rows': [[5]]}),
            (owner_plan('youtubereporting.jobs.list', {}), 'https://youtubereporting.googleapis.com/v1/jobs', {}),
            (owner_plan('youtube.membershipsLevels.list', {}, ('id',)), 'https://www.googleapis.com/youtube/v3/membershipsLevels',
             {'kind': 'youtube#membershipsLevelListResponse', 'items': []}),
        )
        with patch('self_hosted_configuration.provider_key', side_effect=AssertionError('installation key read')):
            for plan, url, body in cases_:
                with self.subTest(method=plan.method_id):
                    self.answers.append(Response(200, body))
                    result = self.run_plan(plan)
                    self.assertEqual(result.completeness.value, 'complete')
                    sent_url, kwargs = self.sent[-1]
                    self.assertEqual(sent_url, url)
                    self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer ' + NOW_TOKEN})
                    self.assertIs(kwargs['allow_redirects'], False)
        self.assertEqual([(item['endpoint'], item['quota_bucket'], item['quota_units']) for item in self.recorder.started],
                         [('analytics.reports', 'youtube_analytics', 1), ('reporting.jobs', 'youtube_reporting', 1),
                          ('membershipsLevels', 'general', 1)])
        self.assertIsNone(self.store.read()['error'])

    def test_owner_401_on_analytics_never_marks_the_installation_key(self):
        self.answers.append(Response(401, {'error': {'code': 401, 'status': 'UNAUTHENTICATED'}}))
        result = self.run_plan(owner_plan('youtubeAnalytics.groups.list', {'mine': True}))
        self.assertEqual(result.stop_reason, 'owner_auth_expired')
        self.assertIsNone(self.store.read()['error'])

    def test_media_download_chain_through_the_transport(self):
        report = {'id': 'r-1', 'jobId': 'j-1', 'startTime': '2026-10-01T08:00:00Z', 'endTime': '2026-10-02T08:00:00Z',
                  'createTime': '2026-10-02T09:00:00Z',
                  'downloadUrl': 'https://youtubereporting.googleapis.com/v1/media/CHANNEL/ref/jobs/j-1/reports/r-1?alt=media'}
        csv = ('date,channel_id,video_id,live_or_on_demand,subscribed_status,country_code,views\n'
               '20261001,' + OWNER_CHANNEL + ',vidxxxxxxx1,on_demand,subscribed,DE,4\n').encode()
        self.answers += [Response(200, report), Response(200, None, headers={'Content-Type': 'text/csv'}, raw=csv)]
        plan = owner_plan('youtubereporting.media.download',
                          {'jobId': 'j-1', 'reportId': 'r-1', 'reportTypeId': 'channel_basic_a3'}, (), ('views',))
        result = self.run_plan(plan)
        self.assertEqual([url for url, _ in self.sent], [
            'https://youtubereporting.googleapis.com/v1/jobs/j-1/reports/r-1',
            'https://youtubereporting.googleapis.com/v1/media/CHANNEL/ref/jobs/j-1/reports/r-1'])
        self.assertEqual(self.sent[1][1]['params'], {'alt': 'media'})
        self.assertEqual([item['endpoint'] for item in self.recorder.started], ['reporting.reports', 'reporting.media'])
        self.assertEqual((result.completeness.value, len(result.rows)), ('complete', 1))

    def test_owner_only_hosts_refuse_the_key_and_any_other_base(self):
        options = dict(phase='explicit_acquisition', attempt_recorder=self.recorder, params={})
        for family, bucket in (('analytics', 'youtube_analytics'), ('reporting', 'youtube_reporting')):
            with self.subTest(family=family), self.assertRaisesRegex(ValueError, 'needs_owner_credential'):
                youtube_client.youtube_api_get('reports', quota_bucket=bucket, endpoint_label='analytics.reports',
                                               api_family=family, **options)
        with self.assertRaisesRegex(ValueError, 'api_family_invalid'):
            youtube_client.youtube_api_get('reports', quota_bucket='general', endpoint_label='analytics.reports',
                                           api_family='https://evil.example', credential=OwnerBearer(NOW_TOKEN), **options)
        plan = owner_plan('youtube.captions.download', {'id': 'AUieDa=+track'})
        self.assertEqual(plan.endpoint, 'captions/AUieDa%3D%2Btrack')   # quoted, accepted by the transport below
        self.answers.append(Response(200, None, raw=b'WEBVTT\n'))
        self.assertEqual(self.run_plan(plan).completeness.value, 'complete')
        self.assertEqual(self.sent[-1][0], 'https://www.googleapis.com/youtube/v3/captions/AUieDa%3D%2Btrack')
        self.sent.clear()
        self.recorder.started.clear()
        for endpoint in ('media/../x', 'media//x', 'https://evil.example/x', 'reports?x=1', 'a b', 'media/a%2Fb',
                         'media/%2e%2e/x'):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, 'plain relative path'):
                youtube_client.youtube_api_get(endpoint, quota_bucket='youtube_reporting', endpoint_label='reporting.media',
                                               api_family='reporting', credential=OwnerBearer(NOW_TOKEN), **options)
        self.assertEqual((self.sent, self.recorder.started), ([], []))


class OwnerServiceTests(unittest.TestCase):
    """The HTTP service: without an owner credential provider, owner contexts return 409."""

    def service(self, **kwargs):
        return AcquisitionService(attempt_recorder=None, ledger_reader=lambda day, quota_bucket='general': ([], False),
                                  **kwargs)

    def document(self, fixture):
        return json.dumps(fixture['request']).encode()

    def test_without_an_owner_provider_every_owner_context_is_409(self):
        service = self.service()
        for method, name in (('youtubeAnalytics.reports.query', 'daily_two_pages'),
                             ('youtube.captions.download', 'vtt'), ('youtubereporting.media.download', 'csv_full')):
            fixture = load(method, name, OWNER_FIXTURES)
            with self.subTest(method=method):
                with self.assertRaises(ServiceRefused) as raised:
                    service.preview(self.document(fixture))
                self.assertEqual((raised.exception.code, raised.exception.status), ('owner_auth_unavailable', 409))
                body = json.dumps({'version': 1, 'request': fixture['request'], 'plan_fingerprint': '0' * 64,
                                   'confirmed_cost': {}}).encode()
                with self.assertRaises(ServiceRefused) as raised:
                    service.execute(body, cancelled=threading.Event())
                self.assertEqual((raised.exception.code, raised.exception.status), ('owner_auth_unavailable', 409))

    def test_a_fake_owner_provider_runs_the_owner_plan_with_its_scopes(self):
        asked = []

        class Provider:
            def binding(self):
                return OWNER_CONTEXT.binding

            def access_token(self, *, scopes, deadline):
                asked.append(scopes)
                return OwnerBearer(NOW_TOKEN)

        fixture = load('youtube.captions.list', 'full', OWNER_FIXTURES)
        service = self.service(owner_provider=Provider(), send=FakeSender(fixture['responses']))
        preview = service.preview(self.document(fixture))
        self.assertEqual((preview['worst_cost'], preview['context']), ({'general': 50}, 'owner:synthetic-binding'))
        body = json.dumps({'version': 1, 'request': fixture['request'], 'plan_fingerprint': preview['plan_fingerprint'],
                           'confirmed_cost': preview['worst_cost']}).encode()
        with patch.dict(sys.modules, {'hot_runtime': types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None)}):
            answer = service.execute(body, cancelled=threading.Event())
        self.assertEqual((answer['completeness'], len(answer['rows'])), ('complete', 2))
        self.assertEqual(asked, [frozenset({SCOPE + 'youtube.force-ssl'})])
        self.assertEqual(service.cap.reading()['used_units'], 50)   # the owner's units count in the daily cap

    def test_owner_provider_refusals_are_fixed_409_codes(self):
        class Expired:
            def binding(self):
                return OWNER_CONTEXT.binding

            def access_token(self, *, scopes, deadline):
                raise OwnerAuthExpired()

        class Rebound(Expired):
            """The binding changes between reading the request and resolving the token."""
            calls = 0

            def binding(self):
                self.calls += 1
                return OWNER_CONTEXT.binding if self.calls == 1 else OwnerBinding(
                    'synthetic-binding', 'UC' + 'r' * 22, OWNER_CONTEXT.binding.scopes, 'p')

        class Other(Expired):
            def binding(self):
                return OwnerBinding('other-binding', OWNER_CHANNEL, OWNER_CONTEXT.binding.scopes, 'p')

        class Revoked(Expired):
            """The owner withdrew the grant: a fixed 409 as well, nothing is sent."""
            def access_token(self, *, scopes, deadline):
                raise OwnerAuthRevoked()

        fixture = load('youtube.membershipsLevels.list', 'full', OWNER_FIXTURES)
        for provider, code in ((Expired(), 'owner_auth_expired'), (Revoked(), 'owner_auth_revoked'),
                               (Rebound(), 'owner_binding_changed'),
                               (Other(), 'plan_changed')):   # another binding is another plan
            service = self.service(owner_provider=provider, send=FakeSender(fixture['responses']))
            plan = plan_for(fixture['request'])
            body = json.dumps({'version': 1, 'request': fixture['request'], 'plan_fingerprint': plan.fingerprint,
                               'confirmed_cost': dict(plan.worst_cost)}).encode()
            with self.subTest(code=code), self.assertRaises(ServiceRefused) as raised, \
                    patch.dict(sys.modules, {'hot_runtime': types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None)}):
                service.execute(body, cancelled=threading.Event())
            self.assertEqual((raised.exception.code, raised.exception.status), (code, 409))


class OwnerStatesTests(unittest.TestCase):
    def test_owner_methods_are_fixture_tested_and_never_live_verified(self):
        states = json.loads((ROOT / 'application/provider_sources/registry/youtube.states.json').read_text(encoding='utf-8'))
        for method in sorted(policy.OWNER_ONLY_FAMILIES):
            record = states['methods'][method]
            with self.subTest(method=method):
                self.assertIn(record['request_supported']['value'], ('yes', 'partial'))
                self.assertIn(record['parse_supported']['value'], ('yes', 'partial'))
                self.assertEqual(record['live_verified']['value'], 'no')

    def test_preview_of_owner_families_never_loads_the_transport(self):
        code = ("import sys; sys.path.insert(0, 'application'); "
                "from provider_sources.credentials import OwnerBinding, OwnerContext; "
                "OWNER_CONTEXT = OwnerContext(OwnerBinding('b', 'UC' + 'o' * 22, frozenset("
                "'https://www.googleapis.com/auth/' + s for s in ('youtube.readonly', 'youtube.force-ssl', "
                "'yt-analytics.readonly')), 'p')); "
                "from provider_sources.plan import AcquisitionRequest, plan_request; "
                "from provider_sources.registry import load_registry; "
                "from provider_sources.selection import FieldSelection as F; r = load_registry(); "
                "plan_request(AcquisitionRequest('youtubeAnalytics.reports.query', {'ids': 'channel==MINE', "
                "'startDate': '2026-09-01', 'endDate': '2026-09-30', 'metrics': 'views'}, F(frozenset()), OWNER_CONTEXT, None), r); "
                "plan_request(AcquisitionRequest('youtubereporting.media.download', {'jobId': 'j', 'reportId': 'r', "
                "'reportTypeId': 'channel_basic_a3'}, F(frozenset()), OWNER_CONTEXT, None), r); "
                "plan_request(AcquisitionRequest('youtube.captions.download', {'id': 't'}, F(frozenset()), OWNER_CONTEXT, None), r); "
                "print(sorted(m for m in sys.modules if m in ('youtube_client', 'provider_runtime', 'requests', "
                "'provider_sources.transport', 'provider_sources.acquire', 'provider_sources.parse', 'core')))")
        environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP') if key in os.environ}
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], cwd=ROOT, capture_output=True, text=True,
                                timeout=60, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')


if __name__ == '__main__':
    if '--write' in sys.argv:
        write_goldens()
        print('goldens written')
    else:
        unittest.main()
