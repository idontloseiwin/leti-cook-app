"""Small scoped PG17 Health scenarios; run only on the disposable Fixture."""
from db.base.read_context import http_method_scope, http_read_scope
from db.reads.queue_health_queries import (get_all_video_queue_health, get_all_discovery_queue_health,
    get_channel_metadata_queue_health, get_system_queue_health)
from db.reads.operational_read_queries import get_typed_video_collection_health
from native_unified_availability import seed, claim, CHANNEL, IDS
from native_pipeline_trace import initial_actual_owner


def pause_playlists(f, kinds):
    """Synthetic inactive state obeys the same runtime/status/lease invariants."""
    f.proof_step = "fixture_pause_playlists"
    rows = f.rows("""UPDATE public.channel_playlist_state SET runtime_enabled=false,status='paused',
        next_due_at=NULL,lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,
        lease_run_id=NULL,lease_expires_at=NULL
        WHERE channel_id=%s AND video_type=ANY(%s) RETURNING video_type""", (CHANNEL, list(kinds)))
    f.check({row["video_type"] for row in rows} == set(kinds), "fixture_playlist_pause_complete")


def health(f):
    before = f.snapshot()
    with http_method_scope("GET"), http_read_scope():
        videos = get_all_video_queue_health(f.database)
        discovery = get_all_discovery_queue_health(f.database)
        metadata = get_channel_metadata_queue_health(f.database)
        combined = get_system_queue_health(f.database)
        disabled = get_system_queue_health(f.database, discovery_enabled=False)
    f.check(before == f.snapshot(), "all_type_health_preserves_domain")
    f.check(videos["total"] == sum(row["total"] for row in videos["by_type"].values()), "video_totals_match_types")
    f.check(discovery["total"] == sum(row["total"] for row in discovery["by_type"].values()), "playlist_totals_match_types")
    f.check(combined == {"videos": videos, "channel_discovery": discovery, "channel_metadata": metadata}, "home_combined_equals_health_queries")
    f.check(disabled == {"videos": videos, "channel_discovery": {"mode": "disabled", "due": None, "failed": None},
                        "channel_metadata": metadata}, "home_discovery_disabled_not_empty")
    return videos, discovery


def queue_policy(f):
    f.proof_step = "queue_seed"
    seed(f)
    f.proof_step = "queue_seed_failures"
    f.rows("UPDATE public.video_collection_state SET consecutive_failures=1,last_error_type='provider_unavailable' WHERE channel_id=%s", (CHANNEL,))
    f.rows("""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,
        status,next_due_at,attempt_count,last_error_code)
        VALUES(%s,'long',%s,true,'ready',now()-interval '1 minute',1,'typed_playlist_not_found')
        ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,status='ready',
        next_due_at=excluded.next_due_at,attempt_count=1,last_error_code=excluded.last_error_code""",
        (CHANNEL, "UULF" + CHANNEL[2:]))
    f.rows("UPDATE public.channel_playlist_state SET status='ready',next_due_at=now()-interval '1 minute',attempt_count=1,last_error_code='provider_unavailable' WHERE channel_id=%s AND video_type<>'long'", (CHANNEL,))
    videos, discovery = health(f)
    f.check(videos["total"] == 3 and videos["due"] == 3 and videos["failed"] == 3, "all_formats_active_failure_candidates")
    f.check(discovery["total"] == 3 and discovery["total_channels"] == 1, "three_playlists_one_channel")
    f.check(discovery["failed"] == 2 and discovery["playlist_not_found"] == 1, "absence_not_technical_error")
    f.proof_step = "queue_legacy_runtime_disable"
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=false WHERE video_type='short'")
    videos, _ = health(f)
    f.check(videos["failed"] == 3 and videos["excluded_failed"] == 0 and videos["runtime_disabled"] == 1, "legacy_runtime_switch_not_shared_queue_authority")
    pause_playlists(f, ("stream",))
    f.proof_step = "queue_playlist_disabled_read"
    videos, discovery = health(f)
    f.check(videos["failed"] == 3 and videos["excluded_failed"] == 0 and videos["playlist_disabled"] == 1, "disabled_playlist_can_still_need_identity_recovery")
    f.check(videos["by_type"]["stream"]["metadata_due"] == 1, "disabled_playlist_recovery_uses_general")
    f.check(discovery["failed"] == 1 and discovery["excluded_failed"] == 1, "disabled_inventory_failure_excluded")
    f.proof_step = "queue_hide_channel"
    f.rows("UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s", (CHANNEL,))
    videos, discovery = health(f)
    f.check(videos["due"] == 0 and videos["failed"] == 0 and videos["policy_paused"] == 3, "hidden_all_video_types_excluded")
    f.check(discovery["due"] == 0 and discovery["failed"] == 0 and discovery["policy_paused"] == 3, "hidden_all_playlists_excluded")
    f.proof_step = "queue_hold_channel"
    f.rows("UPDATE public.channel_collection_state SET is_hidden=false,owner_permission='manual_required' WHERE channel_id=%s", (CHANNEL,))
    videos, discovery = health(f)
    f.check(videos["failed"] == 0 and discovery["failed"] == 0, "held_errors_do_not_claim_active_failure")
    f.proof_step = "queue_resume_and_pause_one_video"
    f.rows("UPDATE public.channel_collection_state SET owner_permission='accepted' WHERE channel_id=%s", (CHANNEL,))
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type='short'")
    f.rows("UPDATE public.channel_playlist_state SET runtime_enabled=true,status='ready',next_due_at=now() WHERE channel_id=%s", (CHANNEL,))
    f.rows("UPDATE public.video_collection_state SET paused_reason='availability_retry_expired' WHERE video_id=%s", (IDS["long"],))
    videos, _ = health(f)
    f.check(videos["failed"] == 2 and videos["excluded_failed"] == 1, "row_pause_excludes_only_its_own_failure")


