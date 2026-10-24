"""Read-only, format-explicit initial coverage; no provider or database I/O."""

from collections.abc import Mapping


TYPED_FORMATS = ("short", "stream")
COUNT_FIELDS = (
    "known_count", "hydrated_count", "unavailable_count",
    "awaiting_completion_count", "pending_count", "missing_queue_count",
    "identity_unresolved_count", "failed_count",
)


def _unknown_format(video_type, *, status="unknown"):
    return {
        "video_type": video_type,
        "status": status,
        "inventory_status": None,
        "inventory_ready": False,
        "current_profile_ready": False,
        "initial_check_complete": False,
        "runtime_enabled": False,
        "counts_available": False,
        **{field: None for field in COUNT_FIELDS},
    }


def _typed_format(video_type, row, *, channel_eligible, runtime_enabled):
    if not isinstance(row, Mapping):
        raise ValueError("typed hydration format is missing")
    counts = {}
    for field in COUNT_FIELDS:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("typed hydration counts are invalid")
        counts[field] = value
    known = counts["known_count"]
    if (
        sum(counts[key] for key in (
            "hydrated_count", "unavailable_count", "awaiting_completion_count",
            "pending_count",
        )) != known
        or any(value > known for value in counts.values())
        or counts["missing_queue_count"] > counts["pending_count"]
        or counts["identity_unresolved_count"] > counts["pending_count"]
    ):
        raise ValueError("typed hydration counts disagree")
    for field in ("inventory_ready", "runtime_enabled"):
        if not isinstance(row.get(field), bool):
            raise ValueError("typed hydration readiness is invalid")
    inventory_status = row.get("inventory_status")
    inventory_ready = row["inventory_ready"]
    enabled = bool(runtime_enabled and row["runtime_enabled"])
    complete = False
    if not channel_eligible:
        status = "blocked"
    elif not enabled:
        status = "disabled" if not runtime_enabled else "pending_inventory"
    elif inventory_status == "playlist_not_found":
        # A completed initial check, NOT evidence of a complete empty playlist.
        status = "not_available" if known == 0 else "blocked"
        complete = known == 0
    elif inventory_status == "error":
        status = "error"
    elif not inventory_ready:
        status = "pending_inventory"
    elif counts["identity_unresolved_count"]:
        status = "blocked"
    elif counts["pending_count"]:
        status = "collecting"
    else:
        status = "checked" if counts["awaiting_completion_count"] else "ready"
        complete = True
    return {
        "video_type": video_type,
        "status": status,
        "inventory_status": inventory_status,
        "inventory_ready": inventory_ready,
        "current_profile_ready": bool(
            complete and inventory_ready and not counts["awaiting_completion_count"]
        ),
        "initial_check_complete": complete,
        "runtime_enabled": enabled,
        "counts_available": True,
        **counts,
    }


def _frozen_format(video_type, row):
    if not isinstance(row, Mapping):
        raise ValueError("initial job format is missing")
    fields = ("total", "updated", "unavailable", "checked", "pending", "errors")
    counts = {field: row.get(field) for field in fields}
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in counts.values()):
        raise ValueError("initial job counts are invalid")
    if sum(counts[field] for field in fields[1:]) != counts["total"]:
        raise ValueError("initial job counts disagree")
    if any(not isinstance(row.get(field), bool) for field in (
        "inventory_ready", "initial_check_complete", "current_profile_ready",
    )):
        raise ValueError("initial job readiness is invalid")
    inventory_ready = row["inventory_ready"]
    complete = bool(row["initial_check_complete"] and not counts["pending"] and not counts["errors"])
    ready = bool(row["current_profile_ready"] and complete and inventory_ready and not counts["checked"])
    if counts["errors"]:
        status = "error"
    elif ready:
        status = "ready"
    elif complete:
        status = "checked" if counts["checked"] else "not_available"
    elif not inventory_ready:
        status = "pending_inventory"
    else:
        status = "collecting"
    return {
        "video_type": video_type, "scope": "frozen_initial_job", "status": status,
        "inventory_ready": inventory_ready, "initial_check_complete": complete,
        "current_profile_ready": ready, "counts_available": True,
        **counts,
        "progress": {
            "total": counts["total"],
            "processed": counts["updated"] + counts["unavailable"] + counts["checked"],
        },
    }


