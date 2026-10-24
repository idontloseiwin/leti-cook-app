"""Initial-hydration commands; repositories and settings are caller-owned."""
import uuid
from channel_collection_scope import channel_scope, require_control_epoch
from channel_hydration import int_value
from operator_jobs import operator_job_snapshot
from source_log import source_log_message
from collection.hydration import (
    completed_initial_hydration_has_errors,
    initial_hydration_has_target_count_mismatch,
    initial_hydration_verified_target_count,
    initial_hydration_verified_target_error_count,
    initial_hydration_scope_expired,
    initial_hydration_requires_new_job,
    initial_hydration_job_is_retryable,
    initial_hydration_estimate_int,
    normalized_initial_hydration_estimate,
    project_initial_hydration_retry_estimate,
    initial_hydration_payload,
    initial_hydration_error_payload,
    require_full_hydration_schema,
    load_initial_hydration_context,
    with_initial_format_readiness
)

def normalize_completed_initial_hydration_failure(repository, row):
    if not completed_initial_hydration_has_errors(row):
        return row

    job_id = str(row.get("id") or row.get("job_id") or "").strip()
    channel_id = str(row.get("channel_id") or "").strip()
    error_count = max(
        int_value(row.get("errors")),
        int_value(row.get("error_count")),
    )
    if not job_id or not channel_id or error_count <= 0:
        raise RuntimeError("operator_job_retry_not_available")

    repository.mark_completed_initial_hydration_failed(
        job_id,
        channel_id,
        error_count=error_count,
        expected_epoch=require_control_epoch(row.get("accepted_epoch")),
    )
    current = repository.get_initial_inventory_job(channel_id)
    if not current or str(current.get("id") or "") != job_id:
        raise RuntimeError("operator_job_retry_not_available")
    if (
        str(current.get("status") or "") in {"queued", "running"}
        or initial_hydration_job_is_retryable(current)
    ):
        return current
    raise RuntimeError("operator_job_retry_not_available")


def retry_initial_hydration_job(
    repository,
    row,
    *,
    retry_request_id,
    expected_epoch,
    max_items,
    max_pages,
    max_quota_units,
    include_transition=False,
):
    def result(current_row, transitioned):
        if include_transition:
            return current_row, bool(transitioned)
        return current_row

    expected_epoch = require_control_epoch(expected_epoch)
    current = (normalize_completed_initial_hydration_failure(repository, row)
               if row.get("accepted_epoch") == expected_epoch else row)
    job_id = str(current.get("id") or "").strip()
    channel_id = str(current.get("channel_id") or "").strip()
    if not job_id or not channel_id:
        raise RuntimeError("operator_job_retry_not_available")
    try:
        retry_request_id = str(uuid.UUID(str(retry_request_id)))
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("operator_job_retry_request_id_required")
    if str(current.get("status") or "") in {"queued", "running"}:
        if current.get("accepted_epoch") != expected_epoch:
            raise RuntimeError("channel_collection_scope_lost")
        return result(current, False)
    if not initial_hydration_job_is_retryable(current):
        raise RuntimeError("operator_job_retry_not_available")

    try:
        retried = repository.retry_initial_inventory(
            job_id,
            retry_request_id=retry_request_id,
            expected_epoch=expected_epoch,
            max_items=max_items,
            max_pages=max_pages,
            max_quota_units=max_quota_units,
            confirmed=True,
        )
        return result(retried, True)
    except Exception as retry_error:
        try:
            latest = repository.get_initial_inventory_job(channel_id)
        except Exception:
            raise retry_error
        if (
            latest
            and str(latest.get("id") or "") == job_id
            and str(latest.get("status") or "") in {"queued", "running"}
            and latest.get("accepted_epoch") == expected_epoch
        ):
            # The durable transition may have committed before a transport
            # failure, or another request may have won the race. Reconcile to
            # the durable public state; the caller may safely rewrite the wake hint.
            return result(latest, False)
        raise retry_error


def prepare_initial_typed_inventory(channel_id, *, get_typed_repository, typed_inventory_enabled):
    if not typed_inventory_enabled:
        return
    try:
        # Only creates missing control rows. Existing scans, leases, clocks and
        # product exposure are untouched; durable/normal workers own all I/O.
        get_typed_repository().prepare_channel_initial_inventory(channel_id)
    except Exception as error:
        # Normal typed prepare is the existing durable fallback on the next
        # cycle. Do not undo an already accepted Long job on an ambiguous reply.
        print(
            source_log_message('initial_typed_inventory_error', lambda: (f"[initial-typed-inventory] channel={channel_id} "
            f"error_type={error.__class__.__name__}"), error=error), flush=True,
        )


