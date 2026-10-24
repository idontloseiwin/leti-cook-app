"""Channel source reads with caller-owned transport and clock."""
import re
from urllib.parse import urlparse, parse_qs, unquote
from typed_playlist import typed_playlist_id
from youtube_client import response_http_status
from youtube_errors import YouTubeDailyQuotaExceededError, is_playlist_not_found_error, provider_refusal_code, youtube_rate_limit_retry_after

class ChannelLookupUnavailable(Exception):
    """YouTube answered with an error (quota, rate limit, access), so nothing is known."""

    def __init__(self, code=None):
        super().__init__(code or "channel_lookup_unavailable")
        self.code = code


CHANNEL_LOOKUP_UNAVAILABLE = {
    "error": "YouTube did not answer the channel lookup. Try again later.",
    "code": "channel_lookup_unavailable",
}
SET_KEY = "sudo python3 manage.py set-key in the installation folder"
# Refusals that waiting does not fix say what to do instead of "try again".
LOOKUP_REFUSALS = {
    "provider_key_rejected": "YouTube rejected the API key, so the channel was not looked up. Collection is "
                             f"stopped until you replace the key: run {SET_KEY}.",
    "provider_access_denied": "YouTube refused access with the API key, so the channel was not looked up. Enable "
                              "YouTube Data API v3 for the key's project and allow this server in the key's "
                              f"restrictions, then run {SET_KEY} (the same key is fine).",
    "provider_quota_exceeded": "Today's YouTube quota for this key is used up, so the channel was not looked up. "
                               "Try again after the daily reset at midnight Pacific time.",
}


def lookup_refusal(code):
    """The answer for a lookup YouTube refused; a passing failure keeps the retry advice."""
    if code in LOOKUP_REFUSALS:
        return {"error": LOOKUP_REFUSALS[code], "code": code}
    return dict(CHANNEL_LOOKUP_UNAVAILABLE)


def response_refusal_code(response):
    try:
        payload = response.json()
    except Exception:
        payload = None
    return provider_refusal_code(response_http_status(response), payload)


VIDEO_ID_RE = re.compile("^[a-zA-Z0-9_-]+$")


def get_channel_id_from_video(video_id, *, youtube_api_get, api_key, attempt_recorder, timeout_seconds):
    params = {"part": "snippet", "id": video_id}
    response = youtube_api_get(
        "videos",
        phase="channel_resolve_video",
        quota_bucket="general",
        attempt_recorder=attempt_recorder,
        params=params,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        quota_units=1,
    )
    if response_http_status(response) != 200:
        raise ChannelLookupUnavailable(response_refusal_code(response))
    items = response.json().get("items")
    if items:
        return items[0]["snippet"]["channelId"]
    return None


def extract_video_id_from_input(raw):
    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    path = parsed.path or ""

    def clean_video_id(value):
        if value and VIDEO_ID_RE.match(value):
            return value
        return None

    if host in {"youtu.be", "www.youtu.be"}:
        return clean_video_id(path.lstrip("/").split("/", 1)[0])

    is_youtube_host = not host or host == "youtube.com" or host.endswith(".youtube.com")
    if not is_youtube_host:
        return None

    if path == "watch" or path.endswith("/watch"):
        video_ids = parse_qs(parsed.query).get("v") or []
        return clean_video_id(video_ids[0] if video_ids else None)

    shorts = re.search(r"(?:^|/)shorts/([a-zA-Z0-9_-]+)", path)
    if shorts:
        return shorts.group(1)

    return None


def parse_channel_input(raw, *, get_channel_id_from_video):
    raw = unquote(raw.strip())
    video_id = extract_video_id_from_input(raw)
    if video_id:
        channel_id = get_channel_id_from_video(video_id)
        return ("id", channel_id) if channel_id else (None, None)
    uc_match = re.search(r'UC[a-zA-Z0-9_-]{22}', raw)
    if uc_match:
        return "id", uc_match.group()
    handle_match = re.search(r'@([^/?#\s]+)', raw)
    if handle_match:
        return "handle", "@" + handle_match.group(1)
    if re.match(r'^[^\s/?#]+$', raw):
        return "handle", "@" + raw
    return None, None


def estimate_initial_inventory(channel_id, *, fetch_playlist_items_page, clock):
    """Observe three typed playlist sizes; no traversal, counters or DB writes.

    A missing playlist is an explicit unavailable source, not a proved empty
    inventory. Unknown/error counts remain null and cannot authorize auto work.
    """
    video_types = ["long", "short", "stream"]
    counts = {video_type: None for video_type in video_types}
    statuses = {video_type: "error" for video_type in video_types}
    checked_at_by_type = {video_type: None for video_type in video_types}
    for video_type in video_types:
        playlist_id = typed_playlist_id(channel_id, video_type)
        if not playlist_id:
            continue
        try:
            page = fetch_playlist_items_page(
                playlist_id, max_results=1, phase="channel_inventory_count",
            )
            count = (page.get("pageInfo") or {}).get("totalResults")
            if isinstance(count, bool) or not isinstance(count, (int, str)):
                continue
            if isinstance(count, str) and not count.isdecimal():
                continue
            count = int(count)
            if count < 0:
                continue
            counts[video_type] = count
            statuses[video_type] = "ok"
            checked_at_by_type[video_type] = clock().isoformat()
        except YouTubeDailyQuotaExceededError:
            # Do not send the remaining count probes after an exhausted quota.
            break
        except Exception as error:
            if (error.__class__.__name__ == "YouTubeDailyQuotaExceededError"
                    or youtube_rate_limit_retry_after(error) is not None):
                break
            if not is_playlist_not_found_error(error):
                # Store only the fixed outcome; no provider payload/error text.
                continue
            counts[video_type] = None
            statuses[video_type] = "playlist_not_found"
            checked_at_by_type[video_type] = clock().isoformat()
    return {
        "scope": "all", "video_types": video_types,
        "counts": counts, "statuses": statuses,
        "checked_at": min(checked_at_by_type.values()) if all(checked_at_by_type.values()) else None,
        "checked_at_by_type": checked_at_by_type,
    }


def lookup_channel_response(params, *, youtube_api_get, api_key, attempt_recorder, timeout_seconds):
    return youtube_api_get('channels', phase='add_channel', quota_bucket='general', attempt_recorder=attempt_recorder, params=params, api_key=api_key, timeout_seconds=timeout_seconds, quota_units=1)
