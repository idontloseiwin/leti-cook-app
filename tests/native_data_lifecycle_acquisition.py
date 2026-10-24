"""Small PG17 producer proof. Root supplies an owned schema27 Fixture; no provider."""
from datetime import timedelta
from uuid import uuid4

from collection_policy import typed_due_at
from collection_schedule import CollectionSchedules
from db.inventory.inventory_scan_pages import settle_typed_inventory_page
from db.videos.video_scheduled_claims import claim_due_typed_video_collection_batch
from db.videos.video_missing_completion import complete_typed_video_missing
from db.videos.video_stream_completion import complete_stream_lifecycle_probe
from db.videos.video_typed_metadata_completion import complete_typed_video_metadata_collection
from db.reviews.video_type_review_decisions import decide_video_type_review
from db.videos.video_identity_recovery import complete_video_identity_refresh
from db.base.postgres import Operation
from db.reviews.video_review_evidence import refresh_review_case

CHANNEL = "UCacquisitionproof000001"
SHORT = "acqshort001"
STREAM = "acqstream01"
LIVE = "acqlive0001"


def _page(f, kind, items, observed):
    """Seed only the source lease; execute the real page/queue/receipt owner."""
    scan, lease = str(uuid4()), str(uuid4())
    playlist = ("UUSH" if kind == "short" else "UULV") + CHANNEL[2:]
    f.rows("""INSERT INTO public.channel_video_inventory_scans
        (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,
         scan_boundary_at,started_at,token_history_verified)
        VALUES(%s,%s,%s,%s,'full_reconciliation','running','partial',%s,%s,true)""",
        (scan, CHANNEL, kind, playlist, observed - timedelta(seconds=1), observed - timedelta(seconds=1)))
    f.rows("""INSERT INTO public.channel_playlist_state
        (channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at,inventory_status,
         active_scan_id,lease_token,lease_owner,lease_run_id,lease_expires_at,lease_channel_epoch)
        VALUES(%s,%s,%s,true,'leased',now(),'running',%s,%s,'acquisition-proof','acquisition-proof',
               now()+interval '5 minutes',%s)
        ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,status='leased',
        next_due_at=now(),inventory_status='running',active_scan_id=excluded.active_scan_id,
        lease_token=excluded.lease_token,lease_owner=excluded.lease_owner,
        lease_run_id=excluded.lease_run_id,lease_expires_at=excluded.lease_expires_at,
        lease_channel_epoch=excluded.lease_channel_epoch""",
        (CHANNEL, kind, playlist, scan, lease, f.epoch(CHANNEL)))
    value = settle_typed_inventory_page(f.database, channel_id=CHANNEL, video_type=kind,
        lease_token=lease, scan_id=scan, transition_id=str(uuid4()),
        expected_page_token=None, next_page_token=None, page_status="complete",
        items=items, source_checked_at=observed.isoformat())
    f.check(value["status"] == "completed", "acquisition_page_complete")
    return value


def _state(f, video):
    return f.rows("""SELECT typed_source_published_at,typed_source_published_acquired_at,
        typed_stream_actual_end_at,typed_source_stream_end_acquired_at,
        typed_collection_checked_at,typed_collection_status,next_due_at,lease_token
        FROM public.video_collection_state WHERE video_id=%s""", (video,))[0]


def _claim(f, kind, count):
    value = claim_due_typed_video_collection_batch(
        f.database, kind, count, "acquisition-proof", str(uuid4()), 300)
    f.check(len(value["rows"]) == count, "acquisition_claim_count")
    return {row["video_id"]: row for row in value["rows"]}


def _row(video, kind, token, observed, **extra):
    return dict(video_id=video,channel_id=CHANNEL,video_type=kind,
        lease_token=str(token),collection_attempt_id=str(uuid4()),
        recorded_at=observed.isoformat(),**extra)


def _leased_again(f, video):
    # A future recurrence is represented by its admissible leased fixture state;
    # no deadline is rewritten, and no provider or collector is started.
    token = str(uuid4())
    f.rows("""UPDATE public.video_collection_state SET lease_token=%s,
        lease_owner='acquisition-proof',lease_started_at=now(),
        lease_expires_at=now()+interval '5 minutes',lease_channel_epoch=%s
        WHERE video_id=%s""", (token, f.epoch(CHANNEL), video))
    return token


