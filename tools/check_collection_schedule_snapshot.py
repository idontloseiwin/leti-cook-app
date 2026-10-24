#!/usr/bin/env python3
"""Finite PG17 schedule proof on the explicitly owned empty schema25 fixture.

Preparation only: no process/container/provider launch, retired RPC, DDL or
implicit execution. One admin + one authenticator connection, <=1000 total rows.
Requires the composed F11 source and external 240s process/container deadline.
"""
from pathlib import Path
import argparse
import ast
from dataclasses import replace
from datetime import timedelta
import json
import os
import re
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection_schedule import CollectionSchedules,TypedMetadata
from db.base.postgres import PostgresDatabase,DatabaseError,Operation
from db.readiness import capabilities
from db.videos.collection_policy import load_cadence_snapshot,cadence_report
from db.videos.video_collection import VideoCollectionRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from hot_runtime import maintenance_access,HotDatabaseAdmissionDenied
from psycopg.types.json import Jsonb
from schema25_regressions.fixtures import Fixture,admission,dsns,CHANNEL

VIDEO='fs110000001'
SCAN='f1100000-0000-4000-8000-000000000001'
TOKEN='f1100000-0000-4000-8000-000000000002'
DEFAULT=CollectionSchedules()
CUSTOM=replace(DEFAULT,
    long_counters=replace(DEFAULT.long_counters,age_0_1d_seconds=43,age_1_7d_seconds=7011),
    long_metadata=replace(DEFAULT.long_metadata,age_0_7d_seconds=8113),
    short_metadata=TypedMetadata(17,29,0),stream_metadata=TypedMetadata(101,211,7))
GROUPS=('readiness','long_deadlines','typed_deadlines','closed_reconciliation','maintenance_orphan')
MAX_ROWS=1000
WORK_SECONDS=180


def attempt(index):
    assert type(index) is int and 1<=index<=3
    return 'f1100000-0000-4000-8000-'+str(index+10).zfill(12)


def switch(f,model):
    """A fresh pool models restart; it never mutates the prior immutable object."""
    before=f.snapshot();previous=f.database;config=previous.config
    previous.close()
    snapshot=None if model is None else model.cadence_snapshot()
    f.database=PostgresDatabase(config,cadence_snapshot=snapshot)
    f.database.open()
    f.check(f.database.cadence_snapshot is snapshot,'injected_snapshot_identity')
    with f.database.transaction(Operation('schedule_snapshot_identity')) as tx:
        f.check(tx.cadence_snapshot is snapshot,'borrowed_snapshot_identity')
    f.check(f.snapshot()==before,'restart_did_not_reschedule')


def bounded(f):
    count=f.observe();f.peak_rows=max(f.peak_rows,count)
    f.check(count<=MAX_ROWS,'row_budget')
    f.check(f.scalar('SELECT count(*) FROM public.youtube_api_request_attempts')==0,'quota_zero')


def queue(f):
    rows=f.rows('SELECT * FROM public.video_collection_state WHERE video_id=%s',(VIDEO,))
    f.check(len(rows)==1,'one_queue_row');return rows[0]


