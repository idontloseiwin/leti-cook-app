"""Repository for the leased video collection queue.

The repository owns queue reads and atomic command dispatch. Sync code works
with claims and completion payloads; it does not construct database queries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from telemetry import emit_event
from channel_collection_scope import channel_scope, require_control_epoch
from ..base.repository import DatabaseRepository
from .collection_policy import cadence_report
from ..readiness import capabilities
from . import video_failures
from ..reads.operational_read_queries import get_video_collection_health, get_typed_video_collection_health


VIDEO_METADATA_COMPLETION_CONTRACT_VERSION = 2
TYPED_VIDEO_COLLECTION_SCHEMA_VERSION = 1
TYPED_INVENTORY_QUOTA_DEFER_SCHEMA_VERSION = 1
COMMON_VIDEO_COLLECTION_SCHEMA_VERSION = 1
COMMON_VIDEO_COLLECTION_POOL_VERSION = 1


MAX_QUEUE_RPC_ROWS = 1000
MAX_TYPED_COLLECTION_RPC_ROWS = 50
MAX_TYPED_COLLECTION_RECONCILE_ROWS = 1000
MAX_COMMON_COLLECTION_RPC_ROWS = 50
MAX_COMMON_COLLECTION_INSPECTED_ROWS = 1000
TYPED_VIDEO_TYPES = frozenset({"short", "stream"})
VIDEO_COLLECTION_MODES_BY_TYPE = {
    "long": frozenset({"hydrate", "metadata", "counters", "identity_refresh"}),
    "short": frozenset({"hydrate", "metadata", "counters", "identity_refresh"}),
    "stream": frozenset({"stream_lifecycle", "hydrate", "metadata", "counters", "identity_refresh"}),
    "unknown": frozenset({"identity_refresh"}),
}


def _scalar_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise TypeError("database count response cannot be boolean")
    if isinstance(value, (int, float, str)):
        return int(value)
    if isinstance(value, Mapping) and len(value) == 1:
        return _scalar_count(next(iter(value.values())))
    if isinstance(value, list) and len(value) == 1:
        row = value[0]
        if isinstance(row, Mapping):
            for key in ("count", "affected_count", "completed_count"):
                if key in row:
                    return int(row[key] or 0)
        return _scalar_count(row)
    raise TypeError(f"unsupported database count response: {type(value).__name__}")


def _chunked(values: Sequence[Any], size: int = MAX_QUEUE_RPC_ROWS):
    for start in range(0, len(values), size):
        yield values[start : start + size]

def _mapping_result(value: Any, *, operation: str) -> dict[str, Any]:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, Mapping):
        raise TypeError(f"{operation} returned invalid data")
    return dict(value)


def _typed_video_type(value: Any) -> str:
    video_type = str(value or "").strip().lower()
    if video_type not in TYPED_VIDEO_TYPES:
        raise ValueError("typed video type must be 'short' or 'stream'")
    return video_type


@dataclass(frozen=True)
class VideoCollectionClaim:
    video_id: str
    channel_id: str
    collection_mode: str
    lease_token: str
    lease_expires_at: str
    next_due_at: str
    video_type: str = "long"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "VideoCollectionClaim":
        video_type = str(row.get("video_type") or "long").strip().lower()
        allowed_modes = VIDEO_COLLECTION_MODES_BY_TYPE.get(video_type)
        if allowed_modes is None:
            raise ValueError(f"unknown video collection type: {video_type!r}")
        mode = str(row.get("collection_mode") or "").strip().lower()
        if mode not in allowed_modes:
            raise ValueError(f"unknown video collection mode: {mode!r}")
        required = {
            key: row.get(key)
            for key in (
                "video_id",
                "channel_id",
                "lease_token",
                "lease_expires_at",
                "next_due_at",
            )
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(
                "video collection claim is missing " + ", ".join(sorted(missing))
            )
        return cls(
            video_id=str(required["video_id"]),
            channel_id=str(required["channel_id"]),
            collection_mode=mode,
            lease_token=str(required["lease_token"]),
            lease_expires_at=str(required["lease_expires_at"]),
            next_due_at=str(required["next_due_at"]),
            video_type=video_type,
        )


class VideoCollectionRepository(DatabaseRepository):
    """Typed access to queue setup, claiming, completion, and health RPCs."""

    def _execute(self, operation: str, callback: Callable[[], Any]) -> Any:
        emit_event(
            "database",
            "video_collection_repository",
            "postgres",
            operation,
            direction="request",
            status="started",
        )
        started = time.perf_counter()
        try:
            response = callback()
        except Exception as error:
            emit_event(
                "database",
                "video_collection_repository",
                "postgres",
                operation,
                direction="response",
                status="error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=error.__class__.__name__,
            )
            raise
        data = response
        count = len(data) if isinstance(data, list) else None
        emit_event(
            "database",
            "video_collection_repository",
            "postgres",
            operation,
            direction="response",
            status="success",
            count=count,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return response

    def _rpc(self, name: str, params: Mapping[str, Any] | None = None, *, abort_on_denied=True) -> Any:
        from ..hot.hot_collection_control import dispatch_owned_rpc
        def execute():
            values = dict(params or {})
            if name == "claim_initial_video_collection_batch":
                from . import video_initial_claims
                return video_initial_claims.claim_initial_video_collection_batch(self.database,
                    job_id=values["p_job_id"], video_ids=values["p_video_ids"], worker_id=values["p_worker_id"],
                    run_id=values["p_run_id"], lease_seconds=values["p_lease_seconds"])
            if name == "reconcile_typed_video_collection_state":
                from . import video_typed_reconciliation
                return video_typed_reconciliation.reconcile_typed_video_collection_state(self.database,
                    video_type=values["p_video_type"], limit=values["p_limit"])
            if name == "enqueue_video_collection_ids":
                from . import video_queue_maintenance
                return video_queue_maintenance.enqueue_video_collection_ids(self.database,
                    video_ids=values["p_video_ids"], channel_scopes=values["p_channel_scopes"])
            if name == "ensure_video_collection_state":
                from . import video_queue_ensure
                return video_queue_ensure.ensure_video_collection_state(self.database,
                    video_ids=values["p_video_ids"], channel_scopes=values["p_channel_scopes"])
            if name == "complete_video_counter_collection":
                from . import video_counter_completion
                return video_counter_completion.complete_video_counter_collection(self.database, values["p_rows"])
            if name == "complete_video_profile_metadata_collection":
                from . import video_profile_completion
                return video_profile_completion.complete_video_profile_metadata_collection(self.database, values["p_rows"])
            if name == "complete_typed_video_metadata_collection":
                from . import video_typed_metadata_completion
                return video_typed_metadata_completion.complete_typed_video_metadata_collection(self.database, values["p_rows"])
            if name == "complete_typed_video_missing":
                from . import video_missing_completion
                return video_missing_completion.complete_typed_video_missing(self.database, values["p_rows"])
            if name == "complete_video_identity_refresh":
                from .video_identity_recovery import complete_video_identity_refresh
                return complete_video_identity_refresh(self.database, values["p_rows"])
            if name == "complete_stream_lifecycle_probe":
                from . import video_stream_completion
                return video_stream_completion.complete_stream_lifecycle_probe(self.database, values["p_rows"])
            if name in ("claim_video_collection_ids", "claim_video_metadata_collection_ids"):
                from . import video_claims
                action = (video_claims.claim_video_collection_ids if name == "claim_video_collection_ids"
                          else video_claims.claim_video_metadata_collection_ids)
                return action(self.database, video_ids=values["p_video_ids"],
                    worker_id=values["p_worker_id"], run_id=values["p_run_id"],
                    channel_scopes=values["p_channel_scopes"], lease_seconds=values["p_lease_seconds"])
            if name == "claim_due_video_collection_batch":
                from . import video_scheduled_claims
                return video_scheduled_claims.claim_due_video_collection_batch(self.database,
                    limit=values["p_limit"], worker_id=values["p_worker_id"], run_id=values["p_run_id"],
                    lease_seconds=values["p_lease_seconds"],
                    **({'scope': values['p_scope']} if 'p_scope' in values else {}),
                    **({'scopes': values['p_scopes']} if 'p_scopes' in values else {}),
                    **({'operation': values['p_operation']} if 'p_operation' in values else {}))
            if name == "claim_due_typed_video_collection_batch":
                from . import video_scheduled_claims
                return video_scheduled_claims.claim_due_typed_video_collection_batch(self.database,
                    video_type=values["p_video_type"], limit=values["p_limit"],
                    worker_id=values["p_worker_id"], run_id=values["p_run_id"], lease_seconds=values["p_lease_seconds"])
            raise ValueError("database_operation_not_registered")
        response = dispatch_owned_rpc(self.database, name, params,
            lambda: self._execute("rpc:" + name, execute), abort_on_denied=abort_on_denied)
        return response

    def _failure(self, name: str, params: Mapping[str, Any], callback: Callable[[], Any]) -> Any:
        # Preserve the owned-lease denial path and legacy telemetry labels.
        # The atomic Python protocol propagates unknown ACKs without replay.
        from ..hot.hot_collection_control import dispatch_owned_rpc
        return dispatch_owned_rpc(
            self.database, name, params,
            lambda: self._execute("rpc:" + name, callback),
        )

    def schema_status(self) -> Mapping[str, Any]:
        data = self._execute("rpc:assert_video_collection_queue_schema", lambda: capabilities.assert_video_collection_queue_schema(self.database))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, Mapping):
            raise TypeError("video collection schema assertion returned invalid data")
        return dict(data)

    def common_pool_schema_status(self) -> Mapping[str, Any]:
        return _mapping_result(
            self._execute("rpc:assert_video_collection_common_pool_schema", lambda: capabilities.assert_video_collection_common_pool_schema(self.database)),
            operation="video collection common pool schema assertion",
        )

    @staticmethod
    def _assert_common_pool_contract(data: Mapping[str, Any]) -> None:
        if data.get("status") != "ok":
            raise RuntimeError("video collection common pool schema is not ready")
        expected_numbers = {
            "schema_version": COMMON_VIDEO_COLLECTION_SCHEMA_VERSION,
            "common_pool_version": COMMON_VIDEO_COLLECTION_POOL_VERSION,
            "max_claim_rows": MAX_COMMON_COLLECTION_RPC_ROWS,
            "max_request_batch": MAX_COMMON_COLLECTION_RPC_ROWS,
            "max_inspected_rows": MAX_COMMON_COLLECTION_INSPECTED_ROWS,
        }
        for field, expected in expected_numbers.items():
            raw_value = data.get(field)
            if isinstance(raw_value, bool):
                raise RuntimeError(
                    "video collection common pool contract is incompatible"
                )
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                raise RuntimeError(
                    "video collection common pool contract is incompatible"
                ) from None
            if value != expected:
                raise RuntimeError(
                    "video collection common pool contract is incompatible"
                )
        if (
            data.get("mixed_claim") is not True
            or data.get("sole_lease_owner") != "video_collection_state"
        ):
            raise RuntimeError(
                "video collection common pool ownership contract is incompatible"
            )

        if data.get("availability_incident_owner") is not True:
            raise RuntimeError("video availability incident owner is not ready")

        if data.get("atomic_long_availability_restore") is not True:
            raise RuntimeError(
                "video collection atomic long availability restore is not ready"
            )

    def preflight_common_pool_ready(self) -> Mapping[str, Any]:
        data = capabilities.telemetry_check(
            self.database, capabilities.assert_video_collection_common_pool_schema,
            phase="preflight",
        )
        ready = _mapping_result(
            data,
            operation="video collection common pool schema assertion",
        )
        self._assert_common_pool_contract(ready)
        return ready

    def assert_common_pool_ready(self) -> Mapping[str, Any]:
        ready = self.common_pool_schema_status()
        self._assert_common_pool_contract(ready)
        return ready

    def typed_quota_defer_schema_status(self) -> Mapping[str, Any]:
        return _mapping_result(
            self._execute("rpc:assert_typed_inventory_quota_defer_schema", lambda: capabilities.assert_typed_inventory_quota_defer_schema(self.database)),
            operation="typed inventory quota defer schema assertion",
        )

    def typed_schema_status(self) -> Mapping[str, Any]:
        ready = _mapping_result(
            self._execute("rpc:assert_typed_video_collection_schema", lambda: capabilities.assert_typed_video_collection_schema(self.database)),
            operation="typed video collection schema assertion",
        )
        quota_ready = self.typed_quota_defer_schema_status()
        self._assert_typed_quota_defer_contract(quota_ready)
        return {
            **ready,
            "typed_inventory_quota_defer": quota_ready,
        }

    @staticmethod
    def _assert_typed_collection_contract(data: Mapping[str, Any]) -> None:
        if data.get("status") != "ok":
            raise RuntimeError("typed video collection schema is not ready")
        raw_version = data.get("schema_version")
        if isinstance(raw_version, bool):
            raise RuntimeError("typed video collection schema v1 is required")
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise RuntimeError(
                "typed video collection schema v1 is required"
            ) from None
        if version < TYPED_VIDEO_COLLECTION_SCHEMA_VERSION:
            raise RuntimeError("typed video collection schema v1 is required")
        runtime_types = data.get("runtime_video_types")
        if not isinstance(runtime_types, list) or set(runtime_types) != set(
            TYPED_VIDEO_TYPES
        ):
            raise RuntimeError(
                "typed video collection runtime types are incompatible"
            )
        raw_batch = data.get("max_request_batch")
        if isinstance(raw_batch, bool):
            raise RuntimeError(
                "typed video collection batch contract is incompatible"
            )
        try:
            max_batch = int(raw_batch)
        except (TypeError, ValueError):
            raise RuntimeError(
                "typed video collection batch contract is incompatible"
            ) from None
        if max_batch != MAX_TYPED_COLLECTION_RPC_ROWS:
            raise RuntimeError(
                "typed video collection batch contract is incompatible"
            )
        if data.get("full_metadata_each_refresh") is not True:
            raise RuntimeError(
                "typed video collection metadata contract is incompatible"
            )
        if data.get("completed_streams_only") is not True:
            raise RuntimeError(
                "typed video collection stream contract is incompatible"
            )
        if data.get("product_exposure_changed") is not False:
            raise RuntimeError(
                "typed video collection exposure contract is incompatible"
            )
        if data.get("exact_receipt_payload_fencing") is not True:
            raise RuntimeError(
                "typed video collection receipt contract is incompatible"
            )
        if (
            type(data.get("repair_version")) is not int
            or data["repair_version"] < 2
            or data.get("bounded_claim_batch") is not True
        ):
            raise RuntimeError(
                "typed video collection bounded repair contract v2 is required"
            )

    def _assert_typed_quota_defer_contract(self, data: Mapping[str, Any]) -> None:
        if data.get("status") != "ok":
            raise RuntimeError("typed inventory quota defer schema is not ready")
        raw_version = data.get("schema_version")
        if isinstance(raw_version, bool):
            raise RuntimeError("typed inventory quota defer schema v1 is required")
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise RuntimeError(
                "typed inventory quota defer schema v1 is required"
            ) from None
        if version < TYPED_INVENTORY_QUOTA_DEFER_SCHEMA_VERSION:
            raise RuntimeError("typed inventory quota defer schema v1 is required")
        expected_numbers = {
            "claim_limit": 500,
            "retry_floor_seconds": 3600,
        }
        for field, expected in expected_numbers.items():
            raw_value = data.get(field)
            if isinstance(raw_value, bool):
                raise RuntimeError(
                    "typed inventory quota defer contract is incompatible"
                )
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                raise RuntimeError(
                    "typed inventory quota defer contract is incompatible"
                ) from None
            if value != expected:
                raise RuntimeError(
                    "typed inventory quota defer contract is incompatible"
                )
        expected_jitter = cadence_report(self.database)["typed_jitter_seconds"]
        reported_jitter = data.get("typed_jitter_seconds")
        if (data.get("schedule_source") != "process_snapshot"
                or not isinstance(reported_jitter, Mapping)
                or set(reported_jitter) != set(expected_jitter)
                or any(type(reported_jitter[kind]) is not int
                       or reported_jitter[kind] != expected_jitter[kind]
                       for kind in expected_jitter)):
            raise RuntimeError("typed inventory quota defer contract is incompatible")
        if (
            data.get("lost_ack_receipts") is not True
            or data.get("active_scan_state_preserved") is not True
            or data.get("quota_claim_floor_guard") is not True
            or data.get("retry_until") != "youtube_quota_reset"
        ):
            raise RuntimeError(
                "typed inventory quota defer contract is incompatible"
            )

    def preflight_typed_collection_ready(self) -> Mapping[str, Any]:
        data = capabilities.telemetry_check(
            self.database, capabilities.assert_typed_video_collection_schema,
            phase="preflight",
        )
        ready = _mapping_result(
            data,
            operation="typed video collection schema assertion",
        )
        self._assert_typed_collection_contract(ready)
        quota_data = capabilities.telemetry_check(
            self.database, capabilities.assert_typed_inventory_quota_defer_schema,
            phase="preflight",
        )
        quota_ready = _mapping_result(
            quota_data,
            operation="typed inventory quota defer schema assertion",
        )
        self._assert_typed_quota_defer_contract(quota_ready)
        return {
            **ready,
            "typed_inventory_quota_defer": quota_ready,
        }

    def assert_ready(self) -> Mapping[str, Any]:
        data = self._execute("rpc:assert_video_collection_queue_ready", lambda: capabilities.assert_video_collection_queue_ready(self.database))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, Mapping):
            raise TypeError("video collection readiness assertion returned invalid data")
        ready = dict(data)
        self._assert_metadata_completion_contract(ready)
        return ready

    @staticmethod
    def _assert_metadata_completion_contract(data: Mapping[str, Any]) -> None:
        raw_version = data.get("metadata_completion_contract_version")
        if isinstance(raw_version, bool):
            raise RuntimeError("video metadata completion contract v2 is required")
        try:
            version = int(raw_version)
        except (TypeError, ValueError):
            raise RuntimeError(
                "video metadata completion contract v2 is required"
            ) from None
        if version < VIDEO_METADATA_COMPLETION_CONTRACT_VERSION:
            raise RuntimeError("video metadata completion contract v2 is required")

    def preflight_queue_ready(self) -> Any:
        data = capabilities.telemetry_check(
            self.database, capabilities.assert_video_collection_queue_ready,
            phase="preflight",
        )
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, Mapping):
            raise TypeError("video collection readiness assertion returned invalid data")
        ready = dict(data)
        self._assert_metadata_completion_contract(ready)
        return ready

    def assert_forced_metadata_claim_ready(
        self,
        *,
        worker_id: str,
        run_id: str,
        lease_seconds: int = 300,
    ) -> Any:
        from ..readiness import collection_preflight
        return collection_preflight.assert_forced_metadata_claim_ready(
            self.database, worker_id=worker_id, run_id=run_id, lease_seconds=lease_seconds,
        )

    def load_video_channel_map(self, video_ids: Iterable[str]) -> dict[str, str]:
        """Read bounded identity once before addressed preparation/provider work."""
        clean_ids = list(dict.fromkeys(str(value).strip() for value in video_ids if value))
        mapping: dict[str, str] = {}
        for batch in _chunked(clean_ids):
            response = self._execute(
                "select:addressed_video_channels",
                lambda batch=batch: self.database.read_rows(
                    "collection_addressed_video_channels",
                    "SELECT video_id, channel_id FROM public.video_inventory "
                    "WHERE video_id = ANY(%s::text[]) LIMIT %s",
                    (batch, len(batch)), max_rows=len(batch),
                ),
            )
            rows = response
            if not isinstance(rows, list):
                raise TypeError("addressed video identity query returned invalid data")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise TypeError("addressed video identity query returned invalid row")
                video_id, channel_id = row.get("video_id"), row.get("channel_id")
                if (
                    video_id not in batch or video_id in mapping
                    or not isinstance(channel_id, str) or not channel_id
                    or channel_id.strip() != channel_id or len(channel_id) > 128
                ):
                    raise ValueError("channel_collection_scope_invalid")
                mapping[video_id] = channel_id
        # Unknown IDs retain their old no-op semantics. SQL checks actual
        # inventory coverage again before any addressed queue mutation.
        return mapping

    def load_channel_scopes(self, channel_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Explicit NEW-action capture; never called by ensure/claim methods."""
        clean_ids = list(dict.fromkeys(channel_ids))
        scopes: dict[str, dict[str, Any]] = {}
        for batch in _chunked(clean_ids):
            response = self._execute(
                "select:addressed_channel_scopes",
                lambda batch=batch: self.database.read_rows(
                    "collection_addressed_channel_scopes",
                    "SELECT channel_id, collection_control_epoch, collection_permission, is_hidden "
                    "FROM public.channels_with_control WHERE channel_id = ANY(%s::text[]) LIMIT %s",
                    (batch, len(batch)), max_rows=len(batch),
                ),
            )
            rows = response
            if not isinstance(rows, list):
                raise TypeError("addressed channel scope query returned invalid data")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise TypeError("addressed channel scope query returned invalid row")
                scope = channel_scope(row)
                identity = scope["channel_id"]
                if identity not in batch or identity in scopes:
                    raise ValueError("channel_collection_scope_invalid")
                if row.get("is_hidden") is not False or row.get("collection_permission") != "accepted":
                    raise ValueError("channel_collection_scope_lost")
                scopes[identity] = scope
        if set(scopes) != set(clean_ids):
            raise ValueError("channel_collection_scope_lost")
        return [scopes[identity] for identity in clean_ids]

    @staticmethod
    def _addressed_batches(
        video_ids: Sequence[str], *, channel_scopes: Sequence[Mapping[str, Any]],
        video_channels: Mapping[str, str],
    ):
        """Subset a pinned action scope without any database/epoch reload."""
        if not isinstance(channel_scopes, (list, tuple)) or not isinstance(video_channels, Mapping):
            raise ValueError("channel_collection_scope_invalid")
        scopes: dict[str, dict[str, Any]] = {}
        for row in channel_scopes:
            if not isinstance(row, Mapping) or set(row) != {"channel_id", "control_epoch"}:
                raise ValueError("channel_collection_scope_invalid")
            identity = row["channel_id"]
            if (
                not isinstance(identity, str) or not identity or identity.strip() != identity
                or len(identity) > 128 or identity in scopes
            ):
                raise ValueError("channel_collection_scope_invalid")
            scopes[identity] = {"channel_id": identity, "control_epoch": require_control_epoch(row["control_epoch"])}
        # Validate the entire addressed workset before the first preparation RPC.
        for video_id in video_ids:
            if video_id in video_channels and video_channels[video_id] not in scopes:
                raise ValueError("channel_collection_scope_invalid")
        for batch in _chunked(video_ids):
            identities = sorted({video_channels[video_id] for video_id in batch if video_id in video_channels})
            yield batch, [dict(scopes[identity]) for identity in identities]

    def ensure_video_ids(
        self, video_ids: Iterable[str], *, channel_scopes: Sequence[Mapping[str, Any]],
        video_channels: Mapping[str, str],
    ) -> int:
        clean_ids = list(dict.fromkeys(str(value).strip() for value in video_ids if value))
        if not clean_ids:
            return 0
        affected = 0
        for batch, scopes in self._addressed_batches(
            clean_ids, channel_scopes=channel_scopes, video_channels=video_channels,
        ):
            affected += _scalar_count(
                self._rpc("ensure_video_collection_state", {
                    "p_video_ids": batch, "p_channel_scopes": scopes,
                })
            )
        return affected

    @staticmethod
    def _claims(
        data: Any,
        *,
        expected_video_type: str,
        operation: str,
    ) -> list[VideoCollectionClaim]:
        if not isinstance(data, list):
            raise TypeError(f"{operation} returned invalid data")
        claims = [VideoCollectionClaim.from_row(row) for row in data]
        if any(
            claim.video_type != expected_video_type
            for claim in claims
        ):
            raise RuntimeError(
                f"{operation} returned claims outside "
                f"{expected_video_type!r} type"
            )
        return claims

    @staticmethod
    def _mixed_claims(data: Any, *, operation: str) -> list[VideoCollectionClaim]:
        if not isinstance(data, list):
            raise TypeError(f"{operation} returned invalid data")
        claims = [VideoCollectionClaim.from_row(row) for row in data]
        if len({claim.video_id for claim in claims}) != len(claims):
            raise RuntimeError(f"{operation} returned duplicate video IDs")
        return claims


    def reconcile_typed(
        self,
        video_type: str,
        limit: int = MAX_TYPED_COLLECTION_RECONCILE_ROWS,
    ) -> Mapping[str, Any]:
        parsed_type = _typed_video_type(video_type)
        parsed_limit = int(limit)
        if (
            parsed_limit < 1
            or parsed_limit > MAX_TYPED_COLLECTION_RECONCILE_ROWS
        ):
            raise ValueError(
                "typed video collection reconcile limit must be 1..1000"
            )
        return _mapping_result(
            self._rpc(
                "reconcile_typed_video_collection_state",
                {"p_video_type": parsed_type, "p_limit": parsed_limit},
            ),
            operation="typed video collection reconciliation",
        )

    def enqueue_video_ids(
        self, video_ids: Iterable[str], *, channel_scopes: Sequence[Mapping[str, Any]],
    ) -> int:
        clean_ids = list(
            dict.fromkeys(str(value).strip() for value in video_ids if value)
        )
        if not clean_ids:
            return 0
        if len(clean_ids) > MAX_QUEUE_RPC_ROWS:
            raise ValueError("common video collection enqueue accepts at most 1000 ids")
        return _scalar_count(
            self._rpc(
                "enqueue_video_collection_ids",
                {"p_video_ids": clean_ids, "p_channel_scopes": [dict(scope) for scope in channel_scopes]},
            )
        )

    def claim_due_typed_batch(
        self,
        *,
        video_type: str,
        limit: int,
        worker_id: str,
        run_id: str,
        lease_seconds: int = 300,
    ) -> Mapping[str, Any]:
        """Claim a bounded queue window; an empty filtered page is not EOF."""
        parsed_type = _typed_video_type(video_type)
        parsed_limit = int(limit)
        parsed_worker = str(worker_id or "").strip()
        parsed_run = str(run_id or "").strip()
        parsed_lease = int(lease_seconds)
        if parsed_limit < 0 or parsed_limit > MAX_TYPED_COLLECTION_RPC_ROWS:
            raise ValueError("typed video collection claim limit must be 0..50")
        if not parsed_worker or not parsed_run:
            raise ValueError("worker_id and run_id are required")
        if parsed_lease < 30 or parsed_lease > 3600:
            raise ValueError("typed video collection lease must be 30..3600 seconds")
        if parsed_limit == 0:
            return {"rows": [], "inspected_count": 0, "paused_count": 0,
                    "scan_exhausted": True}
        # Claims are mutations without an immutable result receipt. Never
        # automatically replay an uncertain response.
        result = _mapping_result(
            self._rpc("claim_due_typed_video_collection_batch", {
                "p_video_type": parsed_type,
                "p_limit": parsed_limit,
                "p_worker_id": parsed_worker,
                "p_run_id": parsed_run,
                "p_lease_seconds": parsed_lease,
            }),
            operation="typed video collection claim batch",
        )
        rows = self._claims(
            result.get("rows"), expected_video_type=parsed_type,
            operation="typed video collection claim batch",
        )
        inspected = result.get("inspected_count")
        paused = result.get("paused_count")
        exhausted = result.get("scan_exhausted")
        if (
            type(inspected) is not int or not 0 <= inspected <= 1000
            or type(paused) is not int or not 0 <= paused <= inspected
            or type(exhausted) is not bool
            or len(rows) > parsed_limit
            or len(rows) + paused > inspected
            or len({row.video_id for row in rows}) != len(rows)
        ):
            raise RuntimeError("typed video collection claim batch contract is invalid")
        return {"rows": rows, "inspected_count": inspected,
                "paused_count": paused, "scan_exhausted": exhausted}

    def due_candidate_page(self, *, video_type, run_id, after=None, limit=16, operation=None, frontiers=None):
        """Advisory channel keyset only; claims remain with claim_due_batch."""
        from .video_scheduled_claims import due_candidate_page
        return self._execute('common_video_candidates', lambda: due_candidate_page(
            self.database, video_type=video_type, run_id=run_id, after=after, limit=limit, operation=operation, frontiers=frontiers))

    def claim_due_batch(
        self,
        *,
        limit: int,
        worker_id: str,
        run_id: str,
        lease_seconds: int = 300,
        scope: Mapping[str, str] | None = None,
        scopes: Sequence[Mapping[str, str]] | None = None,
        operation: str | None = None,
    ) -> Mapping[str, Any]:
        """Claim one bounded due window across long, short, and stream rows."""
        from .video_scheduled_claims import validate_scope, validate_scopes
        if operation not in (None, 'metadata', 'counters') or (operation is not None and scopes is None):
            raise ValueError('common_video_operation_invalid')
        scope = validate_scope(scope)
        scopes = validate_scopes(scopes)
        if scope is not None and scopes is not None:
            raise ValueError('common_video_scope_invalid')
        allowed_scopes = {(item['channel_id'], item['video_type'])
                          for item in (scopes or ([scope] if scope else []))}
        parsed_limit = int(limit)
        parsed_worker = str(worker_id or "").strip()
        parsed_run = str(run_id or "").strip()
        parsed_lease = int(lease_seconds)
        if parsed_limit < 0 or parsed_limit > MAX_COMMON_COLLECTION_RPC_ROWS:
            raise ValueError("common video collection claim limit must be 0..50")
        if not parsed_worker or not parsed_run:
            raise ValueError("worker_id and run_id are required")
        if parsed_lease < 30 or parsed_lease > 3600:
            raise ValueError("common video collection lease must be 30..3600 seconds")
        if parsed_limit == 0:
            return {
                "rows": [],
                "inspected_count": 0,
                "paused_count": 0,
                "scan_exhausted": True,
            }
        # This lease mutation has no immutable result receipt. Never replay an
        # uncertain response: the server-side lease is the recovery boundary.
        result = _mapping_result(
            self._rpc(
                "claim_due_video_collection_batch",
                {
                    "p_limit": parsed_limit,
                    "p_worker_id": parsed_worker,
                    "p_run_id": parsed_run,
                    "p_lease_seconds": parsed_lease,
                    **({"p_scope": scope} if scope is not None else {}),
                    **({"p_scopes": scopes} if scopes is not None else {}),
                    **({"p_operation": operation} if operation is not None else {}),
                },
            ),
            operation="common video collection claim batch",
        )
        rows = self._mixed_claims(
            result.get("rows"),
            operation="common video collection claim batch",
        )
        inspected = result.get("inspected_count")
        paused = result.get("paused_count")
        exhausted = result.get("scan_exhausted")
        if (
            ("claim_stop_reason" in result and result["claim_stop_reason"] not in (
                "full", "raw_scan_bound", "admission_changed_or_locked", "eligible_window_exhausted"))
            or type(inspected) is not int
            or not 0 <= inspected <= MAX_COMMON_COLLECTION_INSPECTED_ROWS
            or type(paused) is not int
            or not 0 <= paused <= inspected
            or type(exhausted) is not bool
            or len(rows) > parsed_limit
            or len(rows) + paused > inspected
            or (not inspected and not exhausted)
            or (allowed_scopes and any((row.channel_id, row.video_type) not in allowed_scopes
                for row in rows))
            or (operation is not None and any(("counters" if row.collection_mode == "counters"
                else "metadata") != operation for row in rows))
            or (scopes is not None and len({
                "counters" if row.collection_mode == "counters"
                else "metadata" for row in rows
            }) > 1)
        ):
            raise RuntimeError("common video collection claim batch contract is invalid")
        return {
            "rows": rows,
            "inspected_count": inspected,
            "paused_count": paused,
            "scan_exhausted": exhausted,
            **({"claim_stop_reason": result["claim_stop_reason"]} if "claim_stop_reason" in result else {}),
        }

    def claim_video_ids(
        self,
        video_ids: Iterable[str],
        *,
        channel_scopes: Sequence[Mapping[str, Any]],
        video_channels: Mapping[str, str],
        worker_id: str,
        run_id: str,
        lease_seconds: int = 300,
    ) -> list[VideoCollectionClaim]:
        clean_ids = list(dict.fromkeys(str(value).strip() for value in video_ids if value))
        if not clean_ids:
            return []
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        claims: list[VideoCollectionClaim] = []
        for batch, scopes in self._addressed_batches(
            clean_ids, channel_scopes=channel_scopes, video_channels=video_channels,
        ):
            data = self._rpc(
                "claim_video_collection_ids",
                {
                    "p_video_ids": batch,
                    "p_channel_scopes": scopes,
                    "p_worker_id": worker_id,
                    "p_run_id": run_id,
                    "p_lease_seconds": lease_seconds,
                },
            )
            claims.extend(
                self._claims(
                    data,
                    expected_video_type="long",
                    operation="explicit video collection claim",
                )
            )
        return claims

    def claim_initial_video_ids(
        self,
        video_ids: Iterable[str],
        *,
        job_id: str,
        channel_id: str,
        worker_id: str,
        run_id: str,
        lease_seconds: int = 900,
    ) -> list[VideoCollectionClaim]:
        """Force one frozen initial-job batch through the existing mixed owner."""
        clean_ids = list(dict.fromkeys(
            str(value).strip() for value in video_ids if value
        ))
        if not clean_ids:
            return []
        if len(clean_ids) > MAX_COMMON_COLLECTION_RPC_ROWS or any(
            not video_id or len(video_id) > 128 for video_id in clean_ids
        ):
            raise ValueError("initial video claim accepts at most 50 valid IDs")
        job_id = str(job_id or "").strip()
        channel_id = str(channel_id or "").strip()
        worker_id = str(worker_id or "").strip()
        run_id = str(run_id or "").strip()
        if not job_id or not channel_id or not worker_id or not run_id:
            raise ValueError("initial video claim requires job/channel/worker/run")
        if isinstance(lease_seconds, bool) or not 30 <= int(lease_seconds) <= 3600:
            raise ValueError("initial video lease must be 30..3600 seconds")
        # No immutable claim receipt: a lost response must not replay this write.
        claims = self._mixed_claims(
            self._rpc("claim_initial_video_collection_batch", {
                "p_job_id": job_id,
                "p_video_ids": clean_ids,
                "p_worker_id": worker_id,
                "p_run_id": run_id,
                "p_lease_seconds": int(lease_seconds),
            }),
            operation="initial mixed video claim",
        )
        positions = {video_id: index for index, video_id in enumerate(clean_ids)}
        if any(
            claim.video_id not in positions
            or claim.channel_id != channel_id
            or claim.collection_mode == "counters"
            for claim in claims
        ) or len(claims) > len(clean_ids):
            raise RuntimeError("initial mixed video claim escaped its full-profile scope")
        if claims != sorted(claims, key=lambda claim: positions[claim.video_id]):
            raise RuntimeError("initial mixed video claim changed caller order")
        return claims

    def claim_video_metadata_ids(
        self,
        video_ids: Iterable[str],
        *,
        channel_scopes: Sequence[Mapping[str, Any]],
        video_channels: Mapping[str, str],
        worker_id: str,
        run_id: str,
        lease_seconds: int = 300,
    ) -> list[VideoCollectionClaim]:
        clean_ids = list(dict.fromkeys(str(value).strip() for value in video_ids if value))
        if not clean_ids:
            return []
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        claims: list[VideoCollectionClaim] = []
        for batch, scopes in self._addressed_batches(
            clean_ids, channel_scopes=channel_scopes, video_channels=video_channels,
        ):
            data = self._rpc(
                "claim_video_metadata_collection_ids",
                {
                    "p_video_ids": batch,
                    "p_channel_scopes": scopes,
                    "p_worker_id": worker_id,
                    "p_run_id": run_id,
                    "p_lease_seconds": lease_seconds,
                },
            )
            claims.extend(
                self._claims(
                    data,
                    expected_video_type="long",
                    operation="forced metadata collection claim",
                )
            )
        return claims

    def complete_initial_batch(self, commands: Sequence[Mapping[str, Any]]) -> int:
        """Commit one initial response, including mixed outcomes, without replay."""
        from .initial_video_batch import complete_initial_video_batch, validate_commands
        from ..hot.hot_collection_control import dispatch_owned_rpc
        prepared = validate_commands(commands)
        rows = [row for command in prepared for row in command["rows"]]
        if not rows:
            return 0
        return self._execute(
            "complete_initial_video_batch",
            lambda: dispatch_owned_rpc(
                self.database, "complete_initial_video_batch", {"p_rows": rows},
                lambda: complete_initial_video_batch(self.database, prepared),
            ),
        )

    def complete_counters(self, rows: Sequence[Mapping[str, Any]]) -> int:
        return self._complete("complete_video_counter_collection", rows)

    def complete_metadata(self, rows: Sequence[Mapping[str, Any]]) -> int:
        return self._complete("complete_video_profile_metadata_collection", rows)

    def complete_identity_refresh(self, rows: Sequence[Mapping[str, Any]]) -> int:
        return self._complete("complete_video_identity_refresh", rows, batch_size=50)

    @staticmethod
    def _typed_rows(
        rows: Sequence[Mapping[str, Any]],
        *,
        expected_video_type: str | None = None,
        require_single_type: bool = False,
    ) -> list[dict[str, Any]]:
        payload = [dict(row) for row in rows]
        video_types = {
            _typed_video_type(row.get("video_type"))
            for row in payload
        }
        if (
            expected_video_type is not None
            and video_types != {expected_video_type}
        ):
            raise ValueError(
                f"typed completion requires {expected_video_type!r} rows"
            )
        if require_single_type and len(video_types) > 1:
            raise ValueError("typed metadata completion cannot mix video types")
        return payload

    def _complete_typed(
        self,
        rpc_name: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        expected_video_type: str | None = None,
        require_single_type: bool = False,
    ) -> int:
        if not rows:
            return 0
        payload = self._typed_rows(
            rows,
            expected_video_type=expected_video_type,
            require_single_type=require_single_type,
        )
        completed = 0
        for batch in _chunked(payload, MAX_TYPED_COLLECTION_RPC_ROWS):
            try:
                result = self._rpc(rpc_name, {"p_rows": batch})
            except Exception as error:
                from hot_runtime import HotDatabaseAdmissionDenied
                if isinstance(error, HotDatabaseAdmissionDenied): raise
                # Typed completion operations persist attempt receipts, so one
                # replay is safe if the commit response was lost.
                result = self._rpc(rpc_name, {"p_rows": batch}, abort_on_denied=False)
            completed += _scalar_count(result)
        if completed != len(payload):
            raise RuntimeError(
                f"{rpc_name} completed {completed} of "
                f"{len(payload)} requested rows"
            )
        return completed

    def complete_typed_metadata(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        # The named Python writer has no legacy metadata fallback.
        # An unavailable typed contract stops this collection path closed.
        return self._complete_typed(
            "complete_typed_video_metadata_collection",
            rows,
            require_single_type=True,
        )

    def complete_stream_lifecycle(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        payload = [dict(row) for row in rows]
        if any(row.get("video_type") not in ("long", "short", "stream", "unknown") for row in payload):
            raise ValueError("publication completion requires a retained video identity")
        return self._complete("complete_stream_lifecycle_probe", payload,
                              batch_size=MAX_COMMON_COLLECTION_RPC_ROWS)

    def complete_missing(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Settle valid metadata omissions through the shared all-format owner."""
        payload = [dict(row) for row in rows]
        if any(str(row.get("video_type") or "").strip().lower()
               not in ("long", "short", "stream") for row in payload):
            raise ValueError("missing completion requires Long, Short or Stream claims")
        # Keep the existing <=50-row transactions and exact-body receipt replay.
        return self._complete("complete_typed_video_missing", payload,
                              batch_size=MAX_COMMON_COLLECTION_RPC_ROWS)

    def complete_long_missing(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Compatibility entrypoint; retire after Phase5 caller closure."""
        payload = [dict(row) for row in rows]
        if any(row.get("video_type") != "long" for row in payload):
            raise ValueError("long missing completion requires Long claims")
        # Preserve the former Long compatibility boundary for each batch.
        return sum(self.complete_missing(batch)
                   for batch in _chunked(payload, MAX_COMMON_COLLECTION_RPC_ROWS))

    def complete_typed_missing(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        """Compatibility entrypoint; keep the former typed-only validation."""
        return self.complete_missing(self._typed_rows(rows))

    def _complete(
        self, rpc_name: str, rows: Sequence[Mapping[str, Any]], *,
        batch_size: int = MAX_QUEUE_RPC_ROWS,
    ) -> int:
        if not rows:
            return 0
        completed = 0
        for batch in _chunked(list(rows), batch_size):
            try:
                result = self._rpc(rpc_name, {"p_rows": batch})
            except Exception as error:
                from hot_runtime import HotDatabaseAdmissionDenied
                if isinstance(error, HotDatabaseAdmissionDenied): raise
                # This is the only automatically replayed write in the
                # repository. Every row carries collection_attempt_id and the
                # completion contract is explicitly idempotent.
                result = self._rpc(rpc_name, {"p_rows": batch}, abort_on_denied=False)
            completed += _scalar_count(result)
        if completed != len(rows):
            raise RuntimeError(
                f"{rpc_name} completed {completed} of {len(rows)} requested rows"
            )
        return completed

    def fail(self, rows: Sequence[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        failed = 0
        for batch in _chunked(list(rows)):
            failed += _scalar_count(
                self._failure("fail_video_collection_claims", {"p_rows": batch},
                              lambda: video_failures.fail_video_collection_claims(self.database, batch))
            )
        if failed != len(rows):
            raise RuntimeError(
                f"fail_video_collection_claims released {failed} of "
                f"{len(rows)} requested claims"

            )
        return failed

    def fail_typed(
        self,
        video_type: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        parsed_type = _typed_video_type(video_type)
        if not rows:
            return 0
        payload = [dict(row) for row in rows]
        failed = 0
        for batch in _chunked(payload, MAX_TYPED_COLLECTION_RPC_ROWS):
            failed += _scalar_count(
                self._failure(
                    "fail_typed_video_collection_claims",
                    {"p_video_type": parsed_type, "p_rows": batch},
                    lambda: video_failures.fail_typed_video_collection_claims(self.database, parsed_type, batch),
                )
            )
        if failed != len(payload):
            raise RuntimeError(
                f"fail_typed_video_collection_claims released {failed} of "
                f"{len(payload)} requested claims"
            )
        return failed

    def fail_batch(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Release a mixed claim batch; an unknown ACK must reach the caller."""
        if not rows:
            return 0
        if len(rows) > MAX_COMMON_COLLECTION_RPC_ROWS:
            raise ValueError("common video collection failure accepts at most 50 rows")
        payload: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_row in rows:
            video_id = str(raw_row.get("video_id") or "").strip()
            lease_token = str(raw_row.get("lease_token") or "").strip()
            video_type = str(raw_row.get("video_type") or "").strip().lower()
            error_type = str(raw_row.get("error_type") or "").strip()
            retry_after = raw_row.get("retry_after_seconds")
            if not video_id or not lease_token or not error_type:
                raise ValueError("common video collection failure row is incomplete")
            if video_type not in VIDEO_COLLECTION_MODES_BY_TYPE:
                raise ValueError("common video collection failure type is invalid")
            if len(error_type) > 120:
                raise ValueError("common video collection failure error_type is too long")
            if video_id in seen_ids:
                raise ValueError("common video collection failure contains duplicate IDs")
            seen_ids.add(video_id)
            if retry_after is not None:
                if isinstance(retry_after, bool):
                    raise ValueError("retry_after_seconds must be a positive integer")
                retry_after = int(retry_after)
                if retry_after < 30 or retry_after > 86400:
                    raise ValueError("retry_after_seconds must be 30..86400")
            payload.append({
                "video_id": video_id,
                "video_type": video_type,
                "lease_token": lease_token,
                "error_type": error_type,
                "retry_after_seconds": retry_after,
            })
        # No receipt exists for failures. A lost acknowledgement is deliberately
        # surfaced instead of issuing a blind second write.
        failed = _scalar_count(
            self._failure("fail_video_collection_batch", {"p_rows": payload},
                          lambda: video_failures.fail_video_collection_batch(self.database, payload))
        )
        if failed != len(payload):
            raise RuntimeError(
                f"fail_video_collection_batch released {failed} of "
                f"{len(payload)} requested claims"
            )
        return failed

    def load_counter_presence(
        self,
        video_ids: Iterable[str],
    ) -> dict[str, Mapping[str, Any]]:
        clean_ids = list(dict.fromkeys(str(value).strip() for value in video_ids if value))
        rows: list[Mapping[str, Any]] = []
        for batch in _chunked(clean_ids, 500):
            response = self._execute(
                "select:video_counter_presence",
                lambda batch=batch: self.database.read_rows(
                    "collection_video_counter_presence",
                    "SELECT video_id, like_count, comment_count FROM public.videos "
                    "WHERE video_id = ANY(%s::text[]) LIMIT %s",
                    (batch, len(batch)), max_rows=len(batch),
                ),
            )
            data = response or []
            if not isinstance(data, list):
                raise TypeError("video counter presence query returned invalid data")
            rows.extend(data)
        return {
            str(row["video_id"]): dict(row)
            for row in rows
            if row.get("video_id")
        }

    def health(self) -> Mapping[str, Any]:
        data = self._execute("rpc:get_video_collection_health",
                             lambda: get_video_collection_health(self.database))

        rows = data or []
        if not isinstance(rows, list) or len(rows) != 1:
            raise TypeError("video collection health returned invalid data")
        return dict(rows[0])

    def typed_health(self) -> Mapping[str, Any]:
        return _mapping_result(
            self._execute("rpc:get_typed_video_collection_health",
                          lambda: get_typed_video_collection_health(self.database)),
            operation="typed video collection health",
        )
