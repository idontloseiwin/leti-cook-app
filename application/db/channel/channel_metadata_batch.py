"""Metadata batch and initial-error writers used by ChannelSyncRepository.

Metadata control actions use the initial_job_id column UPDATE grant provided by
migration18. Borrowed channel workflow helpers apply normalization and
snapshots at the source-write boundary; native integrity guards remain
authoritative.
"""
from __future__ import annotations

from .channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, channel_mode_row)
from collections.abc import Mapping
from decimal import Decimal
import json
import re

from psycopg import sql
from psycopg.types.json import Jsonb

from .channel_control import assert_channel_scope, assert_channel_scopes
from ..base.operation_contracts import get_operation
from .channel_workflow_updates import apply_channel_stage, update_channel_fields
from ..base.postgres import DatabaseServerError, Operation

_CHANNEL_FIELDS = frozenset({"title", "avatar_url", "uploads_playlist_id", "country_code", "country_collected_at",
    "channel_created_at", "subscribers", "views", "video_count", "initial_inventory_estimate",
    "channel_video_count_changed_at", "channel_metadata_checked_at", "channel_metadata_next_after",
    "channel_metadata_status", "channel_metadata_started_at", "channel_metadata_error", "channel_metadata_error_count",
    "initial_long_inventory_status", "initial_28d_stats_status", "deep_inventory_error"})
_DISCOVERY_FIELDS = {"channel_discovery_next_after": "source_next_after", "channel_discovery_no_new_streak": "no_new_streak"}
_ACTIONS = frozenset({"no_long_restart", "no_long_manual_required"})
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_ABSENT = object()

_ORDERED = """
    SELECT value AS row, (value->>'control_epoch')::bigint AS epoch
    FROM jsonb_array_elements(%s::jsonb) ORDER BY value->>'channel_id'
"""
_ACTION_SOURCE = "SELECT initial_long_inventory_status AS status FROM public.channels WHERE channel_id=%s FOR UPDATE"
_MODE = ('SELECT policy_facts FROM (' + CHANNEL_CONTROL_FACTS_SQL + ') policy WHERE channel_id=%s')
_ACTION_JOB = "SELECT initial_job_id::text AS initial_job_id FROM public.channel_collection_state WHERE channel_id=%s"
_CHANNEL_LOCK = "SELECT channel_id FROM public.channels WHERE channel_id=%s FOR UPDATE"
_PLAYLIST_LOCK = "SELECT channel_id FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long' FOR UPDATE"
_CONTROL_SHARE = "SELECT to_jsonb(control) AS value FROM public.channel_collection_state control WHERE channel_id=%s FOR SHARE"
_CONTROL_UPDATE = "SELECT to_jsonb(control) AS value FROM public.channel_collection_state control WHERE channel_id=%s FOR UPDATE"
_CONTROL_RESTART = "UPDATE public.channel_collection_state SET initial_job_id=NULL WHERE channel_id=%s"
_CONTROL_MANUAL = """
    UPDATE public.channel_collection_state SET owner_permission='manual_required',initial_job_id=NULL,
        control_epoch=nextval('public.channel_collection_epoch_seq') WHERE channel_id=%s
"""
_DISCOVERY_UPDATE = """
    UPDATE public.channel_playlist_state target SET
        source_checked_at=proposed.source_checked_at,source_next_after=proposed.source_next_after,
        last_result=proposed.last_result,discovery_started_at=proposed.discovery_started_at,
        resume_page_token=proposed.resume_page_token,resume_started_at=proposed.resume_started_at,
        no_new_streak=greatest(coalesce(proposed.no_new_streak,0),0),discovery_error=proposed.discovery_error,
        discovery_error_count=proposed.discovery_error_count,
        next_due_at=coalesce(proposed.source_next_after,now()),
        priority=CASE WHEN proposed.resume_page_token IS NOT NULL THEN greatest(current.priority,100) ELSE current.priority END,
        status=CASE WHEN NOT %(eligible)s::boolean THEN 'paused'
                    WHEN current.status='leased' AND current.lease_expires_at>now() THEN 'leased' ELSE 'ready' END,
        lease_channel_epoch=CASE WHEN %(eligible)s::boolean AND current.status='leased' AND current.lease_expires_at>now() THEN current.lease_channel_epoch END,
        lease_token=CASE WHEN %(eligible)s::boolean AND current.status='leased' AND current.lease_expires_at>now() THEN current.lease_token END,
        lease_owner=CASE WHEN %(eligible)s::boolean AND current.status='leased' AND current.lease_expires_at>now() THEN current.lease_owner END,
        lease_run_id=CASE WHEN %(eligible)s::boolean AND current.status='leased' AND current.lease_expires_at>now() THEN current.lease_run_id END,
        lease_expires_at=CASE WHEN %(eligible)s::boolean AND current.status='leased' AND current.lease_expires_at>now() THEN current.lease_expires_at END,
        updated_at=now()
    FROM public.channel_playlist_state current
    CROSS JOIN LATERAL jsonb_populate_record(current,%(values)s::jsonb) proposed
    WHERE target.channel_id=current.channel_id AND target.video_type=current.video_type
      AND current.channel_id=%(channel_id)s AND current.video_type='long'
"""
_RECORD_ERROR = """
    UPDATE public.channels SET initial_long_inventory_status='error',initial_long_inventory_started_at=NULL,
        initial_long_inventory_error_code=%s,initial_long_inventory_error_at=now(),
        initial_long_inventory_error_count=initial_long_inventory_error_count+1
    WHERE channel_id=%s
    RETURNING jsonb_build_object('channel_id',channel_id,'error_code',initial_long_inventory_error_code,
        'error_at',initial_long_inventory_error_at,'error_count',initial_long_inventory_error_count) AS value
"""
_CLEAR_ERROR = """
    UPDATE public.channels SET initial_long_inventory_error_code=NULL,initial_long_inventory_error_at=NULL,
        initial_long_inventory_error_count=0 WHERE channel_id=%s RETURNING channel_id
"""
_RECORD_ARGUMENTS = "SELECT %s::text AS channel_id,%s::text AS error_code,%s::bigint AS epoch"
_CLEAR_ARGUMENTS = "SELECT %s::text AS channel_id,%s::bigint AS epoch"


