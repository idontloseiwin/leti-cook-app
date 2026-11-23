"""Observed growth, coverage, filtering and rounding."""
from decimal import Decimal, ROUND_HALF_EVEN
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import itertools
import os
from pathlib import Path
import random
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from db.reads import research_growth
from db.reads.research_aggregate import compile_operation
from db.reads.research_sql import _SOURCE, compile_query
from research_query import aggregate, growth
from research_query.contracts import QueryResult
from research_query.operation_kinds import observed_growth_summary as summary
from research_query.operations import FIELD_OPERATIONS, OPERATIONS
from research_query.registry import FIELDS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities, validate
import web.research as web_research
from test_research_operation_kinds import assert_gateway_statement
from test_research_query_contract import query
from test_research_query_service import NOW, Repository

CONTEXT = AccessContext('fixture', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
EPOCH = 'youtube.views.play_start.20260824'


def per_day_cases():
    """(fixed cases, 20,000 seeded random cases) of (delta views, interval seconds)."""
    rng = random.Random(28)
    fixed = [(1, 691200), (3, 691200), (-1, 691200), (-3, 691200), (0, 86400), (2800, 2419200), (-100, 2419200),
             (9223372036854775000, 2419200), (-9223372036854775000, 86400), (400, 2408399), (1, 2505600)]
    drawn = [(rng.randint(-10**12, 10**12), rng.randint(86400, 2505600)) for _ in range(20000)]
    fixed += [(delta, interval) for delta in range(-50, 51) for interval in (86400, 172800, 691200, 2419200)]
    return fixed, drawn


def growth_filter_rows():
    rows = []
    for status in growth.STATUSES:
        for delta, interval in itertools.product((-500, -1, 0, 1, 99, 100, 101, 5000), (86400, 86399 * 9, 2408399, 2419200, 2505600)):
            rows.append({'status': status, 'delta_views': delta if status in growth.CALCULABLE else None,
                         'actual_interval_seconds': interval if status != 'unavailable' else None})
    return rows


GROWTH_FILTERS = [{}, {'min_views_gained': '100'}, {'max_views_gained': '0'}, {'min_views_gained': '-1', 'max_views_gained': '100'},
                  {'min_tracked_days': '0'}, {'min_tracked_days': '9'}, {'min_tracked_days': '28'}, {'min_tracked_days': '29'},
                  {'min_tracked_days': '30'}, {'min_tracked_days': '1000000000000000'}, {'full_history': True}, {'full_history': False},
                  {'min_views_gained': '1', 'min_tracked_days': '27', 'full_history': True}]


def evidence(status='ready', **changes):
    """One synthetic pair of observation endpoints per status."""
    end = NOW - timedelta(hours=2)
    base = dict(method=growth.OPERATION_ID, version=1, status=status, reason=None, from_at=None, to_at=None,
                from_views=None, to_views=None, actual_interval_seconds=None, boundary_distance_seconds=None,
                tracked_days=None, definition_epoch=None, from_definition_epoch=None, to_definition_epoch=None, time_zone='UTC')
    start = {'ready': end - timedelta(days=28), 'partial': end - timedelta(days=9, hours=1),
             'started_today': end - timedelta(hours=1), 'waiting_next_snapshot': end}.get(status)
    if start is not None:
        interval = int((end - start).total_seconds())
        base.update(from_at=start, to_at=end, actual_interval_seconds=str(interval),
                    reason=growth.REASONS[status][0], from_definition_epoch=EPOCH, to_definition_epoch=EPOCH, definition_epoch=EPOCH)
        if status in growth.CALCULABLE:
            base.update(from_views='1000', to_views='3801')
        if status == 'ready':
            base['boundary_distance_seconds'] = '0'
        if status == 'partial':
            base['tracked_days'] = str(interval // 86400)
    else:
        base.update(reason='current_data_stale')
    base.update(changes)
    return base


def growth_row(status='ready', **changes):
    """The prepared columns research_growth.FIELDS produce for one row."""
    e = evidence(status, **changes)
    calculable = e['status'] in growth.CALCULABLE
    delta = int(e['to_views']) - int(e['from_views']) if calculable else None
    interval = None if e['actual_interval_seconds'] is None else int(e['actual_interval_seconds'])
    state = lambda known: 'known' if known else 'unavailable'
    return {'observation_growth_28d_status': e['status'], 'observation_growth_28d_status_state': 'known',
            'observation_growth_28d_reason': e['reason'],
            'observation_views_gained_28d': delta, 'observation_views_gained_28d_state': state(calculable),
            'observation_views_per_day_28d': None if delta is None else growth.average_views_per_day(delta, interval),
            'observation_views_per_day_28d_state': state(calculable),
            'observation_growth_28d_interval_seconds': interval, 'observation_growth_28d_interval_seconds_state': state(interval is not None),
            'observation_growth_28d_tracked_days': None if e['tracked_days'] is None else int(e['tracked_days']),
            'observation_growth_28d_tracked_days_state': state(e['tracked_days'] is not None),
            'observation_growth_28d_observed_at': e['to_at'] or NOW, 'observation_growth_28d_expires_at': NOW + timedelta(hours=600),
            'observation_growth_28d_evidence': e}


def holds(expression, cells):
    """The shared predicate semantics on one row's cells (unknown is never true)."""
    if 'all' in expression:
        return all(holds(item, cells) for item in expression['all'])
    value = cells[expression['field']]
    if expression['op'] == 'is_known':
        return value is not None
    if value is None:
        return False
    if expression['op'] == 'eq':
        return value == expression['value']
    number, bound = int(value), int(expression['value'])
    return number >= bound if expression['op'] == 'gte' else number <= bound


class VocabularyTests(unittest.TestCase):
    def test_per_day_rounding_uses_half_even(self):
        fixed, drawn = per_day_cases()
        for delta, interval in fixed + drawn:
            expected = (Decimal(delta) * 86400 / interval).quantize(Decimal('.01'), rounding=ROUND_HALF_EVEN)
            self.assertEqual(Decimal(growth.average_views_per_day(delta, interval)), expected, (delta, interval))
        self.assertEqual([growth.average_views_per_day(d, 691200) for d in (1, 3, -1)], ['0.12', '0.38', '-0.12'])

    def test_growth_conditions_require_a_calculable_interval(self):
        rows = growth_filter_rows()
        for raw in GROWTH_FILTERS:
            normalized = {k:v for k,v in raw.items() if v is not False}
            expression = growth.legacy_conditions(normalized)
            if expression is not None:
                plan = validate(canonical(query(select=['video.id'], where=expression)), CONTEXT)  # A valid shared condition.
                self.assertTrue(set(growth.FIELDS) & set(plan.referenced_fields))
            for row in rows:
                with self.subTest(filters=raw, row=row):
                    gained, interval = row['delta_views'], row['actual_interval_seconds']
                    matches = not normalized or (gained is not None
                              and ('min_views_gained' not in raw or gained >= int(raw['min_views_gained']))
                              and ('max_views_gained' not in raw or gained <= int(raw['max_views_gained']))
                              and ('min_tracked_days' not in raw or interval >= 86400*int(raw['min_tracked_days']))
                              and (not raw.get('full_history') or row['status'] == 'ready'))
                    cells = {growth.STATUS: row['status'], growth.GAINED: None if row['delta_views'] is None else str(row['delta_views']),
                             growth.INTERVAL: None if row['actual_interval_seconds'] is None else str(row['actual_interval_seconds'])}
                    self.assertEqual(expression is None or holds(expression, cells), matches)
        self.assertEqual(growth.LEGACY_SORT_FIELDS, {'views_gained': growth.GAINED})

    def test_registry_grants_vocabulary_and_capabilities(self):
        self.assertEqual(FIELD_OPERATIONS[growth.GAINED], growth.OPERATION_ID)
        self.assertEqual({f for f, op in FIELD_OPERATIONS.items() if op == growth.OPERATION_ID}, set(growth.FIELDS))
        for field in growth.FIELDS:
            self.assertEqual((FIELDS[field].origin, FIELDS[field].use_class), ('calculated', 'additional_analytics'))
        self.assertEqual(FIELDS[growth.STATUS].allowed_values, growth.STATUSES)
        self.assertFalse(FIELDS[growth.STATUS].sortable)
        for field in (growth.GAINED, growth.PER_DAY, growth.INTERVAL, growth.TRACKED_DAYS):
            self.assertTrue(FIELDS[field].sortable)
        without = replace(CONTEXT, allowed_operations=frozenset(OPERATIONS) - {growth.OPERATION_ID})
        for placement in (query(select=[growth.STATUS]), query(order_by=[{'field': growth.GAINED, 'direction': 'desc'}]),
                          query(where={'field': growth.INTERVAL, 'op': 'gte', 'value': '86400'})):
            with self.assertRaises(QueryRejected) as error:
                validate(canonical(placement), without)
            self.assertEqual(error.exception.code, 'grant_required')
        for value in ('waiting', 'READY', '', 'x' * 33):
            with self.assertRaises(QueryRejected):
                validate(canonical(query(where={'field': growth.STATUS, 'op': 'eq', 'value': value})), CONTEXT)
        plan = validate(canonical(query(where={'field': growth.STATUS, 'op': 'in', 'value': ['partial', 'ready']})), CONTEXT)
        self.assertIn(growth.STATUS, plan.referenced_fields)
        with self.assertRaises(QueryRejected):
            validate(canonical(query(order_by=[{'field': growth.STATUS, 'direction': 'asc'}])), CONTEXT)
        visible = {f['id']: f for f in capabilities(CONTEXT)['fields']}
        self.assertEqual(visible[growth.STATUS]['values'], list(growth.STATUSES))
        self.assertNotIn('values', visible[growth.GAINED])
        self.assertIn(growth.OPERATION_ID, capabilities(CONTEXT)['operations'])
        self.assertIn(growth.SUMMARY_ID, capabilities(CONTEXT)['operations'])

    def test_the_legacy_derived_insights_flag_gates_growth(self):
        for value, allowed in (('true', True), ('1', True), ('false', False), (None, False)):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=False):
                os.environ.pop('CHANNEL_DERIVED_INSIGHTS_ENABLED', None)
                if value is not None:
                    os.environ['CHANNEL_DERIVED_INSIGHTS_ENABLED'] = value
                operations = web_research.allowed_operations()
                self.assertEqual(growth.OPERATION_ID in operations, allowed)
                self.assertEqual(growth.SUMMARY_ID in operations, allowed)


class SqlTests(unittest.TestCase):
    def compile(self, **changes):
        return compile_query(validate(canonical(query(**changes)), CONTEXT))

    def test_gateway_statement_parameters_and_no_age_limit(self):
        sql, params = self.compile(select=['video.id', *growth.FIELDS], order_by=[{'field': growth.GAINED, 'direction': 'desc'}],
                                   where=growth.legacy_conditions({'min_views_gained': '5', 'full_history': True}),
                                   time={'mode': 'current', 'timezone': 'Asia/Kathmandu'})
        assert_gateway_statement(self, sql)
        self.assertFalse([name for name in re.findall(r'%\((\w+)\)s', sql) if name not in params])
        self.assertEqual({k: params[k] for k in params if k.startswith('growth_')},
                         {'growth_window': '672 hours', 'growth_late_start': '10800 seconds', 'growth_partial_minimum': '86400 seconds',
                          'growth_horizon': '120 seconds', 'growth_time_zone': 'Asia/Kathmandu'})
        self.assertIsNone(params['observation_age_limit'])  # Growth never narrows the population by age.
        self.assertIn('g_args.evaluated_at+%(growth_horizon)s::interval', sql)
        self.assertIn("jsonb_build_object('observation_views_per_day_28d',details.observation_views_per_day_28d::text)", sql)
        self.assertNotIn('CROSS JOIN args g_args', self.compile(select=['video.id', 'observation.views_delta'])[0])

    def test_current_and_frozen_horizons(self):
        # The current page's maximum life is the literal in the shared statement.
        self.assertIn(f"evaluated_at+interval '{growth.CURRENT_RESULT_SECONDS} seconds'", _SOURCE)
        sql, params = self.compile(select=['video.id', growth.STATUS], consistency='frozen')
        self.assertIn('g_args.fresh_until-%(window)s::interval AS points_from', sql)
        self.assertNotIn('growth_horizon', params)
        assert_gateway_statement(self, sql)

    def test_endpoint_metadata_needs_only_the_retention_window(self):
        """Metadata refreshed every 672 h must not make growth unavailable when a refresh is late."""
        for consistency, horizon in (('current', 'g_args.evaluated_at'), ('frozen', 'g_args.fresh_until')):
            with self.subTest(consistency=consistency):
                sql, params = self.compile(select=['video.id', growth.STATUS], consistency=consistency)
                self.assertIn(f'n.metadata_checked_at>{horizon}-%(window)s::interval', sql)
                self.assertEqual(params['window'], '696 hours')
                # The counter and the inventory proof keep the 28-day floor.
                self.assertIn('n.checked_at>=g_args.evaluated_at-%(growth_window)s::interval', sql)
                self.assertIn('n.identity_clock>=g_args.evaluated_at-%(growth_window)s::interval', sql)
                self.assertNotIn('n.metadata_checked_at>=g_args.evaluated_at-%(growth_window)s', sql)
        self.assertEqual(growth.METHODS['endpoint_freshness'], 'counter_and_inventory_observed_within_672h_metadata_within_696h')

    def test_population_operation_compiles_and_checks(self):
        raw = lambda parameters: canonical(dict(version=1, operation=growth.SUMMARY_ID, query=query(select=['video.id']), parameters=parameters))
        for parameters in ({}, {'population': {'kind': 'query'}}):
            plan = aggregate.validate_operation(raw(parameters), CONTEXT)
            self.assertIn(growth.STATUS, plan.query.referenced_fields)
            sql, params = compile_operation(plan)
            assert_gateway_statement(self, sql)
            self.assertFalse([name for name in re.findall(r'%\((\w+)\)s', sql) if name not in params])
        for parameters in ({'population': {'kind': 'frozen'}}, {'x': 1}, {'population': 'query'}):
            with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                aggregate.validate_operation(raw(parameters), CONTEXT)
        result = summary.check(dict(population=6, statuses={'ready': 3, 'partial': 1, 'unavailable': 2},
                                    reasons={'full_window_history_unavailable': 1, 'current_data_stale': 2}, time_zone='UTC'))
        self.assertEqual({k: result[k] for k in ('requested_video_count', 'calculable_count', 'ready_count', 'started_today_count', 'unavailable_count')},
                         dict(requested_video_count=6, calculable_count=4, ready_count=3, started_today_count=0, unavailable_count=2))
        self.assertEqual(result['methods'], growth.METHODS)
        for bad in (dict(population=2, statuses={'ready': 1}, reasons={}, time_zone='UTC'),
                    dict(population=1, statuses={'waiting': 1}, reasons={'x': 1}, time_zone='UTC'),
                    dict(population=1, statuses={'partial': 1}, reasons={}, time_zone='UTC'),
                    dict(population=1, statuses={'ready': True}, reasons={}, time_zone='UTC')):
            with self.assertRaises(ValueError):
                summary.check(bad)


class EvidenceTests(unittest.TestCase):
    def wire(self, value):
        from research_query.service import iso
        return {**value, 'from_at': value['from_at'] and iso(value['from_at']), 'to_at': value['to_at'] and iso(value['to_at'])}

    def test_endpoint_integrity_rules(self):
        for status in growth.STATUSES:
            self.assertIsNone(growth.evidence_problem(self.wire(evidence(status))), status)
        broken = [evidence('ready', boundary_distance_seconds='3600'), evidence('ready', tracked_days='28'),
                  evidence('ready', reason='full_window_history_unavailable'), evidence('ready', from_definition_epoch='other.epoch'),
                  evidence('partial', tracked_days='8'), evidence('partial', boundary_distance_seconds='0'),
                  evidence('started_today', actual_interval_seconds='90000'), evidence('waiting_next_snapshot', reason='tracking_started_today'),
                  evidence('unavailable', from_at=NOW), evidence('unavailable', reason='endpoint_changed_during_read'),
                  evidence('unavailable', reason='incompatible_definition')]
        for value in broken:
            with self.subTest(value=value):
                self.assertIsNotNone(growth.evidence_problem(self.wire(value)))
        incompatible = dict(evidence('ready'), status='unavailable', reason='incompatible_definition', actual_interval_seconds=None,
                                boundary_distance_seconds=None, definition_epoch=None, from_definition_epoch='other.epoch')
        self.assertIsNone(growth.evidence_problem(self.wire(incompatible)))
        unknown = dict(incompatible, reason='unknown_definition', from_definition_epoch=None)
        self.assertIsNone(growth.evidence_problem(self.wire(unknown)))

    def test_status_reason_survives_wire_encoding(self):
        for status in growth.STATUSES:
            with self.subTest(status=status):
                original = evidence(status)
                wire = json.loads(json.dumps(self.wire(original)))
                self.assertEqual((wire['status'], wire['reason']), (status, original['reason']))
                self.assertIsNone(growth.evidence_problem(wire))


class ServiceTests(unittest.TestCase):
    def execute(self, select, row):
        repo = Repository()
        repo.data['rows'][0].update(row)
        return QueryService(repo, lambda: CONTEXT, clock=lambda: NOW).execute(canonical(query(select=select)))

    def test_cells_share_one_evidence_object_per_row(self):
        for status in growth.STATUSES:
            with self.subTest(status=status):
                cells = self.execute(['video.id', *growth.FIELDS], growth_row(status))['rows'][0]['cells']
                self.assertEqual(cells[growth.STATUS]['value'], status)
                self.assertEqual(cells[growth.STATUS]['observation']['status'], status)
                self.assertEqual([cells[f]['observation'] for f in growth.FIELDS[1:]], [None] * 4)
                calculable = status in growth.CALCULABLE
                self.assertEqual(cells[growth.GAINED]['value'], '2801' if calculable else None)
                self.assertEqual(cells[growth.PER_DAY]['state'], 'known' if calculable else 'unavailable')
                self.assertEqual(cells[growth.TRACKED_DAYS]['value'], '9' if status == 'partial' else None)
                self.assertTrue(cells[growth.GAINED]['provenance_ref'].startswith(growth.GAINED + ':'))
        cells = self.execute(['video.id', growth.TRACKED_DAYS, growth.PER_DAY], growth_row('partial'))['rows'][0]['cells']
        self.assertEqual(cells[growth.PER_DAY]['observation']['tracked_days'], '9')
        self.assertIsNone(cells[growth.TRACKED_DAYS]['observation'])
        self.assertEqual(cells[growth.PER_DAY]['value'], growth.average_views_per_day(2801, 9 * 86400 + 3600))

    def test_values_that_disagree_with_their_evidence_are_refused(self):
        for status, change in (('ready', {'observation_views_gained_28d': 2802}), ('ready', {'observation_views_per_day_28d': '100.01'}),
                               ('partial', {'observation_growth_28d_tracked_days': 8}), ('ready', {'observation_growth_28d_status': 'partial'}),
                               ('started_today', {'observation_views_gained_28d': 0, 'observation_views_gained_28d_state': 'known'})):
            with self.subTest(status=status, change=change), self.assertRaisesRegex(QueryRejected, 'backend_unavailable'):
                self.execute(['video.id', *growth.FIELDS], {**growth_row(status), **change})
        broken = growth_row('ready')
        broken['observation_growth_28d_evidence'] = dict(broken['observation_growth_28d_evidence'], boundary_distance_seconds='7200')
        with self.assertRaisesRegex(QueryRejected, 'backend_unavailable'):
            self.execute(['video.id', growth.STATUS], broken)
        # A result model never carries growth evidence on another field, or twice.
        output = self.execute(['video.id', growth.STATUS, growth.GAINED], growth_row('ready'))
        twice = deepcopy(output)
        twice['rows'][0]['cells'][growth.GAINED]['observation'] = twice['rows'][0]['cells'][growth.STATUS]['observation']
        with self.assertRaises(ValueError):
            QueryResult.model_validate(twice)


if __name__ == '__main__':
    unittest.main()
