"""Client, normalization, and comparison helpers for videos.batchGetStats.

Persistence and scheduler routing remain owned by the collection services.
"""

from youtube_client import youtube_api_get
from youtube_source_fields import youtube_counter_integer
from youtube_errors import classify_youtube_outcome, youtube_response_error


BATCH_STATS_QUOTA_BUCKET = "video_batch_get_stats"
BATCH_STATS_ENDPOINT = "videos:batchGetStats"
BATCH_STATS_MAX_IDS = 50
BATCH_STATS_PILOT_MAX_IDS = BATCH_STATS_MAX_IDS
BATCH_STATS_ALLOWED_PARTS = {
    "contentDetails",
    "id",
    "snippet",
    "statistics",
}
BATCH_STATS_COUNTER_PARTS = (
    "id",
    "statistics",
)
BATCH_STATS_PILOT_PARTS = (
    "id",
    "statistics",
    "snippet",
    "contentDetails",
)
BATCH_STATS_ERROR_CODES = frozenset({
    "api_error_response",
    "batch_stats_error",
    "duplicate_item_ids",
    "invalid_failed_ids_shape",
    "invalid_items_shape",
    "invalid_payload_shape",
    "invalid_summary_shape",
    "quota_exhausted",
    "succeeded_failed_overlap",
    "summary_count_mismatch",
    "unaccounted_requested_ids",
    "unrequested_failed_ids",
    "unrequested_item_ids",
})
# Provider refusals after which no further Batch request should be sent in a run.
BATCH_STATS_QUOTA_REASONS = frozenset({
    "dailyLimitExceeded",
    "quotaExceeded",
})
BATCH_STATS_SAFE_DIAGNOSTIC_COUNT_KEYS = frozenset({
    "accounted_unique_count",
    "failed_id_count",
    "http_status",
    "input_requested_count",
    "items_count",
    "missing_requested_count",
    "overlap_count",
    "summary_failed_count",
    "summary_requested_count",
    "summary_succeeded_count",
    "unique_item_id_count",
    "unrequested_failed_count",
    "unrequested_item_count",
})
BATCH_STATS_SAFE_DIAGNOSTIC_TYPE_KEYS = frozenset({
    "failed_ids_type",
    "items_type",
    "payload_type",
    "summary_type",
})
BATCH_STATS_SAFE_TYPE_NAMES = frozenset({
    "NoneType",
    "bool",
    "dict",
    "float",
    "int",
    "list",
    "str",
})


class YouTubeBatchStatsError(RuntimeError):
    def __init__(self, message, *, code="batch_stats_error", diagnostics=None):
        super().__init__(message)
        normalized_code = str(code or "batch_stats_error")
        self.code = (
            normalized_code
            if normalized_code in BATCH_STATS_ERROR_CODES
            else "batch_stats_error"
        )
        self.diagnostics = {}
        for key, value in dict(diagnostics or {}).items():
            if key in BATCH_STATS_SAFE_DIAGNOSTIC_COUNT_KEYS:
                if value is None or type(value) is int:
                    self.diagnostics[key] = value
                continue
            if (
                key in BATCH_STATS_SAFE_DIAGNOSTIC_TYPE_KEYS
                and value in BATCH_STATS_SAFE_TYPE_NAMES
            ):
                self.diagnostics[key] = value


def is_batch_stats_quota_error(error):
    """True only for an explicit daily Batch quota refusal."""
    return isinstance(error, YouTubeBatchStatsError) and error.code == "quota_exhausted"


def provider_quota_unavailable(http_status, payload):
    """Compatibility predicate: explicit daily evidence only."""
    outcome = classify_youtube_outcome(http_status, payload, quota_bucket=BATCH_STATS_QUOTA_BUCKET)
    return outcome is not None and outcome.kind == "daily_quota"


def unique_video_ids(video_ids):
    result = []
    seen = set()
    for value in video_ids or []:
        video_id = str(value or "").strip()
        if video_id and video_id not in seen:
            seen.add(video_id)
            result.append(video_id)
    return result


