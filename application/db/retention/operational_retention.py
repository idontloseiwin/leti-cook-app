"""Finite operational-copy retention, borrowed transaction only.

The closed maintenance driver entry is a separate prerequisite. No provider,
retained business function, retry, Python clock or native JSON number decoding
is used. Four fixed protocols preserve row-local lock recovery and native clocks.
"""
from psycopg.types.json import Jsonb
from . import inventory_chain_retention as chains
from operational_copy_policy import quota_label
from ..inventory.inventory_scan_clock import SCAN_IS_HOT_SQL
from ..runs.sync_run_summary import persisted_summary_text
from ..runs.sync_run_write_normalization import scrub_sync_run
from ..quota.quota_retry_detachment import delete_expired_quota_attempt
from ..base.postgres import DatabaseServerError

ERRORS=frozenset({'hot_operational_limit_invalid','hot_operational_cursor_invalid','hot_operational_target_invalid'})
TARGETS=frozenset({'operator_jobs','operator_job_targets','sync_runs','quota_attempts'})
ORDERED=frozenset({'sync_runs','quota_attempts'})
_HEADER="""WITH native AS(SELECT %s::text AS target,%s::text AS cursor,%s::integer AS limit),
 clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT native.*,length(cursor) AS cursor_length,to_jsonb(at)#>>'{}' AS at,
 to_jsonb(at-interval '696 hours')#>>'{}' AS cutoff FROM native CROSS JOIN clock"""
_PAGE={
 'operator_jobs':"SELECT id::text AS id,NULL::text AS video,id::text AS cursor FROM public.operator_jobs WHERE %(after)s::uuid IS NULL OR id>%(after)s::uuid ORDER BY id LIMIT %(limit)s::integer",
 'operator_job_targets':"SELECT job_id::text AS id,video_id AS video,jsonb_build_array(job_id::text,video_id)::text AS cursor FROM public.operator_job_targets WHERE %(after)s::text IS NULL OR (job_id,video_id)>(%(after_job)s::uuid,%(after_video)s::text) ORDER BY job_id,video_id LIMIT %(limit)s::integer",
# Runs and API records have one immutable clock: only rows past the cutoff or from
# the future are read, oldest first, through the (started_at, key) indexes. Fresh
# rows were normalized by their writers and need no visit.
 'sync_runs':"SELECT id,video,cursor FROM ((SELECT id::text AS id,NULL::text AS video,started_at AS clock,id AS key,jsonb_build_array(to_jsonb(started_at)#>>'{}',id::text)::text AS cursor FROM public.sync_runs WHERE started_at<%(cutoff)s::timestamptz AND (%(after)s::text IS NULL OR (started_at,id)>(%(after_clock)s::timestamptz,%(after_id)s::bigint)) ORDER BY started_at,id LIMIT %(limit)s::integer) UNION ALL (SELECT id::text AS id,NULL::text AS video,started_at AS clock,id AS key,jsonb_build_array(to_jsonb(started_at)#>>'{}',id::text)::text AS cursor FROM public.sync_runs WHERE started_at>%(at)s::timestamptz AND (%(after)s::text IS NULL OR (started_at,id)>(%(after_clock)s::timestamptz,%(after_id)s::bigint)) ORDER BY started_at,id LIMIT %(limit)s::integer)) page ORDER BY clock,key LIMIT %(limit)s::integer",
 'quota_attempts':"SELECT id,video,cursor FROM ((SELECT request_attempt_id::text AS id,NULL::text AS video,started_at AS clock,request_attempt_id AS key,jsonb_build_array(to_jsonb(started_at)#>>'{}',request_attempt_id::text)::text AS cursor FROM public.youtube_api_request_attempts WHERE started_at<%(cutoff)s::timestamptz AND (%(after)s::text IS NULL OR (started_at,request_attempt_id)>(%(after_clock)s::timestamptz,%(after_id)s::uuid)) ORDER BY started_at,request_attempt_id LIMIT %(limit)s::integer) UNION ALL (SELECT request_attempt_id::text AS id,NULL::text AS video,started_at AS clock,request_attempt_id AS key,jsonb_build_array(to_jsonb(started_at)#>>'{}',request_attempt_id::text)::text AS cursor FROM public.youtube_api_request_attempts WHERE started_at>%(at)s::timestamptz AND (%(after)s::text IS NULL OR (started_at,request_attempt_id)>(%(after_clock)s::timestamptz,%(after_id)s::uuid)) ORDER BY started_at,request_attempt_id LIMIT %(limit)s::integer)) page ORDER BY clock,key LIMIT %(limit)s::integer",
}
_JOB="""SELECT id::text,channel_id,inventory_scan_id::text,initial_scope_version,summary_json::text AS summary,
 to_jsonb(requested_at)#>>'{}' AS source_at,last_error_code,
 to_jsonb(lease_expires_at)#>>'{}' AS lease_expires_at,lease_token IS NOT NULL AS has_lease
 FROM public.operator_jobs WHERE id=%(id)s::uuid"""
