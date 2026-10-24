"""Bounded read-only aggregates for Health's all-type queue observations.

The video projection requires schema34. Discovery reads existing admitted views;
neither reader acquires work, changes deadlines or issues provider requests.
"""
from collections.abc import Mapping

from queue_health import validate_queue_health, validate_metadata_queue_health
from ..base.postgres import DatabaseError, Operation
from ..base.sql_values import application_value


def _read_value(database, operation, statement):
    with database.transaction(Operation(operation, read_only=True,
            statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=8000)) as tx:
        row = application_value(tx.fetch_one(statement))
    if not isinstance(row, Mapping) or set(row) != {"value"}:
        raise DatabaseError("database_result_shape_invalid")
    return row["value"]


def _read(database, operation, statement, unit):
    value = _read_value(database, operation, statement)
    try:
        return (validate_metadata_queue_health(value) if unit == "channels"
                else validate_queue_health(value, unit=unit))
    except ValueError:
        raise DatabaseError("queue_health_observation_unavailable") from None


def get_all_video_queue_health(database):
    return _read(database, "operational_all_video_queue_health", VIDEO_SQL, "videos")


def get_all_discovery_queue_health(database):
    return _read(database, "operational_all_discovery_queue_health", DISCOVERY_SQL, "playlists")


def get_channel_metadata_queue_health(database):
    return _read(database, "operational_channel_metadata_queue_health", METADATA_SQL, "channels")


def get_system_queue_health(database, *, discovery_enabled=True):
    """One deadline and snapshot for Home's current queue state, never three waits."""
    value = _read_value(database, "operational_system_queue_health",
                        SYSTEM_SQL if discovery_enabled else SYSTEM_NO_DISCOVERY_SQL)
    try:
        if not isinstance(value, Mapping) or set(value) != {"videos", "channel_discovery", "channel_metadata"}:
            raise ValueError("queue_health_observation_invalid")
        discovery = value["channel_discovery"]
        if not discovery_enabled and discovery is not None:
            raise ValueError("queue_health_observation_invalid")
        return {
            "videos": validate_queue_health(value["videos"], unit="videos"),
            "channel_discovery": validate_queue_health(discovery, unit="playlists") if discovery_enabled
                else {"mode": "disabled", "due": None, "failed": None},
            "channel_metadata": validate_metadata_queue_health(value["channel_metadata"]),
        }
    except ValueError:
        raise DatabaseError("queue_health_observation_unavailable") from None


VIDEO_SQL = "SELECT value->'queue_health' AS value FROM readable.typed_video_collection_health"

METADATA_SQL = """
WITH facts AS MATERIALIZED (
 SELECT channel_metadata_status AS status,channel_metadata_next_after AS next_after,
 channel_metadata_started_at AS started_at,
 coalesce(NOT is_hidden AND collection_permission='accepted' AND collection_control_epoch>0
  AND collection_mode IN('active','metadata_only'),false) AS eligible
 FROM readable.channels_with_control
), grouped AS (
 SELECT CASE WHEN status IN('ok','error','running','pending') THEN status ELSE 'unknown' END AS status,
 count(*) AS count FROM facts WHERE eligible GROUP BY 1
), totals AS (
 SELECT count(*) AS total,count(*) FILTER(WHERE eligible) AS eligible_channels,
 count(*) FILTER(WHERE NOT eligible) AS policy_paused_channels,
 count(*) FILTER(WHERE eligible AND status IS DISTINCT FROM 'running'
  AND (next_after IS NULL OR next_after<=now())) AS due,
 count(*) FILTER(WHERE eligible AND status='error') AS failed,
 count(*) FILTER(WHERE NOT eligible AND status='error') AS excluded_failed,
 count(*) FILTER(WHERE eligible AND status='running') AS running,
 count(*) FILTER(WHERE eligible AND next_after IS NULL) AS null_schedule,
 min(next_after) FILTER(WHERE eligible AND status IS DISTINCT FROM 'running' AND next_after<=now()) AS oldest_overdue_at,
 min(started_at) FILTER(WHERE eligible AND status='running') AS oldest_running_at
 FROM facts
)
SELECT to_jsonb(totals) || jsonb_build_object('schema_version',1,'scope','channel_profiles','unit','channels',
 'status_counts',coalesce((SELECT jsonb_object_agg(status,count) FROM grouped),'{}'::jsonb)) AS value
FROM totals
"""

