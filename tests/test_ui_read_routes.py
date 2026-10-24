"""UI diagnostics observe partial responses and cache recovery without changing them."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import telemetry
from telemetry_contract import build_event, normalize_stored_event
from db.base.postgres import DatabaseServerError
from hot_storage import HotSourceReadExpired
from web.cache import create_read_cache
from web.home import create_home_router
from web.middleware import HttpReadScopeMiddleware
import ui_read_trace
from http_test_support import system_http


class UiReadRouteTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        for setting in (
            patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE="self-hosted"),
            patch.object(telemetry, "is_enabled", return_value=True),
            patch.object(telemetry, "emit_event", side_effect=self.record),
        ):
            setting.start()
            self.addCleanup(setting.stop)

    def record(self, *args, **kwargs):
        value = build_event(*args, **kwargs)
        self.assertEqual(normalize_stored_event(value), value)
        self.rows.append(value)
        return value

    def run_read(self, fn, path="/explorer/home-summary"):
        async def app(scope, receive, send):
            result = fn()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})
            return result

        async def send(message):
            pass

        return asyncio.run(ui_read_trace.observe_http(
            app, {"type": "http", "method": "GET", "path": path}, None, send))

    def test_failed_count_is_partial_200_on_miss_and_cached_hit_without_repeating_failure(self):
        cache = create_read_cache()
        app = FastAPI()
        app.add_middleware(HttpReadScopeMiddleware)
        core = SimpleNamespace(VIDEO_TYPE_LONG="long", VIDEO_TYPE_SHORT="short", VIDEO_TYPE_STREAM="stream")
        app.include_router(create_home_router(cache=cache, core=core, database=object(),
            library=SimpleNamespace(load_visible_channel_ids=lambda: {"UC_PRIVATE"})))
        inventory = Mock()
        inventory.count_inventory_by_type.side_effect = DatabaseServerError("DO_NOT_LOG", code="57014")
        runs = Mock()
        runs.latest_completed.return_value = None
        runs.latest_completed_with_changes.return_value = None
        with patch("web.home.FrontendRepository", return_value=inventory), \
             patch("web.home.SyncRunRepository", return_value=runs), \
             patch("builtins.print"):
            client = TestClient(app)
            first = client.get("/explorer/home-summary?secret=PRIVATE_QUERY")
            divider = len(self.rows)
            second = client.get("/explorer/home-summary")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIsNone(first.json()["saved_videos"])
        self.assertEqual(first.json(), second.json())
        inventory.count_inventory_by_type.assert_called_once()
        for response, rows in ((first, self.rows[:divider]), (second, self.rows[divider:])):
            trace_id = response.headers["X-Leti-Read-Trace"]
            self.assertTrue(all(row["trace_id"] == trace_id for row in rows))
            root = [row for row in rows if row.get("phase") == "http" and row.get("status") != "started"]
            self.assertEqual(len(root), 1)
            self.assertEqual(root[0]["http_status"], 200)
            self.assertEqual(root[0]["status"], "partial")
            self.assertTrue(any(row.get("reason") == "component_unavailable" for row in rows))
        first_errors = [row for row in self.rows[:divider] if row.get("status") == "error"]
        self.assertTrue(any(row.get("error_code") == "57014" for row in first_errors))
        self.assertFalse(any(row.get("status") == "error" for row in self.rows[divider:]))
        self.assertTrue(any(row.get("status") == "hit" for row in self.rows[divider:]))
        encoded = json.dumps(self.rows)
        for private in ("DO_NOT_LOG", "UC_PRIVATE", "PRIVATE_QUERY"):
            self.assertNotIn(private, encoded)

    def test_cache_source_retry_recovery_is_not_reported_as_partial(self):
        cache = create_read_cache()
        loader = Mock(side_effect=[HotSourceReadExpired("PRIVATE"), {"value": 1}])
        received = []
        self.run_read(lambda: received.append(cache.cached_read("explorer-home-summary", 20, loader)))
        self.assertEqual(received, [{"value": 1}])
        self.assertEqual(loader.call_count, 2)
        self.assertTrue(any(row.get("status") == "retry" and row.get("reason") == "source_expired"
                            for row in self.rows))
        terminal = [row for row in self.rows if row.get("phase") == "http" and row.get("status") != "started"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["status"], "ok")

    def test_cached_unavailable_system_components_are_partial_without_new_errors(self):
        payload = {
            "recent_runs_read_status": "unavailable", "queue_health": None,
            "video_type_reviews": {"available": False, "has_open": None},
            "quota": {"read_status": "ok"}, "recent_runs": [], "latest_run": None,
        }
        app = FastAPI()
        app.add_middleware(HttpReadScopeMiddleware)
        app.include_router(system_http(cache=SimpleNamespace(
            cached_read=lambda key, ttl, load: dict(payload))))
        response = TestClient(app).get("/system-summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(
            {row["operation"] for row in self.rows if row.get("status") == "unavailable"},
            {"recent_sync_runs", "queue_health", "video_type_reviews"},
        )
        self.assertFalse(any(row.get("status") == "error" for row in self.rows))
        self.assertEqual(self.rows[-1]["phase"], "http")
        self.assertEqual(self.rows[-1]["status"], "partial")

    def test_invalidated_loader_result_is_not_republished_and_dynamic_key_is_private(self):
        cache = create_read_cache()
        key = "explorer-recent-videos:PRIVATE_CURSOR:first"

        def first_load():
            cache.clear_read_cache("explorer-recent-videos:")
            return {"value": 1}

        values = []
        self.run_read(lambda: values.append(cache.cached_read(key, 20, first_load)),
                      "/explorer/recent-videos")
        self.run_read(lambda: values.append(cache.cached_read(key, 20, lambda: {"value": 2})),
                      "/explorer/recent-videos")
        self.assertEqual(values, [{"value": 1}, {"value": 2}])
        self.assertTrue(any(row.get("status") == "invalidated" for row in self.rows))
        self.assertNotIn("PRIVATE_CURSOR", json.dumps(self.rows))

    def home_client(self, cache):
        app = FastAPI()
        app.add_middleware(HttpReadScopeMiddleware)
        core = SimpleNamespace(VIDEO_TYPE_LONG="long", VIDEO_TYPE_SHORT="short", VIDEO_TYPE_STREAM="stream")
        app.include_router(create_home_router(cache=cache, core=core, database=object(),
            library=SimpleNamespace(load_visible_channel_ids=lambda: {"UC_PRIVATE"})))
        return TestClient(app)

    def test_home_timestamp_failure_remains_partial_on_cache_hit_with_no_new_error(self):
        for failed_method, operation in (
            ("latest_completed", "latest_completed_sync"),
            ("latest_completed_with_changes", "latest_data_change"),
        ):
            with self.subTest(operation=operation):
                self.rows.clear()
                inventory = Mock()
                inventory.count_inventory_by_type.return_value = {"long": 2, "short": 0, "stream": 0}
                runs = Mock()
                runs.latest_completed.return_value = None
                runs.latest_completed_with_changes.return_value = None
                getattr(runs, failed_method).side_effect = DatabaseServerError("PRIVATE_FAILURE", code="57014")
                with patch("web.home.FrontendRepository", return_value=inventory), \
                     patch("web.home.SyncRunRepository", return_value=runs), patch("builtins.print"):
                    client = self.home_client(create_read_cache())
                    first = client.get("/explorer/home-summary")
                    divider = len(self.rows)
                    second = client.get("/explorer/home-summary")
                self.assertEqual(first.json(), second.json())
                self.assertIsNotNone(first.json()["saved_videos"])
                self.assertNotIn("unavailable_components", first.json())
                for rows in (self.rows[:divider], self.rows[divider:]):
                    self.assertEqual(rows[-1]["status"], "partial")
                    self.assertTrue(any(row["operation"] == operation and row["status"] == "unavailable"
                                        for row in rows))
                self.assertFalse(any(row["status"] == "error" for row in self.rows[divider:]))
                getattr(runs, failed_method).assert_called_once()

    def test_legitimate_empty_history_is_not_an_unavailable_component(self):
        inventory = Mock()
        inventory.count_inventory_by_type.return_value = {"long": 0, "short": 0, "stream": 0}
        runs = Mock()
        runs.latest_completed.return_value = None
        runs.latest_completed_with_changes.return_value = None
        with patch("web.home.FrontendRepository", return_value=inventory), \
             patch("web.home.SyncRunRepository", return_value=runs):
            client = self.home_client(create_read_cache())
            first = client.get("/explorer/home-summary")
            second = client.get("/explorer/home-summary")
        self.assertEqual(first.json(), second.json())
        self.assertIsNone(first.json()["last_successful_sync_at"])
        self.assertIsNone(first.json()["last_data_change_at"])
        self.assertFalse(any(row["status"] in {"partial", "unavailable", "error"} for row in self.rows))

    def test_cached_provenance_survives_disabled_recording_without_public_fields(self):
        cache = create_read_cache()
        calls = []
        def loader():
            calls.append(True)
            ui_read_trace.failure("latest_data_change", RuntimeError("PRIVATE_OLD_FAILURE"))
            return {"last_data_change_at": None, "saved_videos": {"long": 12}}
        received = []
        with patch.object(telemetry, "is_enabled", return_value=False):
            self.run_read(lambda: received.append(cache.cached_read("explorer-home-summary", 20, loader)))
        self.assertEqual(self.rows, [])
        self.run_read(lambda: received.append(cache.cached_read("explorer-home-summary", 20, loader)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(received[0], received[1])
        self.assertNotIn("unavailable_components", received[1])
        self.assertEqual(self.rows[-1]["status"], "partial")
        self.assertTrue(any(row["operation"] == "latest_data_change" and row["status"] == "unavailable"
                            for row in self.rows))
        self.assertFalse(any(row["status"] == "error" for row in self.rows))
        self.assertNotIn("PRIVATE_OLD_FAILURE", json.dumps(self.rows))

    def test_nested_cache_provenance_propagates_only_accepted_values_and_does_not_leak(self):
        cache = create_read_cache()
        def failed_child():
            ui_read_trace.failure("recent_sync_runs", RuntimeError("PRIVATE"))
            return {"history": None}
        def parent():
            return {"child": cache.cached_read("system-queue-health", 15, failed_child)}
        received = []
        self.run_read(lambda: received.append(cache.cached_read("system-summary", 20, parent)),
                      "/system-summary")
        divider = len(self.rows)
        self.run_read(lambda: received.append(cache.cached_read("system-summary", 20, parent)),
                      "/system-summary")
        self.assertEqual(received[0], received[1])
        self.assertEqual(self.rows[-1]["status"], "partial")
        self.assertTrue(any(row["operation"] == "recent_sync_runs" and row["status"] == "unavailable"
                            for row in self.rows[divider:]))
        self.assertFalse(any(row["status"] == "error" for row in self.rows[divider:]))
        self.rows.clear()
        self.run_read(lambda: cache.cached_read("explorer-home-summary", 20, lambda: {"healthy": True}))
        self.assertEqual(self.rows[-1]["status"], "ok")
        self.assertFalse(any(row["status"] == "unavailable" for row in self.rows))

    def test_discarded_child_attempt_does_not_taint_parent_cache_or_root(self):
        cache = create_read_cache()
        attempts = []
        def child():
            attempts.append(True)
            if len(attempts) == 1:
                ui_read_trace.failure("inventory_counts", RuntimeError("PRIVATE_DISCARDED"))
                raise HotSourceReadExpired("PRIVATE_CLOCK")
            return {"count": 2}
        def parent():
            return cache.cached_read("system-queue-health", 15, child)
        self.run_read(lambda: cache.cached_read("system-summary", 20, parent), "/system-summary")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(any(row["status"] == "error" for row in self.rows))
        self.assertEqual(self.rows[-1]["status"], "ok")
        self.rows.clear()
        self.run_read(lambda: cache.cached_read("system-summary", 20, parent), "/system-summary")
        self.assertEqual(self.rows[-1]["status"], "ok")
        self.assertFalse(any(row["status"] in {"partial", "error", "unavailable"} for row in self.rows))


if __name__ == "__main__":
    unittest.main()
