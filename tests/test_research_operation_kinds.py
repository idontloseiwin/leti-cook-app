"""Whole-population operations are registered kinds; generic code has no per-operation branch.

No database or provider: SQL is compiled and checked by the gateway's own guard.
"""
import ast
from copy import deepcopy
from dataclasses import replace
import itertools
import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from db.base.postgres import _reviewed_statement
from db.reads import research_aggregate
from db.reads.research_aggregate import compile_operation
from research_query import aggregate, aggregate_service, graph, validation
from research_query.operation_kinds import OPERATION_KINDS, calendar, channel_history, summary
from db.reads.research_operations import COMPILERS
from db.reads.research_operations.common import FINAL_SELECT
from research_query.operation_kinds.kind import OperationDefinition, OperationKind, population_provenance
from research_query.operations import CALCULATED_FIELD_OPERATIONS, FIELD_GRANTS, FIELD_LEVEL_OPERATIONS, FIELD_OPERATIONS, OPERATIONS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical
import web.research as web_research
from test_research_channels import channel_query
from test_research_query_contract import query
from test_research_query_service import NOW, page

# The five operations before kinds existed, written out again: moving a
# definition into its own module must not change any value. New meaning = new id.
TODAY = {x.id: x for x in (
    OperationDefinition('video.summary.v1','public.videos.current.v1','video','population','descriptive_analytics',(),
        'Exact numeric summaries and fixed distributions over the full matching population; unknown values excluded per metric.'),
    OperationDefinition('video.publication_calendar.v1','public.videos.current.v1','video','calendar','descriptive_analytics',(),
        'Local calendar dates, hours and ISO weekdays from real publication timestamps, before pagination.'),
    OperationDefinition('channel.observed_history.v1','public.channel.observations.v1','channel+observed_at','channel_series','channel_history',(),
        'Last real retained channel observation per time bucket; no interpolation, video epoch or computed rate.',population='authorized_single_channel'),
    OperationDefinition('video.observed_change.v1','public.video.observations.v1','video+observed_at+epoch','video','additional_analytics',
        ('observation.views_delta','observation.views_per_hour'),
        'Latest snapshot matching the current counter; last real point at or before its target boundary, within tolerance. Signed difference and average over actual elapsed seconds.'),
    OperationDefinition('video.observed_age.v1','public.video.observations.v1','video+observed_at+epoch','video','source_observation',
        ('observation.views_at_age',),
        # A Stream's age is counted from its actual end (was: publication).
        'Latest real point at or before publication (a Stream: its actual end) + target age, within tolerance. Actual age disclosed; no interpolation or publication-time zero.'),
)}
# One accepted request per kind of today; a new kind's own test brings its samples.
SAMPLES = {
    'video.summary.v1': (query(), {}),
    'video.publication_calendar.v1': (query(), {'year': 2026, 'time_zone': 'Europe/Kyiv'}),
    'channel.observed_history.v1': (channel_query(), {'metric': 'views', 'hours': 24, 'bucket_hours': 1}),
}
CONTEXT = AccessContext('synthetic', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))


def request(operation, q, parameters):
    return canonical(dict(version=1, operation=operation, query=q, parameters=parameters))


def assert_gateway_statement(test, sql):
    """The PostgreSQL gateway guard every operation statement must pass at run time."""
    test.assertNotIn(';', sql)
    test.assertTrue(_reviewed_statement(sql, read=True, max_rows=1))


class Repository:
    def __init__(self, result, **extra):
        self.data = {**page(), 'source_exists': True, 'result': result, **extra}
        self.data.pop('rows')

    def read_operation(self, plan, *, deadline, cancelled):
        return deepcopy(self.data)

    def recheck_scope(self, *args, **kwargs):
        pass


