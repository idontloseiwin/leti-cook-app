"""Single durable engine modes and the deprecated legacy alias, offline."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ast
import io
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
import operator_jobs


class OperatorModeTests(unittest.TestCase):
    def setUp(self):
        reset = patch.object(operator_jobs, "_LEGACY_MODE_WARNED", False)
        reset.start(); self.addCleanup(reset.stop)

    def test_missing_none_empty_and_whitespace_default_to_durable(self):
        for config in ({}, {"OPERATOR_JOBS_MODE": None}, {"OPERATOR_JOBS_MODE": ""},
                       {"OPERATOR_JOBS_MODE": " \t\n "}):
            with self.subTest(config=config), patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(operator_jobs.operator_jobs_mode(config), "durable")
                self.assertEqual(error.getvalue(), "")

    def test_explicit_modes_normalize_case_and_outer_whitespace(self):
        for supplied, expected in (("durable", "durable"), (" DURABLE ", "durable"),
                                   ("drain", "drain"), (" DrAiN ", "drain")):
            self.assertEqual(operator_jobs.operator_jobs_mode({"OPERATOR_JOBS_MODE": supplied}), expected)

    def test_legacy_is_same_engine_with_one_clear_warning(self):
        with patch("sys.stderr", new_callable=io.StringIO) as error:
            for supplied in ("legacy", " LEGACY ", "legacy"):
                self.assertEqual(operator_jobs.operator_jobs_mode({"OPERATOR_JOBS_MODE": supplied}), "durable")
        self.assertEqual(error.getvalue().count("OPERATOR_JOBS_MODE=legacy is deprecated"), 1)
        self.assertIn("using durable jobs", error.getvalue())
        self.assertIn("Set OPERATOR_JOBS_MODE=durable", error.getvalue())

    def test_concurrent_alias_resolution_warns_only_once(self):
        with patch("sys.stderr", new_callable=io.StringIO) as error, ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: operator_jobs.operator_jobs_mode({"OPERATOR_JOBS_MODE": "legacy"}), range(32)))
        self.assertEqual(results, ["durable"] * 32)
        self.assertEqual(len(error.getvalue().splitlines()), 1)

    def test_invalid_modes_and_nonstrings_fail_closed(self):
        for value in ("disabled", "off", "legacy2", "durable drain", True, False, 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                operator_jobs.operator_jobs_mode({"OPERATOR_JOBS_MODE": value})

    def test_explicit_mapping_never_falls_back_to_ambient_legacy(self):
        with patch.dict(os.environ, {"OPERATOR_JOBS_MODE": "legacy"}), patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(operator_jobs.operator_jobs_mode({}), "durable")
            self.assertEqual(error.getvalue(), "")
            self.assertEqual(operator_jobs.operator_jobs_mode(), "durable")
        self.assertIn("deprecated", error.getvalue())

    def test_worker_no_longer_disables_legacy_alias(self):
        # Execute only the worker's preflight prefix, with the collection gate
        # disabled so no repository/provider resource is opened.
        tree = ast.parse((ROOT / "application/scripts/run_operator_job_worker.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        ns = dict(DEFAULT_AUTO_SYNC_INTERVAL_MINUTES=30, operator_jobs_mode=operator_jobs.operator_jobs_mode,
                  load_collection_settings=Mock(return_value=SimpleNamespace(database_video_collection_enabled=False)),
                  os=os, json=json, sys=sys)
        exec(compile(ast.fix_missing_locations(module), "worker-mode-prefix", "exec"), ns)
        for mode in ("legacy", "durable", "drain"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"OPERATOR_JOBS_MODE": mode}), \
                    patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(ns["main"](), 2)
                self.assertNotIn('"disabled"', output.getvalue())
                self.assertIn("database_video_collection_disabled", error.getvalue())

    def test_cli_drain_flag_remains_independent_of_environment_mode(self):
        import argparse
        tree = ast.parse((ROOT / "application/scripts/run_operator_job_worker.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser")
        ns = dict(argparse=argparse, DEFAULT_AUTO_SYNC_INTERVAL_MINUTES=30,
                  SUPPORTED_AUTO_SYNC_INTERVAL_MINUTES=(6, 10, 15, 20, 30))
        exec(compile(ast.Module(body=[function], type_ignores=[]), "worker-parser", "exec"), ns)
        for mode in ("durable", "drain", "legacy"):
            with patch.dict(os.environ, {"OPERATOR_JOBS_MODE": mode}):
                self.assertTrue(ns["build_parser"]().parse_args(["--drain"]).drain)
                self.assertFalse(ns["build_parser"]().parse_args([]).drain)


if __name__ == "__main__": unittest.main()
