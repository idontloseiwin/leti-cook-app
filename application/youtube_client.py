"""Shared transport for official YouTube Data API requests.

The client centralizes HTTP transport and observe-only quota accounting.
An explicitly bound collector control scope also checks durable provider bans;
discovery's local application budgets remain with its separate admission owner.

Every request names a reviewed quota bucket and ledger endpoint label, so a
refusal is always classifiable and an attempt is never counted as "other".
 ``credential=None`` is the installation key
context; an ``OwnerBearer`` request never carries or marks that key.
``api_family`` picks one of three fixed bases: the Data API, and the
owner-only YouTube Analytics and YouTube Reporting APIs. No other host is
possible, and the Analytics/Reporting bases accept only an owner token.
"""

import time
import os
import math
import re
from types import MappingProxyType
from datetime import datetime, timezone

import requests

from telemetry import emit_event
import time_trust
import pipeline_trace as trace
from operational_copy_policy import QUOTA_LABELS
from provider_boundary import PROVIDER_FORBIDDEN, provider_forbidden_active
from provider_credentials import OwnerBearer
from youtube_errors import OUTCOME_BUCKETS, classify_youtube_outcome, key_refusal_code


YOUTUBE_DATA_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_DATA_API_UPLOAD_BASE_URL = "https://www.googleapis.com/upload/youtube/v3"
VIDEO_BATCH_STATS_ENDPOINT = "videos:batchGetStats"
VIDEO_BATCH_STATS_QUOTA_BUCKET = "video_batch_get_stats"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The only API bases a request can reach. The Analytics and Reporting
# APIs never accept the installation key, so those bases require an owner token.
API_BASE_URLS = MappingProxyType({
    "data": YOUTUBE_DATA_API_BASE_URL,
    "analytics": "https://youtubeanalytics.googleapis.com/v2",
    "reporting": "https://youtubereporting.googleapis.com/v1",
})
OWNER_ONLY_API_FAMILIES = frozenset({"analytics", "reporting"})
# A relative endpoint path: registry paths with substituted, already checked IDs. A percent escape
# (a quoted "=" or "+" of an ID) is allowed; an escaped "/" or "." never is.
_ENDPOINT = re.compile(r"(?:[A-Za-z0-9_.:~/-]|%(?![2][EeFf])[0-9A-Fa-f]{2}){1,1100}\Z")


class YouTubeResponseTooLarge(requests.RequestException):
    """A fixed failure without provider content or request credentials."""


def _remaining(timeout, deadline, cancelled):
    if cancelled is not None and cancelled.is_set():
        from provider_admission import AdmissionCancelled
        raise AdmissionCancelled('provider_request_cancelled')
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise requests.Timeout('provider_request_deadline')
    return remaining


def _read_bounded_response(response, maximum, deadline, cancelled, clock_started):
    """Cache bounded decoded bytes for the existing response.json() callers."""
    try:
        # Closing remains guaranteed if clock validation itself refuses a response.
        time_trust.record_date(clock_started, response_date_header(response), source="youtube_api")
        try:
            length = response.headers.get('Content-Length')
        except Exception:
            length = None  # decoded-byte counting remains authoritative
        if isinstance(length, str) and length.isascii() and length.isdecimal() and int(length) > maximum:
            raise YouTubeResponseTooLarge('provider_response_too_large')
        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            _remaining(1, deadline, cancelled)
            if len(content) + len(chunk) > maximum:
                raise YouTubeResponseTooLarge('provider_response_too_large')
            content.extend(chunk)
        _remaining(1, deadline, cancelled)
        response._content = bytes(content)
        response._content_consumed = True
    finally:
        response.close()


def emit_event_safely(*args, **kwargs):
    """Telemetry must never change a transport result."""
    try:
        return emit_event(*args, **kwargs)
    except Exception:
        return None


def emit_observer_failure(operation, phase, error):
    emit_event_safely(
        "observer",
        "youtube_client",
        "telemetry",
        operation,
        phase=phase,
        direction="internal",
        status="error",
        detail=error.__class__.__name__,
    )


