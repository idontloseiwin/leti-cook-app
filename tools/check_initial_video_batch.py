#!/usr/bin/env python3
"""Explicit initial-load atomic batch/400-target probe on empty disposable PG17.

No server launch, schema/ACL changes or provider requests. Two connections and
1000 total synthetic rows maximum. Uses actual repository and transaction owners.
"""
import argparse
from contextlib import contextmanager
from datetime import timedelta
from functools import partial
from threading import Barrier,Lock
import json
import os
import re
import traceback
from pathlib import Path
import sys
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'application'))
import check_direct_pg_repositories as fixtures
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseUnavailable,PostgresConfig,PostgresDatabase
from db.videos.video_collection import VideoCollectionRepository
from db.jobs.operator_jobs import OperatorJobRepository

safety=fixtures.safety
CHANNEL='UC0000000000000000000001'
TOKEN='ac000000-0000-4000-8000-000000000001'
ATTEMPT='ac000000-0000-4000-8000-000000000002'
JOB='f8200000-0000-4000-8000-000000000001'
KINDS=('long','short','stream')
SCANS={kind:'f8200000-0000-4000-8000-00000000001'+str(i) for i,kind in enumerate(KINDS,1)}
RECEIPTS={kind:'f8200000-0000-4000-8000-00000000002'+str(i) for i,kind in enumerate(KINDS,1)}
CASES={'atomic':'Mixed Long, Short, Stream, missing and failure commit and complete rollback.',
       'receipts':'Confirmed commit then lost acknowledgement is never retried; explicit receipts remain exact.',
       'chunks':'400-target full initial preparation, one-unit reservation, bounded unsent refund and settlement.',
       'service':'Actual initial coordinator, concurrent synthetic provider,100 source writes, job ACK and verification.'}


def seed_channel(probe,channel_id=CHANNEL):
    # Historical seed helpers call removed SQL workflows. Compose the current
    # real Python channel owner instead; only the test fixture binding changes.
    from db.channel.channel_control import create_channel_for_collection
    create_channel_for_collection(probe.database,dict(channel_id=channel_id,title='initial fixture',
        initial_long_inventory_status='completed',initial_28d_stats_status='completed'),'accepted')
    probe.admin.execute("UPDATE public.channels SET user_input=%s,channel_metadata_status='ok',channel_metadata_checked_at=%s WHERE channel_id=%s",
        ('https://www.youtube.com/channel/'+channel_id,probe.now-timedelta(minutes=1),channel_id))
    probe.observe()


def safe_failure(error):
    from db.base.operation_contracts import OPERATIONS
    result=fixtures.safe_failure(error)
    codes=frozenset(code for op in OPERATIONS.values() for code in op.error_codes)
    token=getattr(error,'message',None)
    if token in codes:result['failure_code']=token
    # Emit only code locations and a missing SQL function name, never arguments,
    # connection details, row contents or a raw server diagnostic.
    result['trace']=[dict(file=Path(frame.filename).name,line=frame.lineno,function=frame.name)
        for frame in traceback.extract_tb(error.__traceback__)[-5:]]
    if getattr(error,'sqlstate',None)=='42883':
        message=getattr(getattr(error,'diag',None),'message_primary','') or ''
        match=re.match(r'function ([a-z_][a-z0-9_.]*)\(',message)
        if match:result['missing_function']=match.group(1)
    return result


class DirectOnly:
    def __init__(self,database,*,lose_first_ack=False):
        self.database=database;self.lose_first_ack=lose_first_ack
        self.attempts=0;self.commits=0;self.ack_losses=0;self.rpc_calls=0
    def call(self,*args,**kwargs):
        self.rpc_calls+=1
        raise AssertionError('initial_probe_sql_workflow_forbidden')
    @contextmanager
    def transaction(self,operation):
        self.attempts+=1
        with self.database.transaction(operation) as tx:yield tx
        self.commits+=1
        if self.lose_first_ack and self.ack_losses==0:
            self.ack_losses+=1
            raise DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)