def scope_independence(f):
    f.proof_step = "independence_seed"
    seed(f)
    f.proof_step = "independence_remove_long_queue"
    f.rows("DELETE FROM public.video_collection_state WHERE video_id=%s", (IDS["long"],))
    f.rows("DELETE FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long'", (CHANNEL,))
    videos, discovery = health(f)
    f.check(videos["by_type"]["long"]["total"] == 0 and videos["due"] == 2, "missing_long_does_not_hide_other_types")
    f.check(discovery["by_type"]["long"]["total"] == 0 and discovery["total_channels"] == 1, "missing_long_playlist_still_has_channel")
    f.proof_step = "independence_unknown_identity"
    f.rows("UPDATE public.video_collection_state SET video_type=NULL,typed_stream_actual_end_at=NULL,typed_source_stream_end_acquired_at=NULL WHERE video_id=%s", (IDS["stream"],))
    videos, _ = health(f)
    f.check(videos["by_type"]["unknown"]["total"] == 1 and videos["identity_pending"] == 1, "unknown_identity_explicit_not_lost")
    f.check(videos["by_type"]["unknown"]["metadata_due"] == 1, "unknown_identity_requires_general")


def initial_handoff(f):
    f.proof_step = "handoff_actual_initial_owner"
    initial_actual_owner(f)
    f.proof_step = "handoff_pending_due"
    f.rows("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 minute' WHERE channel_id=%s", (CHANNEL,))
    videos, _ = health(f)
    f.check(videos["due"] == 0, "all_unsettled_full_initial_targets_not_automatic")
    f.proof_step = "handoff_settle_targets"
    f.rows("UPDATE public.operator_job_targets SET status='checked' WHERE video_id=ANY(%s)", (list(IDS.values()),))
    pause_playlists(f, ("long", "short", "stream"))
    f.proof_step = "handoff_paused_playlist_read"
    videos, _ = health(f)
    f.check(videos["due"] == 3 and videos["policy_paused"] == 0, "confirmed_handoff_survives_initial_channel_and_playlist_pause")


def legacy_initial_typed_work(f):
    f.proof_step = "legacy_initial_seed"
    seed(f)
    f.proof_step = "legacy_initial_pending_channel"
    f.rows("UPDATE public.channels SET initial_long_inventory_status='pending' WHERE channel_id=%s", (CHANNEL,))
    f.rows("UPDATE public.video_collection_state SET consecutive_failures=1,last_error_type='provider_unavailable' WHERE channel_id=%s", (CHANNEL,))
    f.check(f.scalar("SELECT collection_mode FROM readable.channel_control WHERE channel_id=%s", (CHANNEL,)) == "initial_inventory", "legacy_initial_mode")
    videos, _ = health(f)
    f.check(videos["due"] == 2 and videos["failed"] == 2 and videos["by_type"]["long"]["failed"] == 0, "legacy_initial_keeps_ready_typed_failures_visible")
    f.proof_step = "legacy_initial_shared_claim"
    owned = claim(f, kinds=("short", "stream"), operation="counters")["rows"]
    f.check({row.video_type for row in owned} == {"short", "stream"}, "shared_claim_matches_legacy_initial_typed_queue")


