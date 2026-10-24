"""Known connection failures are bounded HTTP refusals, never write replays."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, _mapped
from web.middleware import install_runtime_middleware


class DatabaseUnavailableHttpTests(unittest.TestCase):
    def client(self, action):
        app = FastAPI()
        with patch('web.middleware.selected_deployment_profile', return_value='operator'):
            install_runtime_middleware(app, core=SimpleNamespace())

        def endpoint():
            return action()

        app.add_api_route('/operation', endpoint, methods=['GET', 'POST'])
        return TestClient(app, raise_server_exceptions=False)

    def test_first_three_writes_get_safe_503_and_are_not_replayed(self):
        for failure in (PoolTimeout('private timeout'), TooManyRequests('private queue'),
                        PoolClosed('private pool'), psycopg.OperationalError('private connection')):
            with self.subTest(failure=type(failure).__name__):
                action = Mock(side_effect=_mapped(failure, unknown=False, allowed=frozenset()))
                with self.client(action) as client:
                    for _ in range(3):
                        response = client.post('/operation')
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json(), {
                            'code': 'database_unavailable', 'outcome_unknown': False})
                        self.assertEqual(response.headers['cache-control'], 'no-store')
                        self.assertNotIn('retry-after', response.headers)
                self.assertEqual(action.call_count, 3)

    def test_lost_write_acknowledgement_is_explicit_and_never_retried(self):
        action = Mock(side_effect=DatabaseUnavailable('private error', outcome_unknown=True))
        with self.client(action) as client:
            response = client.post('/operation')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            'code': 'database_write_outcome_unknown', 'outcome_unknown': True})
        self.assertNotIn('retry-after', response.headers)
        self.assertNotIn('private', response.text)
        action.assert_called_once()

    def test_read_retry_advice_does_not_repeat_the_read_on_the_server(self):
        action = Mock(side_effect=DatabaseUnavailable('private error'))
        with self.client(action) as client:
            response = client.get('/operation')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['retry-after'], '5')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        action.assert_called_once()

    def test_server_and_programming_errors_are_not_disguised_as_pressure(self):
        for failure in (DatabaseServerError('database_operation_failed', code='23505'),
                        ValueError('programming_error')):
            with self.subTest(failure=type(failure).__name__):
                with self.client(Mock(side_effect=failure)) as client:
                    self.assertEqual(client.get('/operation').status_code, 500)

    def test_pool_failure_diagnostics_distinguish_safe_reasons(self):
        for failure, reason in ((PoolTimeout('secret'), 'pool_timeout'),
                                (TooManyRequests('secret'), 'pool_queue_full'),
                                (PoolClosed('secret'), 'pool_closed'),
                                (psycopg.OperationalError('secret'), 'connection_failed')):
            with self.subTest(reason=reason):
                error = _mapped(failure, unknown=False, allowed=frozenset())
                self.assertEqual(error.reason, reason)
                self.assertNotIn('secret', str(error))


if __name__ == '__main__':
    unittest.main()
