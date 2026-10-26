"""Real psql upgrade acceptance in one explicitly owned disposable PG17 cluster."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops.schema_manifest import BASELINE_ID, SchemaError, SchemaPlan, SchemaStep, validate_schema_bundle
from ops import schema_upgrade as engine
from tools import schema_catalog_snapshot as catalog

FIXTURES = Path(__file__).resolve().parent
SYNTHETIC_SEEDS = {"public.settings": ()}
MAX_ROWS = 1000
MAX_SECONDS = 240


def require(value, code):
    if not value:
        raise RuntimeError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def owned_identity(environment, run=subprocess.run):
    """Require exact identity exported by the shell resource owner, before SQL."""
    cid = environment.get("LIC_SCHEMA_CI_PG_ID", "")
    token = environment.get("LIC_SCHEMA_CI_PG_TOKEN", "")
    image = environment.get("LIC_SCHEMA_CI_PG_IMAGE", "")
    name = environment.get("LIC_SCHEMA_CI_PG_NAME", "")
    component = environment.get("LIC_SCHEMA_CI_PG_COMPONENT", "")
    require(re.fullmatch(r"[0-9a-f]{64}", cid) and re.fullmatch(r"[0-9a-f]{32}", token)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", image) and component in {"postgres", "base"}
            and name == f"leti-cook-schema-{component}-{token}", "upgrade_fixture_ownership_required")
    fmt = '{{.Id}}|{{.Name}}|{{.Image}}|{{index .Config.Labels "org.leti-cook.schema-ci"}}|{{index .Config.Labels "org.leti-cook.schema-ci.component"}}|{{.HostConfig.NetworkMode}}|{{len .HostConfig.PortBindings}}'
    result = run(["docker", "container", "inspect", "--format", fmt, cid], capture_output=True, text=True, timeout=10)
    require(result.returncode == 0 and result.stdout.strip() == f"{cid}|/{name}|{image}|{token}|{component}|none|0",
            "upgrade_fixture_ownership_mismatch")
    return cid


class Probe:
    def __init__(self, container):
        self.container = container
        self.started = time.monotonic()
        self.checks = []
        self.command_calls = 0

    def command(self, sql, *, timeout=30, database="leti-cook"):
        require(time.monotonic() - self.started < MAX_SECONDS, "upgrade_fixture_deadline")
        args = list(engine.PSQL_ARGUMENTS)
        if database != "leti-cook":
            require(database == "postgres", "upgrade_fixture_database_invalid")
            args[args.index("--dbname=leti-cook")] = "--dbname=postgres"
        result = subprocess.run(["docker", "exec", "-i", self.container, *args], input=sql,
                                capture_output=True, text=True, timeout=min(timeout, 35))
        self.command_calls += 1
        require(len(result.stdout.encode()) <= catalog.MAX_SNAPSHOT_BYTES, "upgrade_fixture_output_limit")
        if result.returncode:
            first_error = next((line for line in result.stderr.splitlines() if "ERROR:" in line), "unknown")
            raise RuntimeError("upgrade_fixture_psql_failed: " + first_error[:200])
        return result.stdout

    def query(self, sql):
        output = self.command("SET client_min_messages=warning; SET search_path=pg_catalog; SET timezone='UTC'; "
                              "SET statement_timeout='20s'; SET lock_timeout='1s';\n" + sql)
        rows = output.strip().splitlines()
        require(len(rows) == 1, "upgrade_fixture_json_shape")
        return json.loads(rows[0])

    def scalar(self, expression):
        return self.query("SELECT to_jsonb(" + expression + ")::text;")

    def check(self, value, label):
        require(value, label)
        self.checks.append(label)

    def expect_failure(self, callback, label):
        try:
            callback()
        except (SchemaError, RuntimeError, ValueError):
            self.check(True, label)
        else:
            self.check(False, label)

    def reset(self, schema="synthetic_a.sql"):
        self.command("DROP SCHEMA IF EXISTS readable CASCADE; DROP SCHEMA IF EXISTS operator_private CASCADE; "
                     "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public AUTHORIZATION postgres;\n"
                     + (FIXTURES / schema).read_text(encoding="utf-8"))

    def state(self):
        return self.query("SELECT jsonb_build_object('version',(SELECT current_version FROM operator_private.schema_state),"
                          "'initial',(SELECT initial_version FROM operator_private.schema_state),"
                          "'receipts',(SELECT coalesce(jsonb_agg(to_jsonb(m) ORDER BY version),'[]') FROM operator_private.schema_migrations m),"
                          "'items',(SELECT jsonb_agg(to_jsonb(i) ORDER BY id) FROM public.items i))::text;")

    def row_budget(self):
        # Counts only these synthetic domain/admin rows; production has no API or
        # user rows and its configuration seed cardinalities are checked below.
        value = self.scalar("(SELECT count(*) FROM public.items)+(SELECT count(*) FROM public.settings)"
                            "+(SELECT count(*) FROM operator_private.schema_state)+(SELECT count(*) FROM operator_private.schema_migrations)")
        self.check(value <= MAX_ROWS, "synthetic_rows_bounded")


def synthetic_plan(*steps):
    baseline = (FIXTURES / "synthetic_a.sql").read_bytes()
    target = (FIXTURES / "synthetic_b.sql").read_bytes() if steps else baseline
    values = tuple(SchemaStep(26 + index, f"steps/{26 + index:04d}-synthetic.sql", digest(sql), sql)
                   for index, sql in enumerate(steps))
    return SchemaPlan(BASELINE_ID, 25 + len(values), 25, digest(target), digest(baseline),
                      target, baseline, b"-- synthetic settings only\n", values)


def production_plan():
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in (ROOT / "schema").rglob("*") if p.is_file()}
    files["application/schema-release.json"] = (ROOT / "application/schema-release.json").read_bytes()
    return validate_schema_bundle(files)


def upgraded(probe):
    plan = production_plan()
    before = catalog.capture(probe.query)
    # Exercise the real predecessor checkpoint with an existing ordinary profile,
    # then apply the lifecycle and read-plan steps. This proves the nullable
    # lifecycle upgrade and view replacement do not relabel or erase videos;
    # the independent catalog comparison remains below.
    require(plan.schema_version == 36 and plan.steps[-1].version == 36, "receipt_scan_index_upgrade_target36")
    previous_sql = plan.schema_sql.split(b"-- Publication state is independent",1)[0] + b"COMMIT;\n"
    previous_sql = previous_sql.replace(b"release-01',36,36",b"release-01',30,30")
    previous = replace(plan,schema_version=30,steps=tuple(step for step in plan.steps if step.version <= 30),
                       schema_sql=previous_sql,schema_sha256=digest(previous_sql))
    intermediate = engine.run_schema_upgrade(previous,expected_current_version=plan.baseline_version,command=probe.command)
    probe.check(intermediate.current_version == 30,"publication_upgrade_checkpoint30")
    probe.command("INSERT INTO public.channels(channel_id,title) VALUES('UC0000000000000000000031','Migration fixture'); "
                  "INSERT INTO public.video_inventory(video_id,channel_id,video_type) "
                  "VALUES('p0000000001','UC0000000000000000000031','long'); "
                  "INSERT INTO public.videos(video_id,channel_id,title,published_at,metadata_checked_at,views,checked_at) "
                  "VALUES('p0000000001','UC0000000000000000000031','Existing published video',now(),now(),7,now());")
    original = probe.scalar("(SELECT to_jsonb(v) FROM public.videos v WHERE video_id='p0000000001')")
    result = engine.run_schema_upgrade(plan, expected_current_version=30, command=probe.command)
    probe.check(result.current_version == plan.schema_version, "production_baseline_runs_real_engine")
    retained = probe.scalar("(SELECT to_jsonb(v)-'publication_status'-'scheduled_start_at' FROM public.videos v WHERE video_id='p0000000001')")
    probe.check(retained == original,"publication_upgrade_retains_ordinary_profile")
    probe.check(probe.scalar("(SELECT publication_status IS NULL AND scheduled_start_at IS NULL FROM public.videos WHERE video_id='p0000000001')"),
                "publication_upgrade_does_not_guess_waiting")
    probe.command("DELETE FROM public.videos WHERE video_id='p0000000001'; "
                  "DELETE FROM public.video_inventory WHERE video_id='p0000000001'; "
                  "DELETE FROM public.channels WHERE channel_id='UC0000000000000000000031';")
    production = catalog.capture(probe.query)
    probe.check(not catalog.differences(before, production) if not plan.steps else True, "production_empty_chain_is_noop")
    # A real read-only reconciliation validates the complete ledger range.
    probe.check(engine.reconcile_schema_upgrade(plan, command=probe.command) == result, "production_ledger_validated")
    probe.check(len(production["seeds"]) <= 100, "production_seed_rows_bounded")
    for table in sorted(catalog.BOOTSTRAP_RECONCILIATION_TABLES):
        for label, assignment in (("null", "last_reconciled_at=NULL"),
                                  ("different_clock", "last_reconciled_at=updated_at+interval '1 second'"),
                                  ("different_flag", "reconciliation_required=true")):
            changed = catalog.canonicalize(probe.query("BEGIN; UPDATE " + table + " SET " + assignment + "; "
                                                       + catalog.snapshot_sql() + " ROLLBACK;"))
            probe.check(catalog.differences(production, changed) == ["seeds"], "bootstrap_clock_detects_" + table + "_" + label)
    step = (FIXTURES / "0026-synthetic.sql").read_bytes()
    plan = synthetic_plan(step)
    probe.reset()
    original = probe.state()
    bad = synthetic_plan(b"CREATE TABLE public.must_rollback(id integer); UPDATE public.items SET quantity=999; SELECT 1/0;")
    probe.expect_failure(lambda: engine.run_schema_upgrade(bad, expected_current_version=25, command=probe.command), "failed_step_refused")
    probe.check(probe.state() == original and probe.scalar("to_regclass('public.must_rollback') IS NULL"), "failed_ddl_dml_receipt_atomic_rollback")
    for index, payload in enumerate((b"COMMIT;", b"BEGIN;", b"ROLLBACK;",
                                    b"CREATE TABLE public.must_rollback(id integer); COMMIT;",
                                    b"CREATE TABLE public.must_rollback(id integer); ROLLBACK;",
                                    b"\\echo UPGRADE_META_MUST_NOT_RUN\n",
                                    b"COPY public.items TO STDOUT;")):
        bad = synthetic_plan(payload)
        probe.expect_failure(lambda: engine.run_schema_upgrade(bad, expected_current_version=25, command=probe.command), f"containment_{index}_refused")
        probe.check(probe.state() == original and probe.scalar("to_regclass('public.must_rollback') IS NULL"), f"containment_{index}_no_commit")
    with patch.object(engine, "STEP_TIMEOUT_SECONDS", 1):
        slow = synthetic_plan(b"CREATE TABLE public.must_rollback(id integer); SELECT pg_sleep(3);")
        probe.expect_failure(lambda: engine.run_schema_upgrade(slow, expected_current_version=25, command=probe.command), "step_timeout_refused")
    probe.check(probe.state() == original and probe.scalar("to_regclass('public.must_rollback') IS NULL"), "timeout_rolls_back")
    # Custom settings and stable IDs survive the actual DDL/DML step.
    probe.command("UPDATE public.settings SET value=777 WHERE name='schedule';")
    result = engine.run_schema_upgrade(plan, expected_current_version=25, command=probe.command)
    probe.check(result.current_version == 26 and result.initial_version == 25 and len(result.applied) == 1, "synthetic_a_to_b_real_runner")
    probe.check(probe.scalar("(SELECT value FROM public.settings WHERE name='schedule')") == 777, "custom_setting_not_reset")
    rows = probe.query("SELECT jsonb_agg(to_jsonb(i) ORDER BY id)::text FROM public.items i;")
    probe.check(rows == [{"id": 1, "label": "Alpha", "quantity": 13, "normalized": "alpha"},
                         {"id": 2, "label": "Beta", "quantity": 14, "normalized": "beta"}], "stable_ids_user_work_and_transform")
    before = probe.state()
    repeat = engine.run_schema_upgrade(plan, expected_current_version=26, command=probe.command)
    probe.check(repeat == result and probe.state() == before, "repeat_exact_plan_does_not_reexecute_sql")
    # Receipt corruption is not ignored as a harmless fresh-vs-upgraded difference.
    probe.command("UPDATE operator_private.schema_migrations SET sha256=repeat('0',64);")
    probe.expect_failure(lambda: engine.reconcile_schema_upgrade(plan, command=probe.command), "changed_receipt_refused")
    probe.command("UPDATE operator_private.schema_migrations SET sha256='" + plan.steps[0].sha256 + "';")
    # Delimiter content remains SQL data within the generated DO wrapper.
    collision = synthetic_plan(step, b"INSERT INTO public.settings VALUES('delimiter',length('$leti_cook_quoted$ \\echo text'));" )
    engine.run_schema_upgrade(collision, expected_current_version=26, command=probe.command)
    probe.check(probe.scalar("(SELECT count(*) FROM public.settings WHERE name='delimiter')") == 1, "delimiter_payload_stays_data")
    probe.reset()
    two = synthetic_plan(step, b"ALTER TABLE public.items ADD COLUMN must_rollback integer; UPDATE public.items SET quantity=999; SELECT 1/0;")
    probe.expect_failure(lambda: engine.run_schema_upgrade(two, expected_current_version=25, command=probe.command), "second_step_failure_refused")
    partial = engine.reconcile_schema_upgrade(two, command=probe.command)
    probe.check(partial.current_version == 26 and len(partial.applied) == 1, "first_step_committed_second_receipt_absent")
    probe.check(probe.scalar("NOT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='items' AND column_name='must_rollback')"), "second_ddl_rolled_back")
    probe.reset()
    calls = []
    def lost_ack(sql, *, timeout):
        calls.append(sql)
        probe.command(sql, timeout=timeout)
        raise TimeoutError()
    probe.expect_failure(lambda: engine.run_schema_upgrade(plan, expected_current_version=25, command=lost_ack), "lost_ack_refused_without_retry")
    reconciled = engine.reconcile_schema_upgrade(plan, command=probe.command)
    probe.check(len(calls) == 1 and reconciled.current_version == 26 and len(reconciled.applied) == 1, "lost_ack_receipt_reconciled_once")
    probe.reset()
    probe.expect_failure(lambda: engine.run_schema_upgrade(plan, expected_current_version=25,
                         command=lambda sql, timeout: probe.command(sql, timeout=timeout, database="postgres")), "foreign_database_refused")
    probe.expect_failure(lambda: engine.reconcile_schema_upgrade(plan, command=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())), "reconciliation_unknown_refused")
    probe.check(engine.reconcile_schema_upgrade(plan, command=probe.command).current_version == 25, "no_receipt_is_not_reported_committed")
    concurrent(probe, plan)
    probe.command("DROP TABLE operator_private.schema_migrations;")
    probe.expect_failure(lambda: engine.run_schema_upgrade(plan, expected_current_version=25, command=probe.command), "missing_ledger_refused")
    probe.reset()
    engine.run_schema_upgrade(plan, expected_current_version=25, command=probe.command)
    drift(probe)
    probe.row_budget()
    return {"production": production, "synthetic": catalog.capture(probe.query, seed_clocks=SYNTHETIC_SEEDS)}


def concurrent(probe, plan):
    one, two = engine.SCHEMA_LOCK
    sql = f"SET application_name='leti-cook-upgrade-lock-probe'; SELECT pg_advisory_lock({one},{two}); SELECT pg_sleep(10);"
    process = subprocess.Popen(["docker", "exec", "-i", probe.container, *engine.PSQL_ARGUMENTS],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    process.stdin.write(sql)
    process.stdin.close()
    try:
        ready = False
        for _ in range(20):
            ready = probe.scalar(f"EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid={one} AND objid={two} AND granted)")
            if ready:
                break
            time.sleep(0.05)
        probe.check(ready, "concurrent_lock_holder_ready")
        before = probe.state()
        probe.expect_failure(lambda: engine.run_schema_upgrade(plan, expected_current_version=25, command=probe.command), "concurrent_second_executor_refused")
        probe.check(probe.state() == before, "concurrent_executor_did_not_apply")
    finally:
        probe.command("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='leti-cook-upgrade-lock-probe' AND pid<>pg_backend_pid();")
        process.wait(timeout=5)


def drift(probe):
    baseline = catalog.capture(probe.query, seed_clocks=SYNTHETIC_SEEDS)
    cases = {
        "column": "ALTER TABLE public.items ALTER COLUMN quantity SET DEFAULT 4;",
        "check": "ALTER TABLE public.items DROP CONSTRAINT items_quantity_nonnegative; ALTER TABLE public.items ADD CONSTRAINT items_quantity_nonnegative CHECK(quantity>1);",
        "index": "DROP INDEX public.items_normalized_idx; CREATE INDEX items_normalized_idx ON public.items(normalized) WHERE quantity>0;",
        "view": "CREATE OR REPLACE VIEW readable.items AS SELECT id,normalized,quantity+1 AS quantity FROM public.items;",
        "function_body": "CREATE OR REPLACE FUNCTION public.normalized_label(value text) RETURNS text LANGUAGE sql IMMUTABLE AS 'SELECT upper(value)';",
        "owner": "ALTER TABLE public.items OWNER TO service_role;",
        "grant": "GRANT SELECT ON public.items TO service_role;",
        "column_grant": "GRANT SELECT(label) ON public.items TO lic_reader;",
        "policy": "ALTER POLICY items_reader ON public.items USING(id>1);",
        "default_privilege": "ALTER DEFAULT PRIVILEGES FOR ROLE postgres GRANT EXECUTE ON FUNCTIONS TO PUBLIC;",
        "seed": "UPDATE public.settings SET value=9 WHERE name='new-setting';",
        "revision": "UPDATE operator_private.schema_state SET current_version=27;",
        "role": "ALTER ROLE lic_reader BYPASSRLS;",
        "membership": "GRANT lic_reader TO service_role;",
        "namespace": "CREATE SCHEMA pgtest;",
        "rls": "ALTER TABLE public.items DISABLE ROW LEVEL SECURITY;",
        "sequence": "ALTER SEQUENCE public.items_id_seq CACHE 2;",
        "function_settings": "ALTER FUNCTION public.normalized_label(text) SET search_path TO public;",
    }
    for name, sql in cases.items():
        changed = catalog.canonicalize(probe.query("BEGIN;\n" + sql + "\n" + catalog.snapshot_sql(SYNTHETIC_SEEDS) + "\nROLLBACK;"))
        probe.check(bool(catalog.differences(baseline, changed)), "drift_detected_" + name)
    probe.expect_failure(lambda: catalog.canonicalize(probe.query(
        "BEGIN; CREATE TYPE public.unreviewed AS (value integer); "
        + catalog.snapshot_sql(SYNTHETIC_SEEDS) + " ROLLBACK;")), "unsupported_composite_fails_closed")
    probe.check(catalog.capture(probe.query, seed_clocks=SYNTHETIC_SEEDS) == baseline, "drift_cases_leave_no_changes")
    for role in ("service_role", "lic_reader"):
        for action in ("UPDATE operator_private.schema_state SET current_version=27;",
                       "INSERT INTO operator_private.schema_migrations(version,filename,sha256) VALUES(27,'steps/0027-denied.sql',repeat('0',64));",
                       "CREATE TABLE public.forbidden(id integer);"):
            probe.expect_failure(lambda: probe.command("BEGIN; SET LOCAL ROLE " + role + "; " + action + " ROLLBACK;"), "ledger_or_ddl_denied_" + role)
        value = probe.query("BEGIN; SET LOCAL ROLE " + role + "; SELECT to_jsonb(v)::text FROM readable.schema_version v; COMMIT;")
        probe.check(value == {"baseline_id": BASELINE_ID, "current_version": 26}, "narrow_revision_read_" + role)


def fresh(probe):
    plan = production_plan()
    state = engine.reconcile_schema_upgrade(plan, command=probe.command)
    probe.check(state.initial_version == state.current_version == plan.schema_version and not state.applied, "production_fresh_ledger_empty")
    production = catalog.capture(probe.query)
    probe.reset("synthetic_b.sql")
    synthetic = synthetic_plan((FIXTURES / "0026-synthetic.sql").read_bytes())
    state = engine.reconcile_schema_upgrade(synthetic, command=probe.command)
    probe.check(state.initial_version == state.current_version == 26 and not state.applied, "fresh_b_initial_version_is_target_no_fake_receipts")
    probe.row_budget()
    return {"production": production, "synthetic": catalog.capture(probe.query, seed_clocks=SYNTHETIC_SEEDS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("upgraded", "fresh", "compare"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--first", type=Path)
    parser.add_argument("--second", type=Path)
    args = parser.parse_args()
    if args.phase == "compare":
        first, second = (json.loads(path.read_bytes()) for path in (args.first, args.second))
        for name in ("production", "synthetic"):
            mismatch = catalog.differences(first[name], second[name])
            if mismatch:
                print(json.dumps({"stage": "schema_comparison_detail", "line": name, "sections": mismatch,
                                  "seed_fields": catalog.seed_difference_fields(first[name], second[name])}))
            require(not mismatch, "schema_comparison_failed_" + name + "_" + "_".join(mismatch))
        print(json.dumps({"stage": "schema_upgrades_comparison", "status": "pass", "independent_clusters": 2,
                          "checks": ["production_baseline_steps_equals_fresh", "synthetic_upgrade_equals_independent_fresh", "oid_order_independence"]}))
        return
    require(args.output is not None and not args.output.exists(), "upgrade_fixture_new_output_required")
    probe = Probe(owned_identity(os.environ))
    require(probe.scalar("current_setting('server_version_num')::integer / 10000") == 17, "upgrade_fixture_pg17_required")
    snapshot = upgraded(probe) if args.phase == "upgraded" else fresh(probe)
    encoded = (json.dumps(snapshot, sort_keys=True, indent=2) + "\n").encode("utf-8")
    require(len(encoded) <= catalog.MAX_SNAPSHOT_BYTES * 2, "upgrade_fixture_snapshot_size")
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(encoded)
    print(json.dumps({"stage": "schema_upgrades_" + args.phase, "status": "pass", "checks": probe.checks,
                      "check_count": len(probe.checks), "max_rows": MAX_ROWS, "elapsed_seconds": round(time.monotonic() - probe.started, 3)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Test SQL/data are synthetic, but keep the runner's usual bounded output.
        print(json.dumps({"stage": "schema_upgrades", "status": "fail", "reason": str(error) if isinstance(error, RuntimeError) else type(error).__name__}))
        raise SystemExit(1)
