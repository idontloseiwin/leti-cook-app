"""Offline guard, temporary setup and HTTP-flow tests; never imports server/core."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lic_direct_pg_application", ROOT / "tools/check_direct_pg_application.py")
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


class GuardTests(unittest.TestCase):
    def test_explicit_single_mode_and_new_report_required_without_connecting(self):
        for args in ([], ["--mode", "all"], ["--mode", "local", "--report", __file__]):
            with self.subTest(args=args), patch.object(probe.safety, "connect") as connect:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    probe.main(args)
                connect.assert_not_called()

    def test_container_requires_linux_loopback_only_and_memory_bound(self):
        for platform, interfaces, value in (("win32", [(1, "lo")], "123"),
                ("linux", [(1, "lo"), (2, "eth0")], "123"),
                ("linux", [], "123"), ("linux", [(1, "lo")], "max"),
                ("linux", [(1, "lo")], str(probe.MAX_MEMORY + 1)),
                ("linux", [(1, "lo")], "0")):
            with self.subTest(platform=platform, interfaces=interfaces, value=value), self.assertRaises(probe.ProbeFailure):
                probe.container_guard(platform=platform, interfaces=interfaces, read_text=lambda _: value)
        self.assertEqual(probe.container_guard(platform="linux", interfaces=[(1, "lo")],
                         read_text=lambda _: str(probe.MAX_MEMORY)), probe.MAX_MEMORY)

    def test_v1_memory_limit_is_checked_only_when_v2_absent(self):
        read = Mock(side_effect=[FileNotFoundError(), "134217728\n"])
        self.assertEqual(probe.container_guard(platform="linux", interfaces=[(1, "lo")], read_text=read), 134217728)
        self.assertEqual(read.call_args_list[1].args, ("/sys/fs/cgroup/memory/memory.limit_in_bytes",))

    def test_unknown_errors_payloads_and_stage_are_never_published(self):
        error = type("PrivatePasswordInClass", (RuntimeError,), {})("postgresql://private:secret@host Cookie=session")
        error.sqlstate = "private-password"
        result = probe.safe_failure(error, "private-stage")
        self.assertEqual(result, {"failure_type": "operation", "failure_code": "application_probe_failed"})
        for error in (probe.ProbeFailure("private"), probe.safety.BaselineFailure("private")):
            self.assertEqual(probe.safe_failure(error, "preflight"), {
                "failure_type": "guard", "failure_code": "application_probe_failed", "failure_stage": "preflight"})

    def test_fixed_owned_failure_code_is_useful_without_exception_details(self):
        error = probe.ProbeFailure("pool_cleanup")
        error.private_payload = "never-published"
        self.assertEqual(probe.safe_failure(error, "application"), {
            "failure_type": "guard", "failure_code": "application_probe_pool_cleanup",
            "failure_stage": "application"})

    def test_exception_categories_and_sqlstate_are_fixed_without_payloads(self):
        for kind, category in ((TypeError, "type"), (KeyError, "key"), (AttributeError, "attribute"), (ImportError, "import")):
            result = probe.safe_failure(kind("private secret"), "application")
            self.assertEqual(result["failure_category"], category)
            self.assertNotIn("private", json.dumps(result))
        class DatabaseFailure(Exception):
            pass
        error = DatabaseFailure("private SQL detail")
        error.code = "22023"
        with patch.dict(sys.modules, {"db.base.postgres": SimpleNamespace(DatabaseError=DatabaseFailure)}):
            self.assertEqual(probe.safe_failure(error, "application")["sqlstate"], "22023")
            self.assertEqual(probe.safe_failure(error, "application")["failure_category"], "sql")
            error.code = "private"
            self.assertNotIn("sqlstate", probe.safe_failure(error, "application"))
        ordinary = RuntimeError("private")
        ordinary.sqlstate = "22023"
        self.assertNotIn("sqlstate", probe.safe_failure(ordinary, "application"))

    def test_runtime_and_http_diagnostics_allow_only_owned_values(self):
        error = probe.ProbeFailure("maintenance_failed")
        error.runtime_failure_code = "hot_runtime_schema_invalid"
        error.collection_state = "paused"
        error.http_route = "/sidebar-data"
        error.http_status, error.expected_http_status = 503, 200
        result = probe.safe_failure(error, "application")
        self.assertEqual(result["runtime_failure_code"], "hot_runtime_schema_invalid")
        self.assertEqual(result["http_status"], 503)
        for name in ("runtime_failure_code", "collection_state", "http_route", "http_status"):
            setattr(error, name, "private-secret")
            self.assertNotIn(name, probe.safe_failure(error, "application"))

    def test_environment_drops_inherited_secrets_and_generates_independent_identity(self):
        with patch.dict(os.environ, {"PATH": "safe", "SUPABASE_KEY": "old-private", "YOUTUBE_API_KEY": "real-private",
                                    "LIC_HOT_RUNTIME_SECRET": "old-private", "HTTP_PROXY": "private"}, clear=True):
            first, second = (probe.environment(Path("temporary"), "synthetic-runtime-dsn") for _ in range(2))
        self.assertEqual(first["LIC_DATABASE_DSN"], "synthetic-runtime-dsn")
        self.assertNotEqual(first["LIC_DATABASE_ID"], second["LIC_DATABASE_ID"])
        self.assertNotEqual(first["LIC_HOT_RUNTIME_SECRET"], second["LIC_HOT_RUNTIME_SECRET"])
        self.assertFalse({"SUPABASE_KEY", "YOUTUBE_API_KEY", "HTTP_PROXY"} & set(first))
        self.assertEqual(first["PYTHON_DOTENV_DISABLED"], "1")

    def test_http_and_subprocess_blockers_fail_before_dispatch(self):
        import httpx
        import requests
        import subprocess
        import urllib.request
        with probe.forbid_external_work() as counts:
            for call in (lambda: requests.get("https://invalid.example"),
                         lambda: urllib.request.urlopen("https://invalid.example"),
                         lambda: httpx.get("https://invalid.example"),
                         lambda: subprocess.Popen(["never-executed"])):
                with self.assertRaises(probe.ProbeFailure):
                    call()
        self.assertEqual(counts, {"http_attempts": 3, "collector_starts": 1})

    def test_official_temporary_configuration_is_paused_complete_and_private(self):
        with tempfile.TemporaryDirectory() as name, patch.object(sys, "path", [str(ROOT / "application"), *sys.path]):
            directory = Path(name)
            with patch.dict(os.environ, probe.environment(directory, "synthetic-runtime-dsn"), clear=True):
                token = probe.prepare_profile()
                from collection_schedule import CollectionSchedules
                from runtime.collection_schedules import load_process_schedules
                self.assertEqual(load_process_schedules(os.environ), CollectionSchedules())
                from self_hosted_configuration import ConfigurationStore
                from self_hosted_settings import valid_api_key
                from db.self_hosted_auth import OwnerAuthRepository
                value = ConfigurationStore(os.environ["LIC_SETUP_DIRECTORY"]).read()
                self.assertTrue(value["prepared"])
                self.assertTrue(valid_api_key(value["api_key"]))
                self.assertFalse(value["settings"]["automatic_collection"])
                self.assertEqual(value["workspace"], {"phase": "complete", "channels": {}})
                self.assertEqual(len(token), 43)
                self.assertEqual(OwnerAuthRepository(os.environ["LIC_AUTH_DATABASE"]).access_state(0)["mode"], "pending")
                encrypted = (directory / "configuration/settings.enc").read_bytes()
                self.assertNotIn(value["api_key"].encode(), encrypted)
                self.assertNotIn("YOUTUBE_API_KEY", os.environ)


class Response:
    def __init__(self, status, value):
        self.status_code, self.value = status, value
        self.content = json.dumps(value).encode()

    def json(self):
        return self.value


class Browser:
    def __init__(self, shared, name):
        self.shared, self.name, self.cookies = shared, name, {}
        self.calls = []

    def get(self, path):
        allowed = self.shared["mode"] == "local" or self.cookies.get("session") == self.name
        if path == "/auth/session":
            result = Response(200, {"access_mode": self.shared["mode"]})
        elif not allowed:
            result = Response(401, {"code": "authentication_required"})
        elif path == "/sidebar-data":
            result = Response(200, {"channels": [], "folders": [], "labels": []})
        else:
            result = Response(200, {"items": [], "has_more": False, "next_cursor": None,
                                    "source": {"youtube_requests": False}})
        self.calls.append(("GET", path, result.status_code))
        return result

    def post(self, path, *, json, headers):
        assert headers == {"Origin": probe.ORIGIN, "X-LIC-Auth": "1"}
        if path == "/auth/claim":
            self.cookies["session"] = "setup"
        elif path == "/auth/complete":
            self.shared["mode"] = json["mode"]
            self.cookies = {"session": self.name} if json["mode"] == "password" else {}
        elif path == "/auth/login":
            self.cookies["session"] = self.name
        elif path == "/auth/logout":
            self.cookies.clear()
        self.calls.append(("POST", path, 200))
        return Response(200, {"status": "ok"})


class SessionFlowTests(unittest.TestCase):
    def test_password_uses_independent_login_and_logout_does_not_remove_second(self):
        shared = {"mode": "pending"}
        first, second = Browser(shared, "first"), Browser(shared, "second")
        self.assertIs(probe.owner_sessions(first, second, "password", "temporary-code", "temporary-password"), second)
        self.assertEqual([row[1] for row in first.calls if row[0] == "POST"], ["/auth/claim", "/auth/complete", "/auth/logout"])
        self.assertEqual([row[1] for row in second.calls if row[0] == "POST"], ["/auth/login"])
        self.assertEqual(first.calls[-1], ("GET", "/sidebar-data", 401))
        self.assertEqual(second.calls[0], ("GET", "/sidebar-data", 401))
        self.assertEqual(second.calls[-1][2], 200)
        self.assertFalse(first.cookies)
        self.assertTrue(second.cookies)

    def test_local_clients_have_no_session_cookies(self):
        shared = {"mode": "pending"}
        first, second = Browser(shared, "first"), Browser(shared, "second")
        self.assertIs(probe.owner_sessions(first, second, "local", "temporary-code", "temporary-password"), first)
        self.assertFalse(first.cookies or second.cookies)
        self.assertFalse(any(row[0] == "POST" for row in second.calls))
        self.assertTrue(all(row[2] == 200 for row in second.calls))

    def test_nonempty_catalogue_and_response_budget_fail_closed(self):
        client = Mock()
        client.get.return_value = Response(200, {"channels": [{"channel_id": "synthetic"}], "folders": [], "labels": []})
        with self.assertRaises(probe.ProbeFailure):
            probe.catalogue(client)
        for response in (Response(503, {}), Response(200, []), Response(200, {"large": "x" * 65536})):
            with self.assertRaises(probe.ProbeFailure):
                probe.response_json(response, 200)

    def test_readiness_requires_real_ready_and_paused_and_rejects_failure(self):
        runtime, collection = Mock(), Mock()
        collection.status.return_value = {"state": "paused"}
        runtime.status.return_value = {"status": "ready", "maintenance_failed": False}
        probe.wait_ready(runtime, collection)
        runtime.require_ready.assert_called_once_with()
        runtime.status.return_value = {"status": "blocked", "maintenance_failed": True,
                                       "failure_code": "hot_runtime_schema_invalid", "private": "secret"}
        with self.assertRaises(probe.ProbeFailure) as caught:
            probe.wait_ready(runtime, collection)
        diagnostic = probe.safe_failure(caught.exception, "application")
        self.assertEqual(diagnostic["runtime_failure_code"], "hot_runtime_schema_invalid")
        self.assertEqual(diagnostic["collection_state"], "paused")
        self.assertNotIn("secret", json.dumps(diagnostic))
        runtime.status.return_value = {"status": "blocked", "maintenance_failed": False}
        with patch.object(probe, "READY_SECONDS", 0), self.assertRaises(probe.ProbeFailure):
            probe.wait_ready(runtime, collection)


class OrchestrationTests(unittest.TestCase):
    def run_probe(self, *, failure=None, changed=False, guard_failure=False):
        events = []
        admin = SimpleNamespace(connection=Mock(), scalar=Mock(return_value=[]))
        before = {table: 0 for table in probe.safety.ALL_TABLES}
        snapshots = {table: [] for table in probe.safety.SEED_TABLES}
        def application(*args):
            events.append("application")
            print("private-unexpected-output")
            if failure:
                raise failure
            return {"pools_and_threads_closed": True}
        def preflight(*args):
            events.append("preflight")
            return snapshots, before
        with contextlib.ExitStack() as stack:
            guard = stack.enter_context(patch.object(probe, "container_guard", side_effect=probe.ProbeFailure("private") if guard_failure else None, return_value=probe.MAX_MEMORY))
            stack.enter_context(patch.object(probe, "deadline", contextlib.nullcontext))
            stack.enter_context(patch.object(probe.safety, "validate_configuration", return_value=("admin", "runtime")))
            connect = stack.enter_context(patch.object(probe.safety, "connect", return_value=admin))
            stack.enter_context(patch.object(probe.safety, "preflight", side_effect=preflight))
            stack.enter_context(patch.object(probe.safety, "bounded_counts", return_value={} if changed else before))
            application_mock = stack.enter_context(patch.object(probe, "run_application", side_effect=application))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = probe.main(["--mode", "local"])
        return code, json.loads(output.getvalue()), events, admin, connect, application_mock

    def test_success_preflights_before_application_and_closes_admin(self):
        code, result, events, admin, connect, application = self.run_probe()
        self.assertEqual(code, 0)
        self.assertEqual(events, ["preflight", "application"])
        self.assertTrue(result["fixture_unchanged"])
        admin.connection.close.assert_called_once_with()
        application.assert_called_once_with("local", "runtime")
        self.assertNotIn("private", json.dumps(result))

    def test_failed_application_closes_admin_and_sanitizes_output(self):
        code, result, _, admin, _, _ = self.run_probe(failure=RuntimeError("private-password"))
        self.assertEqual(code, 1)
        self.assertEqual(result["failure_stage"], "application")
        self.assertNotIn("private", json.dumps(result))
        admin.connection.close.assert_called_once_with()

    def test_preflight_rejection_never_starts_application_and_closes_admin(self):
        admin = SimpleNamespace(connection=Mock())
        with patch.object(probe, "container_guard", return_value=probe.MAX_MEMORY), \
                patch.object(probe, "deadline", contextlib.nullcontext), \
                patch.object(probe.safety, "validate_configuration", return_value=("admin", "runtime")), \
                patch.object(probe.safety, "connect", return_value=admin), \
                patch.object(probe.safety, "preflight", side_effect=probe.safety.BaselineFailure("private")), \
                patch.object(probe, "run_application") as application:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(probe.main(["--mode", "password"]), 1)
            self.assertEqual(json.loads(output.getvalue())["failure_stage"], "preflight")
            application.assert_not_called()
            admin.connection.close.assert_called_once_with()

    def test_container_rejection_never_connects_or_imports_application(self):
        code, result, events, _, connect, application = self.run_probe(guard_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["failure_stage"], "container")
        self.assertEqual(events, [])
        connect.assert_not_called()
        application.assert_not_called()

    def test_changed_fixture_fails_without_reset_or_cleanup_sql(self):
        code, result, _, admin, _, _ = self.run_probe(changed=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["failure_stage"], "unchanged_fixture")
        admin.scalar.assert_not_called()
        admin.connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
