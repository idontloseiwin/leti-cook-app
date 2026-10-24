"""Focused PG17 proof: exact Long/Short conflicts, real restricted owners, no YouTube."""
from pathlib import Path
import json,os,sys,time
from datetime import timedelta
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from native_unified_availability import seed,CHANNEL,IDS,require,error_details,claim
from native_pipeline_trace import collector,profile
from schema25_regressions.fixtures import Fixture,admission,dsns
from db.inventory.automatic_type_reconciliation import reconcile_automatic_video_types
from db.base.postgres import Operation
from db.reviews.video_review_evidence import refresh_review_case
from db.reviews.video_type_review_queries import has_open_video_type_reviews


def member(f,video,kind):
    acquired=f.now-timedelta(minutes=2)
    scan=str(uuid4());playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+CHANNEL[2:]
    f.rows("""INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,
      scan_kind,status,coverage,scan_boundary_at,started_at,completed_at,source_checked_at,pages_completed,items_seen)
      VALUES(%s,%s,%s,%s,'full_reconciliation','completed','complete',%s,%s,%s,%s,1,1)""",
      (scan,CHANNEL,kind,playlist,acquired,acquired,acquired,acquired))
    f.rows("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
      source_published_at,first_seen_scan_id,last_seen_scan_id,membership_status,first_seen_at,last_seen_at,last_present_at)
      VALUES(%s,%s,%s,%s,%s,%s,%s,'present',%s,%s,%s) ON CONFLICT(video_id,channel_id,video_type)
      DO UPDATE SET membership_status='present',last_seen_at=excluded.last_seen_at,last_present_at=excluded.last_present_at""",
      (video,CHANNEL,kind,playlist,f.now-timedelta(days=200),scan,scan,acquired,acquired,acquired))
    f.rows("""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,
      inventory_status,last_completed_scan_id) VALUES(%s,%s,%s,true,'completed',%s)
      ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id""",
      (CHANNEL,kind,playlist,scan))


def overlap(f,*,video=None,duration='PT3M'):
    seed(f,operation='metadata');video=video or IDS['long']
    member(f,video,'long');member(f,video,'short')
    f.rows('UPDATE public.videos SET duration=%s WHERE video_id=%s',(duration,video))
    with f.database.transaction(Operation('automatic_type_native_seed',statement_timeout_ms=8000)) as tx:
        refresh_review_case(tx,video)
    return video


def apply(f,video):
    with f.database.transaction(Operation('automatic_type_native',statement_timeout_ms=8000)) as tx:
        return reconcile_automatic_video_types(tx,[video])


def choice(f,video):
    return f.rows('SELECT automatic_type FROM public.video_type_automatic_candidates WHERE video_id=%s',(video,))


def duration_boundaries(f):
    video=overlap(f)
    for duration,expected in [('PT2M59S','short'),('PT3M','short'),('PT3M1S','long'),('PT1H','long'),
       ('P1DT1S','long'),('180','short'),('180.1','long'),('PT0S',None),('P',None),('PT3MT',None),
       ('PT-1S',None),('',None),(None,None)]:
        f.rows('UPDATE public.videos SET duration=%s WHERE video_id=%s',(duration,video))
        result=choice(f,video)
        f.check(result==([] if expected is None else [dict(automatic_type=expected)]),'duration_'+str(duration))


def atomic_canonical_and_queue(f):
    video=overlap(f)
    before=f.rows('SELECT next_due_at,stats_due_at,metadata_due_at FROM public.video_collection_state WHERE video_id=%s',(video,))[0]
    f.check(apply(f,video)==[video],'automatic_applied')
    state=f.rows('SELECT video_type,next_due_at,stats_due_at,metadata_due_at FROM public.video_collection_state WHERE video_id=%s',(video,))[0]
    f.check(state==dict(video_type='short',**before),'same_queue_keeps_deadlines')
    proof=f.database.read_row('automatic_native_read','SELECT automatic_valid,resolution_status,canonical_video_type FROM readable.video_inventory_type_resolution WHERE video_id=%s',(video,))
    f.check(proof==dict(automatic_valid=True,resolution_status='resolved',canonical_video_type='short'),'protected_read_agrees')
    f.check(not has_open_video_type_reviews(f.database),'no_false_open_review')
    f.check(f.scalar('SELECT count(*) FROM public.video_type_review_history')==0,'not_user_history')
    f.check(f.scalar('SELECT decision_type FROM public.video_type_review_cases WHERE video_id=%s',(video,)) is None,'not_manual_decision')
    f.check(apply(f,video)==[],'idempotent_no_second_queue_write')
    f.rows('UPDATE public.videos SET duration=NULL WHERE video_id=%s',(video,))
    f.check(not choice(f,video) and not has_open_video_type_reviews(f.database),'withdrawn_proof_waits_without_manual_alarm')


def lease_then_general(f):
    video=overlap(f,duration=None)
    rows=claim(f,kinds=('long',),operation='metadata')['rows']
    f.check(len(rows)==1 and rows[0].collection_mode=='identity_refresh','conflict_uses_general_not_stats')
    f.rows("UPDATE public.videos SET duration='PT3M' WHERE video_id=%s",(video,))
    f.check(apply(f,video)==[],'live_lease_not_stolen')
    old=f.scalar('SELECT lease_token::text FROM public.video_collection_state WHERE video_id=%s',(video,))
    f.check(old==str(rows[0].lease_token),'lease_identity_preserved')
    def general(ids,reason):
        values=[profile(identity,f.now-timedelta(days=200)) for identity in ids]
        for value in values:value['contentDetails']['duration']='PT3M'
        return values
    def stats(*args):raise AssertionError('unexpected_stats')
    from db.videos.video_collection import VideoCollectionRepository
    result=collector(VideoCollectionRepository(f.database),general,stats).process_common_claims(rows)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'general_committed')
    f.check(f.scalar('SELECT video_type FROM public.video_inventory WHERE video_id=%s',(video,))=='short','completion_normalized_canonical')
    f.check(f.scalar('SELECT lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',(video,)),'completion_released_own_lease')


def stream_excluded(f):
    video=overlap(f)
    member(f,video,'stream')
    f.check(choice(f,video)==[] and apply(f,video)==[],'stream_membership_not_auto')


def manual_and_stale_evidence(f):
    video=overlap(f)
    f.check(choice(f,video)==[dict(automatic_type='short')],'fresh_overlap_candidate')
    old=f.now-timedelta(days=30)
    metadata=f.scalar('SELECT metadata_checked_at FROM public.videos WHERE video_id=%s',(video,))
    f.rows('UPDATE public.videos SET metadata_checked_at=%s WHERE video_id=%s',(old,video))
    before=f.snapshot()
    f.check(choice(f,video)==[] and apply(f,video)==[],'expired_metadata_not_auto')
    f.check(f.snapshot()==before,'expired_metadata_no_write')
    f.rows('UPDATE public.videos SET metadata_checked_at=%s WHERE video_id=%s',(metadata,video))
    membership=f.rows("SELECT first_seen_at,last_seen_at,last_present_at FROM public.video_inventory_memberships WHERE video_id=%s AND video_type='short'",(video,))[0]
    f.rows("UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s WHERE video_id=%s AND video_type='short'",(old,old,old,video))
    before=f.snapshot()
    f.check(choice(f,video)==[] and apply(f,video)==[],'expired_raw_membership_not_auto')
    f.check(f.snapshot()==before,'expired_raw_membership_no_write')
    f.rows("UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s WHERE video_id=%s AND video_type='short'",
      (membership['first_seen_at'],membership['last_seen_at'],membership['last_present_at'],video))
    f.check(choice(f,video)==[dict(automatic_type='short')],'restored_fixture_candidate')
    # Existing user work still wins; a new decision cannot override an automatic
    # Long/Short check. Seed the already-recorded choice as fixture history.
    f.rows("UPDATE public.video_type_review_cases SET decision_type='long',decision_reason='Synthetic existing manual priority',decision_at=now(),revision=revision+1,decision_revision=revision+1,state='resolved' WHERE video_id=%s",(video,))
    before=f.snapshot()
    f.check(choice(f,video)==[] and apply(f,video)==[],'manual_choice_has_priority')
    f.check(f.snapshot()==before,'manual_queue_and_history_unchanged')
    proof=f.database.read_row('automatic_manual_priority','SELECT canonical_video_type,manual_valid,automatic_valid FROM readable.video_inventory_type_resolution WHERE video_id=%s',(video,))
    f.check(proof==dict(canonical_video_type='long',manual_valid=True,automatic_valid=False),'read_manual_priority')


def ordinary_long_keeps_schedule(f):
    acquired,published=seed(f,operation='counters');video=IDS['long']
    from db.videos.video_collection import VideoCollectionRepository
    repository=VideoCollectionRepository(f.database)
    calls=[]
    def general(ids,reason):
        calls.append(('general',tuple(ids)))
        return [profile(identity,published) for identity in ids]
    def counters(ids,reason):
        calls.append(('stats',tuple(ids)))
        return dict(items=[dict(id=identity,views=23,like_count=2,comment_count=1) for identity in ids],summary=dict(failed_ids=[]))
    service=collector(repository,general,counters)
    f.check(f.scalar("SELECT count(*) FROM public.video_inventory_memberships WHERE video_id=%s AND membership_status='present'",(video,))==0,'ordinary_long_has_no_membership')
    metadata_due=f.scalar('SELECT metadata_due_at FROM public.video_collection_state WHERE video_id=%s',(video,))
    owned=claim(f,kinds=('long',),operation='counters')['rows']
    f.check(len(owned)==1 and owned[0].collection_mode=='counters','ordinary_long_keeps_stats_claim')
    result=service.process_common_claims(owned)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'ordinary_long_stats_completed')
    after=f.rows('SELECT stats_due_at,metadata_due_at,metadata_source_checked_at FROM public.video_collection_state WHERE video_id=%s',(video,))[0]
    f.check(after['stats_due_at']>f.now and after['metadata_due_at']==metadata_due and after['metadata_source_checked_at']==acquired,'ordinary_long_stats_preserves_metadata')
    due=f.now-timedelta(seconds=1)
    f.rows('UPDATE public.video_collection_state SET metadata_due_at=%s,next_due_at=%s WHERE video_id=%s',(due,due,video))
    owned=claim(f,kinds=('long',),operation='metadata')['rows']
    f.check(len(owned)==1 and owned[0].collection_mode=='metadata','ordinary_long_keeps_general_claim')
    result=service.process_common_claims(owned)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'ordinary_long_general_completed')
    final=f.rows('SELECT video_type,hydration_needed,paused_reason,stats_due_at,metadata_due_at FROM public.video_collection_state WHERE video_id=%s',(video,))[0]
    f.check(final['video_type']=='long' and final['hydration_needed'] is False and final['paused_reason'] is None,'ordinary_long_remains_hydrated')
    f.check(final['stats_due_at']>=after['stats_due_at'] and final['metadata_due_at']>f.now,'ordinary_long_future_schedule_preserved')
    f.check(calls==[('stats',(video,)),('general',(video,))],'ordinary_long_no_identity_refresh')


def http_reader_inventory_automatic_proof(f):
    """Real HTTP role, nested proof views and unchanged raw-access denial."""
    from db.base.postgres import DatabaseServerError
    from db.base.read_context import http_method_scope, http_read_scope
    from db.reads.frontend import FrontendRepository

    video = overlap(f)
    f.check(apply(f, video) == [video], 'reader_fixture_auto_short')
    relation = f.rows("SELECT pg_get_userbyid(relowner) AS owner,reloptions FROM pg_class "
                      "WHERE oid='public.video_type_automatic_candidates'::regclass")[0]
    f.check(relation['owner'] == 'postgres'
            and set(relation['reloptions']) == {'security_invoker=false', 'security_barrier=true'},
            'candidate_fixed_owner_and_barrier')
    private_relations = ('video_type_automatic_candidates', 'video_inventory', 'videos',
                         'video_inventory_memberships', 'video_type_review_cases')
    for name in private_relations:
        f.check(not f.scalar("SELECT has_table_privilege('lic_reader',%s,'SELECT')", ('public.' + name,)),
                'no_reader_grant_' + name)
    f.check(f.database.read_row('reader_regression_writer_identity', 'SELECT current_user AS role')
            == {'role': 'service_role'}, 'ordinary_background_owner_unchanged')
    f.check(f.database.read_row('reader_regression_writer_proof',
            'SELECT automatic_type FROM public.video_type_automatic_candidates WHERE video_id=%s', (video,))
            == {'automatic_type': 'short'}, 'writer_can_still_read_candidate')

    def counts_and_proofs():
        with http_method_scope('GET'), http_read_scope():
            f.check(f.database.read_row('reader_regression_identity',
                    "SELECT session_user AS login,current_user AS role,current_setting('transaction_read_only') AS read_only")
                    == {'login': 'authenticator', 'role': 'lic_reader', 'read_only': 'on'}, 'actual_http_read_role')
            f.check(any(row['channel_id'] == CHANNEL
                    for row in FrontendRepository(f.database).load_visible_channel_user_work()),
                    'reader_local_channel_lookup')
            counts = FrontendRepository(f.database).count_inventory_by_type([CHANNEL], ('long', 'short', 'stream'))
            resolution = f.database.read_row('reader_regression_resolution',
                    'SELECT automatic_valid,resolution_status,canonical_video_type FROM readable.video_inventory_type_resolution WHERE video_id=%s', (video,))
            review = f.database.read_rows('reader_regression_review',
                    'SELECT video_id FROM readable.video_type_review_cases WHERE video_id=%s', (video,), max_rows=1)
            return counts, resolution, review

    before = f.snapshot()
    counts, resolution, review = counts_and_proofs()
    f.check(counts == {'long': 0, 'short': 2, 'stream': 1}, 'reader_actual_home_count')
    f.check(resolution == {'automatic_valid': True, 'resolution_status': 'resolved', 'canonical_video_type': 'short'},
            'reader_automatic_resolution')
    f.check(review == [], 'reader_resolved_conflict_not_manual_review')
    with http_method_scope('GET'), http_read_scope():
        for name in private_relations:
            try:
                f.database.read_rows('reader_regression_raw_denied',
                    'SELECT video_id FROM public.' + name + ' LIMIT 1', max_rows=1)
            except DatabaseServerError as error:
                f.check(error.code == '42501', 'reader_raw_still_denied_' + name)
            else:
                raise AssertionError('reader_raw_leaked_' + name)
    f.check(f.snapshot() == before, 'http_reads_and_denials_do_not_write')
    f.rows('UPDATE public.videos SET metadata_checked_at=%s WHERE video_id=%s',
           (f.now - timedelta(days=30), video))
    counts, resolution, review = counts_and_proofs()
    f.check(counts == {'long': 0, 'short': 1, 'stream': 1}, 'reader_expired_auto_proof_not_counted')
    f.check(resolution['automatic_valid'] is False and resolution['resolution_status'] == 'conflict',
            'reader_expired_proof_is_unresolved')
    f.check(review == [{'video_id': video}], 'raw_case_history_survives_expiry')
    from db.reviews.video_type_review_queries import list_video_type_review_cases
    with http_method_scope('GET'), http_read_scope():
        waiting=list_video_type_review_cases(f.database,'awaiting',100)
        f.check(any(row['video_id']==video and row['review_status']=='awaiting_refresh'
                    and row['can_decide'] is False for row in waiting),'expired_overlap_is_waiting_not_manual')
        f.check(not has_open_video_type_reviews(f.database),'expired_overlap_has_no_manual_alarm')



def current_review_presentation(f):
    """Real readers distinguish automatic waiting, current exceptions and history."""
    from db.reviews.video_type_review_queries import list_video_type_review_cases
    from db.base.read_context import http_method_scope, http_read_scope
    video=overlap(f,duration=None)
    def rows(state):
        with http_method_scope('GET'), http_read_scope():
            return [row for row in list_video_type_review_cases(f.database,state,100) if row['video_id']==video]
    waiting=rows('awaiting')
    f.check(len(waiting)==1 and waiting[0]['review_status']=='awaiting_refresh'
            and waiting[0]['can_decide'] is False,'missing_duration_waits_automatically')
    f.check(rows('open')==[] and not has_open_video_type_reviews(f.database),'waiting_has_no_manual_alarm')
    member(f,video,'stream')
    with f.database.transaction(Operation('review_presentation_refresh',statement_timeout_ms=8000)) as tx:
        refresh_review_case(tx,video)
    active=rows('open')
    f.check(len(active)==1 and active[0]['review_status']=='needs_review'
            and active[0]['can_decide'] is True,'fresh_stream_overlap_still_needs_review')
    f.check(has_open_video_type_reviews(f.database),'current_manual_summary_agrees')
    old=f.now-timedelta(days=30)
    f.rows('UPDATE public.video_inventory_memberships SET last_seen_at=%s,last_present_at=%s,first_seen_at=%s WHERE video_id=%s',
            (old,old,old,video))
    f.check(rows('open')==[] and rows('awaiting')[0]['review_status']=='awaiting_refresh',
            'expired_stream_memberships_are_waiting_not_manual')
    f.check(not has_open_video_type_reviews(f.database),'stale_stream_has_no_manual_alarm')
    # A historical case survives after one fresh authoritative list remains.
    member(f,video,'long')
    current=rows('all')
    f.check(len(current)==1 and current[0]['review_status']=='confirmed'
            and current[0]['can_decide'] is False,'resolved_singleton_is_history_not_manual')
    f.check(rows('open')==[],'historical_open_does_not_make_a_new_conflict')


def main():
    require(os.getenv('LIC_SCHEMA_CI_MAX_ROWS')=='1000','explicit_row_budget')
    report=dict(status='failed',cases=[],youtube_requests=0,fixture_restored=False)
    started=time.monotonic()
    from native_initial_type_conflict import CASES as initial_cases
    cases=(duration_boundaries,atomic_canonical_and_queue,lease_then_general,stream_excluded,manual_and_stale_evidence,ordinary_long_keeps_schedule,http_reader_inventory_automatic_proof,current_review_presentation)+tuple(initial_cases)
    try:
        with admission() as gate:
            f=Fixture(*dsns(),gate)
            try:
                for case in cases:
                    checks=f.checks;item=dict(name=case.__name__,status='failed')
                    try:case(f);item.update(status='passed',rows=f.observe())
                    except Exception as error:item.update(error_details(error))
                    item['checks']=f.checks-checks;report['cases'].append(item);f.reset()
            finally:f.close();report['fixture_restored']=True
        report['status']='passed' if all(x['status']=='passed' for x in report['cases']) else 'failed'
    except Exception as error:report.update(error_details(error))
    report['seconds']=round(time.monotonic()-started,3)
    Path('/out/RESULT.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps(report));return 0 if report['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