# The retained scan predicate takes one sampled clock argument. This static
# expansion uses a module-owned native alias, never a caller-provided identifier.
_HOT=SCAN_IS_HOT_SQL.replace('%(scan_at)s::timestamptz','native.at')
_SCAN_HOT='(SELECT '+_HOT+' FROM (SELECT clock_timestamp() AS at OFFSET 0) native)'
_LINKED="""SELECT NOT coalesce(isfinite(scan.started_at) AND scan.started_at BETWEEN %(cutoff)s::timestamptz AND clock_timestamp()
 AND isfinite(scan.scan_boundary_at) AND scan.scan_boundary_at BETWEEN %(cutoff)s::timestamptz AND clock_timestamp(),false)
 OR NOT """+_SCAN_HOT+" AS expired FROM public.channel_video_inventory_scans scan WHERE scan.scan_id=%(scan_id)s::uuid"
_FULL_SOURCES="""SELECT EXISTS(SELECT 1 FROM jsonb_each(coalesce(%(summary)s::jsonb->'full_inventory_sources','{}'::jsonb)) proof
 LEFT JOIN public.channel_video_inventory_scans s ON s.scan_id=(proof.value->>'scan_id')::uuid
 WHERE s.scan_id IS NULL OR s.channel_id IS DISTINCT FROM %(channel_id)s::text OR s.video_type IS DISTINCT FROM proof.key
 OR NOT coalesce(isfinite(s.started_at) AND s.started_at BETWEEN %(cutoff)s::timestamptz AND clock_timestamp()
 AND isfinite(s.scan_boundary_at) AND s.scan_boundary_at BETWEEN %(cutoff)s::timestamptz AND clock_timestamp(),false)) AS expired"""
_ACK="""SELECT (%s::jsonb->'inventory_ack'->>'scan_id')::uuid::text AS scan_id,
 (%s::jsonb->'inventory_ack'->>'transition_id')::uuid::text AS transition_id,
 %s::jsonb->'inventory_ack'->>'outcome' AS outcome"""
_ACK_SCAN='SELECT NOT '+_SCAN_HOT+' AS expired FROM public.channel_video_inventory_scans scan WHERE scan.scan_id=%(scan_id)s::uuid'
_ACK_RECEIPT="""SELECT NOT EXISTS(SELECT 1 FROM public.typed_inventory_receipts r
 WHERE r.transition_id=%(transition_id)s::uuid AND r.receipt_scope=CASE WHEN %(outcome)s::text='quota' THEN 'quota_defer' ELSE 'scan_transition' END
 AND NOT(r.result ? 'source_expired') AND isfinite(r.source_checked_at)
 AND r.source_checked_at BETWEEN %(cutoff)s::timestamptz AND clock_timestamp()) AS expired"""
_SOURCE_HOT="SELECT coalesce(isfinite(%(source_at)s::timestamptz) AND %(source_at)s::timestamptz BETWEEN %(cutoff)s::timestamptz AND clock_timestamp(),false) AS hot"
_SOURCE_STALE="SELECT %(source_at)s::timestamptz IS NULL OR NOT isfinite(%(source_at)s::timestamptz) OR %(source_at)s::timestamptz<%(cutoff)s::timestamptz OR %(source_at)s::timestamptz>clock_timestamp() AS stale"
_LEASE="SELECT %(has_lease)s::boolean AND %(lease_expires_at)s::timestamptz>clock_timestamp() AS busy"
_SCRUB_JOB="""UPDATE public.operator_jobs SET status=CASE WHEN status='completed' THEN status ELSE 'error' END,
 resume_page_token=NULL,inventory_scan_id=NULL,inventory_checkpoint='{}'::jsonb,
 summary_json=CASE WHEN %(stale)s::boolean THEN '{"source_scope_expired":true}'::jsonb ELSE
 (coalesce(summary_json,'{}'::jsonb)-'inventory_ack'-'full_inventory_sources'-'full_hydration_chunk'-'full_retry_after'-'full_retry_requested_at')||'{"inventory_checkpoint_expired":true}'::jsonb END,
 last_error_code=CASE WHEN %(stale)s::boolean THEN 'source_scope_expired' ELSE 'inventory_checkpoint_expired' END,last_error_summary=NULL,
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%(id)s::uuid AND (inventory_scan_id IS NOT NULL OR inventory_checkpoint<>'{}'::jsonb OR status NOT IN('completed','error') OR resume_page_token IS NOT NULL
 OR summary_json IS DISTINCT FROM '{"source_scope_expired":true}'::jsonb OR last_error_code IS DISTINCT FROM 'source_scope_expired'
 OR last_error_summary IS NOT NULL OR lease_token IS NOT NULL OR lease_channel_epoch IS NOT NULL OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL)"""
