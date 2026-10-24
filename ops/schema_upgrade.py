"""Trusted host schema-step protocol for the owner's local PostgreSQL 17 psql.

The injected command owns installation identity/Compose verification. It sends
this module's SQL on stdin to PSQL_ARGUMENTS, returns stdout, and never prints
raw SQL or stderr. No candidate Python is imported and no write is retried.
"""
from __future__ import annotations

import hashlib
import json

from ops.release_manifest import ReleaseError, strict_json
from ops.schema_manifest import (
    MAX_STEPS, SchemaError, validate_plan, validate_schema_state,
)

PSQL_ARGUMENTS = (
    "psql", "--no-psqlrc", "--quiet", "--no-align", "--tuples-only",
    "--set=ON_ERROR_STOP=1", "--no-password", "--host=/var/run/postgresql", "--port=5432",
    "--username=postgres", "--dbname=leti-cook", "--file=-",
)
SCHEMA_LOCK = (0x4C494353, 0x55504454)
STEP_TIMEOUT_SECONDS = 120
LOCK_TIMEOUT_SECONDS = 3
MAX_SCRIPT_BYTES = 48 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024


def _literal(value):
    # Every session sets standard_conforming_strings=on before parsing our DO.
    return "'" + value.replace("'", "''") + "'"


def _do(body):
    # Dollar delimiters are chosen from the complete body, including candidate
    # SQL literals, so a delimiter inside a signed step cannot close this DO.
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    delimiter = "$leti_cook_" + digest + "$"
    while delimiter in body:
        digest = hashlib.sha256(digest.encode("ascii")).hexdigest()
        delimiter = "$leti_cook_" + digest + "$"
    return "DO " + delimiter + "\n" + body + "\n" + delimiter + ";\n"


def _session(*, lock):
    checks = """BEGIN
 IF current_database() <> 'leti-cook' OR session_user <> 'postgres' OR current_user <> 'postgres'
    OR current_setting('server_version_num')::integer / 10000 <> 17 THEN
  RAISE EXCEPTION 'schema_database_identity_invalid';
 END IF;
 IF to_regclass('operator_private.schema_state') IS NULL
    OR to_regclass('operator_private.schema_migrations') IS NULL THEN
  RAISE EXCEPTION 'schema_ledger_missing';
 END IF;
"""
    if lock:
        checks += f""" IF NOT pg_catalog.pg_try_advisory_lock({SCHEMA_LOCK[0]},{SCHEMA_LOCK[1]}) THEN
  RAISE EXCEPTION 'schema_upgrade_busy';
 END IF;
"""
    checks += "END"
    return (
        "SET client_encoding='UTF8';\nSET standard_conforming_strings=on;\n"
        "SET search_path=pg_catalog;\nSET client_min_messages=warning;\n"
        f"SET statement_timeout='{STEP_TIMEOUT_SECONDS}s';\n"
        f"SET lock_timeout='{LOCK_TIMEOUT_SECONDS}s';\n"
        f"SET idle_in_transaction_session_timeout='{STEP_TIMEOUT_SECONDS}s';\n" + _do(checks)
    )


def _state_assertion(plan, expected_current_version, *, write):
    expected = json.dumps([
        {"version": step.version, "filename": step.filename, "sha256": step.sha256}
        for step in plan.steps
    ], separators=(",", ":"), sort_keys=True)
    locked = " FOR UPDATE" if write else ""
    version_check = "" if expected_current_version is None else (
        f" IF state.current_version IS DISTINCT FROM {expected_current_version} THEN\n"
        "  RAISE EXCEPTION 'schema_version_changed';\n END IF;\n"
    )
    ledger_lock = " LOCK TABLE operator_private.schema_migrations IN SHARE ROW EXCLUSIVE MODE;\n" if write else ""
    return f""" IF (SELECT count(*) FROM (SELECT 1 FROM operator_private.schema_state LIMIT 2) AS bounded) <> 1 THEN
  RAISE EXCEPTION 'schema_state_invalid';
 END IF;
 SELECT * INTO STRICT state FROM operator_private.schema_state WHERE singleton{locked};
 IF state.baseline_id IS DISTINCT FROM {_literal(plan.baseline_id)}
    OR state.initial_version IS NULL OR state.current_version IS NULL
    OR state.initial_version < {plan.baseline_version}
    OR state.current_version < state.initial_version OR state.current_version > {plan.schema_version} THEN
  RAISE EXCEPTION 'schema_state_invalid';
 END IF;
{version_check}{ledger_lock} IF (SELECT count(*) FROM (SELECT 1 FROM operator_private.schema_migrations LIMIT {MAX_STEPS + 1}) AS bounded) > {MAX_STEPS} THEN
  RAISE EXCEPTION 'schema_applied_chain_invalid';
 END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('version',version,'filename',filename,'sha256',sha256) ORDER BY version),'[]'::jsonb)
 INTO actual FROM operator_private.schema_migrations;
 SELECT coalesce(jsonb_agg(value ORDER BY (value->>'version')::integer),'[]'::jsonb)
 INTO wanted FROM jsonb_array_elements({_literal(expected)}::jsonb)
 WHERE (value->>'version')::integer > state.initial_version AND (value->>'version')::integer <= state.current_version;
 IF actual IS DISTINCT FROM wanted THEN RAISE EXCEPTION 'schema_applied_chain_invalid'; END IF;
"""


