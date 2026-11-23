"""Initial-hydration projections and reads with caller-owned dependencies."""
import math
from channel_hydration import int_value, with_format_readiness
from hot_storage import source_is_current
from operator_jobs import operator_job_snapshot
from channel_inventory_estimate import initial_inventory_count_bounds
from source_expiry import minimum_expiry

FULL_HYDRATION_SCHEMA_VERSION = 9
FULL_HYDRATION_PUBLIC_STATUSES = {
    "idle",
    "confirmation_required",
    "queued",
    "running",
    "ready",
    "blocked",
    "error",
    "suspended",
}
FULL_HYDRATION_TERMINAL_JOB_STATUSES = {
    "completed",
    "blocked",
    "error",
    "suspended",
}
FULL_HYDRATION_RETRYABLE_JOB_STATUSES = {
    "blocked",
    "error",
    "suspended",
}

def completed_initial_hydration_has_errors(job):
    if not job:
        return False
    return bool(
        str(job.get("status") or "") == "completed"
        and str(job.get("phase") or "") == "verify"
        and max(
            int_value(job.get("errors")),
            int_value(job.get("error_count")),
        ) > 0
    )


def initial_hydration_has_target_count_mismatch(job):
    if not job:
        return False
    return str(
        job.get("last_error_code") or job.get("error_code") or ""
    ) == "initial_hydration_target_count_mismatch"


def initial_hydration_verified_target_count(job):
    if not initial_hydration_has_target_count_mismatch(job):
        return None
    summary = job.get("summary")
    if not isinstance(summary, dict):
        summary = job.get("summary_json")
    if not isinstance(summary, dict):
        return None
    target_count = summary.get("hydration_target_count")
    if type(target_count) is not int:
        return None
    return target_count if target_count >= 0 else None


def initial_hydration_verified_target_error_count(job):
    if not initial_hydration_has_target_count_mismatch(job):
        return None
    summary = job.get("summary")
    if not isinstance(summary, dict):
        summary = job.get("summary_json")
    if not isinstance(summary, dict):
        return None
    error_count = summary.get("hydration_error_count")
    if type(error_count) is not int:
        return None
    return error_count if error_count >= 0 else None


def initial_hydration_scope_expired(job):
    if not job:
        return False
    return (
        str(job.get("last_error_code") or job.get("error_code") or "") == "source_scope_expired"
        or not source_is_current(job.get("requested_at"))
    )



def initial_hydration_requires_new_job(job, *, expected_epoch=None):
    """Replace an unresumable intent only after an explicit full-load command."""
    if (not job or type(job.get("initial_scope_version", 0)) is not int
            or job.get("job_type") != "channel_initial_long_inventory"):
        return False
    code = job.get("last_error_code") or job.get("error_code")
    if job.get("initial_scope_version", 0) == 0:
        return job.get("status") == "blocked" and code in {
            "initial_inventory_cap_exhausted", "initial_hydration_estimate_exceeded"}
    if (job.get("initial_scope_version") == 1
            and type(expected_epoch) is int and expected_epoch > 0
            and job.get("accepted_epoch") != expected_epoch
            and (job.get("status") in {"queued", "running", "blocked", "error", "suspended"}
                 or completed_initial_hydration_has_errors(job))):
        # Restore changes channel authority, not the old accepted workset.
        # Only a new explicitly confirmed job can own the current generation.
        return True
    return (job.get("initial_scope_version") == 1 and job.get("phase") == "inventory"
            and job.get("status") in {"error", "blocked"}
            and code == "initial_inventory_restart_required")


def initial_hydration_job_is_retryable(job):
    if not job or initial_hydration_scope_expired(job):
        return False
    if (
        initial_hydration_has_target_count_mismatch(job)
        and initial_hydration_verified_target_count(job) is None
    ):
        return False
    return bool(
        str(job.get("status") or "") in FULL_HYDRATION_RETRYABLE_JOB_STATUSES
        or completed_initial_hydration_has_errors(job)
    )