def seed(f,kind,age_days=2):
    """One fixed channel/video, at most one typed membership and completed scan."""
    f.channel();at=f.now-timedelta(minutes=2);anchor=at-timedelta(days=age_days)
    old=at-timedelta(days=2)
    f.rows('INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at) VALUES(%s,%s,%s,%s)',
           (VIDEO,CHANNEL,kind,anchor))
    if kind in ('short','stream'):
        playlist=('UUSH' if kind=='short' else 'UULV')+CHANNEL[2:]
        f.rows("INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,completed_at,source_checked_at,pages_completed,items_seen) VALUES(%s,%s,%s,%s,'full_reconciliation','completed','complete',%s,%s,1,1)",
               (SCAN,CHANNEL,kind,playlist,at,at))
        f.rows("INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at,inventory_status,last_completed_scan_id,inventory_completed_at) VALUES(%s,%s,%s,true,'ready',%s,'completed',%s,%s) ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,status='ready',next_due_at=excluded.next_due_at,inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id,inventory_completed_at=excluded.inventory_completed_at",
               (CHANNEL,kind,playlist,at,SCAN,at))
        f.rows("INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_scan_id,last_seen_scan_id,membership_status,first_seen_at,last_seen_at,last_present_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'present',%s,%s,%s)",
               (VIDEO,CHANNEL,kind,playlist,anchor,SCAN,SCAN,at,at,at))
        f.rows('UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type=%s',(kind,))
    f.rows("INSERT INTO public.videos(video_id,channel_id,title,views,like_count,comment_count,checked_at,published_at,metadata_checked_at,availability_checked_at,availability_status,privacy_status,upload_status) VALUES(%s,%s,'F11 synthetic',5,2,1,%s,%s,%s,%s,'public','public','processed')",
           (VIDEO,CHANNEL,old,anchor,old,old))
    f.rows("INSERT INTO public.video_collection_state(video_id,channel_id,video_type,stats_due_at,metadata_due_at,next_due_at,stats_source_checked_at,metadata_source_checked_at,typed_source_published_at,typed_collection_checked_at,typed_collection_status,hydration_needed) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)",
           (VIDEO,CHANNEL,kind,f.now+timedelta(hours=1) if kind=='long' else None,
            f.now+timedelta(hours=3) if kind=='long' else None,f.now+timedelta(hours=1),old,old,
            anchor if kind!='long' else None,old if kind!='long' else None,'pending' if kind!='long' else None))
    lease(f);bounded(f)
    return at,anchor


def lease(f):
    f.rows("UPDATE public.video_collection_state SET lease_token=%s::uuid,lease_owner='f11_schedule_probe',lease_channel_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s),lease_started_at=%s,lease_expires_at=%s WHERE video_id=%s",
           (TOKEN,CHANNEL,f.now-timedelta(minutes=3),f.now+timedelta(minutes=5),VIDEO))


def payload(at,anchor,index=1,kind='long'):
    return dict(video_id=VIDEO,channel_id=CHANNEL,video_type=kind,lease_token=TOKEN,
        collection_attempt_id=attempt(index),recorded_at=at.isoformat(),title='F11 synthetic',
        published_at=anchor.isoformat(),duration='PT2M',tags=['synthetic'],category_id='22',
        views=9+index,like_count=2,comment_count=1,availability_status='public',privacy_status='public',
        upload_status='processed',availability_checked_at=None,
        stream_actual_end_at=anchor.isoformat() if kind=='stream' else None,
        youtube_topic_categories=[],default_audio_language=None,default_language=None,caption_available=False)


def expect_denied(f,action,label,expected):
    before=f.snapshot()
    try:action()
    except DatabaseError as error:f.check(str(error)==expected,label+'_code')
    else:raise AssertionError(label+'_accepted')
    f.check(f.snapshot()==before,label+'_atomic')


def readiness(f):
    for model in (DEFAULT,CUSTOM):
        switch(f,model);before=f.snapshot();video=VideoCollectionRepository(f.database)
        f.check(video.schema_status()['schedule_source']=='process_snapshot','queue_snapshot_report')
        report=video.typed_schema_status()
        f.check(report['metadata_cadence_seconds']==cadence_report(f.database)['metadata_cadence_seconds'],'typed_snapshot_report')
        f.check(video.preflight_typed_collection_ready()['status']=='ok','typed_public_preflight')
        f.check(TypedInventoryRepository(f.database).assert_ready()['status']=='ok','inventory_public_ready')
        f.check(TypedInventoryRepository(f.database).assert_quota_defer_ready()['typed_jitter_seconds']==cadence_report(f.database)['typed_jitter_seconds'],'quota_snapshot_report')
        f.check(capabilities.get_hot_runtime_contract(f.database)['schema_ready'] is True,'hot_closed_ready')
        f.check(f.snapshot()==before,'readiness_did_not_write');bounded(f)
    for change in ('modified','missing'):
        if change=='modified':
            f.rows("UPDATE public.video_collection_cadence SET cadence_seconds=43 WHERE video_type='long' AND collection_kind='counters' AND availability_class='public' AND min_age_seconds=0")
        else:
            f.rows("DELETE FROM public.video_collection_cadence WHERE video_type='short' AND age_bucket='age_0_90d'")
        f.gate.ready=False
        try:
            before=f.snapshot()
            try:capabilities.get_hot_runtime_contract(f.database)
            except HotDatabaseAdmissionDenied as error:
                f.check(error.code=='hot_runtime_not_ready','blocked_ordinary_contract_denied')
            else:raise AssertionError('blocked_ordinary_contract_accepted')
            f.check(f.snapshot()==before,'blocked_ordinary_contract_atomic')
            # The actual startup controller scopes these fixed catalog reads.
            with maintenance_access():
                expect_denied(f,lambda:capabilities.get_hot_runtime_contract(f.database),'legacy_'+change,'collection_schedule_legacy_cadence_conflict')
        finally:f.gate.ready=True
        f.reset()
    switch(f,None)
    expect_denied(f,lambda:VideoCollectionRepository(f.database).schema_status(),'missing_snapshot','collection_schedule_snapshot_required')
    switch(f,DEFAULT)