def enqueue_initial_inventory_for_channel(
    channel_id, *, expected_epoch, channel_repository, repository, load_settings,
):
    """Replace legacy inline collection with one admitted durable job.

    Reload only to verify the original captured scope, never to adopt a later
    remove/restore generation. Estimates, confirmation and caps keep their owner.
    """
    expected_epoch = require_control_epoch(expected_epoch)
    channel = channel_repository.load_channel_record(channel_id)
    if channel is None:
        raise RuntimeError("channel_collection_scope_lost")
    scope = channel_scope(channel)
    if scope != {"channel_id": channel_id, "control_epoch": expected_epoch}:
        raise RuntimeError("channel_collection_scope_lost")
    result, _, _ = enqueue_initial_hydration(
        channel, repository=repository, load_settings=load_settings, confirmed=False,
    )
    job = result["job"]
    if job.get("channel_id") != channel_id or job.get("accepted_epoch") != expected_epoch:
        raise RuntimeError("initial_inventory_enqueue_identity_invalid")
    return {"status": str(job.get("status") or "unknown"), "channel_id": channel_id,
            "job_id": job["id"], "created": bool(result.get("created"))}


def enqueue_initial_hydration(channel, *, repository, load_settings, expected=None, confirmed=False):
    require_full_hydration_schema(repository)
    raw_estimate = repository.estimate_initial_inventory(channel["channel_id"])
    estimate = normalized_initial_hydration_estimate(raw_estimate, channel=channel)
    if not estimate:
        raise RuntimeError("operator_job_initial_inventory_estimate_unavailable")
    echoed = expected or estimate
    settings = load_settings(require_quota_cap=False)
    result = repository.enqueue_initial_inventory(
        channel["channel_id"],
        expected_epoch=channel_scope(channel)["control_epoch"],
        expected_items=echoed["items"],
        expected_pages=echoed["pages"],
        expected_quota_units=echoed["quota_units"],
        # Counts describe expected work; they never cut off a full initial job.
        max_items=None, max_pages=None, max_quota_units=None,
        max_attempts=settings.max_target_attempts,
        confirmed=bool(confirmed),
    )
    return result, raw_estimate, settings


def confirm_initial_hydration(channel, request, *, repository, load_settings):
    channel_id = channel["channel_id"]
    expected_epoch = channel_scope(channel)["control_epoch"]
    require_full_hydration_schema(repository)
    raw_estimate, row = load_initial_hydration_context(
        repository, channel_id, expected_epoch=expected_epoch)
    requires_new_job = initial_hydration_requires_new_job(row, expected_epoch=expected_epoch)
    previous_job_id = (row or {}).get("id")
    estimate = normalized_initial_hydration_estimate(
        raw_estimate,
        channel=channel,
    )
    estimate = project_initial_hydration_retry_estimate(
        estimate,
        operator_job_snapshot(row, channel_id=channel_id) if row and not requires_new_job else None,
    )
    if not estimate:
        raise RuntimeError("operator_job_initial_inventory_estimate_unavailable")
    expected = {
        "items": request.expected_items,
        "pages": request.expected_pages,
        "quota_units": request.expected_quota_units,
    }
    settings = load_settings(require_quota_cap=False)
    row_status = str((row or {}).get("status") or "")
    expired_scope = initial_hydration_scope_expired(row)
    if not row or row_status not in {"queued", "running"} or expired_scope or requires_new_job:
        if any(
            expected[key] != estimate[key]
            for key in ("items", "pages", "quota_units")
        ):
            raise RuntimeError("operator_job_estimate_changed")
        if expired_scope and row_status != "completed":
            if request.confirmed is not True:
                raise RuntimeError("operator_job_confirmation_required")
            if raw_estimate.get("enqueue_allowed") is not True:
                raise RuntimeError("operator_job_source_scope_expired")
            result, raw_estimate, settings = enqueue_initial_hydration(channel, repository=repository, expected=expected, confirmed=request.confirmed, load_settings=load_settings)
            row = result["job"]
        elif requires_new_job:
            if request.confirmed is not True:
                raise RuntimeError("operator_job_confirmation_required")
            result, raw_estimate, settings = enqueue_initial_hydration(
                channel, repository=repository, expected=expected, confirmed=True, load_settings=load_settings)
            row = result["job"]
        elif row and initial_hydration_job_is_retryable(row):
            # Old accepted jobs keep their scope and explicit stored limits.
            # New full jobs have no total limits and resume the same workset.
            caps = {key: None if row.get("initial_scope_version") == 1 else row.get(key)
                    for key in ("max_items", "max_pages", "max_quota_units")}
            row = retry_initial_hydration_job(
                repository,
                row,
                retry_request_id=request.retry_request_id,
                expected_epoch=expected_epoch,
                **caps,
                include_transition=False,
            )
        elif not row:
            result, raw_estimate, settings = enqueue_initial_hydration(channel, repository=repository, expected=expected, confirmed=True, load_settings=load_settings)
            row = result["job"]

    if requires_new_job and (row.get("initial_scope_version") != 1
            or row.get("id") == previous_job_id
            or row.get("accepted_epoch") != expected_epoch):
        # A concurrent retry or a different generation cannot acknowledge
        # replacement of this unresumable intent, including expired sources.
        raise RuntimeError("operator_job_estimate_changed")
    return estimate, row