def initial_hydration_estimate_int(value, default=-1):
    # Unlike display coercion, a zero estimate is data, not a missing value.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def normalized_initial_hydration_estimate(estimate, *, channel=None):
    source = estimate or {}
    all_scope = source.get("scope") == "all"
    accepted_job = source.get("basis") == "accepted_job"
    bounds = None
    if all_scope and not accepted_job:
        try:
            bounds = initial_inventory_count_bounds(source)
        except ValueError:
            return None
    kind = source.get("estimate_kind") or (bounds or {}).get("estimate_kind", "upper_bound")
    if kind not in {"unknown", "upper_bound", "exact_workset"}:
        return None
    if bounds and kind != bounds["estimate_kind"]:
        return None
    if kind == "unknown":
        normalized = {"items": None, "pages": None, "quota_units": None,
                      "estimate_kind": "unknown", "items_upper_bound": None,
                      "items_lower_bound": max(0, initial_hydration_estimate_int(
                          source.get("items_lower_bound", (bounds or {}).get("items_lower_bound", 0))))}
    else:
        items = initial_hydration_estimate_int(source.get("items", source.get("estimated_items")))
        if items < 0 and bounds:
            items = bounds["items_upper_bound"]
        if items is None:
            return None
        if items < 0 and channel is not None and not all_scope:
            items = initial_hydration_estimate_int(channel.get("long_video_count"))
        if items < 0:
            return None
        if bounds and bounds["items_upper_bound"] != items:
            return None
        counts = source.get("counts") or {}
        # Known bounds leave a null count only for a list YouTube says does not
        # exist: it is empty and costs its one probe page, as the SQL estimate counts it.
        default_pages = (sum(max(1, math.ceil((counts[kind] or 0) / 50))
                             for kind in ("long", "short", "stream"))
                         if bounds else max(1, math.ceil(items / 50)))
        pages = initial_hydration_estimate_int(source.get("pages", source.get("estimated_pages")), default_pages)
        quota = initial_hydration_estimate_int(source.get("quota_units", source.get("estimated_quota_units")),
                                               max(1, pages) + (1 if all_scope and not accepted_job else 2) * math.ceil(items / 50))
        if pages < 0 or quota < 0:
            return None
        lower = items if kind == "exact_workset" else initial_hydration_estimate_int(
            source.get("items_lower_bound", (bounds or {}).get("items_lower_bound", 0)))
        upper = initial_hydration_estimate_int(source.get("items_upper_bound", items))
        if not 0 <= lower <= items or upper != items:
            return None
        normalized = {"items": items, "pages": pages, "quota_units": quota,
                      "estimate_kind": kind, "items_lower_bound": lower, "items_upper_bound": upper}
    if all_scope:
        normalized.update({"scope": "all", "video_types": ["long", "short", "stream"]})
        if not accepted_job:
            normalized.update({"counts": dict(source["counts"]), "statuses": dict(source["statuses"])})
    if accepted_job:
        normalized["basis"] = "accepted_job"
    if source.get("quota_basis") == "before_start" and normalized["quota_units"] is not None:
        normalized["quota_basis"] = "before_start"
    if source.get("source_expires_at"):
        normalized["source_expires_at"] = source["source_expires_at"]
    return normalized


