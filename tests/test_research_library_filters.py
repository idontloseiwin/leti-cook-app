"""Library filter vocabulary in the shared backend: fields, presets, facets.

No database or provider. SQL is compiled and checked by the gateway's guard;
PostgreSQL behaviour is proved in tests/research_aggregate_pg.py.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from db.reads.research_aggregate import compile_operation
from db.reads.research_sql import compile_query
from research_query import aggregate
from research_query import registry as presets
from research_query.operation_kinds import facets, summary
from research_query.operations import OPERATIONS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities, search_literal, validate
import web.research as web_research
from test_research_operation_kinds import Repository, assert_gateway_statement, request
from test_research_query_contract import query
from test_research_query_service import NOW

CONTEXT = AccessContext('synthetic', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
CALCULATED = ('video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate')
LOCAL = ('video.published_date', 'video.published_year')


def plan(**changes):
    return validate(canonical(query(**changes)), CONTEXT)


def facets_request(q=None, **parameters):
    return request(facets.ID, q or query(select=['video.id']), {'population': {'kind': 'query'}, **parameters})


class TimeZoneTests(unittest.TestCase):
    def test_request_zone_is_validated_and_part_of_the_fingerprint(self):
        utc, berlin = plan(), plan(time={'mode': 'current', 'timezone': 'Europe/Berlin'})
        self.assertNotEqual(utc.fingerprint, berlin.fingerprint)
        for bad in ('+03:00', 'utc', 'Europe', 'Mars/Olympus', 'Europe/../etc', 'A' * 65):
            with self.subTest(zone=bad), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                plan(time={'mode': 'current', 'timezone': bad})

    def test_local_dates_and_years_bind_the_request_zone(self):
        for zone in ('UTC', 'Pacific/Kiritimati', 'America/Santiago'):
            sql, params = compile_query(plan(select=['video.id', *LOCAL], time={'mode': 'current', 'timezone': zone},
                                             where={'field': 'video.published_date', 'op': 'gte', 'value': '2026-03-01'}))
            self.assertEqual(params['query_time_zone'], zone)
            self.assertIn('AT TIME ZONE %(query_time_zone)s::text', sql)
            self.assertNotIn(zone, sql)
        self.assertNotIn('query_time_zone', compile_query(plan())[1])

    def test_local_date_literals(self):
        for value in ('2026-02-29', '0000-01-01', '2026-1-01', '2026-01-01T00:00:00Z', '20260101'):
            with self.subTest(value=value), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                plan(where={'field': 'video.published_date', 'op': 'gte', 'value': value})
        accepted = plan(where={'field': 'video.published_date', 'op': 'in', 'value': ['2024-02-29', '0001-01-01', '9999-12-31']})
        self.assertEqual(json.loads(accepted.encoded)['where']['value'], ['0001-01-01', '2024-02-29', '9999-12-31'])
        plan(where={'field': 'video.published_year', 'op': 'lt', 'value': '2024'})


class CalculatedFieldTests(unittest.TestCase):
    def test_baseline_gate_like_the_library(self):
        def allowed(flags):
            with patch.dict(os.environ, flags):
                return web_research.allowed_operations()
        on = allowed({'CHANNEL_BASELINE_ANALYTICS_ENABLED': '1', 'CHANNEL_DERIVED_INSIGHTS_ENABLED': '0'})
        off = allowed({'CHANNEL_BASELINE_ANALYTICS_ENABLED': '0', 'CHANNEL_DERIVED_INSIGHTS_ENABLED': '1'})
        self.assertTrue({'video.static_metrics.v1', facets.ID} <= on)
        self.assertFalse({'video.static_metrics.v1', facets.ID} & off)
        denied = replace(CONTEXT, allowed_operations=frozenset(off))
        for field in CALCULATED:
            with self.subTest(field=field), self.assertRaisesRegex(QueryRejected, 'grant_required'):
                validate(canonical(query(select=['video.id', field])), denied)
        offered = {f['id'] for f in capabilities(denied)['fields']}
        self.assertFalse(offered & set(CALCULATED))
        self.assertTrue(set(LOCAL) <= offered)  # Dates and years were never gated.
        self.assertTrue(set(CALCULATED) <= {f['id'] for f in capabilities(CONTEXT)['fields']})

    def test_rates_compare_exactly_and_are_not_sorted_or_matched_exactly(self):
        sql, params = compile_query(plan(select=['video.id', 'video.like_rate'],
                                         where={'field': 'video.like_rate', 'op': 'lte', 'value': '33.333333'}))
        self.assertRegex(sql, r'like_rate_numerator <= %\(p\d+\)s::numeric\*like_rate_denominator')
        self.assertIn('33.333333', params.values())
        for bad in ({'where': {'field': 'video.comment_rate', 'op': 'eq', 'value': '1'}},
                    {'where': {'field': 'video.comment_rate', 'op': 'in', 'value': ['1']}},
                    {'order_by': [{'field': 'video.like_rate', 'direction': 'desc'}]}):
            with self.subTest(bad=bad), self.assertRaisesRegex(QueryRejected, 'unsupported_operator'):
                plan(**bad)
        for value in ('-1', '1.1234567', 'abc'):
            with self.subTest(value=value):
                if value == '-1':
                    plan(where={'field': 'video.like_rate', 'op': 'gte', 'value': value})
                else:
                    with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                        plan(where={'field': 'video.like_rate', 'op': 'gte', 'value': value})

    def test_inputs_are_read_without_being_exposed(self):
        sql, _ = compile_query(plan(select=['video.id', 'video.tag_count', 'video.like_rate']))
        self.assertIn('CASE WHEN p.v_channel_id=p.channel_id THEN p.tags END AS tags', sql)
        self.assertIn('CASE WHEN p.v_channel_id=p.channel_id THEN p.like_count END AS likes', sql)
        self.assertNotIn('END AS tags\n', sql)
        self.assertIn('cardinality(tags)::numeric END AS tag_count', sql)


def search_samples():
    """The texts test_literal_collapses_like_the_library_query_text normalizes."""
    spaces = ''.join(chr(n) for n in range(0x3001) if chr(n).isspace()) + ' 　'
    samples = ['  Foo \t　 Bar  ', 'a' + spaces + 'b', spaces, 'x​y', '﻿a  b﻿', 'Straße   %_']
    return samples


class SearchTests(unittest.TestCase):
    def test_literal_collapses_like_the_library_query_text(self):
        for text in search_samples():
            with self.subTest(text=text):
                self.assertEqual(search_literal(text), ' '.join(text.split()))
        encoded = json.loads(plan(where={'field': 'video.title', 'op': 'contains_casefold_v1', 'value': ' a \n b '}).encoded)
        self.assertEqual(encoded['where']['value'], 'a b')

    def test_empty_text_orders_with_missing_values(self):
        sql, _ = compile_query(plan(order_by=[{'field': 'video.title', 'direction': 'asc'}]))
        self.assertIn("nullif(title_key,'') COLLATE \"C\" ASC NULLS LAST", sql)


class BucketTests(unittest.TestCase):
    def test_long_sets_are_the_familiar_library_presets(self):
        # The familiar Library's sets: the top-level groups of the shared file (catalogue_filters.PRESETS read the same file).
        PRESETS = json.loads(presets.PRESETS_FILE.read_text(encoding='utf-8'))
        for group in presets.PRESET_GROUP_FIELDS:
            self.assertEqual([b.as_dict() for b in presets.preset_buckets(group, 'long')], PRESETS[group])

    def test_every_set_covers_each_value_exactly_once(self):
        for group in presets.PRESET_GROUP_FIELDS:
            for kind in presets.VIDEO_TYPES:
                sets = presets.preset_buckets(group, kind)
                for value in range(0, 20000, 7):
                    hits = [b.key for b in sets if (b.min is None or value >= b.min) and (b.max is None or value < b.max)]
                    self.assertEqual(len(hits), 1 if value >= (sets[0].min or 0) else 0, (group, kind, value))
        self.assertEqual([(b.key, b.min, b.max) for b in presets.preset_buckets('duration', 'short')],
                         [('under_15s', None, 15), ('15s_30s', 15, 30), ('30s_60s', 30, 60), ('1m_3m', 60, 181), ('over_3m', 181, None)])
        self.assertEqual([(b.key, b.min, b.max) for b in presets.preset_buckets('duration', 'stream')],
                         [('under_30m', None, 1800), ('30m_60m', 1800, 3600), ('1h_2h', 3600, 7200), ('2h_4h', 7200, 14400), ('4h_plus', 14400, None)])

    def test_invalid_sets_stop_the_registry(self):
        for items in ([], [{'key': 'a', 'label': 'A', 'min': None, 'max': 5}],
                      [{'key': 'a', 'label': 'A', 'min': None, 'max': 5}, {'key': 'b', 'label': 'B', 'min': 6, 'max': None}],
                      [{'key': 'a', 'label': 'A', 'min': None, 'max': 5}, {'key': 'a', 'label': 'B', 'min': 5, 'max': None}],
                      [{'key': 'a', 'label': 'A', 'min': -1, 'max': None}]):
            with self.subTest(items=items), self.assertRaisesRegex(RuntimeError, 'invalid_bucket_registry'):
                presets._checked_presets(items)

    def test_capabilities_carry_the_presets_of_offered_fields(self):
        document = capabilities(CONTEXT)
        self.assertEqual(document['presets'], presets.describe_presets(None))
        self.assertEqual(document['presets']['groups']['duration']['buckets']['short'][0]['key'], 'under_15s')
        denied = replace(CONTEXT, allowed_operations=frozenset({'video.observed_age.v1'}))
        self.assertEqual(set(capabilities(denied)['presets']['groups']), {'views', 'duration', 'likes', 'comments'})
        self.assertEqual(capabilities(CONTEXT, 'public.channels.current.v1')['presets'], {})

    def test_summary_uses_the_set_of_its_video_type(self):
        for kind, first in (('long', 'under_5m'), ('short', 'under_15s'), ('stream', 'under_30m')):
            operation = aggregate.validate_operation(request(summary.ID, query(video_types=[kind]), {}), CONTEXT)
            _, params = compile_operation(operation)
            durations = [b['key'] for b in params['buckets'].obj if b['metric'] == 'duration_seconds']
            self.assertEqual(durations[0], first)


class FacetTests(unittest.TestCase):
    def test_parameters_are_closed(self):
        bad = [{'population': {'kind': 'frozen', 'ref': 'x'}}, {'population': {'kind': 'query'}, 'groups': []},
               {'population': {'kind': 'query'}, 'groups': ['views', 'views']}, {'population': {'kind': 'query'}, 'groups': ['nope']},
               {'population': {'kind': 'query'}, 'other': 1},
               {'population': {'kind': 'query'}, 'groups': ['views'], 'conditions': {'duration': {'field': 'video.duration_seconds', 'op': 'gte', 'value': '1'}}},
               {'population': {'kind': 'query'}, 'conditions': {'views': {'field': 'video.likes', 'op': 'gte', 'value': '1'}}},
               {'population': {'kind': 'query'}, 'conditions': {'views': None}}]
        for parameters in bad:
            with self.subTest(parameters=parameters), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                aggregate.validate_operation(request(facets.ID, query(), parameters), CONTEXT)
        with self.assertRaises(QueryRejected) as caught:
            aggregate.validate_operation(facets_request(conditions={'views': {'field': 'video.views', 'op': 'has_any', 'value': ['1']}}), CONTEXT)
        self.assertEqual((caught.exception.code, caught.exception.path), ('unsupported_operator', 'parameters.conditions.views'))
        many = {'any': [{'field': 'video.views', 'op': 'eq', 'value': str(n)} for n in range(70)]}
        with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'):
            aggregate.validate_operation(facets_request(conditions={'views': many, 'likes': {'any': [
                {'field': 'video.likes', 'op': 'eq', 'value': str(n)} for n in range(70)]}}), CONTEXT)

    def test_dependencies_follow_requested_groups_and_conditions(self):
        operation = aggregate.validate_operation(facets_request(groups=['category', 'published'], conditions={
            'published': {'field': 'video.published_date', 'op': 'gte', 'value': '2026-01-01'}}), CONTEXT)
        self.assertTrue({'video.category_id', 'video.published_year', 'video.published_date'} <= set(operation.query.referenced_fields))
        self.assertFalse({'video.title_length', 'video.tag_count', 'video.likes'} & set(operation.query.referenced_fields))
        denied = replace(CONTEXT, allowed_operations=frozenset({facets.ID}))
        with self.assertRaisesRegex(QueryRejected, 'grant_required'):  # Title length needs the static grant.
            aggregate.validate_operation(facets_request(), denied)
        aggregate.validate_operation(facets_request(groups=['views', 'published', 'category']), denied)

    def test_sql_is_a_gateway_statement_with_parameters_only(self):
        conditions = {'views': {'any': [{'all': [{'field': 'video.views', 'op': 'gte', 'value': '1000'},
                                                 {'field': 'video.views', 'op': 'lt', 'value': '10000'}]}]},
                      'published': {'field': 'video.published_year', 'op': 'in', 'value': ['2025', '2026']},
                      'category': {'field': 'video.category_id', 'op': 'eq', 'value': "22'; SELECT 1"}}
        for kind in ('long', 'short', 'stream'):
            operation = aggregate.validate_operation(facets_request(query(select=['video.id'], video_types=[kind],
                time={'mode': 'current', 'timezone': 'Asia/Kathmandu'}), conditions=conditions), CONTEXT)
            sql, params = compile_operation(operation)
            assert_gateway_statement(self, sql)
            self.assertNotIn("22'", sql)
            self.assertNotIn('Asia/Kathmandu', sql)
            self.assertEqual(params['facet_time_zone'], 'Asia/Kathmandu')
            for name in re.findall(r'%\((\w+)\)s', sql):
                self.assertIn(name, params)
            duration = [b['key'] for b in params['facet_buckets'].obj if b['grp'] == 'duration']
            self.assertEqual(duration, [b.key for b in presets.preset_buckets('duration', kind)])
            # The shared data lifetime of a population (db/reads/research_operations/common.DATA_FINAL_SELECT) and
            # the shared reason of each row left out (research_sql raw.rejection).
            self.assertIn('coalesce(coverage.data_valid_before,coverage.valid_before)', sql)
            self.assertIn('FILTER(WHERE rejection=', sql)

    def test_growth_group_is_opt_in_and_gated_by_its_field(self):
        from research_query import growth
        self.assertNotIn('growth', facets.STANDARD)
        baseline = replace(CONTEXT, allowed_operations=frozenset(OPERATIONS) - {growth.OPERATION_ID})
        aggregate.validate_operation(facets_request(), baseline)
        with self.assertRaisesRegex(QueryRejected, 'grant_required'):
            aggregate.validate_operation(facets_request(groups=['views', 'growth']), baseline)
        conditions = {'growth': {'any': [{'field': growth.STATUS, 'op': 'eq', 'value': 'ready'},
                                         {'field': growth.GAINED, 'op': 'gte', 'value': '-5'}]}}
        operation = aggregate.validate_operation(facets_request(groups=['views', 'growth'], conditions=conditions), CONTEXT)
        sql, params = compile_operation(operation)
        assert_gateway_statement(self, sql)
        self.assertEqual(params['facet_values_growth'], list(growth.STATUSES))
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):  # A growth condition belongs to the growth group.
            aggregate.validate_operation(facets_request(conditions={'views': conditions['growth']}), CONTEXT)
        values = {'field': growth.STATUS, 'count': 3, 'unknown_count': 0,
                  'values': [{'value': status, 'count': int(status == 'ready') * 3} for status in growth.STATUSES]}
        result = self.result()
        result['groups'] = {'growth': values}
        self.assertEqual(facets.check(result)['groups']['growth'], values)
        for broken in ({**values, 'values': values['values'][::-1]}, {**values, 'count': 4}):
            result['groups'] = {'growth': broken}
            with self.assertRaises(ValueError):
                facets.check(result)

    def result(self):
        group = lambda name, counts: {'field': facets.GROUPS[name].field, 'count': sum(counts) + 1, 'unknown_count': 1,
            'buckets': [dict(b.as_dict(), count=c) for b, c in zip(presets.preset_buckets(name, 'long'), counts)]}
        return {'population': {'kind': 'query', 'count': 9, 'matched_count': 4, 'expired_counters': 1, 'expired_metadata': 0,
                               'excluded': dict.fromkeys(facets.EXCLUDED, 0)},
                'time_zone': 'UTC', 'bucket_registry_version': presets.PRESETS_VERSION,
                'groups': {'views': group('views', [1, 2, 0, 0, 0, 0]),
                           'published': {'field': 'video.published_year', 'count': 6, 'unknown_count': 1,
                                         'years': [{'year': 2026, 'count': 2}, {'year': 2025, 'count': 1}, {'year': 2023, 'count': 1}],
                                         'older': {'before_year': 2023, 'count': 1}},
                           'category': {'field': 'video.category_id', 'count': 4, 'unknown_count': 1,
                                        'categories': [{'id': '27', 'count': 1}, {'id': '10', 'count': 2}]}}}

    def test_check_shapes_labels_and_rejects_inconsistent_counts(self):
        checked = facets.check(self.result())
        self.assertEqual([(c['id'], c['label']) for c in checked['groups']['category']['categories']],
                         sorted([(c['id'], c['label']) for c in checked['groups']['category']['categories']], key=lambda x: (x[1], x[0])))
        self.assertTrue(all(c['label'] for c in checked['groups']['category']['categories']))
        broken = []
        for path, value in ((('groups', 'views', 'unknown_count'), 0), (('groups', 'published', 'years'), [{'year': 2025, 'count': 1}, {'year': 2026, 'count': 1}]),
                            (('groups', 'published', 'older'), {'before_year': 2020, 'count': 1}), (('groups', 'category', 'categories'), []),
                            (('population', 'matched_count'), 10), (('population', 'excluded'), {}), (('bucket_registry_version',), 99),
                            (('groups', 'nope'), {}), (('time_zone',), None)):
            result = self.result()
            target = result
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            broken.append(result)
        for result in broken:
            with self.subTest(result=result), self.assertRaises((ValueError, KeyError, TypeError)):
                facets.check(result)

    def test_lifetime_comes_from_the_data_clocks(self):
        # SQL gives the data lifetime as valid_before (no 120 s cap); Python keeps it.
        raw = facets_request(groups=['views'])
        result = self.result()
        result['groups'] = {'views': result['groups']['views']}
        later = '2026-11-20T00:00:00Z'
        output = QueryService(Repository(result, valid_before=later), lambda: CONTEXT, clock=lambda: NOW).execute_operation(raw)
        self.assertEqual(output['context']['valid_before'], later)

    def test_frozen_population_is_the_shared_reference_shape(self):
        # The Inspector's frozen reference ({kind, frozen_ref}); the unsupported shape {kind, token} is not accepted.
        for population in ({'kind': 'frozen', 'frozen_ref': 'abc'}, {'kind': 'query'}):
            aggregate.validate_operation(request(facets.ID, query(), {'population': population}), CONTEXT)
        for population in ({'kind': 'frozen', 'token': 'abc'}, {'kind': 'frozen'}, {'kind': 'frozen', 'frozen_ref': ''},
                           {'kind': 'frozen', 'frozen_ref': 'a b'}, None):
            with self.subTest(population=population), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                aggregate.validate_operation(request(facets.ID, query(), {'population': population}), CONTEXT)

    def test_excluded_reasons_are_the_shared_rejections(self):
        from db.reads.research_sql import REJECTION_REASONS, _REJECTION
        self.assertEqual(set(facets.EXCLUDED), set(REJECTION_REASONS))
        # A row a current check found not public has one reason, before any other.
        self.assertTrue(_REJECTION.startswith(",CASE WHEN p.availability_status IS DISTINCT FROM 'public' AND coalesce("))
        self.assertLess(_REJECTION.index("'source_unavailable'"), _REJECTION.index("'source_unpublished'"))


class FrozenFacetTests(unittest.TestCase):
    """Facets over the Library's frozen list through the operation bind hook."""
    BASE = {'field': 'video.title', 'op': 'contains_casefold_v1', 'value': 'guide'}
    CONDITIONS = {'views': {'all': [{'field': 'video.views', 'op': 'gte', 'value': '1000'},
                                    {'field': 'video.views', 'op': 'lt', 'value': '10000'}]},
                  'published': {'field': 'video.published_year', 'op': 'in', 'value': ['2025']}}
    GROUPS = ['views', 'published', 'category']
    COLUMNS = ['video.id', 'video.title', 'video.views', 'video.published_at', 'video.category_id']
    ZONE = {'mode': 'current', 'timezone': 'Asia/Tokyo'}

    def operation(self, where=BASE, conditions=CONDITIONS, groups=GROUPS, **changes):
        return aggregate.validate_operation(request(facets.ID, query(select=['video.id'], where=where, time=self.ZONE, **changes),
            {'population': {'kind': 'frozen', 'frozen_ref': 'abc'}, 'groups': groups, 'conditions': conditions}), CONTEXT)

    def handle(self, where, columns=COLUMNS, **changes):
        """A checked frozen reference whose list was read with `where` (the Library's list query)."""
        from datetime import timedelta
        from research_query.frozen import compact_row
        listed = validate(canonical(query(select=columns, where=where, time=self.ZONE, consistency='frozen', **changes)), CONTEXT)
        cell = lambda value, state='known': {'state': state, 'value': value, 'provenance_ref': 'x', 'observed_at': None,
                                             'expires_at': None, 'observation': None}
        values = [{'video.title': cell('Guide'), 'video.views': cell('5000'), 'video.published_at': cell('2024-12-31T15:00:00Z'),
                   'video.category_id': cell('22'), 'video.published_year': cell('2025')},
                  {'video.title': cell(None, 'expired'), 'video.views': cell(None, 'expired'),
                   'video.published_at': cell('2025-06-01T00:00:00Z'), 'video.category_id': cell(None, 'unknown'),
                   'video.published_year': cell('2025')}]
        rows = tuple(compact_row(columns, {'id': f'v000000000{index}', 'cells': {'video.id': cell(f'v000000000{index}'), **value}})
                     for index, value in enumerate(values))
        request_ = listed.request
        session = type('Session', (), dict(source_id=request_.source.id, video_type=request_.video_types[0],
            scope_kind=request_.scope.kind, scope_id=request_.scope.id, plan=listed, columns=tuple(columns), rows=rows,
            ids=tuple(json.loads(row)[0] for row in rows), session_id='facet-test',
            valid_before=NOW + timedelta(seconds=600)))()
        finished = []
        return type('Handle', (), dict(session=session, require_current=lambda self: finished.append(True), finished=finished))()

    def bind(self, operation, handle):
        with patch('research_query.frozen_service.frozen_reference', return_value=handle) as reference:
            binding = facets.bind(QueryService(None, lambda: CONTEXT), operation, lambda: False, 0)
        self.assertEqual(reference.call_args.args[1], 'abc')
        return binding

    def listed_where(self, order=(0, 1, 2, 3)):
        parts = [self.BASE, *self.CONDITIONS['views']['all'], self.CONDITIONS['published']]
        return {'all': [parts[index] for index in order]}

    def test_the_frozen_rows_and_their_values_bind_before_the_read(self):
        self.assertIs(facets.KIND.bind, facets.bind)
        query_mode = aggregate.validate_operation(facets_request(), CONTEXT)
        self.assertIsNone(facets.bind(object(), query_mode, lambda: False, 0))
        # The list's where is the base with every group condition, in any order (the translator's own order).
        handle = self.handle(self.listed_where((3, 1, 0, 2)))
        binding = self.bind(self.operation(), handle)
        self.assertEqual(binding.parameters['facet_frozen_ids'], ['v0000000000', 'v0000000001'])
        self.assertEqual(binding.parameters['facet_frozen_views'], ['5000', None])
        self.assertEqual(binding.parameters['facet_frozen_category'], ['22', None])
        # Not frozen as a year: the frozen instant, counted by the canonical local-year rule in SQL.
        self.assertTrue(binding.parameters['facet_frozen_instant'])
        self.assertEqual(binding.parameters['facet_frozen_published'], ['2024-12-31T15:00:00Z', '2025-06-01T00:00:00Z'])
        self.assertEqual((binding.parameters['facet_frozen_expired_counters'], binding.parameters['facet_frozen_expired_metadata']), (1, 1))
        self.assertEqual(binding.valid_before, handle.session.valid_before)
        binding.finish()
        self.assertEqual(handle.finished, [True])
        year = self.bind(self.operation(), self.handle(self.listed_where(), columns=[*self.COLUMNS, 'video.published_year']))
        self.assertEqual((year.parameters['facet_frozen_instant'], year.parameters['facet_frozen_published']), (False, ['2025', '2025']))

    def test_another_list_or_a_missing_field_is_refused(self):
        refusals = [
            (self.handle({'all': [self.BASE, *self.CONDITIONS['views']['all']]}), 'cursor_context_changed'),  # one condition fewer
            (self.handle(self.listed_where(), video_types=['short']), 'cursor_context_changed'),
            (self.handle(self.listed_where(), scope={'kind': 'channel', 'id': 'UC' + 'b' * 22}), 'cursor_context_changed'),
            (self.handle(self.listed_where(), coverage={'mode': 'require_complete'}), 'cursor_context_changed'),
            (self.handle(self.listed_where(), columns=['video.id', 'video.title', 'video.published_at', 'video.category_id']), 'invalid_query'),
        ]
        zoned = self.handle(self.listed_where())
        zoned.session.plan = validate(canonical(query(select=self.COLUMNS, where=self.listed_where(), consistency='frozen')), CONTEXT)
        refusals.append((zoned, 'cursor_context_changed'))  # Years in another zone are other rows.
        for handle, code in refusals:
            with self.subTest(code=code), self.assertRaisesRegex(QueryRejected, code):
                self.bind(self.operation(), handle)

    def test_frozen_sql_counts_listed_rows_everywhere_and_others_only_where_their_own_condition_fails(self):
        operation = self.operation()
        query_sql, _ = compile_operation(operation)
        frozen = replace(operation, binding=self.bind(operation, self.handle(self.listed_where())))
        sql, params = compile_operation(frozen)
        assert_gateway_statement(self, sql)
        for name in re.findall(r'%\((\w+)\)s', sql):
            self.assertIn(name, params)
        self.assertIn(' UNION ALL\n SELECT video_id,false AS member,', sql)
        self.assertIn('FROM population WHERE NOT (video_id=ANY(%(facet_frozen_ids)s::text[]))', sql)
        self.assertIn("('views',r.v_views,(member OR NOT f_views) AND f_published AND f_category)", sql)
        self.assertIn('WHERE (member OR NOT f_published) AND f_views AND f_category', sql)
        self.assertIn("'kind','frozen'", sql)
        self.assertIn("'matched_count',(SELECT count(*) FROM facet_rows WHERE member)", sql)
        self.assertIn('extract(year FROM (f.published_instant AT TIME ZONE %(query_time_zone)s::text))', sql)
        self.assertNotIn(' AS member', query_sql)
        self.assertEqual(params['query_time_zone'], 'Asia/Tokyo')
        result = FacetTests().result()
        result['population']['kind'] = 'frozen'
        self.assertEqual(facets.check(result)['population']['kind'], 'frozen')
        result['population']['kind'] = 'other'
        with self.assertRaises(ValueError):
            facets.check(result)


if __name__ == '__main__':
    unittest.main()
