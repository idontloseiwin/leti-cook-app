"""Shared bounded inventory runtime for canonical Long, Shorts and Streams.

The service reads exactly one playlist page per claimed state.  It deliberately
does not request ``videos.list`` and never schedules metadata/counter work.
"""

from __future__ import annotations

from collection.budgets import PageBudget
from pipeline_trace import capture, event, scope
from collection.inventory_steps import InventoryPageResponse, InventoryPageStep, PreparedInventoryPage
from channel_collection_scope import validate_channel_scopes
from collection_outcome import copy_collection_stop
from collection_shutdown import stop_at_boundary, release_unstarted_playlists
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from db.hot.hot_collection_control import admission_denial, abort_pending_playlists
from db.inventory.typed_inventory import (
    MAX_TYPED_INVENTORY_DUE_CLAIMS,
    TypedInventoryClaim,
    TypedInventoryRepository,
    InventoryTokenCycleError,
)
from typed_playlist import (
    SCAN_COMPLETE,
    SCAN_CONTINUATION,
    SCAN_EMPTY,
    SCAN_ERROR,
    SCAN_PLAYLIST_NOT_FOUND,
    scan_typed_playlist_window,
    typed_playlist_id,
)


PAGE_STATUS_BOUNDARY = "boundary"
RUNTIME_VIDEO_TYPES = ("long", "short", "stream")
MAX_CLAIM_WINDOW = 50
MAX_CONTINUATION_PROOFS = 500
MAX_RESULT_EXAMPLES = 500
DAILY_QUOTA_ERROR_TYPES = frozenset({
    "youtube_daily_quota_exceeded_error",
    "youtubedailyquotaexceedederror",
    "youtubetypedplaylistquotaerror",
    "youtube_quota_exhausted",
})
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class _TypedInventoryFailureSettlementError(RuntimeError):
    """Preserve sanitized provider evidence across a failed DB settlement."""

    def __init__(
        self,
        *,
        scan_id: str,
        transition_id: str,
        expected_page_token: str | None,
        outcome: str,
        provider_error_type: str,
        source_checked_at: datetime,
        settlement_error_type: str,
    ):
        super().__init__("typed inventory provider failure settlement failed")
        self.scan_id = scan_id
        self.transition_id = transition_id
        self.expected_page_token = expected_page_token
        self.outcome = outcome
        self.provider_error_type = provider_error_type
        self.source_checked_at = source_checked_at
        self.settlement_error_type = settlement_error_type


