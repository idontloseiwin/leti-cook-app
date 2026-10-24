"""Provider-only reconnect reads for the supervised collection worker.

request is a caller-owned, single-attempt transport. It must enforce the passed
byte cap, timeout and cancellation/deadline again after any admission wait.
It returns an HTTP response or a fixed ProviderOutcome. It must not retry, read
repositories, or enter database transactions. runtime.collection binds the
shared GET transport; this resolver owns no cache, repository or lifecycle.

UTC stamps are taken conservatively immediately before each provider read,
never when the coordinator later binds the result. A tracked-ID merge belongs
to a future coordinator preflight; this resolver does not inspect local state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time

from requests.exceptions import RequestException

from channel_inventory_estimate import COUNT_KINDS, COUNT_MAXIMUM
from channel_reconnect_outcome import ReconnectResponse, reconnect_refusal
from typed_playlist import typed_playlist_id
from user_channel_input import parse_user_channel_input, UnsupportedLegacyChannelLink
from youtube_client import response_http_status
from youtube_errors import (
    ProviderOutcome, YouTubeAPIError, YouTubeDailyQuotaExceededError, YouTubeRateLimitError,
    classify_youtube_outcome,
)
from .preparation import initial_channel_status, prepared_channel_row

MAX_RESPONSE_BYTES = 1024 * 1024


class _Refused(Exception):
    def __init__(self, response):
        super().__init__("reconnect_source_refused")
        self.response = response


def _identity(value):
    _, kind, parsed = parse_user_channel_input(value)
    if kind != "id" or parsed != value:
        raise ValueError("reconnect_source_identity_invalid")
    return parsed


def _single_item(payload, missing_code, *, expected_kind):
    if (not isinstance(payload, dict) or "error" in payload
            or ("kind" in payload and payload["kind"] != expected_kind)):
        raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
    if "pageInfo" in payload:
        page = payload["pageInfo"]
        if (not isinstance(page, dict) or type(page.get("totalResults")) is not int
                or page["totalResults"] != len(items)
                or ("resultsPerPage" in page and (type(page["resultsPerPage"]) is not int
                    or page["resultsPerPage"] < 0))):
            raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
    # An omitted items field proves absence only in the expected zero-result
    # list envelope; arbitrary objects and explicit null items remain transient.
    if "items" not in payload and (payload.get("kind") != expected_kind or "pageInfo" not in payload):
        raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
    if "nextPageToken" in payload or "prevPageToken" in payload:
        raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
    if not items:
        raise _Refused(ReconnectResponse(code=missing_code))
    if len(items) != 1 or not isinstance(items[0], dict):
        raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
    return items[0]


def _count(payload):
    page = payload.get("pageInfo") if isinstance(payload, dict) else None
    value = page.get("totalResults") if isinstance(page, dict) else None
    if isinstance(value, str) and 1 <= len(value) <= 10 and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 <= value <= COUNT_MAXIMUM:
        raise _Refused(ReconnectResponse(code="initial_inventory_count_unavailable"))
    return value


class ReconnectSource:
    """Callable suitable for ReconnectUnit.resolve_channel; at most five reads."""
    def __init__(self, request, *, metadata_update_values, timeout_seconds=10,
                 clock=time.monotonic, utc_clock=lambda: datetime.now(timezone.utc)):
        if (not all(callable(value) for value in (request, metadata_update_values, clock, utc_clock))
                or type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30):
            raise ValueError("reconnect_source_config_invalid")
        self.request, self.metadata_update_values = request, metadata_update_values
        self.timeout_seconds, self.clock, self.utc_clock = timeout_seconds, clock, utc_clock

    def __call__(self, locator, *, cancelled, deadline):
        if (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= 0
                or not callable(getattr(cancelled, "is_set", None))):
            raise ValueError("reconnect_source_budget_invalid")
        sent = 0

        def active():
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise ValueError("reconnect_source_clock_invalid")
            if cancelled.is_set() or now >= deadline:
                # Partial source reads are never described as an unsent request.
                raise _Refused(ReconnectResponse(code="provider_unavailable", retry_seconds=1)
                               if sent else ReconnectResponse(dispatched=False))
            return deadline - now

        def read(resource, params, phase):
            nonlocal sent
            remaining = active()
            observed = self.utc_clock()
            if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("reconnect_source_clock_invalid")
            observed = observed.astimezone(timezone.utc)
            remaining = active()
            sent += 1
            try:
                response = self.request(
                    resource, params=params, phase=phase, quota_bucket="general", quota_units=1,
                    timeout_seconds=min(self.timeout_seconds, remaining), deadline=deadline,
                    cancelled=cancelled, max_response_bytes=MAX_RESPONSE_BYTES,
                )
            except YouTubeDailyQuotaExceededError:
                raise _Refused(ReconnectResponse(code="provider_quota_exceeded")) from None
            except YouTubeRateLimitError as error:
                raise _Refused(ReconnectResponse(code="provider_rate_limited",
                                               retry_seconds=error.retry_after_seconds)) from None
            except (YouTubeAPIError, TimeoutError, ConnectionError, RequestException):
                raise _Refused(ReconnectResponse(code="provider_unavailable", retry_seconds=60)) from None
            if isinstance(response, ProviderOutcome):
                if response.quota_bucket != "general":
                    raise ValueError("reconnect_provider_outcome_invalid")
                return None, observed, response
            status = response_http_status(response)
            if status is not None and not 100 <= status <= 599:
                status = None
            try:
                payload = response.json()
            except Exception:
                payload = None
            headers = getattr(response, "headers", None)
            retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
            outcome = classify_youtube_outcome(
                status, payload, quota_bucket="general", retry_after=retry_after, now=observed)
            if outcome is not None:
                return None, observed, outcome
            if status != 200:
                raise _Refused(ReconnectResponse(code="channel_lookup_unavailable"))
            active()
            return payload, observed, None

        def require_success(outcome):
            if outcome is not None:
                raise _Refused(reconnect_refusal(outcome))

        try:
            active()
            try:
                user_input, kind, value = parse_user_channel_input(locator)
            except UnsupportedLegacyChannelLink:
                return ReconnectResponse(code="unsupported_legacy_channel_link")
            except ValueError:
                return ReconnectResponse(code="needs_input")

            if kind == "video":
                payload, _, outcome = read("videos", {"part": "snippet", "id": value},
                                           "channel_resolve_video")
                if outcome is not None and outcome.kind == "resource_missing" and outcome.resource == "video":
                    return ReconnectResponse(code="channel_not_found")
                require_success(outcome)
                item = _single_item(payload, "channel_not_found", expected_kind="youtube#videoListResponse")
                try:
                    value = _identity(item["snippet"]["channelId"])
                except (KeyError, TypeError, ValueError):
                    return ReconnectResponse(code="channel_lookup_unavailable")
                kind = "id"

            params = {"part": "snippet,statistics,contentDetails,brandingSettings",
                      "id" if kind == "id" else "forHandle": value}
            payload, observed, outcome = read("channels", params, "add_channel")
            require_success(outcome)
            channel = _single_item(payload, "channel_not_found", expected_kind="youtube#channelListResponse")
            try:
                identity = _identity(channel["id"])
                if ((kind == "id" and identity != value)
                        or not isinstance(channel.get("statistics"), dict)
                        or not isinstance(channel.get("snippet"), dict)):
                    return ReconnectResponse(code="channel_lookup_unavailable")
            except (KeyError, TypeError, ValueError):
                return ReconnectResponse(code="channel_lookup_unavailable")

            counts, statuses, stamps = {}, {}, {}
            for video_type in COUNT_KINDS:
                playlist = typed_playlist_id(identity, video_type)
                payload, counted_at, outcome = read(
                    "playlistItems", {"part": "id", "playlistId": playlist, "maxResults": 1,
                                      "fields": "pageInfo(totalResults)"},
                    "channel_inventory_count")
                if outcome is not None and outcome.kind == "resource_missing" and outcome.resource == "playlist":
                    counts[video_type], statuses[video_type] = None, "playlist_not_found"
                else:
                    require_success(outcome)
                    counts[video_type], statuses[video_type] = _count(payload), "ok"
                stamps[video_type] = counted_at.isoformat()
            estimate = {"scope": "all", "video_types": list(COUNT_KINDS), "counts": counts,
                        "statuses": statuses, "checked_at": min(stamps.values()),
                        "checked_at_by_type": stamps}
            try:
                status = initial_channel_status(channel, estimate)
                row = prepared_channel_row(channel, estimate, status, observed_at=observed,
                                           metadata_update_values=self.metadata_update_values)
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                return ReconnectResponse(code="initial_inventory_count_unavailable")
            active()
            row["user_input"] = user_input
            return ReconnectResponse(channel=row)
        except _Refused as refusal:
            return refusal.response
