"""Run history is an independent bounded read, even during slow diagnostics."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
from unittest.mock import Mock, patch

from http_test_support import endpoint, system_http
from hot_storage import capture_read_lifetime, register_read_deadline
from web.cache import create_read_cache


class RunHistoryReadTests(unittest.TestCase):
    def setUp(self):
        setting = patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE="self-hosted")
        setting.start()
        self.addCleanup(setting.stop)

    def test_history_calls_only_stored_run_repository_and_keeps_bounded_projection(self):
        repository = Mock()
        repository.list_recent.return_value = [
            {"id": 8, "status": "running", "summary_json": {
                "current_phase": "video_collection", "items": [1, 2],
                "video_collection": {"updated": 3, "nested": {"private": True}},
            }},
        ]
        router = system_http()
        with patch("web.system.SyncRunRepository", return_value=repository), \
             patch("web.system.VideoCollectionRepository", side_effect=AssertionError("queue read")), \
             patch("web.system.load_quota_read_model", side_effect=AssertionError("quota read")), \
             patch("web.system.collect_pipeline_health", side_effect=AssertionError("pipeline read")):
            result = endpoint(router, "system_run_history")()
        repository.list_recent.assert_called_once_with(limit=10)
        self.assertEqual(result["recent_runs_read_status"], "ok")
        self.assertEqual(result["latest_run"], result["recent_runs"][0])
        self.assertEqual(result["latest_run"]["summary_json"], {
            "current_phase": "video_collection", "items_count": 2,
            "video_collection": {"updated": 3},
        })
        self.assertEqual(set(result), {"latest_run", "recent_runs", "recent_runs_read_status", "now"})
        self.assertIsNotNone(datetime.fromisoformat(result["now"]).tzinfo)

    def test_failed_history_is_not_successful_empty_and_can_recover_after_invalidation(self):
        cache = create_read_cache()
        read = endpoint(system_http(cache=cache), "system_run_history")
        repository = Mock()
        repository.list_recent.side_effect = [RuntimeError("synthetic failure"), []]
        with patch("web.system.SyncRunRepository", return_value=repository), patch("builtins.print"):
            failed = read()
            self.assertEqual(failed["recent_runs_read_status"], "unavailable")
            cache.clear_frontend_read_cache()
            recovered = read()
        self.assertEqual(recovered["recent_runs_read_status"], "ok")
        self.assertEqual(recovered["recent_runs"], [])
        self.assertIsNone(recovered["latest_run"])
        self.assertEqual(repository.list_recent.call_count, 2)

    def test_history_does_not_wait_for_inflight_heavy_summary(self):
        entered, release = threading.Event(), threading.Event()
        cache = create_read_cache()
        history = endpoint(system_http(cache=cache), "system_run_history")

        def diagnostic():
            entered.set()
            if not release.wait(5):
                raise AssertionError("diagnostic was not released")
            return {}

        repository = Mock()
        repository.list_recent.return_value = [{"id": 9, "status": "completed"}]
        with ThreadPoolExecutor(max_workers=2) as pool, \
             patch("web.system.SyncRunRepository", return_value=repository):
            heavy = pool.submit(cache.cached_read, "system-summary", 3, diagnostic)
            try:
                self.assertTrue(entered.wait(2))
                result = pool.submit(history).result(timeout=2)
                self.assertEqual(result["latest_run"]["id"], 9)
                self.assertFalse(heavy.done())
            finally:
                release.set()
            heavy.result(timeout=2)

    def test_history_replays_real_deadline_on_cache_hit(self):
        cache = create_read_cache()
        history = endpoint(system_http(cache=cache), "system_run_history")
        deadline = datetime.now(timezone.utc) + timedelta(seconds=60)

        def load(*, limit):
            register_read_deadline(deadline)
            return []

        repository = Mock()
        repository.list_recent.side_effect = load
        with patch("web.system.SyncRunRepository", return_value=repository):
            with capture_read_lifetime() as first:
                history()
            with capture_read_lifetime() as second:
                history()
        self.assertEqual(first.expires_at, deadline)
        self.assertEqual(second.expires_at, deadline)
        repository.list_recent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
