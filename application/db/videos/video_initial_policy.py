"""Fixed initial-to-regular handoff and retry projections.

Initial job targets record first-load progress, not a second retry policy. The
shared queue owns its next deadline. A completed target permits only that video
to refresh while the channel initial job is still pending or has failed; public
channel/UI and inventory permissions deliberately stay unchanged.
"""

# Deliberately fixed aliases. These templates never accept runtime identifiers.
_INITIAL_TARGET = """EXISTS(SELECT 1 FROM public.channel_collection_state handoff_control
 JOIN public.operator_jobs handoff_job ON handoff_job.id=handoff_control.initial_job_id
 JOIN public.operator_job_targets handoff_target ON handoff_target.job_id=handoff_job.id
 WHERE handoff_control.channel_id={row}.channel_id
 AND handoff_control.owner_permission='accepted' AND NOT handoff_control.is_hidden
 AND handoff_job.channel_id=handoff_control.channel_id
 AND handoff_job.job_type='channel_initial_long_inventory'
 AND handoff_job.initial_scope_version=1 AND handoff_job.targets_frozen_at IS NOT NULL
 AND handoff_job.accepted_epoch=handoff_control.control_epoch
 AND handoff_job.initial_video_types=ARRAY['long','short','stream']::text[]
 AND handoff_job.phase IN('hydrate_videos','verify')
 AND handoff_job.status IN('queued','running','completed','error')
 AND handoff_target.video_id={row}.video_id AND handoff_target.video_type={row}.video_type"""
_HANDOFF = _INITIAL_TARGET + "\n AND handoff_target.status IN('updated','skipped','unavailable','checked'))"
VIDEO_HANDOFF_STATE_SQL = _HANDOFF.format(row='state')
VIDEO_HANDOFF_INVENTORY_SQL = _HANDOFF.format(row='inventory')
VIDEO_HANDOFF_TYPED_SQL = _HANDOFF.format(row='i')

# Playlist runtime is a discovery projection. A failed parent can pause it after
# its full inventory already established identity. First-load video admission
# instead uses the exact live initial chunk; completed targets use the handoff.
# Neither authority restarts discovery or replaces the typed identity proof.
_INITIAL_CHUNK = _INITIAL_TARGET + """
 AND handoff_job.phase='hydrate_videos' AND handoff_job.status='running'
 AND handoff_job.lease_token IS NOT NULL AND handoff_job.lease_owner IS NOT NULL
 AND handoff_job.lease_expires_at>now()
 AND handoff_job.lease_channel_epoch=handoff_control.control_epoch
 AND handoff_target.status='pending' AND handoff_target.job_attempts>0
 AND jsonb_typeof(handoff_job.summary_json->'full_hydration_chunk')='array'
 AND (handoff_job.summary_json->'full_hydration_chunk') ? {row}.video_id)"""
VIDEO_INITIAL_CHUNK_TYPED_SQL = _INITIAL_CHUNK.format(row='i')

# Before handoff an initial target belongs to its first-load coordinator. Do not
# let the automatic queue claim it and compete with that progress protocol.
VIDEO_INITIAL_PENDING_STATE_SQL = """(EXISTS(SELECT 1 FROM public.channel_collection_state initial_control
 JOIN public.operator_jobs initial_job ON initial_job.id=initial_control.initial_job_id
 WHERE initial_control.channel_id=state.channel_id
 AND initial_job.job_type='channel_initial_long_inventory' AND initial_job.initial_scope_version=1
 AND initial_job.status<>'completed') AND NOT (""" + VIDEO_HANDOFF_STATE_SQL + "))"

# The General first load may bypass a normal cadence, but never a shared error
# backoff. Legacy/manual jobs keep their earlier quota-only exception.
INITIAL_RETRY_ALLOWED_SQL = """NOT coalesce(state.next_due_at>now() AND (
 state.last_error_type='youtube_quota_exhausted' OR (
 j.job_type='channel_initial_long_inventory' AND j.initial_scope_version=1
 AND state.last_error_type IS NOT NULL)),false)"""

# Correlated with the updated job row. The earliest runnable target wakes the
# job; live queue leases and retry deadlines are respected without polling.
INITIAL_JOB_NEXT_DUE_SQL = """(SELECT min(greatest(
 CASE WHEN state.lease_token IS NOT NULL AND state.lease_expires_at>now()
 THEN state.lease_expires_at ELSE now() END,
 CASE WHEN state.last_error_type IS NOT NULL AND state.next_due_at>now()
 THEN state.next_due_at ELSE now() END))
 FROM public.operator_job_targets pending LEFT JOIN public.video_collection_state state USING(video_id)
 WHERE pending.job_id=job.id AND pending.status='pending')"""
