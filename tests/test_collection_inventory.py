"""Retained inventory behavior at the explicit collection boundary; offline only."""

import ast
from datetime import datetime, timezone, timedelta
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
from collection import initial, lifecycle
from collection.settings import CollectionSettings

CHANNEL = "UC" + "c" * 22
VIDEO = "vid00000001"
OTHER = "vid00000002"
NOW = "2026-09-27T10:00:00+00:00"
SETTINGS = CollectionSettings(True, True, True)


class MissingPlaylist(RuntimeError):
    pass


class QuotaExceeded(RuntimeError):
    pass


def dependencies():
    return dict(
        database=object(), settings=SETTINGS, worker_id="test-worker",
        fetch_page=Mock(return_value={"items": []}),
        get_longform_playlist_id=Mock(return_value="UULF" + CHANNEL[2:]),
        parse_dt=Mock(side_effect=lambda value: datetime.fromisoformat(value) if value else None),
        playlist_not_found_error=MissingPlaylist, daily_quota_exceeded_error=QuotaExceeded,
    )


class InventoryDependencyTests(unittest.TestCase):
    def test_new_owners_import_without_bootstrap_or_network(self):
        for name in ("initial", "lifecycle"):
            tree = ast.parse((APPLICATION / "collection" / (name + ".py")).read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
                elif isinstance(node, ast.Import):
                    imports.update(item.name.split(".")[0] for item in node.names)
            self.assertFalse(imports & {"core", "sync_service", "server", "os", "dotenv"})
        script = """
import builtins, importlib, socket, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'dotenv'}:
        raise AssertionError('bootstrap import: ' + name)
    return original(name, *args, **kwargs)
with patch('builtins.__import__', side_effect=checked_import), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network during import')):
    for name in ('collection.initial', 'collection.lifecycle'):
        importlib.import_module(name)
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(APPLICATION)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(initial.collect_initial_inventory_chunk.__module__, "collection.initial")
        self.assertEqual(initial.initial_28d_stats_import.__module__, "collection.initial")

    def test_initial_dependencies_are_required_even_for_stored_inventory(self):
        supplied = dependencies()
        for name in supplied:
            with self.subTest(name=name):
                missing = dict(supplied)
                del missing[name]
                with self.assertRaises(TypeError):
                    initial.collect_initial_inventory_chunk(CHANNEL, video_type="long", max_pages=0, **missing, expected_epoch=7)
                with self.assertRaises(TypeError):
                    initial.collect_initial_inventory_chunk(
                        CHANNEL, video_type="long", max_pages=0, **{**supplied, name: None}, expected_epoch=7)

    def test_retained_dependency_contracts_reject_missing_and_none(self):
        gateway, parse = object(), Mock(return_value=None)
        cases = (
            (lifecycle.update_channel_values, (CHANNEL, {}), {"repository": Mock()}),
            (lifecycle.update_channel_lifecycle_status, (CHANNEL, {}), {"database": gateway}),
            (initial.load_recent_long_inventory_rows, (CHANNEL,), {"database": gateway}),
            (initial.initial_28d_stats_import, (CHANNEL,),
             {"database": gateway, "settings": SETTINGS, "run_video_collection_queue_batch": Mock(), "copy_collection_stop": Mock()}),
        )
        for entry, args, supplied in cases:
            scoped = entry in {
                lifecycle.update_channel_values, lifecycle.update_channel_lifecycle_status,
                initial.initial_28d_stats_import,
            }
            scope = {"expected_epoch": 7} if scoped else {}
            for name in supplied:
                with self.subTest(entry=entry.__name__, name=name):
                    missing = dict(supplied)
                    del missing[name]
                    with self.assertRaises(TypeError):
                        entry(*args, **scope, **missing)
                    with self.assertRaises(TypeError):
                        entry(*args, **scope, **{**supplied, name: None})


class LifecyclePersistenceTests(unittest.TestCase):
    def setUp(self):
        profile = patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"})
        profile.start()
        self.addCleanup(profile.stop)


    def test_process_state_warning_and_lifecycle_exception_are_preserved(self):
        repository = Mock(update_process_state=Mock(return_value=False))
        with patch.object(lifecycle, "source_log_message", side_effect=lambda event, message, **kw: message()), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            lifecycle.update_channel_values(CHANNEL, {"state": "running"}, repository=repository, expected_epoch=7)
        repository.update_process_state.assert_called_once_with(CHANNEL, {"state": "running"}, expected_epoch=7)
        self.assertIn("Optional channel process columns unavailable", output.getvalue())
        error = RuntimeError("write failed")
        repository.update_process_state.side_effect = error
        with patch.object(lifecycle, "ChannelSyncRepository", return_value=repository), \
             patch("sys.stdout", new=io.StringIO()), self.assertRaises(RuntimeError) as caught:
            lifecycle.update_channel_lifecycle_status(CHANNEL, {"state": "error"}, database=object(), expected_epoch=7)
        self.assertIs(caught.exception, error)


class InitialInventoryTests(unittest.TestCase):
    def test_untyped_zero_pages_keeps_old_shape_but_typed_zero_uses_stored_evidence(self):
        supplied = dependencies()
        for video_type in ("long", "short", "stream"):
            with self.subTest(video_type=video_type):
                self.assertEqual(initial.collect_initial_inventory_chunk(
                    CHANNEL, video_type=video_type, max_pages=0, **supplied, expected_epoch=7),
                    {"pages": 0, "items": 0, "outcome": "stored_inventory"})
        old = initial.collect_initial_inventory_chunk(CHANNEL, max_pages=0, **supplied, expected_epoch=7)
        self.assertEqual(old["longform_pages"], 0)
        self.assertEqual(old["outcome"], "stored_inventory")
        supplied["fetch_page"].assert_not_called()

    def test_long_missing_playlist_uses_shared_owner_and_zero_successful_pages(self):
        supplied = dependencies()
        summary = {"results": [{"channel_id": CHANNEL, "video_type": "long",
            "status": "playlist_not_found", "pages_processed": 0, "provider_attempts": 1}]}
        with patch.object(initial, "run_typed_inventory_batch", return_value=summary) as run:
            result = initial.collect_initial_inventory_chunk(CHANNEL, video_type="long", **supplied, expected_epoch=7)
            legacy = initial.collect_initial_inventory_chunk(CHANNEL, **supplied, expected_epoch=7)
        self.assertEqual((result["pages"], result["provider_attempts"], result["outcome"]),
                         (0, 1, "playlist_not_found"))
        self.assertEqual(legacy["longform_pages"], 0)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.kwargs["video_types"] == ("long",) for call in run.call_args_list))
        supplied["fetch_page"].assert_not_called()

    def test_typed_dispatch_keeps_addressed_owner_and_never_exports_its_cursor(self):
        supplied = dependencies()
        summary = {"results": [{"channel_id": CHANNEL, "video_type": "short", "status": "playlist_not_found",
                                "pages_processed": 0, "page_items": 3, "inserted": 1, "already_matching": 2,
                                "next_page_token": "private-scan-cursor"}]}
        with patch.object(initial, "run_typed_inventory_batch", return_value=summary) as run:
            result = initial.collect_initial_inventory_chunk(CHANNEL, video_type="short", **supplied, expected_epoch=7)
        self.assertEqual(result, dict(pages=0, items=3, inserted=1, updated=2, next_page_token=None, outcome="playlist_not_found",
                             provider_attempts=0, error_code=None, quota_exhausted=False))
        args = run.call_args.kwargs
        self.assertIs(args["repository"].database, supplied["database"])
        self.assertIs(args["fetch_page"], supplied["fetch_page"])
        self.assertEqual(args["worker_id"], "test-worker")
        self.assertEqual({key: args[key] for key in ("limit", "explicit_channel_ids", "video_types", "playlist_not_found_errors", "prepare", "expected_channel_scopes")},
                         dict(limit=1, explicit_channel_ids=[CHANNEL], video_types=("short",),
                              playlist_not_found_errors=(MissingPlaylist,), prepare=False,
                              expected_channel_scopes=[{"channel_id": CHANNEL, "control_epoch": 7}]))

    def test_typed_quota_errors_identity_and_empty_results_remain_distinct(self):
        supplied = dependencies()
        for summary, expected in (({"quota_exhausted": True, "errors": 1}, QuotaExceeded),
                                  ({"errors": 1}, RuntimeError),
                                  ({"results": [{"channel_id": "other", "video_type": "short"}]}, RuntimeError)):
            with self.subTest(summary=summary), patch.object(initial, "run_typed_inventory_batch", return_value=summary):
                with self.assertRaises(expected):
                    initial.collect_initial_inventory_chunk(CHANNEL, video_type="short", **supplied, expected_epoch=7)
        with patch.object(initial, "run_typed_inventory_batch", return_value={"results": []}):
            self.assertEqual(initial.collect_initial_inventory_chunk(CHANNEL, video_type="stream", **supplied, expected_epoch=7),
                             {"pages": 0, "items": 0, "outcome": "deferred"})

    def test_typed_cursor_rejection_and_disabled_gate_do_not_claim(self):
        supplied = dependencies()
        supplied["settings"] = CollectionSettings(True, False, True)
        with patch.object(initial, "run_typed_inventory_batch") as run:
            with self.assertRaisesRegex(ValueError, "cursor"):
                initial.collect_initial_inventory_chunk(CHANNEL, video_type="short", page_token="cursor", **supplied, expected_epoch=7)
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                initial.collect_initial_inventory_chunk(CHANNEL, video_type="short", **supplied, expected_epoch=7)
        run.assert_not_called()


class InitialStatisticsTests(unittest.TestCase):
    def setUp(self):
        profile = patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"})
        profile.start()
        self.addCleanup(profile.stop)

    def execute(self, rows, summary=None, stop=None, failure=None):
        gateway, events, settings = object(), [], CollectionSettings(True, True, True)
        repository = Mock()
        repository.load_recent_long_inventory.side_effect = lambda *a, **kw: events.append(("read", kw)) or rows
        repository.update_process_state.side_effect = lambda channel, values, *, expected_epoch: events.append(("state", values["initial_28d_stats_status"])) or True
        def collect(**arguments):
            events.append(("collect", arguments))
            if failure:
                raise failure
            return summary
        def copy_stop(target, result):
            events.append(("stop",))
            if stop:
                target.update(stop)
                return True
            return False
        with patch.object(initial, "ChannelSyncRepository", return_value=repository) as factory, \
             patch.object(lifecycle, "ChannelSyncRepository", new=factory), \
             patch("sys.stdout", new=io.StringIO()):
            result = initial.initial_28d_stats_import(
                CHANNEL, database=gateway, settings=settings,
                run_video_collection_queue_batch=collect, copy_collection_stop=copy_stop, expected_epoch=7)
        self.assertTrue(all(call.args == (gateway,) for call in factory.call_args_list))
        self.assertEqual(events[0][1]["limit"], 151)
        cutoff = datetime.fromisoformat(events[0][1]["published_at_gte"])
        self.assertLess(abs((datetime.now(timezone.utc) - cutoff).total_seconds() - 28 * 86400), 5)
        return result, events, gateway, settings

    def test_empty_and_over_limit_skip_without_collecting(self):
        for rows, expected in (([], "skipped_no_recent_videos"),
                               ([{"video_id": VIDEO}] * 151, "skipped_too_many_recent_videos")):
            with self.subTest(expected=expected):
                result, events, _, _ = self.execute(rows)
                self.assertEqual(result["status"], expected)
                self.assertEqual([event[0] for event in events], ["read", "state"])
                self.assertEqual(events[1][1], expected)

    def test_success_binds_same_scope_and_keeps_distinct_selection_projection(self):
        result, events, gateway, settings = self.execute(
            [{"video_id": VIDEO}, {"video_id": VIDEO}], {"claimed": 1, "updated": 1, "errors": 0})
        self.assertEqual(result["status"], "completed")
        self.assertEqual([event[0] for event in events], ["read", "state", "collect", "stop", "state"])
        self.assertEqual([event[1] for event in events if event[0] == "state"], ["running", "completed"])
        self.assertEqual(events[2][1], dict(limit=2, explicit_video_ids=[VIDEO, VIDEO], reason="initial_28d_import",
                                          force_metadata=True, database=gateway, settings=settings,
                                          channel_scopes=[{"channel_id": CHANNEL, "control_epoch": 7}]))
        self.assertEqual(result["sync_summary"]["explicit_selected"], 1)

    def test_stop_preserves_unknown_settlement_without_success_or_error_lifecycle_write(self):
        result, events, _, _ = self.execute(
            [{"video_id": VIDEO}], {"claimed": 1, "updated": 0, "settlement_unknown": True},
            stop={"settlement_unknown": True, "stopped_reason": "settlement_unknown"})
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["status"], "error")
        self.assertEqual([event[1] for event in events if event[0] == "state"], ["running"])

    def test_incomplete_and_exception_keep_error_lifecycle(self):
        result, events, _, _ = self.execute([{"video_id": VIDEO}], {"claimed": 1, "updated": 0, "errors": 0})
        self.assertEqual((result["status"], result["completion_outcome"], result["incomplete"]), ("error", "partial", True))
        self.assertEqual(events[-1], ("state", "error"))
        result, events, _, _ = self.execute([{"video_id": VIDEO}], failure=RuntimeError("provider failed"))
        self.assertEqual(result["error"], "provider failed")
        self.assertEqual(events[-1], ("state", "error"))


if __name__ == "__main__":
    unittest.main()