def long_deadlines(f):
    for mode,age,default_seconds,custom_seconds in (('counters',2,7200,7011),('metadata',0.5,3600,43)):
        f.reset();switch(f,DEFAULT);at,anchor=seed(f,'long',age)
        method='complete_counters' if mode=='counters' else 'complete_metadata'
        preserved=queue(f)['metadata_due_at']
        row=payload(at,anchor)
        f.check(getattr(VideoCollectionRepository(f.database),method)([row])==1,'long_default_count')
        first=queue(f)
        f.check(first['stats_due_at']==at+timedelta(seconds=default_seconds),'long_default_counter_due')
        f.check(first['metadata_due_at']==(preserved if mode=='counters' else at+timedelta(seconds=21600)),'long_default_metadata_due')
        switch(f,CUSTOM)
        f.check(queue(f)==first,'nearest_existing_long_deadline_unchanged')
        before=f.snapshot()
        f.check(getattr(VideoCollectionRepository(f.database),method)([row])==1 and f.snapshot()==before,'long_old_receipt_replay')
        for index in (2,3):
            later=at+timedelta(seconds=index-1);lease(f)
            f.check(getattr(VideoCollectionRepository(f.database),method)([payload(later,anchor,index)])==1,'long_custom_count')
            after=queue(f)
            f.check(after['stats_due_at']==later+timedelta(seconds=custom_seconds),'long_custom_next_interval')
            f.check(after['metadata_due_at']==(preserved if mode=='counters' else later+timedelta(seconds=8113)),'long_custom_metadata_interval')
            f.check(after['next_due_at']==min(after['stats_due_at'],after['metadata_due_at']),'long_next_minimum')
            f.check(after['policy_version']==1 and after['lease_token'] is None,'long_unchanged_epoch_and_release')
            bounded(f)
    f.reset();switch(f,DEFAULT);at,anchor=seed(f,'long');switch(f,None)
    # Borrowed actual helper avoids the existing public completion receipt replay;
    # the one failing transaction must undo snapshot/source work already reached.
    from db.videos.video_counter_completion import complete_video_counter_collection
    expect_denied(f,lambda:complete_video_counter_collection(f.database,[payload(at,anchor)]),'missing_policy_rollback','collection_schedule_snapshot_required')
    switch(f,DEFAULT)


def expected_typed(f,model,kind,anchor,at):
    rules=getattr(model,kind+'_metadata')
    return f.scalar("SELECT %s::timestamptz+make_interval(secs=>CASE WHEN %s::timestamptz >= %s::timestamptz-interval '90 days' THEN %s ELSE %s END-%s+((('x'||substr(md5(%s),1,8))::bit(32)::bigint) %% (%s+1))::integer)",
        (at,anchor,at,rules.age_through_90d_seconds,rules.age_over_90d_seconds,rules.jitter_seconds,VIDEO,rules.jitter_seconds))