def shared_switch_and_recovery(f):
    f.proof_step = "switch_recovery_seed"
    seed(f)
    f.proof_step = "switch_recovery_disable_legacy_runtime"
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=false WHERE video_type IN('short','stream')")
    pause_playlists(f, ("short",))
    f.proof_step = "switch_recovery_seed_failures"
    f.rows("UPDATE public.video_collection_state SET consecutive_failures=1,last_error_type='provider_unavailable' WHERE channel_id=%s", (CHANNEL,))
    videos, _ = health(f)
    f.check(videos["failed"] == 3 and videos["runtime_disabled"] == 2, "shared_queue_failures_survive_legacy_switches")
    with http_method_scope("GET"), http_read_scope():
        both = get_typed_video_collection_health(f.database)
    f.check(both["video_types"]["short"]["due"] == 0
            and both["queue_health"]["by_type"]["short"]["due"] == 1,
            "legacy_runtime_contract_preserved_shared_queue_separate")
    legacy_expected = {"runtime_enabled": False, "total": 1, "policy_paused": 0, "due": 0,
            "leased": 0, "pending": 0, "hydrated": 1, "awaiting_completion": 0,
            "unavailable": 0, "failed": 1, "oldest_overdue_at": None}
    f.check(all(both["video_types"][kind] == legacy_expected for kind in ("short", "stream")),
            "legacy_schema33_fields_and_counts_preserved_exactly")
    f.proof_step = "switch_recovery_general_claim"
    owned = claim(f, kinds=("short",))["rows"]
    f.check(len(owned) == 1 and owned[0].collection_mode == "identity_refresh", "disabled_playlist_still_claims_general_recovery")
    f.proof_step = "switch_recovery_counter_claim"
    stream = claim(f, kinds=("stream",), operation="counters")["rows"]
    f.check(len(stream) == 1 and stream[0].collection_mode == "counters", "shared_stats_ignores_retired_typed_runtime_switch")


def metadata_policy(f):
    from youtube_pipeline_health import aggregate_channels
    f.proof_step = "metadata_seed"
    seed(f)

    def read():
        before = f.snapshot()
        with http_method_scope("GET"), http_read_scope():
            observed = get_channel_metadata_queue_health(f.database)
        f.check(before == f.snapshot(), "metadata_health_preserves_domain")
        rows = f.rows("""SELECT channel_id,is_hidden,collection_permission,collection_control_epoch,
            collection_mode,channel_metadata_status,channel_metadata_next_after,channel_metadata_started_at
            FROM readable.channels_with_control""")
        full = aggregate_channels(rows, f.scalar("SELECT now()"))["channel_metadata"]
        f.check(all(observed[key] == full[key] for key in
            ("due", "failed", "running", "null_schedule", "eligible_channels")), "summary_metadata_matches_health")
        return observed

    f.proof_step = "metadata_error_state"
    f.rows("UPDATE public.channels SET channel_metadata_status='error',channel_metadata_next_after=now()-interval '1 minute' WHERE channel_id=%s", (CHANNEL,))
    observed = read()
    f.check(observed["total"] == 1 and observed["failed"] == 1 and observed["due"] == 1, "metadata_one_channel_not_three_types")
    f.check(observed["oldest_overdue_at"] is not None and observed["status_counts"] == {"error": 1}, "metadata_status_and_age_available")
    f.proof_step = "metadata_hidden_state"
    f.rows("UPDATE public.channel_collection_state SET is_hidden=true WHERE channel_id=%s", (CHANNEL,))
    observed = read()
    f.check(observed["failed"] == 0 and observed["excluded_failed"] == 1 and observed["due"] == 0, "hidden_metadata_error_separate")
    f.proof_step = "metadata_held_state"
    f.rows("UPDATE public.channel_collection_state SET is_hidden=false,owner_permission='manual_required' WHERE channel_id=%s", (CHANNEL,))
    observed = read()
    f.check(observed["failed"] == 0 and observed["excluded_failed"] == 1, "held_metadata_error_separate")
    f.proof_step = "metadata_running_state"
    f.rows("UPDATE public.channel_collection_state SET owner_permission='accepted' WHERE channel_id=%s", (CHANNEL,))
    f.rows("UPDATE public.channels SET channel_metadata_status='running',channel_metadata_next_after=NULL,channel_metadata_started_at=now()-interval '1 minute' WHERE channel_id=%s", (CHANNEL,))
    observed = read()
    f.check(observed["running"] == 1 and observed["due"] == 0 and observed["oldest_running_at"] is not None, "running_metadata_not_due")
    f.proof_step = "metadata_unknown_state"
    f.rows("UPDATE public.channels SET channel_metadata_status=NULL WHERE channel_id=%s", (CHANNEL,))
    observed = read()
    f.check(observed["due"] == 1 and observed["status_counts"] == {"unknown": 1}, "unscheduled_metadata_explicit_unknown")


CASES = (queue_policy, scope_independence, initial_handoff, legacy_initial_typed_work,
         shared_switch_and_recovery, metadata_policy)


if __name__ == "__main__":
    from native_case_runner import run_cases
    raise SystemExit(run_cases("native_all_type_queue_health", CASES))
