"""Real DB publication lifecycle proof in the existing disposable schema CI.

No provider/network calls, no new queue, at most1000 synthetic rows per case.
Runtime owners execute as service_role; reader assertions execute as lic_reader.
"""
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
import json
import os
import re
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests"), str(ROOT / "tools")]
from schema25_regressions.fixtures import Fixture, admission, dsns
from native_unified_availability import seed, claim, payload, missing_payload, instant, state, IDS, CHANNEL, KINDS
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseServerError
from db.reads.channel_publications import _PAGE
from psycopg.types.json import Jsonb
from psycopg.errors import CheckViolation


def waiting(owned, observed, *, scheduled=None, status="upcoming"):
    return payload(owned, observed, title="Synthetic waiting publication", duration=None,
        published_at=(observed-timedelta(days=200)).isoformat(), publication_status=status,
        scheduled_start_at=scheduled.isoformat() if scheduled else None,
        availability_status="public", privacy_status="public", upload_status="processed",
        stream_actual_end_at=None, tags=[], youtube_topic_categories=[])


def clear_stream_end(f):
    f.rows("UPDATE public.videos SET stream_actual_end_at=NULL WHERE channel_id=%s", (CHANNEL,))
    f.rows("UPDATE public.video_collection_state SET typed_stream_actual_end_at=NULL, "
        "typed_source_stream_end_acquired_at=NULL WHERE channel_id=%s", (CHANNEL,))


def reader(f, sql, args=()):
    with f.admin.transaction():
        f.admin.execute("SET LOCAL ROLE lic_reader")
        return f.admin.execute(sql, args).fetchall()


def source_rows(f):
    return f.rows("SELECT video_id,publication_status,scheduled_start_at,metadata_checked_at,checked_at,views "
        "FROM public.videos WHERE channel_id=%s ORDER BY video_id", (CHANNEL,))


