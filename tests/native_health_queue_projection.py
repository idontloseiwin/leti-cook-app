"""Small PG17 cases for schema33 Health reads; use only a disposable Fixture."""
from db.base.read_context import http_method_scope, http_read_scope
from db.reads.system_health import SystemHealthRepository, CHANNEL_HEALTH_ORDER
from youtube_pipeline_health import CHANNEL_COLUMNS, aggregate_channels
from native_unified_availability import seed, CHANNEL, IDS
from native_pipeline_trace import initial_actual_owner


def health(f):
    before = f.snapshot()
    with http_method_scope('GET'), http_read_scope():
        long = f.database.read_row('health_test_long', 'SELECT * FROM readable.video_collection_health')
        typed = f.database.read_row('health_test_typed', 'SELECT value FROM readable.typed_video_collection_health')['value']
        discovery = f.database.read_row('health_test_discovery', 'SELECT * FROM readable.channel_discovery_health')
        channels, _ = SystemHealthRepository(f.database).fetch_paginated(
            'channels', CHANNEL_COLUMNS, operations=CHANNEL_HEALTH_ORDER)
    f.check(before == f.snapshot(), 'health_reads_preserve_domain_rows')
    return long, typed, discovery, aggregate_channels(channels, f.now)


def queue_policy(f):
    seed(f)
    f.rows("UPDATE public.channels SET channel_metadata_status='error',channel_metadata_next_after=now()-interval '1 minute' WHERE channel_id=%s", (CHANNEL,))
    f.rows("""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,
        runtime_enabled,status,next_due_at,attempt_count,last_error_code)
        VALUES(%s,'long',%s,true,'ready',now()-interval '1 minute',1,'typed_playlist_not_found')
        ON CONFLICT(channel_id,video_type) DO UPDATE SET status='ready',runtime_enabled=true,
        next_due_at=excluded.next_due_at,attempt_count=1,last_error_code=excluded.last_error_code""",
        (CHANNEL, 'UULF' + CHANNEL[2:]))
    long, typed, discovery, channels = health(f)
    f.check(long['due'] == 1 and long['policy_paused'] == 0, 'active_long_due')
    f.check(all(typed['video_types'][k]['due'] == 1 for k in ('short', 'stream')), 'active_all_types_due')
    f.check(discovery['failed'] == 0 and discovery['playlist_not_found'] == 1, 'playlist_absence_not_failure')
    f.check(channels['sync_control']['mode_counts'] == {'active': 1}, 'real_projection_active_channel')
    f.check(channels['channel_metadata']['due'] == 1, 'real_projection_metadata_due')
    f.rows("UPDATE public.channel_playlist_state SET last_error_code='provider_unavailable' WHERE channel_id=%s AND video_type='long'", (CHANNEL,))
    f.check(health(f)[2]['failed'] == 1, 'provider_failure_counted')
    f.rows("UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s", (CHANNEL,))
    long, typed, discovery, channels = health(f)
    f.check(long['due'] == 0 and long['oldest_overdue_at'] is None and long['policy_paused'] == 1, 'hidden_deadline_not_overdue')
    f.check(all(typed['video_types'][k]['due'] == 0 and typed['video_types'][k]['policy_paused'] == 1 for k in ('short', 'stream')), 'hidden_all_types_paused')
    f.check(discovery['failed'] == 0 and discovery['paused'] == 1, 'hidden_discovery_not_failed')
    f.rows("UPDATE public.channel_collection_state SET is_hidden=false,owner_permission='manual_required' WHERE channel_id=%s", (CHANNEL,))
    f.check(health(f)[0]['due'] == 0, 'held_deadline_not_overdue')
    f.rows("UPDATE public.channel_collection_state SET owner_permission='accepted' WHERE channel_id=%s", (CHANNEL,))
    f.check(health(f)[0]['due'] == 1, 'resumed_channel_due_again')
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=false WHERE video_type='short'")
    f.check(health(f)[1]['video_types']['short']['due'] == 0, 'disabled_runtime_not_due')


def initial_handoff(f):
    initial_actual_owner(f)  # Real admission/write owners with fake provider callbacks.
    f.rows("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 minute' WHERE channel_id=%s", (CHANNEL,))
    f.check(health(f)[0]['due'] == 0, 'unsettled_initial_target_not_automatic')
    # Explicit fixture boundary: the initial coordinator has settled these
    # targets, while their parent channel still has work left to do.
    f.rows("UPDATE public.operator_job_targets SET status='checked' WHERE video_id=ANY(%s)", (list(IDS.values()),))
    long, typed, _, channels = health(f)
    f.check(channels['sync_control']['mode_counts'] == {'initial_inventory': 1}, 'initial_channel_still_incomplete')
    f.check(long['due'] == 1 and long['policy_paused'] == 0, 'completed_initial_target_remains_eligible')
    f.check(all(typed['video_types'][k]['due'] == 1 for k in ('short', 'stream')), 'all_initial_handoffs_remain_eligible')