def run(f):
    f.channel(CHANNEL)
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type IN('short','stream')")
    acquired = f.now - timedelta(minutes=10)
    publication = f.now - timedelta(days=200)
    items = [dict(video_id=SHORT,published_at=publication.isoformat())]
    _page(f, "short", items, acquired)
    inventory = f.rows("SELECT published_at,published_acquired_at FROM public.video_inventory WHERE video_id=%s", (SHORT,))[0]
    pending = _state(f, SHORT)
    f.check(inventory == dict(published_at=publication,published_acquired_at=acquired), "canonical_origin_from_page")
    f.check(pending["typed_source_published_at"] == publication
        and pending["typed_source_published_acquired_at"] == acquired
        and pending["typed_collection_checked_at"] is None
        and pending["typed_collection_status"] == "pending", "pending_origin_without_hydration")

    _page(f, "short", [dict(video_id=SHORT,published_at=(publication+timedelta(days=1)).isoformat())],
        acquired+timedelta(minutes=1))
    f.check(f.rows("SELECT published_at,published_acquired_at FROM public.video_inventory WHERE video_id=%s",(SHORT,))[0] == inventory,
        "reinventory_preserves_canonical_value_and_origin")
    f.check(_state(f, SHORT) == pending, "reinventory_preserves_pending_pair_and_due")

    stream_items = [dict(video_id=v,published_at=publication.isoformat()) for v in (STREAM,LIVE)]
    _page(f, "stream", stream_items, acquired)
    claims = _claim(f, "stream", 2)
    metadata_at, counters_at = acquired+timedelta(minutes=2), acquired+timedelta(minutes=3)
    end = publication+timedelta(hours=1)
    row = _row(STREAM,"stream",claims[STREAM]["lease_token"],counters_at,
        metadata_observed_at=metadata_at.isoformat(),published_at=publication.isoformat(),
        stream_actual_end_at=end.isoformat(),title="Synthetic stream",views=12,like_count=2,
        comment_count=0,availability_status="public",privacy_status="public",upload_status="processed",
        availability_checked_at=metadata_at.isoformat(),youtube_topic_categories=[])
    f.check(complete_typed_video_metadata_collection(f.database,[row]) == 1,"typed_metadata_complete")
    hydrated = _state(f, STREAM)
    f.check(hydrated["typed_source_published_acquired_at"] == metadata_at
        and hydrated["typed_source_stream_end_acquired_at"] == metadata_at
        and hydrated["typed_stream_actual_end_at"] == end, "metadata_owns_both_queue_origins")
    profile = f.rows("SELECT metadata_checked_at,checked_at FROM public.videos WHERE video_id=%s",(STREAM,))[0]
    f.check(profile == dict(metadata_checked_at=metadata_at,checked_at=counters_at), "metadata_counter_clocks_independent")
    f.check(complete_typed_video_metadata_collection(f.database,[row]) == 1 and _state(f,STREAM) == hydrated,
        "exact_metadata_replay_preserves_origins_and_due")

    live_before = _state(f, LIVE)
    observed = f.scalar("SELECT clock_timestamp()")
    live_row = _row(LIVE,"stream",claims[LIVE]["lease_token"],observed,stream_actual_end_at=None,
                   availability_status='public',privacy_status='public',upload_status='uploaded')
    f.check(complete_stream_lifecycle_probe(f.database,[live_row]) == 1,"live_probe_complete")
    live_after = _state(f, LIVE)
    f.check(live_after["typed_source_published_at"] == live_before["typed_source_published_at"]
        and live_after["typed_source_published_acquired_at"] == acquired
        and live_after["typed_collection_checked_at"] == observed,"live_result_does_not_refresh_origin")

    # Real missing writer for both kinds; prepare an existing Short profile only.
    f.rows("""INSERT INTO public.videos(video_id,channel_id,title,published_at,metadata_checked_at,
        privacy_status,upload_status,availability_status,availability_checked_at)
        VALUES(%s,%s,'Synthetic short',%s,%s,'public','processed','public',%s)""",
        (SHORT,CHANNEL,publication,acquired,acquired))
    short_claim = _claim(f,"short",1)[SHORT]
    tokens = {SHORT:short_claim["lease_token"],STREAM:_leased_again(f,STREAM)}
    snapshot = CollectionSchedules().cadence_snapshot()
    for video,kind,anchor in ((SHORT,"short",publication),(STREAM,"stream",end)):
        before = _state(f,video)
        observed = f.scalar("SELECT clock_timestamp()")
        missing = _row(video,kind,tokens[video],observed)
        f.check(complete_typed_video_missing(f.database,[missing]) == 1,"typed_missing_complete")
        after = _state(f,video)
        f.check(all(after[key] == before[key] for key in (
            "typed_source_published_at","typed_source_published_acquired_at",
            "typed_stream_actual_end_at","typed_source_stream_end_acquired_at")),
            "missing_preserves_both_anchor_pairs")
        f.check(after["next_due_at"] == typed_due_at(snapshot,kind,video,anchor,observed,db_now=observed),
            "missing_due_matches_existing_policy")
        status=f.rows("SELECT availability_status,availability_checked_at,privacy_status,upload_status FROM public.videos WHERE video_id=%s",(video,))[0]
        f.check(status == dict(availability_status="unknown_unavailable",availability_checked_at=observed,
            privacy_status=None,upload_status=None),"missing_clears_unknown_status_fields")

    # A second typed observation creates an actual review conflict. The case
    # itself is produced by the runtime review owner, not injected by the test.
    f.rows("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,
        playlist_id,source_published_at,evidence_source,first_seen_at,last_seen_at,last_present_at)
        VALUES(%s,%s,'stream',%s,%s,'typed_playlist_scan',%s,%s,%s)""",
        (SHORT,CHANNEL,"UULV"+CHANNEL[2:],publication,acquired,acquired,acquired))
    with f.database.transaction(Operation("lifecycle_manual_proof",statement_timeout_ms=5000,
            lock_timeout_ms=250,total_timeout_ms=10000)) as tx:
        refresh_review_case(tx,SHORT)
    case = f.rows("SELECT case_id,revision FROM public.video_type_review_cases WHERE video_id=%s",(SHORT,))[0]
    value = decide_video_type_review(f.database,case_id=str(case["case_id"]),request_id=str(uuid4()),
        expected_revision=case["revision"],video_type="stream",reason="Synthetic owner choice")
    f.check(value["outcome"]=="ready","manual_type_change_complete")
    manual = _state(f,SHORT)
    f.check(manual["typed_source_published_at"] == publication
        and manual["typed_source_published_acquired_at"] == acquired
        and manual["typed_source_stream_end_acquired_at"] is None
        and manual["typed_collection_checked_at"] is None,"manual_inherits_origin_not_decision_time")
    # Existing fresh member is now consulted by the real skeleton recovery
    # owner. Admin setup represents the retained-ID state, not a fake completion.
    f.rows("""UPDATE public.video_inventory SET video_type=NULL,published_at=NULL,
        published_acquired_at=NULL,skeleton_since=now() WHERE video_id=%s""",(LIVE,))
    f.rows("""UPDATE public.video_collection_state SET video_type=NULL,typed_collection_status=NULL,
        typed_collection_checked_at=NULL,typed_source_published_at=NULL,
        typed_source_published_acquired_at=NULL,typed_stream_actual_end_at=NULL,
        typed_source_stream_end_acquired_at=NULL,paused_reason=NULL WHERE video_id=%s""",(LIVE,))
    token = _leased_again(f,LIVE)
    observed = f.scalar("SELECT clock_timestamp()")
    recovery = _row(LIVE,"unknown",token,observed,missing=False,title="Synthetic recovered stream",
        published_at=publication.isoformat(),duration="PT5M",availability_status="public",
        privacy_status="public",upload_status="processed")
    f.check(complete_video_identity_refresh(f.database,[recovery])==1,"identity_recovery_complete")
    recovered = f.rows("""SELECT video_type,skeleton_since,published_at,published_acquired_at
        FROM public.video_inventory WHERE video_id=%s""",(LIVE,))[0]
    f.check(recovered==dict(video_type="stream",skeleton_since=None,published_at=publication,
        published_acquired_at=acquired),"identity_recovery_inherits_membership_origin")
    recovered_queue = _state(f,LIVE)
    f.check(recovered_queue["typed_source_published_at"]==publication
        and recovered_queue["typed_source_published_acquired_at"]==acquired,
        "identity_recovery_queue_keeps_same_origin")
    f.check(complete_video_identity_refresh(f.database,[recovery])==1
        and _state(f,LIVE)==recovered_queue,"identity_recovery_replay_does_not_refresh_origin_or_due")
    f.observe()