def waiting_all_types(f):
    acquired, _ = seed(f, operation="metadata")
    clear_stream_end(f)
    # The reviewed schema31 contract admits an unknown type only for identity
    # refresh and lifecycle probes; NULL never slips through ordinary receipts.
    allowed={"identity_refresh":{None},"stream_probe":{None,"long","short","stream"},
             "metadata":{"short","stream"},"missing":{"long","short","stream"}}
    for kind in (*allowed,"invalid_kind"):
        for video_type in (None,"long","short","stream","unknown","invalid_type"):
            accepted=True
            try:
                with f.admin.transaction(force_rollback=True):
                    f.admin.execute("INSERT INTO public.typed_video_collection_receipts(video_id,collection_attempt_id,"
                        "channel_id,video_type,lease_token,receipt_kind,recorded_at,payload_fingerprint) "
                        "VALUES(%s,%s,%s,%s,%s,%s,clock_timestamp(),%s)",
                        (IDS["long"],str(uuid4()),CHANNEL,video_type,str(uuid4()),kind,"0"*32))
            except CheckViolation:
                accepted=False
            f.check(accepted==(video_type in allowed.get(kind,set())),"publication_receipt_kind_type_matrix")
    owned = claim(f, operation="metadata")["rows"]
    f.check(len(owned)==3, "publication_three_types_claimed")
    at = instant(f)
    future = at + timedelta(hours=2)
    rows = [waiting(item,at,scheduled=future) for item in owned]
    repo = VideoCollectionRepository(f.database)
    f.check(repo.complete_stream_lifecycle(rows)==3, "publication_three_types_settled")
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots")==0, "publication_no_snapshot")
    for row in source_rows(f):
        q=state(f,row["video_id"])
        f.check(row["publication_status"]=="upcoming" and row["scheduled_start_at"]==future
            and row["metadata_checked_at"]==at and row["checked_at"] is None and row["views"] is None,
            "publication_has_one_metadata_observation_not_counter_renewal")
        f.check(q["next_due_at"]==future and q["metadata_due_at"]==future
            and q["stats_due_at"] is None and q["hydration_needed"] and q["lease_token"] is None,
            "publication_future_date_shortens_existing_general")
    f.check(len(reader(f,_PAGE,(CHANNEL,None,None,51)))==3, "reader_waiting_page_all_types")
    f.check(all(r["views"] is None and r["like_count"] is None and r["comment_count"] is None
        for r in reader(f,"SELECT views,like_count,comment_count FROM readable.videos WHERE channel_id=%s",(CHANNEL,))),
        "reader_waiting_counters_masked")
    f.check(reader(f,"SELECT count(*) AS count FROM readable.hot_video_inventory WHERE channel_id=%s",(CHANNEL,))[0]["count"]==0,
        "reader_waiting_excluded_from_analytics_inventory")
    f.check(reader(f,"SELECT coalesce(sum(canonical_count),0) AS n FROM readable.published_inventory_counts WHERE channel_id=%s",(CHANNEL,))[0]["n"]==0,
        "ordinary_totals_denominator_excludes_waiting")
    f.check(reader(f,"SELECT sum(canonical_count) AS n FROM readable.canonical_inventory_counts WHERE channel_id=%s",(CHANNEL,))[0]["n"]==3,
        "raw_canonical_counts_retain_waiting")
    before=f.snapshot()
    f.check(repo.complete_stream_lifecycle(rows)==3 and f.snapshot()==before, "publication_exact_replay_no_mutation")
    try:
        repo.complete_stream_lifecycle([dict(rows[0],scheduled_start_at=(future+timedelta(days=1)).isoformat())])
    except DatabaseServerError as error:
        f.check(error.message=="stream_lifecycle_completion_receipt_mismatch", "publication_changed_replay_rejected")
    else:
        f.check(False,"publication_changed_replay_rejected")
    f.check(f.snapshot()==before, "publication_mismatched_receipt_rolls_back")
    # Simulate the next ordinary due instant only in this isolated fixture.
    f.rows("UPDATE public.video_collection_state SET metadata_due_at=now()-interval '1 second', "
        "next_due_at=now()-interval '1 second' WHERE channel_id=%s",(CHANNEL,))
    owned=claim(f,operation="metadata")["rows"]
    f.check(len(owned)==3 and all(c.collection_mode!="counters" for c in owned), "waiting_reclaimed_only_as_general")
    at=instant(f)
    rows=[waiting(c,at,scheduled=at-timedelta(days=1),status="awaiting_completion") for c in owned]
    f.check(repo.complete_stream_lifecycle(rows)==3, "past_date_still_waiting")
    for kind,video in IDS.items():
        q=state(f,video)
        f.check(q["next_due_at"]>at and q["next_due_at"]<=at+timedelta(days=28), "past_date_not_busy_loop")
        f.check(q["next_due_at"]==q["metadata_due_at"], "waiting_uses_one_deadline")
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots")==0, "past_date_does_not_prove_completion")
    # A subsequent full General is the only transition back to ordinary data.
    f.rows("UPDATE public.video_collection_state SET metadata_due_at=now()-interval '1 second', "
        "next_due_at=now()-interval '1 second' WHERE channel_id=%s",(CHANNEL,))
    owned=claim(f,operation="metadata")["rows"]
    at=instant(f)
    for c in owned:
        row=payload(c,at,title="Published",published_at=(at-timedelta(days=200)).isoformat(),
            duration="PT5M",views=30,like_count=2,comment_count=1,tags=[],youtube_topic_categories=[],
            availability_status="public",privacy_status="public",upload_status="processed",
            stream_actual_end_at=(at-timedelta(hours=1)).isoformat() if c.video_type=="stream" else None)
        done=repo.complete_metadata([row]) if c.video_type=="long" else repo.complete_typed_metadata([row])
        f.check(done==1,"ordinary_general_finishes_publication")
    f.check(all(r["publication_status"] is None and r["scheduled_start_at"] is None and r["views"]==30
        for r in source_rows(f)), "ordinary_general_clears_waiting_atomically")
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots")==3, "ordinary_general_writes_snapshots")
    f.check(not reader(f,_PAGE,(CHANNEL,None,None,51)), "completed_absent_from_waiting_list")
    # Native fixture has Short/Stream proof; Long is deliberately not fabricated.
    f.check(reader(f,"SELECT count(*) AS count FROM readable.hot_video_inventory WHERE channel_id=%s",(CHANNEL,))[0]["count"]==2,
        "published_typed_rows_return_to_analytics")