# Preserve the one-way chain marker only when it already exists on this parent.
_EXPIRED_SUMMARY="""CASE WHEN summary_json->'inventory_chain_version'='1'::jsonb
 THEN '{"source_scope_expired":true,"inventory_chain_version":1}'::jsonb
 ELSE '{"source_scope_expired":true}'::jsonb END"""
_SCRUB_JOB=_SCRUB_JOB.replace("""'{"source_scope_expired":true}'::jsonb""",_EXPIRED_SUMMARY)
_RUN="""SELECT to_jsonb(started_at)#>>'{}' AS source_at,started_at<%(cutoff)s::timestamptz AS expired,
 summary_json::text AS summary FROM public.sync_runs WHERE id=%(id)s::bigint"""
_QUOTA="""SELECT to_jsonb(started_at)#>>'{}' AS source_at,started_at<%(cutoff)s::timestamptz AS expired,
 phase,endpoint,quota_bucket,error_type FROM public.youtube_api_request_attempts WHERE request_attempt_id=%(id)s::uuid"""
_DELETE={
 'sync_runs':'DELETE FROM public.sync_runs WHERE id=%(id)s::bigint AND started_at<%(cutoff)s::timestamptz',
 'quota_attempts':'DELETE FROM public.youtube_api_request_attempts WHERE request_attempt_id=%(id)s::uuid AND started_at<%(cutoff)s::timestamptz',
}
_SCRUB_QUOTA="""UPDATE public.youtube_api_request_attempts SET phase=%(phase)s,endpoint=%(endpoint)s,quota_bucket=%(quota_bucket)s,error_type=%(error_type)s
 WHERE request_attempt_id=%(id)s::uuid AND (phase IS DISTINCT FROM %(phase)s::text OR endpoint IS DISTINCT FROM %(endpoint)s::text
 OR quota_bucket IS DISTINCT FROM %(quota_bucket)s::text OR error_type IS DISTINCT FROM %(error_type)s::text)"""
_TELEMETRY="""WITH clocks AS(SELECT * FROM jsonb_to_recordset(%s::jsonb)
 c(source_at timestamptz,reason text,clock_observed boolean,position integer)),
 facts AS(SELECT *,CASE WHEN reason='unfenced_target' THEN source_at
 WHEN isfinite(source_at) AND source_at<%s::timestamptz THEN source_at END AS old_clock,
 (reason='lease' AND NOT coalesce(isfinite(source_at) AND source_at<%s::timestamptz,false))
 OR (clock_observed AND (source_at IS NULL OR NOT isfinite(source_at) OR source_at>clock_timestamp())) AS invalid
 FROM clocks)
 SELECT to_jsonb(min(old_clock))#>>'{}' AS oldest,count(*) FILTER(WHERE invalid) AS invalid,
 coalesce(array_agg(position) FILTER(WHERE old_clock IS NOT NULL OR invalid),ARRAY[]::integer[]) AS unsafe_positions FROM facts"""


def _reject(code):raise DatabaseServerError(code,code='P0001')


