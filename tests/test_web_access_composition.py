"""Exercise the assembled ASGI boundary in a fresh process without DB/API/startup."""
from pathlib import Path
import json
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:19999"


def probe():
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch
    import socket
    import asyncio
    import httpx
    import sqlite3
    import tempfile
    sys.path.insert(0, str(ROOT / "application"))
    with tempfile.TemporaryDirectory(prefix="lic-http-test-") as temporary:
        os.environ.update({
            "HOME": temporary, "USERPROFILE": temporary,
            "PYTHON_DOTENV_DISABLED": "1", "LIC_DEPLOYMENT_PROFILE": "self-hosted",
            "LIC_DATABASE_TRANSPORT": "postgres",
            "LIC_DATABASE_DSN": "postgresql://authenticator:synthetic@127.0.0.1:1/offline",
            "LIC_DATABASE_ID": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "LIC_HOT_RUNTIME_SECRET": "synthetic-http-contract-not-a-secret-000000",
            "LIC_HOT_RUNTIME_URL": ORIGIN + "/internal/hot-runtime", "LIC_PUBLIC_ORIGIN": ORIGIN,
            "LIC_AUTH_DATABASE": str(Path(temporary) / "owner.sqlite3"),
            "LIC_SETUP_DIRECTORY": str(Path(temporary) / "configuration"),
            "YOUTUBE_QUOTA_PROJECT_ID": "offline-http-test",
            "YOUTUBE_QUOTA_JOURNAL_PATH": str(Path(temporary) / "quota.sqlite3"),
            "TELEMETRY_ENABLED": "false", "TELEMETRY_LEGACY_HTTP_ENABLED": "false",
            "OPERATOR_JOBS_MODE": "durable",
            "CHANNEL_BASELINE_ANALYTICS_ENABLED": "true",
            "CHANNEL_DERIVED_INSIGHTS_ENABLED": "true",
            "CHANNEL_TIMELINE_INSIGHTS_ENABLED": "false",
            "CHANNEL_LAUNCH_ANALYTICS_ENABLED": "false",
        })
        forbidden_calls = []
        def forbidden(*args, **kwargs):
            forbidden_calls.append(True)
            raise AssertionError("HTTP boundary test attempted an external effect")
        # Windows creates an internal loopback socket pair when constructing an event loop.
        # Construct that infrastructure first; application network calls remain forbidden.
        loop = asyncio.new_event_loop()
        with ExitStack() as guards:
            guards.enter_context(patch.object(socket, "has_ipv6", False))
            guards.enter_context(patch.object(socket.socket, "connect", forbidden))
            guards.enter_context(patch.object(socket, "getaddrinfo", forbidden))
            guards.enter_context(patch.object(sqlite3, "connect", forbidden))
            import psycopg
            import psycopg_pool
            guards.enter_context(patch.object(psycopg, "connect", forbidden))
            guards.enter_context(patch.object(psycopg.Connection, "connect", forbidden))
            guards.enter_context(patch.object(psycopg_pool.ConnectionPool, "open", forbidden))
            from collection_schedule import CollectionSchedules
            from runtime.collection_schedules import schedule_environment
            os.environ.update(schedule_environment(CollectionSchedules()))
            import server
            import hot_runtime
            import self_hosted_auth
            import self_hosted_onboarding
            guards.enter_context(patch.object(server.database, "open", forbidden))
            guards.enter_context(patch.object(server.YOUTUBE_QUOTA_ATTEMPT_RECORDER, "start", forbidden))
            guards.enter_context(patch.object(subprocess, "Popen", forbidden))
            state = {"mode": "local", "setup": True, "hot": True}
            owner = SimpleNamespace(configured=lambda: True, access_state=lambda *args: {
                "mode": state["mode"], "setup_verified": True, "setup_expired": False,
            }, session=lambda *args: None)
            manager = SimpleNamespace(ready=lambda: state["setup"], workspace_pending=lambda: False)
            def require_ready():
                if not state["hot"]:
                    raise hot_runtime.HotRuntimeUnavailable()
            controller = SimpleNamespace(require_ready=require_ready,
                                         require_collection_ready=require_ready,
                                         require_database_ready=lambda: None,
                                         status=lambda: {"status": "ready" if state["hot"] else "blocked"})
            guards.enter_context(patch.object(self_hosted_auth, "OwnerAuthRepository", return_value=owner))
            guards.enter_context(patch.object(self_hosted_onboarding, "_MANAGER", manager))
            guards.enter_context(patch.object(hot_runtime, "_local_controller", controller))
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=ORIGIN, follow_redirects=False)
            results = {}
            def request(name, method, path, **kwargs):
                response = loop.run_until_complete(client.request(method, path, **kwargs))
                value = {"status": response.status_code,
                         "location": response.headers.get("location"),
                         "no_store": response.headers.get("cache-control") == "no-store",
                         "csp": bool(response.headers.get("content-security-policy"))}
                if response.headers.get("content-type", "").startswith("application/json"):
                    value["json"] = response.json()
                results[name] = value
                return response
            request("root", "GET", "/")
            request("root_head", "HEAD", "/")
            response = request("frontend", "GET", "/app-explorer-v3")
            assert response.content == (server.FRONTEND_DIST_DIR / "explorer-v3.html").read_bytes()
            response = request("deep_link", "GET", "/app-explorer-v3/channels/test")
            assert response.content == (server.FRONTEND_DIST_DIR / "explorer-v3.html").read_bytes()
            request("missing_asset", "GET", "/app-explorer-v3/assets/not-present.js")
            request("asset_escape", "GET", "/app-explorer-v3/assets/%2e%2e%2fexplorer-v3.html")
            for path in ("/app", "/app-explorer-v2/old", "/app-next"):
                request("retired" + path, "GET", path)
            headers = {"origin": ORIGIN}
            request("retired_command", "POST", "/run-deep-stats-channel", headers=headers)
            request("channel_bound", "POST", "/add-channel", headers=headers, json={"url": "x" * 2049})
            request("folder_bound", "POST", "/folders", headers=headers, json={"name": "x" * 81})
            request("label_bool", "POST", "/channels/channel/labels/monetized", headers=headers, json={"enabled": "yes"})
            request("body_bound", "POST", "/folders", headers={**headers, "content-length": str(1048577)}, content=b"{}")
            request("bad_host", "GET", "/", headers={"host": "evil.invalid"})
            request("bad_origin", "POST", "/folders", headers={"origin": "http://evil.invalid"}, json={"name": "x"})
            request("internal_auth", "GET", "/internal/hot-runtime")
            state["mode"] = "password"
            request("owner_page", "GET", "/app-explorer-v3", headers={"accept": "text/html"})
            request("owner_api", "GET", "/system-summary")
            request("auth_ui", "GET", "/auth")
            state["mode"] = "local"
            state["setup"] = False
            request("setup_page", "GET", "/app-explorer-v3", headers={"accept": "text/html"})
            request("setup_api", "GET", "/system-summary")
            request("setup_ui", "GET", "/setup")
            state["setup"] = True
            state["hot"] = False
            response = request("hot_api", "GET", "/sidebar-data")
            assert response.headers["retry-after"] == "5"
            response = request("hot_page", "GET", "/app-explorer-v3", headers={"accept": "text/html"})
            assert response.content == (server.FRONTEND_DIST_DIR / "explorer-v3.html").read_bytes()
            assert "retry-after" not in response.headers
            assert not forbidden_calls
            assert server.database._pool is None or server.database._pool.closed
            assert list(Path(temporary).iterdir()) == []
            results["flags"] = [server.CHANNEL_BASELINE_ANALYTICS_ENABLED,
                server.CHANNEL_DERIVED_INSIGHTS_ENABLED, server.CHANNEL_TIMELINE_INSIGHTS_ENABLED,
                server.CHANNEL_LAUNCH_ANALYTICS_ENABLED]
            loop.run_until_complete(client.aclose())
            loop.close()
            print(json.dumps(results))