def expiry_and_unknown(f):
    seed(f,operation="metadata")
    clear_stream_end(f)
    # Lost membership is identity uncertainty, never an implicit Long conversion.
    video=IDS["long"]
    f.rows("UPDATE public.video_inventory SET video_type=NULL,skeleton_since=now(),published_at=NULL,published_acquired_at=NULL WHERE video_id=%s",(video,))
    f.rows("UPDATE public.video_collection_state SET video_type=NULL WHERE video_id=%s",(video,))
    repo=VideoCollectionRepository(f.database)
    owned=claim(f,operation="metadata",kinds=("unknown",))["rows"]
    f.check(len(owned)==1 and owned[0].video_type=="unknown", "unknown_identity_claimed_without_type_guess")
    at=instant(f)
    f.check(repo.complete_stream_lifecycle([waiting(owned[0],at,scheduled=at+timedelta(days=60))])==1,
        "unknown_publication_settles_metadata")
    q=state(f,video)
    f.check(q["video_type"] is None and q["next_due_at"]==at+timedelta(days=28), "unknown_uses_existing_identity_schedule")
    page=reader(f,_PAGE,(CHANNEL,None,None,51))
    f.check(len(page)==1 and page[0]["video_type"] is None, "reader_unknown_stays_unknown")
    try:
        reader(f,"SELECT publication_status FROM public.videos LIMIT 1")
    except Exception as error:
        f.check(getattr(error,"sqlstate",None)=="42501", "reader_cannot_bypass_windows")
    else:
        f.check(False,"reader_cannot_bypass_windows")
    # Old acquisition clock hides both API title and future-valued date. Future
    # scheduled time never grants a fresh acquisition clock by itself.
    f.rows("UPDATE public.videos SET metadata_checked_at=now()-interval '697 hours' WHERE video_id=%s",(video,))
    masked=reader(f,"SELECT title,publication_status,scheduled_start_at FROM readable.videos WHERE video_id=%s",(video,))[0]
    f.check(all(v is None for v in masked.values()) and not reader(f,_PAGE,(CHANNEL,None,None,51)),
        "expired_metadata_date_masked_together")
    from db.hot.update_copy_sources import UPDATE_COPY_SOURCE_ROWS as SOURCES
    f.check("publication_status IS NOT NULL" in SOURCES["video_metadata"]
        and "scheduled_start_at IS NOT NULL" in SOURCES["video_metadata"], "copy_guard_covers_publication_source")
    result=f.cleanup("video_metadata",limit=10)
    f.check(result.scrubbed==1 and f.scalar("SELECT publication_status IS NULL AND scheduled_start_at IS NULL "
        "AND title IS NULL FROM public.videos WHERE video_id=%s",(video,)), "cleanup_scrubs_waiting_payload_retains_id")


def initial_handoff(f):
    # Reuse the exact bounded initial fixture from its existing native harness;
    # this adapter only exposes the same current repository and admin interface.
    import check_initial_regular_handoff as initial
    class Admin:
        def execute(self,*args,**kwargs): return f.admin.execute(*args,**kwargs)
        def scalar(self,*args,**kwargs): return f.scalar(*args,**kwargs)
        def transaction(self): return f.admin.transaction()
    class Probe:
        admin=Admin()
        database=f.database
        now=f.now
        def call(self,method,*args,**kwargs): return method(*args,**kwargs)
        def observe(self): return f.observe()
        def check(self,label,condition): return f.check(condition,label)
    probe=Probe()
    ids,jobs,videos,claims=initial.seed(probe,3)
    at=instant(f)
    rows=[waiting(c,at,scheduled=at+timedelta(hours=1)) for c in claims]
    f.check(videos.complete_initial_batch([dict(operation="complete_stream_lifecycle",rows=rows)])==3,
        "initial_waiting_atomic_batch")
    job=initial.settle(probe,jobs)
    targets=f.rows("SELECT status,result_code FROM public.operator_job_targets WHERE job_id=%s",(initial.J,))
    f.check(len(targets)==3 and all(t==dict(status="checked",result_code="publication_awaiting_completion") for t in targets),
        "initial_waiting_targets_checked_all_types")
    f.check(job["phase"]=="verify" and job["status"]=="queued", "waiting_does_not_block_initial_verification")
    job=jobs.claim_initial_hydration(initial.C,"publication-proof")
    verified=jobs.verify_initial_hydration(initial.J,job["lease_token"])
    f.check(verified["status"]=="completed", "initial_waiting_job_completed")
    f.rows("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second', "
        "metadata_due_at=now()-interval '1 second' WHERE channel_id=%s",(initial.C,))
    owned=videos.claim_due_batch(limit=50,worker_id="publication-proof",run_id=str(uuid4()),operation="metadata",
        scopes=[dict(channel_id=initial.C,video_type=kind) for kind in KINDS])["rows"]
    f.check({c.video_id for c in owned}==set(ids), "waiting_handed_to_regular_all_types")