def project_initial_hydration_retry_estimate(estimate, job):
    if not estimate or not job or initial_hydration_scope_expired(job):
        return estimate
    if (
        str(job.get("status") or "") not in {"queued", "running"}
        and not initial_hydration_job_is_retryable(job)
    ):
        return estimate

    if initial_hydration_requires_new_job(job):
        return estimate

    if job.get("initial_scope_version") == 1:
        projected = dict(estimate)
        if job.get("source_expires_at"):
            projected["source_expires_at"] = minimum_expiry(estimate.get("source_expires_at"), job["source_expires_at"])
        observed = int_value(job.get("inventory_items"))
        frozen = bool(job.get("targets_frozen_at"))
        if frozen:
            total = int_value(job.get("total"))
            projected.update(items=total, items_lower_bound=total, items_upper_bound=total,
                             estimate_kind="exact_workset", pages=int_value(job.get("pages_processed")))
            if projected.get("quota_basis") != "before_start":
                projected["quota_units"] = int_value(job.get("quota_units_estimated"))
        else:
            projected["items_lower_bound"] = max(projected["items_lower_bound"], observed)
            if projected["items_upper_bound"] is not None:
                projected["items"] = projected["items_upper_bound"] = max(projected["items_upper_bound"], observed)
                projected["pages"] = max(projected["pages"], int_value(job.get("pages_processed")))
                if projected.get("quota_basis") != "before_start":
                    projected["quota_units"] = max(projected["quota_units"],
                        int_value(job.get("quota_units_observed_estimate")))
        return projected

    projected = dict(estimate)
    if job.get("source_expires_at"):
        projected["source_expires_at"] = minimum_expiry(estimate.get("source_expires_at"), job["source_expires_at"])
    # The stored job scope, never a newer channel estimate, owns retry work.
    all_scope = job.get("initial_video_types") == ["long", "short", "stream"]
    hydration_factor = 2  # Retained scope-version-zero jobs use split providers.
    # Typed continuation belongs to its existing scan, not the Long job token.
    # A terminal all-format inventory retry must authorize one next bounded
    # page even when that token is deliberately absent from the public job.
    has_remaining_inventory = bool(job.get("has_resume_page_token") or (
        all_scope and initial_hydration_job_is_retryable(job)
    ))
    projected["items"] = max(
        projected["items"],
        int_value(job.get("inventory_items")),
        int_value(job.get("total")),
    )
    verified_target_count = initial_hydration_verified_target_count(job)
    if verified_target_count is not None:
        projected["items"] = max(
            projected["items"],
            verified_target_count,
        )
    projected["pages"] = max(
        projected["pages"],
        int_value(job.get("pages_processed")),
    )
    if (
        str(job.get("phase") or "") == "inventory"
        and has_remaining_inventory
    ):
        projected["items"] = max(
            projected["items"],
            int_value(job.get("inventory_items")) + 50,
        )
        projected["pages"] = min(
            2000,
            max(projected["pages"], int_value(job.get("pages_processed")) + 1),
        )
    reserved_units = int_value(job.get("quota_units_reserved"))
    phase = str(job.get("phase") or "")
    remaining_units = 0
    if phase == "inventory":
        remaining_inventory_pages = max(
            projected["pages"] - int_value(job.get("pages_processed")),
            0,
        )
        # A failed inventory request can consume its reservation before the
        # page counter advances. A retry therefore re-authorizes all visibly
        # remaining inventory pages plus the one-pass hydration workset.
        if not remaining_inventory_pages and has_remaining_inventory:
            remaining_inventory_pages = 1
        remaining_units = (
            remaining_inventory_pages + hydration_factor * math.ceil(projected["items"] / 50)
        )
    elif phase in {"hydrate_videos", "verify", "initial_stats"}:
        verified_error_count = initial_hydration_verified_target_error_count(job)
        if verified_error_count is not None:
            remaining_targets = verified_error_count
        else:
            remaining_targets = max(
                int_value(job.get("total")) - int_value(job.get("processed")),
                0,
            ) + int_value(job.get("errors"))
        remaining_units = hydration_factor * math.ceil(remaining_targets / 50)

    projected["quota_units"] = max(
        projected["quota_units"],
        reserved_units + remaining_units,
        int_value(job.get("quota_units_observed_estimate")),
        projected["pages"] + hydration_factor * math.ceil(projected["items"] / 50),
    )
    projected.update(estimate_kind="upper_bound", items_lower_bound=0,
                     items_upper_bound=projected["items"])
    return projected


