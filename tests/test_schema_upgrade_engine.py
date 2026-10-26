"""Offline contracts for administrative schema steps; native PG tests are separate."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.schema_manifest import (
    BASELINE_ID, MAX_STEP_BYTES, SchemaError, validate_schema_bundle, validate_schema_state,
)
from ops import schema_upgrade as runner


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def source_files():
    paths = ("schema/manifest.json", "schema/schema.sql", "schema/baseline/schema.sql",
             "schema/configuration-seeds.sql", "application/schema-release.json")
    files = {name: (ROOT / name).read_bytes() for name in paths}
    for step in json.loads(files['schema/manifest.json'])['steps']:
        name = 'schema/' + step['file']
        files[name] = (ROOT / name).read_bytes()
    return files


def fixture(steps=1):
    files = source_files()
    manifest = json.loads(files["schema/manifest.json"])
    # Synthetic engine cases start from the immutable baseline, independently
    # of however many real release steps the checkout now carries.
    for step in manifest['steps']:
        files.pop('schema/' + step['file'])
        manifest['files'].pop(step['file'])
    manifest['steps'] = []
    files['schema/schema.sql'] = files['schema/baseline/schema.sql']
    manifest['files']['schema.sql'] = digest(files['schema/schema.sql'])
    target = 25 + steps
    manifest["schema_version"] = target
    for version in range(26, target + 1):
        name = f"steps/{version:04d}-synthetic.sql"
        sql = f"CREATE TABLE public.synthetic_{version} (id integer PRIMARY KEY);\n".encode()
        files["schema/" + name] = sql
        manifest["files"][name] = digest(sql)
        manifest["steps"].append({"version": version, "file": name, "sha256": digest(sql)})
    # Native tests own independent fresh SQL. This fixture only tests the byte contract.
    if steps:
        files["schema/schema.sql"] += b"-- synthetic target, not an executable native fixture\n"
        manifest["files"]["schema.sql"] = digest(files["schema/schema.sql"])
    files["schema/manifest.json"] = encoded(manifest)
    runtime = json.loads(files["application/schema-release.json"])
    runtime["schema_version"] = target
    files["application/schema-release.json"] = encoded(runtime)
    return files


def state(plan, *, initial=25, current=None):
    current = plan.schema_version if current is None else current
    return {"baseline_id": BASELINE_ID, "initial_version": initial, "current_version": current,
            "applied": [{"version": step.version, "filename": step.filename, "sha256": step.sha256}
                        for step in plan.steps if initial < step.version <= current]}


class SchemaManifestTests(unittest.TestCase):
    def test_current_release_preserves_the_baseline_and_carries_real_steps(self):
        plan = validate_schema_bundle(source_files())
        self.assertEqual((plan.baseline_id, plan.baseline_version, plan.schema_version),
                         (BASELINE_ID, 25, 38))
        self.assertEqual(plan.baseline_sha256,
                         'c349be588ffd48cf170095755aeb96145e4a9371ecf559cbf4566145a0f4c8ec')
        self.assertEqual([step.filename for step in plan.steps],
                         ['steps/0026-release-readiness.sql',
                          'steps/0027-data-source-provenance.sql',
                          'steps/0028-unified-video-availability.sql',
                          'steps/0029-automatic-long-short-policy.sql',
                          'steps/0030-automatic-type-reader-permissions.sql',
                          'steps/0031-publication-lifecycle.sql',
                          'steps/0032-automatic-type-read-plan.sql',
                          'steps/0033-health-queue-projections.sql',
                          'steps/0034-all-type-queue-health.sql',
                          'steps/0035-research-read-model.sql',
                          'steps/0036-inventory-receipt-scan-index.sql',
                          'steps/0037-type-resolution-membership-index.sql',
                          'steps/0038-user-work-limit-lock.sql'])
        self.assertNotEqual(plan.baseline_sql, plan.schema_sql)
        baseline = validate_schema_bundle(fixture(0))
        self.assertEqual(baseline.steps, ())
        self.assertEqual(baseline.schema_sql, baseline.baseline_sql)

    def test_plan_and_steps_capture_immutable_bytes(self):
        files = fixture(2)
        plan = validate_schema_bundle(files)
        original = plan.steps[0].sql
        files["schema/steps/0026-synthetic.sql"] = b"changed"
        self.assertEqual(plan.steps[0].sql, original)
        with self.assertRaises(FrozenInstanceError):
            plan.schema_version = 99
        with self.assertRaises(FrozenInstanceError):
            plan.steps[0].filename = "other"

    def test_manifest_rejects_unknown_duplicate_and_wrong_scalar_fields(self):
        mutations = (
            lambda m: m.update(extra=1), lambda m: m.update(schema_version=True),
            lambda m: m.update(schema_version=24), lambda m: m.update(baseline_version=1),
            lambda m: m.update(baseline_id="other"), lambda m: m.update(version=True),
            lambda m: m.update(postgres_major=18), lambda m: m.update(api_data_rows=False),
            lambda m: m.update(user_work_rows=1), lambda m: m.update(fresh_database_only=False),
            lambda m: m.update(steps={}), lambda m: m.update(configuration_seed_counts={"unsafe-name": 1}),
        )
        for mutation in mutations:
            files = fixture()
            value = json.loads(files["schema/manifest.json"])
            mutation(value); files["schema/manifest.json"] = encoded(value)
            with self.subTest(value=value), self.assertRaises(SchemaError):
                validate_schema_bundle(files)
        files = fixture()
        files["schema/manifest.json"] = b'{"version":1,"version":1}'
        with self.assertRaisesRegex(SchemaError, "schema_manifest_invalid"):
            validate_schema_bundle(files)

    def test_missing_extra_duplicate_and_nonconsecutive_steps_fail(self):
        mutations = (
            lambda m: m["steps"].clear(),
            lambda m: m["steps"].append(deepcopy(m["steps"][0])),
            lambda m: m["steps"][0].update(version=27),
            lambda m: m["steps"][0].update(file="steps/../0026-test.sql"),
            lambda m: m["steps"][0].update(file="steps/0026-Upper.sql"),
            lambda m: m["steps"][0].update(unknown=1),
        )
        for mutation in mutations:
            files = fixture()
            manifest = json.loads(files["schema/manifest.json"])
            mutation(manifest); files["schema/manifest.json"] = encoded(manifest)
            with self.subTest(mutation=mutation), self.assertRaises(SchemaError):
                validate_schema_bundle(files)
        for name in ("schema/steps/0026-synthetic.sql", "schema/baseline/schema.sql"):
            files = fixture(); del files[name]
            with self.assertRaises(SchemaError): validate_schema_bundle(files)
        files = fixture(); files["schema/steps/0027-hidden.sql"] = b"SELECT 1;"
        with self.assertRaisesRegex(SchemaError, "schema_file_set_invalid"):
            validate_schema_bundle(files)

    def test_every_sql_digest_is_checked(self):
        for name in fixture():
            if not name.endswith(".sql"): continue
            files = fixture(); files[name] += b"--changed"
            with self.subTest(name=name), self.assertRaisesRegex(SchemaError, "digest"):
                validate_schema_bundle(files)
        files = fixture(); manifest = json.loads(files["schema/manifest.json"])
        manifest["steps"][0]["sha256"] = "0" * 64
        files["schema/manifest.json"] = encoded(manifest)
        with self.assertRaisesRegex(SchemaError, "schema_step_digest_invalid"):
            validate_schema_bundle(files)

    def test_baseline_drift_without_a_step_fails_even_with_matching_hash(self):
        files = fixture(0); manifest = json.loads(files["schema/manifest.json"])
        files["schema/schema.sql"] += b"-- drift\n"
        manifest["files"]["schema.sql"] = digest(files["schema/schema.sql"])
        files["schema/manifest.json"] = encoded(manifest)
        with self.assertRaisesRegex(SchemaError, "schema_baseline_mismatch"):
            validate_schema_bundle(files)

    def test_runtime_metadata_is_required_and_must_match(self):
        for data in (None, b"{}", encoded({"format":"leti-cook-schema-release", "format_version":1,
                                          "baseline_id":BASELINE_ID,"schema_version":25})):
            files = fixture()
            if data is None: del files["application/schema-release.json"]
            else: files["application/schema-release.json"] = data
            with self.subTest(data=data), self.assertRaisesRegex(SchemaError, "schema_runtime_metadata"):
                validate_schema_bundle(files)

    def test_sql_is_bounded_utf8_and_contains_no_nul_or_bom(self):
        for data in (b"", b"\0", b"\xff", b"\xef\xbb\xbfSELECT 1;", b"x" * (MAX_STEP_BYTES + 1)):
            files = fixture(); manifest = json.loads(files["schema/manifest.json"])
            files["schema/steps/0026-synthetic.sql"] = data
            manifest["files"]["steps/0026-synthetic.sql"] = digest(data)
            manifest["steps"][0]["sha256"] = digest(data)
            files["schema/manifest.json"] = encoded(manifest)
            with self.subTest(length=len(data)), self.assertRaisesRegex(SchemaError, "schema_sql_invalid"):
                validate_schema_bundle(files)

    def test_directly_constructed_invalid_plan_is_refused_before_command(self):
        plan = validate_schema_bundle(fixture())
        for bad in (replace(plan, schema_version=25), replace(plan, baseline_id="other"),
                    replace(plan, steps=(replace(plan.steps[0], sql=b"COMMIT;"),)),
                    replace(plan, schema_version=True)):
            command = Mock()
            with self.assertRaises(SchemaError):
                runner.run_schema_upgrade(bad, expected_current_version=25, command=command)
            command.assert_not_called()


class SchemaStateTests(unittest.TestCase):
    def setUp(self): self.plan = validate_schema_bundle(fixture(2))

    def test_fresh_target_has_no_fictional_receipts(self):
        value = state(self.plan, initial=27)
        result = validate_schema_state(self.plan, value)
        self.assertEqual((result.initial_version, result.current_version, result.applied), (27,27,()))

    def test_upgrade_prefix_and_complete_chain_are_exact(self):
        for current in (25,26,27):
            result = validate_schema_state(self.plan,state(self.plan,current=current),expected_current_version=current)
            self.assertEqual(len(result.applied),current-25)

    def test_missing_reordered_extra_and_modified_receipts_fail(self):
        mutations = (lambda v:v["applied"].pop(), lambda v:v["applied"].reverse(),
                     lambda v:v["applied"].append(deepcopy(v["applied"][0])),
                     lambda v:v["applied"][0].update(sha256="0"*64),
                     lambda v:v["applied"][0].update(filename="other"),
                     lambda v:v["applied"][0].update(version=True),
                     lambda v:v["applied"][0].update(extra=1))
        for mutation in mutations:
            value=state(self.plan); mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises(SchemaError):
                validate_schema_state(self.plan,value)

    def test_invalid_bounds_baseline_or_expected_version_fail(self):
        for change in ({"baseline_id":"other"},{"initial_version":24},{"current_version":28},
                       {"initial_version":28},{"current_version":True},{"extra":1}):
            with self.subTest(change=change), self.assertRaises(SchemaError):
                validate_schema_state(self.plan,{**state(self.plan),**change})
        with self.assertRaisesRegex(SchemaError,"schema_version_changed"):
            validate_schema_state(self.plan,state(self.plan),expected_current_version=26)


class SchemaRunnerTests(unittest.TestCase):
    def setUp(self): self.plan=validate_schema_bundle(fixture(2))

    def test_one_callback_session_and_exact_final_receipt(self):
        command=Mock(return_value=encoded(state(self.plan)).decode().replace("\n", " "))
        result=runner.run_schema_upgrade(self.plan,expected_current_version=25,command=command)
        self.assertEqual(result.current_version,27)
        command.assert_called_once()
        sql=command.call_args.args[0]
        self.assertEqual(len(re.findall(r"^BEGIN;$",sql,re.M)),2)
        self.assertEqual(sql.count(" INSERT INTO operator_private.schema_migrations"),2)
        self.assertIn("pg_try_advisory_lock",sql)
        self.assertEqual(len(re.findall(r"^COMMIT;$",sql,re.M)),3)
        self.assertLessEqual(command.call_args.kwargs["timeout"],600)

    def test_same_version_does_not_execute_steps_or_write_receipts(self):
        command=Mock(return_value=json.dumps(state(self.plan)))
        runner.run_schema_upgrade(self.plan,expected_current_version=27,command=command)
        sql=command.call_args.args[0]
        self.assertIn("READ ONLY",sql)
        self.assertNotIn(" EXECUTE ",sql)
        self.assertNotIn(" INSERT INTO ",sql)
        self.assertNotIn(" FOR UPDATE",sql)

    def test_reconcile_is_read_only_and_does_not_take_upgrade_lock(self):
        command=Mock(return_value=json.dumps(state(self.plan,current=26)))
        result=runner.reconcile_schema_upgrade(self.plan,command=command)
        self.assertEqual(result.current_version,26)
        sql=command.call_args.args[0]
        self.assertIn("READ ONLY",sql)
        self.assertNotIn("pg_try_advisory_lock",sql)
        self.assertNotIn(" EXECUTE ",sql)
        self.assertNotIn(" FOR UPDATE",sql)
        self.assertNotIn("LOCK TABLE",sql)

    def test_lost_acknowledgement_and_malformed_output_never_retry(self):
        for effect,output in ((TimeoutError("private DB error"),None),(None,""),(None,"{}"),
                              (None,json.dumps(state(self.plan))+"\nextra"),
                              (None,"x"*(runner.MAX_OUTPUT_BYTES+1))):
            command=Mock(side_effect=effect,return_value=output)
            with self.subTest(output=str(output)[:20]), self.assertRaisesRegex(SchemaError,"^schema_upgrade_outcome_unknown$"):
                runner.run_schema_upgrade(self.plan,expected_current_version=25,command=command)
            command.assert_called_once()

    def test_invalid_start_refuses_without_dispatch(self):
        for version in (24,28,True,"25",None):
            command=Mock()
            with self.assertRaisesRegex(SchemaError,"schema_upgrade_version_invalid"):
                runner.run_schema_upgrade(self.plan,expected_current_version=version,command=command)
            command.assert_not_called()

    def test_step_is_sql_literal_not_a_psql_script(self):
        # This is a construction check. Real transaction containment is proven on PG17.
        hostile=b"COMMIT;\n\\connect other\nSELECT '$unexpected$';\n"
        step=replace(self.plan.steps[0],sql=hostile,sha256=digest(hostile))
        plan=replace(self.plan,steps=(step,self.plan.steps[1]))
        sql=runner.build_upgrade_sql(plan,expected_current_version=25)
        self.assertIn(" EXECUTE 'COMMIT;\n\\connect other\nSELECT ''$unexpected$'';\n';",sql)
        self.assertIn("SET standard_conforming_strings=on",sql)
        self.assertNotIn("--command",runner.PSQL_ARGUMENTS)
        for argument in ("--no-psqlrc","--no-password","--host=/var/run/postgresql","--dbname=leti-cook","--set=ON_ERROR_STOP=1","--file=-"):
            self.assertIn(argument,runner.PSQL_ARGUMENTS)

    def test_script_limit_refuses_before_dispatch(self):
        command=Mock()
        with patch.object(runner,"MAX_SCRIPT_BYTES",1), self.assertRaisesRegex(SchemaError,"schema_script_too_large"):
            runner.run_schema_upgrade(self.plan,expected_current_version=25,command=command)
        command.assert_not_called()

    def test_reconcile_error_does_not_expose_command_details(self):
        command=Mock(side_effect=RuntimeError("password private stderr"))
        with self.assertRaisesRegex(SchemaError,"^schema_reconciliation_unavailable$"):
            runner.reconcile_schema_upgrade(self.plan,command=command)
        command.assert_called_once()


if __name__ == "__main__": unittest.main()
