"""Database contract for the channel synchronization control plane."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from ..base.repository import DatabaseRepository
from ..base.operation_contracts import get_operation
from ..base.sql_values import application_value
from .channel_discovery_projection import CHANNEL_DISCOVERY_RELATION
from ..readiness import capabilities
from . import channel_control, channel_metadata_batch
from ..reads.operational_read_queries import get_channel_sync_control_health
from ..base.postgres import DatabaseServerError
from channel_sync_policy import channel_sync_eligibility
from channel_collection_scope import channel_scope, require_control_epoch, validate_channel_scopes
from user_channel_input import normalize_user_channel_input


OPTIONAL_CHANNEL_PROCESS_COLUMNS = frozenset({
    "initial_long_inventory_started_at",
    "initial_28d_stats_started_at",
    "channel_discovery_started_at",
    "channel_metadata_started_at",
    "channel_metadata_checked_at",
    "channel_metadata_next_after",
    "channel_metadata_status",
    "channel_metadata_error",
    "channel_metadata_error_count",
    "channel_video_count_changed_at",
    "country_code",
    "country_collected_at",
    "channel_created_at",
})


class ChannelMetadataColumnsUnavailableError(RuntimeError):
    """The deployed database does not expose the optional metadata columns."""


def _channel_metadata_columns_unavailable(error: Exception) -> bool:
    return isinstance(error, DatabaseServerError) and error.code == "42703"


def _optional_process_columns_unavailable(
    error: Exception,
    values: Mapping[str, Any],
) -> bool:
    return (
        any(column in values for column in OPTIONAL_CHANNEL_PROCESS_COLUMNS)
        and isinstance(error, DatabaseServerError) and error.code == "42703"
    )


def _mapping(value: Any, operation: str) -> dict[str, Any]:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, Mapping):
        raise TypeError(f"{operation} returned a non-object response")
    return dict(value)


_METADATA_CLOCK = "SELECT now() AS at"
_METADATA_CANDIDATE_PAGE = """WITH physical AS MATERIALIZED (
 SELECT channel_id,channel_metadata_next_after FROM public.channels
 WHERE (channel_metadata_next_after IS NULL
        OR channel_metadata_next_after<=%(due_ceiling)s::timestamptz)
 AND (%(after_id)s::text IS NULL
  OR (%(after_due)s::timestamptz IS NULL AND channel_metadata_next_after IS NULL
      AND channel_id>%(after_id)s::text)
  OR (%(after_due)s::timestamptz IS NOT NULL
      AND (channel_metadata_next_after>%(after_due)s::timestamptz
       OR (channel_metadata_next_after=%(after_due)s::timestamptz
           AND channel_id>%(after_id)s::text)
       OR channel_metadata_next_after IS NULL)))
 ORDER BY channel_metadata_next_after ASC NULLS LAST,channel_id ASC
 LIMIT %(limit)s::integer)
 SELECT physical.channel_id,physical.channel_metadata_next_after,
 c.collection_mode,c.collection_permission,c.collection_control_epoch,
 c.is_hidden,c.channel_metadata_status
 FROM physical LEFT JOIN public.channels_with_control c USING(channel_id)
 ORDER BY physical.channel_metadata_next_after ASC NULLS LAST,physical.channel_id ASC
"""
_METADATA_CAPTURE = """WITH requested AS (
 SELECT * FROM unnest(%(channel_ids)s::text[],%(epochs)s::bigint[])
 WITH ORDINALITY AS scopes(channel_id,control_epoch,position))
 SELECT c.channel_id,c.title,c.video_count,c.channel_metadata_next_after,
 c.collection_mode,c.collection_permission,c.collection_control_epoch,
 c.channel_metadata_status,c.channel_metadata_error_count,
 c.initial_long_inventory_status,c.deep_inventory_status,c.is_hidden,
 c.initial_long_inventory_error_code,c.initial_long_inventory_error_count
 FROM requested JOIN public.channels_with_control c
 ON c.channel_id=requested.channel_id AND c.collection_control_epoch=requested.control_epoch
 WHERE c.channel_metadata_next_after IS NULL
    OR c.channel_metadata_next_after<=%(due_ceiling)s::timestamptz
 ORDER BY requested.position LIMIT %(limit)s::integer