def initial_hydration_payload(
    channel,
    *,
    row=None,
    estimate=None,
    mode="legacy",
):
    channel_id = str(channel.get("channel_id") or "")
    lifecycle = str(channel.get("initial_long_inventory_status") or "")
    job = operator_job_snapshot(row, channel_id=channel_id) if row else None
    if job is not None:
        job.setdefault("initial_video_types", row.get("initial_video_types") or ["long"])
    all_scope = bool(job and job.get("initial_video_types") == ["long", "short", "stream"])
    scope_expired = initial_hydration_scope_expired(row)
    requires_new_job = initial_hydration_requires_new_job(
        row, expected_epoch=channel.get("collection_control_epoch"))
    historical_complete = bool(
        scope_expired and job and job.get("status") == "completed"
        and job.get("phase") == "verify" and int_value(job.get("errors")) == 0
    )
    normalized_estimate = None if historical_complete else normalized_initial_hydration_estimate(
        estimate,
        channel=None if scope_expired else channel,
    )
    normalized_estimate = project_initial_hydration_retry_estimate(
        normalized_estimate, None if requires_new_job else job
    )

    legacy_scope = bool(
        job
        and str(job.get("job_type") or "") == "channel_initial_long_inventory"
        and str(job.get("phase") or "") == "initial_stats"
        and str(job.get("status") or "") == "completed"
    )
    empty_profile = bool(
        job
        and not all_scope
        and lifecycle == "skipped_no_long_playlist"
        and str(job.get("status") or "") == "completed"
    )
    if job:
        raw_status = str(job.get("status") or "error")
        raw_phase = str(job.get("phase") or "")
        profile_ready = not scope_expired and (
            legacy_scope or empty_profile
            or (
                raw_status == "completed"
                and raw_phase == "verify"
                and int_value(job.get("errors")) == 0
                and (job.get("initial_scope_version") != 1 or bool(job.get("targets_frozen_at")))
            )
        )
        if profile_ready:
            status = "ready"
            phase = "ready"
        else:
            status = raw_status if raw_status in FULL_HYDRATION_PUBLIC_STATUSES else "error"
            phase = (
                "hydrate_videos"
                if raw_phase == "initial_stats"
                else raw_phase
                if raw_phase in {"inventory", "hydrate_videos", "verify"}
                else None
            )
    else:
        profile_ready = lifecycle in {"completed", "skipped_no_long_playlist"}
        if profile_ready:
            status = "ready"
            phase = "ready"
        elif lifecycle == "manual_required":
            status = "confirmation_required"
            phase = None
        elif lifecycle == "running":
            status = "running"
            phase = "inventory"
        elif lifecycle == "pending":
            status = "queued" if mode == "legacy" else "error"
            phase = "inventory"
        elif lifecycle == "error":
            status = "error"
            phase = None
        else:
            status = "idle"
            phase = None

    inventory_ready = bool(
        profile_ready
        or lifecycle in {"completed", "skipped_no_long_playlist"}
        or (job and phase in {"hydrate_videos", "verify", "ready"})
    )
    total = int_value(
        job.get("total")
        if job
        else normalized_estimate.get("items")
        if normalized_estimate
        else 0
    )
    if job and phase == "inventory":
        processed = int_value(job.get("inventory_items"))
    elif job:
        processed = int_value(job.get("processed"))
    else:
        processed = total if profile_ready else 0
    processed = min(max(processed, 0), max(total, 0))
    if profile_ready or historical_complete:
        processed = total
        percent = 100
    elif total:
        percent = min(99, round((processed / total) * 100))
    else:
        percent = 0

    action_required = None
    if historical_complete:
        # A completed initial operation does not request another full hydration.
        status, phase = "completed", "verify"
    elif scope_expired:
        status, phase = "error", None
        if mode == "durable" and normalized_estimate and (estimate or {}).get("enqueue_allowed") is True:
            status, action_required = "confirmation_required", "confirm"
    elif mode == "durable":
        if requires_new_job:
            action_required = "start"
            if status in {"queued", "running"}:
                status = "suspended"
        elif job and initial_hydration_job_is_retryable(job):
            action_required = "retry"
        elif not job and lifecycle == "manual_required":
            action_required = "confirm"
        elif not job and lifecycle in {"pending", "error"}:
            action_required = "start"

    full_scope = bool(job and job.get("initial_scope_version") == 1)
    discovered = int_value(job.get("inventory_items")) if job else 0
    if full_scope and not job.get("targets_frozen_at") and not historical_complete:
        total, processed, percent = None, discovered, None
    elif not job and normalized_estimate and normalized_estimate["items"] is None:
        total, processed, percent = None, 0, None
    if (full_scope and not scope_expired and not requires_new_job and job.get("status") == "blocked"
            and job.get("error_code") == "initial_inventory_confirmation_required"):
        status, action_required = "confirmation_required", "confirm"

    payload = {
        "contract_version": 2,
        "channel_id": channel_id,
        "quota_limit_enforced": False,
        "status": status,
        "phase": phase,
        "current_profile_ready": bool(profile_ready),
        "inventory_ready": inventory_ready,
        "legacy_scope": legacy_scope,
        "action_required": action_required,
        "progress": {
            "processed": processed,
            "total": max(total, 0) if total is not None else None,
            "percent": percent,
            "discovered": discovered,
        },
        "estimate": normalized_estimate,
        "confirmation_required": status == "confirmation_required",
        "job": job,
    }
    if historical_complete:
        payload["all_initial_checks_complete"] = True
    return with_format_readiness(payload) if all_scope or historical_complete else payload


def initial_hydration_error_payload(channel, *, typed_inventory_enabled, estimate=None):
    payload = initial_hydration_payload(
        channel,
        estimate=estimate,
        mode="durable",
    )
    payload.update({
        "status": "error",
        "phase": None,
        "current_profile_ready": False,
        "confirmation_required": False,
        "action_required": None,
    })
    return with_format_readiness(
        payload, runtime_enabled=typed_inventory_enabled,
        read_error="initial_hydration_unavailable",
    )


def require_full_hydration_schema(repository):
    schema = repository.schema_status()
    if (
        schema.get("ready") is not True
        or int_value(schema.get("schema_version")) < FULL_HYDRATION_SCHEMA_VERSION
        or schema.get("initial_quota_limit_enforced") is not False
        or type(schema.get("full_initial_scope_version")) is not int
        or schema["full_initial_scope_version"] != 1
        or schema.get("initial_total_limits_enforced") is not False
    ):
        raise RuntimeError("operator_jobs_full_hydration_schema_not_ready")
    return schema


