"""RR-16 recovery with the real pool and a connection double: no sockets or DB."""
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))

import psycopg
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from db.base import postgres
from test_runtime_gates import runtime, step


class PoolRecoveryTests(unittest.TestCase):
    def test_recovery_does_not_check_live_connections_or_an_unused_or_closed_pool(self):
        for closed, size, waiting in ((True, 0, 2), (False, 1, 2), (False, 0, 0)):
            with self.subTest(closed=closed, size=size, waiting=waiting):
                pool = Mock(closed=closed)
                pool.get_stats.return_value = {'pool_size': size, 'requests_waiting': waiting}
                postgres._reconnect_waiting_pool(pool)
                pool.check.assert_not_called()

    def make_pool(self, *, production=True):
        available, abandoned = threading.Event(), threading.Event()
        attempts = []

        class Connection:
            @classmethod
            def connect(cls, *args, **kwargs):
                attempts.append(time.monotonic())
                if not available.is_set():
                    raise psycopg.OperationalError('synthetic_database_unavailable')
                return cls()

            def __init__(self):
                self.pgconn = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
                self._pool = None

            def close(self):
                self.pgconn.transaction_status = TransactionStatus.UNKNOWN

        def factory(*args, **kwargs):
            callback = kwargs.pop('reconnect_failed', None)

            def reconnect_failed(pool):
                abandoned.set()
                if callback is not None:
                    callback(pool)

            # Keep production queue/worker/lifecycle settings, accelerate only
            # waiting, and replace native network/configuration with the double.
            kwargs.update(connection_class=Connection, kwargs={}, configure=None,
                          timeout=0.05, reconnect_timeout=0.05,
                          reconnect_failed=reconnect_failed)
            return ConnectionPool('', **kwargs)

        config = postgres.PostgresConfig(
            'host=synthetic.invalid port=5432 dbname=fixture user=authenticator password=fixture',
            max_size=1, max_waiting=2)
        if production:
            with patch.object(postgres, 'ConnectionPool', side_effect=factory):
                pool = postgres._new_pool(config)
        else:
            pool = factory(min_size=0, max_size=1, max_waiting=2, num_workers=1, open=False)
        self.addCleanup(pool.close)
        pool.open(wait=False)
        return pool, available, abandoned, attempts

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('bounded pool recovery timed out')
            time.sleep(0.01)

    def expire_two_waiters(self, pool):
        for _ in range(2):
            with self.assertRaises(PoolTimeout):
                pool.getconn(timeout=0.05)
        self.assertEqual(pool.get_stats()['requests_waiting'], 2)

    def test_control_reproduces_refusal_before_a_healthy_connection_is_attempted(self):
        pool, available, abandoned, attempts = self.make_pool(production=False)
        self.expire_two_waiters(pool)
        self.wait_for(lambda: abandoned.is_set() and pool.get_stats()['pool_size'] == 0)
        available.set()
        before = len(attempts)
        with self.assertRaises(TooManyRequests):
            pool.getconn(timeout=0.05)
        self.assertEqual(len(attempts), before)

    def test_production_pool_recovers_without_replaying_expired_requests(self):
        pool, available, abandoned, attempts = self.make_pool()
        self.expire_two_waiters(pool)
        self.assertTrue(abandoned.wait(timeout=3))
        available.set()
        self.wait_for(lambda: pool.get_stats()['pool_available'] == 1)
        self.assertEqual(pool.get_stats()['requests_waiting'], 0)
        connection = pool.getconn(timeout=0.1)
        pool.putconn(connection)
        self.assertEqual(pool.get_stats()['pool_size'], 1)
        # No query/cursor implementation exists on the double: only connection
        # creation happened; the two failed client operations were not replayed.
        self.assertGreater(len(attempts), 1)

    def test_pool_shutdown_stops_background_reconnection(self):
        pool, available, abandoned, attempts = self.make_pool()
        self.expire_two_waiters(pool)
        self.assertTrue(abandoned.wait(timeout=3))
        pool.close()
        before = len(attempts)
        # Waiting beyond the accelerated reconnect window must not create a
        # new connection, even when the provider becomes available afterwards.
        available.set()
        time.sleep(0.12)
        self.assertEqual(len(attempts), before)

    def test_repeated_outages_preserve_the_one_connection_bound(self):
        pool, available, abandoned, attempts = self.make_pool()
        for _ in range(2):
            self.expire_two_waiters(pool)
            self.assertTrue(abandoned.wait(timeout=3))
            available.set()
            self.wait_for(lambda: pool.get_stats()['pool_available'] == 1)
            self.assertEqual(pool.get_stats()['pool_size'], 1)
            connection = pool.getconn(timeout=0.1)
            available.clear()
            abandoned.clear()
            connection.close()
            pool.putconn(connection)


class DatabaseRetryTests(unittest.TestCase):
    def test_database_return_does_not_wait_for_the_cleanup_backoff(self):
        controller, clocks = runtime()
        controller.repository.error = 'runtime_contract_unavailable'
        controller.initialize()
        step(controller, clocks, 151)
        self.assertFalse(controller.status()['user_work']['allowed'])
        controller.repository.error = None
        result = step(controller, clocks, 16)
        self.assertTrue(result['user_work']['allowed'])
        self.assertTrue(result['api_read']['allowed'])

    def test_wrong_schema_still_cannot_open_on_a_connection_retry(self):
        controller, clocks = runtime()
        controller.repository.error = 'runtime_contract_unavailable'
        controller.initialize()
        step(controller, clocks, 31)
        controller.repository.error = 'runtime_contract_schema_invalid'
        result = step(controller, clocks, 16)
        self.assertFalse(result['user_work']['allowed'])
        self.assertFalse(result['api_read']['allowed'])
        self.assertEqual(result['reason'], 'schema_invalid')


if __name__ == '__main__':
    unittest.main()