def state(probe):
    return probe.admin.scalar("""SELECT jsonb_build_object(
 'queue',coalesce((SELECT jsonb_agg(to_jsonb(s)-'created_at'-'updated_at' ORDER BY video_id) FROM public.video_collection_state s),'[]'),
 'receipts',coalesce((SELECT jsonb_agg(to_jsonb(r)-'created_at' ORDER BY video_id,collection_attempt_id) FROM public.typed_video_collection_receipts r),'[]'),
 'sources',coalesce((SELECT jsonb_agg(to_jsonb(v) ORDER BY video_id) FROM public.videos v),'[]'),
 'snapshots',coalesce((SELECT jsonb_agg(to_jsonb(s)-'id' ORDER BY video_id,collection_attempt_id) FROM public.video_snapshots s),'[]'),
 'incidents',coalesce((SELECT jsonb_agg(to_jsonb(i)-'id'-'created_at' ORDER BY video_id) FROM public.video_availability_incidents i),'[]'),
 'events',coalesce((SELECT jsonb_agg(to_jsonb(e)-'id'-'created_at' ORDER BY video_id,event_type) FROM public.video_availability_events e),'[]'))""")


def seed_mixed(probe,*,terminal_stream=False,include_failure=True):
    seed_channel(probe)
    old=probe.now-timedelta(days=2);at=probe.now-timedelta(minutes=1)
    ids=[f'ib{i:09d}' for i in range(5 if include_failure else 4)]
    source=[dict(video_id=video,channel_id=CHANNEL,video_type=kind,lease_token=TOKEN,
        collection_attempt_id=ATTEMPT,recorded_at=at.isoformat(),availability_status='private',
        availability_source='initial_probe') for video,kind in zip(ids[:3],KINDS)]
    extra=ids[3:]
    with probe.admin.transaction():
        for kind in ('short','stream'):
            playlist={'short':'UUSH','stream':'UULV'}[kind]+CHANNEL[2:]
            probe.admin.execute("INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled) VALUES(%s,%s,%s,true)",(CHANNEL,kind,playlist))
            probe.admin.execute("""INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,
                scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at,token_history_verified)
                VALUES(%s::uuid,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s,true)""",
                (SCANS[kind],CHANNEL,kind,playlist,old,old,probe.now,probe.now))
            probe.admin.execute("UPDATE public.channel_playlist_state SET inventory_status='completed',last_completed_scan_id=%s::uuid WHERE channel_id=%s AND video_type=%s",(SCANS[kind],CHANNEL,kind))
        for index,video in enumerate(ids):
            kind=KINDS[index] if index<3 else 'long'
            probe.admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at) VALUES(%s,%s,%s,%s)",(video,CHANNEL,kind,old))
            if kind!='long':
                playlist={'short':'UUSH','stream':'UULV'}[kind]+CHANNEL[2:]
                probe.admin.execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
                    source_published_at,first_seen_at,last_seen_at,last_present_at,first_seen_scan_id,last_seen_scan_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,%s::uuid)""",
                    (video,CHANNEL,kind,playlist,old,old,probe.now,probe.now,SCANS[kind],SCANS[kind]))
            probe.admin.execute("""INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,metadata_checked_at,
                availability_checked_at,availability_status,privacy_status,upload_status)
                VALUES(%s,%s,%s,%s,%s,%s,'public','public','processed')""",(video,CHANNEL,old,old,old,old))
            probe.admin.execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
                typed_collection_status,typed_source_published_at,typed_collection_checked_at)
                VALUES(%s,%s,%s,CASE WHEN %s<>'long' THEN 'pending' END,%s,%s)""",(video,CHANNEL,kind,kind,old,old))
            probe.admin.execute("""UPDATE public.video_collection_state SET lease_token=%s::uuid,lease_owner='initial_batch_probe',
                lease_channel_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s),
                lease_started_at=%s,lease_expires_at=%s,stats_source_checked_at=%s,metadata_source_checked_at=%s,
                hydration_needed=true,consecutive_failures=2,last_error_type='temporary'
                WHERE video_id=%s""",(TOKEN,CHANNEL,probe.now-timedelta(minutes=2),probe.now+timedelta(minutes=5),old,old,video))
    def metadata(row):
        return dict(row,title='atomic initial batch',published_at=old.isoformat(),duration='PT2M',tags=['test'],
            category_id='22',views=100,like_count=2,comment_count=0,availability_status='public',privacy_status='public',
            upload_status='processed',availability_checked_at=None,availability_restored_at=None,
            youtube_topic_categories=[],default_audio_language=None,default_language=None,caption_available=False,
            stream_actual_end_at=old.isoformat() if row['video_type']=='stream' else None)
    commands=[dict(operation='complete_metadata',rows=[metadata(source[0])]),
              dict(operation='complete_typed_metadata',rows=[metadata(source[1])])]
    commands.append(dict(operation='complete_typed_metadata' if terminal_stream else 'complete_stream_lifecycle',
        rows=[metadata(source[2]) if terminal_stream else dict(source[2],stream_actual_end_at=None,title='Waiting publication',
            published_at=old.isoformat(),duration=None,publication_status='live',scheduled_start_at=None)]))
    commands.append(dict(operation='complete_missing',rows=[dict(source[0],video_id=extra[0])]))
    if include_failure:
        commands.append(dict(operation='fail_batch',rows=[dict(video_id=extra[1],video_type='long',lease_token=TOKEN,
            error_type='youtube_request_failed',retry_after_seconds=60)]))
    probe.observe()
    return commands


