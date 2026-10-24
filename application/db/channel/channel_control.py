"""Atomic channel-control writers used by synchronization and trash repositories.

Requires the reviewed F6 column/sequence grants and column-aware readiness.
Channel workflow helpers own initialization, normalization and snapshots;
native integrity guards remain authoritative. No provider work, callback
transaction, nested pool or retry.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import json
import re

from psycopg import sql
from psycopg.types.json import Jsonb

from ..base.operation_contracts import get_operation
from .channel_workflow_updates import update_channel_fields
from .channel_workflow_inserts import insert_collection_channel
from ..base.postgres import DatabaseError, DatabaseServerError, Operation
from ..base.sql_values import application_value


CHANNEL_FIELDS = frozenset({
    "title", "avatar_url", "uploads_playlist_id", "country_code", "country_collected_at",
    "channel_created_at", "subscribers", "views", "video_count", "initial_inventory_estimate",
    "channel_video_count_changed_at", "channel_metadata_checked_at", "channel_metadata_next_after",
    "channel_metadata_status", "channel_metadata_started_at", "channel_metadata_error",
    "channel_metadata_error_count", "initial_long_inventory_status", "initial_long_inventory_started_at",
    "initial_long_inventory_error_code", "initial_long_inventory_error_at", "initial_long_inventory_error_count",
    "initial_28d_stats_status", "initial_28d_stats_started_at", "deep_inventory_status", "deep_inventory_started_at",
    "deep_inventory_checked_at", "deep_inventory_error",
})
CREATE_FIELDS = CHANNEL_FIELDS | {"channel_id", "added_at", "user_input", "is_hidden", "hidden_at"}
_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_PERMISSIONS = frozenset({"accepted", "manual_required", "legacy_hold"})

_CONTROL = """
    SELECT to_jsonb(control) AS value FROM public.channel_collection_state control
    WHERE channel_id = %s FOR UPDATE
"""
_SCOPE = """
    SELECT channel_id, owner_permission, is_hidden, control_epoch
    FROM public.channel_collection_state WHERE channel_id = ANY(%s::text[])
    ORDER BY channel_id FOR SHARE
"""
_CREATE_RESULT = """
    SELECT to_jsonb(channel) AS value,
           EXISTS (SELECT 1 FROM public.channel_playlist_state source
                   WHERE source.channel_id = channel.channel_id AND source.video_type = 'long') AS long_initialized
    FROM public.channels_with_control channel WHERE channel.channel_id = %s
"""
_SET_PERMISSION = """
    UPDATE public.channel_collection_state
    SET owner_permission = %s, control_epoch = nextval('public.channel_collection_epoch_seq')
    WHERE channel_id = %s RETURNING channel_id, owner_permission, control_epoch
"""
_TRASH_INPUT = """
    SELECT to_jsonb(clock_timestamp()) #>> '{}' AS stamp,
           to_jsonb(%s::uuid) #>> '{}' AS user_work_id,
           to_jsonb(%s::timestamptz) #>> '{}' AS expected_hidden_at
"""
_TRASH_LOCK = """
    SELECT to_jsonb(control) AS value,
           hidden_at IS NOT DISTINCT FROM %s::timestamptz AS hidden_at_matches,
           COALESCE(isfinite(hidden_at) AND hidden_at > %s::timestamptz - interval '696 hours'
                    AND hidden_at <= %s::timestamptz, false) AS restorable
    FROM public.channel_collection_state control
    WHERE CASE WHEN %s::uuid IS NOT NULL THEN user_work_id = %s::uuid ELSE channel_id = %s END
    FOR UPDATE
"""
_HIDE = """
    UPDATE public.channel_collection_state SET is_hidden = true, hidden_at = %s::timestamptz,
        control_epoch = nextval('public.channel_collection_epoch_seq') WHERE channel_id = %s
"""
_RESTORE = """
    UPDATE public.channel_collection_state SET is_hidden = false, hidden_at = NULL,
        control_epoch = nextval('public.channel_collection_epoch_seq') WHERE channel_id = %s
"""
_SUSPEND_JOB = """
    UPDATE public.operator_jobs job SET status = 'suspended', finished_at = coalesce(job.finished_at, now()),
        updated_at = now(), lease_token = NULL, lease_channel_epoch = NULL, lease_owner = NULL,
        lease_expires_at = NULL, last_error_code = 'channel_sync_not_ready',
        last_error_summary = 'Channel control generation changed.'
    WHERE job.id = (SELECT active.id FROM public.operator_jobs active
                   WHERE active.channel_id = %s AND active.status IN ('queued', 'running')
                   FOR UPDATE SKIP LOCKED)
