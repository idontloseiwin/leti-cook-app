"""Boundary failures must discard the complete page, without leaking input."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
sys.path.insert(0, str(ROOT/'tests'))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_research_query_contract import query
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities
from research_query.service import QueryService
from research_query.http import create_router
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import DatabaseUnavailable, DatabaseServerError

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
CONTEXT = AccessContext('fixture', 'owner-v1', enabled=True)


def page():
    return {'evaluated_at': NOW, 'valid_before': NOW+timedelta(seconds=120),
            'channels_in_scope': 1, 'channels_complete': 1, 'rejected_rows': 0,
            'required_missing': 0, 'unresolved_collection_members': 0, 'scope_revision': 'a'*64,
            'rows': [{'video_id': 'a0000000001', 'channel_id': 'UC'+'a'*22, 'video_type': 'long',
                      'identity_clock': NOW-timedelta(hours=1), 'identity_expiry': NOW+timedelta(hours=695),
                      'metadata_checked_at': NOW-timedelta(hours=1), 'checked_at': NOW-timedelta(hours=1),
                      'title': 'private fixture text', 'title_state': 'known',
                      'views': 9007199254740993, 'views_state': 'known', 'sort_keys': ['a0000000001']}]}


class Repository:
    def __init__(self):
        self.data, self.reads, self.rechecks = page(), 0, 0
        self.on_read, self.on_recheck = lambda: None, lambda: None

    def read_page(self, *args, **kwargs):
        self.reads += 1
        self.on_read()
        return deepcopy(self.data)

    def recheck_scope(self, *args, **kwargs):
        self.rechecks += 1
        self.on_recheck()


class ServiceTests(unittest.TestCase):
    def test_related_clocks_and_provenance_have_no_invented_rule(self):
        self.request['select'] = ['channel.title','channel.country_code','video.actual_end_at','video.type']
        source = self.repo.data['rows'][0]
        source.update(channel_title='Name', channel_title_state='known', channel_country_code='UA', channel_country_code_state='known',
                      channel_metadata_checked_at=NOW-timedelta(hours=3),country_collected_at=NOW-timedelta(hours=7),
                      actual_end_at=NOW-timedelta(hours=2), actual_end_at_state='known')
        for method in ('unknown','typed_playlist_membership_v1','manual_decision_v1','automatic_long_short_v1'):
            source.update(type_method=method, type_threshold=180,type_rule_version=1)
            result = self.execute()
            cells = result['rows'][0]['cells']
            self.assertNotEqual(cells['channel.title']['observed_at'], cells['channel.country_code']['observed_at'])
            self.assertEqual(cells['video.actual_end_at']['value'], '2026-10-24T22:00:00Z')
            proof = result['provenance'][cells['video.type']['provenance_ref']]
            self.assertEqual(proof['method'], method)
            self.assertEqual(proof['type_rule'] is not None, method=='automatic_long_short_v1')
        source['type_threshold'] = '180'
        with self.assertRaisesRegex(QueryRejected,'backend_unavailable'): self.execute()

    def test_type_rule_description_matches_canonical_sql_and_schema(self):
        from db.inventory.automatic_type_policy import AUTOMATIC_TYPE_CANDIDATES_SQL, AUTOMATIC_TYPE_MAX_SHORT_SECONDS, AUTOMATIC_TYPE_RULE_VERSION
        clause = f'duration_value.seconds <= {AUTOMATIC_TYPE_MAX_SHORT_SECONDS}'
        self.assertIn(clause, AUTOMATIC_TYPE_CANDIDATES_SQL)
        self.assertIn(clause, (ROOT/'schema/schema.sql').read_text(encoding='utf-8'))
        self.assertEqual(AUTOMATIC_TYPE_RULE_VERSION, 1)

    def test_collection_uses_member_coverage_without_inventing_completed_channels(self):
        self.repo.data['channels_complete'] = 0
        self.request.update(scope={'kind':'collection','id':'11111111-1111-4111-8111-111111111111'},
                            coverage={'mode':'require_complete'})
        coverage = self.execute()['coverage']
        self.assertEqual(coverage['basis'], 'collection_members')
        self.assertEqual(coverage['status'], 'complete')
        self.assertEqual((coverage['channels_in_scope'], coverage['channels_complete']), (1,0))
        for key in ('unresolved_collection_members','required_missing','rejected_rows'):
            self.repo.data[key] = 1
            with self.assertRaisesRegex(QueryRejected, 'incomplete_source'): self.execute()
            self.repo.data[key] = 0
        for scope in ({'kind':'channel','id':'UC'+'a'*22},
                      {'kind':'folder','id':'11111111-1111-4111-8111-111111111111'},
                      {'kind':'label','id':'monetized'}):
            self.request['scope'] = scope
            with self.assertRaisesRegex(QueryRejected, 'incomplete_source'): self.execute()

    def test_extra_fields_keep_wire_types_and_separate_clocks(self):
        self.request['select'] = ['video.likes', 'video.tags', 'video.caption_available']
        source = self.repo.data['rows'][0]
        source.update(likes=0, likes_state='known', tags=[], tags_state='known',
                      caption_available=False, caption_available_state='known')
        source['metadata_checked_at'] = NOW-timedelta(hours=2)
        cells = self.execute()['rows'][0]['cells']
        self.assertEqual(cells['video.likes']['value'], '0')
        self.assertEqual(cells['video.tags']['value'], [])
        self.assertIs(cells['video.caption_available']['value'], False)
        self.assertNotEqual(cells['video.tags']['observed_at'], cells['video.likes']['observed_at'])
        source.update(tags=['must not leak'], tags_state='expired')
        self.assertIsNone(self.execute()['rows'][0]['cells']['video.tags']['value'])
        for value in ('false', 0, 1, []):
            source['caption_available'] = value
            with self.assertRaisesRegex(QueryRejected, 'backend_unavailable'): self.execute()

    def test_large_array_page_respects_response_budget(self):
        self.request.update(select=['video.tags', 'video.topic_urls'], page={'size':50})
        source = self.repo.data['rows'][0]
        source.update(tags=['x'*25600], tags_state='known', topic_urls=['x'*25600], topic_urls_state='known')
        self.repo.data['rows'] = [{**source, 'video_id':f'a{i:010d}'} for i in range(50)]
        with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'): self.execute()

    def setUp(self):
        self.repo, self.now, self.context = Repository(), NOW, CONTEXT
        self.service = QueryService(self.repo, lambda: self.context, clock=lambda: self.now)
        self.request = query(select=['video.id', 'video.title', 'video.views'])

    def execute(self, **kwargs):
        return self.service.execute(canonical(self.request), **kwargs)

    def test_missing_collection_members_are_partial_and_strict_mode_refuses(self):
        self.request['scope'] = {'kind':'collection', 'id':'11111111-1111-4111-8111-111111111111'}
        self.repo.data.update(rows=[], channels_in_scope=0, channels_complete=0, unresolved_collection_members=2)
        response = self.execute()
        self.assertEqual(response['coverage']['status'], 'partial')
        self.assertEqual(response['coverage']['reasons'], {'collection_members_unavailable':2})
        self.request['coverage']['mode'] = 'require_complete'
        with self.assertRaisesRegex(QueryRejected, 'incomplete_source'):
            self.execute()
        self.request['coverage']['mode'] = 'available'
        for invalid in (None, -1, True, '2'):
            self.repo.data['unresolved_collection_members'] = invalid
            with self.assertRaisesRegex(QueryRejected, 'backend_unavailable'):
                self.execute()

    def test_permission_and_malformed_input_never_reach_repository(self):
        self.context = replace(CONTEXT, enabled=False)
        with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
            self.service.execute(b'private invalid input')
        self.context = CONTEXT
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            self.service.execute(b'private invalid input')
        self.assertEqual(self.repo.reads, 0)

    def test_projection_clocks_integer_precision_and_no_internal_payload(self):
        output = self.execute()
        self.assertEqual(output['rows'][0]['cells']['video.views']['value'], '9007199254740993')
        self.assertEqual(set(output['rows'][0]['cells']), set(self.request['select']))
        self.assertNotIn('sort_keys', str(output))
        self.assertNotIn('scope_revision', str(output))
        self.assertEqual(self.repo.rechecks, 1)

    def test_source_expiry_after_read_discards_page(self):
        self.repo.on_read = lambda: setattr(self, 'now', NOW+timedelta(seconds=121))
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'): self.execute()

    def test_context_revocation_during_final_scope_read_discards_page(self):
        self.repo.on_recheck = lambda: setattr(self, 'context', replace(CONTEXT, enabled=False))
        with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'): self.execute()

    def test_scope_race_is_not_retried_or_returned(self):
        def moved(): raise QueryRejected('cursor_context_changed')
        self.repo.on_recheck = moved
        with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'): self.execute()
        self.assertEqual(self.repo.reads, 1)

    def test_strict_coverage_is_independent_of_returned_rows(self):
        self.repo.data.update(rows=[], rejected_rows=1)
        self.assertEqual(self.execute()['coverage']['status'], 'partial')
        self.request['coverage'] = {'mode': 'require_complete'}
        with self.assertRaisesRegex(QueryRejected, 'incomplete_source'): self.execute()

    def test_bad_provider_shape_and_payload_budget_fail_closed(self):
        with patch('research_query.service.MAX_RESPONSE_BYTES', 100):
            with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'): self.execute()
        self.repo.data['rows'][0]['views_state'] = 'invented'
        with self.assertRaisesRegex(QueryRejected, 'backend_unavailable') as caught: self.execute()
        self.assertNotIn('private fixture', str(caught.exception.payload()))

    def test_cancellation_after_read_never_returns_rows(self):
        stopped = []
        self.repo.on_recheck = lambda: stopped.append(True)
        with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded') as caught:
            self.execute(cancelled=lambda: bool(stopped))
        self.assertIsNone(caught.exception.limit, 'a client that left is not a time limit')

    def test_continuation_keeps_original_deadline(self):
        self.request['page'] = {'size': 1}
        self.repo.data['rows'].append({**self.repo.data['rows'][0], 'video_id': 'a0000000002'})
        first = self.execute()
        self.now += timedelta(seconds=10)
        self.repo.data['evaluated_at'] += timedelta(seconds=10)
        self.repo.data['valid_before'] += timedelta(seconds=10)
        self.request['page']['after'] = first['page']['next_cursor']
        self.assertEqual(self.execute()['context']['valid_before'], first['context']['valid_before'])

    def test_repository_safe_error_mapping_and_pre_acquisition_cancellation(self):
        class FailedDatabase:
            calls = 0
            error = DatabaseUnavailable('hidden detail', reason='pool_queue_full')
            @contextmanager
            def transaction(self, operation):
                self.calls += 1
                raise self.error
                yield
        database = FailedDatabase()
        repo = ResearchQueryRepository(database)
        # A statement or lock timeout names the time limit; SQLSTATE 53400 (a server setting
        # such as temp_file_limit) names the memory limit.
        for error, code, limit in [(database.error, 'query_busy', None),
                                   (DatabaseServerError('hidden SQL detail', code='57014'), 'query_budget_exceeded', 'time'),
                                   (DatabaseServerError('hidden SQL detail', code='55P03'), 'query_budget_exceeded', 'time'),
                                   (DatabaseServerError('hidden SQL detail', code='53400'), 'query_budget_exceeded', 'memory'),
                                   (DatabaseServerError('hidden SQL detail', code='42501'), 'backend_unavailable', None)]:
            database.error = error
            with self.assertRaisesRegex(QueryRejected, code) as caught:
                repo._read('SELECT 1', {}, deadline=time.monotonic()+2, cancelled=lambda: False, name='query_test')
            self.assertNotIn('hidden', str(caught.exception.payload()))
            self.assertEqual(caught.exception.payload().get('limit'), limit, error)
        # A passed deadline names the time limit; a client that left is not called a time limit.
        for cancelled, deadline, expected in (
                (lambda: True, time.monotonic()+2, {'code': 'query_budget_exceeded', 'path': None}),
                (lambda: False, time.monotonic()-1, {'code': 'query_budget_exceeded', 'path': None, 'limit': 'time'}),
                (lambda: True, time.monotonic()-1, {'code': 'query_budget_exceeded', 'path': None, 'limit': 'time'})):
            with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded') as caught:
                repo._read('SELECT 1', {}, deadline=deadline, cancelled=cancelled, name='query_test')
            self.assertEqual(caught.exception.payload(), expected)
        self.assertEqual(database.calls, 5)

    def test_repository_reads_are_read_only_snapshots_without_jit(self):
        class RecordingDatabase:
            operations = []
            @contextmanager
            def transaction(self, operation):
                self.operations.append(operation)
                raise DatabaseUnavailable('closed', reason='pool_queue_full')
                yield
        database = RecordingDatabase()
        with self.assertRaises(QueryRejected):
            ResearchQueryRepository(database)._read('SELECT 1', {}, deadline=time.monotonic()+2,
                                                    cancelled=lambda: False, name='query_test')
        operation = database.operations[0]
        self.assertTrue(operation.read_only)
        self.assertTrue(operation.repeatable_read)
        self.assertTrue(operation.disable_jit)


def list_row(index):
    """The fixture row as video `index` (the test lists them in index order)."""
    row = deepcopy(page()['rows'][0])
    row.update(video_id='a%010d' % index, views=index, sort_keys=[str(index), 'a%010d' % index])
    return row


def joined(indices):
    return ''.join('a%010d' % index for index in indices)


class OrderedListTests(unittest.TestCase):
    """page.order = "ids": the complete ordered ID list read with its first page; page.ids: its slices."""
    CHANNEL = {'kind': 'channel', 'id': 'UC' + 'a' * 22}

    def setUp(self):
        self.repo = Repository()
        self.service = QueryService(self.repo, lambda: CONTEXT, clock=lambda: NOW)
        self.request = query(scope=self.CHANNEL, select=['video.id', 'video.title', 'video.views'])

    def execute(self, page, rows, total=None, ids=None):
        self.repo.data['rows'] = rows
        if total is not None:
            self.repo.data.update(order_total=total, order_ids=ids)
        self.request['page'] = page
        return self.service.execute(canonical(self.request))

    def refused(self, *args, code='backend_unavailable'):
        with self.assertRaisesRegex(QueryRejected, code):
            self.execute(*args)

    def test_the_list_page_returns_every_id_in_the_page_order(self):
        listed = {'size': 3, 'growth_lifetime': 'page', 'order': 'ids'}
        result = self.execute(listed, [list_row(i) for i in range(4)], 10, joined(range(10)))
        self.assertEqual([row['id'] for row in result['rows']], ['a%010d' % i for i in range(3)])
        self.assertEqual(result['page']['order'], {'total_count': 10, 'ids': joined(range(10)), 'limit': 200_000})
        self.assertTrue(result['page']['has_more'])
        # At an anchor the page is the list's slice from that row; the list stays whole.
        anchored = {**listed, 'anchor_id': 'a0000000005'}
        result = self.execute(anchored, [list_row(i) for i in range(5, 9)], 10, joined(range(10)))
        self.assertEqual(result['page']['order']['ids'], joined(range(10)))
        self.assertEqual(self.execute({**listed, 'anchor_id': 'a0000000007'}, [list_row(i) for i in range(7, 10)], 10,
                                      joined(range(10)))['page']['has_more'], False)
        # An empty list; a list past the limit keeps its count and withholds its IDs.
        self.assertEqual(self.execute(listed, [], 0, '')['page']['order'], {'total_count': 0, 'ids': '', 'limit': 200_000})
        past = self.execute(listed, [list_row(i) for i in range(4)], 200_001, None)['page']['order']
        self.assertEqual(past, {'total_count': 200_001, 'ids': None, 'limit': 200_000})
        # The page is not served unless it is exactly the list's slice at its first row.
        self.refused(listed, [list_row(i) for i in range(1, 5)], 10, joined(range(10)))        # not from the start
        self.refused(anchored, [list_row(i) for i in range(6, 10)], 10, joined(range(10)))     # not from the anchor
        self.refused(anchored, [list_row(i) for i in range(5, 8)], 10, joined(range(10)))      # has_more disagrees
        self.refused(listed, [list_row(i) for i in (0, 2, 1, 3)], 10, joined(range(10)))       # another order
        self.refused(listed, [list_row(i) for i in range(4)], 10, joined([*range(9), 0]))      # an ID twice
        self.refused(listed, [list_row(i) for i in range(4)], 10, 'b' + joined(range(10))[1:])  # misaligned
        self.refused(listed, [list_row(i) for i in range(4)], 11, joined(range(10)))           # count disagrees
        self.refused(listed, [list_row(i) for i in range(4)], 10, None)                        # withheld within the limit
        self.refused(listed, [], 1, joined([0]))                                               # empty page of a list
        self.refused(listed, [list_row(i) for i in range(4)], 10, joined(range(10)).replace('a', '!', 1))
        for total in (-1, True, '10'):
            self.refused(listed, [list_row(i) for i in range(4)], total, joined(range(10)))

    def test_slices_return_requested_videos_in_the_requested_order(self):
        wanted = ['a%010d' % i for i in (7, 3, 5)]
        page = {'size': 3, 'growth_lifetime': 'page', 'ids': wanted}
        result = self.execute(page, [list_row(7), list_row(5)])
        self.assertEqual([row['id'] for row in result['rows']], [wanted[0], wanted[2]], 'a video that left is absent')
        self.assertEqual(result['page'], {'has_more': False, 'next_cursor': None})
        self.refused(page, [list_row(5), list_row(7)])
        self.refused(page, [list_row(7), list_row(4)])

    def test_list_options_keep_the_query_identity_and_its_text(self):
        plain = {'size': 3, 'growth_lifetime': 'page'}
        plans = [self.service.validate(canonical({**self.request, 'page': value})) for value in (
            plain, {**plain, 'order': 'ids'}, {**plain, 'order': 'ids', 'anchor_id': 'a0000000001'},
            {**plain, 'ids': ['a0000000001']})]
        self.assertEqual(len({plan.fingerprint for plan in plans}), 1, 'a slice continues the list of its first page')
        self.assertNotIn(b'"order"', plans[0].encoded)
        self.assertNotIn(b'"ids"', plans[0].encoded)
        # The list page's keyset cursor is a cursor of the same query.
        cursor = self.execute({**plain, 'order': 'ids'}, [list_row(i) for i in range(4)], 10, joined(range(10)))['page']['next_cursor']
        self.repo.data.pop('order_total'), self.repo.data.pop('order_ids')
        self.assertEqual(len(self.execute({'size': 3, 'growth_lifetime': 'page', 'after': cursor}, [list_row(i) for i in range(3, 6)])['rows']), 3)

    def test_list_options_need_a_current_channel_page(self):
        for request in (
                query(page={'size': 3, 'order': 'ids'}),                                  # folder scope
                query(scope=self.CHANNEL, consistency='frozen', page={'size': 3, 'order': 'ids'}),
                query(scope=self.CHANNEL, page={'size': 3, 'order': 'ids', 'after': 'abc'}),
                query(scope=self.CHANNEL, page={'size': 3, 'order': 'ids', 'ids': ['a0000000001']}),
                query(scope=self.CHANNEL, page={'size': 3, 'growth_lifetime': 'page', 'anchor_id': 'a0000000001', 'ids': ['a0000000001']}),
                query(scope=self.CHANNEL, page={'size': 1, 'ids': ['a0000000001', 'a0000000002']}),
                query(scope=self.CHANNEL, page={'size': 3, 'ids': ['a0000000001', 'a0000000001']}),
                query(scope=self.CHANNEL, page={'size': 3, 'ids': []}),
                query(scope=self.CHANNEL, page={'size': 3, 'ids': ['UC' + 'a' * 22]}),
                query(scope=self.CHANNEL, page={'size': 3, 'order': 'all'})):
            with self.subTest(page=request['page'], scope=request['scope']['kind']), self.assertRaisesRegex(QueryRejected, 'invalid_query'):
                self.service.validate(canonical(request))

    def test_the_whole_list_has_its_own_response_budget(self):
        from research_query.contracts import MAX_ORDER_IDS, MAX_RESPONSE_BYTES
        listed = {'size': 3, 'growth_lifetime': 'page', 'order': 'ids'}
        result = self.execute(listed, [list_row(i) for i in range(4)], MAX_ORDER_IDS, joined(range(MAX_ORDER_IDS)))
        self.assertEqual(len(result['page']['order']['ids']), MAX_ORDER_IDS * 11)
        self.assertGreater(len(canonical(result)), MAX_RESPONSE_BYTES)

    def test_the_list_uses_the_page_order_for_every_sort_key(self):
        from db.reads.research_sql import compile_query
        from research_query.validation import validate
        page_order = re.compile(r"\) ORDER BY (.+?) LIMIT %\(limit\)s\)")
        list_order = re.compile(r"string_agg\(video_id,'' ORDER BY (.+?)\),''\) END AS order_ids")
        for field in ('video.views', 'video.likes', 'video.comments', 'video.published_at', 'video.duration_seconds', 'video.title'):
            for direction in ('asc', 'desc'):
                with self.subTest(field=field, direction=direction):
                    request = {**self.request, 'select': ['video.id', field] if field != 'video.id' else ['video.id'],
                               'order_by': [{'field': field, 'direction': direction}]}
                    plain, _ = compile_query(validate(canonical({**request, 'page': {'size': 50, 'growth_lifetime': 'page'}}), CONTEXT))
                    listed, params = compile_query(validate(canonical({**request, 'page': {'size': 50, 'growth_lifetime': 'page', 'order': 'ids'}}), CONTEXT))
                    ordering = page_order.search(listed)[1]
                    self.assertEqual(list_order.search(listed)[1], ordering, 'one ORDER BY for the page and its list')
                    self.assertEqual(ordering.count('NULLS LAST'), 2, 'unknown values last; the video ID decides ties')
                    self.assertTrue(ordering.endswith('video_id COLLATE "C" ASC NULLS LAST'))
                    self.assertEqual(params['order_limit'], 200_000)
                    self.assertNotIn('matched_order', plain)
                    join = re.search(r" CROSS JOIN \(SELECT count\(\*\) AS order_total[\s\S]+? matched_order", listed)[0]
                    self.assertEqual(listed.replace(join, '').replace(',matched_order.order_total,matched_order.order_ids', ''), plain,
                                     'a page without the list keeps exactly its own text')
                    sliced, params = compile_query(validate(canonical({**request, 'page': {'size': 50, 'growth_lifetime': 'page',
                                                                                             'ids': ['a0000000002', 'a0000000001']}}), CONTEXT))
                    self.assertEqual(params['page_ids'], ['a0000000002', 'a0000000001'])
                    self.assertIn("AND (video_id=ANY(%(page_ids)s::text[])) ORDER BY array_position(%(page_ids)s::text[],video_id) LIMIT", sliced)
                    self.assertIn('ORDER BY array_position(%(page_ids)s::text[],video_id)) FROM details', sliced)
                    self.assertNotIn('matched_order', sliced)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.authorized = False
        self.repo = Repository()
        self.service = QueryService(self.repo, lambda: CONTEXT, clock=lambda: NOW)
        def owner():
            if not self.authorized: raise HTTPException(status_code=401)
        self.app = FastAPI()
        self.app.include_router(create_router(lambda request: self.service, owner))
        self.client = TestClient(self.app)

    def tearDown(self): self.client.close()

    def test_owner_boundary_is_required_for_all_routes(self):
        for method, url in [('get', '/research/capabilities'), ('post', '/research/queries/validate'),
                            ('post', '/research/queries/execute')]:
            self.assertEqual(getattr(self.client, method)(url).status_code, 401)
        self.assertEqual(self.repo.reads, 0)

    def test_validate_capabilities_and_execute_share_registry_and_no_store(self):
        self.authorized = True
        q = query(select=['video.id', 'video.title', 'video.views'])
        response = self.client.get('/research/capabilities')
        self.assertEqual(response.json(), capabilities(CONTEXT))
        response = self.client.post('/research/queries/validate', json=q)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.repo.reads, 0)
        response = self.client.post('/research/queries/execute', json=q)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json()['columns'], q['select'])

    def test_oversized_or_invalid_body_has_fixed_error_no_echo(self):
        self.authorized = True
        for raw, body in [(b'x'*65537, {'code': 'query_budget_exceeded', 'path': None, 'limit': 'request'}),
                          (b'private marker', {'code': 'invalid_query', 'path': None})]:
            response = self.client.post('/research/queries/execute', content=raw)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), body)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.repo.reads, 0)


class DisconnectTests(unittest.TestCase):
    """A client that goes away stops a running read, also behind the runtime's @app.middleware wrapper."""

    def test_a_dropped_connection_sets_the_stop_flag_of_the_running_read(self):
        import asyncio
        import json
        import threading
        import time
        running, seen = threading.Event(), []

        class Slow:
            def execute(self, raw, *, cancelled):
                running.set()
                started = time.monotonic()
                while time.monotonic() - started < 3:
                    if cancelled():
                        seen.append(round(time.monotonic() - started, 2))
                        raise QueryRejected('query_busy')
                    time.sleep(.01)
                seen.append(None)
                raise QueryRejected('query_busy')
        app = FastAPI()

        @app.middleware('http')         # the same wrapper kind as web.middleware.install_runtime_middleware
        async def passthrough(request, call_next):
            return await call_next(request)
        app.include_router(create_router(lambda request: Slow(), lambda: None))
        raw = json.dumps({'query_version': 1}).encode()
        messages, sent = [{'type': 'http.request', 'body': raw, 'more_body': False}], []

        async def receive():
            # Like uvicorn: after the body, receive() waits until the client is gone, then answers at once.
            if messages:
                return messages.pop(0)
            while not running.is_set():
                await asyncio.sleep(.01)
            return {'type': 'http.disconnect'}

        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                 'path': '/research/queries/execute', 'raw_path': b'/research/queries/execute', 'query_string': b'',
                 'root_path': '', 'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 8002),
                 'headers': [(b'host', b'127.0.0.1:8002'), (b'content-type', b'application/json'),
                             (b'content-length', str(len(raw)).encode())]}
        asyncio.run(app(scope, receive, send))
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertLess(seen[0], 1.5)


if __name__ == '__main__': unittest.main()
