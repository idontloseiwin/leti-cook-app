"""Passive bounded collection evidence; no queue, scheduling or database ownership."""
from pipeline_trace import event


# Closed diagnostics: values, provider messages and exception text never enter
# these observations. The queue keeps its existing broader failure code.
_METADATA_VALIDATION_FIELDS = {
    "snippet_missing_or_invalid": "snippet",
    "channel_id_mismatch": "snippet.channelId",
    "published_at_missing": "snippet.publishedAt",
    "published_at_invalid": "snippet.publishedAt",
    "content_details_missing_or_invalid": "contentDetails",
    "status_missing_or_invalid": "status",
    "statistics_missing_or_invalid": "statistics",
    "duration_missing": "contentDetails.duration",
    "duration_invalid": "contentDetails.duration",
    "privacy_status_invalid": "status.privacyStatus",
    "upload_status_invalid": "status.uploadStatus",
    "live_streaming_details_invalid": "liveStreamingDetails",
    "actual_end_time_invalid": "liveStreamingDetails.actualEndTime",
    "stream_completion_evidence_regressed": "liveStreamingDetails.actualEndTime",
    "actual_end_time_future": "liveStreamingDetails.actualEndTime",
    "title_invalid": "snippet.title",
    "tags_invalid": "snippet.tags",
    "category_id_invalid": "snippet.categoryId",
    "topic_details_invalid": "topicDetails",
    "topic_categories_invalid": "topicDetails.topicCategories",
    "default_audio_language_invalid": "snippet.defaultAudioLanguage",
    "default_language_invalid": "snippet.defaultLanguage",
    "caption_invalid": "contentDetails.caption",
    "live_broadcast_content_invalid": "snippet.liveBroadcastContent",
    "scheduled_start_time_invalid": "liveStreamingDetails.scheduledStartTime",
    "actual_start_time_invalid": "liveStreamingDetails.actualStartTime",
    "actual_start_time_future": "liveStreamingDetails.actualStartTime",
    "publication_evidence_conflict": "liveStreamingDetails",
    "validation_unexpected": "metadata",
}


def metadata_validation_failed(claim, reason):
    """Record one refusal independently of the later failure-write receipt."""
    try:
        if not isinstance(reason, str) or reason not in _METADATA_VALIDATION_FIELDS:
            reason = "validation_unexpected"
        event("video_validation", "failed", video_id=claim.video_id,
              channel_id=claim.channel_id, video_type=claim.video_type,
              phase=_METADATA_VALIDATION_FIELDS[reason], reason=reason)
    except Exception:
        pass  # Recording cannot change validation, release or retry behaviour.


def claims_selected(claims, reason):
    """Log the actual claimed deadline; it is not the next scheduled deadline."""
    try:
        rows = list(claims)
        event("video_batch_manifest", "claimed", reason=reason,
              video_ids=[claim.video_id for claim in rows], requested_count=len(rows))
        for claim in rows:
            event("video_selected", "claimed", video_id=claim.video_id,
                  channel_id=claim.channel_id, video_type=claim.video_type,
                  reason=claim.collection_mode, due_at=claim.next_due_at,
                  quota_bucket="stats" if claim.collection_mode == "counters" else "general")
    except Exception:
        pass  # Diagnostic preparation must never alter collection.


def write_events(operation, rows, status, **diagnostics):
    """Only normalized identities/outcomes, never provider fields or lease tokens."""
    try:
        for row in rows:
            reason = operation
            retry = {}
            if operation in ("fail_batch", "fail_typed"):
                reason = row.get("error_type") or "collection_error"
                delay = row.get("retry_after_seconds")
                if type(delay) is int and 0 <= delay <= 2**53 - 1:
                    retry["retry_after_seconds"] = delay
            elif operation == "complete_identity_refresh":
                reason = ("counters_pending" if row.get("counterless_profile") else
                          "unavailable" if row.get("missing") else "identity_refreshed")
            elif operation == "complete_stream_lifecycle":
                reason = row.get("publication_status") or "awaiting_completion"
            elif operation == "complete_missing":
                reason = "unavailable"
            event("video_write", status, video_id=row.get("video_id"),
                  channel_id=row.get("channel_id"), video_type=row.get("video_type"),
                  reason=reason, observed_at=row.get("recorded_at"), **retry, **diagnostics)
    except Exception:
        pass


def write_failed(operation, rows, error):
    # Do not turn an absent acknowledgement into a known rollback. A SQL data
    # or integrity rejection is the one known-refusal contract the service owns.
    try:
        code = getattr(error, "code", None)
        if not (isinstance(code, str) and len(code) == 5 and code.isascii() and code.isalnum()):
            code = None
        rejected = code is not None and code[:2] in ("22", "23")
        write_events(operation, rows, "rejected" if rejected else "unknown",
                     error_code=code, error_type=type(error).__name__)
    except Exception:
        pass


def write(repository, operation, rows, *prefix):
    """Observe the existing repository contract without replay or extra reads."""
    if getattr(repository, "_trace_staging", False) is True:
        return getattr(repository, operation)(*prefix, rows)
    write_events(operation, rows, "started")
    try:
        result = getattr(repository, operation)(*prefix, rows)
    except Exception as error:
        write_failed(operation, rows, error)
        raise
    # Real repository methods enforce exact row counts. A nonconforming test
    # double/alternate adapter must not manufacture acknowledgement evidence.
    write_events(operation, rows,
                 "confirmed" if type(result) is int and result == len(rows) else "unknown")
    return result