class _TypedInventoryDailyQuotaError(RuntimeError):
    """Stop typed provider work before mutating the active scan."""

    def __init__(self, *, provider_error_type: str, transition_id: str,
                 source_checked_at: datetime):
        super().__init__("typed inventory daily quota exhausted")
        self.provider_error_type = provider_error_type
        self.transition_id = transition_id
        self.source_checked_at = source_checked_at


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return fallback
    else:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_error_code(value: Any, *, default: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    normalized = normalized.strip("_")[:64]
    if not normalized or _ERROR_CODE_RE.fullmatch(normalized) is None:
        return default
    return normalized


def _before_known_boundary(
    items: tuple[dict[str, Any], ...],
    known_ids: set[str],
) -> tuple[tuple[dict[str, Any], ...], bool]:
    selected = []
    for item in items:
        if item["video_id"] in known_ids:
            return tuple(selected), True
        selected.append(item)
    return tuple(selected), False


def _is_daily_quota_error_type(value: Any) -> bool:
    return str(value or "").strip().lower() in DAILY_QUOTA_ERROR_TYPES


def run_typed_inventory_claim(
    *, repository: TypedInventoryRepository, claim: TypedInventoryClaim,
    fetch_page: Callable[..., Mapping[str, Any]], page_budget: PageBudget | None = None,
    require_observed_video_ids: bool = False, transition_id: str | None = None,
    before_dispatch: Callable[[TypedInventoryClaim, Mapping[str, Any], str], bool] | None = None,
    playlist_not_found_errors: tuple[type[BaseException], ...] = (),
    now: Callable[[], datetime] = _utc_now, uuid_factory: Callable[[], Any] = uuid.uuid4,
) -> dict[str, Any]:
    """Sequential compatibility path: begin/bind, debit, one fetch, settle."""
    budget = PageBudget(1) if page_budget is None else page_budget
    prepared = prepare_typed_inventory_claim(repository=repository, claim=claim,
        fetch_page=fetch_page, page_budget=budget, require_observed_video_ids=require_observed_video_ids,
        transition_id=transition_id, before_dispatch=before_dispatch,
        playlist_not_found_errors=playlist_not_found_errors, now=now, uuid_factory=uuid_factory)
    if isinstance(prepared, dict):
        return prepared
    # Preserve the public sequential contract: callbacks already observe the
    # charged attempt. The opt-in dispatcher uses owner-side reservations.
    if not budget.try_debit():
        raise RuntimeError("inventory provider attempt was not dispatched")
    return settle_typed_inventory_claim(repository=repository, prepared=prepared,
                                        response=prepared.step.execute())


def prepare_typed_inventory_claim(
    *,
    repository: TypedInventoryRepository,
    claim: TypedInventoryClaim,
    fetch_page: Callable[..., Mapping[str, Any]],
    page_budget: PageBudget | None = None,
    require_observed_video_ids: bool = False,
    transition_id: str | None = None,
    before_dispatch: Callable[[TypedInventoryClaim, Mapping[str, Any], str], bool] | None = None,
    playlist_not_found_errors: tuple[type[BaseException], ...] = (),
    now: Callable[[], datetime] = _utc_now,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
) -> PreparedInventoryPage | dict[str, Any]:
    """Prepare one page on its owner thread without spending the page budget.

    A job can bind its durable checkpoint after begin and before dispatch.
    An ambiguous/replayed binding must return False and cannot send another page.
    The planned transition is distinct from the scan identity and survives every
    page/failure/quota acknowledgement retry.
    """
    budget = PageBudget(1) if page_budget is None else page_budget
    if not isinstance(budget, PageBudget):
        raise TypeError("page_budget must be a PageBudget")
    if claim.video_type not in RUNTIME_VIDEO_TYPES:
        raise ValueError("inventory video type is invalid")
    if (not isinstance(claim.channel_id, str) or len(claim.channel_id) != 24
            or not claim.channel_id.startswith("UC")):
        raise ValueError("inventory channel identity is invalid")
    planned_transition = str(transition_id or uuid_factory())
    # Reject an invalid planned identity before begin/bind/provider effects.
    uuid.UUID(planned_transition)
    identity = dict(channel_id=claim.channel_id, video_type=claim.video_type,
                    scan_kind=claim.scan_kind, transition_id=planned_transition,
                    channel_scope={"channel_id": claim.channel_id, "control_epoch": claim.lease_channel_epoch})
    if not budget.remaining:
        event("inventory_page", "skipped", operation_id=planned_transition, channel_id=claim.channel_id, video_type=claim.video_type, reason="page_budget_exhausted")
        return {**identity, "status": "budget_exhausted", "provider_attempts": 0,
                "pages_processed": 0, "page_items": 0}
    started_at = now()
    scan_id = claim.active_scan_id or str(uuid_factory())
    requested_boundary_at = _parse_datetime(claim.scan_boundary_at, fallback=started_at)
    begin = repository.begin_scan(claim, scan_id=scan_id, scan_boundary_at=requested_boundary_at)
    if str(begin.get("status") or "") != "running":
        raise RuntimeError("typed inventory begin did not return a running scan")
    scan_id = str(begin.get("scan_id") or scan_id)
    expected_page_token = str(begin["resume_page_token"]) if begin.get("resume_page_token") else None
    scan_boundary_at = _parse_datetime(begin.get("scan_boundary_at"), fallback=requested_boundary_at)
    if before_dispatch is not None and before_dispatch(claim, begin, planned_transition) is not True:
        event("inventory_page", "unknown", operation_id=planned_transition, channel_id=claim.channel_id, video_type=claim.video_type, reason="await_receipt")
        return {**identity, "scan_id": scan_id, "status": "await_receipt",
                "provider_attempts": 0, "pages_processed": 0, "page_items": 0,
                "settlement_unknown": True}

    with scope("inventory_prepare", operation_id=planned_transition, channel_id=claim.channel_id,
               video_type=claim.video_type, reason=claim.scan_kind) as trace:
        trace_context = capture()
        trace.finish("prepared")
    return PreparedInventoryPage(claim=claim, identity=identity, scan_id=scan_id,
        expected_page_token=expected_page_token, scan_boundary_at=scan_boundary_at,
        step=InventoryPageStep(fetch=fetch_page, playlist_id=typed_playlist_id(claim.channel_id, claim.video_type),
            page_token=expected_page_token, phase="typed_inventory_" + claim.video_type,
            now=now, before_fetch=started_at, trace_context=trace_context),
        playlist_not_found_errors=playlist_not_found_errors,
        require_observed_video_ids=require_observed_video_ids)


def settle_typed_inventory_claim(*, repository, prepared: PreparedInventoryPage,
                                 response: InventoryPageResponse):
    """Validate a captured page and settle it once, on the database owner thread."""
    if not isinstance(response, InventoryPageResponse) or response.attempted is not True:
        raise RuntimeError("inventory provider attempt was not dispatched")
    claim, identity = prepared.claim, prepared.identity
    scan_id, planned_transition = prepared.scan_id, identity["transition_id"]
    expected_page_token, scan_boundary_at = prepared.expected_page_token, prepared.scan_boundary_at
    require_observed_video_ids = prepared.require_observed_video_ids
    # The original scanner parses the captured raw page/error, never a second
    # HTTP request. Its validation and exception classification stay unchanged.
    result = scan_typed_playlist_window(
        claim.channel_id, claim.video_type, fetch_page=lambda *a, **kw: response.unwrap(),
        max_pages=1, page_token=expected_page_token, max_results=50,
        phase="typed_inventory_" + claim.video_type,
        playlist_not_found_errors=prepared.playlist_not_found_errors,
    )
    checked_at, attempts = response.source_checked_at, 1

    def fail(outcome, code):
        try:
            settled = repository.fail_scan(
                claim, scan_id=scan_id, transition_id=planned_transition,
                expected_page_token=expected_page_token, outcome=outcome,
                error_code=code, source_checked_at=checked_at,
            )
        except Exception as settlement_error:
            event("inventory_write", "unknown", operation_id=planned_transition, channel_id=claim.channel_id, video_type=claim.video_type, reason="failure_settlement_unknown")
            raise _TypedInventoryFailureSettlementError(
                scan_id=scan_id, transition_id=planned_transition,
                expected_page_token=expected_page_token, outcome=outcome,
                provider_error_type=code, source_checked_at=checked_at,
                settlement_error_type=settlement_error.__class__.__name__,
            ) from settlement_error
        event("inventory_write", "confirmed", operation_id=planned_transition, channel_id=claim.channel_id,
              video_type=claim.video_type, reason=code, observed_at=checked_at)
        return {**identity, "scan_id": scan_id, "status": outcome,
                "error_type": code, "pages_processed": 0, "provider_attempts": attempts,
                "page_items": 0, "inserted": 0, "already_matching": 0, "conflicts": 0,
                "items_seen": int(settled.get("items_seen") or 0),
                "canonical_video_ids": [], "new_video_ids": [],
                "receipt": settled}

    if result.status in {SCAN_ERROR, SCAN_PLAYLIST_NOT_FOUND}:
        outcome = "playlist_not_found" if result.status == SCAN_PLAYLIST_NOT_FOUND else "error"
        code = ("typed_playlist_not_found" if outcome == "playlist_not_found" else
                _safe_error_code(result.error_type, default="typed_playlist_request_failed"))
        if _is_daily_quota_error_type(code):
            raise _TypedInventoryDailyQuotaError(provider_error_type=code,
                transition_id=planned_transition, source_checked_at=checked_at)
        if code == "youtuberatelimiterror":
            code = "provider_rate_limited"
        result = fail(outcome, code)
        if code == "provider_rate_limited":
            result.update(rate_limited=True, stopped_reason="provider_rate_limited")
        return result

    items, page_status, next_page_token = result.items, result.status, result.next_page_token
    unseen_prefix = items
    if claim.scan_kind == "discovery" and items:
        known_ids = repository.known_membership_ids(claim, (item["video_id"] for item in items),
            scan_id=scan_id, scan_boundary_at=scan_boundary_at)
        unseen_prefix, boundary_found = _before_known_boundary(items, known_ids)
        if boundary_found:
            page_status, next_page_token = PAGE_STATUS_BOUNDARY, None
        # A known ID ends pagination, not observation of an already received
        # page. Settle all <=50 items atomically so a returning membership after
        # that ID is not discarded. The repository alone counts new identities.
    if page_status not in {SCAN_CONTINUATION, SCAN_COMPLETE, SCAN_EMPTY, PAGE_STATUS_BOUNDARY}:
        raise RuntimeError("typed inventory produced an unsupported page status")
    event("inventory_write", "started", operation_id=planned_transition, channel_id=claim.channel_id,
          video_type=claim.video_type, requested_count=len(items), observed_at=checked_at)
    try:
        settled = repository.settle_page(
            claim, scan_id=scan_id, transition_id=planned_transition,
            expected_page_token=expected_page_token, next_page_token=next_page_token,
            page_status=page_status, items=items, source_checked_at=checked_at,
        )
    except InventoryTokenCycleError:
        # Only a confirmed server-side rollback permits a different terminal
        # outcome. A transport failure keeps the original page/lease for recovery.
        return fail("error", "inventory_token_cycle")
    except Exception:
        event("inventory_write", "unknown", operation_id=planned_transition, channel_id=claim.channel_id, video_type=claim.video_type, reason="settlement_unknown")
        raise
    for key, expected in (("channel_id", claim.channel_id), ("video_type", claim.video_type),
                          ("scan_id", scan_id), ("lease_token", claim.lease_token),
                          ("transition_id", planned_transition)):
        if str(settled.get(key) or "") != str(expected):
            raise RuntimeError("inventory receipt identity mismatch")
    for key in ("page_items", "inserted", "already_matching", "conflicts"):
        if type(settled.get(key)) is not int or not 0 <= settled[key] <= 50:
            raise RuntimeError("inventory receipt counts are invalid")
    for key in ("canonical_video_ids", "new_video_ids"):
        ids = settled.get(key)
        if (not isinstance(ids, list) or len(ids) > 50 or
                any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is None for value in ids)):
            raise RuntimeError("inventory receipt identities are invalid")
    observed = {}
    if require_observed_video_ids or "observed_video_ids" in settled:
        ids = settled.get("observed_video_ids")
        if (not isinstance(ids, list) or len(ids) > 50 or
                any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is None for value in ids)
                or len(set(ids)) != len(ids)
                or set(ids) != {item["video_id"] for item in items}):
            raise RuntimeError("inventory receipt observed identities are invalid")
        observed["observed_video_ids"] = ids
    event("inventory_write", "confirmed", operation_id=planned_transition, channel_id=claim.channel_id,
          video_type=claim.video_type, reason=page_status, observed_at=checked_at,
          requested_count=len(items), received_count=settled["page_items"], video_ids=[item["video_id"] for item in items])
    return {**identity, "scan_id": scan_id, "resume_page_token": settled.get("resume_page_token"),
            "status": str(settled.get("status") or ""), "coverage": str(settled.get("coverage") or "partial"),
            "pages_processed": 1, "provider_attempts": attempts,
            "inserted": settled["inserted"], "already_matching": settled["already_matching"],
            "conflicts": settled["conflicts"], "page_items": settled["page_items"],
            "items_seen": int(settled.get("items_seen") or 0),
            "boundary_found": page_status == PAGE_STATUS_BOUNDARY,
            "latest_uulf_known": bool(claim.scan_kind == "discovery" and expected_page_token is None
                and result.items and page_status == PAGE_STATUS_BOUNDARY and not unseen_prefix),
            "full_generation_new_count": settled.get("new_memberships_found"),
            "pending_channel_count_change": settled.get("pending_channel_count_change") is True,
            "canonical_video_ids": settled["canonical_video_ids"],
            "new_video_ids": settled["new_video_ids"], "receipt": settled, **observed}