def start_attempt_safely(
    attempt_recorder,
    *,
    phase,
    endpoint,
    quota_bucket,
    quota_units,
    retry_of_request_attempt_id,
):
    if attempt_recorder is None:
        return None
    try:
        return attempt_recorder.start_attempt(
            phase=phase,
            endpoint=endpoint,
            quota_bucket=quota_bucket,
            quota_units=quota_units,
            retry_of_request_attempt_id=retry_of_request_attempt_id,
        )
    except Exception as error:
        emit_observer_failure("youtube_quota_ledger_start", phase, error)
        return None


def finish_attempt_safely(
    attempt_recorder,
    request_attempt_id,
    *,
    phase,
    status,
    duration_ms,
    http_status=None,
    error_type=None,
):
    if attempt_recorder is None or request_attempt_id is None:
        return False
    try:
        return attempt_recorder.finish_attempt(
            request_attempt_id,
            status=status,
            phase=phase,
            duration_ms=duration_ms,
            http_status=http_status,
            error_type=error_type,
        )
    except Exception as error:
        emit_observer_failure("youtube_quota_ledger_finish", phase, error)
        return False


def response_date_header(response):
    """Only Google's Date header string, for the clock check; never fails."""
    try:
        value = response.headers.get("Date")
    except Exception:
        return None
    return value if type(value) is str else None


def response_http_status(response):
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _youtube_part_names(value):
    if value is None:
        return frozenset()
    values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
    return frozenset(
        part.strip().casefold()
        for item in values
        for part in str(item or "").split(",")
        if part.strip()
    )


def _validate_video_request_boundary(endpoint, quota_bucket, request_params, *, initial_profile=False, complete_profile=False):
    normalized_endpoint = endpoint.casefold()
    normalized_batch_endpoint = VIDEO_BATCH_STATS_ENDPOINT.casefold()
    parts = _youtube_part_names(request_params.get("part"))

    if not isinstance(initial_profile, bool):
        raise TypeError("initial_profile must be an explicit bool")
    if not isinstance(complete_profile, bool):
        raise TypeError("complete_profile must be an explicit bool")
    if (initial_profile or complete_profile) and (
        normalized_endpoint != "videos" or quota_bucket != "general"
        or parts not in (frozenset({"snippet", "contentdetails", "status", "topicdetails",
                               "livestreamingdetails", "statistics"}),
                         frozenset({"snippet", "contentdetails", "status", "topicdetails",
                               "livestreamingdetails", "statistics", "paidproductplacementdetails"}))
    ):
        raise ValueError("Complete profiles require the exact General video parts")
    if normalized_endpoint == "videos" and "statistics" in parts and not (initial_profile or complete_profile):
        raise ValueError("videos.list requests must not include the statistics part")
    if (
        normalized_endpoint == normalized_batch_endpoint
        and quota_bucket != VIDEO_BATCH_STATS_QUOTA_BUCKET
    ):
        raise ValueError(
            "videos:batchGetStats requires the video_batch_get_stats quota bucket"
        )
    if (
        normalized_endpoint == "videos"
        and quota_bucket == VIDEO_BATCH_STATS_QUOTA_BUCKET
    ):
        raise ValueError(
            "videos.list must not use the video_batch_get_stats quota bucket"
        )


def valid_metadata_etag(value):
    """Bound one opaque YouTube metadata tag; reject header injection and tag lists.

    YouTube matches If-None-Match only against the bare tag from its response
    body: the same tag in quotes always gets a full 200.
    """
    return (isinstance(value, str) and 1 <= len(value) <= 1024
            and all(33 <= ord(char) <= 126 and char not in '",' for char in value))


