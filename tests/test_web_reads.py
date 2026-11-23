"""Shared read-cache and source-lifetime behavior survives HTTP module extraction."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.responses import PlainTextResponse, JSONResponse, StreamingResponse
from hot_storage import HotSourceReadExpired, capture_read_lifetime, register_read_deadline
from web.cache import create_read_cache
from web.middleware import install_runtime_middleware

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class SharedReadCacheTests(unittest.TestCase):
    def test_cache_hit_copies_the_payload_and_preserves_its_source_deadline(self):
        cache = create_read_cache()
        deadline = NOW + timedelta(seconds=30)
        def load():
            register_read_deadline(deadline)
            return {"items": ["original"]}
        loader = Mock(side_effect=load)
        with patch("web.cache.hot_read_now", return_value=NOW):
            first = cache.cached_read("sidebar-data", 60, loader)
            first["items"].append("changed by caller")
            with capture_read_lifetime() as lifetime:
                second = cache.cached_read("sidebar-data", 60, loader)
        self.assertEqual(second, {"items": ["original"]})
        self.assertEqual(lifetime.expires_at, deadline)
        loader.assert_called_once()

    def test_invalidation_uses_one_shared_revision_and_preserves_unrelated_prefixes(self):
        cache = create_read_cache()
        cache.cached_read("sidebar-data", 60, lambda: "before")
        cache.cached_read("unrelated", 60, lambda: "retained")
        cache.clear_frontend_read_cache("channel")
        self.assertEqual(cache.cached_read("sidebar-data", 60, lambda: "after"), "after")
        self.assertEqual(cache.cached_read("unrelated", 60, lambda: self.fail("Unrelated cache was cleared")), "retained")

    def test_clear_during_loading_does_not_republish_stale_cached_work(self):
        cache = create_read_cache()
        def load():
            cache.clear_read_cache()
            return "in-flight"
        self.assertEqual(cache.cached_read("sidebar-data", 60, load), "in-flight")
        self.assertEqual(cache.cached_read("sidebar-data", 60, lambda: "fresh"), "fresh")

    def test_expired_source_is_retried_once_and_never_cached(self):
        cache = create_read_cache()
        def load():
            register_read_deadline(NOW - timedelta(seconds=1))
            return "expired"
        loader = Mock(side_effect=load)
        with patch("web.cache.hot_read_now", return_value=NOW):
            with self.assertRaises(HTTPException) as error:
                cache.cached_read("sidebar-data", 60, loader)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(cache.cached_read("sidebar-data", 60, lambda: "fresh"), "fresh")


class ReadLifetimeHttpTests(unittest.TestCase):
    def client(self, handler):
        app = FastAPI()
        with patch("web.middleware.selected_deployment_profile", return_value="operator"):
            install_runtime_middleware(app, core=SimpleNamespace())
        app.add_api_route("/read", handler, methods=["GET"])
        return TestClient(app)

    def test_json_marks_the_same_source_deadline_in_body_and_headers(self):
        async def read():
            register_read_deadline(NOW + timedelta(seconds=10))
            return {"items": [1]}
        with patch("web.middleware.hot_read_now", return_value=NOW):
            response = self.client(read).get("/read")
        self.assertEqual(response.json(), {"items": [1], "source_read_at": "2026-10-07T12:00:00Z",
                                           "source_expires_at": "2026-10-07T12:00:10Z"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-source-read-at"], "2026-10-07T12:00:00Z")
        self.assertEqual(response.headers["x-source-expires-at"], "2026-10-07T12:00:10Z")

    def test_json_that_expires_during_handling_is_replaced_by_503(self):
        async def read():
            register_read_deadline(NOW)
            return {"must_not_escape": True}
        with patch("web.middleware.hot_read_now", return_value=NOW):
            response = self.client(read).get("/read")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Source data expired during this read; retry."})
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_source_changed_exception_keeps_its_fixed_503_response(self):
        async def read():
            raise HotSourceReadExpired("private details")
        response = self.client(read).get("/read")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Source data changed during this read; retry."})
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_non_json_delivery_does_not_acquire_source_headers(self):
        async def read():
            return PlainTextResponse("asset")
        response = self.client(read).get("/read")
        self.assertEqual(response.text, "asset")
        self.assertNotIn("x-source-read-at", response.headers)

    def test_envelope_does_not_shorten_independent_items_or_touch_cookies(self):
        items = [{"title": "Первый", "source_expires_at": "2026-10-07T12:00:10Z"},
                 {"title": "Second", "source_expires_at": "2026-10-07T13:00:00Z"}]
        async def read():
            register_read_deadline(NOW + timedelta(seconds=10))
            response = JSONResponse({"items": items, "source_expires_at": "wrong",
                                     "source_read_at": "old"}, status_code=202)
            response.set_cookie("first", "one")
            response.set_cookie("second", "two")
            return response
        with patch("web.middleware.hot_read_now", return_value=NOW):
            response = self.client(read).get("/read")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["items"], items)
        for field in ("source_read_at", "source_expires_at"):
            self.assertEqual(response.json()[field], response.headers["x-" + field.replace("_", "-")])
        self.assertEqual(len(response.headers.get_list("set-cookie")), 2)
        self.assertEqual(int(response.headers["content-length"]), len(response.content))

    def test_cached_response_keeps_its_original_deadline(self):
        cache = create_read_cache()
        def load():
            register_read_deadline(NOW + timedelta(seconds=10))
            return {"items": [1]}
        loader = Mock(side_effect=load)
        async def read():
            return cache.cached_read("test", 60, loader)
        client = self.client(read)
        with patch("web.middleware.hot_read_now", return_value=NOW), patch("web.cache.hot_read_now", return_value=NOW):
            first = client.get("/read")
        later = NOW + timedelta(seconds=5)
        with patch("web.middleware.hot_read_now", return_value=later), patch("web.cache.hot_read_now", return_value=later):
            second = client.get("/read")
        self.assertEqual(first.json()["source_expires_at"], second.json()["source_expires_at"])
        self.assertNotEqual(first.json()["source_read_at"], second.json()["source_read_at"])
        loader.assert_called_once()

    def test_array_shape_and_individual_deadlines_are_preserved(self):
        rows = [{"id": "one", "source_expires_at": "2026-10-07T12:00:10Z"},
                {"id": "two", "source_expires_at": "2026-10-07T13:00:00Z"}]
        async def read():
            register_read_deadline(NOW + timedelta(seconds=10))
            return rows
        with patch("web.middleware.hot_read_now", return_value=NOW):
            response = self.client(read).get("/read")
        self.assertEqual(response.json(), rows)
        self.assertEqual(response.headers["x-source-expires-at"], rows[0]["source_expires_at"])

    def test_user_work_and_signed_protocol_without_source_reads_are_unmodified(self):
        payload = {"version": 2, "proof": "synthetic", "user_work": {"ids": ["one"]}}
        async def read():
            return payload
        response = self.client(read).get("/read")
        self.assertEqual(response.json(), payload)
        self.assertNotIn("x-source-expires-at", response.headers)

    def test_json_expired_while_rendering_does_not_escape(self):
        async def read():
            register_read_deadline(NOW + timedelta(seconds=1))
            return {"title": "Must not escape"}
        with patch("web.middleware.hot_read_now", side_effect=[NOW, NOW + timedelta(seconds=1)]):
            response = self.client(read).get("/read")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Must not escape", response.text)

    def test_source_json_stream_is_rejected_without_unbounded_buffering(self):
        async def read():
            register_read_deadline(NOW + timedelta(seconds=10))
            async def chunks():
                yield b'{"title":"must not escape"}'
            return StreamingResponse(chunks(), media_type="application/json")
        with patch("web.middleware.hot_read_now", return_value=NOW):
            response = self.client(read).get("/read")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("must not escape", response.text)

    def test_event_stream_passes_through_without_envelope_buffering(self):
        async def read():
            async def chunks():
                yield b"data: ready\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream")
        response = self.client(read).get("/read")
        self.assertEqual(response.text, "data: ready\n\n")
        self.assertNotIn("x-source-read-at", response.headers)


if __name__ == "__main__":
    unittest.main()
