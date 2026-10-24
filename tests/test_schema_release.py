"""Release schema identity, generated metadata and fail-closed runtime admission."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from schema_release import SchemaRelease, load_schema_release, parse_schema_release
import check_schema_release as generator
from db.base.postgres import DatabaseError
from db.hot.hot_runtime import MaintenanceRepository
from db.hot.hot_retention import RetentionError
from db.readiness import capabilities, schema_release_capabilities as release
from hot_runtime import HotRuntimeController, HotRuntimeUnavailable
from test_capabilities import FactsDatabase, valid_release_facts, reviewed_chain_facts
from test_runtime_gates import Time


def encode(value):
    return json.dumps(value).encode("utf-8")


class SchemaReleaseMetadataTests(unittest.TestCase):
    def test_generated_file_exactly_matches_manifest(self):
        expected = generator.check(ROOT)
        self.assertEqual(expected, (ROOT / "application/schema-release.json").read_bytes())
        identity = load_schema_release()
        self.assertEqual(identity, SchemaRelease("leti-cook-release-01", 34))
        self.assertEqual(parse_schema_release(expected).document(), identity.document())

    def test_runtime_file_rejects_duplicate_keys_unknown_fields_and_wrong_types(self):
        good = SchemaRelease("leti-cook-release-01", 25).document()
        invalid = [b"", b"x" * 4097, b"\xff", b"null", b"[]",
                   b'{"format":"leti-cook-schema-release","format":"leti-cook-schema-release"}',
                   encode({**good, "private": "unexpected"})]
        for field, alternatives in {
            "format": [None, "old-schema", 1],
            "format_version": [True, 1.0, 0, 2],
            "baseline_id": [None, "", "other baseline", "A", 25],
            "schema_version": [None, True, "25", 25.0, 0, -1, 1000001],
        }.items():
            invalid += [encode({**good, field: item}) for item in alternatives]
            invalid.append(encode({key: item for key, item in good.items() if key != field}))
        for data in invalid:
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "schema_release_metadata_invalid"):
                parse_schema_release(data)
        with self.assertRaisesRegex(ValueError, "schema_release_metadata_invalid"):
            parse_schema_release(encode(good).decode())

    def test_regeneration_is_explicit_and_never_stamps_a_database(self):
        manifest = {"format": "leti-cook-self-hosted-schema", "version": 1,
                    "baseline_id": "leti-cook-release-01", "schema_version": 26}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "schema").mkdir()
            (root / "application").mkdir()
            (root / "schema/manifest.json").write_bytes(encode(manifest))
            target = root / "application/schema-release.json"
            with self.assertRaisesRegex(ValueError, "out_of_date"):
                generator.check(root)
            self.assertFalse(target.exists())
            generator.check(root, write=True)
            self.assertEqual(load_schema_release(target).schema_version, 26)
            before = target.read_bytes()
            manifest["schema_version"] = 27
            (root / "schema/manifest.json").write_bytes(encode(manifest))
            with self.assertRaisesRegex(ValueError, "out_of_date"):
                generator.check(root)
            self.assertEqual(target.read_bytes(), before)
            generator.check(root, write=True)
            self.assertEqual(load_schema_release(target).schema_version, 27)
            self.assertEqual(generator.check(root), target.read_bytes())

    def test_manifest_identity_is_required_and_duplicate_keys_rejected(self):
        good = {"format": "leti-cook-self-hosted-schema", "version": 1,
                "baseline_id": "leti-cook-release-01", "schema_version": 25}
        invalid = [encode({key: value for key, value in good.items() if key != "baseline_id"}),
                   encode({**good, "schema_version": True}),
                   encode({**good, "version": True}),
                   b'{"baseline_id":"one","baseline_id":"two"}', b"x" * 262145]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "schema").mkdir()
            for raw in invalid:
                (root / "schema/manifest.json").write_bytes(raw)
                with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                    generator.expected_bytes(root)


class SchemaReleaseAdmissionTests(unittest.TestCase):
    def setUp(self):reviewed_chain_facts(self)

    def test_exact_baseline_and_global_version_are_required_not_a_minimum(self):
        good = valid_release_facts()
        for field, values in (("baseline_id", ("other-release", "leti-cook-release-02")),
                              ("current_version", (good['current_version'] - 1, good['current_version'] + 1))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(DatabaseError, "schema_release_mismatch"):
                    release.validate_readiness({**good, field: value})
        self.assertEqual(release.validate_readiness(good), load_schema_release())

    def test_shape_acl_and_missing_metadata_all_fail_closed(self):
        good = valid_release_facts()
        bad = [None, {}, [], {**good, "extra": True}, {**good, "current_version": True},
               {**good, "current_version": "25"}, {**good, "baseline_id": None}]
        for field in ("view_valid", "ledger_private"):
            bad.extend(({**good, field: None}, {**good, field: 1}))
            with self.assertRaisesRegex(DatabaseError, "schema_release_acl_invalid"):
                release.validate_readiness({**good, field: False})
        for row in bad:
            with self.subTest(row=row), self.assertRaisesRegex(DatabaseError, "schema_release_result_invalid"):
                release.validate_readiness(row)
        for error in (FileNotFoundError(), ValueError("invalid")):
            with patch.object(release, "load_schema_release", side_effect=error):
                with self.assertRaisesRegex(DatabaseError, "schema_release_metadata_invalid"):
                    release.validate_readiness(good)

    def test_every_collection_entry_rejects_wrong_release_before_domain_queries(self):
        for name, check in capabilities.CHECKS.items():
            for bad in ({"current_version": load_schema_release().schema_version - 1},
                        {"current_version": load_schema_release().schema_version + 1},
                        {"baseline_id": "other-release"}):
                with self.subTest(name=name, bad=bad):
                    db = FactsDatabase()
                    db.overrides["schema_release"] = {**valid_release_facts(), **bad}
                    with self.assertRaisesRegex(DatabaseError, "schema_release_mismatch"):
                        check(db)
                    self.assertEqual([call[0] for call in db.calls], ["capability_schema_release"])

    def test_good_version_does_not_replace_existing_structure_acl_checks(self):
        for field in ("roles_valid", "functions_valid", "triggers_valid"):
            db = FactsDatabase()
            db.overrides["final_schema"] = {name: name != field for name in
                                            ("roles_valid", "functions_valid", "triggers_valid")}
            with self.assertRaises(DatabaseError):
                capabilities.assert_typed_inventory_runtime_schema(db)
            self.assertEqual([call[0] for call in db.calls],
                             ["capability_schema_release", "capability_final_schema"])

    def test_release_check_is_per_invocation_with_no_old_success_cache(self):
        db = FactsDatabase()
        capabilities.assert_inventory_switch_schema(db)
        db.overrides["schema_release"] = {**valid_release_facts(),
                                         "current_version": load_schema_release().schema_version + 1}
        with self.assertRaisesRegex(DatabaseError, "schema_release_mismatch"):
            capabilities.assert_inventory_switch_schema(db)
        self.assertEqual(sum(call[0] == "capability_schema_release" for call in db.calls), 2)
        self.assertEqual(db.calls[-1][0], "capability_schema_release")

    def test_hot_contract_shape_and_version_match_preserve_all_runtime_gates(self):
        db = FactsDatabase()
        as_of = db.hot["value"]["as_of"]
        db.read_startup_expiry = Mock(side_effect=lambda **args: {
            "as_of": as_of, "cutoff": args["cutoff"].isoformat(), "expired": False})
        cleanup, caches = Mock(return_value={"status": "current"}), Mock()
        controller = HotRuntimeController(MaintenanceRepository(db), local_cleanup=cleanup,
            clear_caches=caches, clock=lambda: datetime.fromisoformat(as_of).timestamp(),
            monotonic=lambda: 100.0, time_trust=Time())
        controller.initialize()
        for gate in (controller.require_ready, controller.require_collection_ready, controller.require_database_ready):
            gate()
        self.assertEqual(set(capabilities.get_hot_runtime_contract(db)), {"version", "schema_ready", "as_of"})
        cleanup.assert_not_called()
        db.read_startup_expiry.assert_called_once()

    def test_mismatched_release_blocks_startup_before_expiry_or_cleanup(self):
        for bad in ({"current_version": load_schema_release().schema_version - 1},
                    {"current_version": load_schema_release().schema_version + 1},
                    {"baseline_id": "other-release"}):
            with self.subTest(bad=bad):
                db = FactsDatabase()
                db.overrides["schema_release"] = {**valid_release_facts(), **bad}
                db.read_startup_expiry = Mock(side_effect=AssertionError("must not inspect data"))
                cleanup, caches = Mock(), Mock()
                repo = MaintenanceRepository(db)
                with self.assertRaisesRegex(RetentionError, "runtime_contract_schema_invalid"):
                    repo.contract()
                db.calls.clear()
                controller = HotRuntimeController(repo, local_cleanup=cleanup, clear_caches=caches,
                    clock=lambda: datetime.fromisoformat(db.hot["value"]["as_of"]).timestamp(),
                    monotonic=lambda: 100.0, time_trust=Time())
                controller.initialize()
                for gate in (controller.require_ready, controller.require_collection_ready, controller.require_database_ready):
                    with self.assertRaises(HotRuntimeUnavailable):
                        gate()
                self.assertEqual(db.calls, [("maintenance", "hot_runtime"), ("maintenance", "schema_release")])
                db.read_startup_expiry.assert_not_called()
                cleanup.assert_not_called()
                caches.assert_not_called()


if __name__ == "__main__":
    unittest.main()
