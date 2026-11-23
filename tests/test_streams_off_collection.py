"""Offline checks of the per-format collection gates.

The disposable-PostgreSQL proof that a stream-off pass sends YouTube no
request for stream work is tests/streams_off_pg.py.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from collection.auto_pass import AutoPassState, CommonCandidateCursor  # noqa: E402
from collection_formats import FORMATS_ENV  # noqa: E402
from queue_health import with_turned_off_formats  # noqa: E402
from runtime.process_collection import load_collection_policy  # noqa: E402

FLAGS = {"SHORT_VIDEO_COLLECTION_ENABLED": "true", "COMPLETED_STREAM_COLLECTION_ENABLED": "true"}


def page(request, rows=()):
    return dict(rows=list(rows), inspected_count=0, scan_exhausted=True, next_after=request["after"])


class CommonCandidateGateTests(unittest.TestCase):
    def kinds_requested(self, formats):
        state = AutoPassState(100, concurrency=2, run_id=str(uuid.uuid4()), formats=formats)
        cursor = CommonCandidateCursor()
        state.bind_candidate_cursor(cursor)
        kinds = []
        for _ in range(12):
            request = state.seed_request(2)
            if request is None:
                break
            kinds.append(request["video_type"])
            state.accept_seed_page(page(request))
        for operation in ("metadata", "counters"):
            for _ in range(6):
                request = state.candidate_request(2, operation=operation)
                if request is None:
                    break
                kinds.append(request["video_type"])
                state.accept_candidate_page(page(request))
        return kinds, state, cursor

    def test_stream_frontiers_are_never_read_when_streams_are_off(self):
        kinds, state, cursor = self.kinds_requested(("long", "short"))
        self.assertTrue(kinds)
        self.assertNotIn("stream", kinds)
        self.assertEqual(set(kinds), {"long", "short", "unknown"})
        self.assertEqual(state.off_frontiers, {("metadata", "stream"), ("counters", "stream")})
        self.assertTrue(state.operation_window_exhausted("metadata"))
        self.assertTrue(cursor.sweep_exhausted)
        # A wrap of the window keeps the turned-off frontiers closed.
        state.wrap_candidate_window("metadata", scopes=None)
        self.assertIn(("metadata", "stream"), cursor._exhausted)

    def test_every_format_on_reads_streams_as_before(self):
        kinds, state, _ = self.kinds_requested(("long", "short", "stream"))
        self.assertIn("stream", kinds)
        self.assertEqual(state.off_frontiers, frozenset())

    def test_a_format_turned_on_again_resumes_in_the_next_pass(self):
        cursor = CommonCandidateCursor()
        first = AutoPassState(10, run_id=str(uuid.uuid4()), formats=("long", "short"))
        first.bind_candidate_cursor(cursor)
        cursor._release(first)
        second = AutoPassState(10, run_id=str(uuid.uuid4()), formats=("long", "short", "stream"))
        second.bind_candidate_cursor(cursor)
        self.assertNotIn(("metadata", "stream"), cursor._exhausted)
        self.assertNotIn(("counters", "stream"), cursor._exhausted)

    def test_process_snapshot_is_the_default(self):
        with patch.dict("os.environ", {FORMATS_ENV: "long,short"}):
            self.assertEqual(AutoPassState(1).formats, ("long", "short"))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(AutoPassState(1).formats, ("long", "short", "stream"))
        with self.assertRaises(ValueError):
            AutoPassState(1, formats=())


class PolicyTests(unittest.TestCase):
    def test_effective_switch_is_flag_and_config(self):
        policy = load_collection_policy({**FLAGS, FORMATS_ENV: "long,short"})
        self.assertEqual(policy.video_formats, ("long", "short"))
        self.assertTrue(policy.short_video_collection_enabled)
        self.assertFalse(policy.completed_stream_collection_enabled)
        policy = load_collection_policy({**FLAGS, FORMATS_ENV: "long,short,stream"})
        self.assertTrue(policy.completed_stream_collection_enabled)
        policy = load_collection_policy({**FLAGS, "COMPLETED_STREAM_COLLECTION_ENABLED": "false",
                                         FORMATS_ENV: "long,short,stream"})
        self.assertEqual(policy.video_formats, ("long", "short"))
        self.assertFalse(policy.completed_stream_collection_enabled)
        self.assertEqual(load_collection_policy({**FLAGS}).video_formats, ("long", "short", "stream"))


def bucket(due, failed, total, stamp=None):
    counts = dict.fromkeys(("total", "due", "failed", "excluded_failed", "paused", "policy_paused",
                            "runtime_disabled", "leased", "expired_leases", "hydration_due", "metadata_due",
                            "counter_due", "pending", "hydrated", "awaiting_completion", "unavailable",
                            "identity_pending", "playlist_disabled"), 0)
    return dict(counts, total=total, due=due, failed=failed, oldest_overdue_at=stamp)


class HealthTests(unittest.TestCase):
    def queue(self):
        by_type = dict(long=bucket(3, 1, 10, "2026-11-08T10:00:00+00:00"), short=bucket(2, 0, 5, "2026-11-08T09:00:00+00:00"),
                       stream=bucket(7, 2, 9, "2026-11-01T00:00:00+00:00"), unknown=bucket(0, 0, 1))
        total = bucket(12, 3, 25, "2026-11-01T00:00:00+00:00")
        return dict(total, schema_version=2, scope="all_types", unit="videos", by_type=by_type)

    def test_turned_off_stream_queue_is_neither_due_nor_failed(self):
        value = with_turned_off_formats(self.queue(), ("long", "short"))
        self.assertEqual((value["due"], value["failed"], value["total"]), (5, 1, 16))
        self.assertEqual(value["oldest_overdue_at"], "2026-11-08T09:00:00+00:00")
        self.assertTrue(value["by_type"]["stream"]["turned_off"])
        self.assertEqual(value["by_type"]["stream"]["due"], 7)
        self.assertNotIn("turned_off", value["by_type"]["long"])
        self.assertEqual(value["turned_off_types"], ["stream"])

    def test_all_formats_or_unavailable_readings_are_unchanged(self):
        queue = self.queue()
        self.assertIs(with_turned_off_formats(queue, ("long", "short", "stream")), queue)
        self.assertEqual(with_turned_off_formats({"mode": "disabled"}, ("long",)), {"mode": "disabled"})
        self.assertIsNone(with_turned_off_formats(None, ("long",)))


if __name__ == "__main__":
    unittest.main()