def _with_frozen_format_readiness(payload):
    result = dict(payload)
    job = result["job"]
    summary = job.get("summary") or {}
    rows = summary.get("per_format") if isinstance(summary, Mapping) else None
    formats = {}
    read_error = None
    for kind in ("long", "short", "stream"):
        try:
            formats[kind] = _frozen_format(kind, rows.get(kind) if isinstance(rows, Mapping) else None)
        except (TypeError, ValueError):
            formats[kind] = _unknown_format(kind)
            # Queued/inventory jobs may not have a frozen summary yet.
            if job.get("phase") not in {None, "inventory"}:
                read_error = "invalid_format_readiness"
    # Per-format counts may finish before the job's frozen-target verification.
    # Keep the existing Overview gate closed until that final check succeeds.
    verified = (job.get("status") == "completed" and job.get("phase") == "verify"
                and job.get("errors", 0) == 0
                and (job.get("initial_scope_version") != 1 or bool(job.get("targets_frozen_at"))))
    # The catalogue excludes waiting videos individually. A completed initial
    # check must not hide the published Long profiles in the same channel.
    long_ready = bool(verified and formats["long"]["inventory_ready"]
                      and formats["long"]["initial_check_complete"])
    all_checked = verified and all(row["initial_check_complete"] for row in formats.values())
    all_ready = verified and all(row["current_profile_ready"] for row in formats.values())
    result.update({
        "current_profile_ready": long_ready,
        "inventory_ready": formats["long"]["inventory_ready"],
        "readiness_scope": "long", "job_scope": "all",
        "format_readiness_version": 1, "typed_coverage_scope": "frozen_initial_job",
        "formats": formats, "all_formats_ready": all_ready,
        "all_initial_checks_complete": all_checked,
        "all_formats_status": "ready" if all_ready else "error" if read_error or any(
            row["status"] == "error" for row in formats.values()
        ) else "checked" if all_checked else "pending",
        "format_readiness_error": read_error,
    })
    if job.get("status") == "completed":
        # Completing the initial job is independent of whether this channel
        # has a Long profile. Keep that analytical gate truthful and separate.
        result.update({"status": "ready" if long_ready else "completed" if all_checked else "error",
                       "phase": "ready" if long_ready else "verify"})
        if all_checked and not long_ready:
            result.update({
                "action_required": None,
                "confirmation_required": False,
            })
    return result


def with_format_readiness(long_payload, typed_state=None, *, runtime_enabled=False,
                          read_error=None):
    """Preserve the v1 Long UI gate; never imply that it covers hidden formats.

    Typed coverage describes the currently known inventory, not a new frozen
    workset or a refresh-after-click promise. GET/polling never enqueues work.
    """
    job = long_payload.get("job") or {}
    expired = job.get("error_code") == "source_scope_expired"
    if expired and job.get("status") == "completed" and job.get("phase") == "verify" and job.get("errors", 0) == 0:
        # Preserve the completed operation, without copying expired per-format
        # source claims or requesting another full initial hydration.
        result = dict(long_payload)
        result.update({
            "status": "completed", "phase": "verify", "action_required": None,
            "confirmation_required": False, "current_profile_ready": False,
            "all_initial_checks_complete": True, "all_formats_ready": False,
            "all_formats_status": "checked", "readiness_scope": "long",
            "format_readiness_version": 1, "typed_coverage_scope": "frozen_initial_job",
            "formats": {kind: _unknown_format(kind) for kind in ("long", "short", "stream")},
            "format_readiness_error": None,
        })
        return result
    if job.get("initial_video_types") == ["long", "short", "stream"]:
        return _with_frozen_format_readiness(long_payload)
    result = dict(long_payload)
    long_ready = result.get("current_profile_ready") is True
    formats = {
        "long": {
            "video_type": "long",
            "scope": "initial_long_hydration",
            "status": result.get("status"),
            "phase": result.get("phase"),
            "inventory_ready": result.get("inventory_ready") is True,
            "current_profile_ready": long_ready,
            "initial_check_complete": long_ready,
            "progress": dict(result.get("progress") or {}),
        },
        **{video_type: _unknown_format(video_type) for video_type in TYPED_FORMATS},
    }
    if typed_state is not None and not read_error:
        try:
            if (
                not isinstance(typed_state, Mapping)
                or isinstance(typed_state.get("schema_version"), bool)
                or typed_state.get("schema_version") != 1
                or typed_state.get("channel_id") != result.get("channel_id")
                or not isinstance(typed_state.get("channel_eligible"), bool)
                or not isinstance(typed_state.get("formats"), Mapping)
            ):
                raise ValueError("typed hydration response is invalid")
            typed_formats = {
                video_type: _typed_format(
                    video_type, typed_state["formats"].get(video_type),
                    channel_eligible=typed_state["channel_eligible"],
                    runtime_enabled=runtime_enabled,
                )
                for video_type in TYPED_FORMATS
            }
            formats.update(typed_formats)
        except (TypeError, ValueError, KeyError):
            read_error = "invalid_format_readiness"
    if read_error:
        for video_type in TYPED_FORMATS:
            formats[video_type] = _unknown_format(video_type, status="unknown")
    all_ready = all(value["initial_check_complete"] for value in formats.values())
    if all_ready:
        status = "ready"
    elif read_error or any(value["status"] == "error" for value in formats.values()):
        status = "error"
    elif any(value["status"] in {"blocked", "confirmation_required", "suspended"}
             for value in formats.values()):
        status = "blocked"
    else:
        status = "pending"
    result.update({
        "readiness_scope": "long",
        "format_readiness_version": 1,
        "typed_coverage_scope": "current_inventory",
        "formats": formats,
        "all_formats_ready": all_ready,
        "all_formats_status": status,
        "format_readiness_error": read_error,
    })
    return result


def int_value(value, default=0):
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default
