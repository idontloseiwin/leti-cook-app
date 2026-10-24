"""Database boundary for canonical Long/Shorts/Streams playlist inventory.

PostgreSQL owns leases and atomic scan transitions.  Python owns only bounded
YouTube traversal between those short transactions.  This repository never
touches video metadata, counters, snapshots, or product-exposure gates.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from telemetry import emit_event

from ..base.repository import DatabaseRepository
from ..videos.collection_policy import cadence_report
from ..readiness import capabilities
from . import inventory_scans
from ..base.postgres import DatabaseServerError
from channel_collection_scope import require_control_epoch, validate_channel_scopes


MAX_TYPED_INVENTORY_CLAIMS = 1_000
MAX_TYPED_INVENTORY_DUE_CLAIMS = 500
MAX_TYPED_INVENTORY_PAGE_ITEMS = 50
MAX_TYPED_CANONICAL_REPAIR_ITEMS = 5_000
MAX_TYPED_CANONICAL_REPAIR_EXAMPLES = 25
TYPED_RUNTIME_VIDEO_TYPES = frozenset({"long", "short", "stream"})


class InventoryTokenCycleError(RuntimeError):
    """The server explicitly rejected and rolled back a repeated token."""


def _mapping_result(value: Any, *, operation: str) -> dict[str, Any]:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, Mapping):
        raise TypeError(f"{operation} returned invalid data")
    return dict(value)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if value))


@dataclass(frozen=True)
class TypedInventoryClaim:
    channel_id: str
    video_type: str
    playlist_id: str
    lease_token: str
    lease_channel_epoch: int
    lease_expires_at: str
    no_new_streak: int
    active_scan_id: str | None
    resume_page_token: str | None
    scan_kind: str
    scan_boundary_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TypedInventoryClaim":
        required = {
            key: str(row.get(key) or "").strip()
            for key in (
                "channel_id",
                "video_type",
                "playlist_id",
                "lease_token",
                "lease_expires_at",
                "scan_kind",
            )
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(
                "typed inventory claim is missing " + ", ".join(sorted(missing))
            )
        if required["video_type"] not in TYPED_RUNTIME_VIDEO_TYPES:
            raise ValueError("typed inventory claim has invalid video_type")
        if required["scan_kind"] not in {
            "initial_inventory",
            "full_reconciliation",
            "discovery",
        }:
            raise ValueError("typed inventory claim has invalid scan_kind")
        return cls(
            channel_id=required["channel_id"],
            video_type=required["video_type"],
            playlist_id=required["playlist_id"],
            lease_token=required["lease_token"],
            lease_channel_epoch=require_control_epoch(row.get("lease_channel_epoch")),
            lease_expires_at=required["lease_expires_at"],
            no_new_streak=max(int(row.get("no_new_streak") or 0), 0),
            active_scan_id=(
                str(row["active_scan_id"]) if row.get("active_scan_id") else None
            ),
            resume_page_token=(
                str(row["resume_page_token"])
                if row.get("resume_page_token")
                else None
            ),
            scan_kind=required["scan_kind"],
            scan_boundary_at=(
                str(row["scan_boundary_at"])
                if row.get("scan_boundary_at")
                else None
            ),
        )


class TypedInventoryRepository(DatabaseRepository):
    """Service-role owner for inventory-only typed playlist collection."""

    def prepare_channel_initial_inventory(self, channel_id: str) -> dict[str, Any]:
        """Ensure only this admitted channel's two missing inventory states."""
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel_id is required")
        return _mapping_result(
            self._rpc("prepare_channel_initial_typed_inventory", {
                "p_channel_id": channel_id,
            }),
            operation="channel initial typed inventory preparation",
        )

    def load_channel_initial_hydration(self, channel_id: str) -> dict[str, Any]:
        """Read stored inventory/outcomes; never prepare or claim work."""
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            raise ValueError("channel_id is required")
        return _mapping_result(
            self._rpc("get_channel_initial_typed_hydration", {
                "p_channel_id": channel_id,
            }),
            operation="channel initial typed hydration read",
        )

    def _execute(self, operation: str, callback):
        emit_event(
            "database",
            "typed_inventory_repository",
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
                "typed_inventory_repository",
                "postgres",
                operation,
                direction="response",
                status="error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=error.__class__.__name__,
            )
            raise
        data = response
        emit_event(
            "database",
            "typed_inventory_repository",
            "postgres",
            operation,
            direction="response",
            status="success",
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            count=len(data) if isinstance(data, list) else None,
        )
        return data

    def _rpc(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        retry_lost_ack: bool = False,
    ) -> Any:
        def execute():
            if name == "inspect_typed_inventory_canonical_repair":
                from . import inventory_canonical_preview
                return inventory_canonical_preview.inspect_typed_inventory_canonical_repair(
                    self.database, example_limit=dict(params or {})["p_example_limit"])
            if name == "repair_typed_inventory_canonical_types":
                from . import inventory_canonical_repair
                values = dict(params or {})
                return inventory_canonical_repair.repair_typed_inventory_canonical_types(self.database,
                    expected_count=values["p_expected_count"], expected_fingerprint=values["p_expected_fingerprint"])
            if name == "prepare_typed_inventory_runtime":
                from . import inventory_runtime_preparation
                values = dict(params or {})
                return inventory_runtime_preparation.prepare_typed_inventory_runtime(self.database,
                    limit=values["p_limit"], enable=values["p_enable"])
            if name == "get_channel_initial_typed_hydration":
                from . import inventory_runtime_queries
                values = dict(params or {})
                return inventory_runtime_queries.get_channel_initial_typed_hydration(
                    self.database, channel_id=values["p_channel_id"])
            if name == "prioritize_typed_inventory_channels":
                from . import inventory_priority
                values = dict(params or {})
                return inventory_priority.prioritize_typed_inventory_channels(self.database,
                    channel_ids=values["p_channel_ids"], expected_channel_scopes=values["p_expected_channel_scopes"])
            if name == "prepare_channel_initial_typed_inventory":
                from . import inventory_initial_preparation
                values = dict(params or {})
                return inventory_initial_preparation.prepare_channel_initial_typed_inventory(
                    self.database, channel_id=values["p_channel_id"])
            if name == "claim_due_typed_inventory":
                from . import inventory_claims
                values = dict(params or {})
                return inventory_claims.claim_due_typed_inventory(self.database,
                    limit=values["p_limit"], worker_id=values["p_worker_id"],
                    run_id=values["p_run_id"], lease_seconds=values["p_lease_seconds"])
            if name == "claim_inventory_pass":
                from . import inventory_claims
                values = dict(params or {})
                return inventory_claims.claim_inventory_pass(self.database,
                    continuations=values["p_continuations"], limit=values["p_limit"],
                    worker_id=values["p_worker_id"], run_id=values["p_run_id"],
                    lease_seconds=values["p_lease_seconds"], include_due=values["p_include_due"])
            if name == "claim_typed_inventory_channels":
                from . import inventory_claims
                values = dict(params or {})
                return inventory_claims.claim_typed_inventory_channels(self.database,
                    channel_ids=values["p_channel_ids"], video_types=values["p_video_types"],
                    worker_id=values["p_worker_id"], run_id=values["p_run_id"],
                    lease_seconds=values["p_lease_seconds"],
                    expected_channel_scopes=values["p_expected_channel_scopes"],
                    only_due=values.get("p_only_due",False))
            if name == "inventory_candidate_page":
                from .inventory_candidates import candidate_page
                return candidate_page(self.database, **dict(params or {}))
            if name == "claim_initial_inventory_source":
                from . import inventory_claims
                values = dict(params or {})
                return inventory_claims.claim_initial_inventory_source(self.database,
                    job_id=values["p_job_id"], job_lease_token=values["p_job_lease_token"],
                    worker_id=values["p_worker_id"], run_id=values["p_run_id"],
                    lease_seconds=values["p_lease_seconds"])
            if name == "begin_typed_inventory_scan":
                values = dict(params or {})
                return inventory_scans.begin_typed_inventory_scan(self.database,
                    channel_id=values["p_channel_id"], video_type=values["p_video_type"],
                    lease_token=values["p_lease_token"], scan_id=values["p_scan_id"],
                    scan_kind=values["p_scan_kind"], scan_boundary_at=values["p_scan_boundary_at"])
            if name == "fail_typed_inventory_scan":
                from . import inventory_scan_steps
                values = dict(params or {})
                return inventory_scan_steps.fail_typed_inventory_scan(self.database,
                    channel_id=values["p_channel_id"], video_type=values["p_video_type"],
                    lease_token=values["p_lease_token"], scan_id=values["p_scan_id"],
                    transition_id=values["p_transition_id"], expected_page_token=values["p_expected_page_token"],
                    outcome=values["p_outcome"], error_code=values["p_error_code"],
                    source_checked_at=values["p_source_checked_at"])
            if name == "settle_typed_inventory_page":
                from . import inventory_scan_pages
                values = dict(params or {})
                return inventory_scan_pages.settle_typed_inventory_page(self.database,
                    channel_id=values["p_channel_id"], video_type=values["p_video_type"],
                    lease_token=values["p_lease_token"], scan_id=values["p_scan_id"],
                    transition_id=values["p_transition_id"], expected_page_token=values["p_expected_page_token"],
                    next_page_token=values["p_next_page_token"], page_status=values["p_page_status"],
                    items=values["p_items"], source_checked_at=values["p_source_checked_at"])
            if name == "defer_typed_inventory_claims_after_daily_quota":
                from . import inventory_quota_deferral
                values = dict(params or {})
                return inventory_quota_deferral.defer_typed_inventory_claims_after_daily_quota(self.database,
                    transition_id=values["p_transition_id"], claims=values["p_claims"],
                    source_checked_at=values["p_source_checked_at"])
            raise ValueError("database_operation_not_registered")

        from ..hot.hot_collection_control import dispatch_owned_rpc
        from hot_runtime import HotDatabaseAdmissionDenied
        try:
            return dispatch_owned_rpc(self.database, name, params,
                lambda: self._execute("rpc:" + name, execute))
        except Exception as error:
            if not retry_lost_ack or isinstance(error, HotDatabaseAdmissionDenied):
                raise
            return dispatch_owned_rpc(self.database, name, params,
                lambda: self._execute("rpc-retry:" + name, execute), abort_on_denied=False)

    def assert_ready(self) -> dict[str, Any]:
        result = _mapping_result(
            self._execute("rpc:assert_typed_inventory_runtime_schema", lambda: capabilities.assert_typed_inventory_runtime_schema(self.database)),
            operation="typed inventory runtime assertion",
        )
        if (result.get("status") != "ok" or type(result.get("schema_version")) is not int
                or result["schema_version"] < 2
                or result.get("runtime_video_types") != ["long", "short", "stream"]
                or result.get("durable_fairness") is not True
                or result.get("product_exposure_independent_of_active_scan") is not True
                or type(result.get("initial_chunk_pages")) is not int
                or result["initial_chunk_pages"] != 1):
            raise RuntimeError("shared inventory runtime schema is not ready")
        self.assert_quota_defer_ready()
        return result

    def assert_receipts_ready(self) -> dict[str, Any]:
        """Read-only capability for the scoped inventory acknowledgement store."""
        result = _mapping_result(
            self._execute("rpc:assert_inventory_receipts_schema", lambda: capabilities.assert_inventory_receipts_schema(self.database)),
            operation="inventory receipts schema assertion",
        )
        if (result.get("status") != "ok"
                or type(result.get("inventory_receipt_scopes")) is not int
                or result["inventory_receipt_scopes"] != 2
                or result.get("service_direct_writes") is not True):
            raise RuntimeError("inventory receipts schema is not ready")
        return result

    def assert_quota_defer_ready(self) -> dict[str, Any]:
        result = _mapping_result(
            self._execute("rpc:assert_typed_inventory_quota_defer_schema", lambda: capabilities.assert_typed_inventory_quota_defer_schema(self.database)),
            operation="typed inventory quota defer assertion",
        )
        try:
            valid_numbers = (
                int(result.get("schema_version") or 0) >= 1
                and int(result.get("claim_limit") or 0) == 500
                and int(result.get("retry_floor_seconds") or 0) == 3600
            )
        except (TypeError, ValueError):
            valid_numbers = False
        expected_jitter = cadence_report(self.database)["typed_jitter_seconds"]
        reported_jitter = result.get("typed_jitter_seconds")
        valid_jitter = (
            result.get("schedule_source") == "process_snapshot"
            and isinstance(reported_jitter, Mapping)
            and set(reported_jitter) == set(expected_jitter)
            and all(type(reported_jitter[kind]) is int
                    and reported_jitter[kind] == expected_jitter[kind]
                    for kind in expected_jitter)
        )
        if not (
            str(result.get("status") or "") == "ok"
            and valid_numbers and valid_jitter
            and result.get("lost_ack_receipts") is True
            and result.get("active_scan_state_preserved") is True
            and result.get("quota_claim_floor_guard") is True
            and result.get("retry_until") == "youtube_quota_reset"
        ):
            raise RuntimeError("typed inventory quota defer schema is not ready")
        return result

    def assert_canonical_repair_ready(self) -> dict[str, Any]:
        return _mapping_result(
            self._execute("rpc:assert_typed_inventory_canonical_repair_schema", lambda: capabilities.assert_typed_inventory_canonical_repair_schema(self.database)),
            operation="typed inventory canonical repair assertion",
        )

    def inspect_canonical_repair(
        self,
        *,
        example_limit: int = 0,
    ) -> dict[str, Any]:
        parsed_limit = int(example_limit)
        if (
            parsed_limit < 0
            or parsed_limit > MAX_TYPED_CANONICAL_REPAIR_EXAMPLES
        ):
            raise ValueError("typed canonical repair example limit must be 0..25")
        return _mapping_result(
            self._rpc(
                "inspect_typed_inventory_canonical_repair",
                {"p_example_limit": parsed_limit},
            ),
            operation="typed inventory canonical repair plan",
        )

    def repair_canonical_types(
        self,
        *,
        expected_count: int,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        parsed_count = int(expected_count)
        if parsed_count < 1 or parsed_count > MAX_TYPED_CANONICAL_REPAIR_ITEMS:
            raise ValueError("typed canonical repair expected count must be 1..5000")
        fingerprint = str(expected_fingerprint or "").strip().lower()
        if len(fingerprint) != 32 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("typed canonical repair fingerprint must be 32 hex chars")
        # One exact-set transaction is idempotent in durable state but has no
        # acknowledgement receipt. Surface an ambiguous transport result; the
        # operator must run a new read-only plan instead of blindly retrying.
        return _mapping_result(
            self._rpc(
                "repair_typed_inventory_canonical_types",
                {
                    "p_expected_count": parsed_count,
                    "p_expected_fingerprint": fingerprint,
                },
            ),
            operation="typed inventory canonical repair",
        )

    def prepare_runtime(self, *, limit: int, enable: bool) -> dict[str, Any]:
        parsed_limit = int(limit)
        if parsed_limit < 1 or parsed_limit > MAX_TYPED_INVENTORY_CLAIMS:
            raise ValueError("typed inventory prepare limit must be 1..1000")
        return _mapping_result(
            self._rpc(
                "prepare_typed_inventory_runtime",
                {"p_limit": parsed_limit, "p_enable": bool(enable)},
            ),
            operation="typed inventory runtime preparation",
        )

    @staticmethod
    def _claims(value: Any) -> list[TypedInventoryClaim]:
        if not isinstance(value, list):
            raise TypeError("typed inventory claim returned invalid data")
        return [TypedInventoryClaim.from_row(row) for row in value]

    def claim_due(
        self,
        *,
        limit: int,
        worker_id: str,
        run_id: str,
        lease_seconds: int = 900,
    ) -> list[TypedInventoryClaim]:
        parsed_limit = int(limit)
        if parsed_limit < 0 or parsed_limit > MAX_TYPED_INVENTORY_DUE_CLAIMS:
            raise ValueError("typed inventory due claim limit must be 0..500")
        if parsed_limit == 0:
            return []
        return self._claims(
            self._rpc(
                "claim_due_typed_inventory",
                {
                    "p_limit": parsed_limit,
                    "p_worker_id": str(worker_id),
                    "p_run_id": str(run_id),
                    "p_lease_seconds": int(lease_seconds),
                },
            )
        )

    def claim_pass(
        self, *, continuations: Sequence[Mapping[str, Any]], limit: int,
        include_due: bool, worker_id: str, run_id: str, lease_seconds: int = 900,
    ) -> list[TypedInventoryClaim]:
        """One ordinal queue for ordinary due work and exact own continuations."""
        if type(limit) is not int or not 0 <= limit <= MAX_TYPED_INVENTORY_DUE_CLAIMS:
            raise ValueError("inventory pass claim limit must be 0..500")
        if type(include_due) is not bool:
            raise TypeError("include_due must be a boolean")
        if len(continuations) > 500:
            raise ValueError("inventory pass accepts at most 500 continuation proofs")
        required = {"channel_id", "video_type", "scan_id", "control_epoch", "last_transition_id"}
        clean, seen = [], set()
        for proof in continuations:
            if not isinstance(proof, Mapping) or set(proof) != required:
                raise ValueError("inventory continuation proof has invalid fields")
            row = dict(proof)
            identity = (row["channel_id"], row["video_type"])
            if (not isinstance(row["channel_id"], str) or not row["channel_id"]
                    or row["video_type"] not in TYPED_RUNTIME_VIDEO_TYPES or identity in seen):
                raise ValueError("inventory continuation identity is invalid")
            row["control_epoch"] = require_control_epoch(row["control_epoch"])
            for name in ("scan_id", "last_transition_id"):
                row[name] = str(uuid.UUID(str(row[name])))
            clean.append(row)
            seen.add(identity)
        if not limit:
            return []
        return self._claims(self._rpc("claim_inventory_pass", {
            "p_continuations": clean, "p_limit": limit, "p_include_due": include_due,
            "p_worker_id": str(worker_id), "p_run_id": str(run_id),
            "p_lease_seconds": int(lease_seconds),
        }))

    def claim_initial_source(
        self, *, job_id: str, job_lease_token: str, worker_id: str,
        run_id: str, lease_seconds: int = 900,
    ) -> list[TypedInventoryClaim]:
        """Claim only the prepared source owned by this exact initial job lease."""
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("initial inventory worker identity is invalid")
        if type(lease_seconds) is not int or not 60 <= lease_seconds <= 3600:
            raise ValueError("initial inventory lease must be 60..3600 seconds")
        params = {
            "p_job_id": str(uuid.UUID(str(job_id))),
            "p_job_lease_token": str(uuid.UUID(str(job_lease_token))),
            "p_worker_id": worker_id, "p_run_id": str(uuid.UUID(str(run_id))),
            "p_lease_seconds": lease_seconds,
        }
        rows = self._rpc("claim_initial_inventory_source", params)
        if not isinstance(rows, list) or len(rows) > 1:
            raise RuntimeError("initial inventory source claim exceeded its bound")
        return self._claims(rows)

    def candidate_page(self, *, limit=16, after=None, ceiling=None, continuations=()):
        """Advisory physical window; caller bounds limit by free ready capacity."""
        return self._rpc("inventory_candidate_page", dict(limit=limit, after=after,
            ceiling=ceiling, continuations=continuations))

    def claim_channels(
        self,
        channel_ids: Iterable[str],
        *,
        video_types: Iterable[str] = ("short", "stream"),
        worker_id: str,
        run_id: str,
        lease_seconds: int = 900,
        expected_channel_scopes: list[Mapping[str, Any]] | None = None,
        only_due: bool = False,
    ) -> list[TypedInventoryClaim]:
        if type(only_due) is not bool:
            raise ValueError("typed_inventory_due_scope_invalid")
        clean_channel_ids = _unique(channel_ids)
        clean_video_types = _unique(video_types)
        if not clean_channel_ids:
            return []
        if len(clean_channel_ids) > MAX_TYPED_INVENTORY_CLAIMS:
            raise ValueError("typed inventory explicit claim accepts at most 1000 channels")
        if not clean_video_types or any(
            video_type not in TYPED_RUNTIME_VIDEO_TYPES
            for video_type in clean_video_types
        ):
            raise ValueError("typed inventory explicit claim has invalid video types")
        return self._claims(
            self._rpc(
                "claim_typed_inventory_channels",
                {
                    "p_channel_ids": clean_channel_ids,
                    "p_video_types": clean_video_types,
                    "p_worker_id": str(worker_id),
                    "p_run_id": str(run_id),
                    "p_lease_seconds": int(lease_seconds),
                    "p_expected_channel_scopes": expected_channel_scopes,
                    **({"p_only_due": True} if only_due else {}),
                },
            )
        )

    def list_active_runtime_scan_pairs(self, *, limit: int = 500) -> list[tuple[str, str]]:
        """Bounded continuation identities; claims retain lease/quota authority."""
        if not 1 <= int(limit) <= 500:
            raise ValueError("typed inventory continuation limit must be 1..500")
        rows = self._execute(
            "select:typed_inventory_active_scan_pairs",
            lambda: self.database.read_rows(
                "inventory_active_scan_pairs",
                "SELECT channel_id, video_type FROM public.channel_playlist_state "
                "WHERE runtime_enabled = true AND video_type IN ('short', 'stream') "
                "AND active_scan_id IS NOT NULL "
                "ORDER BY channel_id ASC NULLS LAST, video_type ASC NULLS LAST LIMIT %s",
                (int(limit),), max_rows=int(limit),
            ),
        )
        if not isinstance(rows, list) or len(rows) > int(limit):
            raise TypeError("typed inventory continuation query returned invalid data")
        result = []
        for row in rows:
            channel_id = str(row.get("channel_id") or "")
            video_type = row.get("video_type")
            if not channel_id or video_type not in TYPED_RUNTIME_VIDEO_TYPES:
                raise TypeError("typed inventory continuation identity is invalid")
            result.append((channel_id, video_type))
        return list(dict.fromkeys(result))

    def begin_scan(
        self,
        claim: TypedInventoryClaim,
        *,
        scan_id: str,
        scan_boundary_at: datetime,
    ) -> dict[str, Any]:
        return _mapping_result(
            self._rpc(
                "begin_typed_inventory_scan",
                {
                    "p_channel_id": claim.channel_id,
                    "p_video_type": claim.video_type,
                    "p_lease_token": claim.lease_token,
                    "p_scan_id": str(scan_id),
                    "p_scan_kind": claim.scan_kind,
                    "p_scan_boundary_at": _utc_iso(scan_boundary_at),
                },
                retry_lost_ack=True,
            ),
            operation="typed inventory scan begin",
        )

    def known_membership_ids(
        self,
        claim: TypedInventoryClaim,
        video_ids: Iterable[str],
        *,
        scan_id: str,
        scan_boundary_at: datetime,
    ) -> set[str]:
        clean_ids = _unique(video_ids)
        if not clean_ids:
            return set()
        rows = []
        for start in range(0, len(clean_ids), 1000):
            batch = clean_ids[start:start + 1000]
            data = self._execute(
                "select:typed_inventory_membership_boundary",
                lambda batch=batch: self.database.read_rows(
                    "inventory_membership_boundary",
                    "SELECT video_id, first_seen_at, last_seen_scan_id "
                    "FROM public.video_inventory_memberships "
                    "WHERE channel_id = %s AND video_type = %s "
                    "AND membership_status = 'present' AND video_id = ANY(%s::text[]) LIMIT %s",
                    (claim.channel_id, claim.video_type, batch, len(batch)), max_rows=len(batch),
                ),
            )
            if not isinstance(data, list):
                raise TypeError("typed inventory membership query returned invalid data")
            rows.extend(data)
        if not isinstance(rows, list):
            raise TypeError("typed inventory membership query returned invalid data")
        boundary = scan_boundary_at
        if boundary.tzinfo is None:
            boundary = boundary.replace(tzinfo=timezone.utc)
        boundary = boundary.astimezone(timezone.utc)
        known = set()
        for row in rows:
            video_id = str(row.get("video_id") or "").strip()
            if not video_id:
                continue
            first_seen_at = _parse_datetime(row.get("first_seen_at"))
            if first_seen_at is not None and first_seen_at <= boundary:
                known.add(video_id)
            elif str(row.get("last_seen_scan_id") or "") != str(scan_id):
                known.add(video_id)
        return known

    def settle_page(
        self,
        claim: TypedInventoryClaim,
        *,
        scan_id: str,
        transition_id: str | None,
        expected_page_token: str | None,
        next_page_token: str | None,
        page_status: str,
        items: Sequence[Mapping[str, Any]],
        source_checked_at: datetime,
    ) -> dict[str, Any]:
        normalized_items = []
        seen = set()
        for item in items:
            video_id = str(item.get("video_id") or "").strip()
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            normalized_items.append(
                {"video_id": video_id, "published_at": item.get("published_at")}
            )
        if len(normalized_items) > MAX_TYPED_INVENTORY_PAGE_ITEMS:
            raise ValueError("typed inventory page exceeds 50 unique items")
        params = {
            "p_channel_id": claim.channel_id,
            "p_video_type": claim.video_type,
            "p_lease_token": claim.lease_token,
            "p_scan_id": str(scan_id),
            "p_transition_id": str(transition_id or uuid.uuid4()),
            "p_expected_page_token": expected_page_token,
            "p_next_page_token": next_page_token,
            "p_page_status": str(page_status),
            "p_items": normalized_items,
            "p_source_checked_at": _utc_iso(source_checked_at),
        }
        try:
            value = self._rpc("settle_typed_inventory_page", params, retry_lost_ack=True)
        except Exception as error:
            # A direct database exception is a confirmed rolled-back transaction;
            # timeout/transport errors never enter this terminal-outcome path.
            if (isinstance(error, DatabaseServerError) and getattr(error, "code", None) == "P0001"
                    and getattr(error, "message", None) == "inventory_token_cycle"):
                raise InventoryTokenCycleError("inventory_token_cycle") from error
            raise
        return _mapping_result(value, operation="typed inventory page settlement")

    def fail_scan(
        self,
        claim: TypedInventoryClaim,
        *,
        scan_id: str,
        transition_id: str | None,
        expected_page_token: str | None,
        outcome: str,
        error_code: str,
        source_checked_at: datetime,
    ) -> dict[str, Any]:
        params = {
            "p_channel_id": claim.channel_id,
            "p_video_type": claim.video_type,
            "p_lease_token": claim.lease_token,
            "p_scan_id": str(scan_id),
            "p_transition_id": str(transition_id or uuid.uuid4()),
            "p_expected_page_token": expected_page_token,
            "p_outcome": str(outcome),
            "p_error_code": str(error_code),
            "p_source_checked_at": _utc_iso(source_checked_at),
        }
        return _mapping_result(
            self._rpc(
                "fail_typed_inventory_scan",
                params,
                retry_lost_ack=True,
            ),
            operation="typed inventory scan failure",
        )

    def defer_claims_after_daily_quota(
        self,
        claims: Sequence[TypedInventoryClaim],
        *,
        transition_id: str,
        source_checked_at: datetime,
    ) -> dict[str, Any]:
        """Release exact owned leases without changing resumable scan state."""

        normalized_claims = []
        seen = set()
        for claim in claims:
            if not isinstance(claim, TypedInventoryClaim):
                raise TypeError("typed inventory quota defer claim is invalid")
            identity = (claim.channel_id, claim.video_type)
            if identity in seen:
                raise ValueError("typed inventory quota defer claims must be unique")
            seen.add(identity)
            normalized_claims.append({
                "channel_id": claim.channel_id,
                "video_type": claim.video_type,
                "lease_token": claim.lease_token,
            })
        if not normalized_claims or len(normalized_claims) > MAX_TYPED_INVENTORY_DUE_CLAIMS:
            raise ValueError("typed inventory quota defer accepts 1..500 claims")
        clean_transition_id = str(transition_id or "").strip()
        if not clean_transition_id:
            raise ValueError("typed inventory quota defer transition is required")
        return _mapping_result(
            self._rpc(
                "defer_typed_inventory_claims_after_daily_quota",
                {
                    "p_transition_id": clean_transition_id,
                    "p_claims": normalized_claims,
                    "p_source_checked_at": _utc_iso(source_checked_at),
                },
                retry_lost_ack=True,
            ),
            operation="typed inventory daily quota defer",
        )

    def prioritize_channels(self, channel_ids: Iterable[str], *, expected_channel_scopes=None) -> int:
        clean_ids = _unique(channel_ids)
        if not clean_ids:
            return 0
        if len(clean_ids) > MAX_TYPED_INVENTORY_CLAIMS:
            raise ValueError("typed inventory priority accepts at most 1000 channels")
        captured = (None if expected_channel_scopes is None else
                    validate_channel_scopes(expected_channel_scopes, channel_ids=clean_ids))
        total = 0
        for start in range(0, len(clean_ids), 500):
            value = self._rpc(
                "prioritize_typed_inventory_channels",
                {"p_channel_ids": clean_ids[start : start + 500],
                 "p_expected_channel_scopes": (None if captured is None else
                     [scope for scope in captured if scope["channel_id"] in clean_ids[start : start + 500]])},
            )
            if isinstance(value, list) and len(value) == 1:
                value = value[0]
            if isinstance(value, Mapping):
                value = value.get("affected_count", value.get("count", 0))
            total += int(value or 0)
        return total


    def resolved_video_types(
        self,
        channel_id: str,
        video_ids: Iterable[str],
    ) -> dict[str, str]:
        clean_ids = _unique(video_ids)
        if not clean_ids:
            return {}
        rows = []
        for start in range(0, len(clean_ids), 1000):
            batch = clean_ids[start:start + 1000]
            data = self._execute(
                "select:resolved_typed_inventory",
                lambda batch=batch: self.database.read_rows(
                    "inventory_resolved_types",
                    "SELECT video_id, canonical_channel_id, canonical_video_type, resolution_status "
                    "FROM public.video_inventory_type_resolution "
                    "WHERE canonical_channel_id = %s AND resolution_status = 'resolved' "
                    "AND video_id = ANY(%s::text[]) LIMIT %s",
                    (str(channel_id), batch, len(batch)), max_rows=len(batch),
                ),
            )
            if not isinstance(data, list):
                raise TypeError("typed inventory resolution query returned invalid data")
            rows.extend(data)
        if not isinstance(rows, list):
            raise TypeError("typed inventory resolution query returned invalid data")
        return {
            str(row["video_id"]): str(row["canonical_video_type"])
            for row in rows
            if row.get("video_id")
            and row.get("canonical_video_type") in {"long", "short", "stream"}
        }
