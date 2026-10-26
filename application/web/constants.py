"""Fixed HTTP payload fields, limits and labels."""
import re
from datetime import timedelta

EXPLORER_CAPABILITIES_CONTRACT_VERSION = 1


LEGACY_VIRAL_DISABLED_DETAIL = "Legacy viral runtime is disabled. Use typical-range performance signals instead."


CHANNEL_BACKFILL_TERMINAL_STATUSES = {"completed", "error", "blocked"}


VISIBLE_CHANNEL_COLUMNS = (
    "channel_id,added_at,title,avatar_url,subscribers,views,video_count,long_video_count,"
    "country_code,country_collected_at,channel_created_at,"
    "folder_id,parent_channel_id,initial_long_inventory_status,"
    "initial_28d_stats_status,deep_inventory_status,channel_metadata_status,"
    "channel_discovery_status,channel_metadata_checked_at,"
    "channel_discovery_checked_at,is_hidden"
)


VISIBLE_CHANNEL_FIELDS = (*VISIBLE_CHANNEL_COLUMNS.split(","), "source_expires_at")


OPERATOR_JOBS_MODE_INVALID = {
    "message": "Durable operator job mode is not configured correctly.",
    "code": "operator_jobs_mode_invalid",
}


FRONTEND_VIDEO_COLUMNS = (
    "video_id,channel_id,title,published_at,"
    "checked_at,duration,views,like_count,comment_count,availability_status,"
    "availability_checked_at"
)


EXPLORER_RECENT_VIDEO_WINDOWS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "28d": timedelta(days=28),
}


EXPLORER_RECENT_VIDEO_PAGE_SIZE = 20


EXPLORER_RECENT_VIDEO_SCAN_SIZE = 500


# Each scan page builds the stored catalogue for the whole window, so one
# request reads at most this many; a short page then continues by cursor.
EXPLORER_RECENT_VIDEO_MAX_PAGES = 3


LABEL_NAME_MAX_LENGTH = 40


LABEL_KEY_RE = re.compile(r"^(?:monetized|custom:[0-9a-f-]{36})$")


KEY_REFUSAL_TEXT = {
    "provider_key_rejected": "YouTube rejected the API key. Collection is stopped and no requests are sent "
                             "until set-key stores a key YouTube accepts.",
    "provider_access_denied": "YouTube refused access with the API key. Collection is stopped and no requests "
                              "are sent until set-key stores a key YouTube accepts.",
}


COLLECTION_PAUSED_TEXT = ("Collection is paused: nothing is collected and new channels wait "
                          "until it is turned on.")
