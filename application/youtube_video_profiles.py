"""Video metadata and full-profile assembly with explicit process resources."""

from collections.abc import Mapping

from youtube_errors import (YouTubeAPIError, key_refusal_code,
                            youtube_error_message, youtube_response_error)


def fetch_video_profiles(
    video_ids, phase="video_stats", *, batch_stats_enabled, get_video_metadata,
    fetch_video_batch_stats, api_key, attempt_recorder, timeout_seconds,
    counter_parts, api_error,
):
    """Fetch full profiles with explicit metadata/Batch provider dependencies.

    The common queue uses get_video_metadata and packs its own mixed counter
    batch. Older explicit full-profile callers retain this composed interface.
    A failed counter never becomes an absent video or a General fallback.
    """
    video_ids = list(video_ids)
    if not video_ids:
        return []
    if not isinstance(batch_stats_enabled, bool):
        raise TypeError("batch_stats_enabled must be an explicit bool")
    if not isinstance(api_error, type) or not issubclass(api_error, Exception):
        raise TypeError("api_error must be an exception class")
    if not callable(get_video_metadata) or not callable(fetch_video_batch_stats):
        raise TypeError("metadata and Batch providers must be callable")
    if not batch_stats_enabled:
        raise api_error("Batch video statistics is disabled")
    if any(
        not isinstance(video_id, str)
        or not video_id
        or video_id != video_id.strip()
        for video_id in video_ids
    ):
        raise api_error("Video request identities are invalid")
    video_ids = list(dict.fromkeys(video_ids))
    all_videos = []
    for offset in range(0, len(video_ids), 50):
        items = get_video_metadata(video_ids[offset:offset + 50], phase=phase)
        if not items:
            continue

        # The Batch client validates unique/exact IDs and complete success/fail
        # accounting. Whole-request failures propagate: no General fallback.
        counter_result = fetch_video_batch_stats(
            [item["id"] for item in items],
            api_key=api_key,
            attempt_recorder=attempt_recorder,
            timeout_seconds=timeout_seconds,
            phase=phase,
            parts=counter_parts,
        )
        counters_by_id = {item["id"]: item for item in counter_result["items"]}
        failed_ids = set(counter_result["summary"]["failed_ids"])
        for metadata in items:
            video = dict(metadata)
            # Discard even an unsolicited General statistics section.
            video["statistics"] = {}
            counters = counters_by_id.get(video["id"])
            if counters is not None and video["id"] not in failed_ids:
                video["statistics"] = {
                    "viewCount": counters.get("views"),
                    "likeCount": counters.get("like_count"),
                    "commentCount": counters.get("comment_count"),
                }
            all_videos.append(video)
    return all_videos


def fetch_video_metadata(
    video_ids, phase="video_metadata", *, reject_identity_anomalies=True, include_statistics=False,
    youtube_api_get, api_key, attempt_recorder, timeout_seconds, emit_event, perf_counter,
):
    """Fetch General metadata, optionally including counters in the same call.

    Recurring metadata-due work requests complete profiles; metadata-only
    callers retain their existing request. Neither path spends Batch quota.

    By default a response that names an unrequested or repeated video fails
    the whole request. The common queue passes reject_identity_anomalies=False
    and fails only the anomalous videos (_process_common_claims).
    """
    if not isinstance(include_statistics, bool):
        raise TypeError("include_statistics must be an explicit bool")
    video_ids = list(video_ids)
    if not video_ids:
        return []
    if any(
        not isinstance(video_id, str)
        or not video_id
        or video_id != video_id.strip()
        for video_id in video_ids
    ):
        raise YouTubeAPIError("Video request identities are invalid")
    video_ids = list(dict.fromkeys(video_ids))
    all_videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        params = {
            "part": (
                "snippet,contentDetails,status,topicDetails,"
                "liveStreamingDetails" + (",statistics" if include_statistics else "")
            ),
            "id": ",".join(batch),
        }
        emit_event(
            "api",
            "sync_engine",
            "youtube_api",
            "videos.list",
            direction="request",
            status="started",
            count=len(batch),
            quota=1,
        )
        started = perf_counter()
        try:
            response = youtube_api_get(
                "videos",
                phase=phase,
                quota_bucket="general",
                attempt_recorder=attempt_recorder,
                params=params,
                api_key=api_key,
                timeout_seconds=timeout_seconds,
                quota_units=1,
                **({"complete_profile": True} if include_statistics else {}),
            )
            try:
                data = response.json()
            except ValueError:
                refusal = youtube_response_error(response, None)
                if refusal is not None:
                    raise refusal from None
                raise
            refusal = youtube_response_error(response, data)
            if refusal is not None:
                raise refusal
            if not isinstance(data, Mapping):
                raise YouTubeAPIError("YouTube videos response is invalid")
            if data.get("error"):
                raise YouTubeAPIError(youtube_error_message(data, "YouTube videos error"))
            response.raise_for_status()
            from youtube_response_trace import provider_response_observed
            provider_response_observed(batch, data, endpoint="videos",
                                       include_statistics=include_statistics)
            if "items" not in data or not isinstance(data.get("items"), list):
                raise YouTubeAPIError(
                    "YouTube videos response items are invalid"
                )
            items = data["items"]
            requested_ids = set(batch)
            returned_ids = set()
            for item in items:
                if not reject_identity_anomalies:
                    break
                video_id = item.get("id") if isinstance(item, Mapping) else None
                if (
                    not isinstance(video_id, str)
                    or video_id not in requested_ids
                    or video_id in returned_ids
                ):
                    raise YouTubeAPIError("Video metadata response identities are invalid")
                returned_ids.add(video_id)
            emit_event(
                "api",
                "youtube_api",
                "sync_engine",
                "videos.list",
                direction="response",
                status="ok",
                count=len(items),
                quota=1,
                duration_ms=round((perf_counter() - started) * 1000),
            )
        except Exception as error:
            emit_event(
                "api",
                "youtube_api",
                "sync_engine",
                "videos.list",
                direction="response",
                status="error",
                count=0,
                quota=1,
                duration_ms=round((perf_counter() - started) * 1000),
                detail=error.__class__.__name__,
            )
            raise
        for item in items:
            if not isinstance(item, Mapping):
                all_videos.append(item)  # counted as an anomaly by the caller
                continue
            metadata = dict(item)
            # Metadata-only callers cannot accidentally renew counter clocks.
            if not include_statistics:
                metadata.pop("statistics", None)
            all_videos.append(metadata)
    return all_videos


