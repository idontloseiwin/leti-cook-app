"""Execute real bootstrap statements with inert imports; no DB/provider or dotenv file.

The AST prefix preserves every top-level statement through the public gates,
including imports and core's optional dotenv bootstrap. The process composition
function is executed with the same import guard; no real client is constructed.
"""
import ast
import builtins
from pathlib import Path
import os
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
if str(APPLICATION) not in sys.path:
    sys.path.insert(0, str(APPLICATION))
from collection.settings import CollectionSettings, load_collection_settings
from collection_schedule import CollectionSchedules, ScheduleError
from runtime.collection_schedules import require_schedule_model


GATES = {
    "DATABASE_VIDEO_COLLECTION_ENABLED": "database_video_collection_enabled",
    "TYPED_PLAYLIST_INVENTORY_ENABLED": "typed_playlist_inventory_enabled",
    "YOUTUBE_BATCH_STATS_ENABLED": "youtube_batch_stats_enabled",
}


class Bootstrap:
    """Run the actual statement order, replacing only imported infrastructure."""

    def __init__(self, dotenv=None):
        self.dotenv = dotenv
        self.events, self.snapshots, self.modules, self.executing = [], [], {}, []
        self.database = object()
        self.schedules = CollectionSchedules()
        self.imported_settings = SimpleNamespace(
            CollectionSettings=CollectionSettings, load_collection_settings=self.load_settings,
        )

    def load_settings(self, environ):
        if environ is not os.environ:
            raise AssertionError("bootstrap must snapshot its applied process environment")
        owner = self.executing[-1]
        snapshot = load_collection_settings(environ)
        self.events.append(owner + ":settings")
        self.snapshots.append((owner, snapshot))
        return snapshot

    def apply_dotenv(self):
        self.events.append("core:dotenv")
        os.environ.update(self.dotenv)

    def import_module(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name.split(".")[0] in {
            "os", "re", "time", "collections", "datetime", "zoneinfo", "socket", "uuid", "contextlib",
            "youtube_errors", "youtube_playlist",
        }:
            return builtins.__import__(name, globals, locals, fromlist, level)
        if name == "collection.settings":
            return self.imported_settings
        if name == "dotenv":
            if self.dotenv is None:
                raise ImportError("synthetic absence of optional dotenv")
            return SimpleNamespace(load_dotenv=self.apply_dotenv)
        if name == "core":
            module = self.execute("core")
            # These function definitions follow the tested bootstrap prefix.
            for exported in (fromlist or ()):
                if not hasattr(module, exported):
                    setattr(module, exported, Mock(side_effect=AssertionError("provider body must not run")))
            self.events.append(self.executing[-1] + ":core_imported")
            return module
        stubs = {
            "load_process_schedules": lambda environ, **kwargs: self.schedules,
            "require_schedule_model": require_schedule_model,
            "load_database_config": lambda: object(),
            "load_hot_admission_config": lambda: SimpleNamespace(database_id="offline-installation"),
            "create_database_gateway": lambda *a, **kw: self.database,
            "YouTubeQuotaRepository": lambda *a, **kw: object(),
            "YouTubeQuotaAttemptRecorder": lambda *a, **kw: object(),
            "quota_journal_config": lambda **kw: (None, "synthetic"),
            "install_self_hosted_framework_logging": lambda: None,
            "INITIAL_28D_STATS_DAYS": 28,
            "INITIAL_28D_STATS_MAX_VIDEOS": 150,
        }
        return SimpleNamespace(**{
            exported: stubs.get(exported, Mock(side_effect=AssertionError("unexpected imported operation: " + name)))
            for exported in (fromlist or ())
        })

    def configuration(self):
        from runtime.process_collection import load_collection_policy
        path = APPLICATION / "runtime/process_collection.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "process_collection_configuration")
        namespace = dict(os=os, load_collection_settings=self.load_settings,
                         load_collection_policy=load_collection_policy,
                         __builtins__={**vars(builtins), "__import__": self.import_module})
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        self.executing.append("process_collection")
        try:
            return namespace["process_collection_configuration"]()
        finally:
            self.executing.pop()

    def execute(self, name):
        if name in self.modules:
            return self.modules[name]
        path = APPLICATION / (name + ".py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        required = {"YOUTUBE_BATCH_STATS_ENABLED"} if name == "core" else set(GATES)
        prefix, assigned = [], set()
        for node in tree.body:
            prefix.append(node)
            if isinstance(node, ast.Assign):
                assigned.update(target.id for target in node.targets if isinstance(target, ast.Name))
            if required <= assigned:
                break
        if not required <= assigned:
            raise AssertionError("public gate assignments missing: " + name)
        module = ModuleType("_settings_bootstrap_" + name)
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": self.import_module}
        self.executing.append(name)
        try:
            exec(compile(ast.Module(body=prefix, type_ignores=[]), str(path), "exec"), module.__dict__)
        finally:
            self.executing.pop()
        self.modules[name] = module
        return module


class CollectionSettingsBootstrapTests(unittest.TestCase):
    def test_optional_core_snapshot_does_not_authorize_collector_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            bootstrap = Bootstrap()
            bootstrap.schedules = None
            self.assertIsNone(bootstrap.execute("core").COLLECTION_SCHEDULES)
            with self.assertRaisesRegex(ScheduleError, '^schedule_snapshot_required$'):
                bootstrap.configuration()

    def test_core_applies_dotenv_before_loading_and_freezing_its_batch_alias(self):
        with patch.dict(os.environ, {"YOUTUBE_BATCH_STATS_ENABLED": "false"}, clear=True):
            bootstrap = Bootstrap(dotenv={"YOUTUBE_BATCH_STATS_ENABLED": " YeS "})
            core = bootstrap.execute("core")
            self.assertEqual(bootstrap.events, ["core:dotenv", "core:settings"])
            self.assertEqual(bootstrap.snapshots, [("core", CollectionSettings(False, False, True))])
            self.assertIs(core.YOUTUBE_BATCH_STATS_ENABLED, True)
            os.environ["YOUTUBE_BATCH_STATS_ENABLED"] = "false"
            self.assertIs(core.YOUTUBE_BATCH_STATS_ENABLED, True)

    def test_process_snapshots_all_gates_after_the_actual_core_bootstrap(self):
        updates = {"DATABASE_VIDEO_COLLECTION_ENABLED": " On ",
                   "TYPED_PLAYLIST_INVENTORY_ENABLED": "off", "YOUTUBE_BATCH_STATS_ENABLED": "YES"}
        with patch.dict(os.environ, dict.fromkeys(GATES, "false"), clear=True):
            bootstrap = Bootstrap(dotenv=updates)
            snapshot, policy = bootstrap.configuration()
            self.assertEqual(bootstrap.events, ["core:dotenv", "core:settings",
                "process_collection:core_imported", "process_collection:settings"])
            self.assertEqual([owner for owner, _ in bootstrap.snapshots], ["core", "process_collection"])
            self.assertIs(snapshot, bootstrap.snapshots[-1][1])
            self.assertEqual(snapshot, load_collection_settings(updates))
            self.assertIs(snapshot.youtube_batch_stats_enabled, bootstrap.modules["core"].YOUTUBE_BATCH_STATS_ENABLED)
            os.environ.update(dict.fromkeys(GATES, "false"))
            self.assertEqual(snapshot, CollectionSettings(True, False, True))

    def test_without_dotenv_process_and_core_use_the_same_loader_semantics(self):
        for values in ({}, dict.fromkeys(GATES, "true"), dict.fromkeys(GATES, "unexpected"),
                       dict(zip(GATES, ("1", " YeS ", "OFF")))):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                expected = load_collection_settings(values)
                bootstrap = Bootstrap()
                snapshot, policy = bootstrap.configuration()
                self.assertNotIn("core:dotenv", bootstrap.events)
                self.assertEqual([value for _, value in bootstrap.snapshots], [expected, expected])
                self.assertIs(snapshot, bootstrap.snapshots[-1][1])
                self.assertIs(bootstrap.modules["core"].YOUTUBE_BATCH_STATS_ENABLED,
                              expected.youtube_batch_stats_enabled)

    def test_only_the_shared_loader_reads_the_three_collection_gate_environment_keys(self):
        for filename in ("core.py", "runtime/process_collection.py", "runtime/collection.py"):
            tree = ast.parse((APPLICATION / filename).read_text(encoding="utf-8"))
            reads = []
            for node in ast.walk(tree):
                key = None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
                    receiver = ast.unparse(node.func.value)
                    if (receiver, node.func.attr) in {("os", "getenv"), ("os.environ", "get")}:
                        key = node.args[0]
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                    key = node.slice
                if isinstance(key, ast.Constant) and key.value in GATES:
                    reads.append((node.lineno, key.value))
            self.assertEqual(reads, [], filename)

    def test_policy_snapshot_preserves_existing_defaults_bounds_and_does_not_read_global_environment(self):
        from runtime.process_collection import load_collection_policy
        with patch.dict(os.environ, {"DATABASE_CHANNEL_DISCOVERY_ENABLED": "true",
                                    "SYNC_RUN_STALE_AFTER_HOURS": "168"}, clear=True):
            policy = load_collection_policy({})
            self.assertEqual(policy.sync_run_stale_after_hours, 24)
            self.assertFalse(policy.database_channel_discovery_enabled)
            for value, expected in (("", 24), ("invalid", 24), ("0", 1), ("-5", 1), ("12", 12), ("999", 168)):
                self.assertEqual(load_collection_policy({"SYNC_RUN_STALE_AFTER_HOURS": value}).sync_run_stale_after_hours, expected)
            values = {"DATABASE_CHANNEL_DISCOVERY_ENABLED": " On ",
                      "SHORT_VIDEO_COLLECTION_ENABLED": "yes", "COMPLETED_STREAM_COLLECTION_ENABLED": "false"}
            policy = load_collection_policy(values)
            values.clear()
            self.assertTrue(policy.database_channel_discovery_enabled)
            self.assertTrue(policy.short_video_collection_enabled)
            self.assertFalse(policy.completed_stream_collection_enabled)


if __name__ == "__main__":
    unittest.main()
