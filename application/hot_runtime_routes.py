"""Audited HTTP admission categories; unknown surfaces require API admission."""
from __future__ import annotations

import re

# Local user work never requires the API/collection gate. Any database query
# still requires a live database and the exact installed schema.
ROUTE_ACCESS = {
    'GET /research/capabilities': 'user_work',
    'POST /research/queries/validate': 'user_work',
    'POST /research/queries/execute': 'api_read',
    'GET /saved/collections/{collection_id}/member-identities': 'user_work',
    'DELETE /folders/{folder_id}': 'user_work',
    'DELETE /labels/{label_key}': 'user_work',
    'DELETE /saved/collections/{collection_id}': 'user_work',
    'DELETE /saved/views/{view_id}': 'user_work',
    'GET /': 'user_work',
    'GET /app': 'user_work',
    'GET /app-explorer': 'user_work',
    'GET /app-explorer-v2': 'user_work',
    'GET /app-explorer-v2/{path:path}': 'user_work',
    'GET /app-explorer-v3': 'user_work',
    'GET /app-explorer-v3/assets/{asset_path:path}': 'user_work',
    'GET /app-explorer-v3/{path:path}': 'user_work',
    'GET /app-explorer/{path:path}': 'user_work',
    'GET /app-next': 'user_work',
    'GET /app-next/{path:path}': 'user_work',
    'GET /app/': 'user_work',
    'GET /channels/{channel_id}/backfill-current-stats': 'api_read',
    'GET /channels/{channel_id}/explorer': 'api_read',
    'GET /channels/{channel_id}/explorer/baseline': 'api_read',
    'GET /channels/{channel_id}/explorer/counter-dynamics': 'api_read',
    'GET /channels/{channel_id}/explorer/counter-dynamics-28d': 'api_read',
    'GET /channels/{channel_id}/explorer/derived-insights': 'api_read',
    'GET /channels/{channel_id}/explorer/format-totals': 'api_read',
    'GET /channels/{channel_id}/explorer/profile': 'api_read',
    'GET /channels/{channel_id}/explorer/publications': 'api_read',
    'GET /channels/{channel_id}/explorer/timeline': 'api_read',
    'GET /channels/{channel_id}/explorer/video-growth': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/{video_type}/growth': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/{video_type}/growth/browse': 'api_read',
    'GET /channels/{channel_id}/explorer/video-growth/browse': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/long': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/long/baseline': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/{video_type}/browse': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/long/derived-insights': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/{video_type}/filter-facets': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/long/{video_id}/history': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/long/{video_id}/launch': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/long/{video_id}/lifetime-baseline': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/{video_type}/{video_id}/static-details': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/short': 'api_read',
    'GET /channels/{channel_id}/explorer/videos/stream': 'api_read',
    'GET /channels/{channel_id}/hydration': 'api_read',
    'GET /channels/{channel_id}/videos/long/backfill-current-stats': 'api_read',
    'GET /docs': 'user_work',
    'GET /docs/oauth2-redirect': 'user_work',
    'GET /explorer/capabilities': 'user_work',
    'GET /explorer/channel-library/video-channel': 'api_read',
    'GET /explorer/home-summary': 'api_read',
    'GET /explorer/recent-videos': 'api_read',
    'GET /internal/hot-runtime': 'user_work',
    'GET /internal/update-status': 'user_work',
    'GET /openapi.json': 'user_work',
    'GET /redoc': 'user_work',
    'GET /saved/collections': 'user_work',
    'GET /saved/collections/{collection_id}': 'user_work',
    'GET /saved/collections/{collection_id}/members': 'api_read',
    'GET /saved/views': 'user_work',
    'GET /saved/views/{view_id}': 'user_work',
    'GET /sidebar-data': 'api_read',
    'GET /sidebar-user-data': 'user_work',
    'GET /system-summary': 'user_work',
    'GET /system/run-history': 'user_work',
    'GET /system/hot-runtime': 'user_work',
    'GET /system/pipeline-health': 'user_work',
    'GET /system/video-type-reviews': 'api_read',
    'GET /system/video-type-reviews/hidden': 'user_work',
    'GET /system/video-type-reviews/{case_id}': 'api_read',
    'GET /telemetry/recent': 'user_work',
    'GET /telemetry/stream': 'user_work',
    'GET /user-work/deleted-channels': 'user_work',
    'GET /user-work/deleted-channels/titles': 'api_read',
    'GET /user-work/status': 'user_work',
    'HEAD /': 'user_work',
    'HEAD /docs': 'user_work',
    'HEAD /docs/oauth2-redirect': 'user_work',
    'HEAD /openapi.json': 'user_work',
    'HEAD /redoc': 'user_work',
    'PATCH /folders/{folder_id}': 'user_work',
    'PATCH /labels/{label_key}': 'user_work',
    'PATCH /saved/collections/{collection_id}': 'user_work',
    'PATCH /saved/views/{view_id}': 'user_work',
    'PATCH /user-work/pending/{item_id}': 'user_work',
    'POST /add-channel': 'api_read',
    'POST /channels/{channel_id}/backfill-current-stats': 'api_read',
    'POST /channels/{channel_id}/folder': 'user_work',
    'POST /channels/{channel_id}/hydration': 'api_read',
    'POST /channels/{channel_id}/labels/{label_key}': 'user_work',
    'POST /channels/{channel_id}/videos/long/backfill-current-stats': 'api_read',
    'POST /folders': 'user_work',
    'POST /labels': 'user_work',
    'POST /recalculate-viral': 'user_work',
    'POST /refresh-all': 'user_work',
    'POST /run-deep-stats-channel': 'user_work',
    'POST /saved/collections': 'user_work',
    'POST /saved/collections/{collection_id}/members': 'user_work',
    'POST /saved/collections/{collection_id}/members/remove': 'user_work',
    'POST /saved/views': 'user_work',
    'POST /starter-channels/add': 'api_read',
    'POST /starter-channels/finish': 'user_work',
    'POST /system/video-type-reviews/{case_id}/decision': 'api_read',
    'POST /user-work/backup': 'user_work',
    'POST /user-work/deleted-channels/remove/{identity}': 'user_work',
    'POST /user-work/deleted-channels/{identity}/restore': 'user_work',
    'POST /user-work/pending/reconnect': 'user_work',
    'POST /user-work/pending/{item_id}/resolve': 'user_work',
    'POST /user-work/restore': 'user_work',
    'POST /user-work/restore/preview': 'user_work',
}

