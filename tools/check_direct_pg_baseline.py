"""Shared safety guards for opt-in probes on a fresh disposable PostgreSQL 17 DB.

Used by check_direct_pg_repositories.py, check_initial_*.py, check_direct_pg_application.py
and check_direct_pg_timeout_cache.py; it has no cases of its own. Both
LIC_DIRECT_PG_PROBE_* DSNs must name one loopback database lic_direct_pg_probe_*;
the admin login is postgres and the runtime login is a real NOINHERIT authenticator
that uses SET LOCAL ROLE service_role. Never bootstraps a database, installs
dependencies or runs YouTube.

Cleanup TRUNCATEs a fixed list of initially empty domain tables, without CASCADE,
and restores only the configuration/preset tables captured at preflight.
Sequences intentionally continue: this database is disposable, not an installation.
"""
from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import os
import re
import time
from typing import Any

MAX_ROWS = 1000
LOCK_ID = 962106004
# All tables are fixed identifiers, never supplied by a DSN, SQL result or CLI.
DOMAIN_TABLES = tuple("public." + name for name in (
    "youtube_category_reference",
    "operator_jobs", "operator_job_inventory_chains", "channel_video_inventory_scans", "video_collection_state",
    "channel_collection_state", "channels", "channel_playlist_state",
    "channel_label_assignments", "channel_snapshots", "folders", "video_inventory",
    "video_inventory_memberships", "video_type_review_cases", "video_availability_incidents",
    "operator_job_targets", "saved_collection_commands", "saved_collection_members",
    "saved_objects", "sync_runs", "typed_inventory_receipts", "typed_video_collection_receipts",
    "user_channel_restore_items", "video_availability_events", "video_snapshots",
    "video_type_review_history", "videos", "youtube_api_request_attempts",
)) + ("operator_private.operator_job_retry_requests",)
SEED_TABLES = tuple("public." + name for name in (
    "channel_labels", "channel_discovery_queue_control", "video_collection_control",
    "typed_video_collection_runtime", "video_collection_cadence",
))
ALL_TABLES = DOMAIN_TABLES + SEED_TABLES


class BaselineFailure(RuntimeError):
    """Only fixed diagnostic tokens can enter the public report."""


def require(condition: Any, code: str) -> None:
    if not condition:
        raise BaselineFailure(code)


