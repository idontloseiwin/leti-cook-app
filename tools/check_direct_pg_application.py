#!/usr/bin/env python3
"""Explicit local/password API probe on an empty disposable PostgreSQL 17 DB.

Run each mode in a fresh process inside the approved, network-none Linux probe
container (at most 768 MiB). Uses LIC_DIRECT_PG_PROBE_ADMIN_DSN and
LIC_DIRECT_PG_PROBE_DSN with the baseline's exact loopback/name/role/empty guards.
Real application lifespan, maintenance, owner authentication and SQL reads run;
HTTP uses TestClient in memory. No listener, collector, provider request, fixture
seed/reset, installation upgrade, or full acceptance test is performed here.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import json
import logging
import os
import re
from pathlib import Path
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_direct_pg_baseline as safety

ORIGIN = "http://127.0.0.1:8002"
MAX_MEMORY = 768 * 1024 * 1024
DEADLINE_SECONDS = 90
READY_SECONDS = 20
MAX_REPORT_BYTES = 16384
LIMITATIONS = [
    "Empty catalogue and owner sessions only; no nonempty domain parity or performance claim.",
    "TestClient runs ASGI in memory; no TCP listener, reverse proxy or browser is exercised.",
    "Signed worker admission is verified in process; no collector process is started.",
    "No fresh installation, upgrade, rollback, concurrency or provider acceptance is claimed.",
    "Run local and password separately in fresh processes; this tool does not reset SQL fixtures.",
]
STAGES = frozenset({"container", "configuration", "preflight", "application", "unchanged_fixture", "complete"})
PUBLIC_FAILURE_CODES = frozenset({
    "linux_container_required", "network_none_required", "bounded_cgroup_required",
    "application_deadline", "external_http_forbidden", "subprocess_forbidden",
    "collection_not_paused", "unexpected_http_status", "http_response_budget",
    "unexpected_http_shape", "nonempty_sidebar", "sidebar_labels_shape", "nonempty_recent",
    "recent_source_shape", "owner_not_pending", "sessions_not_independent", "local_session_cookie",
    "maintenance_failed", "maintenance_readiness_deadline", "fresh_process_required",
    "pool_budget_changed", "http_runtime_not_ready", "closed_http_admission",
    "closed_worker_admission", "pool_cleanup", "thread_cleanup", "external_work_attempted",
    "fixture_counts_changed", "fixture_seed_changed", "report_budget",
})

RUNTIME_FAILURE_CODES = frozenset({
    "hot_runtime_schema_invalid", "hot_runtime_clock_invalid", "hot_runtime_files_unavailable",
    "hot_runtime_cleanup_failed", "runtime_contract_unavailable", "invalid_runtime_contract",
    "hot_runtime_cache_clear_failed", "hot_runtime_maintenance_failed",
})
COLLECTION_STATES = frozenset({"paused", "waiting_for_setup", "key_refused", "maintenance_unavailable",
                              "scheduler_unavailable", "waiting"})
HTTP_ROUTES = frozenset({"/auth/session", "/auth/claim", "/auth/complete", "/auth/login", "/auth/logout",
                         "/sidebar-data", "/explorer/recent-videos", "/internal/hot-runtime", "/system/hot-runtime"})


class ProbeFailure(RuntimeError):
    """Arguments are private; only fixed stages leave this harness."""


def require(value, code):
    if not value:
        raise ProbeFailure(code)


def safe_failure(error, stage):
    result = {"failure_type": "guard" if isinstance(error, (ProbeFailure, safety.BaselineFailure)) else "operation",
              "failure_code": "application_probe_failed"}
    if type(error) is ProbeFailure and len(error.args) == 1 and isinstance(error.args[0], str):
        if error.args[0] in PUBLIC_FAILURE_CODES:
            result["failure_code"] = "application_probe_" + error.args[0]
    if stage in STAGES:
        result["failure_stage"] = stage
    for kind, category in ((TypeError, "type"), (KeyError, "key"), (AttributeError, "attribute"), (ImportError, "import")):
        if isinstance(error, kind):
            result["failure_category"] = category
            break
    for module, name in (("db.base.postgres", "DatabaseError"), ("psycopg", "Error")):
        kind = getattr(sys.modules.get(module), name, None)
        if isinstance(kind, type) and isinstance(error, kind):
            result["failure_category"] = "sql"
            for field in ("sqlstate", "code"):
                state = getattr(error, field, None)
                if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
                    result["sqlstate"] = state
                    break
            break
    if type(error) is ProbeFailure:
        for field, allowed in (("runtime_failure_code", RUNTIME_FAILURE_CODES),
                               ("collection_state", COLLECTION_STATES), ("http_route", HTTP_ROUTES)):
            item = getattr(error, field, None)
            if isinstance(item, str) and item in allowed:
                result[field] = item
        for field in ("http_status", "expected_http_status"):
            item = getattr(error, field, None)
            if type(item) is int and 100 <= item <= 599:
                result[field] = item
    return result


def container_guard(*, platform=None, interfaces=None, read_text=None):
    platform = sys.platform if platform is None else platform
    require(platform == "linux", "linux_container_required")
    interfaces = socket.if_nameindex() if interfaces is None else interfaces
    require({name for _, name in interfaces} == {"lo"}, "network_none_required")
    read_text = (lambda name: Path(name).read_text(encoding="ascii")) if read_text is None else read_text
    try:
        value = read_text("/sys/fs/cgroup/memory.max").strip()
    except FileNotFoundError:
        value = read_text("/sys/fs/cgroup/memory/memory.limit_in_bytes").strip()
    require(value.isdecimal() and 0 < int(value) <= MAX_MEMORY, "bounded_cgroup_required")
    return int(value)


@contextmanager
def deadline():
    previous = signal.getsignal(signal.SIGALRM)
    def expired(*_):
        raise ProbeFailure("application_deadline")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class DiscardOutput:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


@contextmanager
def quiet_runtime():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(DiscardOutput()), redirect_stderr(DiscardOutput()):
            yield
    finally:
        logging.disable(previous)


@contextmanager
def forbid_external_work():
    """Fail at transport dispatch, including accidental scheduler/provider work."""
    import httpx
    import requests
    counts = {"http_attempts": 0, "collector_starts": 0}
    lock = threading.Lock()
    def reject_http(*args, **kwargs):
        with lock:
            counts["http_attempts"] += 1
        raise ProbeFailure("external_http_forbidden")
    async def reject_async_http(*args, **kwargs):
        return reject_http()
    def reject_process(*args, **kwargs):
        with lock:
            counts["collector_starts"] += 1
        raise ProbeFailure("subprocess_forbidden")
    with ExitStack() as stack:
        for target in ("requests.sessions.Session.request", "urllib.request.urlopen",
                       "httpx.HTTPTransport.handle_request"):
            stack.enter_context(patch(target, reject_http))
        stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", reject_async_http))
        stack.enter_context(patch("subprocess.Popen", reject_process))
        yield counts


def environment(directory, runtime_dsn):
    # No inherited app secrets, PG options, proxy settings, key or dotenv file.
    preserved = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if name in os.environ}
    return {**preserved, "HOME": str(directory), "USERPROFILE": str(directory),
            "TMPDIR": str(directory), "TEMP": str(directory), "TMP": str(directory),
            "PYTHON_DOTENV_DISABLED": "1", "LIC_DEPLOYMENT_PROFILE": "self-hosted",
            "LIC_DATABASE_TRANSPORT": "postgres", "LIC_DATABASE_DSN": runtime_dsn,
            "LIC_DATABASE_ID": str(uuid.uuid4()), "LIC_HOT_RUNTIME_SECRET": secrets.token_urlsafe(32),
            "LIC_HOT_RUNTIME_URL": ORIGIN + "/internal/hot-runtime", "LIC_PUBLIC_ORIGIN": ORIGIN,
            "LIC_AUTH_DATABASE": str(directory / "owner.sqlite3"),
            "LIC_SETUP_DIRECTORY": str(directory / "configuration"),
            "YOUTUBE_QUOTA_PROJECT_ID": "synthetic-application-probe",
            "YOUTUBE_QUOTA_JOURNAL_PATH": str(directory / "quota.sqlite3"),
            "TELEMETRY_ENABLED": "false", "TELEMETRY_LEGACY_HTTP_ENABLED": "false"}


def prepare_profile():
    """Use the real encrypted store, config validator and owner setup issuer."""
    from self_hosted_configuration import ConfigurationStore, default_settings
    from self_hosted_settings import browser_environment, settings_ini
    from ops.self_hosted_owner import initialize, setup_code
    Path(os.environ["LIC_SETUP_DIRECTORY"]).mkdir(mode=0o700)
    store = ConfigurationStore.initialize(os.environ["LIC_SETUP_DIRECTORY"])
    settings = {**default_settings(), "automatic_collection": False}
    store.settings_path.write_text(settings_ini(settings), encoding="utf-8")
    synthetic_key = "synthetic_" + secrets.token_urlsafe(32)
    store.update(0, lambda value: value.update(api_key=synthetic_key, prepared=True, phase="ready",
                                             workspace={"phase": "complete", "channels": {}}))
    require(store.read()["settings"]["automatic_collection"] is False, "collection_not_paused")
    os.environ.update(browser_environment(store.read()["settings"]))
    os.environ["LIC_SETTINGS_FILE"] = str(store.settings_path)
    # This direct-import API harness bypasses the backend launcher. Supply its
    # explicit synthetic defaults; never make production core read a file here.
    from collection_schedule import CollectionSchedules
    from runtime.collection_schedules import schedule_environment
    os.environ.update(schedule_environment(CollectionSchedules()))
    return setup_code(initialize(os.environ["LIC_AUTH_DATABASE"]))


def response_json(response, status):
    if response.status_code != status:
        error = ProbeFailure("unexpected_http_status")
        error.http_status, error.expected_http_status = response.status_code, status
        error.http_route = getattr(getattr(getattr(response, "request", None), "url", None), "path", None)
        raise error
    require(len(response.content) <= 65536, "http_response_budget")
    value = response.json()
    require(type(value) is dict, "unexpected_http_shape")
    return value


def auth_post(client, path, value):
    return response_json(client.post(path, json=value, headers={"Origin": ORIGIN, "X-LIC-Auth": "1"}), 200)


def catalogue(client):
    sidebar = response_json(client.get("/sidebar-data"), 200)
    require(sidebar.get("channels") == [] and sidebar.get("folders") == [], "nonempty_sidebar")
    require(type(sidebar.get("labels")) is list, "sidebar_labels_shape")
    recent = response_json(client.get("/explorer/recent-videos?window=24h"), 200)
    require(recent.get("items") == [] and recent.get("has_more") is False
            and recent.get("next_cursor") is None, "nonempty_recent")
    require(recent.get("source", {}).get("youtube_requests") is False, "recent_source_shape")


def owner_sessions(first, second, mode, setup_token, password):
    require(response_json(first.get("/auth/session"), 200).get("access_mode") == "pending", "owner_not_pending")
    response_json(first.get("/sidebar-data"), 401)
    auth_post(first, "/auth/claim", {"setup_code": setup_token})
    credentials = {"username": "synthetic-probe-owner", "password": password}
    auth_post(first, "/auth/complete", {"mode": mode, **(credentials if mode == "password" else {})})
    catalogue(first)
    if mode == "password":
        response_json(second.get("/sidebar-data"), 401)
        auth_post(second, "/auth/login", credentials)
        require(bool(first.cookies) and bool(second.cookies) and dict(first.cookies) != dict(second.cookies), "sessions_not_independent")
        catalogue(second)
        auth_post(first, "/auth/logout", {})
        response_json(first.get("/sidebar-data"), 401)
        catalogue(second)
        return second
    require(not first.cookies and not second.cookies, "local_session_cookie")
    catalogue(second)
    return first


def wait_ready(runtime, collection):
    until = time.monotonic() + READY_SECONDS
    status, collection_state = {}, None
    while time.monotonic() < until:
        status = runtime.status()
        collection_state = collection.status().get("state")
        if status.get("maintenance_failed"):
            error = ProbeFailure("maintenance_failed")
            error.runtime_failure_code = status.get("failure_code")
            error.collection_state = collection_state
            raise error
        if status.get("status") == "ready" and collection_state == "paused":
            runtime.require_ready()
            return
        time.sleep(0.05)
    error = ProbeFailure("maintenance_readiness_deadline")
    error.runtime_failure_code = status.get("failure_code")
    error.collection_state = collection_state
    raise error


def run_application(mode, runtime_dsn):
    require(not any(name in sys.modules for name in ("core", "server", "self_hosted_collection")), "fresh_process_required")
    sys.path.insert(0, str(ROOT / "application"))
    with tempfile.TemporaryDirectory(prefix="lic-application-probe-") as name:
        directory = Path(name)
        with patch.dict(os.environ, environment(directory, runtime_dsn), clear=True), forbid_external_work() as counts:
            setup_token = prepare_profile()
            password = secrets.token_urlsafe(32)
            # Imports occur only after guards, random temporary setup and I/O blockers.
            import core
            import server
            from fastapi.testclient import TestClient
            from hot_runtime import RemoteAdmission, RuntimeOwnerLock, HotRuntimeUnavailable
            from self_hosted_auth_protocol import HEADER, worker_proof
            from self_hosted_onboarding import get_manager
            options = {"base_url": ORIGIN, "client": ("127.0.0.1", 44000), "raise_server_exceptions": True}
            second = TestClient(server.app, **options)
            try:
                with TestClient(server.app, **options) as first:
                    runtime = server.app.state.hot_runtime
                    collection = get_manager().collection
                    require(core.database.config.max_size == 4 and runtime.repository.database.config.max_size == 1, "pool_budget_changed")
                    wait_ready(runtime, collection)
                    selected = owner_sessions(first, second, mode, setup_token, password)
                    response_json(selected.get("/internal/hot-runtime"), 403)
                    def signed_fetch():
                        return response_json(selected.get("/internal/hot-runtime", headers={HEADER: worker_proof(os.environ["LIC_HOT_RUNTIME_SECRET"])}), 200)
                    RemoteAdmission(os.environ["LIC_HOT_RUNTIME_URL"], admission=core.HOT_ADMISSION_CONFIG, fetch=signed_fetch).require_ready()
                    require(response_json(selected.get("/system/hot-runtime"), 200).get("status") == "ready", "http_runtime_not_ready")
                    runtime.close()
                    require(response_json(selected.get("/sidebar-data"), 503).get("code") == "hot_runtime_not_ready", "closed_http_admission")
                    try:
                        RemoteAdmission(os.environ["LIC_HOT_RUNTIME_URL"], admission=core.HOT_ADMISSION_CONFIG, fetch=signed_fetch).require_ready()
                    except HotRuntimeUnavailable:
                        pass
                    else:
                        raise ProbeFailure("closed_worker_admission")
            finally:
                second.close()
            require(core.database._closed and core.database._pool.closed
                    and runtime.repository.database._closed and runtime.repository.database._pool.closed, "pool_cleanup")
            require(not runtime._thread.is_alive() and not collection.thread.is_alive()
                    and collection.process is None, "thread_cleanup")
            with RuntimeOwnerLock(admission=core.HOT_ADMISSION_CONFIG):
                pass
            require(counts == {"http_attempts": 0, "collector_starts": 0}, "external_work_attempted")
            return {"maintenance_ready": True, "empty_catalogue_reads": True,
                    "independent_browser_clients": 2, "password_sessions": 2 if mode == "password" else 0,
                    "unsigned_worker_denied": True, "signed_worker_ready": True,
                    "closed_http_and_worker_blocked": True, "pools_and_threads_closed": True,
                    "owner_lock_released": True, "youtube_requests": 0, "outbound_http_attempts": 0,
                    "collector_starts": 0, "connection_budget": 6}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "password"), required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.report and (args.report.exists() or not args.report.parent.is_dir()):
        parser.error("report must be a new file in an existing directory")
    result = {"status": "failed", "mode": args.mode, "limitations": LIMITATIONS}
    metrics = {"client_execute_calls": 0, "client_execute_seconds": 0.0}
    stage, admin = "container", None
    try:
        with quiet_runtime():
            result["memory_limit_bytes"] = container_guard()
            with deadline():
                stage = "configuration"
                admin_dsn, runtime_dsn = safety.validate_configuration(os.environ.get("LIC_DIRECT_PG_PROBE_ADMIN_DSN", ""), os.environ.get("LIC_DIRECT_PG_PROBE_DSN", ""))
                stage = "preflight"
                try:
                    admin = safety.connect(admin_dsn, metrics)
                    snapshots, before = safety.preflight(admin)
                    stage = "application"
                    result["checks"] = run_application(args.mode, runtime_dsn)
                    stage = "unchanged_fixture"
                    require(safety.bounded_counts(admin) == before, "fixture_counts_changed")
                    # Fixed seed table identifiers, shared with the guarded baseline.
                    after = {table: admin.scalar("SELECT coalesce(jsonb_agg(to_jsonb(row)), '[]'::jsonb) FROM " + table + " row") for table in safety.SEED_TABLES}
                    require(after == snapshots, "fixture_seed_changed")
                    result.update(status="passed", fixture_unchanged=True)
                    stage = "complete"
                finally:
                    if admin is not None:
                        admin.connection.close()
    except Exception as error:
        result.update(status="failed", **safe_failure(error, stage))
    output = json.dumps(result, sort_keys=True)
    require(len(output.encode("utf-8")) <= MAX_REPORT_BYTES, "report_budget")
    if args.report:
        try:
            fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(output + "\n")
        except OSError:
            print('{"status":"failed","failure_code":"application_probe_report_write_failed"}')
            return 1
    print(output)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
