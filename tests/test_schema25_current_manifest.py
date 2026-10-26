"""Current-suite manifest rejects missing, changed or miswired regression data."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
from schema25_regressions import fixtures, manifest, suite


# Independent ownership inventory: retaining the same counts is not sufficient.
EXPECTED_CASE_MODULES = {
    "availability_recheck": "cases_retention",
    "restore_merge": "cases_user_work",
    "cadence_policy": "cases_policy",
    "playlist_absence": "cases_retention",
    "quota_phase_labels": "cases_retention",
    "removal_stops_job": "cases_jobs",
    "removed_job_scan_cleanup": "cases_jobs",
    "failed_job_backoff": "cases_jobs",
    "retention_scrub": "cases_retention",
    "detached_reviews": "cases_retention",
    "operational_retention": "cases_retention",
    "inventory_window": "cases_retention",
    "saved_noop_revision": "cases_user_work",
    "folder_label_management": "cases_user_work",
    "runtime_readiness": "cases_readiness",
    "negative_intrinsic_guard": "cases_readiness",
    "trashed_channel_removal": "cases_retention",
    "inventory_queue_seek": "cases_retention",
    "receipt_scan_index": "cases_retention",
}
EXPECTED_CASE_FIXTURES = {
    "availability_recheck": ("availability_recheck_seed.sql",),
    "restore_merge": (
        "restore_merge_expression.sql", "restore_parent_cycle_expression.sql",
    ),
    "cadence_policy": (),
    "playlist_absence": (),
    "quota_phase_labels": (),
    "removal_stops_job": (),
    "removed_job_scan_cleanup": (),
    "failed_job_backoff": (),
    "retention_scrub": ("review_retention_seed.sql",),
    "detached_reviews": (),
    "operational_retention": ("operational_retention_seed.sql",),
    "inventory_window": ("inventory_window_seed.sql",),
    "saved_noop_revision": (),
    "folder_label_management": (
        "labels_renamed_preset_expression.sql",
        "labels_deleted_preset_expression.sql",
        "labels_invalid_version_expression.sql",
    ),
    "runtime_readiness": (),
    "negative_intrinsic_guard": (),
    "trashed_channel_removal": ("trashed_channel_seed.sql",),
    "inventory_queue_seek": ("inventory_queue_seed.sql",),
    "receipt_scan_index": (),
}
MANIFEST_PATH = Path("tests/schema25_regressions/manifest.json")
FIXTURE_PATH = Path("tests/schema25_regressions/data")


class CurrentManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="schema25-current-manifest-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Deliberately copy no application schema, migrations or historical tree.
        for directory in (Path("tests/sql"), FIXTURE_PATH):
            target = self.root / directory
            target.mkdir(parents=True)
            for source in (ROOT / directory).iterdir():
                if source.is_file():
                    shutil.copyfile(source, target / source.name)
        shutil.copyfile(ROOT / MANIFEST_PATH, self.root / MANIFEST_PATH)
        self.data = json.loads((self.root / MANIFEST_PATH).read_text(encoding="utf-8"))

    def write_manifest(self):
        (self.root / MANIFEST_PATH).write_text(json.dumps(self.data), encoding="utf-8")

    def reject_manifest(self, code):
        self.write_manifest()
        with self.assertRaisesRegex(AssertionError, "^" + code + "$"):
            manifest.load(self.root)

    def reject_dispatch(self, cases, code):
        data = manifest.load(self.root)
        with patch.object(fixtures, "Fixture") as database_fixture:
            with self.assertRaisesRegex(AssertionError, "^" + code + "$"):
                manifest.validate_dispatch(data, cases)
            database_fixture.assert_not_called()

    def test_current_only_tree_loads_without_schema_migrations_or_oracle(self):
        self.assertFalse((self.root / "schema").exists())
        self.assertFalse((self.root / "tests/direct_pg").exists())
        data = manifest.load(self.root)
        self.assertEqual(data["format"], 2)
        self.assertNotIn("historical", data)
        self.assertEqual(data, self.data)
        manifest.validate_dispatch(data, suite.CASES)

    def test_exact_independent_case_and_fixture_ownership(self):
        data = manifest.load(self.root)
        self.assertEqual(
            {name: entry["callable"] for name, entry in data["python"].items()},
            {name: module + "." + name for name, module in EXPECTED_CASE_MODULES.items()},
        )
        self.assertEqual(
            {name: tuple(entry["fixtures"]) for name, entry in data["python"].items()},
            EXPECTED_CASE_FIXTURES,
        )
        expected = {
            name: {"kind": "seed" if name.endswith("_seed.sql") else "expression", "cases": [case]}
            for case, names in EXPECTED_CASE_FIXTURES.items() for name in names
        }
        self.assertEqual(len(expected), 11)
        self.assertEqual(
            {name: {"kind": entry["kind"], "cases": entry["cases"]}
             for name, entry in data["fixtures"].items()}, expected,
        )
        self.assertEqual(sum(entry["kind"] == "seed" for entry in data["fixtures"].values()), 6)

    def test_every_current_fixture_loads_with_its_declared_kind(self):
        data = manifest.load(self.root)
        for name, entry in data["fixtures"].items():
            with self.subTest(name=name):
                self.assertEqual(
                    manifest.load_fixture(name, kind=entry["kind"], root=self.root),
                    (self.root / FIXTURE_PATH / name).read_text(encoding="utf-8"),
                )

    def test_missing_fixture_file_is_rejected(self):
        (self.root / FIXTURE_PATH / "availability_recheck_seed.sql").unlink()
        self.reject_manifest("fixture_missing")

    def test_extra_unlisted_fixture_is_rejected(self):
        (self.root / FIXTURE_PATH / "unlisted.sql").write_text("SELECT 1", encoding="utf-8")
        self.reject_manifest("fixture_manifest_coverage")

    def test_changed_fixture_bytes_are_rejected(self):
        target = self.root / FIXTURE_PATH / "availability_recheck_seed.sql"
        target.write_bytes(target.read_bytes() + b"\n-- changed fixture\n")
        self.reject_manifest("fixture_digest")

    def test_swapped_fixture_contents_are_rejected(self):
        first = self.root / FIXTURE_PATH / "restore_merge_expression.sql"
        second = self.root / FIXTURE_PATH / "restore_parent_cycle_expression.sql"
        first_raw, second_raw = first.read_bytes(), second.read_bytes()
        self.assertNotEqual(first_raw, second_raw)
        first.write_bytes(second_raw)
        second.write_bytes(first_raw)
        self.reject_manifest("fixture_digest")

    def test_fixture_removed_from_manifest_but_referenced_is_rejected(self):
        del self.data["fixtures"]["availability_recheck_seed.sql"]
        self.reject_manifest("case_fixture_missing")

    def test_missing_forward_fixture_binding_is_rejected(self):
        self.data["python"]["availability_recheck"]["fixtures"] = []
        self.reject_manifest("fixture_consumers")

    def test_missing_inverse_fixture_binding_is_rejected(self):
        self.data["fixtures"]["availability_recheck_seed.sql"]["cases"] = []
        self.reject_manifest("fixture_consumers")

    def test_wrong_inverse_fixture_binding_is_rejected(self):
        self.data["fixtures"]["availability_recheck_seed.sql"]["cases"] = ["restore_merge"]
        self.reject_manifest("fixture_consumers")

    def test_duplicate_forward_fixture_binding_is_rejected(self):
        self.data["python"]["availability_recheck"]["fixtures"] *= 2
        self.reject_manifest("case_fixture_duplicate")

    def test_sql_parent_path_escape_is_rejected(self):
        entry = self.data["sql"].pop("01_contract_and_privileges.sql")
        self.data["sql"]["../01_contract_and_privileges.sql"] = entry
        # Coverage rejects this before the per-file path check or any read.
        self.reject_manifest("sql_manifest_coverage")

    def test_fixture_parent_path_escape_is_rejected(self):
        name = "availability_recheck_seed.sql"
        escaped = "../" + name
        self.data["fixtures"][escaped] = self.data["fixtures"].pop(name)
        self.data["python"]["availability_recheck"]["fixtures"] = [escaped]
        self.reject_manifest("fixture_path")

    def test_resolved_sql_path_outside_tree_is_rejected(self):
        original_resolve = Path.resolve
        target = self.root / "tests/sql/01_contract_and_privileges.sql"
        def resolve(path, *args, **kwargs):
            if path == target:
                return self.root.parent / "outside-schema25-fixture.sql"
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            self.reject_manifest("sql_escape")

    def test_resolved_fixture_path_outside_tree_is_rejected(self):
        original_resolve = Path.resolve
        target = self.root / FIXTURE_PATH / "availability_recheck_seed.sql"
        def resolve(path, *args, **kwargs):
            if path == target:
                return self.root.parent / "outside-schema25-fixture.sql"
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            self.reject_manifest("fixture_escape")

    def test_loader_rejects_wrong_kind(self):
        for name, kind in (("availability_recheck_seed.sql", "expression"),
                           ("restore_merge_expression.sql", "seed")):
            with self.subTest(name=name):
                with self.assertRaisesRegex(AssertionError, "^fixture_kind$"):
                    manifest.load_fixture(name, kind=kind, root=self.root)

    def test_loader_rejects_unknown_fixture(self):
        with self.assertRaisesRegex(AssertionError, "^fixture_name$"):
            manifest.load_fixture("unknown.sql", kind="seed", root=self.root)

    def test_missing_dispatch_case_is_rejected_before_fixture(self):
        cases = dict(suite.CASES)
        del cases["availability_recheck"]
        self.reject_dispatch(cases, "exact_dispatch")

    def test_extra_dispatch_case_is_rejected_before_fixture(self):
        cases = dict(suite.CASES)
        cases["extra_case"] = lambda fixture: None
        self.reject_dispatch(cases, "exact_dispatch")

    def test_swapped_dispatch_callables_are_rejected_before_fixture(self):
        cases = dict(suite.CASES)
        cases["availability_recheck"], cases["restore_merge"] = cases["restore_merge"], cases["availability_recheck"]
        self.reject_dispatch(cases, "case_binding")

    def test_duplicate_dispatch_callable_is_rejected_before_fixture(self):
        cases = dict(suite.CASES)
        cases["availability_recheck"] = cases["restore_merge"]
        self.reject_dispatch(cases, "case_binding")

    def test_non_callable_dispatch_value_is_rejected_before_fixture(self):
        cases = dict(suite.CASES)
        cases["availability_recheck"] = None
        self.reject_dispatch(cases, "case_binding")

    def test_replacement_noop_callable_is_rejected_before_fixture(self):
        cases = dict(suite.CASES)
        cases["availability_recheck"] = lambda fixture: None
        self.reject_dispatch(cases, "case_binding")

    def test_manifest_cannot_bind_case_to_other_named_function(self):
        self.data["python"]["availability_recheck"]["callable"] = "cases_user_work.restore_merge"
        self.reject_manifest("case_binding")


if __name__ == "__main__":
    unittest.main()
