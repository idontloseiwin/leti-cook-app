"""Receipt-backed scan failure used by the typed inventory repository.

Transition advisory lock -> receipt replay -> playlist -> scan -> final control
SHARE is the retained order. A matching durable receipt replays before current
lease/visibility checks, but never bypasses source retention. No provider work,
internal retry, or acknowledgement before known COMMIT belongs here.

Direct receipt INSERT requires a separately reviewed ACL/readiness transition.
Current schema deliberately keeps that permission unavailable to service_role.
"""
from __future__ import annotations

import re
from psycopg.types.json import Jsonb

from collection_policy import inventory_source_policy, PolicyError, _BAD_SCAN_ERRORS
from ..videos.collection_policy import require_cadence_snapshot
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError, DatabaseServerError, Operation

_ERROR = re.compile(r"[a-z0-9_]{1,64}\Z")
_TYPES = frozenset({"long", "short", "stream"})
_QUOTA_ERRORS = frozenset({"youtube_daily_quota_exceeded_error", "youtube_quota_exhausted"})
_FAIL_INPUT = """
    SELECT to_jsonb(input) AS value,source_checked_at>now()+interval '5 minutes' AS future
    FROM (SELECT %s::text AS channel_id,%s::text AS video_type,%s::uuid AS lease_token,
        %s::uuid AS scan_id,%s::uuid AS transition_id,%s::text AS expected_page_token,
        %s::text AS outcome,%s::text AS error_code,%s::timestamptz AS source_checked_at) input
"""
_HOT = """
    WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
    SELECT coalesce(isfinite(%s::timestamptz) AND %s::timestamptz BETWEEN at-interval '696 hours' AND at,false) AS valid
    FROM clock
"""
_FAIL_HASH = """
    SELECT md5(jsonb_build_array(btrim(%s::text),%s::text,%s::uuid,%s::uuid,%s::text,
        %s::text,%s::text,extract(epoch FROM %s::timestamptz))::text) AS value
"""
_TRANSITION_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('inventory-transition:'||%s::uuid::text,0))"
_RECEIPT = """
    SELECT to_jsonb(receipt) AS value FROM (
        SELECT receipt.scan_id,receipt.transition_kind,receipt.result,receipt.source_checked_at,
            receipt.result->>'request_hash' AS request_hash,scan.channel_id,scan.video_type
        FROM public.typed_inventory_receipts receipt JOIN public.channel_video_inventory_scans scan USING(scan_id)
        WHERE receipt.receipt_scope='scan_transition' AND receipt.transition_id=%s::uuid
    ) receipt
"""
_PLAYLIST = "SELECT to_jsonb(source) AS value FROM public.channel_playlist_state source WHERE channel_id=%s AND video_type=%s FOR UPDATE"
_FORMAT = "SELECT active_scan_id::text AS active_scan_id FROM public.channel_video_inventory_state WHERE channel_id=%s AND video_type=%s"
_SCAN = "SELECT to_jsonb(scan) AS value FROM public.channel_video_inventory_scans scan WHERE scan_id=%s::uuid FOR UPDATE"
_FRESH_LEASE = "SELECT coalesce(%s::timestamptz>clock_timestamp(),false) AS valid"
_FULL_GENERATION = """
    SELECT EXISTS(SELECT 1 FROM public.channel_video_inventory_scans WHERE channel_id=%s AND video_type=%s
        AND scan_id<>%s::uuid AND status='completed' AND coverage='complete'
        AND scan_kind IN ('initial_inventory','full_reconciliation')) AS present
"""
_INITIAL_JOB = """
    SELECT EXISTS(SELECT 1 FROM public.channel_collection_state control JOIN public.operator_jobs job ON job.id=control.initial_job_id
        WHERE control.channel_id=%s AND job.channel_id=%s AND job.initial_scope_version=1 AND job.phase='inventory'
          AND job.accepted_epoch=control.control_epoch AND job.lease_channel_epoch=control.control_epoch
          AND job.inventory_scan_id=%s::uuid AND job.inventory_checkpoint->>'phase'='bound'
          AND job.inventory_checkpoint->>'playlist_lease_token'=%s::text
          AND job.summary_json->'full_inventory_sources'->%s::text->>'scan_id'=%s::text) AS present
"""
_FAIL_SCAN = """
    UPDATE public.channel_video_inventory_scans SET status=CASE WHEN %(retain)s THEN 'running' ELSE 'failed' END,
        coverage='partial',completed_at=NULL,
        source_checked_at=CASE WHEN %(retain)s THEN source_checked_at ELSE %(source_checked_at)s::timestamptz END,
        error_code=%(error_code)s,error_at=%(source_checked_at)s::timestamptz,
        last_transition_id=%(transition_id)s::uuid,last_transition_kind='failure',updated_at=now()
    WHERE scan_id=%(scan_id)s::uuid
"""
_FAIL_SOURCE = """
    UPDATE public.channel_playlist_state SET inventory_status=%(inventory_status)s,
        inventory_last_error_code=%(error_code)s,inventory_last_error_at=%(source_checked_at)s::timestamptz,
        inventory_error_count=inventory_error_count+CASE WHEN %(outcome)s='error' AND %(error_code)s<>'provider_rate_limited' THEN 1 ELSE 0 END,
        inventory_state_origin=CASE WHEN inventory_state_origin='legacy_long_backfill' AND last_completed_scan_id IS NULL
            THEN inventory_state_origin ELSE 'typed_native' END,inventory_updated_at=now(),
        status='ready',next_due_at=%(next_due_at)s::timestamptz,priority=0,
        active_scan_id=CASE WHEN %(retain)s THEN active_scan_id END,
        resume_page_token=CASE WHEN %(retain)s THEN resume_page_token END,
        resume_started_at=CASE WHEN %(retain)s THEN resume_started_at END,
        scan_boundary_at=CASE WHEN %(retain)s THEN scan_boundary_at END,
        source_next_after=%(next_due_at)s::timestamptz,
        source_checked_at=CASE WHEN %(retain)s THEN source_checked_at ELSE %(source_checked_at)s::timestamptz END,
        last_result=%(outcome)s,discovery_started_at=NULL,discovery_error=%(error_code)s,
        discovery_error_count=coalesce(discovery_error_count,0)+CASE WHEN %(outcome)s='error' AND %(error_code)s<>'provider_rate_limited' THEN 1 ELSE 0 END,
        lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,
        last_error_code=%(error_code)s,last_failure_attempt_id=%(transition_id)s::uuid,updated_at=now()
    WHERE channel_id=%(channel_id)s AND video_type=%(video_type)s AND status='leased'
        AND lease_token=%(lease_token)s::uuid AND active_scan_id=%(scan_id)s::uuid
    RETURNING to_jsonb(next_due_at)#>>'{}' AS next_due_at
"""
_INSERT_RECEIPT = """
    INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,scan_id,transition_kind,result,source_checked_at,created_at)
    VALUES ('scan_transition',%s::uuid,%s::uuid,%s,%s::jsonb,%s::timestamptz,now())
"""


