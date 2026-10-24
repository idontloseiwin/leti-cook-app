"""Shared YouTube error identities and pure response/refusal classification."""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class YouTubeAPIError(RuntimeError):
    pass


class YouTubePlaylistNotFoundError(RuntimeError):
    pass


class YouTubeDailyQuotaExceededError(YouTubeAPIError):
    """The provider explicitly reported exhausted daily project quota."""


class YouTubeRateLimitError(YouTubeAPIError):
    """A temporary provider refusal; never evidence of daily exhaustion."""
    code = error_type = "provider_rate_limited"

    def __init__(self, *, retry_after_seconds=None):
        super().__init__(self.code)
        self.retry_after_seconds = (
            min(60, max(1, retry_after_seconds))
            if type(retry_after_seconds) is int else 60
        )


def youtube_error_message(response, default="YouTube API error"):
    error = response.get("error") or {}
    message = error.get("message") or default
    status = error.get("status")
    reason = None
    errors = error.get("errors") or []
    if errors:
        reason = errors[0].get("reason")

    details = [message]
    if status:
        details.append(f"status={status}")
    if reason:
        details.append(f"reason={reason}")
    return " | ".join(details)


def is_playlist_not_found_response(response):
    error = response.get("error") or {}
    reasons = [
        (item.get("reason") or "").lower()
        for item in error.get("errors", [])
        if isinstance(item, dict)
    ]
    return "playlistnotfound" in reasons


def is_playlist_not_found_error(error):
    """True when YouTube said the playlist does not exist (HTTP 404, playlistNotFound).

    Decided from the refusal itself, not only from the exception class: a channel
    without Shorts or streams has no such list, which is an absent source, never
    an unknown size.
    """
    if isinstance(error, YouTubePlaylistNotFoundError):
        return True
    if error.__class__.__name__ == "YouTubePlaylistNotFoundError":
        return True
    value = str(error or "").lower()
    return "reason=playlistnotfound" in value


# Refusals about the key itself, never about one channel, list or video. An
# invalid key answers 400 with the generic reason badRequest, so the
# ErrorInfo detail (API_KEY_INVALID) is read as well.
KEY_REFUSALS = ("provider_key_rejected", "provider_access_denied")
KEY_REJECTED_REASONS = frozenset({"keyInvalid", "keyExpired", "API_KEY_INVALID", "API_KEY_EXPIRED"})
ACCESS_DENIED_REASONS = frozenset({
    "accessNotConfigured", "ipRefererBlocked", "SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED",
    "API_KEY_IP_ADDRESS_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_ANDROID_APP_BLOCKED",
    "API_KEY_IOS_APP_BLOCKED",
})
DAILY_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})