def typed_deadlines(f):
    for kind,mode in (('short','metadata'),('stream','metadata'),('stream','lifecycle')):
        for age in (2,100):
            f.reset();switch(f,DEFAULT);at,anchor=seed(f,kind,age)
            method='complete_stream_lifecycle' if mode=='lifecycle' else 'complete_typed_metadata'
            def row(index):
                value=payload(at+timedelta(seconds=index-1),anchor,index,kind)
                if mode=='lifecycle':value['stream_actual_end_at']=None
                return value
            f.check(getattr(VideoCollectionRepository(f.database),method)([row(1)])==1,'typed_default_count')
            first=queue(f)
            f.check(first['next_due_at']==expected_typed(f,DEFAULT,kind,anchor,at),'typed_default_native_due')
            switch(f,CUSTOM);f.check(queue(f)==first,'nearest_existing_typed_deadline_unchanged')
            before=f.snapshot()
            f.check(getattr(VideoCollectionRepository(f.database),method)([row(1)])==1 and f.snapshot()==before,'typed_old_receipt_replay')
            for index in (2,3):
                lease(f);later=at+timedelta(seconds=index-1)
                f.check(getattr(VideoCollectionRepository(f.database),method)([row(index)])==1,'typed_custom_count')
                after=queue(f)
                f.check(after['next_due_at']==expected_typed(f,CUSTOM,kind,anchor,later),'typed_custom_native_due')
                f.check(after['typed_collection_checked_at']==later and after['metadata_source_checked_at']==later,'typed_exact_source_clock')
                f.check(after['stats_due_at'] is None and after['metadata_due_at'] is None and after['policy_version']==1,'typed_no_counter_schedule')
                f.check(after['lease_token'] is None,'typed_owner_released');bounded(f)


def closed_reconciliation(f):
    f.reset();switch(f,CUSTOM);at,anchor=seed(f,'short',100)
    f.rows("UPDATE public.video_collection_state SET lease_token=NULL,lease_owner=NULL,lease_channel_epoch=NULL,lease_started_at=NULL,lease_expires_at=NULL,typed_collection_status='hydrated',typed_collection_checked_at=%s,paused_reason='channel_inventory_not_ready',next_due_at=%s WHERE video_id=%s",
           (at,f.now+timedelta(days=2),VIDEO))
    result=VideoCollectionRepository(f.database).reconcile_typed('short',1)
    f.check(result['affected_count']==1 and result['inventory_examined_count']==1,'closed_actual_reconcile_count')
    f.check(queue(f)['next_due_at']==expected_typed(f,CUSTOM,'short',anchor,at),'closed_actual_custom_interval')
    before=queue(f);switch(f,DEFAULT)
    # Ordinary reconciliation may advance its existing cursor, but not rewrite a
    # healthy queue just because a different process schedule was injected.
    wrapped=VideoCollectionRepository(f.database).reconcile_typed('short',1)
    visited=VideoCollectionRepository(f.database).reconcile_typed('short',1)
    f.check(wrapped['inventory_examined_count']==0 and visited['inventory_examined_count']==1,'healthy_reconcile_was_examined')
    f.check(queue(f)==before,'healthy_reconcile_no_reschedule')
    def extra_key():
        with f.database.transaction(Operation('schedule_closed_negative')) as tx:
            tx._run(None,dict(video_type='short',inventory_ids=[VIDEO],scopes=Jsonb([]),cadence=Jsonb([])),'typed_reconcile_acquire',1000)
    expect_denied(f,extra_key,'closed_extra_key','database_typed_reconcile_capability_invalid')
    bounded(f)