def validate_configuration(admin_dsn: str, runtime_dsn: str, parse=None):
    if parse is None:
        from psycopg.conninfo import conninfo_to_dict
        parse = conninfo_to_dict
    try:
        require(not any(os.environ.get(k) for k in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS")), "baseline_implicit_libpq_environment")
        require(admin_dsn and runtime_dsn, "baseline_dsns_required")
        admin, runtime = parse(admin_dsn), parse(runtime_dsn)
        # No libpq service/options, host lists, env fallback or ambiguous port.
        allowed = {"host", "port", "dbname", "user", "password", "sslmode"}
        for info in (admin, runtime):
            require(set(info) <= allowed, "baseline_dsn_options_forbidden")
            require(info.get("host") in {"127.0.0.1", "localhost", "::1"}, "baseline_loopback_required")
            require(re.fullmatch(r"[0-9]{1,5}", info.get("port", "")) is not None, "baseline_explicit_port_required")
            require(1 <= int(info["port"]) <= 65535, "baseline_invalid_port")
            require(info.get("password"), "baseline_explicit_password_required")
            require(re.fullmatch(r"lic_direct_pg_probe_[a-z0-9_]{1,40}", info.get("dbname", "")), "baseline_disposable_name_required")
        require(admin.get("user") == "postgres", "baseline_admin_role_required")
        require(runtime.get("user") == "authenticator", "baseline_runtime_role_required")
        require(all(admin.get(k) == runtime.get(k) for k in ("host", "port", "dbname")), "baseline_database_mismatch")
    except Exception:
        raise BaselineFailure("baseline_configuration_invalid") from None
    return admin_dsn, runtime_dsn


def validate_actual_server_address(value):
    """Normalize psycopg inet values without widening the exact loopback set."""
    try:
        if not isinstance(value, (str, ipaddress.IPv4Address, ipaddress.IPv6Address)):
            raise ValueError()
        address = ipaddress.ip_address(value)
    except ValueError:
        error = BaselineFailure("baseline_actual_server_not_loopback")
        error.server_address_kind = "missing" if value is None else "invalid"
        raise error from None
    if str(address) not in {"127.0.0.1", "::1"}:
        error = BaselineFailure("baseline_actual_server_not_loopback")
        error.server_address_kind = "ipv4_not_allowed" if address.version == 4 else "ipv6_not_allowed"
        raise error


def safe_failure(error: BaseException) -> dict[str, Any]:
    result = {"failure_type": type(error).__name__}
    if isinstance(error, BaselineFailure) and len(error.args) == 1:
        code = error.args[0]
        if isinstance(code, str) and re.fullmatch(r"baseline_[a-z0-9_]+", code):
            result["failure_code"] = code
    primary = getattr(getattr(error, "diag", None), "message_primary", None)
    if isinstance(primary, str) and re.fullmatch(r"baseline_[a-z0-9_]+", primary):
        result["failure_code"] = primary
    address_kind = getattr(error, "server_address_kind", None)
    if address_kind in {"missing", "invalid", "ipv4_not_allowed", "ipv6_not_allowed"}:
        result["server_address_kind"] = address_kind
    state = getattr(error, "sqlstate", None)
    if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
        result["sqlstate"] = state
    return result


class Session:
    def __init__(self, connection, metrics):
        self.connection, self.metrics = connection, metrics

    def execute(self, command, params=None):
        self.metrics["client_execute_calls"] += 1
        started = time.monotonic()
        try:
            cursor = self.connection.execute(command, params)
            rows = []
            while True:
                if cursor.description:
                    require(cursor.description is not None, "baseline_cursor_invalid")
                    rows = cursor.fetchmany(MAX_ROWS + 1)
                    require(len(rows) <= MAX_ROWS, "baseline_response_row_budget")
                if not cursor.nextset():
                    break
            return rows
        finally:
            self.metrics["client_execute_seconds"] += time.monotonic() - started

    def scalar(self, command, params=None):
        rows = self.execute(command, params)
        require(len(rows) == 1 and len(rows[0]) == 1, "baseline_scalar_shape")
        return next(iter(rows[0].values()))

    @contextmanager
    def transaction(self, runtime=False):
        with self.connection.transaction():
            if runtime:
                self.execute("SET LOCAL ROLE service_role")
                identity = self.execute("SELECT session_user AS login, current_user AS role")[0]
                require(identity == {"login": "authenticator", "role": "service_role"}, "baseline_runtime_identity")
            yield self


def connect(dsn, metrics):
    import psycopg
    from psycopg.rows import dict_row
    raw = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
                          application_name="lic_direct_pg_baseline")
    session = Session(raw, metrics)
    session.execute("SET statement_timeout='5s'; SET lock_timeout='1s'; "
                    "SET idle_in_transaction_session_timeout='15s'; SET timezone='UTC'; SET plpgsql.check_asserts=on")
    return session


def bounded_counts(admin):
    # One round trip; each counted subquery is capped before aggregation.
    entries = ["SELECT '" + t + "' AS name, count(*) AS n FROM (SELECT 1 FROM " + t + " LIMIT 1001) bounded" for t in ALL_TABLES]
    return {r["name"]: r["n"] for r in admin.execute(" UNION ALL ".join(entries))}


def preflight(admin):
    require(admin.scalar("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)), "baseline_already_running")
    require(admin.scalar("SELECT current_setting('server_version_num')::integer") // 10000 == 17, "baseline_pg17_required")
    require(re.fullmatch(r"lic_direct_pg_probe_[a-z0-9_]{1,40}", admin.scalar("SELECT current_database()")), "baseline_actual_database_invalid")
    validate_actual_server_address(admin.scalar("SELECT host(inet_server_addr())"))
    roles = admin.execute("SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolbypassrls "
                          "FROM pg_catalog.pg_roles WHERE rolname IN ('authenticator','service_role','lic_reader')")
    role_map = {r["rolname"]: r for r in roles}
    require(set(role_map) == {"authenticator", "service_role", "lic_reader"}, "baseline_roles_missing")
    login = role_map["authenticator"]
    require(login["rolcanlogin"] and not any(login[k] for k in ("rolsuper", "rolinherit", "rolcreaterole", "rolcreatedb", "rolbypassrls")), "baseline_login_privileges")
    require(not role_map["service_role"]["rolsuper"] and not role_map["service_role"]["rolcanlogin"], "baseline_service_privileges")
    require(not any(role_map["lic_reader"][key] for key in
                    ("rolsuper", "rolinherit", "rolcreaterole", "rolcreatedb", "rolcanlogin", "rolbypassrls")),
            "baseline_reader_privileges")
    counts = bounded_counts(admin)
    require(all(counts[t] == 0 for t in DOMAIN_TABLES), "baseline_nonempty_domain")
    require(sum(counts.values()) <= MAX_ROWS, "baseline_seed_budget")
    require(admin.scalar("SELECT count(*) FROM public.channel_labels WHERE label_key<>'monetized' OR name<>'Monetized' OR label_type<>'preset'") == 0, "baseline_nonempty_labels")
    snapshots = {t: admin.scalar("SELECT coalesce(jsonb_agg(to_jsonb(row)), '[]'::jsonb) FROM " + t + " row") for t in SEED_TABLES}
    return snapshots, counts


def reset_fixture(admin, snapshots, before_counts):
    from psycopg.types.json import Jsonb
    with admin.transaction():
        admin.execute("TRUNCATE TABLE " + ", ".join(ALL_TABLES))
        for table, rows in snapshots.items():
            if rows:
                admin.execute("INSERT INTO " + table + " SELECT * FROM jsonb_populate_recordset(NULL::" + table + ", %s)", (Jsonb(rows),))
    require(bounded_counts(admin) == before_counts, "baseline_cleanup_count_mismatch")

