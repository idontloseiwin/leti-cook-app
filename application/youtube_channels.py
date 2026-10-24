"""Channel metadata through explicitly supplied process transport and resources."""

from youtube_errors import YouTubeAPIError, youtube_error_message, youtube_response_error


CHANNEL_METADATA_MAX_IDS_PER_REQUEST = 50


def fetch_channel_metadata_batch(
    channel_ids, *, youtube_api_get, api_key, attempt_recorder, timeout_seconds,
    emit_event, perf_counter, max_results=CHANNEL_METADATA_MAX_IDS_PER_REQUEST,
):
    if not channel_ids:
        return []

    emit_event(
        "api",
        "sync_engine",
        "youtube_api",
        "channels.list",
        direction="request",
        status="started",
        count=len(channel_ids),
        quota=1,
    )
    started = perf_counter()
    try:
        response = youtube_api_get(
            "channels",
            phase="channel_metadata",
            quota_bucket="general",
            attempt_recorder=attempt_recorder,
            params={
                "part": "snippet,statistics,contentDetails,brandingSettings",
                "id": ",".join(channel_ids),
                "maxResults": max_results,
            },
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            quota_units=1,
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
        if data.get("error"):
            raise YouTubeAPIError(youtube_error_message(data, "YouTube channels metadata error"))
        response.raise_for_status()
        items = data.get("items", [])
        emit_event(
            "api",
            "youtube_api",
            "sync_engine",
            "channels.list",
            direction="response",
            status="ok",
            count=len(items),
            quota=1,
            duration_ms=round((perf_counter() - started) * 1000),
        )
        return items
    except Exception as error:
        emit_event(
            "api",
            "youtube_api",
            "sync_engine",
            "channels.list",
            direction="response",
            status="error",
            count=0,
            quota=1,
            duration_ms=round((perf_counter() - started) * 1000),
            detail=error.__class__.__name__,
        )
        raise