def youtube_api_get(
    endpoint,
    *,
    phase,
    quota_bucket,
    attempt_recorder,
    params=None,
    api_key=None,
    timeout_seconds=20,
    quota_units=1,
    retry_of_request_attempt_id=None,
    if_none_match=None,
    initial_profile=False,
    complete_profile=False,
    deadline=None,
    cancelled=None,
    max_response_bytes=MAX_RESPONSE_BYTES,
    credential=None,
    endpoint_label=None,
    api_family="data",
):
    """Send one GET request to the official YouTube Data, Analytics or Reporting API.

    Attempt start/outcome are journaled before/after transport and delivered in
    batches. Recording failures are observed and never replace a provider result.
    ``endpoint_label`` is the ledger/trace name when it differs from the path.
    The response carries ``lic_request_attempt_id``; an exception raised after
    the HTTP attempt started carries it too, with ``lic_http_attempted``.
    """
    if provider_forbidden_active():
        # A stored-data query reached the transport: nothing is admitted or sent.
        from provider_admission import AdmissionUnavailable
        raise AdmissionUnavailable(PROVIDER_FORBIDDEN)

    endpoint = str(endpoint or "").strip("/")
    if not endpoint:
        raise ValueError("YouTube API endpoint is required")
    if (_ENDPOINT.fullmatch(endpoint) is None or "//" in endpoint
            or any(segment in (".", "..") for segment in endpoint.split("/"))):
        raise ValueError("YouTube API endpoint is not a plain relative path")
    if not isinstance(api_family, str) or api_family not in API_BASE_URLS:
        raise ValueError("provider_api_family_invalid")
    label = endpoint if endpoint_label is None else endpoint_label
    if not isinstance(label, str) or label not in QUOTA_LABELS["endpoint"]:
        raise ValueError("YouTube API endpoint label is not reviewed")

    phase = str(phase or "").strip()
    if not phase:
        raise ValueError("YouTube API phase is required")

    quota_bucket = str(quota_bucket or "").strip()
    if not quota_bucket:
        raise ValueError("YouTube API quota_bucket is required")
    if quota_bucket not in OUTCOME_BUCKETS:
        raise ValueError("YouTube API quota_bucket is not reviewed")

    quota_units = int(quota_units)
    if quota_units <= 0:
        raise ValueError("YouTube API quota_units must be positive")

    if credential is not None and (not isinstance(credential, OwnerBearer) or api_key is not None):
        # An owner token never travels with, or instead of, the installation key.
        raise ValueError("provider_credential_invalid")
    if api_family in OWNER_ONLY_API_FAMILIES and credential is None:
        # The installation key is never sent to the owner-only APIs.
        raise ValueError("provider_api_family_needs_owner_credential")

    request_params = dict(params or {})
    _validate_video_request_boundary(endpoint, quota_bucket, request_params,
                                     initial_profile=initial_profile, complete_profile=complete_profile)
    if "key" in request_params:
        raise ValueError("The YouTube API key must not be sent as a query parameter")

    headers = {}
    if if_none_match is not None:
        # Only metadata validators; never arbitrary headers or source logging.
        if endpoint not in {"videoCategories", "i18nLanguages"}:
            raise ValueError("Invalid conditional YouTube metadata endpoint")
        if not valid_metadata_etag(if_none_match):
            raise ValueError("Invalid conditional YouTube metadata ETag")
        headers["If-None-Match"] = if_none_match

    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0 or (deadline is not None and
                (type(deadline) not in (int, float) or not math.isfinite(deadline)))
            or (cancelled is not None and not callable(getattr(cancelled, 'is_set', None)))
            or type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES):
        raise ValueError('provider_request_bounds_invalid')
    from provider_runtime import admit_request, request_deadline
    deadline = request_deadline(deadline)
    wait = _remaining(30, deadline, cancelled)
    with trace.scope("provider_request", endpoint=label, quota_bucket=quota_bucket,
                     **_request_trace_count(request_params)) as observation:
        trace.event("provider_waiting", "started")
        with admit_request(timeout=wait, cancel=cancelled) as cancellation:
            trace.event("provider_admitted", "ok")
            # Queue waiting creates no attempt. Recheck the deadline, HOT and key
            # after admission before starting the single HTTP attempt.
            timeout = _remaining(timeout_seconds, deadline, cancellation)
            response = _send_request(endpoint, request_params, headers, api_key=api_key,
                phase=phase, quota_bucket=quota_bucket, quota_units=quota_units,
                attempt_recorder=attempt_recorder, retry_of_request_attempt_id=retry_of_request_attempt_id,
                timeout_seconds=timeout, deadline=min(deadline, time.monotonic()+timeout)
                    if deadline is not None else time.monotonic()+timeout,
                cancelled=cancellation, max_response_bytes=max_response_bytes,
                credential=credential, endpoint_label=label, api_family=api_family)
            _observe_admission_response(cancellation, response, quota_bucket)
            observation.finish("completed", http_status=response_http_status(response))
            return response


