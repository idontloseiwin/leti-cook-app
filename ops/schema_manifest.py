"""Pure, bounded schema-release contracts; no filesystem, database or subprocess I/O."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re

from application.schema_release import parse_schema_release
from ops.release_manifest import ReleaseError, SCHEMA_HEADER, strict_json

MAX_SCHEMA_BYTES = 8 * 1024 * 1024
MAX_SEED_BYTES = 1024 * 1024
MAX_STEP_BYTES = 1024 * 1024
MAX_STEP_TOTAL = 16 * 1024 * 1024
MAX_STEPS = 256
MAX_VERSION = 9999
BASELINE_ID = "leti-cook-release-01"
BASELINE_VERSION = 25
_HASH = re.compile(r"[0-9a-f]{64}")
_STEP = re.compile(r"steps/([0-9]{4})-([a-z][a-z0-9-]{0,79})\.sql")
_MANIFEST_KEYS = frozenset((
    "api_data_rows", "baseline_id", "baseline_version", "configuration_seed_counts", "files",
    "format", "fresh_database_only", "postgres_major", "schema_version", "schemas", "steps",
    "user_work_rows", "version",
))


class SchemaError(ReleaseError):
    """Fixed public failure code; never include SQL, paths, DSNs or server output."""


@dataclass(frozen=True, slots=True)
class SchemaStep:
    version: int
    filename: str
    sha256: str
    sql: bytes


@dataclass(frozen=True, slots=True)
class SchemaPlan:
    baseline_id: str
    schema_version: int
    baseline_version: int
    schema_sha256: str
    baseline_sha256: str
    schema_sql: bytes
    baseline_sql: bytes
    seed_sql: bytes
    steps: tuple[SchemaStep, ...]


@dataclass(frozen=True, slots=True)
class AppliedStep:
    version: int
    filename: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SchemaState:
    baseline_id: str
    initial_version: int
    current_version: int
    applied: tuple[AppliedStep, ...]


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _sql(data, maximum):
    if type(data) is not bytes or not 1 <= len(data) <= maximum or b"\0" in data or data.startswith(b"\xef\xbb\xbf"):
        raise SchemaError("schema_sql_invalid")
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise SchemaError("schema_sql_invalid") from None
    return data


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_plan(plan):
    """Recheck even an internally constructed plan before dispatching any SQL."""
    if (type(plan) is not SchemaPlan or plan.baseline_id != BASELINE_ID
            or plan.baseline_version != BASELINE_VERSION or type(plan.baseline_version) is not int
            or not _integer(plan.schema_version, BASELINE_VERSION, MAX_VERSION)
            or type(plan.steps) is not tuple or len(plan.steps) > MAX_STEPS
            or len(plan.steps) != plan.schema_version - plan.baseline_version):
        raise SchemaError("schema_plan_invalid")
    for data, digest in ((plan.schema_sql, plan.schema_sha256), (plan.baseline_sql, plan.baseline_sha256)):
        _sql(data, MAX_SCHEMA_BYTES)
        if (data.splitlines()[0:1] != [SCHEMA_HEADER] or type(digest) is not str
                or _HASH.fullmatch(digest) is None or _digest(data) != digest):
            raise SchemaError("schema_digest_invalid")
    _sql(plan.seed_sql, MAX_SEED_BYTES)
    total = 0
    for version, step in enumerate(plan.steps, plan.baseline_version + 1):
        if type(step) is not SchemaStep or type(step.filename) is not str or type(step.version) is not int:
            raise SchemaError("schema_step_invalid")
        match = _STEP.fullmatch(step.filename)
        if match is None or int(match[1]) != version or step.version != version:
            raise SchemaError("schema_steps_not_consecutive")
        _sql(step.sql, MAX_STEP_BYTES)
        if type(step.sha256) is not str or _HASH.fullmatch(step.sha256) is None or _digest(step.sql) != step.sha256:
            raise SchemaError("schema_step_digest_invalid")
        total += len(step.sql)
    if total > MAX_STEP_TOTAL:
        raise SchemaError("schema_steps_too_large")
    if not plan.steps and plan.schema_sql != plan.baseline_sql:
        raise SchemaError("schema_baseline_mismatch")
    return plan


def validate_schema_bundle(files: Mapping[str, bytes]) -> SchemaPlan:
    """Validate captured bundle bytes; caller authenticates their release signature."""
    if not isinstance(files, Mapping):
        raise SchemaError("schema_bundle_invalid")
    try:
        manifest = strict_json(files["schema/manifest.json"])
    except (KeyError, ReleaseError):
        raise SchemaError("schema_manifest_invalid") from None
    if (type(manifest) is not dict or set(manifest) != _MANIFEST_KEYS
            or manifest["format"] != "leti-cook-self-hosted-schema"
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or manifest["baseline_id"] != BASELINE_ID
            or type(manifest["baseline_version"]) is not int or manifest["baseline_version"] != BASELINE_VERSION
            or not _integer(manifest["schema_version"], BASELINE_VERSION, MAX_VERSION)
            or manifest["fresh_database_only"] is not True
            or type(manifest["postgres_major"]) is not int or manifest["postgres_major"] != 17
            or type(manifest["api_data_rows"]) is not int or manifest["api_data_rows"] != 0
            or type(manifest["user_work_rows"]) is not int or manifest["user_work_rows"] != 0
            or manifest["schemas"] != ["public", "operator_private"]
            or type(manifest["files"]) is not dict or type(manifest["steps"]) is not list
            or len(manifest["steps"]) > MAX_STEPS):
        raise SchemaError("schema_manifest_invalid")
    counts = manifest["configuration_seed_counts"]
    if (type(counts) is not dict or len(counts) > 32 or any(
            type(name) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) is None
            or not _integer(count, 0, 1000000) for name, count in counts.items())):
        raise SchemaError("schema_seed_counts_invalid")
    expected = {"schema.sql", "baseline/schema.sql", "configuration-seeds.sql"}
    steps = []
    for entry in manifest["steps"]:
        if type(entry) is not dict or set(entry) != {"version", "file", "sha256"}:
            raise SchemaError("schema_step_invalid")
        name = entry["file"]
        if type(name) is not str or _STEP.fullmatch(name) is None or name in expected:
            raise SchemaError("schema_step_invalid")
        expected.add(name)
        try:
            sql = files["schema/" + name]
        except KeyError:
            raise SchemaError("schema_file_missing") from None
        steps.append(SchemaStep(entry["version"], name, entry["sha256"], sql))
    hashes = manifest["files"]
    if set(hashes) != expected:
        raise SchemaError("schema_file_set_invalid")
    # Extra SQL must not hide beside the reviewed list; non-SQL pins/docs are
    # authenticated by the outer exact bundle file set.
    actual_sql = {name[7:] for name in files if type(name) is str and name.startswith("schema/") and name.endswith(".sql")}
    if actual_sql != expected:
        raise SchemaError("schema_file_set_invalid")
    captured = {}
    for name in expected:
        try:
            data = files["schema/" + name]
        except KeyError:
            raise SchemaError("schema_file_missing") from None
        maximum = MAX_STEP_BYTES if name.startswith("steps/") else (MAX_SEED_BYTES if name == "configuration-seeds.sql" else MAX_SCHEMA_BYTES)
        _sql(data, maximum)
        if (type(hashes[name]) is not str or _HASH.fullmatch(hashes[name]) is None
                or _digest(data) != hashes[name]):
            raise SchemaError("schema_digest_invalid")
        captured[name] = data
    for step in steps:
        if hashes[step.filename] != step.sha256:
            raise SchemaError("schema_step_digest_invalid")
    plan = validate_plan(SchemaPlan(
        manifest["baseline_id"], manifest["schema_version"], manifest["baseline_version"],
        hashes["schema.sql"], hashes["baseline/schema.sql"], captured["schema.sql"],
        captured["baseline/schema.sql"], captured["configuration-seeds.sql"], tuple(steps),
    ))
    try:
        runtime = parse_schema_release(files["application/schema-release.json"])
    except (KeyError, ValueError):
        raise SchemaError("schema_runtime_metadata_invalid") from None
    if (runtime.baseline_id, runtime.schema_version) != (plan.baseline_id, plan.schema_version):
        raise SchemaError("schema_runtime_metadata_mismatch")
    return plan


def validate_schema_state(plan, value, *, expected_current_version=None):
    """Validate a complete applied range; fresh installs truthfully have no receipts."""
    validate_plan(plan)
    if (type(value) is not dict or set(value) != {"baseline_id", "initial_version", "current_version", "applied"}
            or value["baseline_id"] != plan.baseline_id
            or not _integer(value["initial_version"], plan.baseline_version, plan.schema_version)
            or not _integer(value["current_version"], value["initial_version"], plan.schema_version)
            or type(value["applied"]) is not list or len(value["applied"]) > MAX_STEPS):
        raise SchemaError("schema_state_invalid")
    if expected_current_version is not None and (type(expected_current_version) is not int
            or value["current_version"] != expected_current_version):
        raise SchemaError("schema_version_changed")
    expected = tuple(step for step in plan.steps if value["initial_version"] < step.version <= value["current_version"])
    if len(expected) != len(value["applied"]):
        raise SchemaError("schema_applied_chain_invalid")
    applied = []
    for step, row in zip(expected, value["applied"]):
        if (type(row) is not dict or set(row) != {"version", "filename", "sha256"}
                or type(row["version"]) is not int or row != {
                    "version": step.version, "filename": step.filename, "sha256": step.sha256}):
            raise SchemaError("schema_applied_chain_invalid")
        applied.append(AppliedStep(step.version, step.filename, step.sha256))
    return SchemaState(value["baseline_id"], value["initial_version"], value["current_version"], tuple(applied))
