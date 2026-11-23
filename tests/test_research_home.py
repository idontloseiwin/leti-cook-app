"""Home "Latest videos" on the shared backend: offline contract checks.

The library scope (every visible channel), the relative publication window
within_last_hours_v1, the per-row video.lifetime_views_per_day and the
operation video.view_pace_median.v1. No database: SQL shape and validation.
Native checks on PostgreSQL: tests/research_home_pg.py.
"""
from copy import deepcopy
from datetime import timedelta
from fractions import Fraction
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]

from research_query.aggregate import validate_operation
from research_query.operation_kinds import OPERATION_KINDS
from research_query.operation_kinds import view_pace_median as pace
from research_query.operations import OPERATIONS
from research_query.registry import FIELDS, SOURCE, CHANNEL_SOURCE
from research_query.saved import normalize_definition
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities, validate
from db.reads.research_sql import compile_query, compile_scope_recheck
from db.reads.research_aggregate import compile_operation
from test_research_operation_kinds import assert_gateway_statement

CONTEXT = AccessContext('installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
WINDOW = {'field': 'video.published_at', 'op': 'within_last_hours_v1', 'value': '168'}
SELECT = ['video.id', 'video.channel_id', 'video.title', 'video.thumbnail_url', 'video.published_at',
          'video.views', 'video.likes', 'video.comments', 'video.lifetime_views_per_day']


def home(**changes):
    value = dict(query_version=1, source={'id': 'public.videos.current.v1'}, scope={'kind': 'library'},
                 video_types=['long'], select=list(SELECT), where=deepcopy(WINDOW),
                 order_by=[{'field': 'video.published_at', 'direction': 'desc'}, {'field': 'video.id', 'direction': 'desc'}],
                 page={'size': 20})
    value.update(changes)
    return value


def operation(query, parameters=None):
    return canonical(dict(version=1, operation=pace.ID, query=query, parameters=parameters or {}))


class LibraryScopeTests(unittest.TestCase):
    def test_library_scope_has_no_identity_and_reads_every_visible_channel(self):
        plan = validate(canonical(home()), CONTEXT)
        self.assertEqual(plan.request.scope.kind, 'library')
        self.assertIsNone(plan.request.scope.id)
        self.assertEqual(plan.request.model_dump(mode='json')['scope'], {'kind': 'library'})
        statement, params = compile_query(plan)
        self.assertIsNone(params['scope_id'])
        self.assertIn('WHERE NOT c.is_hidden AND TRUE', statement)
        recheck, recheck_params = compile_scope_recheck(plan)
        self.assertIn('TRUE AS source_exists', recheck)
        self.assertEqual(recheck_params, {'scope_id': None})
        assert_gateway_statement(self, statement)
        assert_gateway_statement(self, recheck)

    def test_library_scope_is_offered_for_videos_and_channels(self):
        self.assertIn('library', SOURCE.scopes)
        self.assertIn('library', CHANNEL_SOURCE.scopes)
        self.assertIn('library', capabilities(CONTEXT)['scopes'])
        channels = dict(query_version=1, source={'id': 'public.channels.current.v1'}, scope={'kind': 'library'},
                        select=['channel.id'], order_by=[{'field': 'channel.id', 'direction': 'asc'}])
        statement, params = compile_query(validate(canonical(channels), CONTEXT))
        self.assertIn('WHERE NOT c.is_hidden AND TRUE', statement)
        assert_gateway_statement(self, statement)

    def test_library_scope_takes_no_identity_and_is_not_saved(self):
        for scope in ({'kind': 'library', 'id': 'UC' + '0' * 22}, {'kind': 'library', 'id': None}, {'kind': 'libraries'}):
            with self.subTest(scope=scope), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                validate(canonical(home(scope=scope)), CONTEXT)
        with self.assertRaisesRegex(ValueError, 'library_scope_not_saved'):
            normalize_definition({'version': 2, 'query': home(page={'size': 20})})

    def test_policy_is_known_and_ungated(self):
        import web.research as web_research
        self.assertEqual(web_research.POLICY_FLAGS['home_feed'], ())
        with patch.dict(os.environ, {name: 'false' for names in web_research.POLICY_FLAGS.values() for name in names}):
            self.assertIn(pace.ID, web_research.allowed_operations())


class WindowOperatorTests(unittest.TestCase):
    def test_hours_are_canonical_whole_numbers(self):
        for value in ('1', '24', '168', '672', '87600'):
            plan = validate(canonical(home(where={**WINDOW, 'value': value})), CONTEXT)
            self.assertEqual(plan.request.where.value, value)
        for value in ('0', '01', '87601', '1.5', '-1', '24h', 24, '', None, ['24']):
            with self.subTest(value=value), self.assertRaises(QueryRejected):
                validate(canonical(home(where={**WINDOW, 'value': value})), CONTEXT)

    def test_only_publication_time_offers_the_window(self):
        for field in ('video.scheduled_start_at', 'video.actual_end_at', 'video.views', 'video.title'):
            with self.subTest(field=field), self.assertRaises(QueryRejected):
                validate(canonical(home(where={**WINDOW, 'field': field})), CONTEXT)
        self.assertIn('within_last_hours_v1', FIELDS['video.published_at'].operators)

    def test_window_compares_with_the_evaluation_time(self):
        statement, params = compile_query(validate(canonical(home()), CONTEXT))
        self.assertIn('published_at >= (SELECT evaluated_at FROM args)-%(p', statement)
        self.assertIn('168 hours', params.values())

    def test_a_conjunctive_window_bounds_the_scan_and_any_window_its_lifetime(self):
        cases = [
            (WINDOW, '168 hours', ['168 hours']),
            ({'all': [WINDOW, {**WINDOW, 'value': '24'}, {'field': 'video.views', 'op': 'gte', 'value': '1'}]},
             '24 hours', ['24 hours', '168 hours']),
            # A window below "any" or "not" does not hold for every match: no scan bound.
            ({'any': [WINDOW, {'field': 'video.views', 'op': 'gte', 'value': '1'}]}, None, ['168 hours']),
            ({'not': WINDOW}, None, ['168 hours']),
            ({'field': 'video.views', 'op': 'gte', 'value': '1'}, None, []),
        ]
        for where, scan, edges in cases:
            with self.subTest(where=where):
                statement, params = compile_query(validate(canonical(home(where=where)), CONTEXT))
                self.assertEqual(params.get('window_scan'), scan)
                self.assertEqual(scan is not None, 'p.published_at>=a.evaluated_at-%(window_scan)s::interval' in statement)
                self.assertEqual(sorted(v for k, v in params.items() if k.startswith('window_edge')), sorted(edges))
                self.assertEqual(statement.count('min(published_at)+%(window_edge'), len(edges))
                assert_gateway_statement(self, statement)

    def test_frozen_and_operation_reads_keep_the_window(self):
        plan = validate(canonical(home(consistency='frozen', page={'size': 50})), CONTEXT)
        statement, params = compile_query(plan)
        self.assertEqual(params['window_scan'], '168 hours')
        assert_gateway_statement(self, statement)
        # They read the full scope: their own joins were planned for its row estimate.
        self.assertNotIn('readable.video_publication_times', statement)
        self.assertNotIn('readable.video_publication_times', compile_operation(validate_operation(operation(home()), CONTEXT))[0])

    def test_a_windowed_page_of_several_channels_reads_the_window_by_index(self):
        # A conjunctive window on a current page of several channels starts from the videos published
        # inside it (readable.video_publication_times), each looked up in the read model by its key;
        # the masked window condition on the raw rows stays. A channel keeps its channel index.
        from db.reads.research_sql import _WINDOW_FROM, _WINDOW_SCOPE
        folder = {'kind': 'folder', 'id': '77777777-7777-4777-8777-777777777777', 'include_descendants': False}
        for scope in ({'kind': 'library'}, folder):
            for where, windowed in ((WINDOW, True), ({'all': [WINDOW, {'field': 'video.views', 'op': 'gte', 'value': '1'}]}, True),
                                    ({'any': [WINDOW, {'field': 'video.views', 'op': 'gte', 'value': '1'}]}, False), (None, False)):
                with self.subTest(scope=scope['kind'], where=where):
                    statement, params = compile_query(validate(canonical(home(scope=scope, where=where)), CONTEXT))
                    self.assertEqual(_WINDOW_FROM in statement, windowed)
                    self.assertEqual(_WINDOW_SCOPE in statement, windowed)
                    self.assertEqual('FROM scope JOIN readable.video_search_projection p USING(channel_id)' in statement, not windowed)
                    if windowed:
                        self.assertIn('p.published_at>=a.evaluated_at-%(window_scan)s::interval' + _WINDOW_SCOPE, statement)
                    assert_gateway_statement(self, statement)
        self.assertEqual(_WINDOW_FROM, 'FROM readable.video_publication_times w\n JOIN LATERAL (SELECT p0.* FROM'
                         ' readable.video_search_projection p0 WHERE p0.video_id=w.video_id LIMIT 1) p ON true\n CROSS JOIN args a')
        # The scope's channels: the same set as the join, checked as a hashed set (IS TRUE keeps it a subplan).
        self.assertEqual(_WINDOW_SCOPE, ' AND w.published_at>=statement_timestamp()-%(window_scan)s::interval'
                                        ' AND (p.channel_id IN (SELECT channel_id FROM scope)) IS TRUE')
        statement, _ = compile_query(validate(canonical(home(scope={'kind': 'channel', 'id': 'UC' + 'A' * 22})), CONTEXT))
        self.assertNotIn('readable.video_publication_times', statement)
        self.assertIn('AND p.channel_id=%(scope_id)s::text', statement)


class PaceFieldTests(unittest.TestCase):
    def test_pace_columns_only_when_referenced(self):
        statement, _ = compile_query(validate(canonical(home()), CONTEXT))
        self.assertIn('AS lifetime_views_per_day_state', statement)
        self.assertIn("'lifetime_views_per_day',rtrim(rtrim(details.lifetime_views_per_day::text,'0'),'.')", statement)
        without, _ = compile_query(validate(canonical(home(select=SELECT[:-1])), CONTEXT))
        self.assertNotIn('lifetime_views_per_day', without)

    def test_pace_is_ungated_state_only_and_not_sortable(self):
        field = FIELDS['video.lifetime_views_per_day']
        self.assertEqual((field.value_type, field.sortable, field.origin), ('signed_decimal', False, 'calculated'))
        self.assertEqual(field.operators, ('is_known', 'is_unknown', 'state_is'))
        no_operations = AccessContext('installation', 'owner-1', enabled=True, allowed_operations=frozenset())
        validate(canonical(home()), no_operations)
        with self.assertRaises(QueryRejected):
            validate(canonical(home(order_by=[{'field': 'video.lifetime_views_per_day', 'direction': 'desc'}])), CONTEXT)

    def test_frozen_pace_expires_against_the_frozen_horizon(self):
        statement, _ = compile_query(validate(canonical(home(consistency='frozen', page={'size': 50})), CONTEXT))
        self.assertIn('checked_at<=fresh_until-%(window)s::interval', statement)


class PaceMedianOperationTests(unittest.TestCase):
    def test_registered_with_its_policy_and_population_parameter(self):
        self.assertIs(OPERATION_KINDS[pace.ID], pace.KIND)
        self.assertEqual(pace.DEFINITION.policy, 'home_feed')
        self.assertEqual(pace.dependencies({}, 'public.videos.current.v1', 'library'), ('video.lifetime_views_per_day',))
        self.assertEqual(pace.dependencies({'population': {'kind': 'query'}}, 'public.videos.current.v1', 'channel'),
                         ('video.lifetime_views_per_day',))
        for params in ({'population': {'kind': 'frozen', 'token': 'x'}}, {'window': '7d'}, {'population': 'query'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                pace.dependencies(params, 'public.videos.current.v1', 'library')
        with self.assertRaises(ValueError):
            pace.dependencies({}, 'public.channels.current.v1', 'library')

    def test_operation_reads_the_whole_window_population_with_data_lifetime(self):
        plan = validate_operation(operation(home()), CONTEXT)
        statement, params = compile_operation(plan)
        assert_gateway_statement(self, statement)
        self.assertIn('population AS MATERIALIZED (SELECT * FROM prepared WHERE (TRUE) AND (published_at >=', statement)
        self.assertIn('coalesce(coverage.data_valid_before,coverage.valid_before) AS valid_before', statement)
        self.assertNotIn('LIMIT %(limit)s', statement.partition('population AS MATERIALIZED')[2])
        self.assertEqual(params['pace_method'], pace.METHOD)
        denied = AccessContext('installation', 'owner-1', enabled=True, allowed_operations=frozenset())
        with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
            validate_operation(operation(home()), denied)

    def test_check_shapes_and_rounds_the_median(self):
        epoch = 'youtube.views.play_start.20260824'
        known = dict(state='known', epoch=epoch, excluded_count=0)
        base = dict(field='video.lifetime_views_per_day', method=pace.METHOD, population_count=5, known_count=3,
                    missing=dict(unknown=1, expired=1, unavailable=0), definition=known, median='12.3456785000000000')
        self.assertEqual(pace.check(base)['median'], '12.345678')
        self.assertEqual(pace.check({**base, 'median': '12.3456795'})['median'], '12.34568')
        self.assertEqual(pace.check({**base, 'median': '100.0000000000'})['median'], '100')
        self.assertEqual(pace.check({**base, 'median': '0.0000000'})['median'], '0')
        self.assertIsNone(pace.check({**base, 'known_count': 0, 'missing': dict(unknown=3, expired=1, unavailable=1),
                                      'median': None})['median'])
        # Values without a recorded definition are left out and counted; two definitions withhold the median.
        self.assertEqual(pace.check({**base, 'known_count': 2, 'definition': {**known, 'excluded_count': 1}})['definition'],
                         {**known, 'excluded_count': 1})
        for state in ('mixed_definition_epochs', 'unknown_definition_epoch'):
            withheld = pace.check({**base, 'known_count': 0, 'median': None, 'definition': dict(state=state, epoch=None, excluded_count=3)})
            self.assertEqual((withheld['median'], withheld['definition']['excluded_count']), (None, 3))
        for broken in ({**base, 'known_count': 4}, {**base, 'median': None}, {**base, 'median': '-1'},
                       {**base, 'median': 1.5}, {**base, 'missing': dict(unknown=1, expired=1)},
                       {**base, 'extra': 1}, {**base, 'field': 'video.views'}, {**base, 'population_count': -1},
                       {**base, 'known_count': 0, 'missing': dict(unknown=3, expired=1, unavailable=1)},
                       {key: value for key, value in base.items() if key != 'definition'},
                       {**base, 'definition': {**known, 'excluded_count': 1}},
                       {**base, 'definition': {**known, 'state': 'mixed_definition_epochs', 'epoch': None}},
                       {**base, 'definition': {**known, 'epoch': None}},
                       {**base, 'definition': {**known, 'state': 'other'}},
                       {**base, 'definition': {**known, 'extra': 1}},
                       {**base, 'known_count': 0, 'median': None, 'definition': dict(state='no_known_values', epoch=epoch, excluded_count=3)}):
            with self.subTest(broken=broken), self.assertRaises((ValueError, TypeError, KeyError)):
                pace.check(broken)

    def test_the_median_follows_the_d27_views_definition_rule(self):
        # The shared population_stats rule, not a copy (one owner with Overview and the baseline).
        from db.reads.research_operations.population_stats import view_definition
        statement, _ = compile_operation(validate_operation(operation(home()), CONTEXT))
        self.assertIn(view_definition(), statement)
        self.assertIn("SELECT value FROM pace_known WHERE epoch IS NOT NULL AND (SELECT state FROM view_definition)='known'", statement)


def oracle_pace(views, published, checked):
    """The per-row value as SQL defines it, in exact Python arithmetic."""
    age = max((checked - published) // timedelta(microseconds=1), 3_600_000_000)
    return Fraction(views * 86_400 * 1_000_000, age)


def half_even_text(value):
    scaled = value * 1_000_000
    whole, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest > scaled.denominator or (2 * rest == scaled.denominator and whole % 2):
        whole += 1
    text = f'{whole // 1_000_000}.{whole % 1_000_000:06d}'.rstrip('0').rstrip('.')
    return text


class PaceOracleTests(unittest.TestCase):
    def test_half_even_oracle(self):
        self.assertEqual(half_even_text(Fraction(1, 8_000_000)), '0')  # 0.000000125 -> 0.000000
        self.assertEqual(half_even_text(Fraction(3, 2_000_000)), '0.000002')  # 0.0000015 -> even 2
        self.assertEqual(half_even_text(Fraction(5, 2_000_000)), '0.000002')  # 0.0000025 -> even 2
        self.assertEqual(half_even_text(Fraction(24)), '24')


class ReadEnvelopeTests(unittest.TestCase):
    """Web/middleware.py adds source_read_at / source_expires_at to a JSON object reply whose
    read registered a deadline. research_query/http.py registers context.valid_before for every
    successful execute (queries, frozen ids, operations, graphs); the Home adapter reads both keys."""

    def client(self, result, error=None):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from research_query.http import create_router
        from web.middleware import install_runtime_middleware

        class Service:
            clock = staticmethod(lambda: datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))
            context_provider = staticmethod(lambda: CONTEXT)

            def execute_operation(self, raw, cancelled):
                if error is not None:
                    raise error
                return deepcopy(result)
            execute = execute_operation

        app = FastAPI()
        app.include_router(create_router(lambda request: Service(), lambda: None))
        with patch('web.middleware.selected_deployment_profile', return_value='operator'):
            install_runtime_middleware(app, core=SimpleNamespace())
        return TestClient(app)

    def test_successful_research_replies_carry_the_read_envelope(self):
        result = {'version': 1, 'context': {'evaluated_at': '2026-10-29T12:00:00Z', 'valid_before': '2099-10-29T12:40:00.5Z'},
                  'result': {'median': '1'}}
        client = self.client(result)
        for path in ('/research/operations/execute', '/research/queries/execute'):
            with self.subTest(path=path):
                reply = client.post(path, content=b'{}', headers={'Content-Type': 'application/json'})
                body = reply.json()
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(set(body) - set(result), {'source_read_at', 'source_expires_at'})
                self.assertEqual(body['source_expires_at'], '2099-10-29T12:40:00.500000Z')
                self.assertEqual(reply.headers['X-Source-Expires-At'], body['source_expires_at'])
        # A refused read registers no deadline: no envelope keys in the body.
        refused = self.client(result, QueryRejected('policy_denied')).post('/research/operations/execute', content=b'{}',
                                                                          headers={'Content-Type': 'application/json'})
        self.assertEqual(refused.status_code, 403)
        self.assertFalse({'source_read_at', 'source_expires_at'} & set(refused.json()))


if __name__ == '__main__':
    unittest.main()
