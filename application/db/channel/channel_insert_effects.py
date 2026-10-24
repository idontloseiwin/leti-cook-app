"""Fixed channel INSERT side effects for the two supported owner shapes.

Only two closed INSERT shapes are supported. Native defaults and constraints
stay on the original channel INSERT; explicit CTE dependencies keep control,
snapshot and missing Long initialization before statement-level backup guards.
No existing queue row is updated or rescheduled.
"""
from psycopg import sql
from .channel_initial_error_inserts import _CREATE_INSERT, _RESTORE_INSERT
from ..base.postgres import DatabaseError

# During channel INSERT the just-created control is always legacy_hold/visible.
# Hence typed runtime eligibility is false before the later owner permission
# UPDATE in create/bind, independently of supplied legacy channel status.
_INITIAL_PERMISSION = 'legacy_hold'
_INITIAL_RUNTIME = False
_SNAPSHOT_SOURCE = 'youtube_channel_metadata_refresh'


def _closed_insert(statement, *, create):
    text=statement.as_string() if isinstance(statement,sql.Composable) else statement
    if create:
        assert text.endswith(' RETURNING channel_id')
        text=text[:-len(' RETURNING channel_id')]
    else:
        assert 'RETURNING' not in text
    # Every subsequent DML consumes the preceding RETURNING relation. Reading
    # the newly inserted control table in the same statement would see its old
    # snapshot; these exact INSERT facts avoid that visibility dependency.
    return sql.SQL("""WITH channel_write AS (
"""+text+""" RETURNING *), control_write AS (
 INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,is_hidden,hidden_at)
 SELECT channel_id,user_work_id,{},false,NULL FROM channel_write RETURNING channel_id
), snapshot_write AS (
 INSERT INTO public.channel_snapshots(channel_id,recorded_at,views,subscribers,video_count,source)
 SELECT channel.channel_id,channel.channel_metadata_checked_at,channel.views,channel.subscribers,channel.video_count,{}
 FROM channel_write channel JOIN control_write control USING(channel_id)
 WHERE channel.channel_metadata_status='ok' AND channel.channel_metadata_checked_at IS NOT NULL
 ON CONFLICT(channel_id,recorded_at) DO NOTHING RETURNING channel_id
), discovery_write AS (
 INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at,state_origin,inventory_state_origin)
 SELECT channel.channel_id,'long','UULF'||substr(channel.channel_id,3),{},'paused',now(),'legacy_long_backfill','legacy_long_backfill'
 FROM channel_write channel JOIN control_write control USING(channel_id)
 CROSS JOIN (SELECT count(*) FROM snapshot_write) snapshot_done
 ON CONFLICT(channel_id,video_type) DO NOTHING RETURNING channel_id
)
SELECT channel.channel_id FROM channel_write channel
CROSS JOIN (SELECT count(*) FROM discovery_write) discovery_done LIMIT 2""").format(
        sql.Literal(_INITIAL_PERMISSION),sql.Literal(_SNAPSHOT_SOURCE),sql.Literal(_INITIAL_RUNTIME))

_INSERTS={'create':_closed_insert(_CREATE_INSERT,create=True),
          'restore':_closed_insert(_RESTORE_INSERT,create=False)}
_PARAMETER_COUNTS={'create':2,'restore':11}


def channel_insert_statement(family,parameters):
    """Closed driver entry: choose two fixed statements, never caller SQL."""
    if (type(family) is not str or family not in _INSERTS
            or type(parameters) is not tuple or len(parameters)!=_PARAMETER_COUNTS[family]):
        raise DatabaseError('database_channel_insert_capability_invalid')
    return _INSERTS[family],parameters
