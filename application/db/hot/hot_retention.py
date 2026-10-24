"""Named, bounded hot-source maintenance; no scheduling or provider dependency."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
import re
import subprocess
from typing import Any

from hot_storage import HOT_DATA_WINDOW, utc_instant


TARGET_FUNCTIONS = {
    "deleted_channels": "cleanup_hot_deleted_channels",
    **dict.fromkeys(("video_counters", "video_metadata", "video_availability",
                     "channel_profile", "channel_country", "channel_inventory_estimate"),
                    "cleanup_hot_current_source"),
    **dict.fromkeys(("video_snapshots", "channel_snapshots", "typed_video_collection_receipts",
                     "availability_events", "availability_incidents", "review_evidence"),
                    "cleanup_hot_evidence"),
    **dict.fromkeys(("memberships", "inventory"), "cleanup_hot_inventory"),
    **dict.fromkeys(("playlist_state", "inventory_scans", "inventory_receipts"),
                    "cleanup_hot_inventory_copies"),
    **dict.fromkeys(("operator_jobs", "operator_job_targets", "sync_runs",
                     "quota_attempts"), "cleanup_hot_operational_copies"),
    # Tombstones of deleted saved views/collections and their receipts.
    "deleted_saved_objects": "cleanup_deleted_saved_objects",
    "category_reference": "cleanup_hot_category_reference",
}
# Source data expires after 29 days; deleted saved-object tombstones after 30.
RETENTION_WINDOWS = {"deleted_saved_objects": timedelta(days=30)}
DEFAULT_RETENTION_WINDOW = HOT_DATA_WINDOW
MAX_PAGE = 100
MAX_PRIORITY_REFERENCES = 100
# Exact retries share every owner that emits blocking retention telemetry.
# Estimate normalization currently emits no age/unknown blocker telemetry.
PRIORITY_TARGETS = {
    **dict.fromkeys(("video_counters", "video_metadata", "video_availability",
                     "channel_profile", "channel_country", "inventory",
                     "video_snapshots", "availability_events", "availability_incidents",
                     "review_evidence", "inventory_scans", "operator_jobs", "sync_runs",
                     "quota_attempts", "category_reference"), 1),
    **dict.fromkeys(("channel_snapshots", "typed_video_collection_receipts", "playlist_state",
                     "inventory_receipts", "operator_job_targets", "deleted_saved_objects",
                     "deleted_channels"), 2),
    "memberships": 3,
}


class RetentionError(RuntimeError):
    """A fixed diagnostic code. Database messages/payloads are never rendered."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _instant(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 48:
        raise RetentionError("invalid_cleanup_result")
    try:
        return utc_instant(value)
    except (ValueError, OverflowError):
        raise RetentionError("invalid_cleanup_result") from None


def _count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise RetentionError("invalid_cleanup_result")
    return value


@dataclass(frozen=True)
class PriorityReference:
    target: str
    key: tuple[str, ...]

    @classmethod
    def parse(cls, value):
        if (type(value) is not dict or set(value) != {"target", "key"}
                or type(value["target"]) is not str or value["target"] not in PRIORITY_TARGETS
                or type(value["key"]) is not list
                or len(value["key"]) != PRIORITY_TARGETS[value["target"]]
                or any(type(part) is not str or not 1 <= len(part) <= 128 or "\0" in part
                       for part in value["key"])
                or (value["target"] == "memberships" and value["key"][2] not in ("long", "short", "stream"))
                or (value["target"] == "playlist_state" and value["key"][1] not in ("long", "short", "stream"))
                or (value["target"] == "deleted_channels" and value["key"][0] not in ("channel", "restore"))
                or (value["target"] == "deleted_saved_objects" and value["key"][0] not in ("view", "collection"))
                or (value["target"] == "category_reference" and value["key"] != ["singleton"])):
            raise RetentionError("invalid_cleanup_result")
        return cls(value["target"], tuple(value["key"]))

    def wire(self):
        return {"target": self.target, "key": list(self.key)}


def priority_references(value):
    if type(value) is not list or len(value) > MAX_PRIORITY_REFERENCES:
        raise RetentionError("invalid_cleanup_result")
    result = tuple(PriorityReference.parse(item) for item in value)
    if len(set(result)) != len(result):
        raise RetentionError("invalid_cleanup_result")
    return result