"""


def _metadata_cursor_clock(value):
    if type(value) is not str or len(value) != 32:
        raise ValueError('metadata_candidate_clock_invalid')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('metadata_candidate_clock_invalid') from None
    if (parsed.utcoffset() != timedelta(0)
            or parsed.astimezone(timezone.utc).isoformat(timespec='microseconds') != value):
        raise ValueError('metadata_candidate_clock_invalid')
    return value


def _metadata_native_clock(value):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() != timedelta(0)):
        raise ValueError('metadata_candidate_clock_invalid')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _metadata_channel_id(value):
    if (type(value) is not str or not value or len(value) > 128
            or value.strip() != value):
        raise ValueError('metadata_candidate_identity_invalid')
    return value


@dataclass(frozen=True)
class MetadataCandidateCursor:
    """Advisory due key; NULL next_after is the final bucket, never page start."""
    next_after: str | None
    channel_id: str

    def __post_init__(self):
        if self.next_after is not None:
            _metadata_cursor_clock(self.next_after)
        _metadata_channel_id(self.channel_id)


class ChannelSyncRepository(DatabaseRepository):
    """Owns channel lifecycle, metadata, and coverage persistence."""

    def schema_status(self) -> dict[str, Any]:
        return _mapping(
            capabilities.assert_channel_sync_control_ready(self.database),
            "channel sync schema assertion",
        )

    def health(self) -> dict[str, Any]:
        return _mapping(
            get_channel_sync_control_health(self.database),
            "channel sync health",
        )

    @staticmethod
    def _chunks(values: list[str], size: int = 100) -> Iterable[list[str]]:
        for start in range(0, len(values), size):
            yield values[start:start + size]

    def _count_for_channels(
        self,
        channel_ids: list[str],
        *, paused_reasons: list[str] | None = None,
    ) -> int:
        total = 0
        for batch in self._chunks(channel_ids):
            predicate = "paused_reason IS NULL" if paused_reasons is None else "paused_reason = ANY(%s::text[])"
            parameters = (batch,) if paused_reasons is None else (batch, paused_reasons)
            row = self.database.read_row(
                "channel_sync_collection_count",
                "SELECT count(*) AS count FROM public.video_collection_state "
                "WHERE channel_id = ANY(%s::text[]) AND " + predicate,
                parameters,
            )
            total += int(row["count"])
        return total

    def eligibility_preflight(self) -> dict[str, Any]:
        """Return aggregate, quota-neutral effects before or after migration."""

        channels = self.database.read_rows(
            "channel_sync_eligibility_preflight",
            "SELECT channel_id, is_hidden, collection_mode, collection_permission, collection_control_epoch "
            "FROM public.channels_with_control",
            max_rows=10000,
        )
        policies = [
            (row, channel_sync_eligibility(row))
            for row in channels
            if isinstance(row, Mapping) and row.get("channel_id")
        ]
        active_ids = [
            str(row["channel_id"])
            for row, policy in policies
            if policy.mode == "active"
        ]
        paused_ids = [
            str(row["channel_id"])
            for row, policy in policies
            if policy.mode != "active"
        ]
        channel_pause_reasons = [
            "channel_hidden",
            "channel_no_long_playlist",
            "channel_inventory_not_ready",
            "channel_inventory_manual_required",
            "channel_inventory_error",
        ]
        collection_to_pause = self._count_for_channels(
            paused_ids,
        ) if paused_ids else 0
        collection_to_resume = self._count_for_channels(
            active_ids,
            paused_reasons=channel_pause_reasons,
        ) if active_ids else 0
        return {
            "channels": {
                "total": len(policies),
                "mode_counts": dict(sorted(Counter(
                    policy.mode for _, policy in policies
                ).items())),
                "reason_counts": dict(sorted(Counter(
                    policy.reason for _, policy in policies
                ).items())),
            },
            "proposed_database_effects": {
                "collection_rows_to_pause": collection_to_pause,
                "collection_rows_to_resume": collection_to_resume,
                "inventory_rows_deleted": 0,
                "videos_deleted": 0,
                "snapshots_deleted": 0,
                "inventory_jobs_enqueued": 0,
                "youtube_requests": 0,
                "youtube_quota_units": 0,
            },
        }

    def record_initial_inventory_error(
        self,
        channel_id: str,
        error_code: str,
        *, expected_epoch: int,
    ) -> dict[str, Any]:
        return _mapping(
            channel_metadata_batch.record_channel_initial_inventory_error(
                self.database, str(channel_id or "").strip(), str(error_code or "").strip(),
                require_control_epoch(expected_epoch)),
            "initial inventory error recording",
        )

    def clear_initial_inventory_error(self, channel_id: str, *, expected_epoch: int) -> dict[str, Any]:
        return _mapping(
            channel_metadata_batch.clear_channel_initial_inventory_error(
                self.database, str(channel_id or "").strip(), require_control_epoch(expected_epoch)),
            "initial inventory error clearing",
        )

    def update_channel(self, channel_id: str, values: Mapping[str, Any], *, expected_epoch: int) -> None:
        epoch = require_control_epoch(expected_epoch)
        channel_control.update_channel_collection_fields(self.database, channel_id, dict(values), epoch)

    def assert_metadata_batch_ready(self) -> None:
        """Prove the direct metadata writer before marks or YouTube."""
        from ..readiness import collection_preflight
        collection_preflight.assert_metadata_batch_ready(self.database)

    def mark_metadata_running(self, channels: list[Mapping[str, Any]], started_at: str) -> None:
        """Mark the already captured bounded scope before its provider request."""
        if not channels:
            return
        rows = [{**channel_scope(channel), "values": {
            "channel_metadata_status": "running", "channel_metadata_started_at": started_at,
        }} for channel in channels]
        outcomes = self.update_metadata_batch(rows)
        if any(value != "updated" for value in outcomes.values()):
            raise RuntimeError("channel_metadata_mark_failed")

    def drain_metadata_running(self, markers):
        """Confirm exact running-marker release through the closed HOT owner."""
        from ..hot.hot_metadata_termination import validate
        from hot_runtime import collection_control_access
        markers = validate(markers)
        with collection_control_access():
            result = self.database.drain_hot_channel_metadata(markers=markers)
        expected = {'status': 'drained', 'requested': len(markers), 'released': len(markers), 'remaining': 0}
        if (not isinstance(result, dict) or result != expected
                or any(type(result[key]) is not int for key in ('requested', 'released', 'remaining'))):
            raise RuntimeError('hot_metadata_drain_unconfirmed')
        return result

    def update_metadata_batch(self, rows: list[Mapping[str, Any]]) -> dict[str, str]:
        """Settle one provider batch; never retry an ambiguous database response."""
        if not rows:
            return {}
        ids = [row.get("channel_id") for row in rows]
        if (len(rows) > 50 or any(not isinstance(value, str) or not value for value in ids)
                or len(set(ids)) != len(rows)):
            raise ValueError("channel_metadata_batch_invalid")
        for row in rows:
            require_control_epoch(row.get("control_epoch"))
            if "control_action" in row and row["control_action"] not in {
                "no_long_restart", "no_long_manual_required",
            }:
                raise ValueError("channel_metadata_control_action_invalid")
        response = channel_metadata_batch.update_channel_metadata_batch(self.database, [dict(row) for row in rows])
        if not isinstance(response, list) or len(response) != len(rows):
            raise TypeError("channel_metadata_batch_response_invalid")
        outcomes = {}
        for row in response:
            if (not isinstance(row, Mapping) or row.get("channel_id") not in ids
                    or row["channel_id"] in outcomes
                    or row.get("status") not in {"updated", "missing", "error"}):
                raise TypeError("channel_metadata_batch_response_invalid")
            outcomes[row["channel_id"]] = row["status"]
        return outcomes

    def update_process_state(
        self,
        channel_id: str,
        values: Mapping[str, Any],
        *, expected_epoch: int,
    ) -> bool:
        """Persist lifecycle/metadata values with the legacy optional-column fallback.

        Returns ``True`` when every supplied field was written and ``False`` when
        the database required a retry without optional process columns.
        """

        payload = dict(values)
        try:
            self.update_channel(channel_id, payload, expected_epoch=expected_epoch)
            return True
        except Exception as error:
            if any(key.startswith("channel_discovery_") for key in payload):
                raise
            if not _optional_process_columns_unavailable(error, payload):
                raise
            fallback = {
                key: value
                for key, value in payload.items()
                if key not in OPTIONAL_CHANNEL_PROCESS_COLUMNS
            }
            if fallback:
                self.update_channel(channel_id, fallback, expected_epoch=expected_epoch)
            return False

    def load_channel_record(self, channel_id: str) -> Mapping[str, Any] | None:
        row = self.database.read_row(
            "channel_sync_record",
            "SELECT * FROM " + CHANNEL_DISCOVERY_RELATION + " WHERE channel_id = %s LIMIT 1",
            (channel_id,),
        )
        return dict(row) if row else None

    def load_video_channel_id(self, video_id: str) -> str | None:
        """The channel of a video already in the catalogue, read without quota."""
        row = self.database.read_row(
            "channel_sync_video_channel",
            "SELECT channel_id FROM public.videos WHERE video_id = %s LIMIT 1",
            (video_id,),
        )
        channel_id = row.get("channel_id") if row else None
        return channel_id if isinstance(channel_id, str) and channel_id else None

    def preserve_user_input(self, channel_id: str, user_input: str) -> None:
        """Atomically preserve first confirmed user locator; never provider fields."""
        safe_input = normalize_user_channel_input(user_input)
        self.database.write(
            "channel_sync_preserve_user_input",
            "UPDATE public.channels SET user_input = %s WHERE channel_id = %s AND user_input IS NULL",
            (safe_input, channel_id),
        )

    def insert_channel(self, values: Mapping[str, Any], *, permission: str) -> dict[str, Any]:
        return _mapping(channel_control.create_channel_for_collection(
            self.database, dict(values), permission), "channel creation")

    def metadata_candidate_page(self, *, limit=50, after=None, due_ceiling=None):
        """Inspect <=100 physical due rows before canonical eligibility filtering.

        The fixed DB-clock ceiling excludes later reschedules. It is not a
        snapshot or authorization; exact capture and existing epoch-fenced
        writes remain required. Callers retain the physical tail on scan caps.
        SQL retains the legacy database collation and NULLS LAST due order.
        """
        if (type(limit) is not int or not 1 <= limit <= 100
                or (after is not None and type(after) is not MetadataCandidateCursor)
                or (after is not None and due_ceiling is None)):
            raise ValueError('metadata_candidate_input_invalid')
        if due_ceiling is not None:
            _metadata_cursor_clock(due_ceiling)
            if after is not None and after.next_after is not None and after.next_after > due_ceiling:
                raise ValueError('metadata_candidate_input_invalid')
        with self.database.transaction(get_operation('metadata_candidate_page')) as tx:
            now = _metadata_native_clock(tx.fetch_one(_METADATA_CLOCK)['at'])
            if due_ceiling is None:
                due_ceiling = now
            elif due_ceiling > now:
                raise ValueError('metadata_candidate_input_invalid')
            rows = tx.fetch_all(_METADATA_CANDIDATE_PAGE, dict(limit=limit, due_ceiling=due_ceiling,
                after_id=None if after is None else after.channel_id,
                after_due=None if after is None else after.next_after), max_rows=limit)
            candidates, next_after, seen = [], after, set()
            for row in rows:
                due = row['channel_metadata_next_after']
                cursor = MetadataCandidateCursor(None if due is None else _metadata_native_clock(due),
                                                 row['channel_id'])
                # Text tie ordering belongs to the database collation, not Python.
                if (cursor.channel_id in seen or cursor == after
                        or (cursor.next_after is not None and cursor.next_after > due_ceiling)
                        or (next_after is not None and cursor.next_after is not None
                            and (next_after.next_after is None or cursor.next_after < next_after.next_after))):
                    raise ValueError('metadata_candidate_result_invalid')
                seen.add(cursor.channel_id)
                next_after = cursor
                if (channel_sync_eligibility(row).allow_channel_metadata
                        and row.get('channel_metadata_status') != 'running'):
                    candidates.append(dict(channel_id=cursor.channel_id,
                        control_epoch=require_control_epoch(row['collection_control_epoch'])))
            return dict(rows=candidates, next_after=next_after, due_ceiling=due_ceiling,
                        inspected_count=len(rows), scan_exhausted=len(rows) < limit)

    def capture_metadata_candidates(self, scopes, *, due_ceiling):
        """Re-read only <=50 exact advisory scopes; never replace stale epochs.

        Missing, rescheduled, running and currently ineligible identities yield
        no captured row. This read owns no lease or marker and cannot authorize
        later writes without the existing captured-epoch owner.
        """
        if not isinstance(scopes, (list, tuple)) or len(scopes) > 50:
            raise ValueError('metadata_capture_input_invalid')
        _metadata_cursor_clock(due_ceiling)
        if any(not isinstance(scope, Mapping) for scope in scopes):
            raise ValueError('metadata_capture_input_invalid')
        identities = [_metadata_channel_id(scope.get('channel_id')) for scope in scopes]
        captured = validate_channel_scopes(scopes, channel_ids=identities)
        if not captured:
            return []
        expected = {scope['channel_id']: scope['control_epoch'] for scope in captured}
        with self.database.transaction(get_operation('capture_metadata_candidates')) as tx:
            now = _metadata_native_clock(tx.fetch_one(_METADATA_CLOCK)['at'])
            if due_ceiling > now:
                raise ValueError('metadata_capture_input_invalid')
            rows = tx.fetch_all(_METADATA_CAPTURE, dict(channel_ids=list(expected),
                epochs=list(expected.values()), due_ceiling=due_ceiling, limit=len(captured)),
                max_rows=len(captured))
            result, seen = {}, set()
            for row in rows:
                identity = row['channel_id']
                if (identity not in expected or identity in seen
                        or require_control_epoch(row['collection_control_epoch']) != expected[identity]):
                    raise ValueError('metadata_capture_result_invalid')
                seen.add(identity)
                due = row['channel_metadata_next_after']
                if due is not None and _metadata_native_clock(due) > due_ceiling:
                    raise ValueError('metadata_capture_result_invalid')
                if (channel_sync_eligibility(row).allow_channel_metadata
                        and row.get('channel_metadata_status') != 'running'):
                    result[identity] = row
            return application_value([result[identity] for identity in expected if identity in result])

    def load_due_metadata_page(
        self,
        now: datetime,
        *,
        page: int,
        page_size: int,
    ) -> list[dict[str, Any]]:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_filter = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            rows = self.database.read_rows(
                "channel_sync_due_metadata",
                "SELECT channel_id, title, video_count, channel_metadata_next_after, "
                "collection_mode, collection_permission, collection_control_epoch, "
                "channel_metadata_status, channel_metadata_error_count, "
                "initial_long_inventory_status, deep_inventory_status, is_hidden, "
                "initial_long_inventory_error_code, initial_long_inventory_error_count "
                "FROM public.channels_with_control "
                "WHERE channel_metadata_next_after IS NULL OR channel_metadata_next_after <= %s::timestamptz "
                "ORDER BY channel_metadata_next_after ASC NULLS LAST, channel_id ASC LIMIT %s OFFSET %s",
                (now_filter, page_size, page * page_size), max_rows=page_size,
            )
        except Exception as error:
            if _channel_metadata_columns_unavailable(error):
                raise ChannelMetadataColumnsUnavailableError(str(error)) from error
            raise
        return [dict(row) for row in rows]

    def load_running_status_page(
        self,
        *,
        channel_id: str | None,
        page: int,
        page_size: int,
    ) -> list[dict[str, Any]]:
        selected_columns = (
            "channel_id,collection_mode,collection_permission,collection_control_epoch,initial_long_inventory_status,"
            "initial_long_inventory_started_at,initial_28d_stats_status,"
            "initial_28d_stats_started_at,"
            "channel_metadata_status,channel_metadata_started_at,"
            "channel_metadata_error_count,deep_inventory_status,"
            "deep_inventory_started_at,deep_inventory_page_token"
        )
        query = "SELECT " + selected_columns + " FROM " + CHANNEL_DISCOVERY_RELATION + " WHERE "
        if channel_id:
            query += "channel_id = %s "
            parameters = (channel_id, page_size, page * page_size)
        else:
            query += ("initial_long_inventory_status = 'running' OR initial_28d_stats_status = 'running' "
                      "OR channel_metadata_status = 'running' OR deep_inventory_status = 'running' ")
            parameters = (page_size, page * page_size)
        rows = self.database.read_rows(
            "channel_sync_running_status",
            query + "ORDER BY channel_id ASC LIMIT %s OFFSET %s", parameters, max_rows=page_size,
        )
        return [dict(row) for row in rows]

    def channels_with_active_initial_jobs(self, channel_ids: list[str]) -> set[str]:
        """Channels whose initial statuses a queued or running operator job owns."""
        owned: set[str] = set()
        for batch in self._chunks(channel_ids):
            rows = self.database.read_rows(
                "channel_sync_active_initial_jobs",
                "SELECT DISTINCT channel_id FROM public.operator_jobs "
                "WHERE job_type = 'channel_initial_long_inventory' "
                "AND status IN ('queued', 'running') AND channel_id = ANY(%s::text[]) LIMIT %s",
                (batch, len(batch)), max_rows=len(batch),
            )
            owned.update(str(row["channel_id"]) for row in rows)
        return owned

    def load_recent_long_inventory(
        self,
        channel_id: str,
        *,
        published_at_gte: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self.database.read_rows(
            "channel_sync_recent_long_inventory",
            "SELECT video_id, channel_id, video_type, published_at FROM public.video_inventory "
            "WHERE channel_id = %s AND video_type = 'long' AND published_at >= %s::timestamptz "
            "ORDER BY published_at DESC NULLS FIRST, video_id ASC LIMIT %s",
            (channel_id, published_at_gte, limit), max_rows=limit,
        )
        return [dict(row) for row in rows]


class ChannelCommandRepository(ChannelSyncRepository):
    """HTTP command preflight reads; collection keeps the raw operational owner."""

    def load_channel_record(self, channel_id: str) -> Mapping[str, Any] | None:
        from ..reads.frontend import FrontendRepository
        rows = FrontendRepository(self.database).load_channel(channel_id)
        return rows[0] if rows else None

    def load_video_channel_id(self, video_id: str) -> str | None:
        # Identity only, preserving the former videos-table presence rule.
        row = self.database.read_row(
            "channel_command_video_channel",
            "SELECT channel_id FROM readable.videos WHERE video_id = %s LIMIT 1",
            (video_id,),
        )
        channel_id = row.get("channel_id") if row else None
        return channel_id if isinstance(channel_id, str) and channel_id else None
