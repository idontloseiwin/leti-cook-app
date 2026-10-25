"""Stage 1.1–1.3: unwired definitions, wire invariants and independent examples."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import ast
import json
from pathlib import Path
import sys
import unittest
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
sys.path.insert(0, str(ROOT / "tests"))
from pydantic import ValidationError
from research_query import contracts as wire
from research_query.registry import DEPENDENCIES, FIELDS, SOURCE, UNICODE_VERSION, describe_definitions
from research_query_oracle import cell, condition, execute_fixture

FIXTURE = json.loads((ROOT / "tests/fixtures/research_query_v1.json").read_text(encoding="utf-8"))


def query(**changes):
    return {**deepcopy(FIXTURE["base_query"]), **changes}


def result():
    return {
        "query_version": 1, "query_fingerprint": "a" * 64,
        "context": {"source_id": SOURCE.id, "source_schema_version": 1, "registry_version": 1,
                    "lifecycle_profile": "public_standard_v1", "preferences_revision": "existing_v1",
                    "type_policy_revision": "existing_v1", "evaluated_at": FIXTURE["now"],
                    "valid_before": "2026-10-26T00:00:00Z", "consistency": "current"},
        "columns": ["video.views"],
        "provenance": {"counter": {"origin": "youtube_reported", "source_paths": ["videos.statistics.viewCount"],
                                     "method": "reported_counter_v1", "definition_epoch": None}},
        "rows": [{"id": "a0000000001", "cells": {"video.views": {
            "value": "9007199254740993", "state": "known", "provenance_ref": "counter",
            "observed_at": "2026-10-24T00:00:00Z", "expires_at": "2026-11-22T00:00:00Z"}}}],
        "coverage": {"status": "partial", "channels_in_scope": 3, "channels_complete": 2,
                     "eligible_count": None, "reasons": {"inventory_incomplete": 1}},
        "page": {"has_more": False, "next_cursor": None}, "warnings": []}


class DefinitionTests(unittest.TestCase):
    def test_definitions_are_immutable_and_not_runtime_capabilities(self):
        self.assertFalse(SOURCE.executable)
        self.assertFalse(describe_definitions()["executable"])
        with self.assertRaises(TypeError):
            FIELDS["video.custom_sql"] = None
        with self.assertRaises(FrozenInstanceError):
            FIELDS["video.views"].clock_family = "metadata"
        projection = describe_definitions()
        projection["fields"][0]["operators"].append("sql")
        self.assertNotIn("sql", FIELDS["video.id"].operators)
        self.assertNotIn("storage_evidence", str(projection))
        self.assertNotIn("mapping_owner", str(projection))

    def test_every_field_has_independent_clocks_policy_and_dependencies(self):
        for field in FIELDS.values():
            with self.subTest(field=field.id):
                self.assertTrue(set(field.dependencies) <= DEPENDENCIES.keys())
                self.assertEqual(field.video_types, ("long", "short", "stream"))
                self.assertEqual(field.query_implementation, "definition_only")
                self.assertTrue(field.policy_evidence and field.storage_evidence and field.definition_epoch)
        self.assertIn("counters", FIELDS["video.views"].dependencies)
        self.assertNotIn("metadata", FIELDS["video.views"].dependencies)
        self.assertNotIn("counters", FIELDS["video.title"].dependencies)
        self.assertEqual(SOURCE.stream_dependencies, ("stream_completion",))

    def test_no_current_application_caller_imports_unwired_package(self):
        callers = []
        for path in (ROOT / "application").rglob("*.py"):
            if "research_query" in path.parts or "node_modules" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "research_query":
                    callers.append(str(path))
                if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "research_query" for a in node.names):
                    callers.append(str(path))
        self.assertEqual(callers, [])  # Remove this rollout fence only at reviewed integration.


class WireTests(unittest.TestCase):
    def test_example_roundtrip_and_schema_export(self):
        parsed = wire.QueryRequest.model_validate(query())
        self.assertEqual(parsed.model_dump(by_alias=True), query())
        schema = wire.QueryRequest.model_json_schema(by_alias=True)
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("workspace_id", schema["properties"])
        self.assertEqual(wire.QueryResult.model_validate(result()).rows[0].cells["video.views"].value, "9007199254740993")

    def test_request_structures_do_not_coerce_or_accept_command_fields(self):
        invalid = [query(query_version=value) for value in (True, "1", 1.0, 2)]
        invalid += [query(video_types=value) for value in ([], ["long", "short"], ["long", "long"], ["live"])]
        invalid += [query(**{key: value}) for key, value in (("workspace_id", "other"), ("sql", "SELECT 1"), ("refresh", True))]
        invalid += [query(page={"size": value}) for value in (True, "50", 0, 201)]
        invalid += [query(where=value) for value in ({"all": []}, {"not": None}, {"any": [], "all": []},
            {"field": "video.views", "op": "gte", "value": 100},
            {"field": "video.views", "op": "is_known", "value": None})]
        invalid += [query(time={"mode": "as_of", "at": "2020-01-01T00:00:00Z"}),
                    query(source={"id": "owner.analytics.report.v1"})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                wire.QueryRequest.model_validate(value)

    def test_shape_acceptance_does_not_claim_registry_admission(self):
        # Stage 1.4 must reject this BEFORE execution; a parsed DTO is not authorization.
        dto = wire.QueryRequest.model_validate(query(select=["video.not_implemented"]))
        self.assertNotIn(dto.select[0], FIELDS)

    def test_exact_unsigned_wire_numbers(self):
        for value in ("0", "9007199254740993", "18446744073709551615"):
            self.assertEqual(str(wire.uint_decimal(value)), value)
        for value in (0, True, 1.0, "01", "-1", "+1", "1e3", "1.0", " 1", "١", "18446744073709551616"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wire.uint_decimal(value)

    def test_no_expired_payload_false_zero_or_missing_provenance(self):
        for state, value in (("expired", "42"), ("unknown", "0"), ("known", None), ("known", 42), ("known", False)):
            data = result()
            data["rows"][0]["cells"]["video.views"].update(state=state, value=value)
            with self.subTest(state=state, value=value), self.assertRaises(ValidationError):
                wire.QueryResult.model_validate(data)
        data = result()
        data["provenance"] = {}
        with self.assertRaises(ValidationError):
            wire.QueryResult.model_validate(data)
        data = result()
        data["rows"][0]["cells"]["video.views"].update(state="expired", value=None)
        self.assertIsNone(wire.QueryResult.model_validate(data).rows[0].cells["video.views"].value)

    def test_source_and_result_lifetimes_are_not_renewed(self):
        for clock, value in (("observed_at", "2026-10-26T00:00:00Z"), ("expires_at", "2026-10-25T12:00:00Z"),
                             ("observed_at", "2026-02-30T00:00:00Z")):
            data = result()
            data["rows"][0]["cells"]["video.views"][clock] = value
            with self.subTest(clock=clock), self.assertRaises(ValidationError):
                wire.QueryResult.model_validate(data)
        with self.assertRaises(ValidationError):
            wire.Cell(value="x", state="known", provenance_ref="x",
                      observed_at="2026-10-25T12:00:00Z", expires_at="2026-10-25T12:00:00.000000Z")

    def test_result_identity_type_and_page_contract(self):
        for field, value in (("video.id", "wrong000001"), ("video.channel_id", "channel"),
                             ("video.type", "live"), ("video.published_at", "2026-02-30T00:00:00Z")):
            data = result()
            data["columns"] = [field]
            data["rows"][0]["cells"] = {field: {**data["rows"][0]["cells"]["video.views"], "value": value}}
            with self.subTest(field=field), self.assertRaises(ValidationError):
                wire.QueryResult.model_validate(data)
        for page in ({"has_more": True, "next_cursor": None}, {"has_more": False, "next_cursor": "cursor"}):
            with self.assertRaises(ValidationError):
                wire.PageResult.model_validate(page)
        with self.assertRaises(ValidationError):
            wire.Coverage(status="complete", channels_in_scope=3, channels_complete=2)
        data = result()
        data["columns"] = ["video.id"]
        data["rows"][0]["cells"] = {"video.id": {**data["rows"][0]["cells"]["video.views"], "value": None, "state": "unknown"}}
        with self.assertRaises(ValidationError):
            wire.QueryResult.model_validate(data)


class IndependentFixtureTests(unittest.TestCase):
    def test_handwritten_answers_match_reference_relational_oracle(self):
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                request = query(**case["patch"])
                wire.QueryRequest.model_validate(request)
                expected = {key: case[key] for key in ("ids", "error") if key in case}
                self.assertEqual(execute_fixture(FIXTURE, request), expected)

    def test_duplicate_membership_does_not_multiply_a_video(self):
        duplicate = deepcopy(FIXTURE)
        duplicate["rows"].append(deepcopy(duplicate["rows"][0]))
        self.assertEqual(execute_fixture(duplicate, query()), execute_fixture(FIXTURE, query()))

    def test_unknown_three_valued_logic_not_coerced_to_false(self):
        row = FIXTURE["rows"][3]
        missing = {"field": "video.views", "op": "gte", "value": "10"}
        self.assertIsNone(condition(missing, row))
        self.assertIsNone(condition({"not": missing}, row))
        self.assertIsNone(condition({"any": [missing, {"field": "video.views", "op": "is_known"}]}, row))

    def test_casefold_version_and_independent_field_states(self):
        self.assertEqual(unicodedata.unidata_version, UNICODE_VERSION)
        self.assertEqual(cell(FIXTURE["rows"][4], "video.views"), (None, "expired"))
        self.assertEqual(cell(FIXTURE["rows"][4], "video.title"), ("Fresh title", "known"))
        self.assertEqual(cell(FIXTURE["rows"][5], "video.views"), ("200", "known"))


class LegacyBoundaryTests(unittest.TestCase):
    def repository(self):
        # Use the actual legacy builder with a fixture-only repository. We do not
        # use legacy filtering to generate the new contract's expected answers.
        from test_typed_catalogue_builders import TypedCatalogueRepository
        repository = TypedCatalogueRepository()
        repository.rows = {}
        repository.inventories["long"] = []
        now = datetime.fromisoformat(FIXTURE["now"])
        fresh, old = (now - timedelta(hours=1)).isoformat(), (now - timedelta(days=30)).isoformat()
        for item in FIXTURE["rows"][:6]:
            states = item.get("states", {})
            repository.rows[item["id"]] = {
                "video_id": item["id"], "channel_id": FIXTURE["channels"][0]["id"],
                "title": item["title"], "views": None if item["views"] is None else int(item["views"]),
                "published_at": item["published_at"], "duration": item["duration_seconds"],
                "checked_at": old if states.get("video.views") == "expired" else fresh,
                "metadata_checked_at": old if states.get("video.title") == "expired" else fresh,
                "availability_status": "public", "availability_checked_at": fresh,
                "source_expires_at": (now + timedelta(days=1)).isoformat(),
            }
            repository.inventories["long"].append({"video_id": item["id"], "source_checked_at": fresh,
                "source_expires_at": (now + timedelta(days=1)).isoformat(), "published_at": item["published_at"]})
        return repository, now

    def test_legacy_full_row_expiry_and_new_field_expiry_are_explicitly_different(self):
        from channel_explorer import build_channel_explorer
        repository, now = self.repository()
        old = build_channel_explorer(repository, FIXTURE["channels"][0]["id"], full_scope=True,
                                     now=now, sort_by="views", sort_order="desc")
        self.assertEqual([row["video_id"] for row in old["videos"]],
                         ["a0000000001", "a0000000002", "a0000000003", "a0000000004"])
        new = execute_fixture(FIXTURE, query(scope={"kind": "channel", "id": FIXTURE["channels"][0]["id"]}))
        self.assertEqual(new["ids"], ["a0000000006", "a0000000001", "a0000000002", "a0000000003", "a0000000004", "a0000000005"])

    def test_supported_fresh_subset_matches_legacy_unicode_search(self):
        from channel_explorer import build_channel_explorer
        repository, now = self.repository()
        old = build_channel_explorer(repository, FIXTURE["channels"][0]["id"], full_scope=True,
                                     now=now, q="STRASSE", sort_by="views", sort_order="desc")
        new = execute_fixture(FIXTURE, query(where={"field": "video.title", "op": "contains_casefold_v1", "value": "STRASSE"}))
        self.assertEqual([row["video_id"] for row in old["videos"]], new["ids"])


if __name__ == "__main__":
    unittest.main()