@dataclass(frozen=True)
class PriorityResult:
    as_of: str
    checked: tuple[PriorityReference, ...]
    remaining: tuple[PriorityReference, ...]
    complete: bool
    deleted: int
    scrubbed: int

    @classmethod
    def parse(cls, value, requested):
        if (type(value) is not dict or set(value) != {
                "as_of", "checked", "remaining", "complete", "deleted", "scrubbed"}
                or type(value["complete"]) is not bool):
            raise RetentionError("invalid_cleanup_result")
        checked = priority_references(value["checked"])
        remaining = priority_references(value["remaining"])
        if (not set(remaining) <= set(checked) or not set(checked) <= set(requested)
                or (value["complete"] and set(checked) != set(requested))):
            raise RetentionError("invalid_cleanup_result")
        return cls(_instant(value["as_of"]).isoformat(), checked, remaining,
                   value["complete"], _count(value["deleted"]), _count(value["scrubbed"]))


@dataclass(frozen=True)
class CleanupPage:
    target: str
    as_of: str
    cutoff: str
    scanned: int
    deleted: int
    scrubbed: int
    deferred: int
    complete: bool
    next_cursor: str | None
    oldest_remaining_expired_source_at: str | None
    invalid_clock_remaining: int
    # Deferred rows whose age was not established. Busy alone is not expiry;
    # these observations request retry, not new collection priority.
    unknown_expiry_remaining: int = 0
    # Private RAM-only identities. summary() deliberately excludes them.
    priority_references: tuple[PriorityReference, ...] = ()
    # Coverage of observed unsafe rows; unknown-only rows need no new cause.
    priority_complete: bool = False
    # Canonical removal expiry has no source clock (deleted_channels only).
    confirmed_expired_remaining: int = 0

    @classmethod
    def parse(cls, target: str, value: Any, limit: int) -> "CleanupPage":
        if not isinstance(value, dict):
            raise RetentionError("invalid_cleanup_result")
        as_of, cutoff = value.get("as_of"), value.get("cutoff")
        now, floor = _instant(as_of), _instant(cutoff)
        if now - floor != RETENTION_WINDOWS.get(target, DEFAULT_RETENTION_WINDOW):
            raise RetentionError("invalid_cleanup_result")
        counts = {key: _count(value.get(key)) for key in ("scanned", "deleted", "scrubbed", "deferred")}
        if counts["scanned"] > limit or type(value.get("complete")) is not bool:
            raise RetentionError("invalid_cleanup_result")
        cursor = value.get("next_cursor")
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or "\0" in cursor):
            raise RetentionError("invalid_cleanup_result")
        oldest = value.get("oldest_remaining_expired_source_at")
        if oldest is not None and (_instant(oldest) > floor or (
                _instant(oldest) == floor and target != "category_reference")):
            raise RetentionError("invalid_cleanup_result")
        refs = priority_references(value.get("priority_references", []))
        coverage = value.get("priority_complete", False)
        invalid = _count(value.get("invalid_clock_remaining", 0))
        unknown = _count(value.get("unknown_expiry_remaining", 0))
        confirmed = _count(value.get("confirmed_expired_remaining", 0))
        if confirmed > counts["deferred"] or (confirmed and target != "deleted_channels"):
            raise RetentionError("invalid_cleanup_result")
        if (type(coverage) is not bool or any(ref.target != target for ref in refs)
                # Deleted channels also sample at most limit pending restore
                # owners independently; ordinary traversal counts only channels.
                or len(refs) > (counts["scanned"] + limit if target == "deleted_channels" else counts["scanned"])
                or (coverage and (target not in PRIORITY_TARGETS
                                  or invalid > len(refs)
                                  or confirmed > len(refs)
                                  or ((oldest or invalid or confirmed) and not refs)))):
            raise RetentionError("invalid_cleanup_result")
        return cls(target, now.isoformat(), floor.isoformat(), **counts, complete=value["complete"], next_cursor=cursor,
                   oldest_remaining_expired_source_at=_instant(oldest).isoformat() if oldest else None,
                   invalid_clock_remaining=invalid, unknown_expiry_remaining=unknown,
                   priority_references=refs, priority_complete=coverage,
                   confirmed_expired_remaining=confirmed)

    def summary(self) -> dict:
        # A cursor can contain an API locator. It is transient and never logged.
        return {"target": self.target, "as_of": self.as_of, "cutoff": self.cutoff,
                "scanned": self.scanned, "deleted": self.deleted, "scrubbed": self.scrubbed,
                "deferred": self.deferred, "scan_exhausted": self.complete,
                "oldest_remaining_expired_source_at": self.oldest_remaining_expired_source_at,
                "invalid_clock_remaining": self.invalid_clock_remaining,
                "unknown_expiry_remaining": self.unknown_expiry_remaining,
                "confirmed_expired_remaining": self.confirmed_expired_remaining,
                "telemetry_scope": "scanned_page"}


