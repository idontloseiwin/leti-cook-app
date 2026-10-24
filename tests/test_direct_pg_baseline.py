"""Focused offline disposable-database guards and safe failure output (no PostgreSQL)."""
import importlib.util
import ipaddress
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lic_direct_pg_baseline", ROOT / "tools/check_direct_pg_baseline.py")
baseline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = baseline
spec.loader.exec_module(baseline)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(baseline.os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.admin = dict(host="127.0.0.1", port="6543", dbname="lic_direct_pg_probe_unit", user="postgres", password="not-a-real-secret")
        self.runtime = {**self.admin, "user": "authenticator"}

    def validate(self):
        return baseline.validate_configuration("ADMIN", "RUNTIME", parse=lambda s: self.admin if s == "ADMIN" else self.runtime)

    def test_explicit_matching_disposable_pair(self):
        self.assertEqual(self.validate(), ("ADMIN", "RUNTIME"))

    def test_rejects_unsafe_or_implicit_destinations_and_roles(self):
        bad = [("host", "remote.example"), ("host", "localhost,remote.example"),
               ("dbname", "leti-cook"), ("dbname", "let_it_cook"), ("dbname", "lic_direct_pg_probe_"),
               ("dbname", "lic_direct_pg_probe_x;drop"), ("port", ""), ("port", "0"),
               ("port", "65536"), ("password", ""), ("user", "service_role"),
               ("service", "private"), ("hostaddr", "192.0.2.1"), ("options", "-c role=postgres")]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                original = self.runtime.copy()
                self.runtime[key] = value
                with self.assertRaisesRegex(baseline.BaselineFailure, "^baseline_configuration_invalid$"):
                    self.validate()
                self.runtime = original

    def test_mismatching_ports_and_databases(self):
        for key, value in [("port", "6544"), ("dbname", "lic_direct_pg_probe_other")]:
            with self.subTest(key=key):
                original = self.runtime.copy()
                self.runtime[key] = value
                with self.assertRaises(baseline.BaselineFailure):
                    self.validate()
                self.runtime = original

    def test_libpq_environment_cannot_redirect_host_or_privileges(self):
        for name in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS"):
            with self.subTest(name=name), patch.dict(baseline.os.environ, {name: "private-value"}):
                with self.assertRaises(baseline.BaselineFailure):
                    self.validate()

    def test_raw_parser_exception_is_sanitized(self):
        def raises(_):
            raise ValueError("postgres://private:secret@remote/db")
        with self.assertRaises(baseline.BaselineFailure) as error:
            baseline.validate_configuration("private-admin", "private-runtime", parse=raises)
        self.assertEqual(str(error.exception), "baseline_configuration_invalid")
        self.assertTrue(error.exception.__suppress_context__)


class ServerAddressTests(unittest.TestCase):
    def test_text_and_psycopg_inet_objects_accept_only_exact_loopbacks(self):
        for value in ("127.0.0.1", "::1", ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")):
            baseline.validate_actual_server_address(value)

    def test_other_addresses_stay_rejected_and_diagnostics_are_fixed(self):
        for value, kind in (("172.17.0.2", "ipv4_not_allowed"), ("127.0.0.2", "ipv4_not_allowed"),
                            ("::ffff:127.0.0.1", "ipv6_not_allowed"), (None, "missing"),
                            ("private-server-value", "invalid"), (True, "invalid")):
            with self.subTest(kind=kind), self.assertRaises(baseline.BaselineFailure) as error:
                baseline.validate_actual_server_address(value)
            report = baseline.safe_failure(error.exception)
            self.assertEqual(report["failure_code"], "baseline_actual_server_not_loopback")
            self.assertEqual(report["server_address_kind"], kind)


class CleanupListTests(unittest.TestCase):
    def test_fixed_cleanup_list_covers_schema_tables_without_migration_history(self):
        import re
        schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")
        tables = set(re.findall(r"^CREATE TABLE (\w+\.\w+)", schema, re.M))
        release_ledger = {"operator_private.schema_migrations", "operator_private.schema_state"}
        self.assertTrue(release_ledger <= tables)
        self.assertEqual(set(baseline.ALL_TABLES), tables - release_ledger)
        self.assertFalse(set(baseline.ALL_TABLES) & release_ledger)
        self.assertEqual(len(set(baseline.ALL_TABLES)), len(baseline.ALL_TABLES))


    def test_preflight_refuses_nonempty_chain_table_before_fixture_use(self):
        from unittest.mock import Mock
        admin = Mock()
        admin.scalar.side_effect = [True, 170000, "lic_direct_pg_probe_unit", "127.0.0.1"]
        roles = [dict(rolname=name, rolsuper=False, rolinherit=False,
                      rolcreaterole=False, rolcreatedb=False, rolbypassrls=False,
                      rolcanlogin=name == "authenticator")
                 for name in ("authenticator", "service_role", "lic_reader")]
        counts = [dict(name=table, n=int(table == "public.operator_job_inventory_chains"))
                  for table in baseline.ALL_TABLES]
        admin.execute.side_effect = [roles, counts]
        with self.assertRaisesRegex(baseline.BaselineFailure, "^baseline_nonempty_domain$"):
            baseline.preflight(admin)
        self.assertIn("FROM public.operator_job_inventory_chains LIMIT 1001",
                      admin.execute.call_args.args[0])
        self.assertEqual(admin.scalar.call_count, 4)  # No seed snapshot after refusal.

    def test_reset_explicitly_covers_chain_parent_and_scan_fk_without_cascade(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")
        chain = schema.split("CREATE TABLE public.operator_job_inventory_chains", 1)[1].split(";", 1)[0]
        for table in ("public.operator_jobs", "public.channel_video_inventory_scans"):
            self.assertIn("REFERENCES " + table, chain)
        admin = Mock()
        admin.transaction.return_value = nullcontext()
        before = dict.fromkeys(baseline.ALL_TABLES, 0)
        admin.execute.side_effect = [None, [dict(name=k, n=v) for k, v in before.items()]]
        baseline.reset_fixture(admin, {}, before)
        query = admin.execute.call_args_list[0].args[0]
        self.assertEqual(query, "TRUNCATE TABLE " + ", ".join(baseline.ALL_TABLES))
        for table in ("public.operator_job_inventory_chains", "public.operator_jobs",
                      "public.channel_video_inventory_scans"):
            self.assertIn(table, query)
        self.assertNotIn("CASCADE", query)


class FailureReportTests(unittest.TestCase):
    def test_failure_does_not_publish_raw_server_error(self):
        error = RuntimeError("dsn=postgres://secret SQL parameters private payload")
        error.sqlstate = "P0001"
        error.diag = SimpleNamespace(message_primary="private payload")
        self.assertEqual(baseline.safe_failure(error), {"failure_type": "RuntimeError", "sqlstate": "P0001"})
        error.diag = SimpleNamespace(message_primary="baseline_expected_conflict_missing")
        self.assertEqual(baseline.safe_failure(error)["failure_code"], "baseline_expected_conflict_missing")

if __name__ == "__main__":
    unittest.main()