def load_initial_hydration_context(repository, channel_id, *, expected_epoch=None):
    # Production callers provide current channel authority. The optional value
    # retains compatibility for projections with no channel control context.
    row = repository.get_initial_inventory_job(channel_id)
    if not row:
        return repository.estimate_initial_inventory(channel_id), row
    if initial_hydration_requires_new_job(row, expected_epoch=expected_epoch):
        # Present the new full scope before the user clicks Load full channel.
        # Polling does not retire, resize, retry or replace the old accepted job.
        return repository.estimate_initial_inventory(channel_id), row
    if initial_hydration_scope_expired(row):
        # Completed initial work remains a milestone. Its old copied source
        # summary is not current-profile proof and does not trigger a reload.
        if str(row.get("status") or "") == "completed":
            return None, row
        return repository.estimate_initial_inventory(channel_id), row
    if row.get("initial_scope_version") == 1:
        summary = row.get("summary_json") or {}
        frozen = bool(row.get("targets_frozen_at"))
        items = int_value(row.get("target_count")) if frozen else summary.get("estimated_items")
        # The accepted estimate and observed attempts answer different questions.
        # Never relabel a later internal work forecast as the owner's initial quote.
        initial_quota = initial_hydration_estimate_int(summary.get("estimated_quota_units"))
        estimate = {
            "scope": "all", "video_types": ["long", "short", "stream"], "basis": "accepted_job",
            "estimated_items": items,
            "estimated_pages": int_value(row.get("pages_processed")) if frozen else summary.get("estimated_pages"),
            "estimated_quota_units": initial_quota if initial_quota >= 0 else row.get("quota_units_estimated"),
            **({"quota_basis": "before_start"} if initial_quota >= 0 else {}),
            "estimate_kind": "exact_workset" if frozen else "unknown" if items is None else "upper_bound",
            "items_lower_bound": items if frozen else int_value(summary.get("items_lower_bound")),
            "items_upper_bound": items,
            **({"source_expires_at": row["source_expires_at"]} if row.get("source_expires_at") else {}),
        }
        return estimate, row
    # The still-valid accepted workset owns its original retry limits.
    phase = str(row.get("phase") or "")
    items = initial_hydration_estimate_int(
        row.get("max_items") if phase == "inventory" else row.get("target_count"),
    )
    if items < 0:
        raise RuntimeError("operator_job_initial_inventory_estimate_unavailable")
    estimate = {
        "estimated_items": items,
        "estimated_pages": max(1, int_value(row.get("max_pages")), int_value(row.get("pages_processed"))),
        "estimated_quota_units": max(0, int_value(row.get("quota_units_estimated"))),
        "basis": "accepted_job",
        **({"source_expires_at": row["source_expires_at"]} if row.get("source_expires_at") else {}),
    }
    if row.get("initial_video_types") == ["long", "short", "stream"]:
        estimate.update({"scope": "all", "video_types": ["long", "short", "stream"]})
    return estimate, row


def load_initial_hydration_state(channel, *, repository, get_typed_repository, mode, typed_inventory_enabled):
    if mode not in {"durable", "drain"}:
        return with_initial_format_readiness(channel, initial_hydration_payload(channel, mode=mode), get_typed_repository=get_typed_repository, typed_inventory_enabled=typed_inventory_enabled)
    require_full_hydration_schema(repository)
    estimate, row = load_initial_hydration_context(
        repository, channel["channel_id"], expected_epoch=channel.get("collection_control_epoch"))
    return with_initial_format_readiness(channel, initial_hydration_payload(channel, row=row, estimate=estimate, mode=mode), get_typed_repository=get_typed_repository, typed_inventory_enabled=typed_inventory_enabled)


def with_initial_format_readiness(channel, payload, *, get_typed_repository, typed_inventory_enabled):
    if (payload.get("job") or {}).get("initial_video_types") == ["long", "short", "stream"]:
        # New durable jobs own frozen per-format targets; current inventory is
        # not their workset and must not overwrite this progress on each poll.
        return with_format_readiness(payload)
    # Failure of the additional typed read must not close an already-ready Long
    # Overview. Unknown is explicit; it is never interpreted as an empty format.
    try:
        state = get_typed_repository().load_channel_initial_hydration(
            channel["channel_id"]
        )
    except Exception as error:
        return with_format_readiness(
            payload, runtime_enabled=typed_inventory_enabled,
            read_error=error.__class__.__name__,
        )
    return with_format_readiness(
        payload, state, runtime_enabled=typed_inventory_enabled,
    )