class HotRetentionRepository:
    def __init__(self, database):
        self.database = database

    def _call(self, function: str, params: dict) -> Any:
        # No automatic retry: a transport failure may follow a committed batch.
        if function == "cleanup_hot_priority":
            return self.database.cleanup_hot_priority(references=params["references"])
        if function == "cleanup_deleted_saved_objects":
            return self.database.cleanup_deleted_saved_objects(
                target=params["p_target"], after_id=params["p_after_id"], limit=params["p_limit"],
            )
        if function == "cleanup_hot_evidence":
            return self.database.cleanup_hot_evidence(
                target=params["p_target"], after_id=params["p_after_id"], limit=params["p_limit"],
            )
        if function in (
            "cleanup_hot_current_source", "cleanup_hot_inventory_copies",
            "cleanup_hot_operational_copies", "cleanup_hot_deleted_channels", "cleanup_hot_inventory",
            "cleanup_hot_category_reference",
        ):
            return getattr(self.database, function)(
                target=params["p_target"], after_id=params["p_after_id"], limit=params["p_limit"],
            )
        raise ValueError("unknown_cleanup_function")

    def cleanup_page(self, target: str, *, after_id: str | None = None, limit: int = 100) -> CleanupPage:
        if target not in TARGET_FUNCTIONS:
            raise ValueError("unknown_cleanup_target")
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError("invalid_cleanup_limit")
        if after_id is not None and (not isinstance(after_id, str) or not 1 <= len(after_id) <= 2048 or "\0" in after_id):
            raise ValueError("invalid_cleanup_cursor")
        try:
            value = self._call(TARGET_FUNCTIONS[target],
                               {"p_target": target, "p_after_id": after_id, "p_limit": limit})
        except RetentionError:
            raise
        except Exception:
            raise RetentionError("cleanup_outcome_unknown") from None
        return CleanupPage.parse(target, value, limit)

    def cleanup_priority(self, references):
        requested = priority_references([ref.wire() for ref in references])
        if not requested:
            raise ValueError("invalid_priority_references")
        try:
            value = self._call("cleanup_hot_priority", {"references": [ref.wire() for ref in requested]})
        except RetentionError:
            raise
        except Exception:
            raise RetentionError("cleanup_outcome_unknown") from None
        return PriorityResult.parse(value, requested)


class LocalTestRetentionRepository(HotRetentionRepository):
    """Explicit local synthetic stand only; never reads project credentials/.env."""

    def __init__(self, *, psql: str, database: str, port: int):
        if not re.fullmatch(r"typed_audit(?:_[a-z0-9]+)*", database):
            raise ValueError("synthetic_database_required")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid_test_port")
        self.psql, self.name, self.port = psql, database, port

    def _call(self, function: str, params: dict) -> Any:
        if function not in TARGET_FUNCTIONS.values():
            raise ValueError("unknown_cleanup_function")
        def literal(value):
            return "NULL" if value is None else "'" + value.replace("'", "''") + "'"
        sql = (f"SET ROLE service_role; SELECT public.{function}("
               f"{literal(params['p_target'])}, {literal(params['p_after_id'])}, {params['p_limit']});")
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
        environment.update({"PGPASSWORD": "typed_audit_no_real_password", "PGPASSFILE": os.devnull,
                            "PGSERVICEFILE": os.devnull, "PGCONNECT_TIMEOUT": "2", "PGSSLMODE": "disable",
                            "PGGSSENCMODE": "disable", "PGCLIENTENCODING": "UTF8",
                            "PGOPTIONS": "-c statement_timeout=5000 -c lock_timeout=250 -c idle_in_transaction_session_timeout=5000 -c standard_conforming_strings=on"})
        try:
            result = subprocess.run([self.psql, "-X", "-w", "-qAt", "-v", "ON_ERROR_STOP=1",
                                     "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres", "-d", self.name],
                                    input=sql, capture_output=True, text=True, encoding="utf-8",
                                    env=environment, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RetentionError("cleanup_outcome_unknown") from None
        if result.returncode != 0:
            # stderr may include query context, locators, or source values.
            raise RetentionError("cleanup_outcome_unknown")
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise RetentionError("invalid_cleanup_result") from None