def _fail(code): raise DatabaseServerError(code, code="P0001")


def _operation(name):
    spec = get_operation(name)
    return Operation(name, statement_timeout_ms=spec.statement_timeout_ms, lock_timeout_ms=spec.lock_timeout_ms,
                     total_timeout_ms=spec.statement_timeout_ms, error_codes=spec.error_codes)


def _epoch(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        _fail("channel_collection_scope_invalid")


def _positive_integer_spelling(value):
    # Envelope syntax and bigint range are separate retained stages: an otherwise
    # valid out-of-range JSON integer raises native 22003 before any row lock.
    if type(value) not in (int, float): return False
    try: number = Decimal(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError): return False
    return number > 0 and number.as_tuple().exponent >= 0


def _validate(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > 50:
        _fail("channel_metadata_batch_invalid")
    seen, clean = set(), []
    for row in rows:
        if not isinstance(row, Mapping): _fail("channel_metadata_batch_invalid")
        identity, values = row.get("channel_id"), row.get("values")
        action = row.get("control_action", _ABSENT)
        if (not isinstance(identity, str) or not identity or identity.strip(" ") != identity or len(identity) > 128
                or identity in seen or not isinstance(values, Mapping) or not values
                or not _positive_integer_spelling(row.get("control_epoch"))
                or set(row) - {"channel_id", "control_epoch", "values", "control_action"}
                or (action is not _ABSENT and (not isinstance(action, str) or action not in _ACTIONS))):
            _fail("channel_metadata_batch_invalid")
        if ((action == "no_long_restart" and (values.get("initial_long_inventory_status") != "pending"
                                              or values.get("initial_28d_stats_status") != "pending"))
                or (action == "no_long_manual_required" and (values.get("initial_long_inventory_status") != "manual_required"
                    or values.get("initial_28d_stats_status", _ABSENT) is not None))):
            _fail("channel_metadata_batch_invalid")
        clean.append({**row, "values": dict(values)})
        seen.add(identity)
    # Field rejection follows complete envelope validation, not each row's visit.
    if any(set(row["values"]) - (_CHANNEL_FIELDS | _DISCOVERY_FIELDS.keys()) for row in clean):
        _fail("channel_metadata_batch_field_invalid")
    return clean


def _update_channel(tx, identity, values):
    return update_channel_fields(tx, identity, values)


def _update_discovery(tx, identity, discovery, values):
    if tx.fetch_one(_CHANNEL_LOCK, (identity,)) is None:
        return 0
    if values:
        _update_channel(tx, identity, values)
    if tx.fetch_one(_PLAYLIST_LOCK, (identity,)) is None:
        _fail("channel_discovery_state_missing")
    mode = channel_mode_row(tx.fetch_one(_MODE, (identity,)))
    tx.execute(_DISCOVERY_UPDATE, {"channel_id": identity, "values": Jsonb(discovery),
                                 "eligible": bool(mode and mode["collection_mode"] == "active")})
    # The retained helper returns 1 even if the quota-floor BEFORE trigger skips
    # this UPDATE. Never activate runtime or invent a new per-row failure here.
    return 1


def update_channel_metadata_batch(database, rows):
    with database.transaction(_operation("update_channel_metadata_batch")) as tx:
        result = update_metadata_in_transaction(tx, rows)
    return result


def update_metadata_in_transaction(transaction, rows):
    with transaction.borrow():
        clean = _validate(rows)
        if not clean: return []
        # Native bigint casts validate every scope and database collation owns
        # the stable write/lock order; no Python lexical-order assumption.
        ordered = transaction.fetch_all(_ORDERED, (Jsonb(clean),), max_rows=len(clean))
        scopes, actions, result = [], {}, []
        for item in ordered:
            row, epoch = item["row"], item["epoch"]
            identity, values = row["channel_id"], row["values"]
            scopes.append({"channel_id": identity, "control_epoch": epoch})
            action, previous_job = row.get("control_action"), None
            try:
                with transaction.savepoint():
                    if action is not None:
                        source = transaction.fetch_one(_ACTION_SOURCE, (identity,))
                        mode = channel_mode_row(transaction.fetch_one(_MODE, (identity,))) if source is not None else None
                        if source is None or source["status"] != "skipped_no_long_playlist" or not mode or mode["collection_mode"] != "metadata_only":
                            _fail("channel_metadata_control_action_state_changed")
                        control = transaction.fetch_one(_ACTION_JOB, (identity,))
                        if control is None: _fail("channel_collection_scope_lost")
                        previous_job = control["initial_job_id"]
                    discovery = {_DISCOVERY_FIELDS[key]: value for key, value in values.items() if key in _DISCOVERY_FIELDS}
                    channel = {key: value for key, value in values.items() if key not in _DISCOVERY_FIELDS}
                    count = (_update_discovery(transaction, identity, discovery, channel) if discovery
                             else _update_channel(transaction, identity, channel))
                if count == 1 and action is not None:
                    actions[identity] = (action, previous_job)
                result.append({"channel_id": identity, "status": "updated" if count == 1 else "missing"})
            except DatabaseServerError as error:
                # PL/pgSQL WHEN OTHERS excludes cancellation and assertion_failure;
                # connection/unknown-outcome errors also never become row receipts.
                if error.code in {"57014", "P0004"}: raise
                result.append({"channel_id": identity, "status": "error", "error_code": error.code})
        if not actions:
            assert_channel_scopes(transaction, scopes)
        else:
            # Mixed modes are acquired once per channel in the exact same sorted
            # order. Do not take SHARE first and later upgrade an action's lock.
            for scope in scopes:
                identity = scope["channel_id"]
                action = actions.get(identity)
                row = transaction.fetch_one(_CONTROL_UPDATE if action else _CONTROL_SHARE, (identity,))
                held = row["value"] if row else None
                if (held is None or held["is_hidden"] is not False or held["owner_permission"] != "accepted"
                        or held["control_epoch"] != scope["control_epoch"]):
                    _fail("channel_collection_scope_lost")
                if action:
                    if held["initial_job_id"] != action[1]: _fail("channel_collection_scope_lost")
                    transaction.execute(_CONTROL_MANUAL if action[0] == "no_long_manual_required" else _CONTROL_RESTART, (identity,))
        return result


def record_channel_initial_inventory_error(database, channel_id, error_code, expected_epoch):
    with database.transaction(_operation("record_channel_initial_inventory_error")) as tx:
        with tx.borrow():
            arguments = tx.fetch_one(_RECORD_ARGUMENTS, (channel_id, error_code, expected_epoch))
            channel_id, error_code, expected_epoch = arguments["channel_id"], arguments["error_code"], arguments["epoch"]
            _epoch(expected_epoch)
            if not isinstance(channel_id, str) or not channel_id.strip(" "):
                _fail("channel_sync_channel_required")
            if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
                _fail("channel_sync_error_code_invalid")
            row = apply_channel_stage(tx, 'record_error', (error_code, channel_id))
            if row is None: _fail("channel_sync_channel_not_found")
            assert_channel_scope(tx, channel_id, expected_epoch)
            result = row["value"]
    return result


def clear_channel_initial_inventory_error(database, channel_id, expected_epoch):
    with database.transaction(_operation("clear_channel_initial_inventory_error")) as tx:
        with tx.borrow():
            arguments = tx.fetch_one(_CLEAR_ARGUMENTS, (channel_id, expected_epoch))
            channel_id, expected_epoch = arguments["channel_id"], arguments["epoch"]
            _epoch(expected_epoch)
            row = tx.returning_one(_CLEAR_ERROR, (channel_id,))
            if row is None: _fail("channel_sync_channel_not_found")
            assert_channel_scope(tx, channel_id, expected_epoch)
            result = {"channel_id": row["channel_id"], "cleared": True}
    return result