def _state_guard(plan, expected_current_version, *, write):
    return _do("DECLARE state record; actual jsonb; wanted jsonb;\nBEGIN\n"
               + _state_assertion(plan, expected_current_version, write=write) + "END")


_STATE_OUTPUT = """SELECT jsonb_build_object(
 'baseline_id',state.baseline_id,'initial_version',state.initial_version,'current_version',state.current_version,
 'applied',(SELECT coalesce(jsonb_agg(jsonb_build_object('version',version,'filename',filename,'sha256',sha256) ORDER BY version),'[]'::jsonb)
            FROM operator_private.schema_migrations))::text
FROM operator_private.schema_state AS state WHERE singleton;
"""


def _bounded_script(script):
    if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise SchemaError("schema_script_too_large")
    return script


def build_reconcile_sql(plan):
    """Read one committed snapshot; missing or contradictory receipts fail closed."""
    validate_plan(plan)
    return _bounded_script(
        _session(lock=False) + "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
        + _state_guard(plan, None, write=False) + _STATE_OUTPUT + "COMMIT;\n"
    )


def build_upgrade_sql(plan, *, expected_current_version):
    validate_plan(plan)
    if (type(expected_current_version) is not int
            or not plan.baseline_version <= expected_current_version <= plan.schema_version):
        raise SchemaError("schema_upgrade_version_invalid")
    statements = [_session(lock=True)]
    pending = tuple(step for step in plan.steps if step.version > expected_current_version)
    if not pending:
        statements += ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n",
                       _state_guard(plan, expected_current_version, write=False), _STATE_OUTPUT, "COMMIT;\n"]
    for step in pending:
        statements.append("BEGIN;\n")
        statements.append(f"SET LOCAL statement_timeout='{STEP_TIMEOUT_SECONDS}s';\n")
        statements.append(f"SET LOCAL lock_timeout='{LOCK_TIMEOUT_SECONDS}s';\n")
        statements.append("SET LOCAL standard_conforming_strings=on;\nSET LOCAL search_path=pg_catalog;\n")
        body = "DECLARE state record; actual jsonb; wanted jsonb;\nBEGIN\n"
        body += _state_assertion(plan, step.version - 1, write=True)
        body += " EXECUTE " + _literal(step.sql.decode("utf-8")) + ";\n"
        body += (
            " INSERT INTO operator_private.schema_migrations(version,filename,sha256) VALUES ("
            f"{step.version},{_literal(step.filename)},{_literal(step.sha256)});\n"
            f" UPDATE operator_private.schema_state SET current_version={step.version}\n"
            f" WHERE singleton AND current_version={step.version - 1};\n"
            " IF NOT FOUND THEN RAISE EXCEPTION 'schema_version_changed'; END IF;\n"
        )
        body += _state_assertion(plan, step.version, write=True) + "END"
        statements.append(_do(body))
        statements.append("COMMIT;\n")
    if pending:
        statements += ["BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n",
                       _state_guard(plan, plan.schema_version, write=False), _STATE_OUTPUT, "COMMIT;\n"]
    # Disconnect releases the session lock, including when psql stops on an error.
    return _bounded_script("".join(statements))


def _result(plan, output, *, expected_current_version=None):
    if type(output) is not str:
        raise SchemaError("schema_result_invalid")
    try:
        encoded = output.encode("utf-8")
    except UnicodeError:
        raise SchemaError("schema_result_invalid") from None
    if not 1 <= len(encoded) <= MAX_OUTPUT_BYTES or len(output.strip().splitlines()) != 1:
        raise SchemaError("schema_result_invalid")
    try:
        value = strict_json(encoded.strip(), limit=MAX_OUTPUT_BYTES)
    except ReleaseError:
        raise SchemaError("schema_result_invalid") from None
    return validate_schema_state(plan, value, expected_current_version=expected_current_version)


def reconcile_schema_upgrade(plan, *, command):
    """Only read; callers decide how a receipt affects whole-release recovery."""
    script = build_reconcile_sql(plan)
    try:
        output = command(script, timeout=STEP_TIMEOUT_SECONDS + 10)
    except Exception:
        raise SchemaError("schema_reconciliation_unavailable") from None
    return _result(plan, output)


def run_schema_upgrade(plan, *, expected_current_version, command):
    """One psql session and one transaction/receipt per step, without retry.

    command(sql: str, *, timeout: int) must use the trusted local owner psql
    transport and PSQL_ARGUMENTS. It returns only stdout and raises on any error.
    A dispatch/parse failure is an unknown outcome: reconcile, never replay.
    """
    script = build_upgrade_sql(plan, expected_current_version=expected_current_version)
    pending = plan.schema_version - expected_current_version
    try:
        output = command(script, timeout=(max(1, pending) + 2) * STEP_TIMEOUT_SECONDS + 10)
        return _result(plan, output, expected_current_version=plan.schema_version)
    except Exception:
        raise SchemaError("schema_upgrade_outcome_unknown") from None