def assert_settled(probe,commands,terminal_stream=False):
    snapshot=state(probe);rows=[r for c in commands for r in c['rows']]
    fixtures.require(len(snapshot['queue'])==len(rows),'initial_batch_queue_count')
    fixtures.require(all(row['lease_token'] is None for row in snapshot['queue']),'initial_batch_all_leases_settled')
    fixtures.require(len(snapshot['snapshots'])==(3 if terminal_stream else 2),'initial_batch_snapshot_count')
    fixtures.require(len(snapshot['receipts'])==3,'initial_batch_receipt_count')
    fixtures.require(len(snapshot['incidents'])==len(snapshot['events'])==1,'initial_batch_missing_history')
    return snapshot


def atomic(probe):
    for terminal in (False,True):
        probe.reset();commands=seed_mixed(probe,terminal_stream=terminal)
        boundary=DirectOnly(probe.database)
        fixtures.require(probe.call(VideoCollectionRepository(boundary).complete_initial_batch,commands)==5,'initial_batch_five_results')
        assert_settled(probe,commands,terminal)
        probe.check('initial_atomic_terminal_'+str(terminal).lower(),(boundary.attempts,boundary.commits,boundary.rpc_calls)==(1,1,0))
    for scenario in ('stale_last','expired_short','generation'):
        probe.reset();commands=seed_mixed(probe)
        if scenario=='stale_last':commands[-1]['rows'][0]['lease_token']='ac000000-0000-4000-8000-000000000099'
        elif scenario=='expired_short':
            probe.admin.execute('UPDATE public.video_collection_state SET lease_expires_at=%s WHERE video_id=%s',
                (probe.now-timedelta(seconds=1),commands[1]['rows'][0]['video_id']))
        else:probe.admin.execute('UPDATE public.channel_collection_state SET control_epoch=control_epoch+1 WHERE channel_id=%s',(CHANNEL,))
        before=state(probe);boundary=DirectOnly(probe.database)
        try:probe.call(VideoCollectionRepository(boundary).complete_initial_batch,commands)
        except DatabaseError:pass
        else:raise fixtures.ProbeFailure('repository_probe_initial_batch_failure_missing')
        probe.check('initial_atomic_rollback_'+scenario,state(probe)==before and boundary.attempts==1 and boundary.commits==0)


def receipts(probe):
    for fail in (False,True):
        probe.reset();commands=seed_mixed(probe,include_failure=fail)
        boundary=DirectOnly(probe.database,lose_first_ack=True)
        try:probe.call(VideoCollectionRepository(boundary).complete_initial_batch,commands)
        except DatabaseError as error:fixtures.require(error.outcome_unknown,'initial_batch_ack_unknown')
        else:raise fixtures.ProbeFailure('repository_probe_initial_batch_ack_missing')
        after=assert_settled(probe,commands)
        probe.check('initial_ack_once_'+str(fail).lower(),(boundary.attempts,boundary.commits,boundary.ack_losses)==(1,1,1))
        repository=VideoCollectionRepository(probe.database)
        if fail:
            # Failure operations have no receipts: the public boundary never
            # tries this replay itself; an explicit attempt must roll back.
            try:probe.call(repository.complete_initial_batch,commands)
            except DatabaseError:pass
            else:raise fixtures.ProbeFailure('repository_probe_initial_failure_replay_accepted')
        else:fixtures.require(probe.call(repository.complete_initial_batch,commands)==4,'initial_receipt_explicit_replay')
        probe.check('initial_replay_preserves_'+str(fail).lower(),state(probe)==after)


