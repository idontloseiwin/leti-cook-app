"""Fixed channel discovery projection after retirement of its workflow view.

The field order and joins preserve the historical public read shape. Shared
Python-owned policy tables compile the mode and estimate-count expressions;
PostgreSQL retains one statement snapshot, native timestamps and JSONB casts.
"""
from .channel_read_policy import CHANNEL_CONTROL_SQL, LONG_ESTIMATE_COUNT_SQL

COLUMNS = ('channel_id', 'title', 'subscribers', 'views', 'video_count', 'added_at', 'avatar_url', 'folder_id', 'parent_channel_id', 'uploads_playlist_id', 'deep_bypass', 'deep_bypass_reason', 'deep_inventory_status', 'deep_inventory_page_token', 'deep_inventory_started_at', 'deep_inventory_checked_at', 'deep_inventory_error', 'long_video_count', 'initial_long_inventory_status', 'initial_28d_stats_status', 'is_hidden', 'hidden_at', 'initial_long_inventory_started_at', 'initial_28d_stats_started_at', 'channel_metadata_checked_at', 'channel_metadata_next_after', 'channel_metadata_status', 'channel_metadata_started_at', 'channel_metadata_error', 'channel_metadata_error_count', 'channel_video_count_changed_at', 'country_code', 'country_collected_at', 'initial_long_inventory_error_code', 'initial_long_inventory_error_at', 'initial_long_inventory_error_count', 'channel_created_at', 'channel_discovery_checked_at', 'channel_discovery_next_after', 'channel_discovery_status', 'channel_discovery_started_at', 'channel_discovery_resume_page_token', 'channel_discovery_resume_started_at', 'channel_discovery_no_new_streak', 'channel_discovery_error', 'channel_discovery_error_count', 'initial_inventory_estimate', 'collection_permission', 'collection_control_epoch', 'collection_mode')
_DISCOVERY = {
    'channel_discovery_checked_at': 'source_checked_at',
    'channel_discovery_next_after': 'source_next_after',
    'channel_discovery_status': 'last_result',
    'channel_discovery_started_at': 'discovery_started_at',
    'channel_discovery_resume_page_token': 'resume_page_token',
    'channel_discovery_resume_started_at': 'resume_started_at',
    'channel_discovery_no_new_streak': 'no_new_streak',
    'channel_discovery_error': 'discovery_error',
    'channel_discovery_error_count': 'discovery_error_count',
}
_EXPRESSIONS = {
    'deep_inventory_page_token': 'NULL::text',
    'long_video_count': LONG_ESTIMATE_COUNT_SQL,
    'is_hidden': 'coalesce(control.is_hidden,true)',
    'hidden_at': 'control.hidden_at',
    'collection_permission': "coalesce(control.owner_permission,'legacy_hold'::text)",
    'collection_control_epoch': 'control.control_epoch',
    'collection_mode': "coalesce(control.collection_mode,'operator_hold'::text)",
    **{name: 's.'+source for name,source in _DISCOVERY.items()},
}
CHANNEL_DISCOVERY_SQL = "SELECT " + ",\n".join(
    _EXPRESSIONS.get(name,'c.'+name) + ' AS ' + name for name in COLUMNS
) + " FROM public.channels c LEFT JOIN (" + CHANNEL_CONTROL_SQL + """
) control ON control.channel_id=c.channel_id
LEFT JOIN public.channel_playlist_state s ON s.channel_id=c.channel_id AND s.video_type='long'"""
# Fixed alias usable by both string and psycopg composable read owners.
CHANNEL_DISCOVERY_RELATION = '(' + CHANNEL_DISCOVERY_SQL + ') AS channel_discovery'

# Browser reads use admitted provider fields. Collection keeps the operational
# relation above: masking an old source must not change its scheduling inputs.
CHANNEL_READ_SQL = "SELECT " + ",\n".join(
    _EXPRESSIONS.get(name, 'c.' + name) + ' AS ' + name for name in COLUMNS
) + ",least(c.source_expires_at,s.source_expires_at) AS source_expires_at" + """
 FROM readable.channels c
 LEFT JOIN readable.channel_control control ON control.channel_id=c.channel_id
 LEFT JOIN readable.channel_playlist_state s ON s.channel_id=c.channel_id AND s.video_type='long'"""
CHANNEL_READ_RELATION = '(' + CHANNEL_READ_SQL + ') AS channel_discovery'