def run_typed_inventory_batch(
    *, repository: TypedInventoryRepository,
    fetch_page: Callable[..., Mapping[str, Any]], limit: int, worker_id: str,
    run_id: str | None = None,
    explicit_channel_ids: list[str] | tuple[str, ...] | None = None,
    video_types: tuple[str, ...] = RUNTIME_VIDEO_TYPES,
    playlist_not_found_errors: tuple[type[BaseException], ...] = (),
    prepare: bool = True, expected_channel_scopes=None,
    initial_job_id: str | None = None, initial_job_lease_token: str | None = None,
    on_page: Callable[[dict[str, Any]], None] | None = None,
    page_budget: PageBudget | None = None,
    transition_id: str | None = None,
    before_dispatch: Callable[[TypedInventoryClaim, Mapping[str, Any], str], bool] | None = None,
    now: Callable[[], datetime] = _utc_now,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
) -> dict[str, Any]:
    """One page per claimed turn, with persistent SQL fairness across formats.

    Due states and this pass's confirmed continuations share the same queue.
    Pending leases, continuation proofs and result examples are bounded separately
    from the API budget. Losing an in-memory proof merely waits for normal due
    time; the durable scan retains its cursor and complete-coverage evidence.
    """
    if not isinstance(worker_id, str):
        raise TypeError("worker_id must be supplied as a string")
    if not worker_id.strip():
        raise ValueError("worker_id must not be empty")
    requested = PageBudget(limit)
    budget = requested if page_budget is None else page_budget
    if not isinstance(budget, PageBudget) or budget.remaining != requested.limit:
        raise ValueError("page budget must match the remaining pass limit")
    if expected_channel_scopes is not None and explicit_channel_ids is None:
        raise ValueError("channel_collection_scope_requires_explicit_channels")
    selected_types = tuple(dict.fromkeys(video_types))
    if not selected_types or any(value not in RUNTIME_VIDEO_TYPES for value in selected_types):
        raise ValueError("inventory video types are invalid")
    addressed = explicit_channel_ids is not None
    if not addressed and set(selected_types) != set(RUNTIME_VIDEO_TYPES):
        raise ValueError("a format-filtered inventory pass requires explicit channels")
    pending_channels = list(dict.fromkeys(explicit_channel_ids or []))
    if transition_id is not None or before_dispatch is not None:
        if not addressed or len(pending_channels) != 1 or len(selected_types) != 1 or limit != 1:
            raise ValueError("planned inventory dispatch requires one addressed page")
    if (initial_job_id is None) != (initial_job_lease_token is None):
        raise ValueError("initial inventory job identity requires both job and lease")
    job_addressed = initial_job_id is not None
    if job_addressed:
        initial_job_id = str(uuid.UUID(str(initial_job_id)))
        initial_job_lease_token = str(uuid.UUID(str(initial_job_lease_token)))
        if (not addressed or len(explicit_channel_ids) != 1 or len(video_types) != 1
                or limit != 1 or transition_id is None or not callable(before_dispatch)):
            raise ValueError("initial inventory job requires one planned addressed page")
        uuid.UUID(str(transition_id))
        if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", pending_channels[0]) is None:
            raise ValueError("initial inventory job channel identity is invalid")
        expected_job_scope = validate_channel_scopes(expected_channel_scopes,
            channel_ids=pending_channels)[0]
    summary = dict(enabled=True, prepared=0, claimed=0, processed=0, completed=0,
        continuations=0, playlist_not_found=0, inserted=0, already_matching=0,
        conflicts=0, errors=0, quota_exhausted=False, quota_deferred=0,
        quota_release_errors=0, provider_attempts=0, results=[], results_omitted=0,
        by_type={kind: dict(processed=0, inserted=0, errors=0) for kind in RUNTIME_VIDEO_TYPES})
    if not limit:
        return summary
    if prepare and not addressed:
        prepared = repository.prepare_runtime(limit=min(limit, 1000), enable=True)
        summary["prepared"] = int(prepared.get("selected_channels") or
            prepared.get("states_inserted") or prepared.get("prepared") or 0)
    effective_run_id = str(run_id or uuid_factory())
    if job_addressed:
        effective_run_id = str(uuid.UUID(effective_run_id))
    attempts_before = budget.attempts
    proofs = {}
    claims_processed = 0

    def remember(result):
        # Source payloads and cursor receipts belong to the page/job boundary,
        # not to a pass-wide telemetry array. The one addressed job needs its
        # receipt for its immediately following durable acknowledgement.
        sample = dict(result)
        if before_dispatch is None:
            sample.pop("receipt", None)
        if len(summary["results"]) < MAX_RESULT_EXAMPLES:
            summary["results"].append(sample)
        else:
            summary["results_omitted"] += 1

    def quota_defer(current, pending, error, result):
        owned = [current, *pending]
        try:
            reply = repository.defer_claims_after_daily_quota(owned,
                transition_id=error.transition_id, source_checked_at=error.source_checked_at)
            if (reply.get("status") != "deferred" or reply.get("active_scan_state_preserved") is not True
                    or reply.get("requested") != len(owned) or reply.get("released") != len(owned)
                    or reply.get("rejected") != 0):
                raise RuntimeError("inventory quota receipt mismatch")
            summary["quota_deferred"] += len(owned)
            result["claim_release_status"] = "quota_deferred"
        except Exception as failure:
            summary["errors"] += 1
            summary["quota_release_errors"] += 1
            summary["settlement_unknown"] = True
            result["claim_release_status"] = "error"
            result["claim_release_error_type"] = failure.__class__.__name__

    # The claim-operation allowance also stops a DB-only failure loop. It is
    # not charged as API quota, and does not discard or complete any scan.
    while budget.remaining and claims_processed < limit:
        if stop_at_boundary(summary):
            break
        window = min(budget.remaining, limit - claims_processed, MAX_CLAIM_WINDOW)
        if job_addressed:
            claims = repository.claim_initial_source(job_id=initial_job_id,
                job_lease_token=initial_job_lease_token, worker_id=worker_id,
                run_id=effective_run_id, lease_seconds=900)
            # Check before stop/release/begin: foreign authority must never be
            # adopted or released, even when a stop arrived during the claim.
            if any(claim.channel_id != expected_job_scope["channel_id"]
                    or claim.video_type != selected_types[0]
                    or claim.lease_channel_epoch != expected_job_scope["control_epoch"]
                    or claim.scan_kind not in {"initial_inventory", "full_reconciliation"}
                    for claim in claims):
                raise RuntimeError("initial inventory source claim scope mismatch")
        elif addressed and pending_channels:
            types = selected_types[:window]
            channel_count = max(window // len(types), 1)
            channels, pending_channels = pending_channels[:channel_count], pending_channels[channel_count:]
            claims = repository.claim_channels(channels, video_types=types,
                expected_channel_scopes=(None if expected_channel_scopes is None else
                    [scope for scope in expected_channel_scopes if scope["channel_id"] in channels]),
                worker_id=worker_id, run_id=effective_run_id, lease_seconds=900)
        else:
            if addressed and not proofs:
                break
            claims = repository.claim_pass(continuations=list(proofs.values()), limit=window,
                include_due=not addressed, worker_id=worker_id, run_id=effective_run_id,
                lease_seconds=900)
        if len(claims) > window:
            raise RuntimeError("inventory claim exceeded its bound")
        if not claims:
            if job_addressed:
                break
            if addressed and pending_channels:
                continue
            break
        summary["claimed"] += len(claims)
        stop = False
        for index, claim in enumerate(claims):
            pending = claims[index + 1:]
            if stop_at_boundary(summary):
                release_unstarted_playlists(repository, claims[index:], summary)
                stop = True
                break
            key = (claim.channel_id, claim.video_type)
            expected = proofs.pop(key, None)
            if expected is not None and (claim.lease_channel_epoch != expected["control_epoch"]
                    or claim.active_scan_id != expected["scan_id"]):
                summary["errors"] += 1
                summary["stopped_reason"] = "inventory_continuation_scope_changed"
                release_unstarted_playlists(repository, claims[index:], summary)
                stop = True
                break
            claims_processed += 1
            try:
                result = run_typed_inventory_claim(repository=repository, claim=claim,
                    fetch_page=fetch_page, page_budget=budget, transition_id=transition_id,
                    before_dispatch=before_dispatch, playlist_not_found_errors=playlist_not_found_errors,
                    require_observed_video_ids=job_addressed,
                    now=now, uuid_factory=uuid_factory)
                summary["processed"] += 1
                typed = summary["by_type"][claim.video_type]
                typed["processed"] += 1
                for count in ("inserted", "already_matching", "conflicts"):
                    summary[count] += int(result.get(count) or 0)
                typed["inserted"] += int(result.get("inserted") or 0)
                status = result.get("status")
                if status == "running":
                    summary["continuations"] += 1
                    if len(proofs) >= MAX_CONTINUATION_PROOFS:
                        proofs.pop(next(iter(proofs)))
                    proofs[key] = dict(channel_id=claim.channel_id, video_type=claim.video_type,
                        scan_id=result["scan_id"], control_epoch=claim.lease_channel_epoch,
                        last_transition_id=result["transition_id"])
                elif status == "completed":
                    summary["completed"] += 1
                elif status == "playlist_not_found":
                    summary["playlist_not_found"] += 1
                elif status == "error":
                    summary["errors"] += 1
                    typed["errors"] += 1
                elif status in {"await_receipt", "budget_exhausted"}:
                    summary.update(settlement_unknown=bool(result.get("settlement_unknown")),
                                   stopped_reason=status)
                    release_unstarted_playlists(repository, pending, summary)
                    stop = True
                if on_page is not None and status in {"completed", "running", "playlist_not_found", "error"}:
                    on_page(result)
                if copy_collection_stop(summary, result):
                    release_unstarted_playlists(repository, pending, summary)
                    stop = True
                remember(result)
            except _TypedInventoryDailyQuotaError as error:
                summary.update(quota_exhausted=True, stopped_reason="youtube_quota_exhausted")
                summary["processed"] += 1
                result = dict(channel_id=claim.channel_id, video_type=claim.video_type,
                    scan_kind=claim.scan_kind, status="quota_deferred",
                    error_type=error.provider_error_type, transition_id=error.transition_id,
                    provider_attempts=1, pages_processed=0, page_items=0)
                quota_defer(claim, pending, error, result)
                remember(result)
                stop = True
            except Exception as error:
                summary["errors"] += 1
                summary["by_type"][claim.video_type]["errors"] += 1
                result = dict(channel_id=claim.channel_id, video_type=claim.video_type,
                    scan_kind=claim.scan_kind, status="error", transition_id=transition_id,
                    error_type=getattr(error, "provider_error_type", None) or error.__class__.__name__)
                denied = admission_denial(error)
                if denied is not None:
                    summary.update(infrastructure_error=True, hot_runtime_interrupted=True,
                                   stopped_reason="hot_runtime_interrupted")
                    # A prior write or the source-free release may lack its ACK.
                    # Keep that uncertainty through the initial-job adapter.
                    if (getattr(denied, "outcome_may_be_committed", False)
                            or getattr(denied, "control_abort_failed", False)):
                        summary["settlement_unknown"] = True
                    if not (getattr(denied, "control_abort_attempted", False)
                            or getattr(denied, "outcome_may_be_committed", False)):
                        pending = [claim, *pending]
                else:
                    # Neither a source-free release nor a new failure can erase
                    # an unconfirmed page/failed-page acknowledgement.
                    summary.update(settlement_unknown=True, stopped_reason="inventory_outcome_unknown")
                release_unstarted_playlists(repository, pending, summary)
                remember(result)
                stop = True
            summary["provider_attempts"] = budget.attempts - attempts_before
            if stop:
                break
        if stop:
            break
    summary["provider_attempts"] = budget.attempts - attempts_before
    summary["budget_exhausted"] = not budget.remaining
    return summary