def seed_chunk(probe,count=400):
    fixtures.require(type(count) is int and 1<=count<=400,'initial_chunk_seed_bound')
    seed_channel(probe)
    ids=[f'ib{i:09d}' for i in range(count)]
    requested=probe.now-timedelta(minutes=3);started=probe.now-timedelta(minutes=2)
    boundary=probe.now-timedelta(minutes=1);checked=probe.now-timedelta(seconds=30)
    sources={}
    with probe.admin.transaction():
        probe.admin.execute("""INSERT INTO public.operator_jobs(id,job_type,channel_id,requested_at,initial_scope_version,
            initial_video_types,max_items,max_pages,max_quota_units,phase,accepted_epoch)
            VALUES(%s::uuid,'channel_initial_long_inventory',%s,%s,1,ARRAY['long','short','stream'],NULL,NULL,NULL,'inventory',
            (SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s))""",(JOB,CHANNEL,requested,CHANNEL))
        for kind in KINDS:
            playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+CHANNEL[2:]
            probe.admin.execute("""INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,
                scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at,pages_completed)
                VALUES(%s::uuid,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s,1)""",
                (SCANS[kind],CHANNEL,kind,playlist,boundary,started,checked,checked))
            probe.admin.execute("""INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,scan_id,
                transition_kind,source_checked_at,result) VALUES('scan_transition',%s::uuid,%s::uuid,'page',%s,'{}'::jsonb)""",
                (RECEIPTS[kind],SCANS[kind],checked))
            sources[kind]=dict(scan_id=SCANS[kind],started_at=started.isoformat(),scan_boundary_at=boundary.isoformat(),
                source_checked_at=checked.isoformat(),acked_pages=1,terminal_outcome='completed',last_transition_id=RECEIPTS[kind])
    summary=dict(full_inventory_sources=sources,inventory_results={kind:{'inventory_ready':True} for kind in KINDS})
    with probe.admin.transaction():
        probe.admin.execute("""INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at)
            SELECT id,%s,'long',%s,%s FROM unnest(%s::text[]) v(id)""",(CHANNEL,probe.now-timedelta(days=2),checked,ids))
        # Queue rows are intentionally absent: preparation does not claim work.
        probe.admin.execute("""INSERT INTO public.operator_job_targets(job_id,video_id,video_type)
            SELECT %s::uuid,id,'long' FROM unnest(%s::text[]) v(id)""",(JOB,ids))
        probe.admin.execute("""UPDATE public.operator_jobs SET status='running',lease_token=%s::uuid,lease_owner='initial_chunk_probe',
            lease_channel_epoch=accepted_epoch,lease_expires_at=%s,target_count=%s,max_target_attempts=3,phase='hydrate_videos',
            targets_frozen_at=%s,summary_json=%s::jsonb WHERE id=%s::uuid""",
            (TOKEN,probe.now+timedelta(minutes=5),count,probe.now-timedelta(seconds=10),Jsonb(summary),JOB))
        probe.admin.execute('UPDATE public.channel_collection_state SET initial_job_id=%s::uuid WHERE channel_id=%s',(JOB,CHANNEL))
    probe.observe();return ids