def _fail(message, code="P0001"):
    raise DatabaseServerError(message, code=code)


def _value(row):
    if not isinstance(row, dict) or not isinstance(row.get("value"), dict):
        raise DatabaseError("database_result_shape_invalid")
    return row["value"]


def _hot(tx, stamp):
    if tx.fetch_one(_HOT, (stamp, stamp))["valid"] is not True:
        _fail("hot_source_clock_out_of_window", "22023")


def _operation(name):
    spec = get_operation(name)
    return Operation(name, statement_timeout_ms=spec.statement_timeout_ms, lock_timeout_ms=spec.lock_timeout_ms,
                     total_timeout_ms=spec.statement_timeout_ms, error_codes=spec.error_codes)


def _replay(tx, values, kind, request_hash):
    tx.fetch_one(_TRANSITION_LOCK, (values["transition_id"],))
    row = tx.fetch_one(_RECEIPT, (values["transition_id"],))
    if row is None: return None
    receipt = _value(row)
    result = receipt["result"]
    if "source_expired" in result: _fail("hot_inventory_receipt_source_expired")
    _hot(tx, receipt["source_checked_at"])
    if (receipt["scan_id"] != values["scan_id"] or receipt["transition_kind"] != kind
            or receipt["channel_id"] != values["channel_id"].strip(" ") or receipt["video_type"] != values["video_type"]):
        _fail("typed_inventory_transition_id_reused")
    if "request_hash" not in result: _fail("typed_inventory_receipt_legacy_unverifiable")
    if receipt["request_hash"] != request_hash: _fail("typed_inventory_transition_id_reused")
    _hot(tx, values["source_checked_at"])
    # Repeated retained assertions are distinct wall-clock samples; preserve them.
    if "source_expired" in result: _fail("hot_inventory_receipt_source_expired")
    _hot(tx, receipt["source_checked_at"])
    return {**result, "replayed": True}


