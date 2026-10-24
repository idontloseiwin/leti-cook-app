"""Application service for durable manual operator jobs."""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from db.base.postgres import DatabaseUnavailable

from db import OperatorJobRepository
from hot_storage import source_is_current


OPERATOR_JOBS_MODE_ENV = "OPERATOR_JOBS_MODE"
OPERATOR_JOB_MODES = {"durable", "drain"}
_LEGACY_MODE_WARNING_LOCK = threading.Lock()
_LEGACY_MODE_WARNED = False
DEFAULT_CHUNK_SIZE = 50
DEFAULT_LEASE_SECONDS = 1200
DEFAULT_MAX_TARGET_ATTEMPTS = 3
DEFAULT_INITIAL_INVENTORY_PAGES_PER_CHUNK = 5
INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT = 10000
VERIFY_TRANSPORT_ATTEMPTS = 3
OPERATOR_JOB_WAKE_PATH_ENV = "OPERATOR_JOB_WAKE_PATH"
DEFAULT_OPERATOR_JOB_WAKE_PATH = (
    str(Path.home() / ".local" / "state" / "leti-cook" / "operator-jobs.wake")
)


def operator_jobs_mode(environ: Mapping[str, str] | None = None) -> str:
    """Resolve one durable engine; drain changes admission, not execution."""
    values = os.environ if environ is None else environ
    supplied = values.get(OPERATOR_JOBS_MODE_ENV)
    if supplied is None:
        mode = "durable"
    elif isinstance(supplied, str):
        mode = supplied.strip().lower() or "durable"
    else:
        raise ValueError("operator_jobs_mode_invalid")
    if mode == "legacy":
        global _LEGACY_MODE_WARNED
        with _LEGACY_MODE_WARNING_LOCK:
            if not _LEGACY_MODE_WARNED:
                print("OPERATOR_JOBS_MODE=legacy is deprecated; using durable jobs. "
                      "Set OPERATOR_JOBS_MODE=durable to remove this warning.", file=sys.stderr)
                _LEGACY_MODE_WARNED = True
        return "durable"
    if mode not in OPERATOR_JOB_MODES:
        raise ValueError(
            f"{OPERATOR_JOBS_MODE_ENV} must be one of: " + ", ".join(sorted(OPERATOR_JOB_MODES))
        )
    return mode


