"""Every HTTP read path has a retention verdict (R07a). No database, network or YouTube.

The routes come from the real assembled application, imported in a fresh
isolated process with every optional router switched on. Paths that the access
and setup middlewares answer themselves are read from their source. A new
route without a classification, or a classified route that disappeared, fails.
The verdicts and gap IDs are explained in the R07a read-path audit.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
FIXTURE = ROOT / "tests" / "fixtures" / "read_paths.json"
VERDICTS = ("protected", "partial", "unprotected", "no_youtube_data")


def probe():
    """Print the (method, path) pairs of the assembled app; run in a fresh process."""
    from contextlib import ExitStack
    from unittest.mock import patch
    import socket
    import sqlite3
    import tempfile
    sys.path.insert(0, str(APPLICATION))
    with tempfile.TemporaryDirectory(prefix="lic-read-paths-") as temporary:
        os.environ.update({
            "HOME": temporary, "USERPROFILE": temporary,
            "PYTHON_DOTENV_DISABLED": "1", "LIC_DEPLOYMENT_PROFILE": "self-hosted",
            "LIC_DATABASE_TRANSPORT": "postgres",
            "LIC_DATABASE_DSN": "postgresql://authenticator:synthetic@127.0.0.1:1/offline",
            "LIC_DATABASE_ID": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "LIC_HOT_RUNTIME_SECRET": "synthetic-read-path-audit-not-a-secret-0000",
            "LIC_HOT_RUNTIME_URL": "http://127.0.0.1:19999/internal/hot-runtime",
            "LIC_PUBLIC_ORIGIN": "http://127.0.0.1:19999",
            "LIC_AUTH_DATABASE": str(Path(temporary) / "owner.sqlite3"),
            # Optional routers are all on, so the inventory is the largest route set.
            "LIC_SETUP_DIRECTORY": str(Path(temporary) / "configuration"),
            "TELEMETRY_ENABLED": "false", "TELEMETRY_LEGACY_HTTP_ENABLED": "true",
            "YOUTUBE_QUOTA_PROJECT_ID": "offline-read-path-test",
            "YOUTUBE_QUOTA_JOURNAL_PATH": str(Path(temporary) / "quota.sqlite3"),
            "OPERATOR_JOBS_MODE": "durable",
        })

        def forbidden(*args, **kwargs):
            raise AssertionError("route inventory attempted an external effect")

        with ExitStack() as guards:
            guards.enter_context(patch.object(socket, "has_ipv6", False))
            guards.enter_context(patch.object(socket.socket, "connect", forbidden))
            guards.enter_context(patch.object(socket, "getaddrinfo", forbidden))
            guards.enter_context(patch.object(sqlite3, "connect", forbidden))
            guards.enter_context(patch.object(subprocess, "Popen", forbidden))
            import psycopg
            import psycopg_pool
            guards.enter_context(patch.object(psycopg, "connect", forbidden))
            guards.enter_context(patch.object(psycopg.Connection, "connect", forbidden))
            guards.enter_context(patch.object(psycopg_pool.ConnectionPool, "open", forbidden))
            from collection_schedule import CollectionSchedules
            from runtime.collection_schedules import schedule_environment
            os.environ.update(schedule_environment(CollectionSchedules()))
            import server
            from fastapi.routing import iter_route_contexts
            if Path(server.__file__).resolve() != APPLICATION / "server.py":
                raise AssertionError("server loaded from another checkout")
            routes = sorted({(method, route.path)
                             for route in iter_route_contexts(server.app.routes)
                             for method in (route.methods or ())})
            assert server.database._pool is None or server.database._pool.closed
            assert list(Path(temporary).iterdir()) == []
    print(json.dumps({"routes": routes}))


def application_routes():
    environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
                   if key in os.environ}
    result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--probe"],
                            capture_output=True, text=True, timeout=60, env=environment)
    if result.returncode:
        raise AssertionError("route probe failed:\n" + result.stderr)
    return {f"{method} {path}" for method, path in json.loads(result.stdout)["routes"]}


def _module(name):
    return ast.parse((APPLICATION / name).read_text(encoding="utf-8"))


def auth_paths():
    """Paths that SelfHostedAuthMiddleware answers before any route."""
    found = {}
    for node in _module("self_hosted_auth.py").body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "PUBLIC_FILES":
                found[name] = set(ast.literal_eval(node.value))
            elif name == "AUTH_PATHS":
                found[name] = set(ast.literal_eval(node.value.args[0]))
    if set(found) != {"PUBLIC_FILES", "AUTH_PATHS"}:
        raise AssertionError("self_hosted_auth no longer declares PUBLIC_FILES and AUTH_PATHS")
    return found["PUBLIC_FILES"] | found["AUTH_PATHS"]


def setup_paths():
    """Path literals that SetupMiddleware compares against (its own pages and API)."""
    for node in ast.walk(_module("self_hosted_onboarding.py")):
        if isinstance(node, ast.ClassDef) and node.name == "SetupMiddleware":
            return {item.value for item in ast.walk(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    and (item.value == "/setup" or item.value.startswith("/setup/"))}
    raise AssertionError("self_hosted_onboarding no longer defines SetupMiddleware")


def fixture():
    def unique(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise AssertionError("duplicate key in read_paths.json")
        return dict(pairs)
    return json.loads(FIXTURE.read_text(encoding="utf-8"), object_pairs_hook=unique)


class ReadPathInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = fixture()

    def assert_classified(self, actual, classified, kind):
        missing = sorted(actual - set(classified))
        gone = sorted(set(classified) - actual)
        self.assertEqual(missing, [], f"{kind} without a retention verdict; classify them in "
                                      "tests/fixtures/read_paths.json after a read-path review")
        self.assertEqual(gone, [], f"classified {kind} no longer exist; remove them from read_paths.json")

    def test_every_application_route_has_a_verdict(self):
        self.assert_classified(application_routes(), self.data["routes"], "routes")

    def test_every_middleware_path_has_a_verdict(self):
        self.assert_classified(auth_paths() | setup_paths(), self.data["middleware_paths"], "middleware paths")

    def test_verdicts_are_known_and_gaps_are_named(self):
        gaps = self.data["gaps"]
        for key, entry in [*self.data["routes"].items(), *self.data["middleware_paths"].items()]:
            with self.subTest(path=key):
                self.assertIn(entry["verdict"], VERDICTS)
                self.assertTrue(entry["reads"].strip())
                named = entry.get("gaps", [])
                self.assertEqual(sorted(set(named) - set(gaps)), [])
                # A route that may show expired YouTube data names the gap that explains it.
                self.assertEqual(bool(named), entry["verdict"] in {"partial", "unprotected"})
        used = {gap for entry in [*self.data["routes"].values(), *self.data["middleware_paths"].values()]
                for gap in entry.get("gaps", [])}
        self.assertEqual(sorted(set(gaps) - used - set(self.data["gaps_outside_routes"])), [])

    def test_fixture_counts_match_the_audit_summary(self):
        counts = {verdict: 0 for verdict in VERDICTS}
        for entry in self.data["routes"].values():
            counts[entry["verdict"]] += 1
        self.assertEqual(counts, self.data["summary"]["routes"])


if __name__ == "__main__":
    if "--probe" in sys.argv:
        import traceback
        try:
            probe()
        except BaseException:
            traceback.print_exc()
            raise SystemExit(1)
    else:
        unittest.main()