def _owned(tx, values, kind):
    identity = values["channel_id"].strip(" ")
    row = tx.fetch_one(_PLAYLIST, (identity, values["video_type"]))
    if row is None: _fail("typed_inventory_discovery_state_missing")
    source = _value(row)
    inventory = tx.fetch_one(_FORMAT, (identity, values["video_type"]))
    if inventory is None: _fail("typed_inventory_format_state_missing")
    row = tx.fetch_one(_SCAN, (values["scan_id"],))
    scan = _value(row) if row is not None else None
    if scan is None or scan["channel_id"] != identity or scan["video_type"] != values["video_type"]:
        _fail("typed_inventory_scan_not_owned")
    _hot(tx, values["source_checked_at"])
    if (source["runtime_enabled"] is not True or source["status"] != "leased"
            or source["lease_token"] != values["lease_token"] or source["lease_expires_at"] is None):
        _fail("typed_inventory_" + kind + "_lease_not_owned")
    if tx.fetch_one(_FRESH_LEASE, (source["lease_expires_at"],))["valid"] is not True:
        _fail("typed_inventory_" + kind + "_lease_not_owned")
    if source["active_scan_id"] != values["scan_id"] or inventory["active_scan_id"] != values["scan_id"]:
        _fail("typed_inventory_" + kind + "_lease_not_owned")
    return source, scan


def fail_typed_inventory_scan(database, *, channel_id, video_type, lease_token, scan_id, transition_id,
                              expected_page_token, outcome, error_code, source_checked_at):
    with database.transaction(_operation("fail_typed_inventory_scan")) as tx:
        result = fail_scan_in_transaction(tx, channel_id=channel_id, video_type=video_type, lease_token=lease_token,
            scan_id=scan_id, transition_id=transition_id, expected_page_token=expected_page_token,
            outcome=outcome, error_code=error_code, source_checked_at=source_checked_at)
    return result


def fail_scan_in_transaction(transaction, **parameters):
    """Borrow a future initial-job owner; caught failures still poison its work."""
    with transaction.borrow():
        return _failure(transaction, **parameters)


