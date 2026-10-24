"""Current policy ownership against native arithmetic and fixed historical vectors."""
from datetime import datetime,timedelta,timezone
from collection_policy import policy_value,long_due_at,typed_due_at,metadata_due_at,select_policy,PolicyError
from db.videos.collection_policy import load_cadence_snapshot
from .fixtures import expect_error,CHANNEL

_REFERENCE = """SELECT cadence_seconds,policy_version,0 AS priority,jitter_window_seconds
FROM public.video_collection_cadence
WHERE video_type='long' AND collection_kind=%s AND availability_class=%s AND enabled
AND min_age_seconds<=greatest(extract(epoch FROM (%s::timestamptz-%s::timestamptz))::bigint,0)
AND (max_age_seconds IS NULL OR max_age_seconds>greatest(extract(epoch FROM (%s::timestamptz-%s::timestamptz))::bigint,0))
ORDER BY policy_version DESC,min_age_seconds DESC LIMIT 1"""

def cadence_policy(f):
    snapshot=load_cadence_snapshot(f.database)
    f.check(len(snapshot.rules)==30,'cadence_count')
    obs=datetime(2025,6,15,12,tzinfo=timezone.utc);checked=obs-timedelta(hours=1);n=0
    for rule in snapshot.rules:
        if rule.video_type!='long':continue
        for delta in (-1,-.51,-.5,-.49,0,.49,.5,.51,1):
            published=obs-timedelta(seconds=rule.min_age_seconds+delta)
            expected=f.rows(_REFERENCE,(rule.collection_kind,rule.availability_class,obs,published,obs,published))[0]
            selected=select_policy(snapshot,'long',rule.collection_kind,rule.availability_class,published,checked,obs)
            for key in ('cadence_seconds','priority','policy_version'):
                f.check(policy_value(snapshot,rule.collection_kind,rule.availability_class,published,obs,key)==expected[key],'long_native_fractional_boundary')
            f.check(selected.jitter_window_seconds==0 and selected.cadence_seconds==expected['cadence_seconds'],'long_selected_row')
            f.check(long_due_at(snapshot,rule.collection_kind,rule.availability_class,published,checked,obs,db_now=f.now)==checked+timedelta(seconds=expected['cadence_seconds']),'long_due')
            n+=1
    f.check(n==108,'twelve_by_nine')
    for status,wanted in ((None,3600),('private',None),('unknown_unavailable',None)):
        f.check(policy_value(snapshot,'counters',status,obs,obs,'cadence_seconds')==wanted,'availability_class')
    for published,stamp,status,wanted in ((obs,checked,'private',None),(None,checked,'public',obs),('infinity',checked,'public',obs),(obs,None,'public',obs),(obs,'infinity','public',obs),(obs,f.now+timedelta(hours=1),'public',obs)):
        f.check(long_due_at(snapshot,'metadata',status,published,stamp,obs,db_now=f.now)==wanted,'long_invalid_clock')
    f.check(policy_value(snapshot,'counters','public',None,obs,'cadence_seconds')==3600,'null_youngest')
    for kind in ('short','stream'):
        for age in (0,7775999,7776000,7776001,7819200,7862400,16416000):
            anchor=obs-timedelta(seconds=age);cadence=604800 if age<=7776000 else 2419200
            rule=select_policy(snapshot,kind,'metadata','all',anchor,obs,obs+timedelta(days=365))
            f.check((rule.cadence_seconds,rule.jitter_window_seconds,rule.policy_version)==(cadence,21600,1),'typed_age_at_observation')
            for identity in ('f4b00000001','f4b00000002','f4b00000003'):
                jitter=f.scalar("SELECT (('x'||substr(md5(%s),1,8))::bit(32)::bigint)%%21601",(identity,))
                value=typed_due_at(snapshot,kind,identity,anchor,obs,db_now=f.now)
                f.check(value==obs+timedelta(seconds=cadence-21600+jitter) and obs+timedelta(seconds=cadence-21600)<=value<=obs+timedelta(seconds=cadence),'typed_native_jitter')
        for anchor,cadence in ((None,604800),(obs+timedelta(days=1),604800),('infinity',604800),('-infinity',2419200)):
            jitter=f.scalar("SELECT (('x'||substr(md5('f4b00000001'),1,8))::bit(32)::bigint)%21601")
            f.check(typed_due_at(snapshot,kind,'f4b00000001',anchor,obs,db_now=f.now)==obs+timedelta(seconds=cadence-21600+jitter),'typed_anchor_edges')
    f.check(typed_due_at(snapshot,'stream','f4b00000001',obs-timedelta(days=1),obs,db_now=f.now)==typed_due_at(snapshot,'short','f4b00000001',obs-timedelta(days=1),obs,db_now=f.now)!=typed_due_at(snapshot,'stream','f4b00000001',obs-timedelta(days=365),obs,db_now=f.now),'stream_end_anchor')
    expect_error(f,lambda:typed_due_at(snapshot,'short','f4b00000001',obs,None,db_now=f.now),PolicyError,code='typed_video_collection_due_input_invalid')
    for stamp in (f.now+timedelta(hours=1),'infinity'):
        f.check(typed_due_at(snapshot,'short','f4b00000001',obs,stamp,db_now=f.now)==f.now,'typed_bad_source_renews_now')
    f.check(metadata_due_at(obs+timedelta(days=30),obs,obs,db_now=f.now)==obs+timedelta(hours=672) and metadata_due_at(obs+timedelta(days=30),None,obs,db_now=f.now)==obs,'hot_ceiling')
    # Production sessions are UTC. Native calendar subtraction supplies the
    # original Europe/Kiev boundary instants before Python policy is called.
    with f.admin.transaction():
        f.rows("SET LOCAL timezone='Europe/Kiev'")
        points=f.rows("SELECT '2025-06-15 12:00+03'::timestamptz AS checked,'2025-06-15 12:00+03'::timestamptz-interval '90 days'+d*interval '1 second' AS anchor FROM unnest(ARRAY[-1,0,1]) d")
        # Preserve the Kyiv-generated instants, then use the production UTC
        # transaction semantics for the native expected timestamp comparison.
        f.rows("SET LOCAL timezone='UTC'")
        for row in points:
            cadence=f.scalar("SELECT CASE WHEN %s::timestamptz >= %s::timestamptz-interval '90 days' THEN 604800 ELSE 2419200 END",(row['anchor'],row['checked']))
            jitter=f.scalar("SELECT (('x'||substr(md5('f4b00000001'),1,8))::bit(32)::bigint)%21601")
            f.check(typed_due_at(snapshot,'short','f4b00000001',row['anchor'],row['checked'],db_now=f.now)==row['checked']+timedelta(seconds=cadence-21600+jitter),'native_dst_boundary')
    # Schema 29 reconciliation first initializes missing queues. Its later
    # schedule-repair count excludes that already-valid row; durable state, not
    # an update count, proves that this public owner created the ready queue.
    from db.videos.video_typed_reconciliation import reconcile_typed_video_collection_state
    _seed_typed_schedule(f)
    f.check(f.scalar("SELECT count(*) FROM public.video_collection_state WHERE video_id='f4b00000001'")==0,'actual_queue_absent')
    result=reconcile_typed_video_collection_state(f.database,'short',2)
    f.check(result['inventory_examined_count']==1 and result['affected_count']==0,'actual_schedule_owner')
    f.check(f.scalar("SELECT count(*) FROM public.video_collection_state WHERE video_id='f4b00000001' AND video_type='short' AND next_due_at IS NOT NULL AND hydration_needed AND paused_reason IS NULL")==1,'actual_schedule_row')
    state=f.rows("SELECT * FROM public.video_collection_state WHERE video_id='f4b00000001'")
    repeated=reconcile_typed_video_collection_state(f.database,'short',2)
    f.check(repeated['inventory_examined_count']==1 and repeated['affected_count']==0,'actual_schedule_no_rewrite')
    f.check(f.rows("SELECT * FROM public.video_collection_state WHERE video_id='f4b00000001'")==state,'actual_schedule_idempotent')


def _seed_typed_schedule(f):
    """Reachable typed identity: accepted owner, completed scan and present member."""
    f.channel()
    scan='10000000-0000-4000-8000-000000000020'
    playlist='UUSH'+CHANNEL[2:]
    f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at) VALUES('f4b00000001',%s,'short',now()-interval '1 day',now())",(CHANNEL,))
    f.rows("INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,completed_at,source_checked_at,pages_completed,items_seen) VALUES(%s,%s,'short',%s,'full_reconciliation','completed','complete',now(),now(),1,1)",(scan,CHANNEL,playlist))
    f.rows("INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at,inventory_status,last_completed_scan_id,inventory_completed_at) VALUES(%s,'short',%s,true,'ready',now(),'completed',%s,now()) ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,status='ready',next_due_at=now(),inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id,inventory_completed_at=now()",(CHANNEL,playlist,scan))
    f.rows("INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_scan_id,last_seen_scan_id,membership_status,first_seen_at,last_seen_at,last_present_at) VALUES('f4b00000001',%s,'short',%s,now()-interval '1 day',%s,%s,'present',now(),now(),now())",(CHANNEL,playlist,scan,scan))
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type='short'")
