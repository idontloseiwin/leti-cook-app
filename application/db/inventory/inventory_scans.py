"""Typed-inventory scan begin protocol used by TypedInventoryRepository.

Playlist -> format projection -> scan is the retained lock/read order. A terminal
scan replays before lease/source/eligibility checks; running replay and creation
hold the final canonical scope through commit. Provider work never enters here.
Native UUID/timestamp grammar, clock comparisons and table constraints remain in
PostgreSQL; JSON clock projection avoids Python's narrower finite-year range.
"""
from __future__ import annotations

from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, channel_eligible_row)
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError, DatabaseServerError, Operation


_INPUT = """
    SELECT to_jsonb(input) AS value,
           coalesce(isfinite(scan_boundary_at) AND scan_boundary_at >= now()-interval '696 hours'
                    AND scan_boundary_at <= now()+interval '5 minutes', false) AS boundary_valid
    FROM (SELECT %s::uuid AS lease_token, %s::uuid AS scan_id, %s::timestamptz AS scan_boundary_at) input
"""
_PLAYLIST = """
    SELECT to_jsonb(source) AS value FROM public.channel_playlist_state source
    WHERE channel_id=%s AND video_type=%s FOR UPDATE
"""
_FORMAT = """
    SELECT active_scan_id::text AS active_scan_id FROM public.channel_video_inventory_state
    WHERE channel_id=%s AND video_type=%s
"""
_SCAN = """
    SELECT to_jsonb(scan) AS value, scan_boundary_at IS NOT DISTINCT FROM %s::timestamptz AS boundary_matches
    FROM public.channel_video_inventory_scans scan WHERE scan_id=%s::uuid FOR UPDATE
"""
_LEASE_CLOCK = "SELECT coalesce(%s::timestamptz > clock_timestamp(), false) AS fresh"
_ELIGIBLE = ('SELECT (SELECT policy_facts FROM public.channels channel JOIN (' + CHANNEL_CONTROL_FACTS_SQL + ') control USING(channel_id) WHERE channel.channel_id=%s) AS policy_facts')
_HOT_CLOCK = """
    WITH observed AS MATERIALIZED (SELECT clock_timestamp() AS stamp)
    SELECT coalesce(isfinite(%s::timestamptz) AND %s::timestamptz >= stamp-interval '696 hours'
                    AND %s::timestamptz <= stamp, false) AS valid FROM observed
"""
_FULL_GENERATION = """
    SELECT EXISTS(SELECT 1 FROM public.channel_video_inventory_scans
        WHERE channel_id=%s AND video_type=%s AND status='completed' AND coverage='complete'
          AND scan_kind IN ('initial_inventory','full_reconciliation')) AS present
"""
_INSERT = """
    INSERT INTO public.channel_video_inventory_scans
        (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,scan_boundary_at,
         resume_page_token,pages_completed,items_seen,new_memberships_found,token_history_verified,
         started_at,created_at,updated_at)
    VALUES (%s::uuid,%s,%s,%s,%s,'running','partial',%s::timestamptz,NULL,0,0,0,true,now(),now(),now())
"""
_BIND = """
    UPDATE public.channel_playlist_state SET playlist_id=%s,
        inventory_status=CASE WHEN inventory_status='completed' OR last_completed_scan_id IS NOT NULL
                              THEN 'completed' ELSE 'running' END,
        inventory_last_error_code=NULL,inventory_last_error_at=NULL,
        inventory_state_origin=CASE WHEN inventory_state_origin='legacy_long_backfill' AND last_completed_scan_id IS NULL
                                    THEN inventory_state_origin ELSE 'typed_native' END,
        inventory_updated_at=now(),active_scan_id=%s::uuid,resume_page_token=NULL,resume_started_at=now(),
        scan_boundary_at=%s::timestamptz,last_error_code=NULL,updated_at=now()
    WHERE channel_id=%s AND video_type=%s AND status='leased' AND lease_token=%s::uuid
"""
_KINDS = frozenset({"initial_inventory", "full_reconciliation", "discovery"})
_TYPES = frozenset({"long", "short", "stream"})
_RESULT_FIELDS = ("scan_id", "status", "coverage", "scan_kind", "scan_boundary_at", "resume_page_token",
                  "pages_completed", "items_seen")


def _fail(message, code="P0001"):
    raise DatabaseServerError(message, code=code)


def _value(row):
    if not isinstance(row, dict) or not isinstance(row.get("value"), dict):
        raise DatabaseError("database_result_shape_invalid")
    return row["value"]


def _result(scan, *, replayed):
    return {**{field: scan[field] for field in _RESULT_FIELDS}, "replayed": replayed}