def _request_trace_count(params):
    """Observe only the number of requested IDs, never parameters or page tokens."""
    value = params.get("id")
    if isinstance(value, str):
        count = len(value.split(",")) if value else 0
        if 0 <= count <= 50:
            return {"requested_count": count}
    return {}


def _observe_admission_response(cancellation, response, quota_bucket):
    """One bounded local observation; classification never sends another request."""
    if cancellation is None:
        return
    status = response_http_status(response)
    rate_seconds = None
    if status is not None and status >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = None
        try:
            retry_after = response.headers.get('Retry-After')
        except Exception:
            retry_after = None
        try:
            outcome = classify_youtube_outcome(status, payload, quota_bucket=quota_bucket,
                retry_after=retry_after, now=datetime.now(timezone.utc))
        except ValueError:
            # An unclassifiable answer (for example a status outside 100-599)
            # is reported to the gate as a failure, never as a lost response.
            outcome = None
        if outcome is not None and outcome.kind == 'rate_limit':
            rate_seconds = outcome.retry_after_seconds or 1
    cancellation.observe(success=status is not None and 200 <= status < 400,
                         rate_seconds=rate_seconds)


def _send_request(endpoint, request_params, headers, *, api_key, phase, quota_bucket,
                  quota_units, attempt_recorder, retry_of_request_attempt_id,
                  timeout_seconds, deadline, cancelled, max_response_bytes,
                  credential=None, endpoint_label=None, api_family="data"):

    from hot_runtime import require_hot_runtime_ready
    require_hot_runtime_ready(operation="provider")
    label = endpoint if endpoint_label is None else endpoint_label
    key = None
    if credential is None:
        # Installation key context: the same key and refusal marking as collection.
        key = api_key
        if os.getenv('LIC_DEPLOYMENT_PROFILE') == 'self-hosted' and os.getenv('LIC_SETUP_DIRECTORY'):
            from self_hosted_configuration import provider_key
            key = provider_key(api_key)
        # The key travels in a header, never in the URL, so request URLs,
        # transport exception messages and response.url cannot contain it.
        if key:
            headers["X-Goog-Api-Key"] = key
    else:
        # Owner context: only the owner's token; the installation key is neither
        # read nor sent, so its refusal state cannot change.
        headers["Authorization"] = credential.authorization_header()

    timeout_seconds = _remaining(timeout_seconds, deadline, cancelled)

    if credential is None:
        # After concurrency/HOT/key admission, before the optional journal or
        # HTTP. The control transaction ends before this request starts.
        from quota_buckets import before_request
        before_request(quota_bucket)
        timeout_seconds = _remaining(timeout_seconds, deadline, cancelled)

    request_attempt_id = start_attempt_safely(
        attempt_recorder,
        phase=phase,
        endpoint=label,
        quota_bucket=quota_bucket,
        quota_units=quota_units,
        retry_of_request_attempt_id=retry_of_request_attempt_id,
    )
    clock_started = time_trust.request_started()
    transport_started = time.perf_counter()
    trace.event("http_started", "started", request_attempt_id=request_attempt_id,
                retry_of_request_attempt_id=retry_of_request_attempt_id,
                endpoint=label, quota_bucket=quota_bucket,
                **_request_trace_count(request_params))

    from list_observation import http_start
    http_start(quota_bucket, quota_units, retry_of_request_attempt_id)
    try:
        response = requests.get(
            f"{API_BASE_URLS[api_family]}/{endpoint}",
            params=request_params,
            headers=headers,
            timeout=timeout_seconds,
            # Requests would forward the key header to any redirect target.
            allow_redirects=False,
            stream=True,
        )
        _read_bounded_response(response, max_response_bytes, deadline, cancelled, clock_started)
    except Exception as error:
        duration_ms = round((time.perf_counter() - transport_started) * 1000)
        trace.event("http_finished", "error", request_attempt_id=request_attempt_id,
                    reason="transport_error", duration_ms=duration_ms)
        recorded = finish_attempt_safely(
            attempt_recorder,
            request_attempt_id,
            phase=phase,
            status="transport_error",
            duration_ms=duration_ms,
            error_type=error.__class__.__name__,
        )
        trace.event("quota_accounting", "confirmed" if recorded else "unknown",
                    request_attempt_id=request_attempt_id)
        try:
            # The attempt started: its units are spent even though no answer came.
            error.lic_request_attempt_id = request_attempt_id
            error.lic_http_attempted = True
        except Exception:
            pass
        raise
    else:
        http_status = response_http_status(response)
        refusal_type = _ledger_refusal_type(response, http_status, quota_bucket)
        if credential is None and refusal_type is not None:
            # Memory latch and synchronous operational write precede every
            # optional outcome observation. A failed write retains the latch
            # and is retried by the control scope; the provider result survives.
            from quota_buckets import note_provider_refusal
            note_provider_refusal(quota_bucket)
        redirected = getattr(response, "is_redirect", False) is True
        status = (
            "http_error"
            if redirected or (http_status is not None and http_status >= 400)
            else "success"
        )
        duration_ms = round((time.perf_counter() - transport_started) * 1000)
        trace.event("http_finished", "error" if status == "http_error" else "received",
                    request_attempt_id=request_attempt_id, duration_ms=duration_ms,
                    http_status=http_status, **_failure_observation(response, http_status))
        recorded = finish_attempt_safely(
            attempt_recorder,
            request_attempt_id,
            phase=phase,
            status=status,
            duration_ms=duration_ms,
            http_status=http_status,
            error_type=refusal_type,
        )
        trace.event("quota_accounting", "confirmed" if recorded else "unknown",
                    request_attempt_id=request_attempt_id)
        try:
            # The caller may cite the ledger row of this answer.
            response.lic_request_attempt_id = request_attempt_id
        except Exception:
            pass
        if redirected:
            # The target is neither followed nor reported: it may be any host.
            error = requests.HTTPError(
                f"Unexpected redirect from the YouTube Data API (HTTP {http_status}).",
                response=response,
            )
            error.lic_request_attempt_id = request_attempt_id
            error.lic_http_attempted = True
            raise error
        if credential is None and key and http_status in (400, 401, 403):
            note_key_refusal(response, http_status, key)
        return response


