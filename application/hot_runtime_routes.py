"""Audited HTTP admission categories; unknown surfaces require API admission."""
from __future__ import annotations

import re

# Local user work never requires the API/collection gate. Any database query
# still requires a live database and the exact installed schema.
ROUTE_ACCESS = {
    'GET /research/alerts/capabilities': 'user_work',
    'GET /research/alerts/rules': 'user_work',
    'POST /research/alerts/rules': 'api_read',
    'PATCH /research/alerts/rules/{rule_id}': 'api_read',
    'DELETE /research/alerts/rules/{rule_id}': 'user_work',
    'POST /research/alerts/preview': 'api_read',
    'GET /research/alerts/inbox': 'api_read',
    'POST /research/alerts/inbox/{item_id}/read': 'api_read',
    'GET /research/capabilities': 'user_work',
    'GET /research/contracts': 'user_work',
    'GET /research/sources': 'user_work',
    'GET /research/fields': 'user_work',
    'POST /research/fields/preview': 'user_work',
    'POST /research/fields/apply': 'user_work',
    'POST /research/queries/validate': 'user_work',
    'POST /research/queries/execute': 'api_read',
    'POST /research/queries/frozen-ids': 'api_read',
    'POST /research/operations/execute': 'api_read',
    'POST /research/graphs/execute': 'api_read',
    'POST /research/graphs/validate': 'user_work',
    # The MCP endpoint and key management read no API data themselves; every
    # tool call enters the research routes above with its own category.
    'POST /mcp': 'user_work',
    'GET /mcp': 'user_work',
    'DELETE /mcp': 'user_work',
    'GET /agent-setup/{client}': 'user_work',
    'GET /agent-keys': 'user_work',
    'POST /agent-keys': 'user_work',
    'DELETE /agent-keys/{key_id}': 'user_work',
    'GET /agent-connections': 'user_work',
    'DELETE /agent-connections/{connection_id}': 'user_work',
    'GET /oauth/authorize': 'user_work',
    'POST /oauth/authorize': 'user_work',
    # Source layer: the registry, the preview and the daily cap status read no
    # YouTube data and send nothing; execute spends quota like POST /add-channel.
    'GET /sources/registry': 'user_work',
    'GET /sources/acquisitions/status': 'user_work',
    'POST /sources/acquisitions/preview': 'user_work',
    'POST /sources/acquisitions/execute': 'api_read',
    'POST /sources/channel-search/preview': 'user_work',
    'POST /sources/channel-search/execute': 'api_read',
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
    'POST /channels/{channel_id}/explorer/list-check/{video_type}': 'user_work',
    'GET /channels/{channel_id}/explorer/list-inventory': 'api_read',
    'GET /channels/{channel_id}/explorer/profile': 'api_read',
    'GET /channels/{channel_id}/hydration': 'api_read',
    'GET /channels/{channel_id}/videos/long/backfill-current-stats': 'api_read',
    'GET /explorer/channel-library/video-channel': 'api_read',
    'GET /explorer/home-summary': 'api_read',
    'GET /internal/hot-runtime': 'user_work',
    'GET /internal/update-status': 'user_work',
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
    'GET /system/websub': 'user_work',
    'GET /system/video-type-reviews': 'api_read',
    'GET /system/video-type-reviews/hidden': 'user_work',
    'GET /system/video-type-reviews/{case_id}': 'api_read',
    'GET /telemetry/recent': 'user_work',
    'GET /telemetry/stream': 'user_work',
    # Private operational references/client reports only. Each route retains
    # its own owner rechecks; classification never grants authentication.
    'GET /diagnostics/discovery-journal': 'user_work',
    'POST /diagnostics/discovery-journal/browser': 'user_work',
    'GET /user-work/deleted-channels': 'user_work',
    'GET /user-work/deleted-channels/titles': 'api_read',
    'GET /user-work/status': 'user_work',
    'HEAD /': 'user_work',
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
    'POST /saved/views/translate': 'user_work',
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
    '/auth/session',
    # Agent programs' sign-in endpoints (agent_oauth.PUBLIC_PATHS).
    '/.well-known/oauth-authorization-server',
    '/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/mcp',
    '/oauth/register',
    '/oauth/token',
    '/setup',
    '/setup/',
    '/setup/assets/setup.css',
    '/setup/assets/setup.js',
    '/setup/prepare',
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