def fetch_initial_video_profiles(
    video_ids, phase="initial_hydration", *, youtube_api_get, api_key,
    attempt_recorder, timeout_seconds, emit_event, perf_counter,
):
    """Fetch one initial-load batch with metadata and counters from General.

    This explicit boundary is separate from recurring metadata/Batch routing.
    Strict identities make an omitted requested ID usable as missing evidence;
    duplicates or foreign IDs invalidate the response before any settlement.
    """
    video_ids = list(video_ids)
    if not video_ids:
        return []
    if any(not isinstance(value, str) or not value or value != value.strip()
           for value in video_ids):
        raise YouTubeAPIError("Video request identities are invalid")
    video_ids = list(dict.fromkeys(video_ids))
    if len(video_ids) > 50:
        raise ValueError("Initial video profiles are limited to 50 IDs")
    params = {
        "part": "snippet,contentDetails,status,topicDetails,liveStreamingDetails,statistics",
        "id": ",".join(video_ids),
    }
    emit_event("api", "sync_engine", "youtube_api", "videos.list",
               direction="request", status="started", count=len(video_ids), quota=1)
    started = perf_counter()
    try:
        response = youtube_api_get(
            "videos", phase=phase, quota_bucket="general",
            attempt_recorder=attempt_recorder, params=params, api_key=api_key,
            timeout_seconds=timeout_seconds, quota_units=1, initial_profile=True,
        )
        status = getattr(response, "status_code", None)
        try:
            data = response.json()
        except ValueError:
            refusal = youtube_response_error(response, None)
            if refusal is not None:
                raise refusal from None
            raise
        refusal = youtube_response_error(response, data)
        if refusal is not None:
            raise refusal
        if not isinstance(data, Mapping):
            raise YouTubeAPIError("YouTube videos response is invalid")
        if data.get("error"):
            error = YouTubeAPIError(key_refusal_code(status, data) or "YouTube initial profiles request refused")
            error.code = key_refusal_code(status, data) or "api_error_response"
            raise error
        response.raise_for_status()
        from youtube_response_trace import provider_response_observed
        provider_response_observed(video_ids, data, endpoint="videos")
        if not isinstance(data.get("items"), list):
            raise YouTubeAPIError("YouTube videos response items are invalid")
        requested_ids = set(video_ids)
        returned_ids = set()
        items = []
        for item in data["items"]:
            video_id = item.get("id") if isinstance(item, Mapping) else None
            if (not isinstance(video_id, str) or video_id not in requested_ids
                    or video_id in returned_ids):
                raise YouTubeAPIError("Video initial response identities are invalid")
            returned_ids.add(video_id)
            items.append(dict(item))
        emit_event("api", "youtube_api", "sync_engine", "videos.list",
                   direction="response", status="ok", count=len(items), quota=1,
                   duration_ms=round((perf_counter() - started) * 1000))
        return items
    except Exception as error:
        emit_event("api", "youtube_api", "sync_engine", "videos.list",
                   direction="response", status="error", count=0, quota=1,
                   duration_ms=round((perf_counter() - started) * 1000),
                   detail=error.__class__.__name__)
        raise