class RegistryTests(unittest.TestCase):
    def test_todays_definitions_are_unchanged_and_all_still_registered(self):
        self.assertIs(type(OPERATIONS), MappingProxyType)
        self.assertIs(type(OPERATION_KINDS), MappingProxyType)
        for identity, definition in TODAY.items():
            self.assertEqual(OPERATIONS[identity], definition)
        self.assertTrue({'video.summary.v1', 'video.publication_calendar.v1', 'channel.observed_history.v1'} <= set(OPERATION_KINDS))
        self.assertEqual({x.id for x in FIELD_LEVEL_OPERATIONS}, {'video.observed_change.v1', 'video.observed_age.v1',
                                                                  'video.observed_growth_28d.v1'})
        self.assertTrue({'observation.views_delta': 'video.observed_change.v1', 'observation.views_per_hour': 'video.observed_change.v1',
                         'observation.views_at_age': 'video.observed_age.v1'}.items() <= FIELD_OPERATIONS.items())
        self.assertEqual(set(OPERATIONS), set(OPERATION_KINDS) | {x.id for x in (*FIELD_LEVEL_OPERATIONS, *CALCULATED_FIELD_OPERATIONS)})
        # Calculated row columns are granted like observation columns but never join observations.
        self.assertEqual({x.id for x in CALCULATED_FIELD_OPERATIONS}, {'video.static_metrics.v1'})
        self.assertEqual(set(FIELD_GRANTS), set(FIELD_OPERATIONS) | {f for x in CALCULATED_FIELD_OPERATIONS for f in x.outputs})
        self.assertFalse(set(FIELD_OPERATIONS) & {f for x in CALCULATED_FIELD_OPERATIONS for f in x.outputs})

    def test_every_kind_fills_the_contract(self):
        for identity, kind in OPERATION_KINDS.items():
            with self.subTest(operation=identity):
                self.assertIsInstance(kind, OperationKind)
                self.assertIs(OPERATIONS[identity], kind.definition)
                self.assertEqual(kind.id, identity)
                self.assertRegex(identity, r'\A(video|channel)\.[a-z][a-z0-9_]*\.v[1-9][0-9]*\Z')
                self.assertTrue(identity.endswith('.v%d' % kind.definition.version))
                self.assertEqual(kind.definition.outputs, ())  # Query columns are field-level operations.
                self.assertIn(kind.definition.policy, web_research.POLICY_FLAGS)
                for part in (kind.dependencies, COMPILERS[identity], kind.check, kind.provenance):
                    self.assertTrue(callable(part))
                self.assertTrue(kind.block is None or re.fullmatch(r'[a-z][a-z0-9_]{0,31}', kind.block))
        for definition in (*FIELD_LEVEL_OPERATIONS, *CALCULATED_FIELD_OPERATIONS):
            self.assertTrue(definition.outputs)
            self.assertIn(definition.policy, web_research.POLICY_FLAGS)

    def test_registry_refuses_duplicates(self):
        from research_query import operation_kinds
        twin = replace(summary.KIND, block='other')
        for modules in ((summary, summary), (summary, type('M', (), {'KIND': twin}))):
            with self.assertRaisesRegex(RuntimeError, 'invalid_operation_registry'):
                operation_kinds._registry(modules)
        same_block = type('M', (), {'KIND': replace(calendar.KIND, definition=replace(calendar.DEFINITION, id='video.other.v1'))})
        with self.assertRaisesRegex(RuntimeError, 'invalid_operation_registry'):
            operation_kinds._registry((calendar, same_block))

    def test_generic_modules_have_no_per_operation_branch(self):
        identity = re.compile(r'\A(video|channel)\.[a-z_]+\.v[0-9]+\Z')
        for name in ('research_query/aggregate.py', 'research_query/aggregate_service.py', 'research_query/graph.py',
                     'research_query/graph_service.py', 'db/reads/research_aggregate.py', 'web/research.py'):
            tree = ast.parse((ROOT/'application'/name).read_text(encoding='utf-8'))
            with self.subTest(module=name):
                self.assertEqual([n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                                  and isinstance(n.value, str) and identity.match(n.value)], [])
                self.assertEqual([n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
                                  and n.id in {'SUMMARY', 'CALENDAR', 'HISTORY', 'METRICS'}], [])


class PolicyGateTests(unittest.TestCase):
    FLAGS = ('CHANNEL_BASELINE_ANALYTICS_ENABLED', 'CHANNEL_DERIVED_INSIGHTS_ENABLED',
             'CHANNEL_LAUNCH_ANALYTICS_ENABLED', 'CHANNEL_TIMELINE_INSIGHTS_ENABLED')

    def allowed(self, environment):
        values = {name: value for name, value in environment.items() if value is not None}
        with patch.dict(os.environ, values):
            for name in self.FLAGS:
                if name not in values:
                    os.environ.pop(name, None)
            return web_research.allowed_operations()

    def test_policy_flags_allow_exactly_what_the_fixed_lists_allowed(self):
        def on(value):
            return (value or 'false').strip().lower() in {'1', 'true', 'yes', 'on'}
        values = (None, '', '1', 'true', ' Yes ', 'ON', '0', 'false', 'off', 'enabled')
        for baseline, derived, other in itertools.product(values, values, (None, '1')):
            with self.subTest(baseline=baseline, derived=derived, other=other):
                previous = ({'video.observed_age.v1'}
                            | ({'video.summary.v1', 'video.publication_calendar.v1', 'channel.observed_history.v1'} if on(baseline) else set())
                            | ({'video.observed_change.v1'} if on(derived) else set()))
                allowed = self.allowed(dict(zip(self.FLAGS, (baseline, derived, other, other))))
                self.assertEqual(allowed & set(TODAY), previous)

    def test_unknown_policy_never_runs(self):
        unknown = OperationDefinition('video.unreviewed.v1', 'public.videos.current.v1', 'video', 'population', 'unreviewed_policy', (), 'x')
        with patch.object(web_research, 'OPERATIONS', MappingProxyType({**OPERATIONS, unknown.id: unknown})):
            allowed = self.allowed(dict.fromkeys(self.FLAGS, '1'))
        self.assertNotIn(unknown.id, allowed)
        self.assertTrue(set(TODAY) <= allowed)


