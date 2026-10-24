"""Application service for leased video counter and metadata collection.

Selection, leases, retries, and atomic persistence live in database RPCs.  This
module only routes claimed work to the appropriate YouTube endpoint and hands
the result back to the repository with the lease token.
"""

from __future__ import annotations
from collection_shutdown import stop_at_boundary

import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from db import VideoCollectionClaim, VideoCollectionRepository
from collection_outcome import collection_outcome_fields
from collection_policy import STATS_AVAILABILITY_CHECK_REQUIRED
from youtube_source_fields import youtube_duration_seconds
from availability import AVAILABILITY_UNKNOWN_UNAVAILABLE
from collection.video_payloads import (
    build_video_identity_payload, build_video_metadata_payload, build_video_optional_metadata,
    VIDEO_TITLE_MAX_CHARS, VideoMetadataValidationError,
)
from collection.provider_steps import ProviderStep, run_provider_steps
from collection.video_trace import claims_selected, metadata_validation_failed, write as trace_write
from pipeline_trace import bind, capture, event, run as trace_run
from youtube_errors import is_youtube_daily_quota_exhausted, youtube_rate_limit_retry_after


def _source_observation(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("video_source_observation_required")
    return value.astimezone(timezone.utc)


MAX_ACTIVE_CLAIMS = 50
DEFAULT_LEASE_SECONDS = 900
VIEW_COUNT_ABSENT_RETRY_SECONDS = 86400
TYPED_COLLECTION_BATCH_SIZE = 50
TYPED_RECONCILE_LIMIT = 1000
TYPED_DEFAULT_MAX_DURATION_SECONDS = 300
TYPED_VIDEO_TYPES = frozenset({"short", "stream"})
TYPED_COLLECTION_MODES = {
    "short": frozenset({"hydrate", "metadata", "counters", "identity_refresh"}),
    "stream": frozenset({"hydrate", "metadata", "counters", "identity_refresh", "stream_lifecycle"}),
}


class _MetadataValidationFailure(ValueError):
    """A closed diagnostic code; the existing queue failure stays unchanged."""

    def __init__(self, reason, *, payload_error=False):
        super().__init__(reason)
        self.reason = reason
        self.payload_error = payload_error


class YouTubeViewCountAbsent(Exception):
    """A returned video has no public viewCount to persist."""

    error_type = "youtube_view_count_absent"
    retry_after_seconds = VIEW_COUNT_ABSENT_RETRY_SECONDS


class YouTubeViewCountInvalid(Exception):
    """A returned video has a present but invalid viewCount."""

    error_type = "youtube_view_count_invalid"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _chunks(values: Sequence[Any], size: int = 50):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _empty_summary(reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "requested": 0,
        "updated": 0,
        "batches": 0,
        "errors": 0,
        "missing": 0,
        "unavailable_marked": 0,
        "video_ids": [],
        "missing_video_ids": [],
        "error_messages": [],
        "warnings": [],
        "metadata_refresh_requested": 0,
        "metadata_requests": 0,
        "batch_recurring_requested": 0,
        "batch_requests": 0,
        "fallback_requests": 0,
        "failed_id_fallbacks": 0,
        "presence_fallback_ids": 0,
        "degraded_batch_requests": 0,
        "partial_counter_updates": 0,
        "metadata_deferred": 0,
        "source_view_count_absent": 0,
        "source_view_count_invalid": 0,
        "metadata_duplicate_items": 0,
        "timings": {
            "youtube_seconds": 0.0,
            "db_save_seconds": 0.0,
            "total_batch_seconds": 0.0,
        },
        "routing": {
            "mode": "leased_video_collection_queue",
            "database_queue_enabled": True,
        },
    }


def _typed_empty_summary(reason: str, video_type: str) -> dict[str, Any]:
    summary = _empty_summary(reason)
    summary.update({
        "video_type": video_type,
        "enabled": True,
        "claimed": 0,
        "quota_exhausted": False,
        "rate_limited": False,
        "stream_lifecycle_probed": 0,
        "publication_waiting": 0,
        "completed_streams": 0,
        "infrastructure_error": False,
        "settlement_unknown": False,
        "stopped_reason": None,
    })
    summary["routing"] = {
        "mode": "leased_typed_video_collection_queue",
        "database_queue_enabled": True,
        "video_type": video_type,
        "full_metadata_only": True,
    }
    return summary


def _common_empty_summary(reason: str) -> dict[str, Any]:
    summary = _empty_summary(reason)
    summary.update({
        "claimed": 0,
        "inspected": 0,
        "paused": 0,
        "quota_exhausted": False,
        "rate_limited": False,
        "general_quota_exhausted": False,
        "batch_quota_exhausted": False,
        "batch_unavailable": False,
        "infrastructure_error": False,
        "settlement_unknown": False,
        "stopped_reason": None,
        "stream_lifecycle_probed": 0,
        "publication_waiting": 0,
        "completed_streams": 0,
        "queue_prepare_errors": 0,
        "short_queue_prepared": 0,
        "stream_queue_prepared": 0,
    })
    for video_type in ("long", "short", "stream", "unknown"):
        for metric in ("requested", "updated", "missing", "unavailable_marked"):
            summary[f"{video_type}_{metric}"] = 0
    summary["routing"] = {
        "mode": "leased_common_video_collection_pool",
        "database_queue_enabled": True,
        "video_types": ["long", "short", "stream"],
        "metadata_request_per_claim_batch": 1,
        "counter_request_per_claim_batch": 1,
    }
    return summary


def _absorb_common_summary(
    combined: dict[str, Any],
    summary: Mapping[str, Any],
    *,
    preserve_video_ids: bool = False,
) -> None:
    """Merge one settled batch without losing stop state or addressed IDs."""
    additive = (
        "requested", "updated", "batches", "errors", "missing",
        "unavailable_marked", "metadata_refresh_requested", "metadata_requests",
        "batch_recurring_requested", "batch_requests", "fallback_requests",
        "failed_id_fallbacks", "presence_fallback_ids", "degraded_batch_requests",
        "partial_counter_updates", "metadata_deferred", "source_view_count_absent",
        "source_view_count_invalid", "metadata_duplicate_items",
        "stream_lifecycle_probed", "publication_waiting", "completed_streams",
        *(
            f"{video_type}_{metric}"
            for video_type in ("long", "short", "stream", "unknown")
            for metric in ("requested", "updated", "missing", "unavailable_marked")
        ),
    )
    for key in additive:
        combined[key] += int(summary.get(key) or 0)
    for key in ("video_ids", "missing_video_ids", "error_messages", "warnings"):
        combined[key].extend(summary.get(key) or [])
        if not preserve_video_ids or key not in {"video_ids", "missing_video_ids"}:
            del combined[key][32:]
    for key in combined["timings"]:
        combined["timings"][key] += float(
            (summary.get("timings") or {}).get(key) or 0.0
        )
    for key in (
        "quota_exhausted", "general_quota_exhausted", "batch_quota_exhausted",
        "batch_unavailable", "rate_limited", "infrastructure_error", "settlement_unknown",
    ):
        combined[key] = bool(combined[key] or summary.get(key))
    if summary.get("stopped_reason"):
        combined["stopped_reason"] = summary["stopped_reason"]
    combined.update(collection_outcome_fields(combined))


def _default_is_quota_exhausted(error: BaseException) -> bool:
    from youtube_batch_stats import is_batch_stats_quota_error
    return is_youtube_daily_quota_exhausted(error, is_batch_stats_quota_error=is_batch_stats_quota_error)


def _parse_provider_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("provider timestamp must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("provider timestamp is invalid")
    iso_value = (
        normalized[:-1] + "+00:00"
        if normalized.endswith("Z")
        else normalized
    )
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as error:
        raise ValueError("provider timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("provider timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _stream_actual_end_at(video: Mapping[str, Any]) -> str | None:
    details = video.get("liveStreamingDetails")
    if details is None:
        return None
    if not isinstance(details, Mapping):
        raise ValueError("invalid liveStreamingDetails object")
    if "actualEndTime" not in details or details.get("actualEndTime") is None:
        return None
    value = details.get("actualEndTime")
    _parse_provider_timestamp(value)
    return str(value).strip()


def _publication_observation(claim, video, observed):
    """Type-independent provider lifecycle; a scheduled date is not completion."""
    snippet = video["snippet"]
    broadcast = snippet.get("liveBroadcastContent", "none")
    if broadcast not in ("none", "upcoming", "live"):
        raise _MetadataValidationFailure("live_broadcast_content_invalid")
    details = video.get("liveStreamingDetails")
    if details is None:
        details = {}
    if not isinstance(details, Mapping):
        raise _MetadataValidationFailure("live_streaming_details_invalid")
    parsed = {}
    for name, reason in (("scheduledStartTime", "scheduled_start_time_invalid"),
                         ("actualStartTime", "actual_start_time_invalid"),
                         ("actualEndTime", "actual_end_time_invalid")):
        value = details.get(name)
        if value is None:
            parsed[name] = None
            continue
        try:
            parsed[name] = _parse_provider_timestamp(value)
        except (ValueError, OverflowError):
            raise _MetadataValidationFailure(reason) from None
        if name != "scheduledStartTime" and parsed[name] > observed:
            raise _MetadataValidationFailure("actual_end_time_future" if name == "actualEndTime"
                                             else "actual_start_time_future")
    end = parsed["actualEndTime"]
    start = parsed["actualStartTime"]
    if end is not None and (broadcast in ("upcoming", "live") or (start is not None and end < start)):
        raise _MetadataValidationFailure("publication_evidence_conflict")
    waiting = broadcast if broadcast in ("upcoming", "live") else None
    if end is None and (start is not None or (claim.video_type == "stream"
                                            and claim.collection_mode != "identity_refresh")):
        waiting = waiting or "awaiting_completion"
    if (waiting is not None and broadcast == "none" and claim.video_type == "stream"
            and claim.collection_mode not in (
            "stream_lifecycle", "identity_refresh")):
        raise _MetadataValidationFailure("stream_completion_evidence_regressed")
    scheduled = parsed["scheduledStartTime"]
    return waiting, scheduled.isoformat() if scheduled is not None else None, end.isoformat() if end else None


class VideoCollectionService:
    """Run already-claimed work without exposing database query details."""

    def __init__(
        self,
        repository: VideoCollectionRepository,
        *,
        fetch_full: Callable[[Sequence[str], str], Sequence[Mapping[str, Any]]],
        fetch_counters: Callable[[Sequence[str], str], Mapping[str, Any]],
        build_full_payload: Callable[[str, Mapping[str, Any], str], Mapping[str, Any]],
        build_counter_payload: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
        is_quota_exhausted: Callable[[BaseException], bool] | None = None,
        fetch_metadata: (
            Callable[[Sequence[str], str], Sequence[Mapping[str, Any]]] | None
        ) = None,
        fetch_initial_profiles: Callable | None = None,
    ):
        self.repository = repository
        self.fetch_full = fetch_full
        self.fetch_counters = fetch_counters
        self.build_full_payload = build_full_payload
        self.build_counter_payload = build_counter_payload
        self.is_quota_exhausted = is_quota_exhausted or _default_is_quota_exhausted
        # The retained typed administrative runner still uses fetch_full.
        # Active queue runners inject complete General profiles explicitly.
        self.fetch_metadata = fetch_metadata or fetch_full
        self.fetch_initial_profiles = fetch_initial_profiles

    @staticmethod
    def _claim_index(
        claims: Sequence[VideoCollectionClaim],
    ) -> dict[str, VideoCollectionClaim]:
        return {claim.video_id: claim for claim in claims}

    def _fail_typed(
        self,
        video_type: str,
        claims: Sequence[VideoCollectionClaim],
        error_type: str,
        summary: dict[str, Any],
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        if not claims or summary["infrastructure_error"]:
            return
        rows = [
            {
                "video_id": claim.video_id,
                "lease_token": claim.lease_token,
                "error_type": str(error_type or "typed_collection_error")[:120],
                "retry_after_seconds": retry_after_seconds,
            }
            for claim in claims
        ]
        try:
            trace_write(self.repository, "fail_typed", rows, video_type)
        except Exception as release_error:
            summary["infrastructure_error"] = True
            summary["stopped_reason"] = "release_failed"
            summary["errors"] += 1
            summary["error_messages"].append(
                "typed claim release failed: " + release_error.__class__.__name__
            )

    @staticmethod
    def _typed_metadata_row(
        claim: VideoCollectionClaim,
        payload: Mapping[str, Any],
        *,
        video_type: str,
        attempt_id: str,
        recorded_at: str,
        stream_actual_end_at: str | None,
    ) -> dict[str, Any]:
        row = payload.get("row")
        snapshot = payload.get("snapshot")
        if not isinstance(row, Mapping) or not isinstance(snapshot, Mapping):
            raise ValueError("typed full payload must contain row and snapshot mappings")
        row_channel_id = str(row.get("channel_id") or claim.channel_id)
        if row_channel_id != claim.channel_id:
            raise ValueError("typed payload channel does not match its claim")
        if video_type == "short" and stream_actual_end_at is not None:
            raise ValueError("short payload contains stream completion evidence")
        if video_type == "stream" and stream_actual_end_at is None:
            raise ValueError("completed stream payload has no actualEndTime")
        return {
            "video_id": claim.video_id,
            "lease_token": claim.lease_token,
            "collection_attempt_id": attempt_id,
            "channel_id": claim.channel_id,
            "video_type": video_type,
            "title": row.get("title"),
            "views": snapshot.get("views"),
            "like_count": snapshot.get("like_count"),
            "comment_count": snapshot.get("comment_count"),
            "recorded_at": recorded_at,
            "published_at": row.get("published_at"),
            "duration": row.get("duration"),
            "tags": row.get("tags") or [],
            "category_id": row.get("category_id"),
            "youtube_topic_categories": row.get("youtube_topic_categories"),
            "default_audio_language": row.get("default_audio_language"),
            "default_language": row.get("default_language"),
            "caption_available": row.get("caption_available"),
            "stream_actual_end_at": stream_actual_end_at,
            "availability_status": row.get("availability_status"),
            "privacy_status": row.get("privacy_status"),
            "upload_status": row.get("upload_status"),
            "availability_checked_at": row.get("availability_checked_at"),
        }

    def _complete_typed_metadata(
        self,
        video_type: str,
        routed: Sequence[tuple[VideoCollectionClaim, Mapping[str, Any], str | None]],
        summary: dict[str, Any],
        *,
        attempt_id: str,
        recorded_at: str,
    ) -> None:
        if summary["infrastructure_error"]:
            return
        completion_rows: list[dict[str, Any]] = []
        completion_claims: list[VideoCollectionClaim] = []
        build_failed: list[VideoCollectionClaim] = []
        view_count_absent: list[VideoCollectionClaim] = []
        view_count_invalid: list[VideoCollectionClaim] = []

        for claim, video, stream_actual_end_at in routed:
            try:
                payload = self.build_full_payload(claim.channel_id, video, recorded_at)
                completion_rows.append(self._typed_metadata_row(
                    claim,
                    payload,
                    video_type=video_type,
                    attempt_id=attempt_id,
                    recorded_at=recorded_at,
                    stream_actual_end_at=stream_actual_end_at,
                ))
                completion_claims.append(claim)
            except YouTubeViewCountAbsent:
                view_count_absent.append(claim)
            except YouTubeViewCountInvalid:
                view_count_invalid.append(claim)
            except Exception as error:
                build_failed.append(claim)
                summary["errors"] += 1
                summary["error_messages"].append(
                    "typed full payload build failed: " + error.__class__.__name__
                )

        self._fail_typed(video_type, build_failed, "payload_build_error", summary)
        if view_count_absent:
            self._complete_identity_refresh(
                [(claim,video) for claim,video,_ in routed if claim in view_count_absent],summary,
                attempt_id=attempt_id,recorded_at=recorded_at,counterless_profile=True)
            summary["warnings"].append(
                "YouTube omitted viewCount for returned typed metadata items: "
                + str(len(view_count_absent))
            )
        if view_count_invalid:
            self._fail_typed(
                video_type,
                view_count_invalid,
                YouTubeViewCountInvalid.error_type,
                summary,
            )
            summary["errors"] += len(view_count_invalid)
            summary["source_view_count_invalid"] += len(view_count_invalid)
            summary["error_messages"].append(
                "YouTube returned invalid viewCount values for typed items: "
                + str(len(view_count_invalid))
            )
        if not completion_rows or summary["infrastructure_error"]:
            return

        db_started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_typed_metadata", completion_rows)
        except Exception as error:
            # The repository already retries the exact receipt payload. If its
            # acknowledgement is still unknown, preserve the lease/receipt and
            # stop; a failure write must not disguise a possibly committed save.
            summary["infrastructure_error"] = True
            summary["settlement_unknown"] = True
            summary["stopped_reason"] = "settlement_failed"
            summary["errors"] += 1
            summary["error_messages"].append(
                "typed metadata completion failed: " + error.__class__.__name__
            )
            return
        finally:
            summary["timings"]["db_save_seconds"] += (
                time.perf_counter() - db_started
            )

        completed_ids = [row["video_id"] for row in completion_rows]
        summary["updated"] += len(completed_ids)
        summary["video_ids"].extend(completed_ids)
        if video_type == "stream":
            summary["completed_streams"] += len(completed_ids)

    def _complete_stream_probes(
        self,
        claims: Sequence[VideoCollectionClaim],
        summary: dict[str, Any],
        *,
        attempt_id: str,
        recorded_at: str,
        videos_by_id: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if not claims or summary["infrastructure_error"]:
            return
        rows = []
        observed = _parse_provider_timestamp(recorded_at)
        for claim in claims:
            video = videos_by_id[claim.video_id]
            publication, scheduled, _ = _publication_observation(claim, video, observed)
            metadata = build_video_metadata_payload(
                claim.channel_id, video, recorded_at, parse_dt=_parse_provider_timestamp,
                allow_missing_duration=True,
            )
            rows.append({
                **metadata, "video_id": claim.video_id, "channel_id": claim.channel_id,
                "video_type": claim.video_type, "lease_token": claim.lease_token,
                "collection_attempt_id": attempt_id, "recorded_at": recorded_at,
                "publication_status": publication, "scheduled_start_at": scheduled,
                "stream_actual_end_at": None,
            })
        db_started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_stream_lifecycle", rows)
        except Exception as error:
            summary["infrastructure_error"] = True
            summary["settlement_unknown"] = True
            summary["stopped_reason"] = "settlement_failed"
            summary["errors"] += 1
            summary["error_messages"].append(
                "publication lifecycle completion failed: " + error.__class__.__name__
            )
            return
        finally:
            summary["timings"]["db_save_seconds"] += (
                time.perf_counter() - db_started
            )
        summary["stream_lifecycle_probed"] += sum(row["video_type"] == "stream" for row in rows)
        summary["publication_waiting"] += len(rows)

    def _complete_missing(
        self,
        video_type: str,
        claims: Sequence[VideoCollectionClaim],
        summary: dict[str, Any],
        *,
        attempt_id: str,
        recorded_at: str,
        source: str = "missing_from_youtube_api",
    ) -> None:
        """Record an API omission without inferring why the video was absent."""
        if not claims or summary["infrastructure_error"]:
            return
        missing_ids = [claim.video_id for claim in claims]
        summary["missing"] += len(missing_ids)
        summary["missing_video_ids"].extend(missing_ids)
        rows = [
            {
                "video_id": claim.video_id,
                "channel_id": claim.channel_id,
                "video_type": video_type,
                "lease_token": claim.lease_token,
                "collection_attempt_id": attempt_id,
                "recorded_at": recorded_at,
                "availability_status": AVAILABILITY_UNKNOWN_UNAVAILABLE,
                "availability_source": source,
            }
            for claim in claims
        ]
        db_started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_missing", rows)
        except Exception as error:
            if self._rejected_settlement(claims, summary, "missing completion", error):
                return
            # Lost settlement acknowledgement keeps these exact leases fenced.
            self._stop_on_unknown_settlement(summary, "missing completion", error)
            return
        finally:
            summary["timings"]["db_save_seconds"] += time.perf_counter() - db_started
        summary["unavailable_marked"] += len(rows)

    def _complete_identity_refresh(self, routed, summary, *, attempt_id, recorded_at,
                                   counterless_profile=False):
        if not routed or summary["settlement_unknown"]:
            return
        rows, accepted, rejected = [], [], []
        for claim, video in routed:
            try:
                metadata = (build_video_identity_payload(claim.channel_id, video, recorded_at,
                            parse_dt=_parse_provider_timestamp) if video is not None else {})
                rows.append({**metadata, "video_id": claim.video_id, "channel_id": claim.channel_id,
                             "video_type": claim.video_type, "lease_token": claim.lease_token,
                             "collection_attempt_id": attempt_id, "recorded_at": recorded_at,
                             "missing": video is None})
                if counterless_profile:
                    rows[-1]["counterless_profile"] = True
                accepted.append(claim)
            except (TypeError, ValueError, KeyError):
                rejected.append(claim)
        if rejected:
            summary["errors"] += len(rejected)
            self._fail_common(rejected, "payload_build_error", summary)
        if not rows or summary["settlement_unknown"]:
            return
        started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_identity_refresh", rows)
        except Exception as error:
            if not self._rejected_settlement(accepted, summary, "identity refresh", error):
                self._stop_on_unknown_settlement(summary, "identity refresh", error)
            return
        finally:
            summary["timings"]["db_save_seconds"] += time.perf_counter() - started
        for row in rows:
            if counterless_profile:
                summary["metadata_deferred"] += 1
                summary["source_view_count_absent"] += 1
                continue
            metric = "missing" if row["missing"] else "updated"
            summary[metric] += 1
            summary[f"{row['video_type']}_{metric}"] += 1
            summary["missing_video_ids" if row["missing"] else "video_ids"].append(row["video_id"])

    def _process_typed_batch(
        self,
        video_type: str,
        claims: Sequence[VideoCollectionClaim],
        summary: dict[str, Any],
        reason: str,
    ) -> bool:
        batch_started = time.perf_counter()
        summary["batches"] += 1
        summary["metadata_refresh_requested"] += len(claims)
        youtube_started = time.perf_counter()
        try:
            try:
                raw_videos = list(
                    self.fetch_full([claim.video_id for claim in claims], reason)
                )
            finally:
                summary["timings"]["youtube_seconds"] += (
                    time.perf_counter() - youtube_started
                )
        except Exception as error:
            try:
                quota_exhausted = bool(self.is_quota_exhausted(error))
            except Exception:
                quota_exhausted = False
            rate_delay = youtube_rate_limit_retry_after(error) if not quota_exhausted else None
            summary["rate_limited"] = rate_delay is not None
            self._fail_typed(
                video_type, claims,
                "youtube_quota_exhausted" if quota_exhausted else
                "provider_rate_limited" if rate_delay is not None else "youtube_full_request_error",
                summary, retry_after_seconds=rate_delay,
            )
            summary["errors"] += 1
            summary["quota_exhausted"] = quota_exhausted
            summary["stopped_reason"] = summary["stopped_reason"] or (
                "quota_exhausted" if quota_exhausted else
                "provider_rate_limited" if rate_delay is not None else "provider_request_failed"
            )
            summary["error_messages"].append(
                "YouTube typed full request failed: " + error.__class__.__name__
            )
            summary["timings"]["total_batch_seconds"] += (
                time.perf_counter() - batch_started
            )
            return quota_exhausted

        claims_by_id = self._claim_index(claims)
        videos: list[Mapping[str, Any]] = []
        identity_anomalies = 0
        for video in raw_videos:
            if not isinstance(video, Mapping):
                identity_anomalies += 1
                continue
            raw_video_id = video.get("id")
            if (
                not isinstance(raw_video_id, str)
                or not raw_video_id.strip()
                or raw_video_id not in claims_by_id
            ):
                identity_anomalies += 1
                continue
            videos.append(video)
        if identity_anomalies:
            summary["errors"] += identity_anomalies
            summary["error_messages"].append(
                "YouTube typed full response contained identity anomalies: "
                + str(identity_anomalies)
            )

        returned_counts = Counter(
            video["id"]
            for video in videos
        )
        duplicate_ids = {
            video_id for video_id, count in returned_counts.items() if count > 1
        }
        duplicate_claims = [claims_by_id[video_id] for video_id in duplicate_ids]
        if duplicate_claims:
            self._fail_typed(
                video_type,
                duplicate_claims,
                "duplicate_video_metadata_item",
                summary,
            )
            summary["errors"] += len(duplicate_claims)
            summary["metadata_duplicate_items"] += len(duplicate_claims)
            summary["error_messages"].append(
                "YouTube typed response contained duplicate claimed items: "
                + str(len(duplicate_claims))
            )

        returned_ids = {video["id"] for video in videos}
        missing_claims = [
            claim for claim in claims if claim.video_id not in returned_ids
        ]
        attempt_id = str(uuid.uuid4())
        observed = datetime.now(timezone.utc)
        recorded_at = observed.isoformat()
        if (identity_anomalies or duplicate_ids) and missing_claims:
            self._fail_typed(
                video_type,
                missing_claims,
                "malformed_youtube_metadata_response",
                summary,
            )
            summary["errors"] += len(missing_claims)
        else:
            self._complete_missing(
                video_type,
                missing_claims,
                summary,
                attempt_id=attempt_id,
                recorded_at=recorded_at,
            )

        routed_full: list[
            tuple[VideoCollectionClaim, Mapping[str, Any], str | None]
        ] = []
        lifecycle_claims: list[VideoCollectionClaim] = []
        route_failed: dict[str, list[VideoCollectionClaim]] = {}
        completion_evidence_regressed: list[VideoCollectionClaim] = []
        for video in videos:
            video_id = str(video.get("id") or "")
            claim = claims_by_id.get(video_id)
            if claim is None or video_id in duplicate_ids:
                continue
            try:
                actual_end_at, waiting = self._validate_common_metadata(claim, video, observed)
                if waiting:
                    lifecycle_claims.append(claim)
                    continue
                if not isinstance(video.get("statistics"), Mapping):
                    raise _MetadataValidationFailure("statistics_missing_or_invalid")
                routed_full.append((claim, video, actual_end_at))
            except Exception as error:
                if isinstance(error, _MetadataValidationFailure) and error.reason == "stream_completion_evidence_regressed":
                    metadata_validation_failed(claim, error.reason)
                    completion_evidence_regressed.append(claim)
                    continue
                metadata_validation_failed(claim, error.reason
                    if isinstance(error, _MetadataValidationFailure) else "validation_unexpected")
                error_type = ("payload_build_error"
                    if isinstance(error, _MetadataValidationFailure) and error.payload_error
                    else "typed_provider_identity_or_lifecycle_error")
                route_failed.setdefault(error_type, []).append(claim)

        for error_type, failed_claims in route_failed.items():
            self._fail_typed(video_type, failed_claims, error_type, summary)
            summary["errors"] += len(failed_claims)
            summary["error_messages"].append(
                "typed provider metadata validation failed: " + str(len(failed_claims))
            )
        if completion_evidence_regressed:
            self._fail_typed(
                video_type,
                completion_evidence_regressed,
                "typed_stream_completion_evidence_regressed",
                summary,
            )
            summary["errors"] += len(completion_evidence_regressed)
            summary["error_messages"].append(
                "typed stream completion evidence regressed: "
                + str(len(completion_evidence_regressed))
            )
        self._complete_stream_probes(
            lifecycle_claims,
            summary,
            attempt_id=attempt_id,
            recorded_at=recorded_at,
            videos_by_id={str(video.get("id") or ""): video for video in videos},
        )
        self._complete_typed_metadata(
            video_type,
            routed_full,
            summary,
            attempt_id=attempt_id,
            recorded_at=recorded_at,
        )
        summary["timings"]["total_batch_seconds"] += (
            time.perf_counter() - batch_started
        )
        return False

    @staticmethod
    def _stop_on_unknown_settlement(
        summary: dict[str, Any],
        label: str,
        error: BaseException,
    ) -> None:
        from db.hot.hot_collection_control import admission_denial
        denied = admission_denial(error)
        if denied is not None:
            summary['hot_runtime_interrupted'] = True
            summary['_hot_abort_attempted_video_ids'] = tuple(getattr(denied, 'control_video_ids', ()))
        summary["infrastructure_error"] = True
        summary["settlement_unknown"] = True
        summary["stopped_reason"] = "hot_runtime_interrupted" if denied is not None else "settlement_failed"
        summary["errors"] += 1
        summary["error_messages"].append(
            f"{label} failed: {error.__class__.__name__}"
        )

    @staticmethod
    def _claims_of(routed, rows) -> list[VideoCollectionClaim]:
        sent = {row["video_id"] for row in rows}
        return [item[0] for item in routed if item[0].video_id in sent]

    def _rejected_settlement(
        self,
        claims: Sequence[VideoCollectionClaim],
        summary: dict[str, Any],
        label: str,
        error: BaseException,
    ) -> bool:
        """PostgreSQL refused the rows themselves: a 22xxx data exception or a
        23xxx constraint violation rolls the statement back, so the outcome is
        known and a replay would fail the same way. These claims fail with the
        usual backoff and the run continues. Any other failure may have
        committed and still stops the run."""
        code = getattr(error, "code", None)
        if not (isinstance(code, str) and len(code) == 5 and code[:2] in ("22", "23")):
            return False
        summary["errors"] += len(claims)
        summary["error_messages"].append(f"{label} rejected: {code}")
        self._fail_common(claims, "settlement_rejected", summary)
        return True

    def _fail_common(
        self,
        claims: Sequence[VideoCollectionClaim],
        error_type: str,
        summary: dict[str, Any],
        *,
        retry_after_seconds: int | None = None,
    ) -> bool:
        if not claims:
            return True
        if summary["settlement_unknown"]:
            return False
        rows = [
            {
                "video_id": claim.video_id,
                "video_type": claim.video_type,
                "lease_token": claim.lease_token,
                "error_type": str(error_type or "collection_error")[:120],
                "retry_after_seconds": retry_after_seconds,
            }
            for claim in claims
        ]
        try:
            trace_write(self.repository, "fail_batch", rows)
        except Exception as error:
            self._stop_on_unknown_settlement(
                summary,
                "common claim release",
                error,
            )
            summary["stopped_reason"] = "release_failed"
            return False
        return True

    @staticmethod
    def _validate_common_metadata(
        claim: VideoCollectionClaim,
        video: Mapping[str, Any],
        observed: datetime,
    ) -> tuple[str | None, bool]:
        snippet = video.get("snippet")
        if not isinstance(snippet, Mapping):
            raise _MetadataValidationFailure("snippet_missing_or_invalid")
        if str(snippet.get("channelId") or "") != claim.channel_id:
            raise _MetadataValidationFailure("channel_id_mismatch")
        published_at = snippet.get("publishedAt")
        if published_at is None:
            raise _MetadataValidationFailure("published_at_missing")
        try:
            _parse_provider_timestamp(published_at)
        except (ValueError, OverflowError):
            raise _MetadataValidationFailure("published_at_invalid") from None
        content_details = video.get("contentDetails")
        status = video.get("status")
        if not isinstance(content_details, Mapping):
            raise _MetadataValidationFailure("content_details_missing_or_invalid")
        if not isinstance(status, Mapping):
            raise _MetadataValidationFailure("status_missing_or_invalid")
        privacy_status = status.get("privacyStatus")
        upload_status = status.get("uploadStatus")
        if not isinstance(privacy_status, str) or privacy_status not in {"public", "private", "unlisted"}:
            raise _MetadataValidationFailure("privacy_status_invalid")
        if (
            not isinstance(upload_status, str)
            or upload_status != upload_status.strip()
            or not upload_status
            or len(upload_status) > 64
        ):
            raise _MetadataValidationFailure("upload_status_invalid")
        publication, _, actual_end_at = _publication_observation(claim, video, observed)
        duration = content_details.get("duration")
        title = snippet.get("title")
        # Early profile diagnostics must keep the published payload failure
        # contract; waiting profiles use their separate lifecycle write path.
        if not isinstance(title, str) or len(title) > VIDEO_TITLE_MAX_CHARS:
            raise _MetadataValidationFailure("title_invalid", payload_error=publication is None)
        try:
            build_video_optional_metadata(video)
        except VideoMetadataValidationError as error:
            raise _MetadataValidationFailure(error.reason, payload_error=publication is None) from None
        if duration is None:
            if publication is None:
                raise _MetadataValidationFailure("duration_missing")
        elif (not isinstance(duration, str) or duration != duration.strip() or len(duration) > 64
              or not duration.startswith("P") or youtube_duration_seconds(duration) is None):
            raise _MetadataValidationFailure("duration_invalid")
        return actual_end_at if claim.video_type == "stream" else None, publication is not None

    @staticmethod
    def _with_batch_counters(
        video: Mapping[str, Any],
        counters: Mapping[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(video)
        enriched["statistics"] = {
            "viewCount": counters.get("views"),
            "likeCount": counters.get("like_count"),
            "commentCount": counters.get("comment_count"),
        }
        return enriched

    def _complete_common_long_metadata(
        self,
        routed: Sequence[tuple[VideoCollectionClaim, Mapping[str, Any]]],
        summary: dict[str, Any],
        *,
        reason: str,
        attempt_id: str,
        recorded_at: str,
        metadata_observed_at: str | None = None,
    ) -> None:
        if not routed or summary["settlement_unknown"]:
            return
        rows: list[dict[str, Any]] = []
        build_failed: list[VideoCollectionClaim] = []
        view_count_absent: list[VideoCollectionClaim] = []
        view_count_invalid: list[VideoCollectionClaim] = []
        for claim, video in routed:
            try:
                payload = self.build_full_payload(
                    claim.channel_id,
                    video,
                    metadata_observed_at or recorded_at,
                )
                row = payload["row"]
                snapshot = payload["snapshot"]
                if not isinstance(row, Mapping) or not isinstance(snapshot, Mapping):
                    raise ValueError("long full payload is invalid")
                if str(row.get("channel_id") or claim.channel_id) != claim.channel_id:
                    raise ValueError("long payload channel does not match claim")
                rows.append({
                    "video_id": claim.video_id,
                    "lease_token": claim.lease_token,
                    "collection_attempt_id": attempt_id,
                    "channel_id": claim.channel_id,
                    "title": row.get("title"),
                    "views": snapshot.get("views"),
                    "like_count": snapshot.get("like_count"),
                    "comment_count": snapshot.get("comment_count"),
                    "recorded_at": recorded_at,
                    "metadata_observed_at": metadata_observed_at or recorded_at,
                    "published_at": row.get("published_at"),
                    "duration": row.get("duration"),
                    "tags": row.get("tags") or [],
                    "category_id": row.get("category_id"),
                    "youtube_topic_categories": row.get("youtube_topic_categories"),
                    "default_audio_language": row.get("default_audio_language"),
                    "default_language": row.get("default_language"),
                    "caption_available": row.get("caption_available"),
                    "stream_actual_end_at": row.get("stream_actual_end_at"),
                    "availability_status": row.get("availability_status"),
                    "privacy_status": row.get("privacy_status"),
                    "upload_status": row.get("upload_status"),
                    "availability_checked_at": row.get("availability_checked_at"),
                    "availability_source": reason,
                    "availability_restored_at": metadata_observed_at or recorded_at,
                })
            except YouTubeViewCountAbsent:
                view_count_absent.append(claim)
            except YouTubeViewCountInvalid:
                view_count_invalid.append(claim)
            except Exception:
                build_failed.append(claim)
        if view_count_absent:
            self._complete_identity_refresh(
                [(claim,video) for claim,video in routed if claim in view_count_absent],summary,
                attempt_id=attempt_id,recorded_at=metadata_observed_at or recorded_at,counterless_profile=True)
            if summary["settlement_unknown"]:
                return
        if view_count_invalid:
            summary["errors"] += len(view_count_invalid)
            summary["source_view_count_invalid"] += len(view_count_invalid)
            if not self._fail_common(
                view_count_invalid, YouTubeViewCountInvalid.error_type, summary,
            ):
                return
        if build_failed:
            summary["errors"] += len(build_failed)
            if not self._fail_common(
                build_failed,
                "payload_build_error",
                summary,
            ):
                return
        if not rows:
            return
        db_started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_metadata", rows)
        except Exception as error:
            if self._rejected_settlement(self._claims_of(routed, rows), summary, "long metadata completion", error):
                return
            self._stop_on_unknown_settlement(
                summary,
                "long metadata completion",
                error,
            )
            return
        finally:
            summary["timings"]["db_save_seconds"] += (
                time.perf_counter() - db_started
            )
        completed_ids = [row["video_id"] for row in rows]
        summary["updated"] += len(rows)
        summary["long_updated"] += len(rows)
        summary["video_ids"].extend(completed_ids)

    def _complete_common_typed_metadata(
        self,
        video_type: str,
        routed: Sequence[
            tuple[VideoCollectionClaim, Mapping[str, Any], str | None]
        ],
        summary: dict[str, Any],
        *,
        attempt_id: str,
        recorded_at: str,
        metadata_observed_at: str | None = None,
    ) -> None:
        if not routed or summary["settlement_unknown"]:
            return
        rows: list[dict[str, Any]] = []
        build_failed: list[VideoCollectionClaim] = []
        view_count_absent: list[VideoCollectionClaim] = []
        view_count_invalid: list[VideoCollectionClaim] = []
        for claim, video, actual_end_at in routed:
            try:
                payload = self.build_full_payload(
                    claim.channel_id,
                    video,
                    metadata_observed_at or recorded_at,
                )
                rows.append(self._typed_metadata_row(
                    claim,
                    payload,
                    video_type=video_type,
                    attempt_id=attempt_id,
                    recorded_at=recorded_at,
                    stream_actual_end_at=actual_end_at,
                ))
                rows[-1]["metadata_observed_at"] = metadata_observed_at or recorded_at
            except YouTubeViewCountAbsent:
                view_count_absent.append(claim)
            except YouTubeViewCountInvalid:
                view_count_invalid.append(claim)
            except Exception:
                build_failed.append(claim)
        if view_count_absent:
            self._complete_identity_refresh(
                [(claim,video) for claim,video,_ in routed if claim in view_count_absent],summary,
                attempt_id=attempt_id,recorded_at=metadata_observed_at or recorded_at,counterless_profile=True)
            if summary["settlement_unknown"]:
                return
        if view_count_invalid:
            summary["errors"] += len(view_count_invalid)
            summary["source_view_count_invalid"] += len(view_count_invalid)
            if not self._fail_common(
                view_count_invalid, YouTubeViewCountInvalid.error_type, summary,
            ):
                return
        if build_failed:
            summary["errors"] += len(build_failed)
            if not self._fail_common(
                build_failed,
                "payload_build_error",
                summary,
            ):
                return
        if not rows:
            return
        db_started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_typed_metadata", rows)
        except Exception as error:
            if self._rejected_settlement(self._claims_of(routed, rows), summary, f"{video_type} metadata completion", error):
                return
            self._stop_on_unknown_settlement(
                summary,
                f"{video_type} metadata completion",
                error,
            )
            return
        finally:
            summary["timings"]["db_save_seconds"] += (
                time.perf_counter() - db_started
            )
        completed_ids = [row["video_id"] for row in rows]
        summary["updated"] += len(rows)
        summary[f"{video_type}_updated"] += len(rows)
        summary["video_ids"].extend(completed_ids)
        if video_type == "stream":
            summary["completed_streams"] += len(rows)

    def _complete_common_counters(
        self,
        routed: Sequence[tuple[VideoCollectionClaim, Mapping[str, Any]]],
        summary: dict[str, Any],
        *,
        attempt_id: str,
        recorded_at: str,
    ) -> None:
        if not routed or summary["settlement_unknown"]:
            return
        rows: list[dict[str, Any]] = []
        build_failed: list[VideoCollectionClaim] = []
        for claim, counters in routed:
            try:
                payload = self.build_counter_payload(counters, recorded_at)
                snapshot = payload["snapshot"]
                if not isinstance(snapshot, Mapping):
                    raise ValueError("counter payload snapshot is invalid")
                rows.append({
                    "video_id": claim.video_id,
                    "lease_token": claim.lease_token,
                    "collection_attempt_id": attempt_id,
                    "views": snapshot.get("views"),
                    "like_count": snapshot.get("like_count"),
                    "comment_count": snapshot.get("comment_count"),
                    "recorded_at": snapshot.get("recorded_at") or recorded_at,
                })
            except Exception:
                build_failed.append(claim)
        if build_failed:
            summary["errors"] += len(build_failed)
            if not self._fail_common(
                build_failed,
                "counter_payload_build_error",
                summary,
            ):
                return
        if not rows:
            return
        db_started = time.perf_counter()
        try:
            trace_write(self.repository, "complete_counters", rows)
        except Exception as error:
            if self._rejected_settlement(self._claims_of(routed, rows), summary, "video counter completion", error):
                return
            self._stop_on_unknown_settlement(
                summary,
                "video counter completion",
                error,
            )
            return
        finally:
            summary["timings"]["db_save_seconds"] += (
                time.perf_counter() - db_started
            )
        completed_ids = [row["video_id"] for row in rows]
        summary["updated"] += len(rows)
        completed = set(completed_ids)
        for claim, _ in routed:
            if claim.video_id in completed:
                summary[f"{claim.video_type}_updated"] += 1
        summary["partial_counter_updates"] += len(rows)
        summary["video_ids"].extend(completed_ids)


    def process_common_claims(
        self,
        claims: Sequence[VideoCollectionClaim],
        *,
        reason: str = "auto_video_collection",
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Drain undispatched claims; uncertain settlement identities stay fenced."""
        claim_rows = list(claims)
        summary = self._process_common_claims(claim_rows, reason=reason)
        return self.finish_common_claims(claim_rows, summary, progress_callback=progress_callback)

    def finish_common_claims(self, claim_rows, summary, *, progress_callback=None):
        """The same owner-thread drain for synchronous and dispatched batches."""
        attempted = set(summary.pop('_hot_abort_attempted_video_ids', ()))
        if summary.get('hot_runtime_interrupted'):
            from db.hot.hot_collection_control import HotCollectionControlRepository
            rows = [{'video_id': claim.video_id, 'video_type': claim.video_type,
                     'lease_token': claim.lease_token}
                    for claim in claim_rows if claim.video_id not in attempted]
            try:
                for start in range(0, len(rows), 50):
                    HotCollectionControlRepository(self.repository.database).abort(videos=rows[start:start+50])
            except Exception:
                summary['errors'] += 1
                summary['settlement_unknown'] = True
            summary['stopped_reason'] = 'hot_runtime_interrupted'
        if progress_callback:
            progress_callback(summary)
        return summary

    def _process_common_claims(
        self,
        claims: Sequence[VideoCollectionClaim],
        *,
        reason: str = "auto_video_collection",
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        initial_response=None,
    ) -> dict[str, Any]:
        return run_provider_steps(self.common_claim_steps(
            claims, reason=reason, progress_callback=progress_callback,
            initial_response=initial_response,
        ))

    def common_claim_steps(self, claims, *, reason="auto_video_collection",
                           progress_callback=None, initial_response=None, summary=None):
        """Bind each resume separately; never leave context installed at yield."""
        steps = self._common_claim_steps(claims, reason=reason,
            progress_callback=progress_callback, initial_response=initial_response, summary=summary)
        if initial_response is None:
            with bind(batch_id=str(uuid.uuid4())):
                context = capture()
        else:
            context = capture()
        response = None
        first = True
        try:
            while True:
                try:
                    step = (trace_run(context, next, steps) if first else
                            trace_run(context, steps.send, response))
                except StopIteration as finished:
                    result = finished.value
                    if initial_response is None:
                        trace_run(context, event, "video_batch", "unknown" if result.get("settlement_unknown")
                                  else result.get("completion_outcome") or "settled",
                                  requested_count=result.get("requested"),
                                  received_count=result.get("updated"), reason=result.get("stopped_reason"))
                    return result
                first = False
                response = yield step
        except GeneratorExit:
            trace_run(context, event, "video_batch", "interrupted")
            raise
        except BaseException:
            trace_run(context, event, "video_batch", "unknown")
            raise
        finally:
            trace_run(context, steps.close)

    def _common_claim_steps(
        self,
        claims: Sequence[VideoCollectionClaim],
        *,
        reason: str = "auto_video_collection",
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        initial_response=None,
        summary=None,
    ):
        """Process one claim batch; General profiles use Stats only as fallback."""
        if summary is None:
            summary = _common_empty_summary(reason)
        else:
            summary.update(_common_empty_summary(reason))
        claim_rows = list(claims)
        summary["requested"] = len(claim_rows)
        summary["claimed"] = len(claim_rows)
        summary["batches"] = 1 if claim_rows else 0
        if len(claim_rows) > TYPED_COLLECTION_BATCH_SIZE:
            raise ValueError("common collection batch accepts at most 50 claims")
        if initial_response is None:
            claims_selected(claim_rows, reason)
        for claim in claim_rows:
            key = f"{claim.video_type}_requested"
            if key in summary:summary[key] += 1
        claim_counts = Counter(claim.video_id for claim in claim_rows)
        if any(count != 1 for count in claim_counts.values()):
            summary["infrastructure_error"] = True
            summary["settlement_unknown"] = True
            summary["stopped_reason"] = "claim_contract_error"
            summary["errors"] += 1
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        full_claims = [
            claim for claim in claim_rows
            if claim.collection_mode != "counters"
        ]
        counter_only_claims = [
            claim for claim in claim_rows
            if claim.collection_mode == "counters"
        ]
        invalid_claims = [
            claim for claim in claim_rows
            if (
                claim.video_type not in {"long", "short", "stream", "unknown"}
                or (claim.video_type == "unknown" and claim.collection_mode != "identity_refresh")
                or (claim.video_type != "unknown" and claim.collection_mode not in TYPED_COLLECTION_MODES.get(
                    claim.video_type,
                    {"hydrate", "metadata", "counters", "identity_refresh"},
                ))
            )
        ]
        if invalid_claims:
            summary["errors"] += len(invalid_claims)
            if not self._fail_common(
                invalid_claims,
                "claim_contract_error",
                summary,
            ):
                summary["finished_at"] = datetime.now(timezone.utc).isoformat()
                return summary
            invalid_ids = {claim.video_id for claim in invalid_claims}
            full_claims = [
                claim for claim in full_claims if claim.video_id not in invalid_ids
            ]
            counter_only_claims = [
                claim
                for claim in counter_only_claims
                if claim.video_id not in invalid_ids
            ]

        metadata_observed = None
        metadata_by_id: dict[str, Mapping[str, Any]] = {}
        metadata_route: dict[str, tuple[str | None, bool]] = {}
        if full_claims:
            summary["metadata_refresh_requested"] += len(full_claims)
            youtube_started = time.perf_counter()
            provider_result = None
            try:
                try:
                    if initial_response is not None:
                        summary["metadata_requests"] += 1
                        raw_videos = list(initial_response.items_or_raise())
                        metadata_observed = _source_observation(initial_response.metadata_observed_at)
                    else:
                        provider_result = yield ProviderStep(self.fetch_metadata,
                            tuple(claim.video_id for claim in full_claims), reason, materialize=True)
                        summary["metadata_requests"] += 1
                        raw_videos = provider_result.unwrap()
                        metadata_observed = _source_observation(provider_result.observed_at)
                finally:
                    # End provider timing before validation or DB settlement.
                    summary["timings"]["youtube_seconds"] += (
                        provider_result.elapsed if provider_result is not None
                        else time.perf_counter() - youtube_started if initial_response is not None else 0.0
                    )
            except Exception as error:
                try:
                    quota_exhausted = bool(self.is_quota_exhausted(error))
                except Exception:
                    quota_exhausted = False
                rate_delay = youtube_rate_limit_retry_after(error) if not quota_exhausted else None
                summary["rate_limited"] = rate_delay is not None
                summary["general_quota_exhausted"] = quota_exhausted
                summary["quota_exhausted"] = quota_exhausted
                summary["errors"] += 1
                summary["error_messages"].append(
                    "YouTube metadata request failed: "
                    + error.__class__.__name__
                )
                self._fail_common(
                    full_claims,
                    "youtube_quota_exhausted"
                    if quota_exhausted
                    else "provider_rate_limited" if rate_delay is not None else "youtube_metadata_request_error",
                    summary,
                    retry_after_seconds=3600 if quota_exhausted else rate_delay,
                )
                if quota_exhausted or rate_delay is not None:
                    if not summary["settlement_unknown"]:
                        self._fail_common(counter_only_claims,
                            "youtube_quota_exhausted" if quota_exhausted else "provider_rate_limited",
                            summary, retry_after_seconds=3600 if quota_exhausted else rate_delay)
                    summary["stopped_reason"] = summary["stopped_reason"] or (
                        "quota_exhausted" if quota_exhausted else "provider_rate_limited")
                    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
                    return summary
            else:
                claims_by_id = self._claim_index(full_claims)
                videos: list[Mapping[str, Any]] = []
                identity_anomalies = 0
                for video in raw_videos:
                    if not isinstance(video, Mapping):
                        identity_anomalies += 1
                        continue
                    video_id = video.get("id")
                    if (
                        not isinstance(video_id, str)
                        or video_id not in claims_by_id
                    ):
                        identity_anomalies += 1
                        continue
                    videos.append(video)
                returned_counts = Counter(video["id"] for video in videos)
                duplicate_ids = {
                    video_id
                    for video_id, count in returned_counts.items()
                    if count > 1
                }
                if identity_anomalies:
                    summary["errors"] += identity_anomalies
                    summary["error_messages"].append(
                        "metadata response contained identity anomalies: "
                        + str(identity_anomalies)
                    )
                if duplicate_ids:
                    duplicate_claims = [
                        claims_by_id[video_id] for video_id in duplicate_ids
                    ]
                    summary["errors"] += len(duplicate_claims)
                    summary["metadata_duplicate_items"] += len(duplicate_claims)
                    self._fail_common(
                        duplicate_claims,
                        "duplicate_video_metadata_item",
                        summary,
                    )
                returned_ids = set(returned_counts)
                event("video_general_observation", "received", quota_bucket="general",
                      video_ids=[claim.video_id for claim in full_claims],
                      requested_count=len(full_claims), received_count=len(returned_ids),
                      observed_at=metadata_observed.isoformat())
                missing_claims = [
                    claim
                    for claim in full_claims
                    if claim.video_id not in returned_ids
                ]
                missing_attempt_id = str(uuid.uuid4())
                missing_recorded_at = metadata_observed.isoformat()
                if identity_anomalies and missing_claims:
                    summary["errors"] += len(missing_claims)
                    self._fail_common(
                        missing_claims,
                        "malformed_youtube_metadata_response",
                        summary,
                    )
                else:
                    for video_type in ("long", "short", "stream"):
                        type_claims = [claim for claim in missing_claims
                                       if claim.video_type == video_type and claim.collection_mode != "identity_refresh"]
                        if not type_claims or summary["settlement_unknown"]:
                            continue
                        before_unavailable = summary["unavailable_marked"]
                        self._complete_missing(
                            video_type,
                            type_claims,
                            summary,
                            attempt_id=missing_attempt_id,
                            recorded_at=missing_recorded_at,
                            source=reason,
                        )
                        summary[f"{video_type}_missing"] += len(type_claims)
                        summary[f"{video_type}_unavailable_marked"] += summary["unavailable_marked"] - before_unavailable
                    self._complete_identity_refresh(
                        [(claim, None) for claim in missing_claims if claim.collection_mode == "identity_refresh"], summary,
                        attempt_id=missing_attempt_id, recorded_at=missing_recorded_at,
                    )

                observed = metadata_observed
                route_failed: dict[str, list[VideoCollectionClaim]] = {}
                for video in videos:
                    video_id = str(video.get("id") or "")
                    claim = claims_by_id.get(video_id)
                    if claim is None or video_id in duplicate_ids:
                        continue
                    try:
                        metadata_route[video_id] = self._validate_common_metadata(
                            claim,
                            video,
                            observed,
                        )
                        metadata_by_id[video_id] = video
                    except Exception as error:
                        metadata_validation_failed(claim, error.reason
                            if isinstance(error, _MetadataValidationFailure) else "validation_unexpected")
                        error_type = ("payload_build_error"
                            if isinstance(error, _MetadataValidationFailure) and error.payload_error
                            else "provider_identity_or_lifecycle_error")
                        route_failed.setdefault(error_type, []).append(claim)
                for error_type, failed_claims in route_failed.items():
                    summary["errors"] += len(failed_claims)
                    self._fail_common(failed_claims, error_type, summary)

        if summary["settlement_unknown"]:
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        attempt_id = str(uuid.uuid4())
        recorded_at = metadata_observed.isoformat() if metadata_observed is not None else None
        self._complete_identity_refresh(
            [(claim, metadata_by_id[claim.video_id]) for claim in full_claims
             if claim.collection_mode == "identity_refresh" and claim.video_id in metadata_by_id
             and not metadata_route[claim.video_id][1]], summary,
            attempt_id=attempt_id, recorded_at=recorded_at,
        )
        lifecycle_claims = [
            claim for claim in full_claims
            if metadata_route.get(claim.video_id, (None, False))[1]
        ]
        if lifecycle_claims:
            self._complete_stream_probes(
                lifecycle_claims,
                summary,
                attempt_id=attempt_id,
                recorded_at=recorded_at,
                videos_by_id=metadata_by_id,
            )
        if summary["settlement_unknown"]:
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary

        try:
            counter_presence = (
                self.repository.load_counter_presence(
                    claim.video_id for claim in counter_only_claims
                )
                if counter_only_claims
                else {}
            )
        except Exception as error:
            summary["infrastructure_error"] = True
            summary["stopped_reason"] = "counter_presence_read_failed"
            summary["errors"] += 1
            summary["error_messages"].append(
                "counter presence read failed: " + error.__class__.__name__
            )
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            return summary
        presence_missing = [
            claim for claim in counter_only_claims
            if claim.video_id not in counter_presence
        ]
        if presence_missing:
            summary["errors"] += len(presence_missing)
            self._fail_common(
                presence_missing,
                "database_counter_presence_missing",
                summary,
                retry_after_seconds=1800,
            )
        counter_only_ready = [
            claim for claim in counter_only_claims
            if claim.video_id in counter_presence
        ]
        full_ready = [
            claim for claim in full_claims
            if (
                claim.video_id in metadata_by_id
                and claim.collection_mode != "identity_refresh"
                and not metadata_route[claim.video_id][1]
            )
        ]
        if initial_response is None:
            # General already supplies counters. Only a missing required view
            # count needs the existing targeted Batch continuation. Invalid
            # values are rejected by payload validation, never silently fixed;
            # optional absent counters remain NULL without another request.
            fallback_ids = {
                claim.video_id for claim in full_ready
                if (metadata_by_id[claim.video_id].get("statistics") is None
                    or (isinstance(metadata_by_id[claim.video_id].get("statistics"), Mapping)
                        and metadata_by_id[claim.video_id]["statistics"].get("viewCount") in (None, "")))
            }
            general_ready = [claim for claim in full_ready if claim.video_id not in fallback_ids]
            self._complete_common_long_metadata(
                [(claim, metadata_by_id[claim.video_id]) for claim in general_ready
                 if claim.video_type == "long"], summary, reason=reason,
                attempt_id=attempt_id, recorded_at=recorded_at, metadata_observed_at=recorded_at,
            )
            for kind in ("short", "stream"):
                self._complete_common_typed_metadata(
                    kind, [(claim, metadata_by_id[claim.video_id], metadata_route[claim.video_id][0])
                           for claim in general_ready if claim.video_type == kind], summary,
                    attempt_id=attempt_id, recorded_at=recorded_at, metadata_observed_at=recorded_at,
                )
            full_ready = [claim for claim in full_ready if claim.video_id in fallback_ids]
        counter_cohort = [*counter_only_ready, *full_ready]
        if initial_response is not None:
            if counter_only_claims:
                raise ValueError("initial collection cannot contain counter-only claims")
            # Provider threads have already finished any targeted Batch
            # fallback. A refusal affects only those incomplete items; healthy
            # General items still join this response's atomic completion.
            summary["batch_requests"] += getattr(initial_response, "batch_requests", 0)
            counter_error = getattr(initial_response, "counter_error", None)
            if counter_error is not None:
                failed_counter_ids = set(getattr(initial_response, "counter_ids", ()))
                failed_counter_claims = [claim for claim in full_ready
                                         if claim.video_id in failed_counter_ids]
                full_ready = [claim for claim in full_ready
                              if claim.video_id not in failed_counter_ids]
                try:
                    quota_exhausted = bool(self.is_quota_exhausted(counter_error))
                except Exception:
                    quota_exhausted = False
                rate_delay = youtube_rate_limit_retry_after(counter_error) if not quota_exhausted else None
                summary["rate_limited"] = rate_delay is not None
                summary["batch_quota_exhausted"] = quota_exhausted
                summary["batch_unavailable"] = not quota_exhausted and rate_delay is None
                summary["quota_exhausted"] = bool(summary["quota_exhausted"] or quota_exhausted)
                summary["degraded_batch_requests"] += 1
                summary["errors"] += 1
                summary["error_messages"].append(
                    "Batch counter request failed: " + counter_error.__class__.__name__
                )
                self._complete_identity_refresh(
                    [(claim,metadata_by_id[claim.video_id]) for claim in failed_counter_claims],summary,
                    attempt_id=attempt_id,recorded_at=recorded_at,counterless_profile=True)
                summary["stopped_reason"] = summary["stopped_reason"] or (
                    "quota_exhausted" if quota_exhausted else
                    "provider_rate_limited" if rate_delay is not None else "batch_unavailable"
                )
            # General contains counters for healthy IDs; only successfully
            # enriched fallback IDs belong to the independently observed Batch.
            fallback_ids = set(initial_response.counter_observed_ids)
            groups = {}
            for claim in full_ready:
                counter_time = (initial_response.counters_observed_at
                                if claim.video_id in fallback_ids else metadata_observed)
                counter_stamp = _source_observation(counter_time).isoformat()
                groups.setdefault(counter_stamp, []).append(claim)
            for counter_stamp, group in groups.items():
                self._complete_common_long_metadata(
                    [(claim, metadata_by_id[claim.video_id]) for claim in group
                     if claim.video_type == "long"], summary, reason=reason,
                    attempt_id=attempt_id, recorded_at=counter_stamp,
                    metadata_observed_at=recorded_at,
                )
                for kind in ("short", "stream"):
                    self._complete_common_typed_metadata(
                        kind, [(claim, metadata_by_id[claim.video_id], metadata_route[claim.video_id][0])
                               for claim in group if claim.video_type == kind], summary,
                        attempt_id=attempt_id, recorded_at=counter_stamp,
                        metadata_observed_at=recorded_at,
                    )
        elif counter_cohort and not summary["settlement_unknown"]:
            summary["batch_recurring_requested"] += len(counter_only_ready)
            youtube_started = time.perf_counter()
            provider_result = None
            try:
                try:
                    provider_result = yield ProviderStep(self.fetch_counters,
                        tuple(claim.video_id for claim in counter_cohort), reason)
                    summary["batch_requests"] += 1
                    result = provider_result.unwrap()
                    counters_recorded_at = _source_observation(provider_result.observed_at).isoformat()
                finally:
                    summary["timings"]["youtube_seconds"] += (
                        provider_result.elapsed if provider_result is not None
                        else 0.0
                    )
                if (
                    not isinstance(result, Mapping)
                    or not isinstance(result.get("items"), list)
                    or not isinstance(result.get("summary"), Mapping)
                ):
                    raise TypeError("batch counter response is invalid")
            except Exception as error:
                try:
                    quota_exhausted = bool(self.is_quota_exhausted(error))
                except Exception:
                    quota_exhausted = False
                rate_delay = youtube_rate_limit_retry_after(error) if not quota_exhausted else None
                summary["rate_limited"] = rate_delay is not None
                summary["batch_quota_exhausted"] = quota_exhausted
                summary["batch_unavailable"] = bool(
                    summary.get("batch_unavailable") or (not quota_exhausted and rate_delay is None)
                )
                summary["quota_exhausted"] = bool(
                    summary["quota_exhausted"] or quota_exhausted
                )
                summary["degraded_batch_requests"] += 1
                summary["errors"] += 1
                summary["error_messages"].append(
                    "Batch counter request failed: " + error.__class__.__name__
                )
                self._fail_common(
                    counter_only_ready,
                    "youtube_quota_exhausted"
                    if quota_exhausted
                    else "provider_rate_limited" if rate_delay is not None else "youtube_batch_request_error",
                    summary,
                    retry_after_seconds=3600 if quota_exhausted else rate_delay if rate_delay is not None else 1800,
                )
                self._complete_identity_refresh(
                    [(claim,metadata_by_id[claim.video_id]) for claim in full_ready],summary,
                    attempt_id=attempt_id,recorded_at=recorded_at,counterless_profile=True)
                # A refused Batch request ends provider work for this run:
                # further batches would spend General quota on metadata whose
                # counters cannot be read. Released claims are retried later.
                # Wrappers and operator drains stop on the flags set above.
                summary["stopped_reason"] = summary["stopped_reason"] or (
                    "quota_exhausted" if quota_exhausted else
                    "provider_rate_limited" if rate_delay is not None else "batch_unavailable"
                )
            else:
                items_by_id = {
                    str(item.get("id")): item
                    for item in result["items"]
                    if isinstance(item, Mapping) and item.get("id")
                }
                failed_ids = {
                    str(video_id)
                    for video_id in result["summary"].get("failed_ids") or []
                    if video_id
                }
                deferred = [
                    claim for claim in counter_cohort
                    if (
                        claim.video_id in failed_ids
                        or claim.video_id not in items_by_id
                        or items_by_id[claim.video_id].get("views") is None
                    )
                ]
                event("video_stats_observation", "received", quota_bucket="stats",
                      video_ids=[claim.video_id for claim in counter_cohort],
                      requested_count=len(counter_cohort), received_count=len(items_by_id),
                      missing_count=len(deferred), observed_at=counters_recorded_at)
                if deferred:
                    # Only Stats-only work needs an availability check. General
                    # fallback already observed the profile; do not loop it back.
                    counter_ids = {claim.video_id for claim in counter_only_ready}
                    summary["metadata_deferred"] += sum(claim.video_id in counter_ids for claim in deferred)
                    self._fail_common(
                        [claim for claim in deferred if claim.video_id in counter_ids],
                        STATS_AVAILABILITY_CHECK_REQUIRED,
                        summary,
                        retry_after_seconds=30,
                    )
                    self._complete_identity_refresh(
                        [(claim,metadata_by_id[claim.video_id]) for claim in deferred if claim.video_id not in counter_ids],summary,
                        attempt_id=attempt_id,recorded_at=recorded_at,counterless_profile=True)
                accepted_ids = {
                    claim.video_id for claim in counter_cohort
                    if claim not in deferred
                }
                accepted_counters = [
                    (claim, items_by_id[claim.video_id])
                    for claim in counter_only_ready
                    if claim.video_id in accepted_ids
                ]
                self._complete_common_counters(
                    accepted_counters,
                    summary,
                    attempt_id=attempt_id,
                    recorded_at=counters_recorded_at,
                )
                if not summary["settlement_unknown"]:
                    long_metadata = [
                        (
                            claim,
                            self._with_batch_counters(
                                metadata_by_id[claim.video_id],
                                items_by_id[claim.video_id],
                            ),
                        )
                        for claim in full_ready
                        if (
                            claim.video_type == "long"
                            and claim.video_id in accepted_ids
                        )
                    ]
                    self._complete_common_long_metadata(
                        long_metadata,
                        summary,
                        reason=reason,
                        attempt_id=attempt_id,
                        recorded_at=counters_recorded_at,
                        metadata_observed_at=recorded_at,
                    )
                for video_type in ("short", "stream"):
                    if summary["settlement_unknown"]:
                        break
                    typed_metadata = [
                        (
                            claim,
                            self._with_batch_counters(
                                metadata_by_id[claim.video_id],
                                items_by_id[claim.video_id],
                            ),
                            metadata_route[claim.video_id][0],
                        )
                        for claim in full_ready
                        if (
                            claim.video_type == video_type
                            and claim.video_id in accepted_ids
                        )
                    ]
                    self._complete_common_typed_metadata(
                        video_type,
                        typed_metadata,
                        summary,
                        attempt_id=attempt_id,
                        recorded_at=counters_recorded_at,
                        metadata_observed_at=recorded_at,
                    )

        # A provider failure releases only this exact batch. A failed metadata
        # request leaves other due rows eligible in the same run; a failed
        # Batch counter request or the daily quota latch stops the run.
        if summary["stopped_reason"] is None and summary["quota_exhausted"]:
            summary["stopped_reason"] = "quota_exhausted"
        summary.update(collection_outcome_fields(summary))
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        for key in summary["timings"]:
            summary["timings"][key] = round(summary["timings"][key], 6)
        if progress_callback:
            progress_callback(summary)
        return summary

    def process_typed_claims(
        self,
        claims: Sequence[VideoCollectionClaim],
        *,
        video_type: str,
        reason: str,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        video_type = str(video_type or "").strip().lower()
        if video_type not in TYPED_VIDEO_TYPES:
            raise ValueError("typed video collection requires short or stream")
        summary = _typed_empty_summary(reason, video_type)
        claim_rows = list(claims)
        summary["requested"] = len(claim_rows)
        summary["claimed"] = len(claim_rows)

        claim_counts = Counter(claim.video_id for claim in claim_rows)
        duplicate_claim_ids = {
            video_id for video_id, count in claim_counts.items() if count > 1
        }
        invalid_by_id: dict[str, VideoCollectionClaim] = {}
        valid_claims: list[VideoCollectionClaim] = []
        for claim in claim_rows:
            claim_type = str(
                getattr(claim, "video_type", "long") or "long"
            ).strip().lower()
            if (
                claim.video_id in duplicate_claim_ids
                or claim_type != video_type
                or claim.collection_mode not in TYPED_COLLECTION_MODES[video_type]
            ):
                invalid_by_id.setdefault(claim.video_id, claim)
            else:
                valid_claims.append(claim)
        invalid_claims = list(invalid_by_id.values())
        if invalid_claims:
            self._fail_typed(
                video_type,
                invalid_claims,
                "typed_claim_contract_error",
                summary,
            )
            summary["errors"] += len(invalid_claims)
            summary["error_messages"].append(
                "typed claim contract validation failed: "
                + str(len(invalid_claims))
            )

        common_modes = any(claim.collection_mode in {"counters", "identity_refresh"}
                           for claim in valid_claims)
        for batch_index, batch in enumerate(
            _chunks(valid_claims, TYPED_COLLECTION_BATCH_SIZE)
        ):
            if summary["infrastructure_error"]:
                break
            if common_modes:
                # Retained addressed callers share the same operation router;
                # a typed Stats claim must never become a full General request.
                for key, value in _common_empty_summary(reason).items():
                    summary.setdefault(key, value)
                common = self.process_common_claims(batch, reason=reason)
                summary["requested"] -= len(batch)
                _absorb_common_summary(summary, common, preserve_video_ids=True)
                summary["routing"]["full_metadata_only"] = False
                quota_exhausted = summary["quota_exhausted"]
            else:
                quota_exhausted = self._process_typed_batch(
                    video_type,
                    batch,
                    summary,
                    reason,
                )
            if progress_callback:
                progress_callback(summary)
            if quota_exhausted:
                remaining_claims = valid_claims[
                    (batch_index + 1) * TYPED_COLLECTION_BATCH_SIZE:
                ]
                if common_modes:
                    self._fail_common(remaining_claims, "youtube_quota_exhausted", summary)
                else:
                    self._fail_typed(
                        video_type,
                        remaining_claims,
                        "youtube_quota_exhausted",
                        summary,
                    )
                break
            if summary["stopped_reason"]:
                break

        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        for key in summary["timings"]:
            summary["timings"][key] = round(summary["timings"][key], 6)
        if progress_callback:
            progress_callback(summary)
        return summary

    def process_claims(
        self,
        claims: Sequence[VideoCollectionClaim],
        *,
        reason: str = "auto_stats_batch",
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Compatibility entrypoint; all processing uses the common processor."""
        combined = _common_empty_summary(reason)
        for batch in _chunks(list(claims), TYPED_COLLECTION_BATCH_SIZE):
            summary = self.process_common_claims(batch, reason=reason)
            combined["claimed"] += len(batch)
            _absorb_common_summary(combined, summary, preserve_video_ids=True)
            if progress_callback:
                progress_callback(combined)
            if (
                combined["stopped_reason"] or combined["infrastructure_error"]
                or combined["settlement_unknown"] or combined["quota_exhausted"]
            ):
                break
        combined["finished_at"] = datetime.now(timezone.utc).isoformat()
        return combined


def run_claimed_video_collection(
    repository: VideoCollectionRepository,
    service: VideoCollectionService,
    *,
    limit: int,
    worker_id: str,
    explicit_video_ids: Sequence[str] | None = None,
    channel_scopes: Sequence[Mapping[str, Any]] | None = None,
    reason: str = "auto_stats_batch",
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    force_metadata: bool = False,
) -> dict[str, Any]:
    return run_provider_steps(addressed_video_steps(
        repository, service, limit=limit, worker_id=worker_id,
        explicit_video_ids=explicit_video_ids, channel_scopes=channel_scopes,
        reason=reason, progress_callback=progress_callback,
        force_metadata=force_metadata,
    ))


def addressed_video_steps(
    repository: VideoCollectionRepository,
    service: VideoCollectionService,
    *,
    limit: int,
    worker_id: str,
    explicit_video_ids: Sequence[str] | None = None,
    channel_scopes: Sequence[Mapping[str, Any]] | None = None,
    reason: str = "auto_stats_batch",
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    force_metadata: bool = False,
    on_claimed=None, on_settled=None, should_stop=lambda: False, summary=None,
):
    """Use one common processor, preserving parent scopes for addressed work.

    None starts a NEW addressed action and captures current permission once.
    A supplied scope belongs to existing parent intent and is never refreshed.
    """
    if (not callable(should_stop) or any(callback is not None and not callable(callback)
            for callback in (on_claimed, on_settled))):
        raise ValueError("addressed_video_observer_invalid")
    limit = max(int(limit or 0), 0)
    # Due work without a list belongs to the common pool (run_claimed_common_video_collection).
    if explicit_video_ids is None:
        raise ValueError("addressed video collection requires explicit video IDs")
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("video collection requires a worker ID")
    claim_run_id = str(uuid.uuid4())
    combined = {} if summary is None else summary
    combined.update(_common_empty_summary(reason))
    combined["routing"]["mode"] = "leased_addressed_video_collection"
    combined["routing"]["explicit_video_ids"] = explicit_video_ids is not None
    selected_ids = (
        _unique(explicit_video_ids)[:limit]
        if explicit_video_ids is not None else None
    )

    def finish() -> dict[str, Any]:
        combined.update(collection_outcome_fields(
            combined, explicit_selected=len(selected_ids) if selected_ids is not None else None,
        ))
        combined["finished_at"] = datetime.now(timezone.utc).isoformat()
        combined["claim_chunk_size"] = MAX_ACTIVE_CLAIMS
        combined["lease_seconds"] = DEFAULT_LEASE_SECONDS
        combined["single_attempt_per_run"] = True
        for key in combined["timings"]:
            combined["timings"][key] = round(combined["timings"][key], 6)
        if progress_callback:
            progress_callback(combined)
        return combined

    if limit == 0:
        return finish()

    if selected_ids is not None:
        try:
            # Identity and authorization are captured once for this full action,
            # before preparation/provider work and before splitting claims.
            video_channels = repository.load_video_channel_map(selected_ids)
            captured_scopes = (
                repository.load_channel_scopes(video_channels.values())
                if channel_scopes is None else [dict(scope) for scope in channel_scopes]
            )
            repository.ensure_video_ids(
                selected_ids, channel_scopes=captured_scopes,
                video_channels=video_channels,
            )
        except Exception as error:
            # Preparation can commit before its acknowledgement is lost.
            # Keep its state/leases intact and stop the enclosing work cycle.
            VideoCollectionService._stop_on_unknown_settlement(
                combined, "addressed queue preparation", error,
            )
            combined["stopped_reason"] = "queue_prepare_failed"
            return finish()
    remaining = limit
    offset = 0
    while remaining > 0:
        if should_stop():
            combined.update(yield_requested=True, stopped_reason="collection_stopped")
            break
        if stop_at_boundary(combined):
            break
        claim_limit = min(remaining, MAX_ACTIVE_CLAIMS)
        id_batch = (
            selected_ids[offset:offset + claim_limit]
            if selected_ids is not None else None
        )
        if id_batch == []:
            break
        try:
            claim_ids = (
                repository.claim_video_metadata_ids
                if force_metadata else repository.claim_video_ids
            )
            claims = claim_ids(
                id_batch, channel_scopes=captured_scopes,
                video_channels=video_channels, worker_id=worker_id,
                run_id=claim_run_id, lease_seconds=DEFAULT_LEASE_SECONDS,
            )
        except Exception as error:
            VideoCollectionService._stop_on_unknown_settlement(combined, "video claim", error)
            combined["stopped_reason"] = "claim_failed"
            break
        if on_claimed is not None:
            on_claimed(claims)
        combined["claimed"] += len(claims)
        if not claims:
            if id_batch is None:
                break
            offset += len(id_batch)
            remaining -= len(id_batch)
            continue
        batch_summary = {}
        try:
            result = yield from service.common_claim_steps(claims, reason=reason, summary=batch_summary)
            summary = service.finish_common_claims(claims, result)
        except GeneratorExit:
            # A dispatched caller has already drained only the current known
            # leases. Preserve confirmed earlier batches and this batch's
            # acknowledged General results without running another finalizer.
            _absorb_common_summary(combined, batch_summary, preserve_video_ids=True)
            raise
        except Exception as error:
            # A completion may already have committed. Never fail its leases
            # or start another provider request on an unknown outcome.
            VideoCollectionService._stop_on_unknown_settlement(combined, "video service", error)
            combined["stopped_reason"] = "service_failed"
            break
        if on_settled is not None and not summary.get("settlement_unknown"):
            on_settled(summary)
        _absorb_common_summary(combined, summary, preserve_video_ids=True)
        combined.update(collection_outcome_fields(
            combined, explicit_selected=len(selected_ids) if selected_ids is not None else None,
        ))
        if progress_callback:
            progress_callback(combined)
        if (
            combined["stopped_reason"] or combined["infrastructure_error"]
            or combined["settlement_unknown"] or combined["quota_exhausted"]
        ):
            break
        if id_batch is not None:
            # A busy/hidden ID is skipped for this run, not replaced with a
            # different due video; retain the original explicit workset order.
            offset += len(id_batch)
            remaining -= len(id_batch)
        else:
            remaining -= len(claims)
            if len(claims) < claim_limit:
                break
    return finish()


def run_claimed_initial_video_collection(
    service: VideoCollectionService,
    *,
    job_id: str,
    channel_id: str,
    video_ids: Sequence[str],
    worker_id: str,
    run_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    reason: str = "initial_channel_hydration",
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    concurrency: int = 8,
) -> dict[str, Any]:
    """One job owner; bounded parallel I/O and atomic <=50-video settlements."""
    from collection.initial_video_hydration import run_initial_video_window
    return run_initial_video_window(
        service, job_id=job_id, channel_id=channel_id, video_ids=video_ids,
        worker_id=worker_id, run_id=run_id, lease_seconds=lease_seconds,
        reason=reason, progress_callback=progress_callback, concurrency=concurrency,
    )


def run_claimed_common_video_collection(
    repository: VideoCollectionRepository,
    service: VideoCollectionService,
    *,
    limit: int,
    worker_id: str,
    run_id: str | None = None,
    reason: str = "auto_video_collection",
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Drain one bounded mixed long/short/stream tranche."""

    from collection.auto_pass import AutoPassState

    limit = max(int(limit or 0), 0)
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("common video collection requires a worker ID")
    claim_run_id = (
        str(uuid.uuid4())
        if run_id is None
        else str(uuid.UUID(str(run_id)))
    )
    state = AutoPassState(limit, concurrency=1, run_id=claim_run_id)
    combined = _common_empty_summary(reason)
    combined["started_at"] = datetime.now(timezone.utc).isoformat()
    combined["scan_limit"] = state.scan_limit

    def finish() -> dict[str, Any]:
        combined.update(collection_outcome_fields(combined))
        combined["finished_at"] = datetime.now(timezone.utc).isoformat()
        combined["claim_chunk_size"] = TYPED_COLLECTION_BATCH_SIZE
        combined["lease_seconds"] = DEFAULT_LEASE_SECONDS
        combined["single_attempt_per_run"] = True
        for key in combined["timings"]:
            combined["timings"][key] = round(combined["timings"][key], 6)
        if progress_callback:
            progress_callback(combined)
        return combined

    def absorb(summary: Mapping[str, Any]) -> None:
        _absorb_common_summary(combined, summary)

    if limit == 0:
        combined["stopped_reason"] = "limit_reached"
        return finish()

    # Inventory/membership is authoritative, while this bounded cursor pass
    # materializes newly eligible typed IDs into the shared queue. A prepare
    # failure must not strand already queued work of any type.
    for video_type in ("short", "stream"):
        try:
            prepared = repository.reconcile_typed(
                video_type,
                limit=TYPED_RECONCILE_LIMIT,
            )
            combined[f"{video_type}_queue_prepared"] = int(
                (prepared.get("affected_count") or 0)
                if isinstance(prepared, Mapping)
                else 0
            )
        except Exception as error:
            combined["queue_prepare_errors"] += 1
            combined["errors"] += 1
            combined["warnings"].append(
                f"{video_type} queue prepare failed: "
                + error.__class__.__name__
            )

    sequence = 0
    while state.remaining > 0 and not state.paused:
        if stop_at_boundary(combined):
            break
        if state.inspected >= state.scan_limit:
            combined["stopped_reason"] = "scan_budget"
            break
        sequence += 1
        key = str(sequence)
        claim_options = state.begin_claim(key)
        claim_limit = claim_options['limit']
        try:
            try:
                claim_batch = repository.claim_due_batch(
                    **claim_options,
                    worker_id=worker_id,
                    lease_seconds=DEFAULT_LEASE_SECONDS,
                )
            except Exception:
                state.claim_failed(key)
                raise
            state.on_claimed(key, claim_batch)
        except Exception as error:
            combined["infrastructure_error"] = True
            combined["settlement_unknown"] = True
            combined["stopped_reason"] = "claim_failed"
            combined["errors"] += 1
            combined["error_messages"].append(
                "common claim failed: " + error.__class__.__name__
            )
            break
        claims = list(claim_batch["rows"])
        scan_exhausted = claim_batch["scan_exhausted"]
        combined.update(inspected=state.inspected, paused=state.paused_rows,
                        claimed=state.claimed)
        if not claims:
            if progress_callback:
                progress_callback(combined)
            if scan_exhausted:
                combined["stopped_reason"] = "no_claimable_work"
                break
            continue
        try:
            batch_summary = service.process_common_claims(
                claims,
                reason=reason,
            )
        except Exception as error:
            state.settlement_failed(key)
            # Some receipt-backed completions may already have committed.
            # Never reinterpret the whole batch as failed.
            combined["infrastructure_error"] = True
            combined["settlement_unknown"] = True
            combined["stopped_reason"] = "service_failed"
            combined["errors"] += 1
            combined["error_messages"].append(
                "common service failed: " + error.__class__.__name__
            )
            break
        absorb(batch_summary)
        if batch_summary.get('settlement_unknown'):
            state.settlement_failed(key)
        else:
            state.on_settled(key, batch_summary)
        if progress_callback:
            progress_callback(combined)
        if combined["stopped_reason"]:
            break
        if len(claims) < claim_limit and scan_exhausted:
            combined["stopped_reason"] = "no_claimable_work"
            break

    if combined["stopped_reason"] is None:
        combined["stopped_reason"] = "limit_reached"
    return finish()


def run_claimed_typed_video_collection(
    repository: VideoCollectionRepository,
    service: VideoCollectionService,
    *,
    video_type: str,
    limit: int,
    worker_id: str,
    run_id: str | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    max_duration_seconds: float = TYPED_DEFAULT_MAX_DURATION_SECONDS,
) -> dict[str, Any]:
    """Reconcile and drain one bounded Short or stream current-data tranche."""

    video_type = str(video_type or "").strip().lower()
    if video_type not in TYPED_VIDEO_TYPES:
        raise ValueError("typed video collection requires short or stream")
    limit = max(int(limit or 0), 0)
    if not 0 < max_duration_seconds <= 3600:
        raise ValueError("typed collection duration must be between 0 and 3600 seconds")
    deadline = time.monotonic() + max_duration_seconds
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("typed video collection requires a worker ID")
    if run_id is None:
        claim_run_id = str(uuid.uuid4())
    else:
        claim_run_id = str(uuid.UUID(str(run_id)))
    reason = f"typed_{video_type}_current_data"
    combined = _typed_empty_summary(reason, video_type)
    combined["started_at"] = datetime.now(timezone.utc).isoformat()
    combined["enabled"] = None
    combined["inspected"] = 0
    combined["scan_limit"] = max(1000, min(((limit * 20 + 999) // 1000) * 1000, 2000000))

    def finish() -> dict[str, Any]:
        combined["finished_at"] = datetime.now(timezone.utc).isoformat()
        combined["claim_chunk_size"] = TYPED_COLLECTION_BATCH_SIZE
        combined["lease_seconds"] = DEFAULT_LEASE_SECONDS
        combined["single_attempt_per_run"] = True
        for key in combined["timings"]:
            combined["timings"][key] = round(combined["timings"][key], 6)
        if progress_callback:
            progress_callback(combined)
        return combined

    if limit == 0:
        combined["stopped_reason"] = "limit_reached"
        return finish()

    try:
        reconciliation = repository.reconcile_typed(
            video_type,
            limit=min(limit, TYPED_RECONCILE_LIMIT),
        )
        if not isinstance(reconciliation, Mapping):
            raise TypeError("typed reconciliation result must be a mapping")
        if type(reconciliation.get("enabled")) is not bool:
            raise TypeError("typed reconciliation gate must be a boolean")
    except Exception as error:
        combined["infrastructure_error"] = True
        combined["stopped_reason"] = "reconciliation_failed"
        combined["errors"] += 1
        combined["error_messages"].append(
            "typed reconciliation failed: " + error.__class__.__name__
        )
        return finish()

    combined["reconciliation"] = dict(reconciliation)
    combined["enabled"] = bool(reconciliation.get("enabled"))
    if not combined["enabled"]:
        combined["stopped_reason"] = "runtime_disabled"
        return finish()

    def absorb(summary: Mapping[str, Any]) -> None:
        for key in (
            "requested",
            "updated",
            "batches",
            "errors",
            "missing",
            "unavailable_marked",
            "metadata_refresh_requested",
            "metadata_deferred",
            "source_view_count_absent",
            "source_view_count_invalid",
            "metadata_duplicate_items",
            "stream_lifecycle_probed",
            "publication_waiting",
            "completed_streams",
        ):
            combined[key] += int(summary.get(key) or 0)
        for key in ("video_ids", "missing_video_ids", "error_messages", "warnings"):
            combined[key].extend(summary.get(key) or [])
        for key in combined["timings"]:
            combined["timings"][key] += float(
                (summary.get("timings") or {}).get(key) or 0.0
            )
        combined["quota_exhausted"] = bool(
            combined["quota_exhausted"] or summary.get("quota_exhausted")
        )
        for key in ("infrastructure_error", "settlement_unknown", "rate_limited"):
            combined[key] = bool(combined[key] or summary.get(key))
        if summary.get("stopped_reason"):
            combined["stopped_reason"] = summary["stopped_reason"]
        # Aggregate logs must stay bounded even for a long drain of bad items.
        for key in ("error_messages", "warnings"):
            del combined[key][32:]

    remaining = limit
    while remaining > 0 and not combined["quota_exhausted"]:
        if stop_at_boundary(combined):
            break
        if time.monotonic() >= deadline:
            combined["stopped_reason"] = "time_budget"
            break
        if combined["inspected"] >= combined["scan_limit"]:
            combined["stopped_reason"] = "scan_budget"
            break
        claim_limit = min(remaining, TYPED_COLLECTION_BATCH_SIZE)
        try:
            claim_batch = repository.claim_due_typed_batch(
                video_type=video_type,
                limit=claim_limit,
                worker_id=worker_id,
                run_id=claim_run_id,
                lease_seconds=DEFAULT_LEASE_SECONDS,
            )
            claims = list(claim_batch["rows"])
            inspected = claim_batch["inspected_count"]
            scan_exhausted = claim_batch["scan_exhausted"]
            if (
                type(inspected) is not int
                or not len(claims) <= inspected <= 1000
                or type(scan_exhausted) is not bool
                or len(claims) > claim_limit
                or (not inspected and not scan_exhausted)
            ):
                raise ValueError("typed claim batch contract is invalid")
            combined["inspected"] += inspected
        except Exception as error:
            combined["infrastructure_error"] = True
            combined["stopped_reason"] = "claim_failed"
            combined["errors"] += 1
            combined["error_messages"].append(
                "typed claim failed: " + error.__class__.__name__
            )
            break
        if not claims:
            if progress_callback:
                progress_callback(combined)
            if scan_exhausted:
                combined["stopped_reason"] = "no_claimable_work"
                break
            continue
        combined["claimed"] += len(claims)
        try:
            batch_summary = service.process_typed_claims(
                claims,
                video_type=video_type,
                reason=reason,
            )
        except Exception as error:
            # An unexpected service exception may follow a committed write.
            # Do not reinterpret the whole batch as failed; leases expire and
            # durable receipts remain authoritative for later reconciliation.
            combined["infrastructure_error"] = True
            combined["settlement_unknown"] = True
            combined["stopped_reason"] = "service_failed"
            combined["errors"] += 1
            combined["error_messages"].append(
                "typed service failed: " + error.__class__.__name__
            )
            break
        absorb(batch_summary)
        remaining -= len(claims)
        if progress_callback:
            progress_callback(combined)
        if combined["stopped_reason"]:
            break
        if len(claims) < claim_limit and scan_exhausted:
            combined["stopped_reason"] = "no_claimable_work"
            break

    if combined["stopped_reason"] is None:
        combined["stopped_reason"] = "limit_reached"
    return finish()