def chunks(probe):
    probe.reset();ids=seed_chunk(probe);repository=OperatorJobRepository(probe.database)
    prepared=probe.call(repository.prepare_initial_hydration_chunk,JOB,TOKEN,limit=400)
    fixtures.require(prepared['video_ids']==ids and prepared['reserved_units']==8,'initial_chunk_four_hundred_eight_units')
    fixtures.require(probe.admin.scalar('SELECT min(job_attempts) FROM public.operator_job_targets WHERE job_id=%s::uuid',(JOB,))==1,'initial_chunk_attempts_reserved')
    # Make a stop before dispatch observable even at the last permitted attempt.
    probe.admin.execute('UPDATE public.operator_job_targets SET job_attempts=3 WHERE job_id=%s::uuid',(JOB,))
    before=probe.admin.scalar('SELECT to_jsonb(job) FROM public.operator_jobs job WHERE id=%s::uuid',(JOB,))
    try:probe.call(repository.prepare_chunk,JOB,TOKEN,limit=400)
    except DatabaseError:pass
    else:raise fixtures.ProbeFailure('repository_probe_initial_manual_limit_widened')
    fixtures.require(before==probe.admin.scalar('SELECT to_jsonb(job) FROM public.operator_jobs job WHERE id=%s::uuid',(JOB,)),'initial_manual_bound_rollback')
    value=probe.call(repository.settle_initial_hydration_chunk,JOB,TOKEN,observed_quota_units=0,error_code=None,
        summary={'hydration_unstarted_video_ids':ids})
    counts=probe.admin.scalar("""SELECT jsonb_build_object('pending',count(*) FILTER(WHERE status='pending'),
        'attempts',min(job_attempts),'max_attempts',max(job_attempts)) FROM public.operator_job_targets WHERE job_id=%s::uuid""",(JOB,))
    probe.check('initial_chunk_stop_refund',counts==dict(pending=400,attempts=2,max_attempts=2)
        and value['status']=='queued' and 'hydration_unstarted_video_ids' not in value['summary_json']
        and 'full_hydration_chunk' not in value['summary_json'])
    try:probe.call(repository.settle_initial_hydration_chunk,JOB,TOKEN,observed_quota_units=0,error_code=None,
        summary={'hydration_unstarted_video_ids':ids})
    except DatabaseError:pass
    else:raise fixtures.ProbeFailure('repository_probe_initial_chunk_settlement_replayed')
    probe.check('initial_chunk_refund_once',probe.admin.scalar('SELECT min(job_attempts) FROM public.operator_job_targets WHERE job_id=%s::uuid',(JOB,))==2)


def service(probe):
    from collection.video_execution import run_video_collection_queue_batch
    from collection.video_payloads import build_video_collection_metadata_payload
    from collection.settings import CollectionSettings
    from video_collection_service import YouTubeViewCountAbsent,YouTubeViewCountInvalid
    from youtube_source_fields import parse_dt
    probe.reset();ids=seed_chunk(probe,100);repository=OperatorJobRepository(probe.database)
    prepared=probe.call(repository.prepare_initial_hydration_chunk,JOB,TOKEN,limit=400)
    fixtures.require(prepared['video_ids']==ids and prepared['reserved_units']==2,'initial_service_prepared')
    barrier=Barrier(2,timeout=10);lock=Lock();calls=[]
    def provider(video_ids,phase):
        # The two real50-ID requests overlap. This callback never touches DB.
        with lock:calls.append(tuple(video_ids))
        barrier.wait()
        return [dict(id=video,snippet=dict(channelId=CHANNEL,title='initial service',
            publishedAt=(probe.now-timedelta(days=2)).isoformat(),tags=['synthetic'],categoryId='22',liveBroadcastContent='none'),
            contentDetails=dict(duration='PT2M',caption='false'),statistics=dict(viewCount='100',likeCount='2',commentCount='0'),
            status=dict(privacyStatus='public',uploadStatus='processed'),topicDetails={}) for video in video_ids]
    def forbidden(*args,**kwargs):raise AssertionError('split_request_not_allowed')
    summary=probe.call(run_video_collection_queue_batch,limit=100,explicit_video_ids=ids,initial_job=prepared['job'],
        force_metadata=True,initial_load_concurrency=8,reason='initial_channel_hydration',database=probe.database,
        settings=CollectionSettings(True,True,True),fetch_initial_profiles=provider,fetch_full=forbidden,
        fetch_metadata=forbidden,fetch_counters=forbidden,build_full_payload=partial(build_video_collection_metadata_payload,
            parse_dt=parse_dt,view_count_absent_error=YouTubeViewCountAbsent,view_count_invalid_error=YouTubeViewCountInvalid),
        build_counter_payload=forbidden,is_quota_exhausted=lambda _:False,worker_id_factory=forbidden)
    probe.check('initial_service_parallel_provider',len(calls)==2 and all(len(batch)==50 for batch in calls)
        and set(v for batch in calls for v in batch)==set(ids))
    probe.check('initial_service_committed',summary['updated']==100 and summary['metadata_requests']==2
        and summary['batch_requests']==0 and not summary['infrastructure_error'] and not summary['settlement_unknown'])
    counts=probe.admin.scalar("""SELECT jsonb_build_object('videos',(SELECT count(*) FROM public.videos),
        'snapshots',(SELECT count(*) FROM public.video_snapshots),
        'released',(SELECT count(*) FROM public.video_collection_state WHERE lease_token IS NULL AND NOT hydration_needed))""")
    probe.check('initial_service_all_rows',counts==dict(videos=100,snapshots=100,released=100))
    value=probe.call(repository.settle_initial_hydration_chunk,JOB,TOKEN,observed_quota_units=2,error_code=None,
        summary={'hydration_unstarted_video_ids':summary['unstarted_video_ids']})
    probe.check('initial_service_settled',value['status']=='queued' and value['phase']=='verify' and value['updated_count']==100)
    claimed=probe.call(repository.claim_initial_hydration,CHANNEL,'initial_verifier')
    fixtures.require(claimed is not None and claimed['phase']=='verify','initial_service_verify_claim')
    verified=probe.call(repository.verify_initial_hydration,JOB,claimed['lease_token'])
    probe.check('initial_service_verified',verified['status']=='completed' and verified['summary_json']['current_profile_ready'] is True)
    probe.observe()