# Middleware-owned pages are answered before FastAPI routing.
MIDDLEWARE_PATHS = frozenset((
    '/auth',
    '/auth/assets/auth.css',
    '/auth/assets/auth.js',
    '/auth/claim',
    '/auth/complete',
    '/auth/login',
    '/auth/logout',
    '/auth/register',
    '/auth/session',
    '/setup',
    '/setup/',
    '/setup/assets/setup.css',
    '/setup/assets/setup.js',
    '/setup/prepare',
    '/setup/save',
    '/setup/status',
    '/setup/workspace/start',
))


def _pattern(template):
    parts = re.split(r"(\{[^}]+\})", template)
    return re.compile("".join(
        ".*" if part.endswith(":path}") else "[^/]+" if part.startswith("{")
        else re.escape(part) for part in parts
    ))


_ROUTES = tuple(
    (method, _pattern(path), access)
    for key, access in sorted(ROUTE_ACCESS.items(), key=lambda item: (item[0].count("{"), ":path}" in item[0]))
    for method, path in (key.split(" ", 1),)
)


def route_access(method: str, path: str) -> str:
    """Classify only reviewed surfaces; no generic prefix-based bypass."""
    if path in MIDDLEWARE_PATHS:
        return "user_work"
    for expected_method, pattern, access in _ROUTES:
        if method == expected_method and pattern.fullmatch(path):
            return access
    return "api_read"