def _cursor(tx,target,value):
    try:
        with tx.savepoint():
            if target=='operator_job_targets':
                if value is None:return None,None
                row=tx.fetch_one("""WITH decoded AS(SELECT %s::jsonb AS value)
                 SELECT CASE WHEN jsonb_typeof(value)='array' THEN jsonb_array_length(value)=2
                 AND jsonb_typeof(value->0)='string' AND jsonb_typeof(value->1) IN ('string','null') ELSE false END AS valid,
                 value->>0 AS job,value->>1 AS video FROM decoded""",(value,))
                if row['valid'] is not True:_reject('hot_operational_cursor_invalid')
                # [job, null]: every target of that job is behind the cursor.
                return tx.fetch_one('SELECT %s::uuid::text AS value',(row['job'],))['value'],row['video']
            cast='bigint' if target=='sync_runs' else 'uuid'
            if target in ORDERED:
                if value is None:return None,None
                row=tx.fetch_one("""WITH decoded AS(SELECT %s::jsonb AS value)
                 SELECT CASE WHEN jsonb_typeof(value)='array' THEN jsonb_array_length(value)=2
                 AND jsonb_typeof(value->0)='string' AND jsonb_typeof(value->1)='string' ELSE false END AS valid,
                 value->>0 AS clock,value->>1 AS id FROM decoded""",(value,))
                if row['valid'] is not True:_reject('hot_operational_cursor_invalid')
                exact=tx.fetch_one("SELECT to_jsonb(%s::timestamptz)#>>'{}' AS clock,%s::"+cast+"::text AS id",(row['clock'],row['id']))
                return exact['clock'],exact['id']
            tx.fetch_one('SELECT %s::'+cast+' AS value',(value,))
            return None,None
    except DatabaseServerError as error:
        if not isinstance(error.code,str) or not error.code.startswith('22'):raise
        _reject('hot_operational_cursor_invalid')


def _job_expiry(tx,job,p):
    params={**p,**job}
    linked=False
    ack=False
    if job['inventory_scan_id'] is not None:
        row=tx.fetch_one(_LINKED,{**params,'scan_id':job['inventory_scan_id']})
        linked=row is None or row['expired']
    if job['initial_scope_version']==1 and not linked:
        linked=tx.fetch_one(_FULL_SOURCES,params)['expired']
    if tx.fetch_one("SELECT %s::jsonb ? 'inventory_ack' AS present",(job['summary'],))['present'] is True:
        values=tx.fetch_one(_ACK,(job['summary'],)*3)
        if values['scan_id'] is not None:
            row=tx.fetch_one(_ACK_SCAN,{**params,**values})
            ack=row is None or row['expired']
        if values['outcome'] in ('page','playlist_not_found','error','quota') and not ack:
            ack=tx.fetch_one(_ACK_RECEIPT,{**params,**values})['expired']
    if chains.marked(job):
        facts=job.get('_chain_facts')
        if facts is None:
            facts=chains.inspect_copies(tx,job,p)
            job['_chain_facts']=facts
        linked=linked or facts['linked']
        ack=ack or bool(facts['clear_ack_types'])
    return linked,ack


def _jobs(tx,target,p,step):
    job=tx.fetch_one(_JOB,p)
    if job is None:return 0,0,0
    step.update(source_at=job['source_at'],clock_observed=True)
    linked,ack=_job_expiry(tx,job,p)
    if tx.fetch_one(_SOURCE_HOT,{**p,**job})['hot'] and not linked and not ack:return 0,0,0
    tx.fetch_all('SELECT channel_id FROM public.channels WHERE channel_id=%s FOR UPDATE NOWAIT',(job['channel_id'],),max_rows=1)
    job=tx.fetch_one(_JOB+' FOR UPDATE NOWAIT',p)
    if job is None:return 0,0,0
    step.update(source_at=job['source_at'],clock_observed=True)
    if chains.marked(job):
        job['_chain_facts']=chains.inspect_copies(tx,job,p,lock=True)
    stale=tx.fetch_one(_SOURCE_STALE,{**p,**job})['stale']
    linked,ack=_job_expiry(tx,job,p)
    if not stale and not linked:
        if target=='operator_jobs' and ack:
            cleared=chains.clear_historical_acks(tx,p,job['_chain_facts']) if '_chain_facts' in job else 0
            return 0,cleared+tx.execute("UPDATE public.operator_jobs SET summary_json=summary_json-'inventory_ack' WHERE id=%(id)s::uuid AND summary_json ? 'inventory_ack'",p),0
        return 0,0,0
    if tx.fetch_one(_LEASE,job)['busy'] is True or job.get('_chain_facts',{}).get('busy') is True:
        step['reason']='lease'
        return 0,0,1
    if target=='operator_jobs':
        scrubbed=chains.scrub_copies(tx,p) if '_chain_facts' in job else 0
        return 0,scrubbed+tx.execute(_SCRUB_JOB,{**p,'stale':stale}),0
    if job['last_error_code']!='source_scope_expired':
        step['reason']='unfenced_target'
        return 0,0,1
    tx.fetch_all('SELECT job_id FROM public.operator_job_targets WHERE job_id=%(id)s::uuid AND video_id=%(video)s FOR UPDATE NOWAIT',p,max_rows=1)
    return tx.execute('DELETE FROM public.operator_job_targets WHERE job_id=%(id)s::uuid AND video_id=%(video)s',p),0,0


