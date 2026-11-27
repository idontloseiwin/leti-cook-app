"""Execution of one plan in bounded request memory.

Requests run one after another; before each one: cancellation, deadline, the
request/unit/row/output budgets and the daily cap. There are no retries: a
refusal stops the acquisition with a fixed reason (partial), and a repeat is a
new explicit action (optionally with the returned continuation). Nothing is
stored; the only write is the quota attempt row the transport journals.

Analytics pages by ``startIndex``; ``members.list`` ``mode=updates``
returns one poll and a continuation; media methods read one bounded body
(captions) or a two-request chain (Reporting: report metadata, then the CSV
from the checked ``downloadUrl``, which never leaves this function). Owner
refusals: 401 ``owner_auth_expired`` (the installation key is never marked),
403 without a needed scope ``owner_scope_insufficient``, 403 about the target
(not the owner's object) ``access_denied``, quota ``quota_exhausted:<bucket>``.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import math
import time

import pipeline_trace as trace
from youtube_errors import MISSING_RESOURCE_REASONS, classify_youtube_outcome, refusal_reasons
from youtube_source_fields import parse_dt

from . import transport
from .errors import PlanRejected, ResponseInvalid
from .families import policy
from .families.hooks import hook_for
from .pagination import CHAIN_START, SearchPageHistory, chain, next_page_token, poll_position
from .parse import decode_json, parse_page
from .results import AcquisitionResult, Completeness, EntityState, PageProvenance, frozen_mapping

OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
DENIED_REASONS = frozenset({"subscriptionForbidden", "playlistItemsNotAccessible", "watchHistoryNotAccessible",
                            "watchLaterNotAccessible", "forbidden",
                            # owner families: the object is not the authorized owner's (live reference pages)
                            "insufficientLivePermissions", "liveStreamingNotEnabled"})
# A token without a needed scope (Google ErrorInfo / legacy reason): the owner must grant more access.
SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
# Owner families: documented "not found" reasons of a named object.
OWNER_NOT_FOUND_REASONS = frozenset({"captionNotFound", "liveChatNotFound", "liveBroadcastNotFound",
                                     "liveStreamNotFound", "notFound"})
DISABLED_REASONS = frozenset({"commentsDisabled", "channelMembershipsNotEnabled"})
# Stops after which the same plan may continue later from the returned position.
RESUMABLE = frozenset({"budget_requests", "budget_units", "budget_rows", "output_budget", "deadline", "cancelled",
                       "transient", "rate_limited", "daily_cap", "provider_busy", "live_chat_open",
                       "updates_poll"})
MAX_PAGE_NOTES = 8


def utc_now():
    return datetime.now(timezone.utc)


def refusal_outcome(status, payload, *, plan, retry_after, now):
    """Table 3.5: (stop code or None, entity state or None, retry seconds)."""
    reasons = refusal_reasons(payload)
    owner = getattr(plan, "owner", False)
    outcome = classify_youtube_outcome(status, payload, quota_bucket=plan.quota_bucket,
                                       retry_after=retry_after, now=now)
    if outcome is not None and outcome.kind == "key_refused":
        if owner and status == 401:
            return "owner_auth_expired", None, None       # the installation key is never marked
        return ("credential_access_denied" if outcome.key_code == "provider_access_denied"
                else "owner_auth_expired" if owner else "credential_rejected"), None, None
    if outcome is not None and outcome.kind == "daily_quota":
        return f"quota_exhausted:{plan.quota_bucket}", None, None
    if outcome is not None and outcome.kind == "rate_limit":
        return "rate_limited", None, outcome.retry_after_seconds
    method_id = getattr(plan, "method_id", None)
    special = policy.TARGET_REFUSALS.get(method_id, {})
    for reason in sorted(reasons & set(special)):
        return None, EntityState(special[reason][0]), None
    stops = policy.STOP_REASONS.get(method_id, {})
    for reason in sorted(reasons & set(stops)):
        return stops[reason], None, None
    if status == 404:
        if outcome is not None and outcome.resource in set(MISSING_RESOURCE_REASONS.values()):
            return None, EntityState.NOT_FOUND, None
        options = getattr(plan, "options", {}) or {}
        if owner and (reasons & OWNER_NOT_FOUND_REASONS or "path_target" in options or "chain" in options):
            return None, EntityState.NOT_FOUND, None      # the one object the request names
        return "not_found_unclassified", None, None
    if status in (400, 403) and reasons & DISABLED_REASONS:
        return None, EntityState.DISABLED, None
    if status == 403 and reasons & DENIED_REASONS:
        return None, EntityState.ACCESS_DENIED, None
    if owner and status == 403 and reasons & SCOPE_REASONS:
        return "owner_scope_insufficient", None, None
    if outcome is not None and outcome.kind == "transient":
        return "transient", None, outcome.retry_after_seconds
    if status == 400 and plan.api == "analytics":
        return "combination_unsupported", None, None
    if status == 400 and "couldNotConvert" in reasons:
        return "caption_not_convertible", None, None
    return "request_invalid", None, None


def _target_parameter(plan, payload):
    """The parameter a documented method-specific refusal names (policy.TARGET_REFUSALS), or None."""
    special = policy.TARGET_REFUSALS.get(plan.method_id, {})
    for reason in sorted(refusal_reasons(payload) & set(special)):
        return special[reason][1]
    return None


def _targets(method, plan, planned, parameter=None):
    """Keys of the entities a refusal is about: requested IDs, or the filter by name."""
    if parameter is not None:
        value = dict(planned.params).get(parameter)
        if value:
            return tuple(value.split(","))
    if plan.strategy == "id_batch":
        return planned.targets
    parameter = method.parameters.get(plan.filter_param) if plan.filter_param else None
    if (parameter is not None and parameter.identity) or plan.filter_param in policy.CHAIN_TARGETS.values():
        return plan.filter_values
    return (plan.filter_param or plan.method_id,)


def _response_date(value):
    moment = parse_dt(value) if value else None
    if moment is None:
        try:
            from email.utils import parsedate_to_datetime
            moment = parsedate_to_datetime(value) if value else None
        except (TypeError, ValueError, IndexError):
            moment = None
    if moment is None or moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


def _parse(method, plan, payload, *, page_index, hook, targets):
    if method.id == "youtubeAnalytics.reports.query":
        from .owner_sources import parse_report_page
        return parse_report_page(method, plan, payload, page_index=page_index)
    return parse_page(method, plan, payload, page_index=page_index, hook=hook, targets=targets)


def acquire(plan, *, registry, credential, cancelled, deadline, attempt_recorder, api_key=None, cap=None,
            codec=None, continuation=None, send=transport.send, clock=utc_now, monotonic=time.monotonic):
    """Run ``plan``; ``deadline`` is a ``time.monotonic()`` instant. Returns AcquisitionResult."""
    if not plan.executable:
        raise PlanRejected("method_not_executable")
    if plan.registry_revision != registry.revision:
        raise PlanRejected("plan_changed")
    if plan.strategy == "media":
        if continuation is not None:
            raise PlanRejected("continuation_invalid")      # a media body is read whole or not at all
        return _acquire_media(plan, registry=registry, credential=credential, cancelled=cancelled,
                              deadline=deadline, attempt_recorder=attempt_recorder, api_key=api_key, cap=cap,
                              send=send, clock=clock, monotonic=monotonic)
    method = registry.methods[plan.method_id]
    hook = hook_for(plan.method_id)
    started = clock()
    position = (codec.decode(continuation, fingerprint=plan.fingerprint, context=plan.context, now=started)
                if continuation is not None else {})
    batch_index = int(position.get("next", 0)) if plan.strategy == "id_batch" else 0
    token = position.get("token") if plan.strategy in ("page_token", "poll") else None
    search_history = SearchPageHistory(position) if plan.method_id == "youtube.search.list" else None
    if search_history is not None:
        token = search_history.resume_token(position)
    not_before = position.get("not_before") if plan.strategy == "poll" else None
    if isinstance(not_before, (int, float)) and started.timestamp() < not_before:
        # The provider asks for pollingIntervalMillis between polls; an early poll is refused unsent.
        raise PlanRejected("continuation_too_early")
    start_index = int(position.get("start", 1)) if plan.strategy == "start_index" else 1
    chain_value = position.get("chain", CHAIN_START)
    page_index = int(position.get("page", 0))
    seen_tokens = {token} if token else set()

    rows, pages, entities, cost = [], [], {}, Counter()
    seen_rows, duplicates, output_bytes, sent = set(), 0, 0, 0
    stop, retry_after, finished, notes = None, None, False, ()
    page_notes, poll_wait_ms = [], None
    requested_ids = set(plan.filter_values) if plan.tracks_requested_ids else set()
    returned_ids = set()
    page_param = method.pagination.page_size_param
    bucket, units = plan.quota_bucket, plan.quota_units

    while True:
        if plan.strategy == "id_batch" and batch_index >= len(plan.requests):
            finished = True
            break
        if cancelled.is_set():
            stop = "cancelled"
            break
        if monotonic() >= deadline:
            stop = "deadline"
            break
        if sent >= plan.max_requests or (plan.strategy in ("page_token", "start_index") and sent >= plan.max_pages):
            stop = "budget_requests"
            break
        if cost[bucket] + units > plan.worst_cost.get(bucket, 0):
            stop = "budget_units"
            break
        if output_bytes >= OUTPUT_LIMIT_BYTES:
            stop = "output_budget"
            break
        if len(rows) >= plan.max_rows:
            stop = "budget_rows"
            break
        if cap is not None and not cap.allows(bucket, units):
            stop = "daily_cap"
            break
        planned = plan.requests[batch_index] if plan.strategy == "id_batch" else plan.requests[0]
        params = dict(planned.params)
        if plan.parts:
            params["part"] = plan.part_param
        if plan.fields_mask:
            params["fields"] = plan.fields_mask
        if plan.strategy == "page_token":
            if page_param and plan.page_size:
                params[page_param] = str(plan.page_size)
            if token:
                params[method.pagination.token_param] = token
        elif plan.strategy == "start_index":
            params["maxResults"] = str(plan.page_size)
            params["startIndex"] = str(start_index)
        elif plan.strategy == "poll":
            if plan.page_size is not None:
                params[page_param] = str(plan.page_size)
            if token:
                params[method.pagination.token_param] = token
        request_started = clock()
        try:
            exchange = send(plan, params, credential=credential, cancelled=cancelled, deadline=deadline,
                            attempt_recorder=attempt_recorder, api_key=api_key)
        except transport.TransportStop as error:
            if error.attempted:
                sent += 1
                cost[bucket] += units
                if cap is not None:
                    cap.record(bucket, units, error.attempt_id)
            stop = error.code
            break
        except BaseException as error:
            if getattr(error, "lic_http_attempted", False) is True and cap is not None:
                cap.record(bucket, units, getattr(error, "lic_request_attempt_id", None))
            raise
        sent += 1
        cost[bucket] += units
        if cap is not None:
            cap.record(bucket, units, exchange.attempt_id)
        output_bytes += len(exchange.content)
        provenance = dict(method_id=plan.method_id, registry_revision=plan.registry_revision,
                          plan_fingerprint=plan.fingerprint, page_index=page_index, token_chain_sha256=chain_value,
                          parts=plan.parts, fields_mask_sha256=hashlib.sha256(plan.fields_mask.encode()).hexdigest(),
                          request_started_at=request_started, response_date=_response_date(exchange.date_header),
                          http_status=exchange.status, quota_bucket=bucket, quota_units=units,
                          request_attempt_id=exchange.attempt_id, context=plan.context)
        if not 200 <= exchange.status < 300:
            try:
                payload = decode_json(exchange.content)
            except ResponseInvalid:
                payload = None
            code, state, retry_after = refusal_outcome(exchange.status, payload, plan=plan,
                                                       retry_after=exchange.retry_after, now=clock())
            pages.append(PageProvenance(**provenance))
            if state is not None:
                parameter = _target_parameter(plan, payload)
                if state is EntityState.NOT_FOUND and plan.strategy == "id_batch" and parameter is None:
                    state = EntityState.OMITTED     # "one or more" IDs of the batch: which one is not said
                for target in _targets(method, plan, planned, parameter):
                    entities[target] = state
                if rows or (plan.strategy == "id_batch" and batch_index > 0):
                    stop = "target_" + state.value
                else:
                    finished = True
                break
            stop = code
            break
        try:
            payload = decode_json(exchange.content)
            page = _parse(method, plan, payload, page_index=page_index, hook=hook,
                          targets=planned.targets if plan.strategy == "id_batch" else tuple(requested_ids))
            failed, summary_reported = (hook.batch_accounting(payload, planned.targets)
                                        if plan.strategy == "id_batch" else (frozenset(), False))
            if plan.method_id == "youtube.search.list" and plan.page_size == 0:
                if page.item_count:
                    raise ResponseInvalid("response_page_size_invalid")
                estimate = (payload.get("pageInfo") or {}).get("totalResults")
                if estimate is not None and estimate > 2**31 - 1:
                    raise ResponseInvalid("response_page_info_invalid")
                provenance["estimated_total_results"] = estimate
            next_token = None
            if plan.strategy == "page_token":
                next_token = next_page_token(payload, page.item_count, seen_tokens,
                                             allow_empty=search_history is not None or bool(plan.options.get("poll")))
                if search_history is not None and next_token is not None:
                    search_history.observe(next_token, page_index + 1)
            polled = poll_position(payload) if plan.strategy == "poll" else None
            for note in hook.page_notes(method, payload):
                if note not in page_notes and len(page_notes) < MAX_PAGE_NOTES:
                    page_notes.append(note)
        except ResponseInvalid as error:
            pages.append(PageProvenance(**provenance))
            stop = "response_invalid"
            notes = (error.code,)
            break
        pages.append(PageProvenance(**provenance, unknown_field_paths=page.unknown_field_paths,
                                    rejected_items=page.rejected_items))
        for row in page.rows:
            if row.identity is not None and row.identity in seen_rows:
                duplicates += 1          # the same resource again on a later page
                continue
            if row.identity is not None:
                seen_rows.add(row.identity)
            rows.append(row)
        returned_ids.update(page.returned_ids)
        if plan.strategy == "id_batch":
            for target in planned.targets:
                entities[target] = (EntityState.PRESENT if target in page.returned_ids else
                                    EntityState.FAILED if summary_reported and target in failed else EntityState.OMITTED)
            batch_index += 1
            page_index += 1
            continue
        if polled is not None:
            next_token, poll_wait_ms, offline = polled
            if offline:
                finished = True             # offlineAt: the chat went offline, nothing more will come
            else:
                stop = "live_chat_open"     # a live chat never ends by itself; poll again later
                retry_after = None if poll_wait_ms is None else math.ceil(poll_wait_ms / 1000)
                if next_token is not None:
                    token = next_token
                    chain_value = chain(chain_value, token)
                    page_index += 1
                else:
                    token = None
            break
        if plan.filter_param in policy.SINGLE_RESULT_FILTERS.get(plan.method_id, ()):
            key = plan.filter_values[0] if plan.filter_values else plan.filter_param
            entities[key] = EntityState.PRESENT if page.rows else EntityState.NOT_FOUND
        if plan.strategy == "start_index":
            if page.item_count < plan.page_size:
                finished = True
                break
            start_index += plan.page_size
            page_index += 1
            continue
        if plan.strategy == "single" or next_token is None:
            finished = True
            break
        token = next_token
        chain_value = chain(chain_value, token)
        page_index += 1
        if plan.options.get("poll"):
            stop = "updates_poll"     # members.list mode=updates: the token is the next poll, never "complete"
            break

    if finished and plan.tracks_requested_ids and plan.strategy != "id_batch":
        # An ID list read page by page: what never came back is omitted (batches account per batch).
        for target in requested_ids:
            if target in returned_ids:
                entities[target] = EntityState.PRESENT
            elif target not in entities:      # a documented refusal state (not_found, ...) is kept
                entities[target] = EntityState.OMITTED
    rows, finish_notes = hook.finish_result(method, tuple(rows))
    if not rows and finished and plan.method_id == "youtubeAnalytics.reports.query" and not entities:
        finish_notes = tuple(finish_notes) + ("no_data",)
    notes = tuple(notes) + tuple(finish_notes)
    continuation_token = None
    if not finished and stop in RESUMABLE and codec is not None:
        issued = clock()
        eligible_at = None
        if plan.strategy == "id_batch" and batch_index < len(plan.requests):
            next_position = {"next": batch_index, "page": page_index}
        elif plan.strategy == "page_token":
            next_position = search_history.position(token) if search_history is not None else {"token": token}
            next_position.update(chain=chain_value, page=page_index)
        elif plan.strategy == "start_index":
            next_position = {"start": start_index, "page": page_index}
        elif plan.strategy == "poll" and token is not None:
            eligible_at = issued + timedelta(milliseconds=poll_wait_ms or 0)
            next_position = {"token": token, "chain": chain_value, "page": page_index,
                             "not_before": eligible_at.timestamp()}
        else:
            next_position = None
        if next_position is not None:
            continuation_token = codec.encode(fingerprint=plan.fingerprint, context=plan.context,
                                              position=next_position, now=issued, eligible_at=eligible_at)
    result = AcquisitionResult(
        method_id=plan.method_id, unit=plan.row_unit, rows=tuple(rows), entities=frozen_mapping(entities),
        pages=tuple(pages), completeness=Completeness.COMPLETE if finished else Completeness.PARTIAL,
        stop_reason=None if finished else stop, order=plan.order, cost=frozen_mapping(cost),
        continuation=continuation_token, retry_after_seconds=retry_after, plan_fingerprint=plan.fingerprint,
        requests_sent=sent, duplicate_rows_dropped=duplicates, notes=tuple(notes) + tuple(page_notes))
    trace.event("explicit_acquisition", result.completeness.value, endpoint=plan.ledger_endpoint_label,
                quota_bucket=bucket, count=len(rows), reason=result.stop_reason)
    return result


def _acquire_media(plan, *, registry, credential, cancelled, deadline, attempt_recorder, api_key, cap, send,
                   clock, monotonic):
    """captions.download (one body) or the Reporting chain: report metadata, then its CSV.

    The downloadUrl of the first answer is checked (exact host and /v1/media/ path) and used only
    here: it is never put in a row, a note, provenance, a continuation or a log.
    """
    from .owner_sources import read_report_csv, report_resource_name
    from .owner_sources import read_caption_file
    method = registry.methods[plan.method_id]
    bucket, units = plan.quota_bucket, plan.quota_units
    rows, pages, entities, cost, notes = [], [], {}, Counter(), ()
    stop, retry_after, finished, sent = None, None, False, 0
    chained = plan.options.get("reader") == "report_csv"
    steps = [("metadata", plan.options["first_endpoint"], plan.options["first_label"])] if chained else []
    steps.append(("media", None, None))
    endpoint = None
    planned = plan.requests[0]
    for kind, step_endpoint, label in steps:
        if cancelled.is_set():
            stop = "cancelled"
            break
        if monotonic() >= deadline:
            stop = "deadline"
            break
        if sent >= plan.max_requests or cost[bucket] + units > plan.worst_cost.get(bucket, 0):
            stop = "budget_requests"
            break
        if cap is not None and not cap.allows(bucket, units):
            stop = "daily_cap"
            break
        params = {} if kind == "metadata" else dict(planned.params)
        if chained and kind == "media":
            params["alt"] = "media"
        request_started = clock()
        try:
            exchange = send(plan, params, credential=credential, cancelled=cancelled, deadline=deadline,
                            attempt_recorder=attempt_recorder, api_key=api_key,
                            endpoint=step_endpoint if kind == "metadata" else endpoint, label=label)
        except transport.TransportStop as error:
            if error.attempted:
                sent += 1
                cost[bucket] += units
                if cap is not None:
                    cap.record(bucket, units, error.attempt_id)
            stop = error.code
            break
        except BaseException as error:
            if getattr(error, "lic_http_attempted", False) is True and cap is not None:
                cap.record(bucket, units, getattr(error, "lic_request_attempt_id", None))
            raise
        sent += 1
        cost[bucket] += units
        if cap is not None:
            cap.record(bucket, units, exchange.attempt_id)
        provenance = PageProvenance(
            method_id=plan.method_id, registry_revision=plan.registry_revision, plan_fingerprint=plan.fingerprint,
            page_index=len(pages), token_chain_sha256=CHAIN_START, parts=(),
            fields_mask_sha256=hashlib.sha256(b"").hexdigest(), request_started_at=request_started,
            response_date=_response_date(exchange.date_header), http_status=exchange.status, quota_bucket=bucket,
            quota_units=units, request_attempt_id=exchange.attempt_id, context=plan.context)
        pages.append(provenance)
        if not 200 <= exchange.status < 300:
            try:
                payload = decode_json(exchange.content)
            except ResponseInvalid:
                payload = None
            code, state, retry_after = refusal_outcome(exchange.status, payload, plan=plan,
                                                       retry_after=exchange.retry_after, now=clock())
            if state is not None:
                for target in plan.filter_values:
                    entities[target] = state
                finished = True
            else:
                stop = code
            break
        try:
            if kind == "metadata":
                report = decode_json(exchange.content)
                if (not isinstance(report, dict) or "error" in report or report.get("id") != plan.filter_values[0]
                        or report.get("jobId") != plan.options["chain"]["jobId"]):
                    raise ResponseInvalid("report_identity_invalid")
                endpoint = "media/" + report_resource_name(report.get("downloadUrl"))
                continue
            if chained:
                page, truncated = read_report_csv(method, plan, exchange.content, page_index=0)
            else:
                page, truncated = read_caption_file(method, plan, exchange.content, page_index=0), False
        except ResponseInvalid as error:
            stop, notes = "response_invalid", (error.code,)
            break
        pages[-1] = PageProvenance(**{**provenance.__dict__, "unknown_field_paths": page.unknown_field_paths})
        rows.extend(page.rows)
        if chained and not rows:
            notes = ("no_data",)          # a day without data: only the header row
        if truncated:
            stop = "budget_rows"
        else:
            finished = True
            for target in plan.filter_values:
                entities[target] = EntityState.PRESENT
    result = AcquisitionResult(
        method_id=plan.method_id, unit=plan.row_unit, rows=tuple(rows), entities=frozen_mapping(entities),
        pages=tuple(pages), completeness=Completeness.COMPLETE if finished else Completeness.PARTIAL,
        stop_reason=None if finished else stop, order="provider", cost=frozen_mapping(cost), continuation=None,
        retry_after_seconds=retry_after, plan_fingerprint=plan.fingerprint, requests_sent=sent,
        duplicate_rows_dropped=0, notes=tuple(notes))
    trace.event("explicit_acquisition", result.completeness.value, endpoint=plan.ledger_endpoint_label,
                quota_bucket=bucket, count=len(rows), reason=result.stop_reason)
    return result