def int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_parts(parts):
    normalized = []
    seen = set()
    for value in parts or []:
        part = str(value or "").strip()
        if not part or part in seen:
            continue
        if part not in BATCH_STATS_ALLOWED_PARTS:
            raise ValueError(f"Unsupported videos.batchGetStats part: {part}")
        seen.add(part)
        normalized.append(part)
    if not normalized:
        raise ValueError("At least one videos.batchGetStats part is required")
    return normalized


def normalize_batch_stats_item(item):
    statistics = item.get("statistics") or {}
    snippet = item.get("snippet") or {}
    content_details = item.get("contentDetails") or {}
    return {
        "id": str(item.get("id") or "").strip(),
        "views": youtube_counter_integer(statistics.get("viewCount")),
        "like_count": youtube_counter_integer(statistics.get("likeCount")),
        "comment_count": youtube_counter_integer(statistics.get("commentCount")),
        "published_at": snippet.get("publishTime"),
        "duration": content_details.get("duration"),
        "duration_ms": int_or_none(content_details.get("durationMillis")),
    }


def normalize_batch_stats_response(payload, requested_video_ids):
    requested_video_ids = unique_video_ids(requested_video_ids)
    requested_set = set(requested_video_ids)

    if not isinstance(payload, dict):
        raise YouTubeBatchStatsError(
            "videos.batchGetStats returned a non-object payload",
            code="invalid_payload_shape",
            diagnostics={"payload_type": type(payload).__name__},
        )

    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list) or any(
        not isinstance(item, dict) for item in raw_items
    ):
        raise YouTubeBatchStatsError(
            "videos.batchGetStats returned invalid items",
            code="invalid_items_shape",
            diagnostics={
                "items_type": type(raw_items).__name__,
                "items_count": len(raw_items) if isinstance(raw_items, list) else None,
            },
        )

    items = [normalize_batch_stats_item(item) for item in raw_items]
    item_ids = [item["id"] for item in items]
    item_id_set = set(item_ids)
    diagnostics = {
        "input_requested_count": len(requested_video_ids),
        "items_count": len(items),
        "unique_item_id_count": len(item_id_set),
    }

    if len(item_ids) != len(item_id_set):
        raise YouTubeBatchStatsError(
            "videos.batchGetStats returned duplicate item IDs",
            code="duplicate_item_ids",
            diagnostics=diagnostics,
        )

    unrequested_item_count = len(item_id_set - requested_set)
    if unrequested_item_count:
        raise YouTubeBatchStatsError(
            "videos.batchGetStats returned an unrequested item ID",
            code="unrequested_item_ids",
            diagnostics={
                **diagnostics,
                "unrequested_item_count": unrequested_item_count,
            },
        )

    raw_summary = payload.get("summary")
    if raw_summary is None:
        summary_source = "derived_from_items"
        failed_ids = [
            video_id
            for video_id in requested_video_ids
            if video_id not in item_id_set
        ]
        requested_count = len(requested_video_ids)
        succeeded_count = len(items)
        failed_count = len(failed_ids)
    else:
        if not isinstance(raw_summary, dict):
            raise YouTubeBatchStatsError(
                "videos.batchGetStats returned an invalid summary",
                code="invalid_summary_shape",
                diagnostics={
                    **diagnostics,
                    "summary_type": type(raw_summary).__name__,
                },
            )
        summary_source = "api"
        raw_failed_ids = raw_summary.get("failedVideoIds")
        if raw_failed_ids is None:
            raw_failed_ids = []
        if not isinstance(raw_failed_ids, list):
            raise YouTubeBatchStatsError(
                "videos.batchGetStats returned invalid failedVideoIds",
                code="invalid_failed_ids_shape",
                diagnostics={
                    **diagnostics,
                    "failed_ids_type": type(raw_failed_ids).__name__,
                },
            )
        failed_ids = unique_video_ids(raw_failed_ids)
        requested_count = int_or_none(raw_summary.get("requestedVideoCount"))
        succeeded_count = int_or_none(raw_summary.get("succeededVideoCount"))
        failed_count = int_or_none(raw_summary.get("failedVideoCount"))

        diagnostics = {
            **diagnostics,
            "failed_id_count": len(failed_ids),
            "summary_requested_count": requested_count,
            "summary_succeeded_count": succeeded_count,
            "summary_failed_count": failed_count,
        }
        expected_counts = {
            "requestedVideoCount": len(requested_video_ids),
            "succeededVideoCount": len(items),
            "failedVideoCount": len(failed_ids),
        }
        actual_counts = {
            "requestedVideoCount": requested_count,
            "succeededVideoCount": succeeded_count,
            "failedVideoCount": failed_count,
        }
        if actual_counts != expected_counts:
            raise YouTubeBatchStatsError(
                "videos.batchGetStats summary counts do not match the response",
                code="summary_count_mismatch",
                diagnostics=diagnostics,
            )

        failed_id_set = set(failed_ids)
        unrequested_failed_count = len(failed_id_set - requested_set)
        if unrequested_failed_count:
            raise YouTubeBatchStatsError(
                "videos.batchGetStats returned an unrequested failed ID",
                code="unrequested_failed_ids",
                diagnostics={
                    **diagnostics,
                    "unrequested_failed_count": unrequested_failed_count,
                },
            )

        overlap_count = len(item_id_set.intersection(failed_id_set))
        if overlap_count:
            raise YouTubeBatchStatsError(
                "videos.batchGetStats returned one ID as both succeeded and failed",
                code="succeeded_failed_overlap",
                diagnostics={**diagnostics, "overlap_count": overlap_count},
            )

    failed_id_set = set(failed_ids)
    accounted_id_set = item_id_set.union(failed_id_set)
    if accounted_id_set != requested_set:
        raise YouTubeBatchStatsError(
            "videos.batchGetStats did not account for every requested ID",
            code="unaccounted_requested_ids",
            diagnostics={
                **diagnostics,
                "failed_id_count": len(failed_ids),
                "accounted_unique_count": len(accounted_id_set),
                "missing_requested_count": len(requested_set - accounted_id_set),
            },
        )

    return {
        "items": items,
        "summary": {
            "source": summary_source,
            "requested_count": requested_count,
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "failed_ids": failed_ids,
        },
    }