DISCOVERY_SQL = """
WITH controls AS MATERIALIZED (
 SELECT channel_id,control_epoch,initial_job_id,typed_runtime_eligible
 FROM readable.channel_control
), facts AS (
 SELECT queue.channel_id,queue.video_type,queue.status,queue.next_due_at,
 queue.runtime_enabled,queue.lease_expires_at,queue.resume_pending,
 queue.attempt_count,queue.last_error_code,
 coalesce(control.typed_runtime_eligible,false) AS channel_eligible,
 EXISTS(SELECT 1 FROM readable.operator_jobs job
  WHERE job.id=control.initial_job_id AND job.channel_id=queue.channel_id
  AND job.accepted_epoch=control.control_epoch AND job.initial_scope_version=1
  AND job.phase='inventory' AND job.status<>'completed') AS initial_owned
 FROM readable.channel_playlist_state queue
 LEFT JOIN controls control USING(channel_id)
), candidates AS (
 SELECT facts.*,channel_eligible AND runtime_enabled AND NOT initial_owned
  AND status IN('ready','leased') AS candidate,
 attempt_count>0 AND last_error_code IS NOT NULL
  AND last_error_code NOT IN('typed_playlist_not_found','channel_not_eligible') AS has_failure
 FROM facts
), observed AS (
 SELECT candidates.*,
 candidate AND (status='ready' OR lease_expires_at<=now())
  AND (next_due_at<=now() OR (status='leased' AND lease_expires_at<=now())) AS is_due
 FROM candidates
), groups AS (
 SELECT video_type,grouping(video_type) AS overall,count(*)::bigint AS total,
 count(DISTINCT channel_id)::bigint AS total_channels,
 count(*) FILTER(WHERE is_due)::bigint AS due,
 count(*) FILTER(WHERE candidate AND has_failure)::bigint AS failed,
 count(*) FILTER(WHERE NOT candidate AND has_failure)::bigint AS excluded_failed,
 count(*) FILTER(WHERE NOT candidate)::bigint AS paused,
 count(*) FILTER(WHERE NOT channel_eligible)::bigint AS policy_paused,
 count(*) FILTER(WHERE NOT runtime_enabled)::bigint AS runtime_disabled,
 count(*) FILTER(WHERE status='leased' AND lease_expires_at>now())::bigint AS leased,
 count(*) FILTER(WHERE status='leased' AND lease_expires_at<=now())::bigint AS expired_leases,
 count(*) FILTER(WHERE candidate AND status='ready')::bigint AS ready,
 count(*) FILTER(WHERE candidate AND resume_pending)::bigint AS active_resume_states,
 count(*) FILTER(WHERE last_error_code='typed_playlist_not_found')::bigint AS playlist_not_found,
 count(*) FILTER(WHERE initial_owned)::bigint AS initial_owned,
 min(next_due_at) FILTER(WHERE is_due AND next_due_at<=now()) AS oldest_overdue_at
 FROM observed GROUP BY GROUPING SETS ((video_type),())
), empty AS (
 SELECT jsonb_build_object('total',0,'due',0,'failed',0,'excluded_failed',0,'paused',0,
 'policy_paused',0,'runtime_disabled',0,'leased',0,'expired_leases',0,'ready',0,
 'active_resume_states',0,'playlist_not_found',0,'initial_owned',0,'oldest_overdue_at',NULL) AS value
), typed AS (
 SELECT jsonb_object_agg(kind.video_type,
  coalesce(to_jsonb(summary)-'video_type'-'overall'-'total_channels',empty.value)) AS value
 FROM (VALUES('long'),('short'),('stream')) kind(video_type)
 CROSS JOIN empty LEFT JOIN groups summary ON summary.video_type=kind.video_type AND summary.overall=0
)
SELECT (to_jsonb(summary)-'video_type'-'overall') || jsonb_build_object(
 'schema_version',2,'scope','all_types','unit','playlists',
 'channel_eligibility_checked',true,'operational_eligibility_checked',false,
 'by_type',typed.value) AS value
 FROM groups summary CROSS JOIN typed WHERE summary.overall=1
"""

# SQL fragments are fixed code-owned SELECTs. No values or identifiers come
# from callers; the disabled variant omits discovery instead of querying it.
SYSTEM_SQL = f"""SELECT jsonb_build_object(
 'videos',({VIDEO_SQL}),
 'channel_discovery',({DISCOVERY_SQL}),
 'channel_metadata',({METADATA_SQL})) AS value"""
SYSTEM_NO_DISCOVERY_SQL = f"""SELECT jsonb_build_object(
 'videos',({VIDEO_SQL}), 'channel_discovery',NULL,
 'channel_metadata',({METADATA_SQL})) AS value"""
