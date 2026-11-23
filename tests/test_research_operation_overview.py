"""Overview operation shapes, periods, populations and local publication dates."""
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]

from db.reads.research_aggregate import compile_operation
from db.reads.research_sql import compile_query, stream_completion_sql, stream_end_order_sql
from db.reads.research_observations import AGE_FROM
from research_query import aggregate
from research_query.operation_kinds import (OPERATION_KINDS, content_breakdown, counter_change, fingerprint,
                                            format_totals, overview, publication_rhythm, publication_years, summary)
from research_query.operations import OPERATIONS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical, validate
import web.research as web_research
from test_research_channels import channel_query
from test_research_operation_kinds import Repository, assert_gateway_statement, request
from test_research_query_contract import query
from test_research_query_service import NOW

CONTEXT = AccessContext('synthetic', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
VIDEO = {'video.fingerprint.v1': {'period': '28d'}, 'video.publication_rhythm.v1': {'period': 'max'},
         'video.publication_years.v1': {}, 'video.content_breakdown.v1': {'period': '1y'}}
CHANNEL = {'channel.counter_change.v1': {}, 'channel.format_totals.v1': {}}


def plan(name, parameters, q=None):
    q = q or (channel_query() if name.startswith('channel.') else query())
    return aggregate.validate_operation(request(name, q, parameters), CONTEXT)


def iso(moment):
    return moment.isoformat().replace('+00:00', 'Z')


PERIOD = dict(key='28d', label='28D', kind='rolling', days=28, start_at=iso(NOW - timedelta(days=28)), end_at=iso(NOW))
POPULATION = dict(admitted=10, matched=6, publication_unknown_excluded=1)


class RegistryAndPolicyTests(unittest.TestCase):
    def test_kinds_are_registered_with_todays_gates(self):
        for name in VIDEO:
            self.assertEqual(OPERATION_KINDS[name].definition.policy, 'descriptive_analytics')
            self.assertIsNone(OPERATION_KINDS[name].block)
        for name in CHANNEL:
            self.assertEqual(OPERATION_KINDS[name].definition.policy, 'channel_header')
        self.assertEqual(web_research.POLICY_FLAGS['channel_header'], ())
        with patch.dict(os.environ, {}, clear=True):
            allowed = web_research.allowed_operations()
        self.assertTrue(set(CHANNEL) <= allowed)
        self.assertFalse(set(VIDEO) & allowed)
        with patch.dict(os.environ, {'CHANNEL_BASELINE_ANALYTICS_ENABLED': '1'}, clear=True):
            self.assertTrue(set(VIDEO) <= web_research.allowed_operations())


class ParameterTests(unittest.TestCase):
    def test_accepted_parameters(self):
        for period in overview.PERIODS:
            plan('video.fingerprint.v1', {'period': period, 'population': {'kind': 'query'}})
            plan('video.content_breakdown.v1', {'period': period})
            plan('video.publication_rhythm.v1', {'period': period})
        for year in (1, 2026, 9999):
            plan('video.publication_years.v1', {'year': year})

    def test_refused_parameters(self):
        bad = [('video.fingerprint.v1', {}), ('video.fingerprint.v1', {'period': '2w'}), ('video.fingerprint.v1', {'period': 7}),
               ('video.fingerprint.v1', {'period': 'max', 'extra': 1}),
               ('video.fingerprint.v1', {'period': 'max', 'population': {'kind': 'frozen', 'ref': 'x'}}),
               ('video.publication_rhythm.v1', {'period': 'max', 'time_zone': 'UTC'}),
               ('video.publication_years.v1', {'year': 0}), ('video.publication_years.v1', {'year': True}),
               ('video.publication_years.v1', {'period': 'max'}), ('video.content_breakdown.v1', {'period': None}),
               ('channel.counter_change.v1', {'window': '7d'}), ('channel.format_totals.v1', {'x': 1})]
        for name, parameters in bad:
            with self.subTest(name=name, parameters=parameters), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                plan(name, parameters)

    def test_sources_and_scopes(self):
        for name in VIDEO:
            with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                plan(name, VIDEO[name], channel_query())
        for name in CHANNEL:
            with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                plan(name, {}, query())
            folder = {**channel_query(), 'scope': {'kind': 'folder', 'id': '11111111-1111-4111-8111-111111111111'}}
            with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                plan(name, {}, folder)

    def test_frozen_queries_are_refused_by_operations(self):
        frozen = {**query(), 'consistency': 'frozen'}
        for name, parameters in VIDEO.items():
            with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                compile_operation(plan(name, parameters, frozen))


class SqlTests(unittest.TestCase):
    def test_every_kind_compiles_a_gateway_statement_with_parameters_only(self):
        for name, parameters in {**VIDEO, **CHANNEL}.items():
            with self.subTest(operation=name):
                sql, params = compile_operation(plan(name, parameters))
                assert_gateway_statement(self, sql)
                for key in re.findall(r'%\((\w+)\)s', sql):
                    self.assertIn(key, params)

    def test_overview_results_take_their_lifetime_from_data_clocks(self):
        for name, parameters in VIDEO.items():
            sql, _ = compile_operation(plan(name, parameters))
            self.assertIn('least(coalesce(coverage.data_valid_before,coverage.valid_before),', sql)
            self.assertNotIn("interval '120 seconds' AS valid_before", sql)
        sql, _ = compile_operation(plan('video.summary.v1', {}))
        self.assertNotIn('data_valid_before,coverage.valid_before', sql)

    def test_local_time_uses_the_query_viewer_zone(self):
        zoned = {**query(), 'time': {'mode': 'current', 'timezone': 'Asia/Kathmandu'}}
        for name in ('video.publication_rhythm.v1', 'video.publication_years.v1'):
            _, params = compile_operation(plan(name, VIDEO[name], zoned))
            self.assertEqual(params['time_zone'], 'Asia/Kathmandu')
        _, params = compile_operation(plan('video.publication_years.v1', {}))
        self.assertEqual((params['time_zone'], params['year']), ('UTC', None))

    def test_local_publication_times_follow_the_canonical_calendar_rule(self):
        # Video.publication_years.v1 reads the registry's own video.published_date /
        # video.published_year columns; video.publication_calendar.v1 the same rule (one helper).
        from db.reads.research_sql import local_publication
        from research_query.operation_kinds import calendar
        zoned = {**query(), 'time': {'mode': 'current', 'timezone': 'Asia/Kathmandu'}}
        years = plan('video.publication_years.v1', {}, zoned)
        self.assertTrue({'video.published_date', 'video.published_year'} <= set(years.query.request.select))
        sql, params = compile_operation(years)
        assert_gateway_statement(self, sql)
        self.assertEqual((params['query_time_zone'], params['time_zone']), ('Asia/Kathmandu', 'Asia/Kathmandu'))
        local, instant, in_range = local_publication('published_at', '%(query_time_zone)s::text')
        self.assertIn(f'CASE WHEN metadata_fresh AND {instant} AND {in_range} THEN extract(year FROM {local}) END AS published_year', sql)
        self.assertIn('SELECT published_date AS date,published_year::integer AS year FROM population WHERE published_year IS NOT NULL', sql)
        self.assertNotIn('AS local_time', sql)
        # The calendar block: time.timezone when the block names no zone; a saved study's own zone still counts.
        for parameters, zone in (({'year': 2026}, 'Asia/Kathmandu'), ({'year': 2026, 'time_zone': 'Europe/Kyiv'}, 'Europe/Kyiv'),
                                 ({'year': 2026, 'time_zone': 'Asia/Kathmandu'}, 'Asia/Kathmandu')):
            with self.subTest(parameters=parameters):
                sql, params = compile_operation(plan('video.publication_calendar.v1', parameters, zoned))
                assert_gateway_statement(self, sql)
                self.assertEqual((params['time_zone'], params['year']), (zone, 2026))
                local, instant, in_range = local_publication('published_at', '%(time_zone)s::text')
                self.assertIn(f'SELECT {local} AS local_time FROM population WHERE published_at IS NOT NULL AND {instant} AND {in_range}', sql)
        _, params = compile_operation(plan('video.publication_calendar.v1', {'year': 2026}))
        self.assertEqual(params['time_zone'], 'UTC')
        for bad in ({}, {'time_zone': 'UTC'}, {'year': 2026, 'time_zone': 'Mars/Olympus'}, {'year': 2026, 'time_zone': 3},
                    {'year': 2026, 'time_zone': 'UTC', 'extra': 1}, {'year': 0}):
            with self.subTest(bad=bad), self.assertRaises(QueryRejected):
                plan('video.publication_calendar.v1', bad)
        self.assertEqual(calendar.DEFINITION.id, 'video.publication_calendar.v1')

    def test_period_parameters_are_exact_hours(self):
        expected = {'7d': 168, '28d': 672, '3m': 2160, '1y': 8760, 'max': None}
        for period, hours in expected.items():
            _, params = compile_operation(plan('video.fingerprint.v1', {'period': period}))
            self.assertEqual(params['period_hours'], hours)

    def test_one_stream_completion_rule(self):
        statement, _ = compile_query(validate(canonical(query(video_types=['stream'])), CONTEXT))
        rule = stream_completion_sql('p.stream_actual_end_at', 'p.checked_at', 'p.metadata_checked_at', 'a.evaluated_at',
                                     'a.evaluated_at', '%(window)s::interval')
        self.assertIn(rule, statement)
        sql, _ = compile_operation(plan('channel.format_totals.v1', {}))
        self.assertIn(stream_end_order_sql('v.stream_actual_end_at', 'v.checked_at', 'v.metadata_checked_at', 'a.evaluated_at'), sql)

    def test_stream_age_reference_is_the_actual_end(self):
        q = query(video_types=['stream'], select=['video.id', 'observation.views_at_age'])
        statement, _ = compile_query(validate(canonical(q), CONTEXT))
        self.assertIn("s.recorded_at<=(" + AGE_FROM + ")+make_interval(secs=>%(observation_age)s)", statement)
        self.assertIn("'from_at'," + AGE_FROM, statement)


def metric(known, missing, excluded=0, views=False, buckets=None):
    value = dict(known_count=known, missing_count=missing, excluded_count=excluded, definition_epoch=None,
                 state='known' if known else 'no_known_values', total='10.125', mean='1.005', median='2', q1=None, q3='3.5',
                 min='0', max='9007199254740993', buckets=buckets if buckets is not None else [{'key': 'a', 'label': 'A', 'count': known}])
    if views:
        value['unknown_definition_epoch_count'] = excluded
    return value


class CheckTests(unittest.TestCase):
    def fingerprint_result(self):
        return dict(period=dict(PERIOD), population=dict(POPULATION, scanned=14, stale_count=2, stale_counter_count=1,
                                                         stale_metadata_count=1, rejected={'source_unavailable': 3}),
                    uploads=dict(known_count=6, unknown_count=0),
                    metrics={m: metric(4, 1, 1, views=True) if m == 'views' else metric(5, 1) for m in summary.METRICS})

    def test_fingerprint_shape_rounding_and_counts(self):
        result = fingerprint.check(self.fingerprint_result())
        self.assertEqual(result['metrics']['views']['total'], '10.12')
        self.assertEqual(result['metrics']['views']['mean'], '1')
        self.assertEqual(result['population']['rejected']['source_unavailable'], 3)
        self.assertEqual(set(result['population']['rejected']), set(fingerprint.REJECTIONS))
        # The reasons are the admission stage names, which own their conditions.
        from db.reads.research_sql import REJECTION_REASONS
        self.assertEqual(fingerprint.REJECTIONS, REJECTION_REASONS)
        self.assertEqual(result['period']['start_at'], PERIOD['start_at'])
        for change in (lambda r: r['metrics']['likes'].update(missing_count=2),
                       lambda r: r['uploads'].update(known_count=5),
                       lambda r: r['metrics']['likes'].update(buckets=[]),
                       lambda r: r['population'].update(matched=11),
                       lambda r: r['population']['rejected'].update(bogus=1),
                       lambda r: r['period'].update(label='7D'),
                       lambda r: r['metrics']['likes'].update(unknown_definition_epoch_count=0)):
            broken = self.fingerprint_result()
            change(broken)
            with self.assertRaises((ValueError, TypeError, KeyError)):
                fingerprint.check(broken)

    def test_rhythm_densifies_and_finds_the_first_peak(self):
        cells = [dict(weekday=1, hour=9, count=2), dict(weekday=3, hour=9, count=1), dict(weekday=7, hour=23, count=3)]
        result = publication_rhythm.check(dict(period=dict(PERIOD), population=dict(POPULATION), time_zone='Europe/Kyiv',
            known_count=6, first_published_at='2026-10-01T10:00:00+00:00', last_published_at='2026-10-20T10:00:00+00:00',
            cells=cells, hours=[dict(hour=9, count=3), dict(hour=23, count=3)],
            weekdays=[dict(weekday=1, count=2), dict(weekday=3, count=1), dict(weekday=7, count=3)]))
        self.assertEqual(len(result['hours']), 24)
        self.assertEqual(result['peak_hour'], {'hour': 9, 'count': 3})
        self.assertEqual(result['peak_weekday'], {'weekday': 7, 'count': 3})
        self.assertEqual(result['grid'][6], {'weekday': 7, 'hours': [0] * 23 + [3]})
        self.assertEqual(result['first_published_at'], '2026-10-01T10:00:00Z')
        empty = publication_rhythm.check(dict(period=dict(PERIOD), population=dict(POPULATION, matched=0), time_zone='UTC',
            known_count=0, first_published_at=None, last_published_at=None, cells=[], hours=[], weekdays=[]))
        self.assertIsNone(empty['peak_hour'])
        with self.assertRaises(ValueError):
            publication_rhythm.check(dict(period=dict(PERIOD), population=dict(POPULATION), time_zone='UTC', known_count=1,
                first_published_at=None, last_published_at=None, cells=cells[:1], hours=[dict(hour=9, count=2)],
                weekdays=[dict(weekday=1, count=2)]))

    def test_years_offer_the_default_year_and_one_year_of_days(self):
        good = dict(time_zone='UTC', population=5, known_count=4, default_year=2026, year=2025, available_years=[2026, 2025, 2019],
                    days=[dict(date='2025-01-02', count=1), dict(date='2025-12-31', count=2)])
        self.assertIs(publication_years.check(deepcopy(good))['year'], 2025)
        for change in (dict(available_years=[2025, 2026]), dict(available_years=[2025]),
                       dict(days=[dict(date='2024-01-02', count=1)]), dict(days=[dict(date='2025-01-02', count=0)]),
                       dict(known_count=6), dict(days=[dict(date='2025-01-02', count=5)])):
            with self.assertRaises(ValueError):
                publication_years.check({**deepcopy(good), **change})

    def breakdown(self):
        groups = {name: {'items': [], 'group_count': 0} for name in content_breakdown.SECTIONS}
        groups['official_categories'] = {'items': [{'key': '22', 'count': 3}, {'key': '10', 'count': 1}], 'group_count': 2}
        groups['youtube_topics'] = {'items': [{'key': 'https://en.wikipedia.org/wiki/Caf%C3%A9_culture', 'count': 1}], 'group_count': 1}
        groups['catalogue_age'] = {'items': [{'key': '5y_plus', 'count': 6}], 'group_count': 1}
        known = dict(official_categories=4, youtube_topics=2, audio_languages=0, metadata_languages=0, captions=0, catalogue_age=6)
        return dict(period=dict(PERIOD), population=dict(POPULATION), sample_count=6, known=known, topics_empty=1, groups=groups)

    def test_breakdown_labels_shares_and_topic_denominator(self):
        result = content_breakdown.check(self.breakdown())
        categories, topics = result['official_categories'], result['youtube_topics']
        self.assertEqual([(g['label'], g['share_percent']) for g in categories['groups']], [('Category 22', '75'), ('Category 10', '25')])
        self.assertEqual((categories['missing_count'], categories['omitted_group_count']), (2, 0))
        # One of the two rows with collected topics, not one of all six rows.
        self.assertEqual((topics['groups'][0]['label'], topics['groups'][0]['share_percent']), ('Café culture', '50'))
        self.assertEqual((topics['assigned_count'], topics['empty_count'], topics['share_basis']), (1, 1, 'collected_rows'))
        self.assertEqual(result['catalogue_age']['groups'][0]['label'], '5+ years')
        # The browser chooses the age wording by this marker.
        self.assertEqual((result['catalogue_age']['source'], result['catalogue_age']['kind']),
                         ('leti_cook.video_age_from_published_at', 'single_label'))
        for change in (lambda r: r['known'].update(official_categories=7), lambda r: r.update(topics_empty=3),
                       lambda r: r['groups']['catalogue_age']['items'].append({'key': 'ancient', 'count': 1}),
                       lambda r: r['groups']['captions'].update(group_count=1)):
            broken = self.breakdown()
            change(broken)
            with self.assertRaises((ValueError, KeyError)):
                content_breakdown.check(broken)

    def test_share_rounds_using_binary_floating_point(self):
        self.assertEqual(overview.share(2, 3), '66.67')
        self.assertEqual(overview.share(1, 4000), '0.03')  # Exact decimal half-even would give 0.02.
        self.assertEqual(overview.share(4, 4), '100')
        with self.assertRaises(ValueError):
            overview.share(1, 0)

    def test_category_label_expiry_bounds_the_result(self):
        from hot_storage import register_read_deadline
        label_expiry = NOW + timedelta(seconds=30)

        def label(category_id, language_code='en'):
            register_read_deadline(label_expiry)
            return 'Music'
        data = self.breakdown()
        with patch('youtube_categories.youtube_video_category_name', label):
            output = QueryService(Repository(data, valid_before=NOW + timedelta(hours=5)), lambda: CONTEXT,
                                  clock=lambda: NOW).execute_operation(request('video.content_breakdown.v1', query(), {'period': '1y'}))
        self.assertEqual(output['context']['valid_before'], iso(label_expiry))
        self.assertEqual(output['result']['official_categories']['label_expires_at'], iso(label_expiry))
        self.assertEqual(output['result']['official_categories']['groups'][0]['label'], 'Music')


class CounterChangeTests(unittest.TestCase):
    def point(self, hours_ago, value):
        return dict(recorded_at=iso(NOW - timedelta(hours=hours_ago)), value=value, expires_at=iso(NOW + timedelta(hours=696 - hours_ago)))

    def test_full_window_needs_the_start_within_the_tolerance(self):
        end = self.point(1, '110')
        full = counter_change._metric(168, end, self.point(169, '100'), None, iso(NOW))
        self.assertEqual((full['status'], full['coverage'], full['actual_interval_seconds'], full['percent_change']),
                         ('ready', 'full', 168 * 3600, '10'))
        partial = counter_change._metric(168, end, None, self.point(122, '100'), iso(NOW))
        self.assertEqual((partial['status'], partial['coverage'], partial['actual_interval_seconds']), ('partial', 'partial', 121 * 3600))
        zero = counter_change._metric(168, end, self.point(170, '0'), None, iso(NOW))
        self.assertEqual((zero['status'], zero['absolute_change'], zero['percent_change']), ('zero_baseline', '110', None))
        stale = counter_change._metric(168, self.point(25, '110'), None, None, iso(NOW))
        self.assertEqual(stale['status'], 'stale_current')
        hidden = counter_change._metric(168, self.point(1, None), None, None, iso(NOW))
        self.assertEqual((hidden['status'], hidden['reason_code']), ('not_reported', 'counter_not_reported'))
        self.assertEqual(counter_change._metric(168, end, None, None, iso(NOW))['status'], 'not_enough_history')
        down = counter_change._metric(672, self.point(1, '90'), self.point(690, '100'), None, iso(NOW))
        self.assertEqual((down['absolute_change'], down['percent_change']), ('-10', '-10'))
        with self.assertRaises(ValueError):  # A full start can never be later than the boundary.
            counter_change._metric(168, end, self.point(100, '100'), None, iso(NOW))

    def test_lifetime_is_the_earliest_snapshot_shown(self):
        result = counter_change.check(dict(evaluated_at=iso(NOW), latest=dict(recorded_at=iso(NOW - timedelta(hours=1)), subscribers='5',
            views='110', expires_at=iso(NOW + timedelta(hours=695))), windows={p: {m: {'boundary': None, 'earliest': None}
            for m in counter_change.METRICS} for p, _ in counter_change.WINDOWS}))
        # The end point shown turns stale_current 24 hours after it was recorded.
        self.assertEqual(counter_change.lifetime(result), NOW + timedelta(hours=23, microseconds=1))
        result['windows']['28d']['metrics']['views']['expires_at'] = iso(NOW + timedelta(hours=3))
        self.assertEqual(counter_change.lifetime(result), NOW + timedelta(hours=3))
        self.assertIsNone(result['definition_epoch'])


class FormatTotalsTests(unittest.TestCase):
    def counts(self, **classes):
        values = dict.fromkeys(format_totals.CLASSES, 0)
        values.update(classes)
        return dict(scanned=sum(values.values()), canonical=sum(values.values()), classes=values,
                    views=str(10 * values['known']) if values['known'] else None, expires_at=iso(NOW + timedelta(hours=9)))

    def state(self, status='completed', current=True):
        return dict(inventory_status=status, current=current, expires_at=iso(NOW + timedelta(hours=8)))

    def test_old_completeness_rule(self):
        ready = format_totals._format(self.counts(known=3, unavailable_excluded=1), self.state())
        self.assertEqual((ready['status'], ready['views'], ready['missing_view_count']), ('ready', '30', 0))
        stale = format_totals._format(self.counts(known=3, stale_counter=1), self.state())
        self.assertEqual((stale['status'], stale['views'], stale['missing_view_count']), ('stale', None, 1))
        incomplete = format_totals._format(self.counts(known=3, stream_completion_withheld=1), self.state())
        self.assertEqual(incomplete['status'], 'incomplete')
        listed = self.counts(known=3)
        listed['canonical'] = 4
        missing = format_totals._format(listed, self.state())
        self.assertEqual((missing['status'], missing['reason_code']), ('incomplete', 'inventory_sources_incomplete'))
        empty = format_totals._format(self.counts(), self.state())
        self.assertEqual((empty['status'], empty['views']), ('ready', '0'))

    def test_typed_list_gates(self):
        counts = self.counts(known=1)
        self.assertEqual(format_totals._format(counts, self.state('playlist_not_found'))['status'], 'absent')
        self.assertEqual(format_totals._format(counts, self.state('running'))['status'], 'incomplete')
        self.assertEqual(format_totals._format(counts, self.state(None))['status'], 'not_collected')
        self.assertEqual(format_totals._format(counts, self.state(current=False))['status'], 'stale')
        self.assertEqual(format_totals._format(counts, self.state(None))['status'], 'not_collected')


if __name__ == '__main__':
    unittest.main()