# A target's decision reads only its job (see _jobs), so the targets of a job that
# needs no work are skipped whole after one check of the job, instead of once per
# target: one large channel's initial load leaves 20,000 targets for 29 days. Jobs
# are walked in key order; at most JOBS_PER_PAGE jobs are checked per page.
JOBS_PER_PAGE=50
_NEXT_TARGET_JOB="""SELECT job_id::text AS id FROM public.operator_job_targets
 WHERE %(after)s::uuid IS NULL OR job_id>%(after)s::uuid ORDER BY job_id LIMIT 1"""
_JOB_TARGETS="""SELECT job_id::text AS id,video_id AS video,jsonb_build_array(job_id::text,video_id)::text AS cursor
 FROM public.operator_job_targets WHERE job_id=%(job)s::uuid AND (%(after_video)s::text IS NULL OR video_id>%(after_video)s::text)
 ORDER BY video_id LIMIT %(limit)s::integer"""


def _job_needs_work(tx,job_id,cutoff):
    """The first, lock-free check of _jobs: False means every target is a no-op."""
    p={'id':job_id,'cutoff':cutoff}
    job=tx.fetch_one(_JOB,p)
    if job is None:return False
    linked,ack=_job_expiry(tx,job,p)
    return not(tx.fetch_one(_SOURCE_HOT,{**p,**job})['hot'] and not linked and not ack)


def _target_page(tx,cutoff,after_job,after_video,limit):
    """Targets of jobs that need work, in key order; returns rows, next cursor and completeness."""
    import json
    rows=[]
    job=after_job
    if job is not None and after_video is not None:
        rows=tx.fetch_all(_JOB_TARGETS,{'job':job,'after_video':after_video,'limit':limit},max_rows=limit)
        if len(rows)==limit:return rows,rows[-1]['cursor'],False
    for _ in range(JOBS_PER_PAGE):
        found=tx.fetch_one(_NEXT_TARGET_JOB,{'after':job})
        if found is None:return rows,None,True
        job=found['id']
        if not _job_needs_work(tx,job,cutoff):continue
        rows+=tx.fetch_all(_JOB_TARGETS,{'job':job,'after_video':None,'limit':limit-len(rows)},max_rows=limit-len(rows))
        if len(rows)==limit:return rows,rows[-1]['cursor'],False
    # Checked enough jobs for one page; the next page starts after this job.
    return rows,json.dumps([job,None],separators=(',',':')),False


def _run_clean(tx,raw):
    projected=persisted_summary_text(raw)
    same=tx.fetch_one('SELECT %s::jsonb IS NOT DISTINCT FROM %s::jsonb AS same',(raw,projected))['same']
    return projected,same


def _copies(tx,target,p,step):
    query=_RUN if target=='sync_runs' else _QUOTA
    row=tx.fetch_one(query,p)
    if row is None:return 0,0,0
    step.update(source_at=row['source_at'],clock_observed=True)
    if row['expired'] is True:
        if target=='quota_attempts':
            return delete_expired_quota_attempt(tx,attempt_id=p['id'],cutoff=p['cutoff']),0,0
        return tx.execute(_DELETE[target],p),0,0
    if target=='sync_runs':
        _,same=_run_clean(tx,row['summary'])
        if same:return 0,0,0
    elif all(row[name]==quota_label(name,row[name]) for name in ('phase','endpoint','quota_bucket','error_type')):return 0,0,0
    if target=='sync_runs':
        # Preserve the initial expiry decision; the shared owner rechecks the
        # locked summary with NOWAIT and performs normalization before writing.
        return 0,scrub_sync_run(tx,run_id=p['id']),0
    row=tx.fetch_one(query+' FOR UPDATE NOWAIT',p)
    if row is None:return 0,0,0
    cleaned={name:quota_label(name,row[name]) for name in ('phase','endpoint','quota_bucket','error_type')}
    return 0,tx.execute(_SCRUB_QUOTA,{**p,**cleaned}),0