class WebAccessCompositionTests(unittest.TestCase):
    def test_real_application_access_and_static_boundaries_without_external_effects(self):
        environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP") if key in os.environ}
        result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--probe"],
                                capture_output=True, text=True, timeout=45, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        expected = {
            "root": 303, "root_head": 303, "frontend": 200, "deep_link": 200,
            "missing_asset": 404, "asset_escape": 404,
            "retired/app": 410, "retired/app-explorer-v2/old": 410, "retired/app-next": 410,
            "retired_command": 410, "channel_bound": 422, "folder_bound": 422,
            "label_bool": 422, "body_bound": 413, "bad_host": 400, "bad_origin": 403,
            "internal_auth": 403, "owner_page": 303, "owner_api": 401, "auth_ui": 200,
            "setup_page": 303, "setup_api": 409, "setup_ui": 200, "hot_api": 503, "hot_page": 200,
        }
        self.assertEqual(set(values) - {"flags"}, set(expected))
        for name, status in expected.items():
            with self.subTest(name=name):
                self.assertEqual(values[name]["status"], status)
                self.assertTrue(values[name]["no_store"])
                self.assertTrue(values[name]["csp"])
        for name, code in {"body_bound": "request_too_large", "bad_host": "invalid_host",
                           "bad_origin": "cross_origin_denied", "internal_auth": "internal_auth_required",
                           "owner_api": "authentication_required", "setup_api": "setup_required",
                           "hot_api": "hot_runtime_not_ready"}.items():
            self.assertEqual(values[name]["json"]["code"], code)
        self.assertEqual(values["owner_page"]["location"], "/auth")
        self.assertEqual(values["setup_page"]["location"], "/setup")
        self.assertEqual(values["root"]["location"], "/app-explorer-v3")
        self.assertEqual(values["retired/app"]["json"]["detail"]["replacement"], "/app-explorer-v3")
        self.assertEqual(values["retired_command"]["json"]["detail"]["code"], "legacy_channel_stats_retired")
        self.assertEqual(values["flags"], [True, True, False, False])


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