def signal_operator_job_worker(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Write a non-authoritative wake signal for the external systemd worker.

    Postgres remains the durable queue. Losing this signal is safe because the
    fallback timer will still discover the queued job.
    """
    values = os.environ if environ is None else environ
    path = Path(
        str(
            values.get(
                OPERATOR_JOB_WAKE_PATH_ENV,
                DEFAULT_OPERATOR_JOB_WAKE_PATH,
            )
            or DEFAULT_OPERATOR_JOB_WAKE_PATH
        )
    )
    if not path.is_absolute():
        raise ValueError(f"{OPERATOR_JOB_WAKE_PATH_ENV} must be an absolute path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, b"1")
    finally:
        os.close(descriptor)
    return path


def _positive_int(values: Mapping[str, str], name: str, *, default: int = 0, maximum: int | None = None) -> int:
    raw = values.get(name)
    parsed = int(raw) if raw not in (None, "") else int(default)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return parsed


@dataclass(frozen=True)
class OperatorJobSettings:
    max_quota_units: int | None
    max_target_attempts: int
    chunk_size: int
    lease_seconds: int
    initial_inventory_pages_per_chunk: int = DEFAULT_INITIAL_INVENTORY_PAGES_PER_CHUNK
    initial_load_concurrency: int = 8


def load_operator_job_settings(
    environ: Mapping[str, str] | None = None,
    *,
    require_quota_cap: bool = True,
) -> OperatorJobSettings:
    values = os.environ if environ is None else environ
    settings = OperatorJobSettings(
        # Current-stats retains its quota budget. Initial jobs load the whole
        # accepted workset; chunk sizes and leases bound execution, not totals.
        max_quota_units=(
            _positive_int(values, "OPERATOR_JOB_MAX_QUOTA_UNITS")
            if require_quota_cap else None
        ),
        max_target_attempts=_positive_int(
            values,
            "OPERATOR_JOB_MAX_TARGET_ATTEMPTS",
            default=DEFAULT_MAX_TARGET_ATTEMPTS,
            maximum=20,
        ),
        chunk_size=_positive_int(
            values,
            "OPERATOR_JOB_CHUNK_SIZE",
            default=DEFAULT_CHUNK_SIZE,
            maximum=250,
        ),
        lease_seconds=_positive_int(
            values,
            "OPERATOR_JOB_LEASE_SECONDS",
            default=DEFAULT_LEASE_SECONDS,
            maximum=3600,
        ),
        initial_load_concurrency=min(8, _positive_int(
            values, 'LIC_YOUTUBE_REQUEST_CONCURRENCY', default=8, maximum=50,
        )),
        initial_inventory_pages_per_chunk=_positive_int(
            values,
            "OPERATOR_INITIAL_INVENTORY_PAGES_PER_CHUNK",
            default=DEFAULT_INITIAL_INVENTORY_PAGES_PER_CHUNK,
            maximum=10,
        ),
    )
    if settings.lease_seconds < 60:
        raise ValueError("OPERATOR_JOB_LEASE_SECONDS must be at least 60")
    return settings


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _all_format_initial_job(row: Mapping[str, Any]) -> bool:
    return row.get("initial_video_types") == ["long", "short", "stream"]


def operator_job_snapshot(row: Mapping[str, Any] | None, *, channel_id: str) -> dict[str, Any]:
    if not row:
        return {
            "job_id": None,
            "channel_id": channel_id,
            "status": "idle",
            "message": "No durable operator job has been created for this channel.",
            "started_at": None,
            "updated_at": None,
            "finished_at": None,
            "total": 0,
            "processed": 0,
            "progress_percent": 0,
            "updated": 0,
            "missing": 0,
            "unavailable_marked": 0,
            "skipped": 0,
            "errors": 0,
            "batches": 0,
            "quota_units_estimated": 0,
            "quota_units_reserved": 0,
            "quota_units_observed_estimate": 0,
            "error": None,
        }

    source_scope_expired = not source_is_current(row.get("requested_at"))
    if source_scope_expired:
        row = dict(row)
        row.update(
            summary_json={"source_scope_expired": True},
            resume_page_token=None,
            last_error_summary=(
                "Initial work completed; its copied source details expired."
                if row.get("status") == "completed"
                else "The original source scope expired. Start a new job to collect fresh data."
            ),
            last_error_code="source_scope_expired",
        )
        if row.get("status") != "completed":
            row["status"] = "error"
    total = _int(row.get("target_count"))
    processed = _int(row.get("processed_count"))
    status = str(row.get("status") or "unknown")
    job_type = str(row.get("job_type") or "")
    phase = str(row.get("phase") or "")
    all_formats = _all_format_initial_job(row)
    summary = row.get("summary_json") or {}
    full_initial = job_type == "channel_initial_long_inventory" and row.get("initial_scope_version") == 1
    if job_type == "channel_initial_long_inventory" and phase == "inventory":
        inventory_processed = _int(row.get("items_processed"))
        percent = 100 if status == "completed" else (
            min(99, round((inventory_processed / total) * 100)) if total else 0
        )
    else:
        percent = 100 if status == "completed" else (
            min(100, round((processed / total) * 100)) if total else 0
        )
    if job_type == "channel_initial_long_inventory":
        messages = {
            "queued": (
                f"Initial channel hydration queued in {phase or 'inventory'}; "
                f"{processed} of {total} videos resolved."
            ),
            "running": (
                f"Initial channel hydration is running in {phase or 'inventory'}; "
                f"{processed} of {total} videos resolved."
            ),
            "completed": (
                f"Initial channel checks completed for {total} videos across three formats."
                if all_formats else
                f"Initial channel hydration completed for {total} long-form videos."
            ),
            "blocked": (
                "Confirm full loading to continue this channel with more than 10,000 videos."
                if full_initial and row.get("last_error_code") == "initial_inventory_confirmation_required"
                else "Initial channel hydration requires review before continuing."
                if full_initial else "Initial channel hydration stopped before exceeding its confirmed safety cap."
            ),
            "suspended": "Initial channel hydration was suspended by drain or channel visibility control.",
            "error": (
                "The saved video list cannot be resumed safely. Start a new full channel load."
                if full_initial and row.get("last_error_code") == "initial_inventory_restart_required"
                else "Initial channel hydration exhausted its bounded retry policy."
            ),
        }
    else:
        messages = {
            "queued": f"Queued durable backfill: {processed} of {total} targets resolved.",
            "running": f"Processing durable backfill: {processed} of {total} targets resolved.",
            "completed": f"Durable backfill completed: {processed} of {total} targets resolved.",
            "blocked": "Durable backfill stopped at its configured safety budget.",
            "suspended": "Durable backfill was suspended by an operator drain action.",
            "error": "Durable backfill failed and requires operator review.",
        }
    return {
        "job_id": row.get("id"),
        "job_type": row.get("job_type"),
        "initial_video_types": row.get("initial_video_types") or ["long"],
        "initial_scope_version": row.get("initial_scope_version", 0),
        "targets_frozen_at": row.get("targets_frozen_at"),
        "scope": "all" if all_formats else "long",
        "inventory_video_type": row.get("inventory_video_type") or "long",
        "phase": phase or row.get("phase"),
        "channel_id": row.get("channel_id") or channel_id,
        "status": status,
        "message": messages.get(status, "Durable backfill status is unknown."),
        "started_at": row.get("started_at") or row.get("requested_at"),
        "requested_at": row.get("requested_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
        "total": total,
        "processed": processed,
        "progress_percent": None if full_initial and not row.get("targets_frozen_at") else percent,
        "updated": _int(row.get("updated_count")),
        "missing": _int(row.get("unavailable_count")),
        "unavailable_marked": _int(row.get("unavailable_count")),
        "skipped": _int(row.get("skipped_count")),
        "checked": _int(summary.get("checked_count")),
        "errors": _int(row.get("error_count")),
        "batches": _int(row.get("quota_units_observed_estimate")),
        "quota_units_estimated": _int(row.get("quota_units_estimated")),
        "quota_units_reserved": _int(row.get("quota_units_reserved")),
        "quota_units_observed_estimate": _int(row.get("quota_units_observed_estimate")),
        "max_items": None if full_initial else _int(row.get("max_items")),
        "max_pages": None if full_initial else _int(row.get("max_pages")),
        "max_quota_units": None if full_initial else _int(row.get("max_quota_units")),
        "quota_limit_enforced": job_type != "channel_initial_long_inventory",
        "pages_processed": _int(row.get("pages_processed")),
        "items_processed": _int(row.get("items_processed")),
        "inventory_items": total if full_initial else _int(row.get("items_processed")),
        "inventory_inserted": _int(row.get("inventory_inserted_count")),
        "inventory_updated": _int(row.get("inventory_updated_count")),
        "has_resume_page_token": bool(row.get("resume_page_token")),
        "current_profile_ready": (
            not source_scope_expired
            and job_type == "channel_initial_long_inventory"
            and status == "completed"
            and phase == "verify"
            and _int(row.get("error_count")) == 0
            and (not all_formats or summary.get("current_profile_ready") is True)
            and (not full_initial or bool(row.get("targets_frozen_at")))
        ),
        "error": row.get("last_error_summary"),
        "error_code": row.get("last_error_code"),
        "summary": summary,
        **({"source_expires_at": row["source_expires_at"]} if row.get("source_expires_at") else {}),
    }


COLLECTION_STOP_FLAGS = (
    "settlement_unknown",
    "infrastructure_error",
    "quota_exhausted",
    "general_quota_exhausted",
    "batch_quota_exhausted",
    # A failed Batch counter request: the drain stops like on quota.
    "batch_unavailable",
    "rate_limited",
)


def operator_collection_stop_reason(summary: Mapping[str, Any]) -> str | None:
    """Return only bounded control reasons, never provider/database text."""
    for key in COLLECTION_STOP_FLAGS:
        if summary.get(key):
            return key
    return None


def _is_daily_quota_error(error: BaseException) -> bool:
    from youtube_errors import is_youtube_daily_quota_exhausted
    from youtube_batch_stats import is_batch_stats_quota_error
    return is_youtube_daily_quota_exhausted(error, is_batch_stats_quota_error=is_batch_stats_quota_error)


def _collection_stop_flags(summary: Mapping[str, Any]) -> dict[str, bool]:
    return {key: bool(summary.get(key)) for key in COLLECTION_STOP_FLAGS}


def _observed_collection_units(summary: Mapping[str, Any]) -> int:
    # A logical full-profile batch can attempt one General metadata request
    # and one Batch statistics request. Neither batches nor fallback_requests
    # represents that split, especially on partial provider failures.
    return max(_int(summary.get("metadata_requests")), 0) + max(
        _int(summary.get("batch_requests")), 0
    )


def compact_operator_chunk_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Keep aggregate allowlisted fields; never persist raw exception messages."""
    keys = (
        "requested",
        "updated",
        "batches",
        "errors",
        "missing",
        "unavailable_marked",
        "metadata_refresh_requested",
        "metadata_deferred",
        "metadata_requests",
        "batch_requests",
        "fallback_requests",
    )
    return {
        **{key: _int(summary.get(key)) for key in keys},
        **_collection_stop_flags(summary),
    }


def compact_initial_inventory_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Persist bounded counters, labels and definitely unstarted target IDs."""
    result = {
        "phase": str(summary.get("phase") or "")[:32],
        "video_type": str(summary.get("video_type") or "")[:16],
        "outcome": str(summary.get("outcome") or "")[:64],
        "pages": _int(summary.get("pages")),
        "items": _int(summary.get("items")),
        "inserted": _int(summary.get("inserted")),
        "updated": _int(summary.get("updated")),
        "initial_stats_imported": _int(summary.get("initial_stats_imported")),
        "initial_stats_batches": _int(summary.get("initial_stats_batches")),
        "hydration_requested": _int(summary.get("hydration_requested")),
        "hydration_updated": _int(summary.get("hydration_updated")),
        "hydration_errors": _int(summary.get("hydration_errors")),
        "hydration_batches": _int(summary.get("hydration_batches")),
        "metadata_deferred": _int(summary.get("metadata_deferred")),
        "metadata_requests": _int(summary.get("metadata_requests")),
        "batch_requests": _int(summary.get("batch_requests")),
        **_collection_stop_flags(summary),
    }
    if 'hydration_unstarted_video_ids' in summary:
        unstarted = summary['hydration_unstarted_video_ids']
        if (not isinstance(unstarted, (list, tuple)) or len(unstarted) > 400
                or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in unstarted)
                or len(set(unstarted)) != len(unstarted)):
            raise ValueError('initial_hydration_unstarted_ids_invalid')
        result['hydration_unstarted_video_ids'] = list(unstarted)
    return result


def operator_worker_id() -> str:
    return f"{socket.gethostname().strip() or 'worker'}:{os.getpid()}"


def _repository_context(
    repository: OperatorJobRepository,
    fresh_repository_factory: (
        Callable[[], AbstractContextManager[OperatorJobRepository]] | None
    ),
) -> AbstractContextManager[OperatorJobRepository]:
    if fresh_repository_factory is None:
        return nullcontext(repository)
    return fresh_repository_factory()


def _log_operator_cleanup_failure(
    *,
    job_id: str,
    phase: str,
    action: str,
    primary_error: Exception,
    cleanup_error: Exception,
) -> None:
    print(
        "[operator-jobs] cleanup failed "
        f"job_id={job_id} phase={phase} action={action} "
        f"primary_error_type={primary_error.__class__.__name__} "
        f"cleanup_error_type={cleanup_error.__class__.__name__}",
        flush=True,
    )


def _release_operator_job_after_error(
    repository: OperatorJobRepository,
    *,
    fresh_repository_factory: (
        Callable[[], AbstractContextManager[OperatorJobRepository]] | None
    ),
    job_id: str,
    lease_token: str,
    phase: str,
    error: Exception,
    error_code_prefix: str = "initial_hydration_",
) -> None:
    from db.hot.hot_collection_control import admission_denial
    denied = admission_denial(error)
    if denied is not None and (getattr(denied, 'control_abort_attempted', False)
                                   or getattr(denied, 'outcome_may_be_committed', False)):
        return  # Includes unknown abort ACK; never submit it a second time.
    try:
        with _repository_context(repository, fresh_repository_factory) as release_repository:
            release_repository.release(
                job_id,
                lease_token,
                error_code=error_code_prefix + error.__class__.__name__.lower(),
            )
    except Exception as cleanup_error:
        _log_operator_cleanup_failure(
            job_id=job_id,
            phase=phase,
            action="release",
            primary_error=error,
            cleanup_error=cleanup_error,
        )


def _reconcile_initial_hydration_verification(
    repository: OperatorJobRepository,
    *,
    fresh_repository_factory: (
        Callable[[], AbstractContextManager[OperatorJobRepository]] | None
    ),
    claimed: Mapping[str, Any],
) -> dict[str, Any] | None:
    channel_id = str(claimed.get("channel_id") or "")
    job_id = str(claimed.get("id") or "")
    if not channel_id or not job_id:
        return None
    with _repository_context(repository, fresh_repository_factory) as status_repository:
        current = status_repository.get_initial_inventory_job(channel_id)
    if not current or str(current.get("id") or "") != job_id:
        return None
    status = str(current.get("status") or "")
    phase = str(current.get("phase") or "")
    if status in {"completed", "error", "blocked", "suspended"}:
        return current
    if status == "queued" and phase in {"verify", "hydrate_videos"}:
        return current
    return None


def _verify_initial_hydration_with_retry(
    repository: OperatorJobRepository,
    claimed: Mapping[str, Any],
    *,
    fresh_repository_factory: (
        Callable[[], AbstractContextManager[OperatorJobRepository]] | None
    ),
) -> dict[str, Any]:
    job_id = str(claimed.get("id") or "")
    lease_token = str(claimed.get("lease_token") or "")
    transport_error: DatabaseUnavailable | None = None

    from db.hot.hot_collection_control import uncertain_collection_ack
    for attempt in range(1, VERIFY_TRANSPORT_ATTEMPTS + 1):
        try:
            replay_scope = uncertain_collection_ack() if attempt > 1 else nullcontext()
            with replay_scope:
                context = (
                    nullcontext(repository)
                    if attempt == 1
                    else _repository_context(repository, fresh_repository_factory)
                )
                with context as verify_repository:
                    return verify_repository.verify_initial_hydration(job_id, lease_token)
        except DatabaseUnavailable as error:
            transport_error = error
            print(
                "[operator-jobs] verify transport retry "
                f"job_id={job_id} attempt={attempt} "
                f"error_type={error.__class__.__name__}",
                flush=True,
            )
        except Exception as error:
            release_scope = uncertain_collection_ack() if transport_error is not None else nullcontext()
            with release_scope:
                _release_operator_job_after_error(
                    repository,
                    fresh_repository_factory=fresh_repository_factory,
                    job_id=job_id,
                    lease_token=lease_token,
                    phase="verify",
                    error=error,
                )
            raise

    if transport_error is None:  # pragma: no cover - loop contract guard
        raise RuntimeError("initial hydration verify retry contract failed")

    try:
        reconciled = _reconcile_initial_hydration_verification(
            repository,
            fresh_repository_factory=fresh_repository_factory,
            claimed=claimed,
        )
    except Exception as reconciliation_error:
        print(
            "[operator-jobs] verify reconciliation failed "
            f"job_id={job_id} primary_error_type={transport_error.__class__.__name__} "
            f"reconciliation_error_type={reconciliation_error.__class__.__name__}",
            flush=True,
        )
    else:
        if reconciled is not None:
            return reconciled

    with uncertain_collection_ack():
        _release_operator_job_after_error(
            repository,
            fresh_repository_factory=fresh_repository_factory,
            job_id=job_id,
            lease_token=lease_token,
            phase="verify",
            error=transport_error,
        )
    raise transport_error


def _run_initial_full_hydration_chunk(
    repository: OperatorJobRepository,
    run_video_collection: Callable[..., Mapping[str, Any]],
    claimed: Mapping[str, Any],
    *,
    settings: OperatorJobSettings,
    fresh_repository_factory: (
        Callable[[], AbstractContextManager[OperatorJobRepository]] | None
    ) = None,
) -> dict[str, Any]:
    """Run one bounded target or verification phase for initial hydration."""
    job_id = str(claimed.get("id") or "")
    lease_token = str(claimed.get("lease_token") or "")
    phase = str(claimed.get("phase") or "")
    if phase == "verify":
        verified = _verify_initial_hydration_with_retry(
            repository,
            claimed,
            fresh_repository_factory=fresh_repository_factory,
        )
        return {
            "status": str(verified.get("status") or "unknown"),
            "job": verified,
            "processed_video_ids": 0,
            "processed_pages": 0,
        }

    prepared = False
    settlement_attempted = False
    try:
        hydration = repository.prepare_initial_hydration_chunk(
            job_id,
            lease_token,
            limit=(50 * settings.initial_load_concurrency
                   if claimed.get("initial_scope_version") == 1 else settings.chunk_size),
            lease_seconds=settings.lease_seconds,
        )
        prepared = True
        video_ids = hydration["video_ids"]
        if not video_ids:
            job = hydration["job"]
            return {
                "status": str(job.get("status") or "unknown"),
                "job": job,
                "processed_video_ids": 0,
                "processed_pages": 0,
                "deferred": hydration.get("deferred", False),
            }

        collection_options = {
            "limit": len(video_ids),
            "explicit_video_ids": video_ids,
            "reason": "initial_channel_hydration",
            "force_metadata": True,
        }
        if claimed.get("initial_scope_version") == 1:
            collection_options['initial_load_concurrency'] = settings.initial_load_concurrency
            collection_options["initial_job"] = {
                key: claimed[key] for key in ("id", "channel_id", "lease_owner", "lease_token")
            }
        else:
            # Legacy accepted scopes retain split providers and their chunk cap;
            # carry the parent generation instead of capturing new authority.
            collection_options["channel_scopes"] = [{
                "channel_id": claimed["channel_id"],
                "control_epoch": claimed["lease_channel_epoch"],
            }]
        sync_summary = dict(run_video_collection(**collection_options) or {})
        observed_units = _observed_collection_units(sync_summary)
        stop_reason = operator_collection_stop_reason(sync_summary)
        # This job RPC reconciles only durable source clocks and unavailable
        # evidence. It never fails/releases the uncertain per-video lease.
        settlement_attempted = True
        settled = repository.settle_initial_hydration_chunk(
            job_id,
            lease_token,
            observed_quota_units=observed_units,
            error_code=(
                "initial_hydration_" + stop_reason
                if stop_reason else (
                    "initial_hydration_video_collection_error"
                    if _int(sync_summary.get("errors")) else None
                )
            ),
            summary=compact_initial_inventory_summary({
                "phase": "hydrate_videos",
                "outcome": "video_chunk_stopped" if stop_reason else "video_chunk_settled",
                "hydration_requested": sync_summary.get("requested"),
                "hydration_unstarted_video_ids": sync_summary.get("unstarted_video_ids", []),
                "hydration_updated": sync_summary.get("updated"),
                "hydration_errors": sync_summary.get("errors"),
                "hydration_batches": sync_summary.get("batches"),
                "metadata_deferred": sync_summary.get("metadata_deferred"),
                "metadata_requests": sync_summary.get("metadata_requests"),
                "batch_requests": sync_summary.get("batch_requests"),
                **_collection_stop_flags(sync_summary),
            }),
        )
        return {
            "status": str(settled.get("status") or "unknown"),
            "job": settled,
            "processed_video_ids": len(video_ids),
            "processed_pages": 0,
            "summary": compact_operator_chunk_summary(sync_summary),
            **_collection_stop_flags(sync_summary),
        }
    except Exception as error:
        from db.hot.hot_collection_control import admission_denial
        denied = admission_denial(error)
        if denied is not None and (getattr(denied, 'control_abort_attempted', False)
                                   or getattr(denied, 'outcome_may_be_committed', False)):
            raise
        if settlement_attempted:
            # The job settlement itself may have committed. Do not replay it
            # with a different payload or clear an uncertain lease. Expiry and
            # the next preparation reconcile existing durable evidence.
            raise
        error_code = "initial_hydration_" + error.__class__.__name__.lower()
        try:
            with _repository_context(
                repository,
                fresh_repository_factory,
            ) as cleanup_repository:
                if prepared:
                    cleanup_repository.settle_initial_hydration_chunk(
                        job_id,
                        lease_token,
                        observed_quota_units=0,
                        error_code=error_code,
                        summary=compact_initial_inventory_summary({
                            "phase": "hydrate_videos",
                            "outcome": "component_error",
                            "hydration_errors": 1,
                            "infrastructure_error": True,
                            "settlement_unknown": True,
                        }),
                    )
                else:
                    cleanup_repository.release(
                        job_id,
                        lease_token,
                        error_code=error_code,
                    )
        except Exception as cleanup_error:
            _log_operator_cleanup_failure(
                job_id=job_id,
                phase=phase,
                action="settle" if prepared else "release",
                primary_error=error,
                cleanup_error=cleanup_error,
            )
        raise


def _run_initial_inventory_page(repository, claimed, prepared, run_inventory):
    """Bridge job checkpoints to one shared page or source-free reconciliation.

    Once preparation has returned, exceptions retain its reservation/checkpoint.
    Only a confirmed page, failure receipt or unused preparation may settle it.
    """
    from uuid import UUID
    from channel_collection_scope import require_control_epoch

    job = prepared["job"]
    job_id, lease_token = str(claimed["id"]), str(claimed["lease_token"])
    channel_id = str(job.get("channel_id") or "")
    if (str(job.get("id") or "") != job_id or channel_id != claimed.get("channel_id")
            or job.get("lease_channel_epoch") != claimed.get("lease_channel_epoch")):
        raise RuntimeError("initial_inventory_job_identity_mismatch")
    expected_epoch = require_control_epoch(job.get("lease_channel_epoch"))
    video_type = prepared.get("video_type")
    if video_type not in {"long", "short", "stream"}:
        raise ValueError("initial_inventory_video_type_invalid")
    action = prepared.get("action")

    def response(settled=job, *, pages=0, **flags):
        return {"status": str(settled.get("status") or "unknown"), "job": settled,
                "processed_video_ids": 0, "processed_pages": pages, **flags}

    if action == "await_receipt":
        return response(settlement_unknown=True, stopped_reason="inventory_outcome_unknown")
    if action in {"reconcile_failure", "reconcile_unknown"}:
        evidence = prepared.get("evidence") or {}
        code = ("initial_inventory_outcome_unknown" if action == "reconcile_unknown"
                else evidence.get("error_code"))
        if not isinstance(code, str) or not code:
            raise RuntimeError("initial_inventory_failure_evidence_missing")
        settled = repository.fail_initial_inventory(job_id, lease_token, error_code=code)
        quota = code == "initial_inventory_quota_exhausted"
        return response(settled, quota_exhausted=quota,
                        **({"stopped_reason": "youtube_quota_exhausted"} if quota else {}))
    if action not in {"inventory", "reconcile_inventory"}:
        raise RuntimeError("initial_inventory_prepare_action_invalid")
    transition_id = prepared.get("transition_id")
    if not isinstance(transition_id, str) or str(UUID(transition_id)) != transition_id:
        raise ValueError("initial_inventory_transition_invalid")
    reserved = prepared.get("reserved_units")
    page_limit = prepared.get("page_limit")
    if type(reserved) is not int or reserved not in (0, 1):
        raise ValueError("initial_inventory_reservation_invalid")

    def settle(inventory):
        values = {}
        for name, maximum in (("pages", 1), ("items", 50), ("inserted", 50), ("updated", 50)):
            value = inventory.get(name, 0)
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("initial_inventory_receipt_count_invalid")
            values[name] = value
        settled = repository.settle_initial_inventory_chunk(
            job_id, lease_token, reserved_units=reserved,
            pages_processed=values["pages"], items_processed=values["items"],
            inserted=values["inserted"], updated=values["updated"],
            next_page_token=None, summary={"transition_id": transition_id},
        )
        return response(settled, pages=values["pages"])

    if action == "reconcile_inventory":
        evidence = prepared.get("evidence")
        if not isinstance(evidence, Mapping) or evidence.get("transition_id") != transition_id:
            raise RuntimeError("initial_inventory_receipt_evidence_missing")
        return settle({"pages": evidence["pages_processed"], "items": evidence["items_processed"],
                       "inserted": evidence["inserted"], "updated": evidence["updated"]})
    if type(page_limit) is not int or page_limit not in (0, 1):
        raise ValueError("initial_inventory_page_limit_invalid")
    if not page_limit:
        return settle({"pages": 0, "items": 0, "inserted": 0, "updated": 0})
    if reserved != 1 or prepared.get("resume_page_token") is not None:
        raise ValueError("initial_inventory_prepared_dispatch_invalid")

    def before_dispatch(claim, scan, planned_transition):
        if (claim.channel_id != channel_id or claim.video_type != video_type
                or claim.lease_channel_epoch != expected_epoch or planned_transition != transition_id):
            raise RuntimeError("initial_inventory_binding_identity_mismatch")
        bound = repository.bind_initial_inventory_scan(
            job_id, lease_token, playlist_lease_token=claim.lease_token,
            scan_id=scan["scan_id"], transition_id=transition_id,
        )
        if (bound.get("status") != "bound" or str(bound.get("job_id")) != job_id
                or bound.get("scan_id") != scan["scan_id"]
                or bound.get("transition_id") != transition_id
                or type(bound.get("replayed")) is not bool
                or type(bound.get("dispatch_allowed")) is not bool
                or (bound["replayed"] and bound["dispatch_allowed"])):
            raise RuntimeError("initial_inventory_binding_receipt_invalid")
        return bound["dispatch_allowed"]

    inventory = dict(run_inventory(
        channel_id, expected_epoch=expected_epoch, max_pages=1, page_token=None,
        video_type=video_type, transition_id=transition_id, before_dispatch=before_dispatch,
        **({"initial_job_id": job_id, "initial_job_lease_token": lease_token}
           if job.get("initial_scope_version") == 1 else {}),
    ) or {})
    if inventory.get("settlement_unknown"):
        return response(settlement_unknown=True, stopped_reason="inventory_outcome_unknown",
                        quota_exhausted=bool(inventory.get("quota_exhausted")),
                        infrastructure_error=bool(inventory.get("infrastructure_error")),
                        hot_runtime_interrupted=bool(inventory.get("hot_runtime_interrupted")))
    if inventory.get("hot_runtime_interrupted"):
        from db.hot.hot_collection_control import HotCollectionControlRepository
        # The child write was definitely refused. Release only this parent's
        # token, leaving its checkpoint intact for the existing receipt recovery.
        # A missing abort ACK is not retried and never authorizes page replay.
        try:
            receipt = HotCollectionControlRepository(repository.database).abort(
                operators=[{"job_id": job_id, "lease_token": lease_token}])
        except Exception:
            return response(settlement_unknown=True, infrastructure_error=True,
                            hot_runtime_interrupted=True, stopped_reason="hot_runtime_interrupted")
        return {**response(infrastructure_error=True, hot_runtime_interrupted=True,
                           stopped_reason="hot_runtime_interrupted"),
                "status": "interrupted", "parent_released": receipt["operator_released"] == 1}
    outcome = inventory.get("outcome")
    if outcome in {"deferred", "quota_deferred", "error"}:
        code = ("initial_inventory_deferred" if outcome == "deferred" else
                "initial_inventory_quota_exhausted" if outcome == "quota_deferred" else
                inventory.get("error_code"))
        if not isinstance(code, str) or not code:
            raise RuntimeError("initial_inventory_failure_evidence_missing")
        settled = repository.fail_initial_inventory(job_id, lease_token, error_code=code)
        return response(settled, deferred=outcome == "deferred", quota_exhausted=outcome == "quota_deferred",
                        **({"rate_limited": True, "stopped_reason": "provider_rate_limited"}
                           if code == "provider_rate_limited" else
                           {"stopped_reason": "youtube_quota_exhausted"} if outcome == "quota_deferred" else {}))
    if outcome not in {"running", "completed", "playlist_not_found"}:
        raise RuntimeError("initial_inventory_page_outcome_invalid")
    return settle(inventory)


def run_one_operator_job_chunk(
    repository: OperatorJobRepository,
    run_video_collection: Callable[..., Mapping[str, Any]],
    *,
    settings: OperatorJobSettings,
    worker_id: str | None = None,
    run_initial_inventory_chunk: Callable[..., Mapping[str, Any]] | None = None,
    run_initial_stats: Callable[[str], Mapping[str, Any]] | None = None,
    target_channel_id: str | None = None,
    fresh_repository_factory: (
        Callable[[], AbstractContextManager[OperatorJobRepository]] | None
    ) = None,
    claimed_job: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim one durable job and process one bounded type-specific chunk."""
    resolved_worker_id = worker_id or operator_worker_id()
    if claimed_job is not None:
        claimed = dict(claimed_job)
    elif target_channel_id:
        claimed = repository.claim_initial_hydration(
            target_channel_id,
            resolved_worker_id,
            lease_seconds=settings.lease_seconds,
        )
    else:
        claimed = repository.claim(
            resolved_worker_id,
            lease_seconds=settings.lease_seconds,
        )
    if not claimed:
        return {"status": "idle", "job": None, "processed_video_ids": 0}

    job_id = str(claimed.get("id") or "")
    lease_token = str(claimed.get("lease_token") or "")
    if not job_id or not lease_token:
        raise ValueError("claimed operator job is missing its job or lease token")

    # Schema v1 rows/test doubles predate the discriminator; preserving the
    # original current-stats behavior keeps staged code-before-migration safe.
    job_type = str(claimed.get("job_type") or "channel_current_stats_backfill")
    if job_type == "channel_initial_long_inventory":
        if str(claimed.get("phase") or "") in {"hydrate_videos", "verify"}:
            return _run_initial_full_hydration_chunk(
                repository,
                run_video_collection,
                claimed,
                settings=settings,
                fresh_repository_factory=fresh_repository_factory,
            )
        if run_initial_inventory_chunk is None or run_initial_stats is None:
            try:
                repository.fail_initial_inventory(
                    job_id,
                    lease_token,
                    error_code="initial_inventory_worker_not_configured",
                )
            except Exception:
                pass
            raise ValueError("initial inventory worker callables are required")
        settlement_attempted = False
        preparation_unknown = True
        try:
            prepared = repository.prepare_initial_inventory_chunk(
                job_id,
                lease_token,
                page_limit=1,
                lease_seconds=settings.lease_seconds,
            )
            preparation_unknown = False
            job = prepared["job"]
            action = str(prepared.get("action") or "blocked")
            if action == "blocked":
                return {
                    "status": str(job.get("status") or "blocked"),
                    "job": job,
                    "processed_video_ids": 0,
                    "processed_pages": 0,
                }

            if action == "targets_reconciled":
                settlement_attempted = True
                # SQL already repaired a bounded target subset or froze the
                # workset. This is not a playlist receipt and sends no API.
                if (job.get("initial_scope_version") != 1
                        or str(job.get("id") or "") != job_id
                        or job.get("channel_id") != claimed.get("channel_id")
                        or job.get("accepted_epoch") != claimed.get("accepted_epoch")
                        or job.get("status") not in {"queued", "blocked"}
                        or job.get("phase") not in {"inventory", "hydrate_videos"}
                        or (job.get("phase") == "hydrate_videos" and not job.get("targets_frozen_at"))
                        or job.get("lease_token") is not None
                        or type(prepared.get("reserved_units")) is not int
                        or prepared["reserved_units"] != 0
                        or type(prepared.get("page_limit")) is not int
                        or prepared["page_limit"] != 0
                        or prepared.get("transition_id") is not None):
                    raise RuntimeError("initial_inventory_target_reconciliation_invalid")
                return {"status": job["status"], "job": job,
                        "processed_video_ids": 0, "processed_pages": 0}

            channel_id = str(job.get("channel_id") or "")
            reserved_units = _int(prepared.get("reserved_units"))
            if action == "initial_stats":
                stats_result = dict(run_initial_stats(channel_id, expected_epoch=job["lease_channel_epoch"]) or {})
                sync_summary = dict(stats_result.get("sync_summary") or {})
                for key in COLLECTION_STOP_FLAGS:
                    sync_summary[key] = bool(sync_summary.get(key) or stats_result.get(key))
                observed_units = _observed_collection_units(sync_summary)
                stop_reason = operator_collection_stop_reason(sync_summary)
                if sync_summary.get("settlement_unknown"):
                    # Unlike full hydration, this retained legacy phase has
                    # no frozen-target source-clock settlement. Leave its
                    # reservation/lease intact for recovery, not a false
                    # completed or retry-limit outcome from an unknown ACK.
                    return {
                        "status": str(job.get("status") or "running"),
                        "job": job,
                        "processed_video_ids": 0,
                        "processed_pages": 0,
                        "summary": compact_operator_chunk_summary(sync_summary),
                        **_collection_stop_flags(sync_summary),
                    }
                settlement_attempted = True
                settled = repository.settle_initial_stats(
                    job_id,
                    lease_token,
                    reserved_units=reserved_units,
                    observed_quota_units=observed_units,
                    status=(
                        # The job waits for the quota reset without spending an attempt.
                        "quota_deferred" if stop_reason and stop_reason.endswith("quota_exhausted")
                        else "rate_limited" if stop_reason == "rate_limited"
                        else "error" if stop_reason
                        else str(stats_result.get("status") or "error")
                    ),
                    summary=compact_initial_inventory_summary({
                        "phase": "initial_stats",
                        "outcome": "collection_stopped" if stop_reason else stats_result.get("status"),
                        "initial_stats_imported": stats_result.get("imported"),
                        "initial_stats_batches": sync_summary.get("batches"),
                        "metadata_requests": sync_summary.get("metadata_requests"),
                        "batch_requests": sync_summary.get("batch_requests"),
                        **_collection_stop_flags(sync_summary),
                    }),
                )
                return {
                    "status": str(settled.get("status") or "unknown"),
                    "job": settled,
                    "processed_video_ids": 0,
                    "processed_pages": 0,
                    "summary": compact_operator_chunk_summary(sync_summary),
                    **_collection_stop_flags(sync_summary),
                }

            # From here every mutation is receipt-backed. A lost ACK stays
            # available to prepare/reconcile, never ordinary failure cleanup.
            settlement_attempted = True
            return _run_initial_inventory_page(
                repository, claimed, prepared, run_initial_inventory_chunk,
            )
        except Exception as error:
            from db.hot.hot_collection_control import admission_denial
            denied = admission_denial(error)
            if denied is not None and (getattr(denied, 'control_abort_attempted', False)
                                   or getattr(denied, 'outcome_may_be_committed', False)):
                raise
            if settlement_attempted or preparation_unknown:
                raise
            error_code = (
                "long_playlist_not_found"
                if error.__class__.__name__ == "YouTubePlaylistNotFoundError"
                # The job waits for the quota reset without spending an attempt.
                else "initial_inventory_quota_exhausted"
                if _is_daily_quota_error(error)
                else "provider_rate_limited" if getattr(error, "code", None) == "provider_rate_limited"
                else "initial_inventory_" + error.__class__.__name__.lower()
            )
            try:
                with _repository_context(
                    repository,
                    fresh_repository_factory,
                ) as cleanup_repository:
                    failed = cleanup_repository.fail_initial_inventory(
                        job_id,
                        lease_token,
                        error_code=error_code,
                        playlist_not_found=error_code == "long_playlist_not_found",
                    )
                if error_code == "long_playlist_not_found":
                    return {
                        "status": str(failed.get("status") or "completed"),
                        "job": failed,
                        "processed_video_ids": 0,
                        "processed_pages": 0,
                    }
            except Exception as cleanup_error:
                _log_operator_cleanup_failure(
                    job_id=job_id,
                    phase=str(claimed.get("phase") or "inventory"),
                    action="fail",
                    primary_error=error,
                    cleanup_error=cleanup_error,
                )
            raise

    if job_type != "channel_current_stats_backfill":
        try:
            repository.release(
                job_id,
                lease_token,
                error_code="operator_worker_unknown_job_type",
            )
        except Exception:
            pass
        raise ValueError("claimed operator job has an unsupported job type")

    settlement_attempted = False
    try:
        prepared = repository.prepare_chunk(
            job_id,
            lease_token,
            limit=settings.chunk_size,
            lease_seconds=settings.lease_seconds,
        )
        video_ids = prepared["video_ids"]
        if not video_ids:
            job = prepared["job"]
            return {
                "status": str(job.get("status") or "idle"),
                "job": job,
                "processed_video_ids": 0,
                "deferred": prepared.get("deferred", False),
            }

        summary = dict(
            run_video_collection(
                limit=len(video_ids),
                explicit_video_ids=video_ids,
                reason="manual_channel_backfill",
                force_metadata=True,
            )
            or {}
        )
        stop_reason = operator_collection_stop_reason(summary)
        error_code = (
            "video_collection_" + stop_reason if stop_reason else (
                "video_collection_chunk_error" if _int(summary.get("errors")) else None
            )
        )
        settlement_attempted = True
        settled = repository.settle_chunk(
            job_id,
            lease_token,
            observed_quota_units=_observed_collection_units(summary),
            error_code=error_code,
            summary=compact_operator_chunk_summary(summary),
        )
        return {
            "status": str(settled.get("status") or "unknown"),
            "job": settled,
            "processed_video_ids": len(video_ids),
            "summary": compact_operator_chunk_summary(summary),
            **_collection_stop_flags(summary),
        }
    except Exception as error:
        from db.hot.hot_collection_control import admission_denial
        denied = admission_denial(error)
        if denied is not None and (getattr(denied, 'control_abort_attempted', False)
                                   or getattr(denied, 'outcome_may_be_committed', False)):
            raise
        if settlement_attempted:
            raise
        _release_operator_job_after_error(
            repository,
            fresh_repository_factory=fresh_repository_factory,
            job_id=job_id,
            lease_token=lease_token,
            phase="current_stats",
            error=error,
            error_code_prefix="operator_worker_",
        )
        raise


def operator_dispatch_turn(
    repository, *, inventory_repository, video_service, fetch_page, settings,
    concurrency, max_chunks, max_seconds, require_ready, should_stop,
    legacy_chunk, coordinator, clock=None, reconnect_factory=None, on_stop=None,
):
    """Borrow one Coordinator for a resumable, finite operator service window.

    Inventory pages each consume one old chunk credit. A hydration window still
    consumes one credit, split into <=50-ID descriptors on the shared executor.
    Parent admission does not occupy a network credit, including concurrency=1.
    No failed claim, settlement or final parent write is called a second time.
    """
    import time
    from collections import deque
    from dataclasses import replace
    from collection.budgets import PageBudget
    from collection.dispatch import Coordinator, Skipped, Work
    from collection.initial_video_hydration import InitialBatchUnit
    from collection.inventory_dispatch import InventoryPageReservations, InventoryPageUnit
    from db.inventory.typed_inventory import TypedInventoryClaim
    from youtube_errors import YouTubePlaylistNotFoundError

    if (type(concurrency) is not int or not 1 <= concurrency <= 50
            or type(max_chunks) is not int or not 1 <= max_chunks <= 512
            or type(max_seconds) is not int or not 1 <= max_seconds <= 1200
            or not all(callable(v) for v in (require_ready, should_stop, legacy_chunk))):
        raise ValueError('operator_dispatch_config_invalid')
    clock = clock or time.monotonic
    if not isinstance(coordinator, Coordinator) or coordinator.concurrency != concurrency:
        raise ValueError('operator_dispatch_coordinator_mismatch')
    from threading import get_ident
    owner_thread = get_ident()
    worker = operator_worker_id()
    started = clock()
    active, admissions, descriptors = {}, {}, {}
    after = ceiling = None
    exhausted = False
    reserved = processed = total_pages = total_videos = 0
    stop_reason = None
    flags = {name: False for name in COLLECTION_STOP_FLAGS}
    peak_ready = peak_held = 0
    sweep_progress = 0
    reconnect_keys = set()
    reconnect_progress = 0
    reconnect_turn = True
    reconnect = None
    finished = False

    def stop(reason, **changes):
        nonlocal stop_reason
        stop_reason = stop_reason or reason
        for name, value in changes.items():
            if name in flags:
                flags[name] = flags[name] or bool(value)
        if on_stop is not None and any(flags.values()):
            on_stop(dict(flags, stopped_reason=stop_reason))

    def absorb(parent, summary):
        for name in ('updated', 'errors', 'batches', 'metadata_requests', 'batch_requests', 'metadata_deferred'):
            parent['summary'][name] = parent['summary'].get(name, 0) + _int(summary.get(name))
        for name in COLLECTION_STOP_FLAGS:
            parent['summary'][name] = bool(parent['summary'].get(name) or summary.get(name))
        reason = operator_collection_stop_reason(summary)
        if reason:
            parent['summary']['stopped_reason'] = reason
            stop(reason, **_collection_stop_flags(summary))

    if reconnect_factory is not None:
        reconnect = reconnect_factory(
            on_settled=lambda summary: absorb({'summary': {}}, summary),
            should_stop=lambda: bool(stop_reason) or should_stop())

    def finalize(parent, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception:
            parent['finalization_failed'] = True
            stop('settlement_unknown', settlement_unknown=True, infrastructure_error=True)
            return None

    def admit(candidate, key):
        nonlocal reserved, sweep_progress
        if stop_reason or should_stop():
            admissions.pop(key)
            reserved -= 1
            return Skipped()
        require_ready()
        claim_started = clock()
        job = repository.claim_id(candidate['job_id'], candidate['channel_id'],
            candidate['accepted_epoch'], worker, lease_seconds=settings.lease_seconds)
        if job is None:
            admissions.pop(key)
            reserved -= 1
            return Skipped()
        if (str(job.get('id')) != candidate['job_id'] or job.get('channel_id') != candidate['channel_id']
                or job.get('accepted_epoch') != candidate['accepted_epoch']):
            raise RuntimeError('operator_dispatch_claim_mismatch')
        parent = dict(job=job, pending=deque(), units={}, summary={}, credit=1,
                      selected=[], settled_ids=set(), done=False, legacy=False,
                      deadline=claim_started + settings.lease_seconds - 2)
        active[candidate['job_id']] = parent
        admissions.pop(key)
        sweep_progress += 1
        if (not _all_format_initial_job(job) or job.get('initial_scope_version') != 1
                or job.get('job_type') != 'channel_initial_long_inventory'):
            parent['legacy'] = True
            return Skipped()
        phase = job.get('phase')
        if phase == 'inventory':
            reply = repository.initialize_inventory_chains(job['id'], job['lease_token'])
            if reply.get('status') != 'initialized' or reply.get('chain_count') != 3:
                raise RuntimeError('operator_chain_initialization_unconfirmed')
            parent['pending'].extend(('long', 'short', 'stream'))
        elif phase == 'hydrate_videos':
            prepared = repository.prepare_initial_hydration_chunk(job['id'], job['lease_token'],
                limit=50 * settings.initial_load_concurrency, lease_seconds=settings.lease_seconds)
            ids = prepared['video_ids']
            if (not isinstance(ids, list) or len(ids) > 50 * settings.initial_load_concurrency
                    or len(set(ids)) != len(ids)):
                raise RuntimeError('operator_hydration_window_invalid')
            parent['selected'] = ids
            parent['pending'].extend(tuple(ids[i:i + 50]) for i in range(0, len(ids), 50))
            if not ids:
                parent['done'] = True  # The preparation owner already released/finished it.
        elif phase == 'verify':
            repository.verify_initial_hydration(job['id'], job['lease_token'])
            parent['done'] = True
        else:
            raise RuntimeError('operator_initial_phase_invalid')
        return Skipped()

    def inventory_work(parent, kind, key):
        job = parent['job']
        state = dict(unit=None, plan=None, summary=None)
        parent['units'][key] = state

        def settled(summary):
            state['summary'] = summary
            plan, unit = state['plan'], state['unit']
            if summary.get('provider_attempts') == 0:
                if unit.prepared is None:
                    reply = repository.cancel_prepared_inventory_chain(job['id'], job['lease_token'], kind,
                        transition_id=plan['transition_id'])
                else:
                    reply = repository.cancel_unsent_inventory_chain(job['id'], job['lease_token'], kind,
                        transition_id=plan['transition_id'], scan_id=unit.prepared.scan_id,
                        playlist_lease_token=unit.claim.lease_token, fetch_unsent=True)
                if reply.get('status') != 'cancelled' or reply.get('dispatch_allowed') is not False:
                    raise RuntimeError('operator_unsent_chain_unconfirmed')
            else:
                reply = repository.acknowledge_inventory_chain(job['id'], job['lease_token'], kind,
                    transition_id=plan['transition_id'])
                if not isinstance(reply.get('ack'), dict) or reply.get('dispatch_allowed') is not False:
                    raise RuntimeError('operator_chain_ack_unconfirmed')
            absorb(parent, summary)

        def prepare():
            if stop_reason or should_stop():
                state['summary'] = {'provider_attempts': 0}
                return Skipped()
            require_ready()
            plan = state['plan'] = repository.prepare_inventory_chain(job['id'], job['lease_token'], kind)
            action = plan.get('action')
            if action in ('done', 'paused'):
                state['summary'] = {'provider_attempts': 0}
                return Skipped()
            if action in ('reconcile_inventory', 'reconcile_failure', 'reconcile_unknown'):
                reply = repository.acknowledge_inventory_chain(job['id'], job['lease_token'], kind,
                    transition_id=plan['transition_id'], resolve_unknown=action == 'reconcile_unknown')
                if not isinstance(reply.get('ack'), dict) or reply.get('dispatch_allowed') is not False:
                    raise RuntimeError('operator_chain_recovery_unconfirmed')
                ack = reply['ack']
                evidence = plan.get('evidence') or {}
                summary = {'provider_attempts': ack.get('provider_attempts', 1),
                           'pages_processed': evidence.get('pages_processed', 0)}
                if ack.get('outcome') == 'quota':
                    summary.update(quota_exhausted=True, stopped_reason='youtube_quota_exhausted')
                elif ack.get('error_code') == 'provider_rate_limited':
                    summary.update(rate_limited=True, stopped_reason='provider_rate_limited')
                state['summary'] = summary
                absorb(parent, summary)
                return Skipped()
            if action != 'inventory' or plan.get('reserved_units') != 1:
                raise RuntimeError('operator_chain_requires_reconciliation')

            def claim():
                reply = repository.claim_inventory_chain_source(job['id'], job['lease_token'], kind,
                    transition_id=plan['transition_id'], worker_id=job['lease_owner'], run_id=job['lease_token'],
                    lease_seconds=settings.lease_seconds)
                rows = reply.get('claims')
                if not isinstance(rows, list) or len(rows) > 1:
                    raise RuntimeError('operator_inventory_claim_unconfirmed')
                if not rows:
                    cancelled = repository.cancel_prepared_inventory_chain(job['id'], job['lease_token'], kind,
                        transition_id=plan['transition_id'])
                    if cancelled.get('status') != 'cancelled':
                        raise RuntimeError('operator_empty_claim_unconfirmed')
                    state['summary'] = {'provider_attempts': 0}
                    return None
                value = TypedInventoryClaim.from_row(rows[0])
                if value.lease_channel_epoch != job['accepted_epoch']:
                    raise RuntimeError('operator_inventory_epoch_mismatch')
                return value

            def bind(claim, scan, transition):
                reply = repository.bind_inventory_chain(job['id'], job['lease_token'], kind,
                    transition_id=transition, scan_id=scan['scan_id'], playlist_lease_token=claim.lease_token)
                if (reply.get('status') != 'bound' or reply.get('scan_id') != scan['scan_id']
                        or reply.get('transition_id') != transition or reply.get('replayed') is not False
                        or reply.get('dispatch_allowed') is not True):
                    raise RuntimeError('operator_inventory_bind_unconfirmed')
                return True

            unit = state['unit'] = InventoryPageUnit(inventory_repository, key=key, lane='operator',
                channel=job['channel_id'], video_type=kind, claim_page=claim, fetch_page=fetch_page,
                reservations=InventoryPageReservations(PageBudget(1)), require_ready=require_ready,
                on_claimed=lambda value: None, on_settled=settled,
                lease_seconds=settings.lease_seconds, transition_id=plan['transition_id'],
                before_dispatch=bind, require_observed_video_ids=True,
                playlist_not_found_errors=(YouTubePlaylistNotFoundError,), clock=clock)
            result = unit.prepare()
            if isinstance(result, Skipped) and state['summary'] is None:
                # Handoff before the adapter's claim still owns a prepared
                # parent reservation. Only confirmed cancellation refunds it.
                settled({'provider_attempts': 0, 'pages_processed': 0})
            return replace(result, deadline=min(result.deadline, parent['deadline'])) if hasattr(result, 'deadline') else result

        return Work(key, 'operator', job['channel_id'], kind, prepare,
            lambda completion: state['unit'].settle(completion),
            exclusion_key='playlist:' + job['channel_id'] + ':' + kind)

    def offer_children(allowance):
        nonlocal reserved
        offered = 0
        # One child per parent per round prevents one window filling ready first.
        for parent in list(active.values()):
            if parent['legacy'] or parent['done'] or not parent['pending']:
                continue
            if offered >= allowance or coordinator.snapshot().ready >= 2 * concurrency:
                break
            job = parent['job']
            inventory = job['phase'] == 'inventory'
            if inventory and parent['credit'] == len(parent['units']):
                if reserved + processed >= max_chunks:
                    continue
                reserved += 1
                parent['credit'] += 1
            value = parent['pending'].popleft()
            key = 'operator:' + str(job['id']) + ':' + (value if inventory else value[0])
            if inventory:
                work = inventory_work(parent, value, key)
            else:
                unit = InitialBatchUnit(video_service, key=key, job_id=str(job['id']),
                    channel_id=job['channel_id'], video_ids=value, worker_id=job['lease_owner'], run_id=job['lease_token'],
                    require_ready=require_ready, on_settled=lambda summary, p=parent: absorb(p, summary),
                    lease_seconds=settings.lease_seconds, clock=clock)
                parent['units'][key] = dict(unit=unit, ids=value)
                def prepare_batch(u=unit, p=parent):
                    if stop_reason or should_stop():
                        return Skipped()
                    result = u.prepare()
                    return replace(result, deadline=min(result.deadline, p['deadline'])) if hasattr(result, 'deadline') else result
                work = replace(unit.work, prepare=prepare_batch)
            descriptors[key] = parent
            coordinator.offer(work)
            offered += 1
        return offered

    def finish_parents():
        nonlocal reserved, processed, total_pages, total_videos
        for identity, parent in list(active.items()):
            job = parent['job']
            if (parent.get('finalization_failed') or any(failure.key == 'operator-parent:' + identity
                    for failure in coordinator.failures())):
                continue
            inventory = job.get('phase') == 'inventory' and not parent['legacy']
            for key, state in list(parent['units'].items()):
                if coordinator.contains(key):
                    continue
                if inventory:
                    summary = state.get('summary') or {}
                    attempts = summary.get('provider_attempts', 0)
                    if attempts not in (0, 1):
                        raise RuntimeError('operator_inventory_attempt_invalid')
                    processed += attempts
                    total_pages += _int(summary.get('pages_processed'))
                    reserved -= 1
                    parent['credit'] -= 1
                elif state['unit'].phase == 'acknowledged':
                    parent['settled_ids'].update(claim.video_id for claim in state['unit'].claims)
                del parent['units'][key]
                descriptors.pop(key, None)
            if parent['units']:
                continue
            if stop_reason or (inventory and not parent['credit'] and reserved + processed >= max_chunks):
                parent['pending'].clear()
            if parent['legacy'] and stop_reason:
                from db.hot.hot_collection_control import HotCollectionControlRepository
                finalize(parent, HotCollectionControlRepository(repository.database).drain,
                         operators=[{'job_id': job['id'], 'lease_token': job['lease_token']}])
                if not parent.get('finalization_failed'):
                    reserved -= parent['credit']
                    del active[identity]
                continue
            if parent['pending'] or parent['legacy']:
                continue
            if inventory:
                finalize(parent, repository.finish_inventory_chains, job['id'], job['lease_token'])
            elif not parent['done']:
                summary = parent['summary']
                unstarted = [value for value in parent['selected'] if value not in parent['settled_ids']]
                reason = operator_collection_stop_reason(summary)
                finalize(parent, repository.settle_initial_hydration_chunk, job['id'], job['lease_token'],
                    observed_quota_units=_observed_collection_units(summary),
                    error_code=('initial_hydration_' + reason if reason else
                        'initial_hydration_video_collection_error' if _int(summary.get('errors')) else None),
                    summary=compact_initial_inventory_summary(dict(summary, phase='hydrate_videos',
                        outcome='video_chunk_stopped' if reason else 'video_chunk_settled',
                        hydration_requested=len(parent['selected']), hydration_unstarted_video_ids=unstarted)))
                total_videos += len(parent['selected'])
            if parent.get('finalization_failed'):
                continue
            if not inventory:
                processed += 1
            reserved -= parent['credit']
            del active[identity]

    def account_reconnect(turn):
        nonlocal reserved, processed, reconnect_progress
        for key in turn.acknowledged + turn.skipped:
            if key not in reconnect_keys:
                continue
            reconnect_keys.remove(key)
            reserved -= 1
            if key in turn.acknowledged:
                processed += 1
                reconnect_progress += 1

    def cancel_ready_and_signal():
        nonlocal reserved
        failed = {failure.key for failure in coordinator.failures()}
        owned = set(admissions) | set(descriptors) | reconnect_keys
        for key in coordinator.keys():
            if key not in owned or key in failed:
                continue
            coordinator.cancel(key)
            if coordinator.contains(key):
                continue  # running provider or known completion still owns credit
            if key in admissions:
                admissions.pop(key)
                reserved -= 1
            if key in reconnect_keys:
                reconnect_keys.remove(key)
                reserved -= 1

    class OperatorTurn:
        # These are the original slice's bounded local counters/maps. The
        # caller alone steps/waits the Coordinator and owns executor lifetime.
        def stop(self, reason, **changes):
            if get_ident() != owner_thread:
                raise RuntimeError('operator_dispatch_owner_thread_required')
            stop(reason, **changes)

        def resume_idle(self):
            """Reopen only an acknowledged empty sweep within this same window."""
            nonlocal finished, stop_reason, after, ceiling, exhausted
            if get_ident() != owner_thread:
                raise RuntimeError('operator_dispatch_owner_thread_required')
            if not finished or stop_reason != 'idle':
                return False
            if clock() - started >= max_seconds or processed + reserved >= max_chunks or should_stop():
                return False
            if active or admissions or descriptors or reconnect_keys:
                raise RuntimeError('operator_idle_resume_not_drained')
            status = coordinator.snapshot()
            if status.paused or status.blocked or status.stopped:
                return False
            after = ceiling = None
            exhausted = finished = False
            stop_reason = None
            if reconnect is not None and reconnect.exhausted:
                reconnect.begin_sweep()
            return True

        @property
        def finished(self):
            return finished

        @property
        def summary(self):
            return dict(status='error' if flags['settlement_unknown'] else 'idle' if not processed else 'processed',
                chunks_processed=processed, processed_pages=total_pages, processed_video_ids=total_videos,
                stop_reason=stop_reason, peak_ready=peak_ready, peak_held=peak_held, **flags)

        def advance(self, turn=None, *, ready_limit=100):
            nonlocal reserved, processed, total_pages, total_videos, after, ceiling, exhausted
            nonlocal peak_ready, peak_held, sweep_progress, reconnect_progress, reconnect_turn, finished
            if get_ident() != owner_thread:
                raise RuntimeError('operator_dispatch_owner_thread_required')
            if type(ready_limit) is not int or not 0 <= ready_limit <= 100:
                raise ValueError('operator_dispatch_ready_limit_invalid')
            if turn is not None:
                account_reconnect(turn)  # Caller delivers each returned Turn once.
            if finished:
                return
            if should_stop():
                stop('scheduled_handoff', yield_requested=True)
            if clock() - started >= max_seconds:
                stop('time_budget')
            if any(clock() >= parent['deadline'] for parent in active.values()):
                stop('lease_deadline')
            if stop_reason:
                cancel_ready_and_signal()
            finish_parents()
            current = coordinator.snapshot()
            allowance = min(ready_limit, 2 * concurrency - current.ready)
            if not stop_reason and not current.blocked and not current.paused:
                # Existing claimed children precede more parent admission. One
                # per parent per round; no private descriptor backlog.
                allowance -= offer_children(allowance)
                reconnect_turn = not reconnect_turn
                def offer_reconnect():
                    nonlocal allowance, reserved
                    if reconnect is not None and allowance:
                        offered = reconnect.offer_to(coordinator,
                            limit=min(allowance, max(0, max_chunks - reserved - processed)))
                        allowance -= offered
                        reserved += offered
                        reconnect_keys.update(k for k in coordinator.keys() if k.startswith('reconnect:'))
                if reconnect_turn:
                    offer_reconnect()
                free = min(allowance, 2 * concurrency - len(active) - len(admissions),
                           max_chunks - reserved - processed)
                if free > 0 and not exhausted and not any(p['legacy'] for p in active.values()):
                    require_ready()
                    page = repository.candidate_page(limit=free, after=after, ceiling=ceiling)
                    after, ceiling, exhausted = page['next_after'], page['ceiling'], page['exhausted']
                    for candidate in page['candidates']:
                        identity = candidate['job_id']
                        key = 'operator-parent:' + identity
                        if identity in active or key in admissions:
                            continue
                        admissions[key] = candidate
                        reserved += 1
                        allowance -= 1
                        coordinator.offer(Work(key, 'operator', candidate['channel_id'], 'channel',
                            lambda c=candidate, k=key: admit(c, k), lambda value: None))
                if not reconnect_turn:
                    offer_reconnect()
            status = coordinator.snapshot()
            peak_ready, peak_held = max(peak_ready, status.ready), max(peak_held, status.held)
            legacy = next((p for p in active.values() if p['legacy']), None)
            if legacy is not None and not stop_reason and not coordinator.keys():
                result = finalize(legacy, legacy_chunk, legacy['job'])
                if not legacy.get('finalization_failed'):
                    absorb(legacy, result)
                    total_pages += _int(result.get('processed_pages'))
                    total_videos += _int(result.get('processed_video_ids'))
                    processed += 1
                    reserved -= legacy['credit']
                    del active[str(legacy['job']['id'])]
            owned = set(admissions) | set(descriptors) | reconnect_keys
            failed = {failure.key for failure in coordinator.failures()}
            known = owned.intersection(coordinator.keys()) - failed
            if stop_reason:
                finished = not known  # Uncertain owners remain retained, never ACKed/retried.
            elif processed >= max_chunks and not active and not admissions and not reconnect_keys:
                stop('max_chunks')
                finished = True
            elif (exhausted and (reconnect is None or reconnect.exhausted)
                    and not active and not admissions and not reconnect_keys):
                if not sweep_progress and not reconnect_progress:
                    stop('idle')
                    finished = True
                elif not status.paused and not status.blocked and not status.stopped:
                    # ACK draining may be paused; preserve this completed sweep
                    # until the caller admits new work again.
                    after = ceiling = None
                    exhausted, sweep_progress, reconnect_progress = False, 0, 0
                    if reconnect is not None and reconnect.exhausted:
                        reconnect.begin_sweep()

    return OperatorTurn()


def run_operator_jobs_dispatch(
    repository, *, inventory_repository, video_service, fetch_page, settings,
    concurrency, max_chunks, max_seconds, require_ready, should_stop,
    legacy_chunk, executor=None, clock=None, fetch_scope=None, reconnect_factory=None,
):
    """Compatibility driver of the same resumable operator owner."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from collection.dispatch import Coordinator
    clock = clock or time.monotonic
    own_executor = executor is None
    executor = executor or ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='operator-provider')
    coordinator = Coordinator(executor, concurrency, clock=clock,
        **({'fetch_scope': fetch_scope} if fetch_scope is not None else {}))
    owner = operator_dispatch_turn(repository, inventory_repository=inventory_repository,
        video_service=video_service, fetch_page=fetch_page, settings=settings,
        concurrency=concurrency, max_chunks=max_chunks, max_seconds=max_seconds,
        require_ready=require_ready, should_stop=should_stop, legacy_chunk=legacy_chunk,
        coordinator=coordinator, clock=clock, reconnect_factory=reconnect_factory)
    advance_failed = False
    def advance(turn=None, *, ready_limit=0):
        nonlocal advance_failed
        if advance_failed:
            return
        try:
            owner.advance(turn, ready_limit=ready_limit)
        except Exception:
            advance_failed = True
            raise
    try:
        while not owner.finished:
            try:
                coordinator.pause()
                advance(coordinator.step(), ready_limit=0)
                coordinator.pause(False)
                advance(ready_limit=2 * concurrency)
                advance(coordinator.step(), ready_limit=0)
            except Exception:
                owner.stop('settlement_unknown', settlement_unknown=True, infrastructure_error=True)
                coordinator.pause()
                try:
                    advance(ready_limit=0)
                except Exception:
                    pass  # The latch retains the uncertain owner; no second call.
                failed = {failure.key for failure in coordinator.failures()}
                for key in coordinator.keys():
                    if key not in failed:
                        coordinator.cancel(key)
                known = tuple(key for key in coordinator.keys() if key not in failed)
                if known:
                    try:
                        advance(coordinator.drain_known(known), ready_limit=0)
                    except Exception:
                        # An unexpected failure of the drain boundary is also
                        # unknown; its callback path is never attempted again.
                        break
            if advance_failed:
                failed = {failure.key for failure in coordinator.failures()}
                if not any(key not in failed for key in coordinator.keys()):
                    break
            if coordinator.snapshot().inflight:
                coordinator.wait(.05)
    finally:
        if own_executor:
            executor.shutdown(wait=True, cancel_futures=True)
    return owner.summary