def fetch_video_batch_stats(
    video_ids,
    *,
    api_key,
    attempt_recorder,
    timeout_seconds=20,
    phase="batch_stats_pilot",
    parts=BATCH_STATS_PILOT_PARTS,
):
    """Fetch one bounded batch from the endpoint-specific quota bucket."""
    video_ids = unique_video_ids(video_ids)
    if not video_ids:
        raise ValueError("At least one video ID is required")
    if len(video_ids) > BATCH_STATS_MAX_IDS:
        raise ValueError(
            "videos.batchGetStats is limited to "
            f"{BATCH_STATS_MAX_IDS} IDs"
        )
    parts = normalize_parts(parts)

    response = youtube_api_get(
        BATCH_STATS_ENDPOINT,
        phase=phase,
        quota_bucket=BATCH_STATS_QUOTA_BUCKET,
        attempt_recorder=attempt_recorder,
        params={
            "part": ",".join(parts),
            "id": ",".join(video_ids),
        },
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        quota_units=1,
    )
    http_status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    # Only fixed messages: provider error text never enters the exception.
    if provider_quota_unavailable(http_status, payload):
        raise YouTubeBatchStatsError(
            "YouTube videos.batchGetStats daily quota exhausted",
            code="quota_exhausted",
            diagnostics={"http_status": http_status},
        )
    refusal = youtube_response_error(response, payload, quota_bucket=BATCH_STATS_QUOTA_BUCKET)
    if refusal is not None and getattr(refusal, "code", None) == "provider_rate_limited":
        raise refusal
    if isinstance(payload, dict) and payload.get("error"):
        raise YouTubeBatchStatsError(
            "YouTube videos.batchGetStats returned an API error",
            code="api_error_response",
            diagnostics={"http_status": http_status},
        )
    response.raise_for_status()
    from youtube_response_trace import provider_response_observed
    try:
        normalized = normalize_batch_stats_response(payload, video_ids)
    except Exception:
        provider_response_observed(video_ids, payload, endpoint=BATCH_STATS_ENDPOINT,
                                   include_statistics="statistics" in parts, normalization_failed=True)
        raise
    provider_response_observed(video_ids, payload, endpoint=BATCH_STATS_ENDPOINT,
                               include_statistics="statistics" in parts)
    return normalized
