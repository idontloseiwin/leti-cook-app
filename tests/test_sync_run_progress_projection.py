"""Offline progress projection checks through the existing writer/read owners.

Transaction responses are fixtures; these tests verify projection and routing,
not PostgreSQL casts, locking, or a live collection run.
"""
import ast
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from operational_copy_policy import (
    SUMMARY_PHASES, SUMMARY_PHASE_STATES, SUMMARY_PROGRESS_FIELDS,
    safe_summary_copy,
)
from sync_run_summary import compact_sync_run_summary
from db.runs.sync_run_summary import persisted_summary_copy, persisted_summary_text
from db.runs.sync_runs import _response_rows
from db.runs.sync_run_write_normalization import update_sync_run
from test_sync_run_write_normalization import Database, row


class SyncRunProgressProjectionTests(unittest.TestCase):
    def setUp(self):
        self.progress = {
            "current_phase": "video_collection",
            "phase_state": "running",
            "progress_updated_at": "2026-10-19T16:51:10.123456+00:00",
        }

    def projections(self, value):
        return (
            safe_summary_copy(value),
            persisted_summary_copy(value),
            json.loads(persisted_summary_text(json.dumps(value))),
        )

    def test_recorded_progress_survives_compaction_write_and_response_projection(self):
        original = {
            **self.progress, "errors": 0,
            "video_collection": {"updated": 6, "current_phase": "preflight"},
            "title": "private provider text", "token": "private credential",
        }
        compact = compact_sync_run_summary(original)
        tx = Database(row(json.dumps(compact), errors=0), 1)
        self.assertEqual(update_sync_run(tx, 23, {"summary_json": compact}), 1)
        self.assertEqual([call[0] for call in tx.calls], ["one", "execute"])
        stored = json.loads(tx.calls[-1][2][0])
        response = _response_rows([{
            "id": 23, "status": "running", "errors": 0, "summary_json": stored,
        }])[0]
        for key, value in self.progress.items():
            self.assertEqual(stored[key], value)
            self.assertEqual(response["summary_json"][key], value)
        self.assertEqual(response["summary_json"]["errors"], 0)
        self.assertEqual(response["summary_json"]["video_collection"], {"updated": 6})
        self.assertNotIn("private", json.dumps(response))
        self.assertEqual(original["video_collection"]["current_phase"], "preflight")

    def test_known_completed_paused_and_error_observations_remain_distinct(self):
        for phase, state in (
            ("completed", "completed"), ("typed_inventory", "paused"),
            ("preflight", "error"), ("completed_stream_collection", "running"),
        ):
            value = {**self.progress, "current_phase": phase, "phase_state": state}
            with self.subTest(phase=phase, state=state):
                for projected in self.projections(value):
                    self.assertEqual(projected, value)

    def test_cycle_producers_use_the_finite_progress_vocabulary(self):
        tree = ast.parse((ROOT / "application/collection/cycle.py").read_text())
        phases, states = set(), set()
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id not in {"mark_progress", "stop_after_collection"}:
                continue
            if call.args and isinstance(call.args[0], ast.Constant):
                phases.add(call.args[0].value)
            if call.func.id == "mark_progress" and len(call.args) > 1:
                argument = call.args[1]
                outcomes = (argument.body, argument.orelse) if isinstance(argument, ast.IfExp) else (argument,)
                for value in outcomes:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        states.add(value.value)
        self.assertTrue(phases)
        self.assertFalse(phases - SUMMARY_PHASES)
        self.assertFalse((states | {"running"}) - SUMMARY_PHASE_STATES)

    def test_only_complete_timezone_qualified_real_calendar_timestamps_survive(self):
        for stamp in (
            "2026-10-19T16:51:10Z", "2026-10-19T19:51:10.1+03:00",
            "2024-02-29T02:01:59.123456-05:30",
        ):
            value = {"progress_updated_at": stamp}
            with self.subTest(stamp=stamp):
                for projected in self.projections(value):
                    self.assertEqual(projected, value)
        for stamp in (
            "2026-10-19", "2026-10-19T16:51:10", "2026-10-19 16:51:10Z",
            "2026-10-19T16:51Z", "2026-02-29T16:51:10Z", "0000-01-01T00:00:00Z",
            "2026-10-19T24:00:00Z", "2026-10-19T16:51:60Z",
            "2026-10-19T16:51:10+00:60", "2026-10-19T16:51:10+24:00",
            "2026-10-19T16:51:10+0300", "2026-10-19T16:51:10.1234567Z",
            "2026-10-19T16:51:10Z\n", "2026-10-19T16:51:10Z token=private",
            "https://example.invalid/?key=private", "running", "",
        ):
            with self.subTest(stamp=stamp):
                for projected in self.projections({"progress_updated_at": stamp}):
                    self.assertEqual(projected, {})

    def test_unknown_and_wrong_type_progress_values_are_not_generic_labels(self):
        for field in SUMMARY_PROGRESS_FIELDS:
            for value in (None, True, False, 0, 1.25, [], {}, "private credential", "unknown"):
                with self.subTest(field=field, value=value):
                    for projected in self.projections({field: value}):
                        self.assertEqual(projected, {})
        for value in ({"current_phase": "running"}, {"phase_state": "video_collection"}):
            for projected in self.projections(value):
                self.assertEqual(projected, {})

    def test_progress_is_root_owned_and_does_not_expand_generic_labels(self):
        value = {
            **self.progress,
            "video_collection": {**self.progress, "updated": 3},
            "reported_values": self.progress.copy(),
            "timings": {"timings": self.progress.copy()},
            "mode": "video_collection",
        }
        for projected in self.projections(value):
            self.assertEqual(projected, {**self.progress, "video_collection": {"updated": 3}})
        self.assertEqual(safe_summary_copy(self.progress, depth=1), {})
        self.assertEqual(persisted_summary_copy(self.progress, depth=1), {})
        self.assertEqual(persisted_summary_copy(self.progress, depth=None), {})

    def test_read_boundary_strips_malformed_historical_progress(self):
        row_value = {
            "id": 20, "summary_json": {
                "current_phase": "private credential", "phase_state": 4,
                "progress_updated_at": "not a timestamp", "errors": 2,
            },
        }
        self.assertEqual(_response_rows([row_value])[0]["summary_json"], {"errors": 2})

    def test_old_records_without_progress_do_not_invent_phase_or_timestamp(self):
        for projected in self.projections({"errors": 0, "completion_outcome": "completed"}):
            self.assertFalse(SUMMARY_PROGRESS_FIELDS & projected.keys())


if __name__ == "__main__":
    unittest.main()
