"""One playlist page through explicitly supplied process transport and resources."""

from youtube_errors import (
    YouTubeAPIError,
    YouTubePlaylistNotFoundError,
    is_playlist_not_found_response,
    youtube_error_message,
    youtube_response_error,
)


def fetch_playlist_items_page(
    playlist_id,
    page_token=None,
    max_results=50,
    phase="playlist_inventory",
    *,
    youtube_api_get,
    api_key,
    attempt_recorder,
    timeout_seconds,
    emit_event,
    perf_counter,
):
    if not all(callable(callback) for callback in (youtube_api_get, emit_event, perf_counter)):
        raise TypeError("playlist transport, telemetry and clock must be callable")
    params = {
        "part": "contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
    }
    if page_token:
        params["pageToken"] = page_token

    emit_event(
        "api",
        "sync_engine",
        "youtube_api",
        "playlistItems.list",
        direction="request",
        status="started",
        count=max_results,
        quota=1,
    )
    started = perf_counter()
    try:
        http_response = youtube_api_get(
            "playlistItems",
            phase=phase,
            quota_bucket="general",
            attempt_recorder=attempt_recorder,
            params=params,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            quota_units=1,
        )
        try:
            response = http_response.json()
        except ValueError:
            refusal = youtube_response_error(http_response, None)
            if refusal is not None:
                raise refusal from None
            raise
        refusal = youtube_response_error(http_response, response)
        if refusal is not None:
            raise refusal
        if not isinstance(response, dict):
            raise YouTubeAPIError("provider_response_invalid")
        if response.get("error"):
            message = response["error"].get("message", "YouTube playlistItems error")
            if is_playlist_not_found_response(response):
                raise YouTubePlaylistNotFoundError(message)
            rendered = youtube_error_message(
                response,
                "YouTube playlistItems error",
            )
            raise YouTubeAPIError(rendered)
        status = getattr(http_response, "status_code", None)
        if type(status) is int and status >= 400:
            raise YouTubeAPIError("provider_response_invalid")
        emit_event(
            "api",
            "youtube_api",
            "sync_engine",
            "playlistItems.list",
            direction="response",
            status="ok",
            count=len(response.get("items") or []),
            quota=1,
            duration_ms=round((perf_counter() - started) * 1000),
        )
        return response
    except Exception as error:
        emit_event(
            "api",
            "youtube_api",
            "sync_engine",
            "playlistItems.list",
            direction="response",
            status="error",
            count=0,
            quota=1,
            duration_ms=round((perf_counter() - started) * 1000),
            detail=error.__class__.__name__,
        )
        raise
