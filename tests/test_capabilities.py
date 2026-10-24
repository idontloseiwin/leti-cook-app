"""Focused capability facts, disclosure, dependency and transport checks; no DB."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0, str(ROOT / "application"))

from db.readiness import capabilities as checks, intrinsic_capabilities as intrinsic, inventory_chain_capabilities as chains
from test_inventory_chain_capabilities import pins as chain_pins
from collection_schedule import CollectionSchedules
from schema_release import load_schema_release
from db.readiness import schema_release_capabilities as release
from db.videos.collection_policy import LEGACY_COMPATIBILITY_SQL
from db.base.operation_contracts import get_operation
from db.base.postgres import DatabaseError, _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer


def valid_facts(name):
    report = {key: None for key in checks._FIELDS[name]}
    report.update({key: value for key, value in {
        "status": "ok", "ready": True, "reconciliation_required": False,
        "schema_version": 2, "product_exposure_ready_rows": 0,
    }.items() if key in report})
    return {"violations": [False] * len(checks._ERRORS[name]), "value": report}


def valid_release_facts():
    expected = load_schema_release()
    return {"baseline_id": expected.baseline_id, "current_version": expected.schema_version,
            "view_valid": True, "ledger_private": True}


CHAIN_FACTS={
    'inventory_chain_acl':dict(roles_valid=True,relations_valid=True,privileges_valid=True),
    'inventory_chain_catalog':dict(table_valid=True,columns_valid=True,readers_denied=True),
    'inventory_chain_definitions':dict(definitions_valid=True,constraints_valid=True,indexes_valid=True),
}


def reviewed_chain_facts(testcase):
    # Positive fake-catalog paths explicitly install fake pins, never production.
    scope=patch.object(chains,'DEFINITION_PINS',chain_pins())
    scope.start();testcase.addCleanup(scope.stop)


class FactsDatabase:
    def __init__(self):
        self.calls = []
        self.cadence_snapshot = CollectionSchedules().cadence_snapshot()
        self.overrides = {}
        self.hot = {"value": {"version": 1, "as_of": "2026-10-05T12:00:00+00:00", "schema_ready": True}}

    def read_row(self, operation, statement, params=()):
        self.calls.append((operation, statement, params))
        if operation in CHAIN_FACTS:
            return deepcopy(self.overrides.get(operation,CHAIN_FACTS[operation]))
        if statement == LEGACY_COMPATIBILITY_SQL:
            return deepcopy(self.overrides.get("legacy_cadence", {"legacy_cadence_defaults": True}))
        if statement == release.READINESS_SQL:
            return deepcopy(self.overrides.get("schema_release", valid_release_facts()))
        if statement == intrinsic.READINESS_SQL:
            return deepcopy(self.overrides.get("final_schema", dict(roles_valid=True,functions_valid=True,triggers_valid=True)))
        for name, (query, errors) in checks._PREREQUISITES.items():
            if query == statement:
                return deepcopy(self.overrides.get("prerequisite:"+name, {"violations": [False]*len(errors)}))
        names = [name for name, query in checks._QUERIES.items() if query == statement]
        if len(names) != 1:
            raise AssertionError("unexpected query")
        name = names[0]
        return deepcopy(self.overrides.get(name, valid_facts(name)))

    def read_maintenance_capability(self, name):
        self.calls.append(("maintenance", name))
        if name == "schema_release":
            return deepcopy(self.overrides.get("schema_release", valid_release_facts()))
        if name == "collection_cadence":
            return deepcopy(self.overrides.get("legacy_cadence", {"legacy_cadence_defaults": True}))
        if name == "hot_runtime":
            return deepcopy(self.hot)
        if name in ("user_work_restore", "video_failure", "evidence_retention", "inventory_receipts", "operational_retention", "deleted_channels", "collection_completion"):
            return deepcopy(self.overrides.get(name, {"roles_valid":True,"relations_valid":True,"privileges_valid":True}))
        if name == "inventory_switch":
            return deepcopy(self.overrides.get("assert_inventory_switch_schema", valid_facts("assert_inventory_switch_schema")))
        raise AssertionError("unexpected maintenance name")

    def call(self, *args, **kwargs):
        raise AssertionError("retained capability function must not be called")


class CapabilityTests(unittest.TestCase):
    def setUp(self):reviewed_chain_facts(self)

    def test_all_eighteen_public_readers_are_explicit_and_bounded(self):
        self.assertEqual(len(checks.CHECKS), 17)
        self.assertEqual(len(checks._QUERIES), 15)
        self.assertEqual(set(checks.MAINTENANCE_CAPABILITY_QUERIES), {"hot_runtime", "inventory_switch", "user_work_restore", "video_failure", "evidence_retention", "inventory_receipts", "operational_retention", "deleted_channels", "collection_completion", "collection_cadence", "schema_release"})
        with patch.object(checks.operational_read_queries, "get_channel_sync_control_health", return_value={"health": "control"}), patch.object(checks.operational_read_queries, "get_typed_video_collection_health", return_value={"health": "typed"}):
            for name, reader in checks.CHECKS.items():
                with self.subTest(name=name):
                    db = FactsDatabase()
                    result = reader(db)
                    self.assertIsInstance(result, dict)
                    self.assertGreater(len(db.calls), 0)
                    self.assertLessEqual(len(db.calls), 16)
                    operations = [call[0] for call in db.calls]
                    self.assertEqual(len(operations), len(set(operations)))
                    self.assertTrue(all(call[2] == () for call in db.calls))
        self.assertTrue(checks.get_hot_runtime_contract(FactsDatabase())["schema_ready"])

    def test_every_negative_fact_rejects_with_existing_safe_error_policy(self):
        for name, messages in checks._ERRORS.items():
            for index, message in enumerate(messages):
                with self.subTest(check=name, fact=index):
                    facts = valid_facts(name)
                    facts["violations"][index] = True
                    with self.assertRaises(DatabaseError) as caught:
                        checks._validate(name, facts)
                    self.assertEqual(caught.exception.code, "P0001")
                    self.assertFalse(caught.exception.outcome_unknown)
                    self.assertEqual(str(caught.exception), message if message in get_operation(name).error_codes else "database_operation_failed")

    def test_missing_wrong_typed_or_extra_facts_cannot_admit(self):
        name = "assert_inventory_switch_schema"
        good = valid_facts(name)
        bad_rows = [None, [], {}, {**good, "private": "payload"},
                    {**good, "violations": []}, {**good, "violations": tuple(good["violations"])},
                    {**good, "violations": [0] * len(good["violations"])},
                    {**good, "violations": [None] * len(good["violations"])},
                    {**good, "value": None}, {**good, "value": {}},
                    {**good, "value": {**good["value"], "private": "payload"}}]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaisesRegex(DatabaseError, "database_result_shape_invalid"):
                checks._validate(name, row)

    def test_shared_schema_dependencies_once_per_invocation_and_never_globally_cached(self):
        db = FactsDatabase()
        checks.assert_typed_inventory_runtime_schema(db)
        first = len(db.calls)
        self.assertEqual(first, 15)
        checks.assert_typed_inventory_runtime_schema(db)
        self.assertEqual(len(db.calls), 2*first)
        db.overrides["assert_inventory_switch_schema"] = valid_facts("assert_inventory_switch_schema")
        db.overrides["assert_inventory_switch_schema"]["violations"][2] = True
        with self.assertRaises(DatabaseError):
            checks.assert_typed_inventory_runtime_schema(db)
        self.assertEqual(len(db.calls), 2*first+6)

    def test_domain_queue_reconciliation_remains_separate_from_schema(self):
        for name, reader in (("assert_channel_discovery_queue_schema", checks.assert_channel_discovery_queue_ready),
                             ("assert_video_collection_queue_schema", checks.assert_video_collection_queue_ready)):
            for value in (True, None):
                with self.subTest(name=name, value=value):
                    db = FactsDatabase()
                    db.overrides[name] = valid_facts(name)
                    db.overrides[name]["value"]["reconciliation_required"] = value
                    self.assertEqual(checks.CHECKS[name](db)["reconciliation_required"], value)
                    with self.assertRaises(DatabaseError):
                        reader(db)

    def test_python_health_owner_and_foundation_envelopes(self):
        db = FactsDatabase()
        with patch.object(checks.operational_read_queries, "get_channel_sync_control_health", return_value={"canonical": 3}) as health:
            self.assertEqual(checks.assert_channel_sync_control_ready(db)["health"], {"canonical": 3})
            health.assert_called_once_with(db)
        with patch.object(checks.operational_read_queries, "get_typed_video_collection_health", return_value={"typed": 2}) as health:
            self.assertEqual(checks.assert_typed_video_collection_schema(db)["health"], {"typed": 2})
            health.assert_called_once_with(db)
        self.assertEqual(checks.assert_typed_inventory_runtime_schema(db)["foundation"], valid_facts("assert_typed_inventory_foundation_schema")["value"])

    def test_hot_uses_only_fixed_catalog_names_and_fails_closed(self):
        db = FactsDatabase()
        self.assertTrue(checks.get_hot_runtime_contract(db)["schema_ready"])
        self.assertEqual(db.calls, [("maintenance", "hot_runtime"), ("maintenance", "schema_release"), ("maintenance", "collection_cadence"), ("maintenance", "inventory_switch"), ("maintenance", "user_work_restore"), ("maintenance", "video_failure"), ("maintenance", "evidence_retention"), ("maintenance", "inventory_receipts"), ("maintenance", "operational_retention"), ("maintenance", "deleted_channels"), ("maintenance", "collection_completion")])
        db.calls.clear()
        db.hot["value"]["schema_ready"] = False
        self.assertFalse(checks.get_hot_runtime_contract(db)["schema_ready"])
        self.assertEqual(db.calls, [("maintenance", "hot_runtime")])
        db.hot["value"]["schema_ready"] = True
        db.overrides["assert_inventory_switch_schema"] = valid_facts("assert_inventory_switch_schema")
        db.overrides["assert_inventory_switch_schema"]["violations"][-1] = True
        with self.assertRaises(DatabaseError):
            checks.get_hot_runtime_contract(db)
        missing_stamp = {key: value for key, value in db.hot["value"].items() if key != "as_of"}
        for bad in (None, [], {"value": {}}, {"value": {**db.hot["value"], "schema_ready": 1}},
                    {"value": {**db.hot["value"], "extra": 5}}, {"value": missing_stamp}):
            db.hot = bad
            with self.assertRaises(DatabaseError):
                checks.get_hot_runtime_contract(db)

    def test_hot_admission_rejects_partial_user_work_acl_without_changing_contract_shape(self):
        for key in ('roles_valid','relations_valid','privileges_valid'):
            db=FactsDatabase()
            db.overrides['user_work_restore']={name:name!=key for name in ('roles_valid','relations_valid','privileges_valid')}
            with self.assertRaisesRegex(DatabaseError,'user_work_restore_acl_invalid') as caught:
                checks.get_hot_runtime_contract(db)
            self.assertEqual(caught.exception.code,'P0001')
            self.assertEqual(db.calls[-1],('maintenance','user_work_restore'))
        self.assertEqual(set(checks.get_hot_runtime_contract(FactsDatabase())),{'version','as_of','schema_ready'})

    def test_hot_admission_rejects_partial_queue_acl_without_changing_contract_shape(self):
        for key in ('roles_valid','relations_valid','privileges_valid'):
            db=FactsDatabase()
            db.overrides['video_failure']={name:name!=key for name in ('roles_valid','relations_valid','privileges_valid')}
            with self.assertRaisesRegex(DatabaseError,'video_failure_acl_invalid') as caught:
                checks.get_hot_runtime_contract(db)
            self.assertEqual(caught.exception.code,'P0001')
            self.assertEqual(db.calls[-1],('maintenance','video_failure'))
        self.assertEqual(set(checks.get_hot_runtime_contract(FactsDatabase())),{'version','as_of','schema_ready'})

    def test_hot_admission_rejects_missing_evidence_retention_rights(self):
        for key in ('roles_valid','relations_valid','privileges_valid'):
            db=FactsDatabase()
            db.overrides['evidence_retention']={name:name!=key for name in ('roles_valid','relations_valid','privileges_valid')}
            with self.assertRaisesRegex(DatabaseError,'evidence_retention_acl_invalid'):
                checks.get_hot_runtime_contract(db)
            self.assertEqual(db.calls[-1],('maintenance','evidence_retention'))

    def test_maintenance_repository_keeps_closed_and_invalid_clock_guards(self):
        from db.hot.hot_runtime import MaintenanceRepository
        from db.hot.hot_retention import RetentionError
        db = FactsDatabase()
        db.close = lambda: None
        repository = MaintenanceRepository(db)
        self.assertTrue(repository.contract()["schema_ready"])
        self.assertEqual(repository.contract(), {"version":1,"schema_ready":True,"as_of":"2026-10-05T12:00:00+00:00"})
        db.hot["value"]["as_of"] = "invalid-clock"
        with self.assertRaisesRegex(RetentionError, "invalid_runtime_contract"):
            repository.contract()
        repository.close()
        calls = len(db.calls)
        with self.assertRaisesRegex(RetentionError, "maintenance_repository_closed"):
            repository.contract()
        self.assertEqual(len(db.calls), calls)

    def test_missing_catalog_prerequisite_prevents_state_query_parsing(self):
        for name, (query, errors) in checks._PREREQUISITES.items():
            for index in range(len(errors)):
                with self.subTest(name=name, fact=index):
                    db = FactsDatabase()
                    values = [False]*len(errors)
                    values[index] = True
                    db.overrides["prerequisite:"+name] = {"violations": values}
                    with self.assertRaises(DatabaseError) as caught:
                        checks.CHECKS[name](db)
                    self.assertEqual(caught.exception.code, "P0001")
                    self.assertNotIn(checks._QUERIES[name], [call[1] for call in db.calls])

    def test_typed_drift_counts_are_real_cte_columns_not_declaration_defaults(self):
        query = checks._QUERIES["assert_typed_video_collection_schema"]
        self.assertNotRegex(query, r"(?i)AS\s*\(0\)")
        for name in ("v_long_typed_drift", "v_typed_long_clock_drift", "v_identity_drift",
                     "v_disabled_active_drift", "v_product_exposure_rows"):
            self.assertIn("count(*) AS "+name, query)
            self.assertIn("SELECT "+name+" FROM observed_", query)

    def test_receipt_readiness_retains_null_rejection_and_all_type_lifecycle(self):
        query=checks._QUERIES['assert_typed_video_collection_schema']
        for predicate in ('identity_refresh','stream_probe','video_type IS NULL',
                          'video_type IS NOT NULL','IS TRUE','long','short','stream'):
            self.assertIn("pg_catalog.pg_get_constraintdef(c.oid) like '%%"+predicate+"%%'",query)

    def test_fixed_queries_pass_driver_and_psycopg_literal_guards(self):
        for name, query in [*checks._QUERIES.items(), *checks.MAINTENANCE_CAPABILITY_QUERIES.items(), *((name, entry[0]) for name, entry in checks._PREREQUISITES.items())]:
            with self.subTest(name=name):
                _reviewed_statement(query, read=True, max_rows=1)
                parsed = PostgresQuery(Transformer())
                parsed.convert(query, ())
                bare = re.sub(r"(?:E)?'(?:''|[^'])*'", "", query)
                self.assertNotRegex(bare, r"(?i)\b(?:DO|INSERT|UPDATE|DELETE|PERFORM|LOOP|RAISE)\b")
                self.assertNotRegex(bare, r"public\.(?:assert_[a-z_]+|get_[a-z_]+health)\(")
                self.assertNotIn("information_schema.columns", query)
                self.assertNotIn("expected.expected.", query)
        with self.assertRaises(TypeError):
            checks.MAINTENANCE_CAPABILITY_QUERIES["arbitrary"] = "SELECT 1"

    def test_scalar_array_subqueries_are_explicit_array_expressions(self):
        # ANY((SELECT text_array)) is PostgreSQL ANY(subquery), comparing each
        # text[] row to the scalar text. Cast outside the scalar subquery to
        # select the array-expression grammar; packet07 exposed SQLSTATE22P02.
        query = checks._QUERIES["assert_typed_inventory_canonical_repair_schema"]
        self.assertEqual(query.count("any((SELECT v_lock_relation_options FROM observed_1)::text[])"), 1)
        self.assertNotIn("v_repair_config", query)
        for statement in checks._QUERIES.values():
            self.assertNotRegex(statement, r"(?i)\b(?:any|all)\(\(SELECT[^)]*\)\)")

    def test_catalog_evidence_and_domain_drift_are_not_replaced_by_function_existence(self):
        inventory = checks._QUERIES["assert_inventory_switch_schema"]
        for evidence in ("aclexplode", "is_grantable", "convalidated", "condeferrable", "condeferred", "pg_get_constraintdef", "pg_get_indexdef", "attnotnull", "seqcycle", "relrowsecurity"):
            self.assertIn(evidence, inventory)
        for evidence in ("proargnames", "pronargdefaults", "proconfig", "provolatile", "prosrc", "tgenabled"):
            self.assertIn(evidence, intrinsic.READINESS_SQL)
        repair = checks._QUERIES["assert_typed_inventory_canonical_repair_schema"]
        for evidence in ("170000", "pg_relation_is_updatable", "pg_depend", "has_any_column_privilege", "maintain"):
            self.assertIn(evidence, repair)
        for name, evidence in (("assert_typed_inventory_runtime_schema", "token_history_verified"),
                               ("assert_channel_sync_control_ready", "control.control_epoch<=0"),
                               ("assert_typed_video_collection_schema", "product_exposure_ready")):
            self.assertIn(evidence, checks._QUERIES[name])

    def test_preflight_telemetry_keeps_selector_identity_and_sanitizes_errors(self):
        db = FactsDatabase()
        with patch.object(checks, "emit_event") as emit:
            checks.telemetry_check(db, checks.assert_channel_discovery_queue_schema, phase="preflight")
            self.assertEqual(emit.call_count, 2)
            self.assertEqual(emit.call_args.args[3], "assert_channel_discovery_queue_schema")
            self.assertEqual(emit.call_args.kwargs["status"], "ok")
        db.overrides["assert_channel_discovery_queue_schema"] = {"private": "payload"}
        with patch.object(checks, "emit_event") as emit, self.assertRaises(DatabaseError):
            checks.telemetry_check(db, checks.assert_channel_discovery_queue_schema, phase="preflight")
        self.assertEqual(emit.call_args.kwargs["detail"], "DatabaseError")
        self.assertNotIn("payload", repr(emit.call_args))
        with self.assertRaises(ValueError):
            checks.telemetry_check(db, lambda _: {}, phase="preflight")


if __name__ == "__main__":
    unittest.main()