def canceled_and_returned(f):
    """An omitted publication follows the ordinary availability protocol."""
    seed(f,operation="metadata")
    clear_stream_end(f)
    repo=VideoCollectionRepository(f.database)
    owned=claim(f,operation="metadata")["rows"]
    at=instant(f)
    f.check(repo.complete_stream_lifecycle([waiting(c,at,scheduled=at+timedelta(days=2)) for c in owned])==3,
        "cancellation_fixture_waiting_all_types")
    # Advance only fixture due dates; provider clocks are actual observations.
    def due():
        f.rows("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second', "
            "metadata_due_at=now()-interval '1 second' WHERE channel_id=%s",(CHANNEL,))
        result=claim(f,operation="metadata")["rows"]
        f.check(len(result)==3,"cancellation_all_types_claimed")
        return result
    owned=due()
    missing_at=instant(f)
    f.check(repo.complete_missing([missing_payload(c,missing_at) for c in owned])==3,
        "waiting_omission_uses_existing_unavailability_owner")
    for video in IDS.values():
        q=state(f,video)
        f.check(q["availability_first_missing_at"]==missing_at
            and q["next_due_at"]==missing_at+timedelta(days=1) and q["stats_due_at"] is None,
            "canceled_publication_uses_normal_daily_retry")
    f.check(not reader(f,_PAGE,(CHANNEL,None,None,51)),"unavailable_publication_not_shown_as_waiting")
    owned=due()
    returned_at=instant(f)
    f.check(repo.complete_stream_lifecycle([waiting(c,returned_at,status="live") for c in owned])==3,
        "positive_waiting_general_restores_available_publication")
    for video in IDS.values():
        q=state(f,video)
        f.check(q["availability_first_missing_at"] is None and q["availability_schedule_type"] is None,
            "waiting_return_closes_episode")
    f.check(len(reader(f,_PAGE,(CHANNEL,None,None,51)))==3,"returned_publication_visible_again")
    f.check(f.scalar("SELECT count(*) FROM public.video_availability_incidents WHERE restored_at IS NOT NULL")==3,
        "waiting_return_restores_incident_once")
    # A subsequent positive metadata-only ordinary result must not resurrect
    # any counters from before the waiting interval.
    owned=due()
    ordinary_at=instant(f)
    rows=[payload(c,ordinary_at,missing=False,counterless_profile=True,
        title="Synthetic ordinary publication",published_at=(ordinary_at-timedelta(days=200)).isoformat(),
        duration="PT5M",tags=[],youtube_topic_categories=[],availability_status="public",
        privacy_status="public",upload_status="processed",
        stream_actual_end_at=(ordinary_at-timedelta(hours=1)).isoformat() if c.video_type=="stream" else None) for c in owned]
    f.check(repo.complete_identity_refresh(rows)==3,"counterless_ordinary_general_ends_waiting")
    f.check(all(r["publication_status"] is None and r["scheduled_start_at"] is None
        and r["checked_at"] is None and r["views"] is None for r in source_rows(f)),
        "ordinary_counterless_return_never_reveals_pre_waiting_counters")
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots")==0,
        "cancellation_and_return_do_not_invent_counter_history")


CASES=(waiting_all_types,expiry_and_unknown,initial_handoff,canceled_and_returned)

def safe_failure(error):
    # Constraint identifiers are bounded catalog names; never emit DETAIL,
    # message_primary, offending row, SQL text or connection information.
    constraint=getattr(getattr(error,"diag",None),"constraint_name",None)
    constraint=constraint if isinstance(constraint,str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,127}",constraint) else None
    return dict(error_class=type(error).__name__,sqlstate=getattr(error,"sqlstate",None),constraint=constraint,
        code=str(error) if isinstance(error,(AssertionError,RuntimeError)) else getattr(error,"code",None),
        trace=[dict(file=Path(frame.filename).name,line=frame.lineno)
               for frame in traceback.extract_tb(error.__traceback__)[-6:]])


def run():
    if os.getenv("LIC_SCHEMA_CI_MAX_ROWS") not in ("1000","15000"):
        raise RuntimeError("publication_explicit_fixture_budget_required")
    with admission() as gate:
        f=Fixture(*dsns(),gate)
        failures=[]
        try:
            for case in CASES:
                before=f.checks
                failure=None
                try:
                    case(f)
                    f.observe()
                except Exception as error:
                    failure=safe_failure(error)
                    failures.append(case.__name__)
                finally:
                    # Only a verified reset permits the next independent case.
                    f.reset()
                print(json.dumps(dict(stage="publication:"+case.__name__,
                    status="fail" if failure else "pass",checks=f.checks-before,
                    **(failure or {}))),flush=True)
        finally:
            f.close()
        if failures:
            raise AssertionError("publication_failed_cases:"+",".join(failures))
    print(json.dumps(dict(stage="publication_lifecycle",status="pass",cases=len(CASES),youtube_requests=0,max_rows=1000)))

if __name__=="__main__":
    try:
        run()
    except Exception as error:
        # Synthetic assertion codes/locations only, never raw provider/SQL data.
        print(json.dumps(dict(stage="publication_lifecycle",status="fail",**safe_failure(error))))
        raise SystemExit(1)