_EXACT_PAGE={
 'operator_jobs':"SELECT j.id::text AS id,NULL::text AS video,NULL::text AS cursor FROM jsonb_to_recordset(%(references)s::jsonb) k(id uuid,video text) JOIN public.operator_jobs j ON j.id=k.id ORDER BY j.id",
 'operator_job_targets':"SELECT t.job_id::text AS id,t.video_id AS video,NULL::text AS cursor FROM jsonb_to_recordset(%(references)s::jsonb) k(id uuid,video text) JOIN public.operator_job_targets t ON t.job_id=k.id AND t.video_id=k.video ORDER BY t.job_id,t.video_id",
 'sync_runs':"SELECT r.id::text AS id,NULL::text AS video,NULL::text AS cursor FROM jsonb_to_recordset(%(references)s::jsonb) k(id bigint,video text) JOIN public.sync_runs r ON r.id=k.id ORDER BY r.id",
 'quota_attempts':"SELECT r.request_attempt_id::text AS id,NULL::text AS video,NULL::text AS cursor FROM jsonb_to_recordset(%(references)s::jsonb) k(id uuid,video text) JOIN public.youtube_api_request_attempts r ON r.request_attempt_id=k.id ORDER BY r.request_attempt_id",
}


def cleanup_hot_operational_copies(transaction,*,target,after_id=None,limit=100,references=None):
    tx=transaction
    with tx.borrow():
        header=tx.fetch_one(_HEADER,(target,after_id,limit))
        target=header['target']
        limit=header['limit']
        if limit is None or not 1<=limit<=500:_reject('hot_operational_limit_invalid')
        if header['cursor'] is not None and not 1<=header['cursor_length']<=2048:_reject('hot_operational_cursor_invalid')
        if target not in TARGETS:_reject('hot_operational_target_invalid')
        first,second=_cursor(tx,target,header['cursor'])
        if references is not None:
            values=Jsonb([dict(id=key[0],video=key[1] if len(key)>1 else None) for key in references])
            rows=tx.fetch_all(_EXACT_PAGE[target],dict(references=values),max_rows=len(references))
            target_cursor,target_complete=None,True
        elif target=='operator_job_targets':
            rows,target_cursor,target_complete=_target_page(tx,header['cutoff'],first,second,limit)
        else:
            rows=tx.fetch_all(_PAGE[target],dict(after=header['cursor'],after_job=first,after_video=second,after_clock=first,after_id=second,
                cutoff=header['cutoff'],at=header['at'],limit=limit),max_rows=limit)
        deleted=scrubbed=deferred=0
        clocks=[]
        unresolved=[]
        for candidate in rows:
            step={'source_at':None,'reason':None,'clock_observed':False}
            try:
                with tx.savepoint():
                    p={**candidate,'cutoff':header['cutoff']}
                    removed,cleared,held=(_jobs(tx,target,p,step) if target in ('operator_jobs','operator_job_targets') else _copies(tx,target,p,step))
                deleted+=removed
                scrubbed+=cleared
                deferred+=held
            except DatabaseServerError as error:
                if error.code!='55P03':raise
                held=1
                deferred+=1
                step['reason']='lock'
            if held:
                step['position']=len(clocks)
                clocks.append(step)
                unresolved.append(dict(target=target,key=[candidate['id'],candidate['video']] if target=='operator_job_targets' else [candidate['id']]))
        telemetry=tx.fetch_one(_TELEMETRY,(Jsonb(clocks),header['cutoff'],header['cutoff'])) if clocks else {'oldest':None,'invalid':0,'unsafe_positions':[]}
        if references is None:
            unsafe=set(telemetry['unsafe_positions'])
            unresolved=[ref for position,ref in enumerate(unresolved) if position in unsafe]
        if target=='operator_job_targets':
            complete,next_cursor=target_complete,target_cursor
        else:
            complete=references is not None or len(rows)<limit
            next_cursor=None if complete else rows[-1]['cursor']
        return dict(target=target,as_of=header['at'],cutoff=header['cutoff'],scanned=len(rows),deleted=deleted,scrubbed=scrubbed,deferred=deferred,
                    next_cursor=next_cursor,complete=complete,telemetry_scope='scanned_page',
                    oldest_remaining_expired_source_at=telemetry['oldest'],invalid_clock_remaining=telemetry['invalid'],
                    # A row locked before its clock was read has an unknown age.
                    unknown_expiry_remaining=sum(not step['clock_observed'] for step in clocks),
                    priority_complete=len(unresolved)<=100,priority_references=unresolved[:100])