def _ledger_refusal_type(response, http_status, quota_bucket):
    """The fixed ledger label of a daily quota refusal, or None.

    This label is observational; operational admission uses its separate owner.
    No provider text is stored; other refusals stay unlabelled.
    """
    if type(http_status) is not int or http_status < 400:
        return None
    try:
        outcome = classify_youtube_outcome(http_status, response.json(), quota_bucket=quota_bucket)
    except Exception:
        return None
    if outcome is None or outcome.kind != "daily_quota":
        return None
    from quota_buckets import QUOTA_REFUSAL_ERROR_TYPE
    return QUOTA_REFUSAL_ERROR_TYPE


def _failure_observation(response, http_status):
    """Finite passive codes only; never retain provider text or affect a request."""
    if type(http_status) is not int or http_status < 500:
        return {}
    fields = {"reason": "provider_server_error"}
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        errors = error.get("errors") if isinstance(error, dict) else None
        if isinstance(errors, list):
            known = {"internalError": "provider_internal_error", "backendError": "provider_backend_error"}
            reasons = {known[item["reason"]] for item in errors[:10]
                       if isinstance(item, dict) and isinstance(item.get("reason"), str)
                       and item["reason"] in known}
            if len(reasons) == 1:
                fields["reason"] = reasons.pop()
    except Exception:
        pass
    return fields


def note_key_refusal(response, http_status, key):
    """A refusal of the key itself stops every later request until set-key (self-hosted only)."""
    if os.getenv('LIC_DEPLOYMENT_PROFILE') != 'self-hosted' or not os.getenv('LIC_SETUP_DIRECTORY'):
        return
    try:
        code = key_refusal_code(http_status, response.json())
    except Exception:
        return
    if code:
        from self_hosted_configuration import record_key_refusal
        record_key_refusal(key, code)