"""
_RELEASE_PLAYLISTS = """
    UPDATE public.channel_playlist_state source
    SET status = CASE WHEN source.runtime_enabled THEN 'ready' ELSE 'paused' END,
        lease_token = NULL, lease_channel_epoch = NULL, lease_owner = NULL, lease_run_id = NULL,
        lease_expires_at = NULL,
        next_due_at = CASE WHEN source.runtime_enabled
                     THEN greatest(coalesce(source.next_due_at, %s::timestamptz), %s::timestamptz + interval '30 seconds')
                     ELSE source.next_due_at END,
        discovery_started_at = NULL, updated_at = %s::timestamptz
    FROM (SELECT locked.channel_id, locked.video_type FROM public.channel_playlist_state locked
          WHERE locked.channel_id = %s AND locked.lease_token IS NOT NULL
          ORDER BY locked.video_type FOR UPDATE SKIP LOCKED) held
    WHERE source.channel_id = held.channel_id AND source.video_type = held.video_type
"""


def _fail(code):
    raise DatabaseServerError(code, code="P0001")


def _operation(name):
    # Preserve each retained entry point's settings and finite error disclosure.
    # One function's former statement budget covers the whole Python transaction.
    spec = get_operation(name)
    return Operation(name, statement_timeout_ms=spec.statement_timeout_ms,
                     lock_timeout_ms=spec.lock_timeout_ms, total_timeout_ms=spec.statement_timeout_ms,
                     error_codes=spec.error_codes)


def _epoch(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        _fail("channel_collection_scope_invalid")


def _json_object(row):
    if not isinstance(row, Mapping) or not isinstance(row.get("value"), dict):
        raise DatabaseError("database_result_shape_invalid")
    return row["value"]


def assert_channel_scopes(transaction, scopes):
    """Validate the whole finite JSON batch, then lock in database text order.

    Locks belong to the caller's transaction and survive until its COMMIT. The
    retained JSONB rule rejects fractional spelling (including 1.0), duplicates,
    extra keys and overflow before taking any lock; an empty array is valid.
    """
    with transaction.borrow():
        _assert_channel_scopes(transaction, scopes)


def _assert_channel_scopes(transaction, scopes):
    if not isinstance(scopes, list) or len(scopes) > 1000:
        _fail("channel_collection_scope_invalid")
    expected = {}
    for item in scopes:
        if not isinstance(item, Mapping) or set(item) != {"channel_id", "control_epoch"}:
            _fail("channel_collection_scope_invalid")
        identity, number = item["channel_id"], item["control_epoch"]
        if (not isinstance(identity, str) or not identity or identity.strip(" ") != identity
                or len(identity) > 128 or identity in expected or type(number) not in (int, float)):
            _fail("channel_collection_scope_invalid")
        # jsonb expands positive scientific exponents, while retaining fractional
        # scale. Match that syntax before the same positive int64 range check.
        try:
            numeric = Decimal(json.dumps(number, allow_nan=False))
        except (ValueError, TypeError):
            _fail("channel_collection_scope_invalid")
        if numeric.as_tuple().exponent < 0 or not 0 < numeric <= 9223372036854775807:
            _fail("channel_collection_scope_invalid")
        expected[identity] = int(numeric)
    if not expected:
        return
    rows = transaction.fetch_all(_SCOPE, (list(expected),), max_rows=len(expected))
    if len(rows) != len(expected):
        _fail("channel_collection_scope_lost")
    for held in rows:
        if (held.get("is_hidden") is not False or held.get("owner_permission") != "accepted"
                or held.get("control_epoch") != expected.get(held.get("channel_id"))):
            _fail("channel_collection_scope_lost")


def assert_channel_scope(transaction, channel_id, expected_epoch):
    """Final one-channel fence on the caller's transaction, held through COMMIT.

    This is not the weaker Saved visibility fence. It additionally requires
    accepted collection permission and the exact caller-captured generation.
    """
    with transaction.borrow():
        _epoch(expected_epoch)
        _assert_channel_scopes(transaction, [{"channel_id": channel_id, "control_epoch": expected_epoch}])


def create_channel_for_collection(database, values, permission):
    if (permission not in ("accepted", "manual_required") or not isinstance(values, Mapping)
            or not isinstance(values.get("channel_id"), str) or not _CHANNEL_ID.fullmatch(values["channel_id"])
            or any(key not in CREATE_FIELDS for key in values)
            or ("is_hidden" in values and values["is_hidden"] is not False)
            or ("hidden_at" in values and values["hidden_at"] is not None)):
        _fail("channel_collection_create_invalid")
    with database.transaction(_operation("create_channel_for_collection")) as tx:
        row = insert_collection_channel(tx, values)
        if not isinstance(row, Mapping) or row.get("channel_id") != values["channel_id"]:
            raise DatabaseError("database_result_shape_invalid")
        channel_id = row["channel_id"]
        changed = tx.execute("UPDATE public.channel_collection_state SET owner_permission = %s WHERE channel_id = %s",
                             (permission, channel_id))
        if changed != 1:
            _fail("channel_collection_state_missing")
        row = tx.fetch_one(_CREATE_RESULT, (channel_id,))
        result = _json_object(row)
        # The composed INSERT effects already initialized Long
        # while permission was legacy_hold. Never reactivate or duplicate it.
        # Omitting the old redundant ON CONFLICT insert avoids requiring the
        # owner-only inventory dispatch sequence; only an unused gap disappears.
        if row.get("long_initialized") is not True:
            raise DatabaseError("database_result_shape_invalid")
    return result


def update_channel_collection_fields(database, channel_id, values, expected_epoch):
    _epoch(expected_epoch)
    if (not isinstance(channel_id, str) or not channel_id.strip(" ") or not isinstance(values, Mapping)
            or not values or any(key not in CHANNEL_FIELDS for key in values)):
        _fail("channel_collection_fields_invalid")
    with database.transaction(_operation("update_channel_collection_fields")) as tx:
        affected = update_channel_fields(tx, channel_id, values)
        assert_channel_scope(tx, channel_id, expected_epoch)
    return affected


def set_channel_collection_permission(database, channel_id, expected_epoch, permission):
    _epoch(expected_epoch)
    if not isinstance(permission, str) or permission not in _PERMISSIONS:
        _fail("channel_collection_scope_invalid")
    with database.transaction(_operation("set_channel_collection_permission")) as tx:
        row = tx.fetch_one(_CONTROL, (channel_id,))
        if row is None:
            _fail("channel_collection_scope_lost")
        held = _json_object(row)
        if held.get("is_hidden") is not False or held.get("control_epoch") != expected_epoch:
            _fail("channel_collection_scope_lost")
        if held["owner_permission"] != permission:
            held = tx.returning_one(_SET_PERMISSION, (permission, channel_id))
            if held is None:
                _fail("channel_collection_scope_lost")
        result = {"channel_id": held["channel_id"], "collection_permission": held["owner_permission"],
                  "collection_control_epoch": held["control_epoch"]}
    return application_value(result)


def set_channel_trash_state(database, channel_id=None, user_work_id=None, expected_hidden_at=None, restore=False):
    if (type(restore) is not bool or (restore and ((user_work_id is None and channel_id is None) or expected_hidden_at is None))
            or (channel_id is not None and (not isinstance(channel_id, str) or not _CHANNEL_ID.fullmatch(channel_id)))
            or (not restore and channel_id is None)):
        _fail("channel_trash_invalid_request")
    with database.transaction(_operation("set_channel_trash_state")) as tx:
        # Native uuid/timestamp grammar and canonical JSON preserve infinity and
        # finite PG years beyond datetime's range. This stamp precedes the lock.
        inputs = tx.fetch_one(_TRASH_INPUT, (user_work_id, expected_hidden_at))
        stamp, owner_id, expected = inputs["stamp"], inputs["user_work_id"], inputs["expected_hidden_at"]
        row = tx.fetch_one(_TRASH_LOCK, (expected, stamp, stamp, owner_id, owner_id, channel_id))
        if row is None:
            result = {"outcome": "not_found"}
        else:
            control = _json_object(row)
            if restore and (control["is_hidden"] is not True or row["hidden_at_matches"] is not True):
                result = {"outcome": "conflict"}
            elif restore and row["restorable"] is not True:
                result = {"outcome": "expired"}
            else:
                identity = control["channel_id"]
                if restore:
                    tx.execute(_RESTORE, (identity,))
                elif control["is_hidden"] is not True:
                    tx.execute(_HIDE, (stamp, identity))
                    control["hidden_at"] = stamp
                tx.execute(_SUSPEND_JOB, (identity,))
                tx.execute(_RELEASE_PLAYLISTS, (stamp, stamp, stamp, identity))
                result = {"outcome": "restored" if restore else "removed", "channel_id": identity,
                          "id": control["user_work_id"], "removed_at": None if restore else control["hidden_at"]}
    return result
