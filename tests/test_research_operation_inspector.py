"""Inspector values, endpoints, exclusions and response contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from db.reads.research_aggregate import compile_operation
from research_query import aggregate
from db.reads.research_operations import launch_comparison as launch_comparison_sql, lifetime_baseline as lifetime_baseline_sql
from db.reads.research_operations import observed_history as observed_history_sql, summary as summary_sql
from research_query.operation_kinds import (OPERATION_KINDS, inspection, launch_comparison, lifetime_baseline,
                                            observed_history, static_details, summary)
from research_query.operation_kinds.kind import Binding
from research_query.operations import OPERATIONS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical
from test_research_operation_kinds import Repository, assert_gateway_statement
from test_research_query_service import NOW
import web.research as web_research

INSPECTOR = ('video.static_details.v1', 'video.observed_history.v1', 'video.launch_comparison.v1', 'video.lifetime_baseline.v1')
CONTEXT = AccessContext('synthetic', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
CHANNEL = 'UC' + 'a' * 22
VIDEO = 'a0000000001'


def query(kind='long', **changes):
    return {'query_version': 1, 'source': {'id': 'public.videos.current.v1'}, 'scope': {'kind': 'channel', 'id': CHANNEL},
            'video_types': [kind], 'select': ['video.id'], 'order_by': [{'field': 'video.id', 'direction': 'asc'}], **changes}


def request(operation, parameters=None, **changes):
    parameters = {'video_id': VIDEO, 'population': {'kind': 'query'}} if parameters is None else parameters
    return canonical(dict(version=1, operation=operation, query=query(**changes), parameters=parameters))


def run(result, operation, parameters=None):
    return QueryService(Repository(result), lambda: CONTEXT, clock=lambda: NOW).execute_operation(request(operation, parameters))


def instant(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


class ParameterTests(unittest.TestCase):
    def test_every_inspector_operation_takes_a_video_and_a_population_reference(self):
        frozen = {'kind': 'frozen', 'frozen_ref': 'wRv-ANLX0cro_1'}
        for operation in INSPECTOR:
            with self.subTest(operation=operation):
                for population in ({'kind': 'query'}, frozen):
                    aggregate.validate_operation(request(operation, {'video_id': VIDEO, 'population': population}), CONTEXT)
                for bad in ({}, {'video_id': VIDEO}, {'population': {'kind': 'query'}},
                            {'video_id': 'short', 'population': {'kind': 'query'}},
                            {'video_id': VIDEO, 'population': {'kind': 'query', 'token': 'x'}},
                            {'video_id': VIDEO, 'population': {'kind': 'frozen'}},
                            {'video_id': VIDEO, 'population': {'kind': 'frozen', 'frozen_ref': 'a b'}},
                            {'video_id': VIDEO, 'population': {'kind': 'other'}},
                            {'video_id': VIDEO, 'population': 'query'},
                            {'video_id': VIDEO, 'population': {'kind': 'query'}, 'extra': 1},
                            {'video_id': 1, 'population': {'kind': 'query'}}):
                    with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                        aggregate.validate_operation(request(operation, bad), CONTEXT)
                channel_source = canonical(dict(version=1, operation=operation, parameters={'video_id': VIDEO, 'population': {'kind': 'query'}},
                    query={'query_version': 1, 'source': {'id': 'public.channels.current.v1'}, 'scope': {'kind': 'channel', 'id': CHANNEL},
                           'select': ['channel.id'], 'order_by': [{'field': 'channel.id', 'direction': 'asc'}]}))
                with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                    aggregate.validate_operation(channel_source, CONTEXT)

    def test_static_details_reads_the_description_only_on_request(self):
        base = {'video_id': VIDEO, 'population': {'kind': 'query'}}
        plain = aggregate.validate_operation(request('video.static_details.v1', base), CONTEXT)
        described = aggregate.validate_operation(request('video.static_details.v1', {**base, 'description': True}), CONTEXT)
        self.assertNotIn('video.description', plain.query.referenced_fields)
        self.assertIn('video.description', described.query.referenced_fields)
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            aggregate.validate_operation(request('video.static_details.v1', {**base, 'description': 'yes'}), CONTEXT)
        with self.assertRaisesRegex(QueryRejected, 'field_disabled'):
            aggregate.validate_operation(request('video.static_details.v1', {**base, 'description': True}),
                                         replace(CONTEXT, disabled_type_fields=frozenset({('long', 'video.description')})))
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            aggregate.validate_operation(request('video.observed_history.v1', {**base, 'description': True}), CONTEXT)


class SqlTests(unittest.TestCase):
    def test_statements_pass_the_gateway_with_parameters_only_for_every_type(self):
        for operation in INSPECTOR:
            for kind in ('long', 'short', 'stream'):
                with self.subTest(operation=operation, kind=kind):
                    plan = aggregate.validate_operation(request(operation, kind=kind), CONTEXT)
                    sql, params = compile_operation(plan)
                    assert_gateway_statement(self, sql)
                    self.assertEqual([name for name in re.findall(r'%\((\w+)\)s', sql) if name not in params], [])
                    self.assertEqual((params['inspect_video_id'], params['inspect_as_of'], params['inspect_frozen']), (VIDEO, None, None))
                    # One inspection clock and one target CTE: the place a frozen reference plugs in.
                    self.assertEqual(sql.count('inspector_args AS MATERIALIZED'), 1)
                    self.assertNotIn('as_of-make_interval', sql)

    def test_summary_statement_is_unchanged_by_the_shared_statistics(self):
        # sha256 of video.summary.v1's SQL with explicit definition evidence (one value without a recorded definition is left
        # out and counted).
        self.assertEqual(hashlib.sha256(summary_sql.SQL.encode()).hexdigest(),
                         '4a587ca037f8ae4ef1f27feacdc9d3c895032074a2619c11ae7361708ed4f385')
        self.assertIn(inspection.WINDOW_HOURS, (672,))
        baseline = lifetime_baseline_sql.SQL
        for part in ('view_definitions AS MATERIALIZED', 'view_definition AS (', 'summaries AS ('):
            self.assertEqual(baseline.count(part), 1)
            self.assertEqual(summary_sql.SQL.count(part), 1)

    def test_launch_reads_one_timestamp_per_target(self):
        sql = launch_comparison_sql.SQL
        self.assertIn('ORDER BY s.recorded_at DESC LIMIT 1) pick', sql)
        self.assertIn('s.recorded_at=pick.recorded_at', sql)
        self.assertNotIn('20000', sql)
        self.assertIn('LIMIT 501', observed_history_sql.SQL)


class PolicyTests(unittest.TestCase):
    def allowed(self, **flags):
        names = ('CHANNEL_BASELINE_ANALYTICS_ENABLED', 'CHANNEL_DERIVED_INSIGHTS_ENABLED', 'CHANNEL_LAUNCH_ANALYTICS_ENABLED')
        with patch.dict(os.environ, {name: '1' if flags.get(name) else '0' for name in names}):
            return web_research.allowed_operations() & set(INSPECTOR)

    def test_inspector_permission_gates(self):
        self.assertEqual(self.allowed(), set())
        self.assertEqual(self.allowed(CHANNEL_BASELINE_ANALYTICS_ENABLED=1), {'video.static_details.v1', 'video.lifetime_baseline.v1'})
        self.assertEqual(self.allowed(CHANNEL_DERIVED_INSIGHTS_ENABLED=1), {'video.observed_history.v1'})
        self.assertEqual(self.allowed(CHANNEL_BASELINE_ANALYTICS_ENABLED=1, CHANNEL_DERIVED_INSIGHTS_ENABLED=1),
                         {'video.static_details.v1', 'video.lifetime_baseline.v1', 'video.observed_history.v1'})
        for partial in ({'CHANNEL_LAUNCH_ANALYTICS_ENABLED': 1}, {'CHANNEL_LAUNCH_ANALYTICS_ENABLED': 1, 'CHANNEL_BASELINE_ANALYTICS_ENABLED': 1},
                        {'CHANNEL_LAUNCH_ANALYTICS_ENABLED': 1, 'CHANNEL_DERIVED_INSIGHTS_ENABLED': 1}):
            self.assertNotIn('video.launch_comparison.v1', self.allowed(**partial))
        self.assertIn('video.launch_comparison.v1', self.allowed(CHANNEL_LAUNCH_ANALYTICS_ENABLED=1, CHANNEL_BASELINE_ANALYTICS_ENABLED=1,
                                                                 CHANNEL_DERIVED_INSIGHTS_ENABLED=1))
        self.assertEqual(web_research.POLICY_FLAGS['launch_analytics'], ('CHANNEL_LAUNCH_ANALYTICS_ENABLED',
                         'CHANNEL_BASELINE_ANALYTICS_ENABLED', 'CHANNEL_DERIVED_INSIGHTS_ENABLED'))
        with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
            aggregate.validate_operation(request('video.launch_comparison.v1'),
                                         replace(CONTEXT, allowed_operations=frozenset(OPERATIONS) - {'video.launch_comparison.v1'}))


def static_row(**changes):
    row = dict(video_id=VIDEO, identity_clock=instant(1), identity_expiry=(NOW + timedelta(hours=600)).isoformat(),
               checked_at=instant(2), metadata_checked_at=instant(3), title='Straße \U0001F600', title_state='known',
               thumbnail_url='https://img.youtube.com/vi/a0000000001/mqdefault.jpg', thumbnail_url_state='known',
               duration_seconds='93784', duration_seconds_state='known', views='3', views_state='known',
               likes='1', likes_state='known', comments='0', comments_state='known', published_at=instant(30),
               published_at_state='known', category_id='22', category_id_state='known', tags=['a', 'b'], tags_state='known',
               # The registry's calculated fields, as research_sql returns them for this row (the field registry defines the rules).
               title_length='8', title_length_state='known', tag_count='2', tag_count_state='known',
               like_rate='33.333333', like_rate_state='known', comment_rate='0', comment_rate_state='known')
    row.update(changes)
    return row


class StaticDetailsCheckTests(unittest.TestCase):
    def result(self, row=None, member=True, frozen=None):
        return {'video_id': VIDEO, 'video_type': 'long', 'member': member, 'frozen': frozen, 'description': False,
                'row': static_row() if row is None and member else row}

    def test_static_fields_and_rates(self):
        out = static_details.check(self.result())
        legacy = {'metrics': {'like_rate_percent': 33.333333, 'comment_rate_percent': 0.0, 'title_length_characters': 8, 'tag_count': 2}}
        self.assertEqual(out['derived']['video.like_rate']['value'], '33.333333')
        self.assertEqual(float(out['derived']['video.like_rate']['value']), legacy['metrics']['like_rate_percent'])
        self.assertEqual(out['derived']['video.comment_rate']['value'], '0')
        self.assertEqual(out['derived']['video.title_length']['value'], str(legacy['metrics']['title_length_characters']))
        self.assertEqual(out['derived']['video.tag_count']['value'], '2')
        self.assertEqual(out['cells']['video.duration_seconds']['value'], '93784')  # P1DT2H3M4S
        self.assertEqual(out['cells']['video.views']['observed_at'], (NOW - timedelta(hours=2)).isoformat().replace('+00:00', 'Z'))
        self.assertEqual(out['cells']['video.views']['expires_at'], (NOW + timedelta(hours=694)).isoformat().replace('+00:00', 'Z'))
        self.assertEqual(out['clocks']['metadata_checked_at'], (NOW - timedelta(hours=3)).isoformat().replace('+00:00', 'Z'))
        self.assertEqual((out['membership'], out['inspection']), ('member', {'mode': 'query'}))
        # Values and clocks come from the registry fields of the same row; rounding and states
        # are research_sql's (proven against catalogue_static in research_aggregate_pg test_facet_04).
        self.assertEqual(out['derived']['video.like_rate']['observed_at'], out['cells']['video.views']['observed_at'])
        self.assertEqual(out['derived']['video.title_length']['expires_at'], out['cells']['video.title']['expires_at'])
        self.assertTrue(set(static_details.DERIVED) <= set(static_details.dependencies({'video_id': VIDEO, 'population': {'kind': 'query'}}, 'public.videos.current.v1', 'channel')))

    def test_unknown_inputs_and_zero_views_are_never_zero(self):
        zero = static_details.check(self.result(static_row(views='0', like_rate=None, like_rate_state='unavailable')))
        self.assertEqual((zero['derived']['video.like_rate']['value'], zero['derived']['video.like_rate']['state']), (None, 'unavailable'))
        stale = static_details.check(self.result(static_row(views=None, views_state='expired', like_rate=None, like_rate_state='expired')))
        self.assertEqual(stale['derived']['video.like_rate']['state'], 'expired')
        untitled = static_details.check(self.result(static_row(title=None, title_state='expired', title_length=None,
                                                                title_length_state='expired', tags=None, tags_state='unknown',
                                                                tag_count=None, tag_count_state='unknown')))
        self.assertEqual((untitled['derived']['video.title_length']['state'], untitled['derived']['video.tag_count']['state']),
                         ('expired', 'unknown'))
        self.assertIsNone(untitled['derived']['video.title_length']['value'])
        empty = static_details.check(self.result(static_row(category_id='', tags=[], tag_count='0')))
        self.assertEqual((empty['category']['label'], empty['derived']['video.tag_count']['value']), (None, '0'))
        with self.assertRaises(ValueError):  # A known calculation without its value is refused.
            static_details.check(self.result(static_row(like_rate=None)))

    def test_outside_the_population_and_frozen_values(self):
        out = static_details.check(self.result(member=False, row=None))
        self.assertEqual((out['membership'], out['cells'], out['derived']), ('not_in_population', None, None))
        cell = lambda value, hours=5: {'state': 'known', 'value': value, 'provenance_ref': 'x', 'observed_at': instant(hours),
                                       'expires_at': (NOW + timedelta(hours=691)).isoformat(), 'observation': None}
        frozen = {'evaluated_at': NOW.isoformat(), 'valid_before': (NOW + timedelta(seconds=600)).isoformat(), 'position': 4,
                  'total_count': 9, 'cells': {'video.views': cell('10'), 'video.likes': cell('5'), 'video.comments': cell('1'),
                                              'video.title': cell('Frozen')}}
        bound = static_details.check(self.result(frozen={**frozen, 'cells': {**frozen['cells'], 'video.like_rate': cell('50')}}))
        self.assertEqual(bound['inspection']['mode'], 'frozen')
        self.assertEqual(bound['inspection']['frozen_fields'], ['video.comments', 'video.like_rate', 'video.likes', 'video.title', 'video.views'])
        self.assertEqual((bound['cells']['video.views']['value'], bound['cells']['video.title']['value']), ('10', 'Frozen'))
        self.assertEqual(bound['derived']['video.like_rate']['value'], '50')  # the frozen calculation
        self.assertEqual(bound['cells']['video.tags']['value'], ['a', 'b'])  # not frozen: current
        self.assertEqual(bound['derived']['video.tag_count']['value'], '2')  # no frozen input: current
        # Frozen inputs without the frozen calculation: never a current rate next to frozen counters.
        self.assertEqual(bound['derived']['video.comment_rate']['state'], 'unavailable')
        self.assertEqual(bound['derived']['video.title_length']['state'], 'unavailable')
        # The Library list freezes video.tag_count (its group counts) but not video.tags:
        # the shown count is the current length of the current tags, never the frozen count beside them.
        counted = static_details.check(self.result(row=static_row(tags=['a', 'b', 'c'], tag_count='3'),
                                                   frozen={**frozen, 'cells': {**frozen['cells'], 'video.tag_count': cell('7')}}))
        self.assertIn('video.tag_count', counted['inspection']['frozen_fields'])
        self.assertEqual(counted['cells']['video.tags']['value'], ['a', 'b', 'c'])
        tags = counted['cells']['video.tags']
        self.assertEqual(counted['derived']['video.tag_count'], {'value': str(len(tags['value'])), 'state': 'known',
                                                                 'observed_at': tags['observed_at'], 'expires_at': tags['expires_at']})
        # Both tags and their count frozen: the frozen count of the frozen tags.
        both = static_details.check(self.result(frozen={**frozen, 'cells': {**frozen['cells'], 'video.tags': cell(['x']),
                                                                            'video.tag_count': cell('1')}}))
        self.assertEqual((both['cells']['video.tags']['value'], both['derived']['video.tag_count']['value']), (['x'], '1'))
        # Mixed inputs (frozen views, current likes): no rate.
        mixed = static_details.check(self.result(frozen={**frozen, 'cells': {'video.views': cell('10'), 'video.like_rate': cell('50')}}))
        self.assertEqual(mixed['derived']['video.like_rate']['state'], 'unavailable')
        gone = static_details.check(self.result(member=False, row=None, frozen=frozen))
        self.assertEqual((gone['membership'], gone['cells']['video.views']['value'], gone['cells']['video.tags']['state']),
                         ('not_in_population', '10', 'unavailable'))

    def test_shapes_are_refused(self):
        for bad in ({}, {**self.result(), 'extra': 1}, {**self.result(), 'video_id': 'bad'}, {**self.result(), 'member': 'yes'},
                    self.result(static_row(views_state='odd')), self.result(static_row(video_id='b0000000001'))):
            with self.assertRaises((ValueError, TypeError, KeyError)):
                static_details.check(bad)


def point(hours, views, likes=None, comments=None, rows=1, epoch='e1', previous=None, conflict=()):
    at = NOW - timedelta(hours=hours)
    data = {'recorded_at': at.isoformat(), 'rows': rows, 'epoch': epoch, 'elapsed_us': None}
    for metric, value in (('views', views), ('likes', likes), ('comments', comments)):
        data[metric] = None if value is None else str(value)
        data[metric + '_values'] = 2 if metric in conflict else 0 if value is None else 1
        data[metric + '_missing'] = value is None
        data[metric + '_invalid'] = False
        data[metric + '_delta'] = None
    if previous is not None:
        data['elapsed_us'] = str(int((at - (NOW - timedelta(hours=previous['hours']))).total_seconds() * 1_000_000))
        for metric in ('views', 'likes', 'comments'):
            before = previous[metric]
            data[metric + '_delta'] = None if before is None or data[metric] is None else str(int(data[metric]) - before)
    return data


def series(*specs):
    points, previous = [], None
    for spec in specs:
        hours, views, likes, comments = spec[:4]
        extra = spec[4] if len(spec) > 4 else {}
        points.append(point(hours, views, likes, comments, previous=previous, **extra))
        previous = {'hours': hours, 'views': views, 'likes': likes, 'comments': comments}
    return points


class HistoryCheckTests(unittest.TestCase):
    def result(self, points, end_hours=1, expected=None, member=True, scanned=None, frozen=None):
        expected = expected or {'views': points[-1]['views'] if points else '1', 'views_state': 'known',
                                'likes': points[-1]['likes'] if points else None, 'likes_state': 'known' if points and points[-1]['likes'] else 'unknown',
                                'comments': points[-1]['comments'] if points else None,
                                'comments_state': 'known' if points and points[-1]['comments'] else 'unknown'}
        return {'video_id': VIDEO, 'video_type': 'long', 'member': member, 'frozen': frozen, 'as_of': NOW.isoformat(),
                'bounds': {'from_at': (NOW - timedelta(hours=672)).isoformat(), 'end_at': (NOW - timedelta(hours=end_hours)).isoformat()},
                'expected': expected, 'scanned': sum(p['rows'] for p in points) if scanned is None else scanned, 'points': points}

    def test_elapsed_intervals_and_signed_rates(self):
        out = observed_history.check(self.result(series((5, 100, 4, 1), (3, 101, None, 1), (1, 98, 6, 2))))
        views = out['series']['views']
        self.assertEqual((out['status'], out['reason'], views['coverage']['valid_count']), ('ready', None, 3))
        self.assertEqual([(i['delta'], i['per_day'], i['actual_interval_seconds'], i['definition']) for i in views['intervals']],
                         [('1', '12', '7200', 'known'), ('-3', '-36', '7200', 'known')])
        self.assertEqual(views['intervals'][0]['per_day'], '12')
        self.assertEqual(views['latest_interval']['delta'], '-3')
        likes = out['series']['likes']
        self.assertEqual(([i['delta'] for i in likes['intervals']], likes['latest_interval'], likes['coverage']['missing_count']),
                         ([None, None], None, 1))
        self.assertEqual([p['likes'] for p in out['points']], ['4', None, '6'])
        self.assertEqual(out['window']['rule'], 'evaluated_at_minus_28_days_through_counter_observation')

    def test_unavailable_observations_have_explicit_states(self):
        conflict = observed_history.check(self.result(series((3, 100, 1, 1), (1, 101, 1, 1, {'conflict': ('views',), 'rows': 2}))))
        self.assertEqual((conflict['status'], conflict['reason'], conflict['points'][0]['views']),
                         ('unavailable', 'conflicting_simultaneous_observations', None))
        self.assertEqual(conflict['series']['likes']['status'], 'ready')  # independent counters
        self.assertEqual(conflict['series']['views']['coverage']['duplicate_timestamp_count'], 1)
        missing = observed_history.check(self.result(series((3, 100, 1, 1)), end_hours=1))
        self.assertEqual(missing['reason'], 'endpoint_snapshot_missing')
        mismatch = observed_history.check(self.result(series((1, 100, 1, 1)), expected={'views': '105', 'views_state': 'known',
                                          'likes': '1', 'likes_state': 'known', 'comments': '1', 'comments_state': 'known'}))
        self.assertEqual(mismatch['reason'], 'endpoint_counter_mismatch')
        limit = observed_history.check(self.result([], scanned=501))
        self.assertEqual((limit['reason'], limit['points']), ('observation_limit_exceeded', []))
        outside = observed_history.check(self.result([], end_hours=700))
        self.assertEqual((outside['reason'], outside['window']['from_at']), ('catalogue_endpoint_outside_hot_window', None))
        absent = observed_history.check(self.result([], member=False))
        self.assertEqual(absent['reason'], 'video_not_in_population')
        invalid = observed_history.check(self.result(series((1, 100, 1, 1)), expected={'views': '100', 'views_state': 'known',
                                         'likes': None, 'likes_state': 'unavailable', 'comments': '1', 'comments_state': 'known'}))
        self.assertEqual((invalid['status'], invalid['series']['likes']['reason']), ('ready', 'invalid_catalogue_endpoint'))

    def test_one_definition_epoch_per_views_interval(self):
        out = observed_history.check(self.result(series((5, 100, 1, 1), (3, 110, 1, 1, {'epoch': 'e2'}), (1, 120, 1, 1, {'epoch': None}))))
        self.assertEqual([(i['definition'], i['delta']) for i in out['series']['views']['intervals']],
                         [('incompatible_definition', None), ('unknown_definition', None)])
        self.assertIsNone(out['series']['views']['latest_interval'])
        self.assertEqual([i['delta'] for i in out['series']['likes']['intervals']], ['0', '0'])

    def test_frozen_counters_are_the_endpoint(self):
        cell = lambda value: {'state': 'known', 'value': value, 'provenance_ref': 'x', 'observed_at': instant(3),
                              'expires_at': (NOW + timedelta(hours=600)).isoformat(), 'observation': None}
        frozen = {'evaluated_at': NOW.isoformat(), 'valid_before': (NOW + timedelta(seconds=600)).isoformat(), 'position': 0,
                  'total_count': 1, 'cells': {'video.views': cell('100'), 'video.likes': cell('1'), 'video.comments': cell('1')}}
        current = {'views': '999', 'views_state': 'known', 'likes': '9', 'likes_state': 'known', 'comments': '9', 'comments_state': 'known'}
        out = observed_history.check(self.result(series((5, 90, 1, 1), (3, 100, 1, 1)), end_hours=3, expected=current, frozen=frozen))
        self.assertEqual((out['status'], out['window']['endpoint'], out['inspection']['mode']), ('ready', 'frozen_counter_observation', 'frozen'))
        partial = {**frozen, 'cells': {'video.views': cell('100')}}
        out = observed_history.check(self.result(series((5, 90, 1, 1), (3, 100, 1, 1)), end_hours=3, expected=current, frozen=partial))
        self.assertEqual((out['reason'], out['window']['endpoint']), ('endpoint_counter_mismatch', 'counter_observation'))

    def test_inconsistent_database_results_are_refused(self):
        good = series((3, 100, 1, 1), (1, 101, 1, 1))
        for change in (lambda p: p[1].update(elapsed_us='1'), lambda p: p[1].update(views_delta='5'), lambda p: p.reverse()):
            points = [dict(p) for p in good]
            change(points)
            with self.assertRaises(ValueError):
                observed_history.check(self.result(points))
        with self.assertRaises(ValueError):
            observed_history.check(self.result(good, scanned=7))


def horizon(ordinal, target, *, ready=8, unknown=0, epochs=1, cohort=8, counts=(2, 4, 2)):
    key, seconds, tolerance = launch_comparison.HORIZONS[ordinal - 1]
    return {'ordinal': ordinal, 'key': key, 'seconds': seconds, 'tolerance': tolerance, 'ready_count': ready,
            'unknown_definition_count': unknown, 'definition_epochs': epochs, 'definition_epoch': 'e1' if epochs == 1 else None,
            'cohort_count': cohort, 'counts': dict(zip(launch_comparison.CLASSES, counts)), 'target': target}


def ready_target(views, median, p25, p75, classification, *, epoch='e1', in_cohort=True, age_hours=23):
    target_at = NOW - timedelta(hours=1)
    return {'status': 'ready', 'target_at': target_at.isoformat(), 'observed_at': (target_at - timedelta(hours=24 - age_hours)).isoformat(),
            'views': str(views), 'epoch': epoch, 'age_us': str(age_hours * 3600 * 1_000_000), 'in_cohort': in_cohort,
            'p25': p25, 'median': median, 'p75': p75, 'classification': classification}


class LaunchCheckTests(unittest.TestCase):
    def result(self, *horizons, member=True):
        empty = {'status': 'not_reached', 'target_at': NOW.isoformat(), 'observed_at': None, 'views': None, 'epoch': None,
                 'age_us': None, 'in_cohort': False, 'p25': None, 'median': None, 'p75': None, 'classification': None}
        filled = list(horizons) + [horizon(n, dict(empty), ready=0, epochs=0, cohort=0, counts=(0, 0, 0)) for n in range(len(horizons) + 1, 4)]
        return {'video_id': VIDEO, 'video_type': 'stream', 'member': member, 'frozen': None, 'as_of': NOW.isoformat(),
                'window_from': (NOW - timedelta(hours=672)).isoformat(), 'conditions_applied': False, 'population': 40,
                'candidates': 30, 'snapshot_rows': 30, 'horizons': filled}

    def test_quartiles_multiplier_and_quality(self):
        out = launch_comparison.check(self.result(horizon(1, ready_target(1, '8.0000', '2.25', '12.750', 'below_typical'))))
        comparison = out['horizons'][0]['target']['comparison']
        self.assertEqual(comparison, {'status': 'preliminary', 'reference_count': 7, 'median': '8', 'p25': '2.25', 'p75': '12.75',
                                      'classification': 'below_typical', 'multiplier': '0.12',
                                      'multiplier_reason': None})
        self.assertEqual(comparison['multiplier'], '0.12')  # 0.125 rounds half-even
        self.assertEqual(launch_comparison.quarters('7.5'), '7.5')
        self.assertEqual(out['horizons'][0]['target']['actual_age_seconds'], '82800')
        self.assertEqual((out['reference']['reference_point'], out['window']['hours']), ('actual_end_at', 672))
        zero = launch_comparison.check(self.result(horizon(1, ready_target(0, '0', '0', '0', 'typical'))))
        self.assertEqual((zero['horizons'][0]['target']['comparison']['multiplier'],
                          zero['horizons'][0]['target']['comparison']['multiplier_reason']), (None, 'zero_reference_median'))
        small = launch_comparison.check(self.result(horizon(1, ready_target(5, None, None, None, None), ready=3, cohort=3, counts=(0, 0, 0))))
        self.assertEqual((small['horizons'][0]['quality'], small['horizons'][0]['target']['comparison']['status'],
                          small['horizons'][0]['target']['comparison']['reference_count']), ('insufficient_data', 'insufficient_data', 2))
        for count, quality in ((3, 'insufficient_data'), (4, 'preliminary'), (11, 'preliminary'), (12, 'ready')):
            self.assertEqual(launch_comparison.quality(count), quality)

    def test_definition_epochs_are_never_mixed(self):
        mixed = launch_comparison.check(self.result(horizon(1, ready_target(3, None, None, None, None, in_cohort=False),
                                                            ready=9, epochs=2, cohort=0, counts=(0, 0, 0))))
        first = mixed['horizons'][0]
        self.assertEqual((first['definition']['state'], first['eligible_count'], first['target']['comparison']['status']),
                         ('mixed_definition_epochs', 0, 'mixed_definition_epochs'))
        unknown = launch_comparison.check(self.result(horizon(1, ready_target(3, None, None, None, None, epoch=None, in_cohort=False),
                                                              ready=9, unknown=1, cohort=8)))
        self.assertEqual((unknown['horizons'][0]['target']['comparison']['status'], unknown['horizons'][0]['definition']['unknown_definition_count']),
                         ('unknown_definition', 1))

    def test_inconsistent_results_are_refused(self):
        bad_cases = (horizon(1, ready_target(1, '8', '2.25', '12.75', 'typical')),                   # wrong classification
                     horizon(1, ready_target(1, '8.1', '2.25', '12.75', 'below_typical')),           # not a quarter
                     horizon(1, ready_target(1, '8', '2.25', '12.75', 'below_typical'), counts=(1, 1, 1)),
                     horizon(1, ready_target(1, '8', '2.25', '12.75', 'below_typical', age_hours=30)))
        for case in bad_cases:
            with self.assertRaises((ValueError, TypeError, KeyError)):
                launch_comparison.check(self.result(case))
        out = launch_comparison.check(self.result(member=False))
        self.assertEqual((out['membership'], out['horizons'][0]['target']), ('not_in_population', None))


class BaselineCheckTests(unittest.TestCase):
    def result(self, known, median='12.5', state='known', population=20, member=True):
        return {'video_id': VIDEO, 'video_type': 'short', 'member': member, 'frozen': None, 'conditions_applied': False,
                'population': population, 'definition': {'state': state, 'epoch': 'e1' if state == 'known' else None,
                                                          'unknown_epoch': 0},
                'views': {'known_count': known, 'median': median, 'q1': '3.250', 'q3': '100.75'} if known else None,
                'target': {'views': '7', 'views_state': 'known', 'checked_at': instant(1)} if member else None}

    def test_sample_status_and_rounding(self):
        for known, status in ((3, 'insufficient_data'), (4, 'preliminary'), (11, 'preliminary'), (12, 'ready')):
            out = lifetime_baseline.check(self.result(known))
            self.assertEqual((out['status'], out['sample_size'], out['missing_views']), (status, known, 20 - known))
            self.assertEqual(out['median'], None if known < 4 else '12.5')
        out = lifetime_baseline.check(self.result(12))
        self.assertEqual((out['p25'], out['p75'], out['target']['views']['value'], out['reference']['video_type']), ('3.25', '100.75', '7', 'short'))
        withheld = lifetime_baseline.check(self.result(0, state='mixed_definition_epochs'))
        self.assertEqual((withheld['sample_size'], withheld['definition'], withheld['median']),
                         (0, {'state': 'mixed_definition_epochs', 'epoch': None, 'unknown_definition_epoch_count': 0}, None))
        with self.assertRaises(ValueError):
            lifetime_baseline.check(self.result(5, state='mixed_definition_epochs'))
        with self.assertRaises(ValueError):
            lifetime_baseline.check(self.result(30))
        self.assertIsNone(lifetime_baseline.check(self.result(12, member=False))['target'])


class BindingTests(unittest.TestCase):
    """The generic hook: bind before the read slot, cap the lifetime, fence after the read."""

    def test_the_service_binds_caps_and_fences(self):
        calls = []
        result = {'video_id': VIDEO, 'video_type': 'long', 'member': False, 'frozen': None, 'description': False, 'row': None}

        class Admission:
            def enter(self, **_):
                calls.append('slot')
                from contextlib import nullcontext
                return nullcontext()

        def bind(service, plan, cancelled, deadline):
            calls.append('bind')
            return Binding({'inspect_as_of': None, 'inspect_frozen': None, 'inspect_end_at': None, 'inspect_frozen_counters': False},
                           valid_before=NOW + timedelta(seconds=30), finish=lambda: calls.append('finish'))

        kind = replace(OPERATION_KINDS['video.static_details.v1'], bind=bind)
        from research_query import aggregate_service
        with patch.object(aggregate_service, 'OPERATION_KINDS', {**OPERATION_KINDS, kind.id: kind}):
            service = QueryService(Repository(result), lambda: CONTEXT, clock=lambda: NOW, admission=Admission())
            output = service.execute_operation(request(kind.id))
        self.assertEqual(calls, ['bind', 'slot', 'finish'])
        self.assertEqual(output['context']['valid_before'], (NOW + timedelta(seconds=30)).isoformat().replace('+00:00', 'Z'))

        def lapsed():
            raise QueryRejected('cursor_expired')
        kind = replace(kind, bind=lambda *args: Binding({}, finish=lapsed))
        with patch.object(aggregate_service, 'OPERATION_KINDS', {**OPERATION_KINDS, kind.id: kind}):
            with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
                QueryService(Repository(result), lambda: CONTEXT, clock=lambda: NOW).execute_operation(request(kind.id))

    def test_inspection_binding_checks_the_frozen_result(self):
        plan = aggregate.validate_operation(request('video.static_details.v1'), CONTEXT)
        self.assertEqual(inspection.bind(None, plan, lambda: False, 0).parameters, inspection.CURRENT)
        frozen_plan = aggregate.validate_operation(request('video.static_details.v1', {'video_id': VIDEO,
                                                   'population': {'kind': 'frozen', 'frozen_ref': 'abc'}}), CONTEXT)

        class Session:
            source_id, video_type, scope_kind, scope_id, total_count = 'public.videos.current.v1', 'long', 'channel', CHANNEL, 3

        class Handle:
            session = Session()
            def member(self, video):
                if video != VIDEO:
                    raise QueryRejected('source_unavailable')
                from research_query.frozen import FrozenMember
                return FrozenMember({'id': VIDEO, 'cells': {'video.views': {'state': 'known', 'value': '5', 'provenance_ref': 'v',
                                     'observed_at': '2026-10-24T23:00:00Z', 'expires_at': '2026-11-23T23:00:00Z', 'observation': None},
                                     'video.likes': {'state': 'unknown', 'value': None, 'provenance_ref': 'l', 'observed_at': None,
                                     'expires_at': None, 'observation': None}, 'video.comments': {'state': 'known', 'value': '0',
                                     'provenance_ref': 'c', 'observed_at': '2026-10-24T23:00:00Z', 'expires_at': '2026-11-23T23:00:00Z',
                                     'observation': None}, 'channel.title': {'state': 'known'}}},
                                    2, NOW, NOW + timedelta(seconds=500), 'f' * 64, 'k', 'public.videos.current.v1', 'long')
            def require_current(self):
                pass

        with patch('research_query.frozen_service.frozen_reference', return_value=Handle()):
            binding = inspection.bind(object(), frozen_plan, lambda: False, 0)
        self.assertEqual((binding.parameters['inspect_as_of'], binding.parameters['inspect_end_at'],
                          binding.parameters['inspect_frozen_counters'], binding.valid_before),
                         (NOW, '2026-10-24T23:00:00Z', True, NOW + timedelta(seconds=500)))
        self.assertNotIn('channel.title', binding.parameters['inspect_frozen'].obj['cells'])
        for attribute, value, code in (('video_type', 'short', 'cursor_context_changed'), ('scope_id', 'UC' + 'b' * 22, 'cursor_context_changed')):
            with patch.object(Session, attribute, value), \
                    patch('research_query.frozen_service.frozen_reference', return_value=Handle()):
                with self.assertRaisesRegex(QueryRejected, code):
                    inspection.bind(object(), frozen_plan, lambda: False, 0)
        other = aggregate.validate_operation(request('video.static_details.v1', {'video_id': 'b0000000001',
                                             'population': {'kind': 'frozen', 'frozen_ref': 'abc'}}), CONTEXT)
        with patch('research_query.frozen_service.frozen_reference', return_value=Handle()):
            with self.assertRaisesRegex(QueryRejected, 'source_unavailable'):
                inspection.bind(object(), other, lambda: False, 0)


if __name__ == '__main__':
    unittest.main()