def _failure(tx, *, channel_id, video_type, lease_token, scan_id, transition_id,
             expected_page_token, outcome, error_code, source_checked_at):
    inputs = tx.fetch_one(_FAIL_INPUT, (channel_id, video_type, lease_token, scan_id, transition_id,
                                      expected_page_token, outcome, error_code, source_checked_at))
    values = _value(inputs)
    _hot(tx, values["source_checked_at"])
    if (values["channel_id"] is None or not values["channel_id"].strip(" ")
            or (values["video_type"] is not None and values["video_type"] not in _TYPES)
            or any(values[key] is None for key in ("lease_token", "scan_id", "transition_id", "error_code", "source_checked_at"))
            or (values["outcome"] is not None and values["outcome"] not in {"playlist_not_found", "error"})
            or not _ERROR.fullmatch(values["error_code"]) or inputs["future"] is True):
        _fail("typed_inventory_failure_identity_invalid")
    if values["expected_page_token"] is not None and not 1 <= len(values["expected_page_token"]) <= 2048:
        _fail("typed_inventory_expected_token_invalid")
    request_hash = tx.fetch_one(_FAIL_HASH, tuple(values[key] for key in
        ("channel_id", "video_type", "lease_token", "scan_id", "expected_page_token", "outcome", "error_code", "source_checked_at")))["value"]
    replay = _replay(tx, values, "failure", request_hash)
    if replay is not None: return replay
    source, scan = _owned(tx, values, "failure")
    if scan["status"] != "running" or scan["resume_page_token"] != values["expected_page_token"]:
        _fail("typed_inventory_failure_checkpoint_mismatch")
    identity = values["channel_id"].strip(" ")
    full = tx.fetch_one(_FULL_GENERATION, (identity, values["video_type"], values["scan_id"]))["present"]
    if values["error_code"] in _QUOTA_ERRORS: _fail("inventory_quota_defer_required")
    at = tx.fetch_one("SELECT clock_timestamp() AS at")["at"]
    try:
        policy = inventory_source_policy(values["video_type"], scan["scan_kind"], values["outcome"], 0,
            source["no_new_streak"], source["attempt_count"], source["discovery_error_count"], values["error_code"], at,
            schedule=getattr(require_cadence_snapshot(tx).inventory_by_type, values["video_type"]))
    except PolicyError as error:
        _fail(error.code, error.sqlstate)
    retain = policy.retain_scan and scan["token_history_verified"]
    if values["outcome"] == "error" and scan["token_history_verified"] and values["error_code"] not in _BAD_SCAN_ERRORS:
        # Preserve the retained raw channel-id comparison here; other identity
        # comparisons use btrim. Never acquire the parent job lock after playlist.
        if tx.fetch_one(_INITIAL_JOB, (values["channel_id"], values["channel_id"], values["scan_id"],
            values["lease_token"], values["video_type"], values["scan_id"]))["present"]:
            retain = True
    inventory_status = ("completed" if full or source["product_exposure_ready"] or
        (source["inventory_state_origin"] == "legacy_long_backfill" and source["inventory_status"] == "completed")
        else "running" if retain else "playlist_not_found" if values["outcome"] == "playlist_not_found" else "error")
    write = {**values, "channel_id": identity, "retain": retain, "inventory_status": inventory_status, "next_due_at": policy.next_due_at}
    tx.execute(_FAIL_SCAN, write)
    released = tx.returning_one(_FAIL_SOURCE, write)
    if released is None: _fail("typed_inventory_failure_lease_lost")
    result = {"scan_id": values["scan_id"], "channel_id": identity, "video_type": values["video_type"],
        "lease_token": values["lease_token"], "transition_id": values["transition_id"], "page_items": 0,
        "canonical_video_ids": [], "inserted": 0, "already_matching": 0, "provider_attempts": 1, "scan_retained": retain,
        "resume_page_token": scan["resume_page_token"] if retain else None, "status": "failed", "coverage": "partial",
        "scan_kind": scan["scan_kind"], "outcome": values["outcome"], "error_code": values["error_code"],
        "inventory_status": inventory_status, "next_due_at": released["next_due_at"], "replayed": False, "request_hash": request_hash}
    tx.execute(_INSERT_RECEIPT, (values["transition_id"], values["scan_id"], "failure", Jsonb(result), values["source_checked_at"]))
    _hot(tx, values["source_checked_at"])
    assert_channel_scope(tx, identity, source["lease_channel_epoch"])
    return result
