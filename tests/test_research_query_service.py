"""Boundary failures must discard the complete page, without leaking input."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
            'required_missing': 0, 'scope_revision': 'a'*64,
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
    def setUp(self):
        self.repo, self.now, self.context = Repository(), NOW, CONTEXT
        self.service = QueryService(self.repo, lambda: self.context, clock=lambda: self.now)
        self.request = query(select=['video.id', 'video.title', 'video.views'])

    def execute(self, **kwargs):
        return self.service.execute(canonical(self.request), **kwargs)

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
        with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'):
            self.execute(cancelled=lambda: bool(stopped))

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
        for error, code in [(database.error, 'query_busy'),
                            (DatabaseServerError('hidden SQL detail', code='57014'), 'query_budget_exceeded'),
                            (DatabaseServerError('hidden SQL detail', code='42501'), 'backend_unavailable')]:
            database.error = error
            with self.assertRaisesRegex(QueryRejected, code) as caught:
                repo._read('SELECT 1', {}, deadline=time.monotonic()+2, cancelled=lambda: False, name='query_test')
            self.assertNotIn('hidden', str(caught.exception.payload()))
        with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'):
            repo._read('SELECT 1', {}, deadline=time.monotonic()+2, cancelled=lambda: True, name='query_test')
        self.assertEqual(database.calls, 3)

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
        for raw, code in [(b'x'*65537, 'query_budget_exceeded'), (b'private marker', 'invalid_query')]:
            response = self.client.post('/research/queries/execute', content=raw)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {'code': code, 'path': None})
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.repo.reads, 0)


if __name__ == '__main__': unittest.main()