def begin_typed_inventory_scan(database, *, channel_id, video_type, lease_token, scan_id, scan_kind, scan_boundary_at):
    """Own one transaction and return only after its known successful COMMIT."""
    spec = get_operation("begin_typed_inventory_scan")
    operation = Operation(spec.name, statement_timeout_ms=spec.statement_timeout_ms,
                          lock_timeout_ms=spec.lock_timeout_ms, total_timeout_ms=spec.statement_timeout_ms,
                          error_codes=spec.error_codes)
    with database.transaction(operation) as tx:
        result = begin_scan_in_transaction(tx, channel_id=channel_id, video_type=video_type,
            lease_token=lease_token, scan_id=scan_id, scan_kind=scan_kind, scan_boundary_at=scan_boundary_at)
    return result


def begin_scan_in_transaction(transaction, *, channel_id, video_type, lease_token, scan_id, scan_kind, scan_boundary_at):
    """Borrow caller ownership; caught domain failures still require rollback.

    Reusable by a future composed initial-job protocol without opening a second
    transaction, refreshing its total time budget, or acknowledging early.
    """
    with transaction.borrow():
        return _begin(transaction, channel_id, video_type, lease_token, scan_id, scan_kind, scan_boundary_at)


def _begin(tx, channel_id, video_type, lease_token, scan_id, scan_kind, scan_boundary_at):
    # PostgreSQL parses function arguments before entering its body. Preserve
    # native input errors even when a later domain field is invalid.
    inputs = tx.fetch_one(_INPUT, (lease_token, scan_id, scan_boundary_at))
    typed = _value(inputs)
    channel_id = channel_id.strip(" ") if isinstance(channel_id, str) else channel_id
    if (not isinstance(channel_id, str) or not channel_id
            or (video_type is not None and video_type not in _TYPES)
            or typed["lease_token"] is None or typed["scan_id"] is None
            or (scan_kind is not None and scan_kind not in _KINDS)
            or inputs["boundary_valid"] is not True):
        _fail("typed_inventory_scan_identity_invalid")
    lease_token, scan_id, boundary = typed["lease_token"], typed["scan_id"], typed["scan_boundary_at"]
    row = tx.fetch_one(_PLAYLIST, (channel_id, video_type))
    if row is None:
        _fail("typed_inventory_discovery_state_missing")
    source = _value(row)
    inventory = tx.fetch_one(_FORMAT, (channel_id, video_type))
    if inventory is None:
        _fail("typed_inventory_format_state_missing")
    row = tx.fetch_one(_SCAN, (boundary, scan_id))
    scan = _value(row) if row is not None else None
    if scan is not None:
        if (scan["channel_id"] != channel_id or scan["video_type"] != video_type
                or scan["playlist_id"] != source["playlist_id"] or scan["scan_kind"] != scan_kind
                or row["boundary_matches"] is not True):
            _fail("typed_inventory_scan_identity_conflict")
        if scan["status"] != "running":
            return _result(scan, replayed=True)
    if (source["runtime_enabled"] is not True or source["status"] != "leased"
            or source["lease_token"] != lease_token or source["lease_expires_at"] is None):
        _fail("typed_inventory_scan_lease_not_owned")
    if tx.fetch_one(_LEASE_CLOCK, (source["lease_expires_at"],))["fresh"] is not True:
        _fail("typed_inventory_scan_lease_not_owned")
    if channel_eligible_row(tx.fetch_one(_ELIGIBLE, (channel_id,)))["eligible"] is not True:
        _fail("typed_inventory_channel_not_eligible")
    if scan is not None:
        if scan["token_history_verified"] is False:
            _fail("inventory_token_history_invalid")
        # Deliberately after the scan lock and token-history check. Each retained
        # source assertion samples its own wall clock; do not reuse lease/now().
        for field in ("scan_boundary_at", "started_at"):
            clock = scan[field]
            if tx.fetch_one(_HOT_CLOCK, (clock, clock, clock))["valid"] is not True:
                _fail("hot_source_clock_out_of_window", "22023")
        if source["active_scan_id"] != scan_id or inventory["active_scan_id"] != scan_id:
            _fail("typed_inventory_active_scan_not_owned")
        assert_channel_scope(tx, channel_id, source["lease_channel_epoch"])
        return _result(scan, replayed=True)
    if source["active_scan_id"] is not None or inventory["active_scan_id"] is not None:
        _fail("typed_inventory_active_scan_conflict")
    if scan_kind == "discovery" and tx.fetch_one(_FULL_GENERATION, (channel_id, video_type))["present"] is not True:
        _fail("typed_inventory_discovery_requires_full_generation")
    if tx.execute(_INSERT, (scan_id, channel_id, video_type, source["playlist_id"], scan_kind, boundary)) != 1:
        raise DatabaseError("database_result_shape_invalid")
    if tx.execute(_BIND, (source["playlist_id"], scan_id, boundary, channel_id, video_type, lease_token)) == 0:
        _fail("typed_inventory_scan_lease_lost")
    assert_channel_scope(tx, channel_id, source["lease_channel_epoch"])
    return {"scan_id": scan_id, "status": "running", "coverage": "partial", "scan_kind": scan_kind,
            "scan_boundary_at": boundary, "resume_page_token": None, "pages_completed": 0, "items_seen": 0,
            "replayed": False}