class GraphBlockTests(unittest.TestCase):
    def test_block_types_follow_the_registry(self):
        self.assertEqual(graph.OPERATION_TYPES, {k.block: k.id for k in OPERATION_KINDS.values() if k.block is not None})
        self.assertTrue({'summary': 'video.summary.v1', 'calendar': 'video.publication_calendar.v1',
                         'history': 'channel.observed_history.v1'}.items() <= graph.OPERATION_TYPES.items())
        self.assertEqual(graph.NODE_TYPES, {'source', 'filter', 'selection', 'table', 'gallery', *graph.OPERATION_TYPES})
        self.assertEqual(graph.OUTPUT_TYPES, {'table', 'gallery', *graph.OPERATION_TYPES})


class KindTests(unittest.TestCase):
    def test_todays_kinds_compile_gateway_statements_with_parameters_only(self):
        for identity, (q, parameters) in SAMPLES.items():
            with self.subTest(operation=identity):
                plan = aggregate.validate_operation(request(identity, q, parameters), CONTEXT)
                sql, params = compile_operation(plan)
                assert_gateway_statement(self, sql)
                self.assertTrue(sql.endswith(COMPILERS[identity](plan, '', {})[0]))
                for name in re.findall(r'%\((\w+)\)s', sql):
                    self.assertIn(name, params)

    def test_each_kind_checks_its_own_result_shape(self):
        metric = dict(total='2.675', mean='0.005', median='1E+3', q1=None, q3='100.00', min='0', max='9007199254740993')
        result = summary.check(dict(population=1, metrics={m: dict(metric) for m in summary.METRICS}))
        self.assertEqual(result['metrics']['views'], dict(total='2.68', mean='0', median='1000', q1=None, q3='100', min='0', max='9007199254740993'))
        for bad in (dict(population=1, metrics={}), dict(population=1, metrics={m: dict(metric) for m in summary.METRICS}, extra=1)):
            with self.assertRaises(ValueError):
                summary.check(bad)
        days = dict(days=[{}] * 366, hours=[{}] * 24, weekdays=[{}] * 7)
        self.assertIs(calendar.check(days), days)
        for key, size in (('days', 367), ('hours', 25), ('weekdays', 8)):
            with self.assertRaises(ValueError):
                calendar.check({**days, key: [{}] * size})
        self.assertEqual(len(channel_history.check(dict(points=[{}] * 697))['points']), 697)
        with self.assertRaises(ValueError):
            channel_history.check(dict(points=[{}] * 698))

    def test_a_new_kind_plugs_in_without_touching_generic_code(self):
        """Template for the next operations: one module's worth of parts, one registry entry."""
        definition = OperationDefinition('video.example_count.v1', 'public.videos.current.v1', 'video', 'population',
                                         'descriptive_analytics', (), 'Count of the matching population.')

        def dependencies(params, source_id, scope_kind):
            if source_id != 'public.videos.current.v1' or params:
                raise ValueError()
            return ('video.views',)

        def compile_sql(plan, sql, params):
            return sql + ',\nexample AS (SELECT count(*) AS n FROM population)' + FINAL_SELECT.replace(
                '{result}', "jsonb_build_object('population',(SELECT n FROM example))"), params

        def check(result):
            if set(result) != {'population'} or type(result['population']) is not int:
                raise ValueError()
            return result

        kind = OperationKind(definition, dependencies, check, population_provenance('calculated'))
        kinds = MappingProxyType({**OPERATION_KINDS, kind.id: kind})
        compilers = MappingProxyType({**COMPILERS, kind.id: compile_sql})
        with patch.object(aggregate, 'OPERATION_KINDS', kinds), patch.object(aggregate_service, 'OPERATION_KINDS', kinds), \
                patch.object(research_aggregate, 'COMPILERS', compilers), \
                patch.object(validation, 'CALCULATIONS', MappingProxyType({**OPERATIONS, kind.id: definition})):
            context = replace(CONTEXT, allowed_operations=frozenset({*OPERATIONS, kind.id}))
            raw = request(kind.id, query(select=['video.id']), {})
            plan = aggregate.validate_operation(raw, context)
            self.assertIn('video.views', plan.query.referenced_fields)  # Dependencies pass the field registry.
            sql, _ = compile_operation(plan)
            assert_gateway_statement(self, sql)
            output = QueryService(Repository({'population': 2}), lambda: context, clock=lambda: NOW).execute_operation(raw)
            self.assertEqual((output['operation'], output['result']), (kind.id, {'population': 2}))
            self.assertEqual(output['provenance'], dict(origin='calculated', method=kind.id,
                population='all_authorized_matching_rows_before_pagination', missing_values='excluded_per_metric', definition_epoch=None))
            for result in ({'population': 2, 'rows': []}, {'population': '2'}):
                with self.assertRaisesRegex(QueryRejected, 'backend_unavailable'):
                    QueryService(Repository(result), lambda: context, clock=lambda: NOW).execute_operation(raw)
            with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                aggregate.validate_operation(request(kind.id, query(), {'x': 1}), context)
            with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
                aggregate.validate_operation(raw, replace(context, allowed_operations=frozenset(OPERATIONS)))
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            aggregate.validate_operation(raw, CONTEXT)  # Not registered: never runs.


if __name__ == '__main__':
    unittest.main()
