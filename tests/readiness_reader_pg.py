"""Native readiness regression: HTTP reader privileges must remain restricted."""
import os
import sys
import unittest
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query_pg import bootstrap
from collection_schedule import CollectionSchedules
from db.base.postgres import PostgresConfig, PostgresDatabase
from db.base.read_context import http_read_scope
from db.readiness import capabilities, intrinsic_capabilities
from schema_release import load_schema_release


class ReaderReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_only')
        password = os.environ['LIC_QUERY_TEST_PASSWORD']
        port = int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook',
                                    user='postgres', password=password, autocommit=True)
        bootstrap(cls.admin)
        # The shared fixture applies every DDL step without the updater's
        # ledger bookkeeping. Stamp its final identity for readiness checks.
        cls.admin.execute('UPDATE operator_private.schema_state SET current_version=%s',
                          (load_schema_release().schema_version,))
        cls.db = PostgresDatabase(
            PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=2),
            admission=lambda _: None,
            cadence_snapshot=CollectionSchedules().cadence_snapshot(),
        ).open()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    def test_http_reader_and_collector_readiness(self):
        for name, check in capabilities.CHECKS.items():
            with self.subTest(role='service_role', check=name):
                self.assertIsInstance(check(self.db), dict)
        # Collector queue checks intentionally need service_role. Hydration
        # status uses this readiness entrypoint from an ordinary HTTP GET.
        with http_read_scope():
            intrinsic_capabilities.assert_final_schema_ready(self.db)
            self.assertTrue(capabilities.assert_operator_jobs_ready(self.db)['ready'])
        with self.admin.transaction(force_rollback=True):
            self.admin.execute('SET LOCAL ROLE lic_reader')
            self.assertFalse(self.admin.execute(
                "SELECT has_schema_privilege(current_user,'operator_private','USAGE')"
            ).fetchone()[0])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.admin.transaction(force_rollback=True):
                self.admin.execute('SET LOCAL ROLE lic_reader')
                self.admin.execute('SELECT * FROM operator_private.schema_state')

    def test_reader_still_rejects_changed_missing_and_extra_integrity_objects(self):
        definition = self.admin.execute(
            "SELECT pg_get_functiondef('public.guard_channel_user_work_identity()'::regprocedure)"
        ).fetchone()[0]
        mutations = (
            ('ALTER TABLE public.saved_objects DISABLE TRIGGER user_work_bytes_before', (True, True, False)),
            ('DROP TRIGGER user_work_bytes_before ON public.saved_objects', (True, True, False)),
            (definition.replace('BEGIN', 'BEGIN\n-- altered definition', 1), (True, False, True)),
            ('ALTER FUNCTION operator_private.check_user_work_bytes() RENAME TO missing_guard', (True, False, False)),
            ("CREATE FUNCTION public.unexpected_guard() RETURNS integer LANGUAGE sql AS 'SELECT 1'", (True, False, True)),
        )
        for mutation, expected in mutations:
            with self.subTest(mutation=mutation.splitlines()[0]):
                with self.admin.transaction(force_rollback=True):
                    self.admin.execute(mutation)
                    self.admin.execute('SET LOCAL ROLE lic_reader')
                    self.assertEqual(self.admin.execute(intrinsic_capabilities.READINESS_SQL).fetchone(), expected)
        with self.admin.transaction(force_rollback=True):
            self.admin.execute('SET LOCAL ROLE lic_reader')
            self.assertEqual(self.admin.execute(intrinsic_capabilities.READINESS_SQL).fetchone(), (True, True, True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
