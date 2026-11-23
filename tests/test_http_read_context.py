"""HTTP provenance reaches threadpool reads, not collection or the next borrower."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
from db.base.read_context import http_read_scope, collection_scope, http_request_method, is_http_read
from db.base.postgres import PostgresDatabase, PostgresConfig, Operation, DatabaseError, DatabaseServerError
from hot_runtime import runtime_collection_access, runtime_request_access
from web.middleware import HttpReadScopeMiddleware
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN


@contextmanager
def database():
    events = []
    connection = Connection(events)
    pool = Pool(connection, events)
    with PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
                          pool_factory=lambda config: pool) as db:
        yield db, connection


def read(db, connection):
    connection.results.append(Result(rows=({'value': 1},)))
    return db.read_row('http_fixture', 'SELECT 1 AS value')


class HttpReadContextTests(unittest.TestCase):
    def test_http_read_writer_and_subsequent_pool_borrower_choose_fixed_roles(self):
        with database() as (db, connection):
            with http_read_scope(), runtime_request_access('POST', '/add-channel'):
                read(db, connection)
                with db.transaction(Operation('writer_fixture')):
                    pass
                read(db, connection)
            read(db, connection)
        roles = [statement for statement, _ in connection.setup if statement.startswith('SET LOCAL ROLE')]
        self.assertEqual(roles, ['SET LOCAL ROLE lic_reader', 'SET LOCAL ROLE service_role',
                                 'SET LOCAL ROLE lic_reader', 'SET LOCAL ROLE service_role'])
        self.assertFalse(is_http_read())

    def test_background_collection_resets_and_restores_request_context_even_on_error(self):
        with database() as (db, connection), http_read_scope(), runtime_request_access('GET', '/sidebar-data'):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'), collection_scope():
                self.assertFalse(is_http_read())
                self.assertIsNone(http_request_method())
                read(db, connection)
                raise RuntimeError('synthetic')
            self.assertTrue(is_http_read())
            self.assertEqual(http_request_method(), 'GET')
            read(db, connection)
        roles = [statement for statement, _ in connection.setup if statement.startswith('SET LOCAL ROLE')]
        self.assertEqual(roles, ['SET LOCAL ROLE service_role', 'SET LOCAL ROLE lic_reader'])

    def test_get_and_head_writer_refusal_precedes_admission_and_pool_wait(self):
        for method in ('GET', 'HEAD'):
            for operation in (Operation('writer_default'), Operation('writer_explicit', read_only=False)):
                with self.subTest(method=method, operation=operation.name):
                    with database() as (db, connection), runtime_request_access(method, '/sidebar-user-data'):
                        def unexpected_admission(name):
                            self.fail('writer must be refused before maintenance admission')
                        db._admission = unexpected_admission
                        with self.assertRaisesRegex(DatabaseError, '^database_http_write_forbidden$'):
                            with db.transaction(operation):
                                self.fail('writer body must not execute')
                        self.assertEqual(connection.events, [])
                        self.assertEqual(connection.setup, [])
                        self.assertEqual(connection.queries, [])
                        self.assertEqual(db._pool.borrowed, 0)
                    self.assertIsNone(http_request_method())

    def test_real_http_middleware_propagates_writer_refusal_to_threadpool(self):
        from starlette.concurrency import run_in_threadpool
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method), database() as (db, connection):
                def write():
                    self.assertTrue(is_http_read())
                    self.assertEqual(http_request_method(), method)
                    with db.transaction(Operation('writer_fixture')):
                        self.fail('writer body must not execute')
                async def app(scope, receive, send):
                    await run_in_threadpool(write)
                with self.assertRaisesRegex(DatabaseError, '^database_http_write_forbidden$'):
                    asyncio.run(HttpReadScopeMiddleware(app)(
                        {'type': 'http', 'method': method, 'path': '/sidebar-user-data'}, None, None))
                self.assertEqual(connection.events, [])
                self.assertEqual(db._pool.borrowed, 0)
                self.assertFalse(is_http_read())
                self.assertIsNone(http_request_method())

    def test_real_http_middleware_keeps_post_writer_and_get_head_read_roles(self):
        from starlette.concurrency import run_in_threadpool
        for method in ('POST', 'GET', 'HEAD'):
            with self.subTest(method=method), database() as (db, connection):
                def operation():
                    self.assertTrue(is_http_read())
                    self.assertEqual(http_request_method(), method)
                    if method == 'POST':
                        with db.transaction(Operation('writer_fixture')):
                            pass
                    else:
                        read(db, connection)
                async def app(scope, receive, send):
                    await run_in_threadpool(operation)
                asyncio.run(HttpReadScopeMiddleware(app)(
                    {'type': 'http', 'method': method, 'path': '/sidebar-user-data'}, None, None))
                expected = 'service_role' if method == 'POST' else 'lic_reader'
                self.assertEqual(connection.setup[0][0], 'SET LOCAL ROLE ' + expected)
                read(db, connection)
                roles = [statement for statement, _ in connection.setup if statement.startswith('SET LOCAL ROLE')]
                self.assertEqual(roles, ['SET LOCAL ROLE ' + expected, 'SET LOCAL ROLE service_role'])
                self.assertIsNone(http_request_method())

    def test_collection_admission_does_not_remove_http_get_write_prohibition(self):
        with database() as (db, connection), http_read_scope(), runtime_request_access('GET', '/sidebar-data'):
            with runtime_collection_access():
                self.assertEqual(http_request_method(), 'GET')
                with self.assertRaisesRegex(DatabaseError, '^database_http_write_forbidden$'):
                    with db.transaction(Operation('writer_fixture')):
                        self.fail('writer body must not execute')
            self.assertEqual(db._pool.borrowed, 0)

    def test_nested_request_method_restores_after_error(self):
        with runtime_request_access('GET', '/sidebar-data'):
            self.assertEqual(http_request_method(), 'GET')
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                with runtime_request_access('POST', '/add-channel'):
                    self.assertEqual(http_request_method(), 'POST')
                    raise RuntimeError('synthetic')
            self.assertEqual(http_request_method(), 'GET')
        self.assertIsNone(http_request_method())

    def test_permission_failure_never_retries_under_service_role(self):
        with database() as (db, connection), http_read_scope():
            connection.results.append(Result(execute_error=ServerFailure('42501')))
            with self.assertRaises(DatabaseServerError) as caught:
                db.read_row('http_fixture', 'SELECT 1 AS value')
            self.assertEqual(caught.exception.code, '42501')
        roles = [statement for statement, _ in connection.setup if statement.startswith('SET LOCAL ROLE')]
        self.assertEqual(roles, ['SET LOCAL ROLE lic_reader'])
        self.assertEqual(connection.rollbacks, 1)

    def test_outer_middleware_covers_setup_style_short_circuit_and_threadpool(self):
        from starlette.concurrency import run_in_threadpool
        seen = []
        async def setup_response(scope, receive, send):
            seen.append(is_http_read())
            with database() as (db, connection):
                seen.append(await run_in_threadpool(is_http_read))
                await run_in_threadpool(read, db, connection)
                seen.append(connection.setup[0][0])
        asyncio.run(HttpReadScopeMiddleware(setup_response)({'type': 'http', 'method': 'GET', 'path': '/setup/status'}, None, None))
        self.assertEqual(seen, [True, True, 'SET LOCAL ROLE lic_reader'])
        self.assertFalse(is_http_read())

    def test_error_and_non_http_scope_cannot_leak_request_role(self):
        seen = []
        async def app(scope, receive, send):
            seen.append(is_http_read())
            raise RuntimeError('synthetic')
        for scope in ('http', 'lifespan', 'websocket'):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                asyncio.run(HttpReadScopeMiddleware(app)({'type': scope}, None, None))
            self.assertFalse(is_http_read())
        self.assertEqual(seen, [True, False, False])

    def test_collection_callback_clears_provenance_before_owning_resources(self):
        import ast
        tree = ast.parse((ROOT/'application/web/jobs.py').read_text(encoding='utf-8'))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == '_run_channel_initial_inventory_task')
        outer = function.body[0]
        self.assertIsInstance(outer, ast.With)
        self.assertEqual([ast.unparse(item.context_expr) for item in outer.items],
                         ['collection_scope()', 'runtime_collection_access()'])


if __name__ == '__main__':
    unittest.main()