def maintenance_orphan(f):
    """Actual deleted-channel maintenance reaches the queue policy with no source."""
    from db.hot.hot_retention import RetentionError
    f.reset();switch(f,CUSTOM);f.channel()
    f.rows("UPDATE public.channels SET title=NULL,avatar_url=NULL,views=NULL,subscribers=NULL,video_count=NULL,country_code=NULL,uploads_playlist_id=NULL,channel_created_at=NULL,initial_inventory_estimate=NULL WHERE channel_id=%s",(CHANNEL,))
    f.rows("UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=%s WHERE channel_id=%s",(f.now-timedelta(days=30),CHANNEL))
    f.rows("INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,first_seen_at,last_seen_at) VALUES(%s,%s,'unknown_unavailable','unknown_unavailable',%s,%s)",(VIDEO,CHANNEL,f.now-timedelta(days=30),f.now-timedelta(days=30)))
    f.check(f.scalar('SELECT count(*) FROM public.video_inventory')==0 and f.scalar('SELECT count(*) FROM public.video_collection_state')==0,'orphan_has_no_source_or_queue')
    playlist_count=f.scalar('SELECT count(*) FROM public.channel_playlist_state WHERE channel_id=%s',(CHANNEL,))
    f.check(0<=playlist_count<=3,'orphan_playlist_bound');bounded(f)
    switch(f,None);before=f.snapshot()
    try:f.cleanup('deleted_channels',limit=1)
    except RetentionError as error:f.check(str(error)=='cleanup_outcome_unknown','orphan_missing_snapshot_denied')
    else:raise AssertionError('orphan_missing_snapshot_accepted')
    f.check(f.snapshot()==before,'orphan_missing_snapshot_atomic')
    from db.hot.hot_runtime import maintenance_repository
    config=f.database.config;f.database.close()
    snapshot=CUSTOM.cadence_snapshot()
    repository=maintenance_repository(config,cadence_snapshot=snapshot)
    # Transfer this sole runtime pool to the Fixture's unconditional close owner.
    f.database=repository.database
    f.check(f.database.cadence_snapshot is snapshot,'orphan_factory_snapshot_identity')
    f.check(f.snapshot()==before,'orphan_factory_did_not_write')
    page=repository.cleanup_page('deleted_channels',limit=1)
    f.check(page.scanned==1 and page.deleted==playlist_count+2 and page.deferred==0 and page.scrubbed==0,'orphan_actual_maintenance_receipt')
    f.check(f.scalar('SELECT count(*) FROM public.channels WHERE channel_id=%s',(CHANNEL,))==0 and f.scalar('SELECT count(*) FROM public.channel_collection_state WHERE channel_id=%s',(CHANNEL,))==0,'orphan_parent_removed')
    f.check(f.scalar('SELECT count(*) FROM public.video_availability_incidents WHERE video_id=%s',(VIDEO,))==0,'orphan_incident_removed')
    f.check(f.scalar('SELECT count(*) FROM public.video_inventory')==0 and f.scalar('SELECT count(*) FROM public.video_collection_state')==0,'orphan_did_not_create_queue')
    bounded(f)


def safe_failure(error):
    frames=[]
    for frame in traceback.extract_tb(error.__traceback__):
        try:name=Path(frame.filename).resolve().relative_to(ROOT).as_posix()
        except ValueError:continue
        frames.append({'file':name,'line':frame.lineno})
    code=getattr(error,'code',None)
    labels={node.args[1].value for node in ast.walk(ast.parse(Path(__file__).read_bytes()))
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='check'
            and len(node.args)>=2 and isinstance(node.args[1],ast.Constant) and isinstance(node.args[1].value,str)}
    check_code=error.args[0] if isinstance(error,AssertionError) and len(error.args)==1 and isinstance(error.args[0],str) and error.args[0] in labels else None
    return {'check_code':check_code,'error_type':type(error).__name__ if type(error).__name__ in {'AssertionError','DatabaseError','DatabaseServerError','DatabaseUnavailable','TypeError','ValueError','KeyError','HotDatabaseAdmissionDenied','HotRuntimeUnavailable'} else 'unclassified',
            'sqlstate':code if isinstance(code,str) and re.fullmatch('[A-Z0-9]{5}',code) else None,
            'frames':frames[-5:]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-proof',action='store_true')
    args=parser.parse_args(argv)
    if not args.run_proof:parser.error('Explicit --run-proof required; no implicit native execution.')
    if os.environ.get('LIC_F11_MAX_ROWS')!='1000':parser.error('Explicit LIC_F11_MAX_ROWS=1000 required.')
    start=time.monotonic();f=None;stage='identity';report={'status':'failed','groups':[],'cleanup_verified':False,'row_limit':MAX_ROWS,'provider_requests':0}
    try:
        with admission() as gate:
            f=Fixture(*dsns(),gate,cadence_snapshot=DEFAULT.cadence_snapshot());f.deadline=start+WORK_SECONDS;f.peak_rows=0
            try:
                for name in GROUPS:
                    stage=name;checks=f.checks
                    globals()[name](f);bounded(f)
                    f.check(f.checks>checks,'nonempty_group')
                    report['groups'].append({'name':name,'checks':f.checks-checks,'status':'passed'})
                    f.reset()
                stage='final_identity';f.identity()
                report.update(status='passed',peak_rows=f.peak_rows)
            finally:
                stage='cleanup' if report['status']=='passed' else stage
                f.close();report['cleanup_verified']=True
    except BaseException as error:report.update(status='failed',failure_stage=stage,**safe_failure(error))
    report['elapsed_seconds']=round(time.monotonic()-start,3)
    print(json.dumps(report,sort_keys=True),flush=True)
    return 0 if report['status']=='passed' and report['cleanup_verified'] else 1

if __name__=='__main__':raise SystemExit(main())