def refusal_reasons(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return set()
    items = [item for field in ("errors", "details") if isinstance(error.get(field), list)
             for item in error[field] if isinstance(item, dict)]
    return {item["reason"] for item in items if isinstance(item.get("reason"), str)}


def key_refusal_code(status, payload):
    """provider_key_rejected or provider_access_denied when YouTube refused the key; otherwise None."""
    reasons = refusal_reasons(payload)
    if status == 401 or (status in (400, 403) and reasons & KEY_REJECTED_REASONS):
        return "provider_key_rejected"
    if status == 403 and reasons & ACCESS_DENIED_REASONS:
        return "provider_access_denied"
    return None


def provider_refusal_code(status, payload):
    """One fixed class for a refused answer: the key, access, the daily quota, or a passing failure.

    A rate limit passes by itself, so it counts as a passing failure. None is a
    refusal about the request itself (for example one private resource).
    """
    outcome = classify_youtube_outcome(status, payload, quota_bucket="general")
    if outcome is None or outcome.kind == "resource_missing":
        return None
    return (outcome.key_code if outcome.kind == "key_refused" else
            "provider_quota_exceeded" if outcome.kind == "daily_quota" else
            "provider_unavailable")


def youtube_error_reason(response):
    error = response.get("error") or {}
    errors = error.get("errors") or []
    if not isinstance(errors, list):
        return None
    for item in errors:
        if isinstance(item, dict) and item.get("reason"):
            return str(item["reason"])
    return None


def is_youtube_daily_quota_exhausted(error, *, is_batch_stats_quota_error):
    """Only explicit daily exhaustion; temporary rate refusals stay separate."""
    value = str(error or "").lower()
    return (
        "reason=quotaexceeded" in value
        or "reason=dailylimitexceeded" in value
        or error.__class__.__name__ == "YouTubeDailyQuotaExceededError"
        or is_batch_stats_quota_error(error)
    )


def youtube_rate_limit_retry_after(error):
    """Safe delay for a known rate refusal, including legacy rendered reasons."""
    value = str(error or "").lower()
    if (isinstance(error, YouTubeDailyQuotaExceededError)
            or "reason=quotaexceeded" in value or "reason=dailylimitexceeded" in value):
        return None
    if (value == "provider_rate_limited" or isinstance(error, YouTubeRateLimitError)
            or getattr(error, "code", None) == "provider_rate_limited"
            or "reason=ratelimitexceeded" in value
            or "reason=userratelimitexceeded" in value):
        delay = getattr(error, "retry_after_seconds", None)
        return min(60, max(1, delay)) if type(delay) is int else 60
    return None


RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
MISSING_RESOURCE_REASONS = {
    "playlistNotFound": "playlist", "channelNotFound": "channel", "videoNotFound": "video",
}
OUTCOME_BUCKETS = frozenset({"general", "video_batch_get_stats"})
MAX_RETRY_AFTER_SECONDS = 60


@dataclass(frozen=True)
class ProviderOutcome:
    kind: str
    quota_bucket: str
    retry_after_seconds: int | None = None
    key_code: str | None = None
    resource: str | None = None


def bounded_retry_after(value, *, now=None):
    """Pure bounded Retry-After parsing; HTTP dates require an explicit clock."""
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return min(int(value), MAX_RETRY_AFTER_SECONDS)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return None
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            return None
        seconds = (moment - now.astimezone(timezone.utc)).total_seconds()
        return min(MAX_RETRY_AFTER_SECONDS, max(0, math.ceil(seconds)))
    except (TypeError, ValueError, OverflowError):
        return None


def classify_youtube_outcome(status, payload=None, *, quota_bucket, retry_after=None, now=None):
    """Return one fixed refusal kind, or None for success/unclassified requests.

    Explicit daily evidence wins over rate evidence. RESOURCE_EXHAUSTED alone
    never proves a daily reset. Generic 404 does not prove an absent playlist:
    callers need resource='playlist' and their existing exact source checks.
    No body, message, reason text, key, identity or URL is returned.
    """
    if not isinstance(quota_bucket, str) or quota_bucket not in OUTCOME_BUCKETS:
        raise ValueError("youtube_outcome_bucket_invalid")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError("youtube_outcome_status_invalid")
    if status is not None and status < 400:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    reasons = set()
    for field in ("errors", "details"):
        values = error.get(field)
        if isinstance(values, list):
            for value in values[:32]:
                reason = value.get("reason") if isinstance(value, dict) else None
                if isinstance(reason, str) and len(reason) <= 128:
                    reasons.add(reason)
    if status == 401 or (status in (400, 403) and reasons & KEY_REJECTED_REASONS):
        return ProviderOutcome("key_refused", quota_bucket, key_code="provider_key_rejected")
    if status == 403 and reasons & ACCESS_DENIED_REASONS:
        return ProviderOutcome("key_refused", quota_bucket, key_code="provider_access_denied")
    if status is not None and reasons & DAILY_QUOTA_REASONS:
        return ProviderOutcome("daily_quota", quota_bucket)
    delay = bounded_retry_after(retry_after, now=now)
    if (status == 429 or (status is not None and (reasons & RATE_LIMIT_REASONS
            or error.get("code") == 429 or error.get("status") == "RESOURCE_EXHAUSTED"))):
        return ProviderOutcome("rate_limit", quota_bucket, retry_after_seconds=delay)
    if status == 404:
        resources = {resource for reason, resource in MISSING_RESOURCE_REASONS.items() if reason in reasons}
        return ProviderOutcome("resource_missing", quota_bucket,
                               resource=next(iter(resources)) if len(resources) == 1 else None)
    if status is None or status in (408, 425) or status >= 500:
        return ProviderOutcome("transient", quota_bucket, retry_after_seconds=delay)
    return None


def youtube_response_error(response, payload, *, quota_bucket="general"):
    """Map a single captured refusal to a safe typed error, without retrying."""
    status = getattr(response, "status_code", None)
    if type(status) is not int:
        status = None
    if status is None and isinstance(payload, dict) and not payload.get("error"):
        return None
    headers = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    outcome = classify_youtube_outcome(status, payload, quota_bucket=quota_bucket,
        retry_after=retry_after, now=datetime.now(timezone.utc))
    if outcome is None or outcome.kind == "resource_missing":
        return None
    if outcome.kind == "daily_quota":
        return YouTubeDailyQuotaExceededError("provider_quota_exceeded")
    if outcome.kind == "rate_limit":
        return YouTubeRateLimitError(retry_after_seconds=outcome.retry_after_seconds)
    error = YouTubeAPIError(outcome.key_code or "provider_unavailable")
    error.code = outcome.key_code or "provider_unavailable"
    return error