RUNNERS={'atomic':atomic,'receipts':receipts,'chunks':chunks,'service':service}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=sorted(CASES),action='append',default=[])
    parser.add_argument('--list-cases',action='store_true');parser.add_argument('--report',type=Path)
    args=parser.parse_args(argv)
    if args.list_cases:print(json.dumps({'cases':CASES,'seed_row_limit':1000}));return 0
    if not args.case or len(args.case)!=len(set(args.case)):parser.error('Select distinct cases explicitly.')
    if args.report and args.report.exists():parser.error('Report must be a new file.')
    report={'status':'failed','cases':[],'youtube_requests':0,'seed_row_limit':1000,'fixture_cleanup_incomplete':False,
        'limitations':['Synthetic leases and source proofs; no live provider or application UI.',
                       'Unknown ACK injected after confirmed COMMIT; no physical transport cut.']}
    admin=None;dirty=False;metrics={'client_execute_calls':0,'client_execute_seconds':0.0}
    try:
        dsns=safety.validate_configuration(os.environ.get('LIC_DIRECT_PG_PROBE_ADMIN_DSN',''),os.environ.get('LIC_DIRECT_PG_PROBE_DSN',''))
        admin=safety.connect(dsns[0],metrics);snapshots,counts=safety.preflight(admin)
        with fixtures.self_hosted_profile(),PostgresDatabase(PostgresConfig(dsns[1],min_size=0,max_size=1,max_waiting=1),
                admission=lambda _:None,cadence_snapshot=CollectionSchedules().cadence_snapshot()) as database:
            for case in args.case:
                probe=fixtures.Probe(admin,database,snapshots,counts);item={'case_id':case,'status':'failed'};report['cases'].append(item);dirty=True
                try:RUNNERS[case](probe);probe.observe();item['status']='passed'
                except BaseException as error:item.update(safe_failure(error));raise
                finally:
                    try:probe.reset();dirty=False
                    except BaseException as error:item.update(status='failed',cleanup_failure=safe_failure(error));raise
                    finally:item.update(probe.report())
        report['status']='passed'
    except BaseException as error:report.update(safe_failure(error),fixture_cleanup_incomplete=dirty)
    finally:
        if admin is not None:admin.connection.close()
    encoded=json.dumps(report,indent=2)
    if args.report:
        with args.report.open('x',encoding='utf-8') as handle:handle.write(encoded+'\n')
    print(encoded);return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
