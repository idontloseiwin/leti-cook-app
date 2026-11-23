"""Public initial-inventory chunk planning used by the operator repository.

Native clocks/counts/JSONB and the original channel->job->child lock order stay
inside one owned transaction. Recorded receipts and prepared commands reconcile
without another provider dispatch. Fresh playlist INSERT requires the reviewed
inventory_dispatch_seq USAGE grant, including its ON CONFLICT path. Existing
source-scope guards remain enabled; control policy uses shared native
projections. No retained business RPC is called.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scope
from ..channel.channel_metadata_batch import _RECORD_ERROR
from ..base.operation_contracts import get_operation
from .operator_job_inventory_evidence import (initial_inventory_checkpoint_evidence,restart_initial_inventory_discovery,
                                            _NativeObject,_encoded)
from .operator_job_reconciliation import reconcile_full_initial_targets
from .operator_job_scope import assert_full_initial_scope
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token,
 %s::integer AS page_limit,%s::integer AS lease_seconds"""
_CHANNEL_ID="SELECT channel_id FROM public.operator_jobs WHERE id=%s::uuid AND job_type='channel_initial_long_inventory'"
_CHANNEL="SELECT channel_id FROM public.channels WHERE channel_id=%s FOR UPDATE"
_JOB="""SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j WHERE id=%(job_id)s::uuid
 AND channel_id=%(channel_id)s AND job_type='channel_initial_long_inventory' FOR UPDATE"""
_RELOAD="SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j WHERE id=%s::uuid"
_LEASE=("""SELECT j.lease_channel_epoch AS epoch,j.accepted_epoch AS accepted_epoch,
 j.status<>'running' OR j.lease_token IS DISTINCT FROM %(lease_token)s::uuid
 OR j.lease_expires_at IS NULL OR j.lease_expires_at<=now()
 OR coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR (j.phase='inventory' AND j.summary_json ? 'inventory_chain_version')
 OR j.phase NOT IN('inventory','initial_stats')
 OR NOT coalesce((SELECT c.typed_runtime_eligible
 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ c WHERE c.channel_id=j.channel_id),false) AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j""")
_ALL_DONE="""SELECT j.inventory_checkpoint='{}'::jsonb AND (SELECT count(*)=3
 AND coalesce(bool_and(coalesce(value->>'terminal_outcome' IN('completed','playlist_not_found'),false)),false)
 FROM jsonb_each(j.summary_json->'full_inventory_sources')) AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_ACTION="""SELECT CASE WHEN %(evidence)s::jsonb->>'kind'='page'
 OR (%(evidence)s::jsonb->>'kind'='failure' AND %(evidence)s::jsonb->>'outcome'='playlist_not_found'
 AND (%(evidence)s::jsonb->>'format_absent')::boolean) THEN 'reconcile_inventory'
 WHEN %(evidence)s::jsonb->>'kind' IN('failure','quota') THEN 'reconcile_failure'
 WHEN (%(evidence)s::jsonb->>'bound_live')::boolean THEN 'await_receipt' ELSE 'reconcile_unknown' END AS value"""
_REFUND="""UPDATE public.operator_jobs j SET quota_units_reserved=greatest(quota_units_reserved-
 (%s::jsonb->>'reserved_units')::integer,0),inventory_checkpoint='{}'::jsonb WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
_CAP="""SELECT j.initial_scope_version=0 AND NOT (%(evidence)s::jsonb->>'format_finished')::boolean
 AND (j.pages_processed>=j.max_pages OR CASE WHEN cardinality(j.initial_video_types)=3
 THEN j.items_processed>j.max_items ELSE j.items_processed>=j.max_items END) AS exhausted,
 cardinality(j.initial_video_types)=1 OR j.inventory_video_type='long' AS record_error
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_BLOCK="""UPDATE public.operator_jobs j SET status='blocked',finished_at=now(),updated_at=now(),
 last_error_code='initial_inventory_cap_exhausted',
 last_error_summary='Job stopped before another playlist request because a confirmed cap was exhausted.',
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
_PLAYLIST_INSERT="""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,
 inventory_status,product_exposure_ready,runtime_enabled,status,next_due_at)
 SELECT j.channel_id,j.inventory_video_type,(CASE j.inventory_video_type WHEN 'long' THEN 'UULF' WHEN 'short' THEN 'UUSH' ELSE 'UULV' END)
 ||substr(j.channel_id,3),'pending',false,true,'ready',clock_timestamp()
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j ON CONFLICT(channel_id,video_type) DO NOTHING"""
_WAKE="""UPDATE public.channel_playlist_state state SET runtime_enabled=true,status='ready',
 next_due_at=coalesce(state.next_due_at,clock_timestamp()),updated_at=clock_timestamp()
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE state.channel_id=j.channel_id AND state.video_type=j.inventory_video_type AND state.status='paused'
 AND NOT(state.lease_token IS NOT NULL AND state.lease_expires_at>clock_timestamp())"""
_RESERVE="""UPDATE public.operator_jobs j SET quota_units_reserved=quota_units_reserved+%(reserved)s::integer,
 inventory_scan_id=CASE WHEN %(reserved)s::integer=0 THEN (%(evidence)s::jsonb->>'scan_id')::uuid ELSE inventory_scan_id END,
 inventory_checkpoint=jsonb_build_object('phase','prepared','job_lease_token',%(lease_token)s::uuid,
 'transition_id',%(transition)s::uuid,'reserved_units',%(reserved)s::integer),
 heartbeat_at=now(),updated_at=now(),lease_expires_at=now()+make_interval(secs=>%(lease_seconds)s::integer)
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_RECENT="""SELECT count(*)::integer AS value FROM public.video_inventory
 WHERE channel_id=%s AND video_type='long' AND published_at>=now()-interval '28 days'"""
_STATS_RESERVED="SELECT CASE WHEN %s::integer>150 THEN 0 ELSE 2*((%s::integer+49)/50) END AS value"
_RESERVE_STATS="""UPDATE public.operator_jobs j SET quota_units_reserved=quota_units_reserved+%(reserved)s::integer,
 heartbeat_at=now(),updated_at=now(),lease_expires_at=now()+make_interval(secs=>%(lease_seconds)s::integer)
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_CHECKPOINT_RETURN="""SELECT jsonb_build_object('action',%(action)s::text,'job',to_jsonb(j),'video_type',j.inventory_video_type,
 'page_limit',CASE WHEN %(action)s='inventory' THEN (%(checkpoint)s::jsonb->>'reserved_units')::integer ELSE 0 END,
 'reserved_units',CASE WHEN %(waiting_discovery)s::boolean THEN coalesce((%(checkpoint)s::jsonb->>'reserved_units')::integer,0)
 ELSE (%(checkpoint)s::jsonb->>'reserved_units')::integer END,
 'transition_id',%(checkpoint)s::jsonb->'transition_id','scan_id',j.inventory_scan_id,'resume_page_token',NULL)
 ||CASE WHEN %(with_evidence)s::boolean THEN jsonb_build_object('evidence',%(evidence)s::jsonb) ELSE '{}'::jsonb END
 ||CASE WHEN %(waiting_discovery)s::boolean THEN jsonb_build_object('reason','initial_inventory_discovery_in_progress') ELSE '{}'::jsonb END AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""


def _fail(code):raise DatabaseServerError(code,code='P0001')
def _value(row):return _NativeObject(row['value']) if row is not None else None


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('prepare_initial_inventory_job_chunk')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:
            yield tx


def _fence(tx,channel_id,captured,*,lease_check=True):
    if lease_check and (captured['epoch'] is None or captured['accepted_epoch']!=captured['epoch']):_fail('operator_job_lease_lost')
    assert_channel_scope(tx,channel_id,captured['epoch'])


def _record_cap_error(tx,channel_id):
    if channel_id is None or not channel_id.strip(' '):_fail('channel_sync_channel_required')
    if tx.returning_one(_RECORD_ERROR,('initial_inventory_cap_exhausted',channel_id)) is None:_fail('channel_sync_channel_not_found')


def _checkpoint_result(tx,job,checkpoint,evidence,action,*,waiting_discovery=False,with_evidence=False):
    return tx.fetch_one(_CHECKPOINT_RETURN,dict(job=_encoded(job),checkpoint=Jsonb(checkpoint),evidence=_encoded(evidence),
        action=action,waiting_discovery=waiting_discovery,with_evidence=with_evidence))['value']


def prepare_initial_inventory_job_chunk(database,job_id,lease_token,page_limit=5,lease_seconds=1200,*,transaction=None):
    """Plan or reconcile one bounded chunk; provider work begins only after COMMIT."""
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(job_id,lease_token,page_limit,lease_seconds))
        if args['page_limit'] is None or not 1<=args['page_limit']<=10:_fail('operator_job_inventory_page_limit_invalid')
        if args['lease_seconds'] is None or not 60<=args['lease_seconds']<=3600:_fail('operator_job_lease_seconds_invalid')
        channel=tx.fetch_one(_CHANNEL_ID,(args['job_id'],))
        if channel is None:_fail('operator_job_lease_lost')
        args['channel_id']=channel['channel_id']
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail('operator_job_lease_lost')
        job=_value(tx.fetch_one(_JOB,args))
        if job is None:_fail('operator_job_lease_lost')
        args['job']=_encoded(job)
        captured=tx.fetch_one(_LEASE,args)
        if captured['invalid'] is True:_fail('operator_job_lease_lost')
        if job['initial_scope_version']==1:
            assert_full_initial_scope(tx,_encoded(job),False)
            if tx.fetch_one(_ALL_DONE,(_encoded(job),))['value'] is True:
                job=reconcile_full_initial_targets(tx,job['id'],250)
                _fence(tx,job['channel_id'],captured,lease_check=False)
                return dict(action='targets_reconciled',reason='target_types',job=job,page_limit=0,reserved_units=0,transition_id=None)
        if job['phase']=='inventory':
            evidence=initial_inventory_checkpoint_evidence(tx,_encoded(job))
            checkpoint=job['inventory_checkpoint']
            if job['initial_scope_version']==0 and checkpoint.get('phase')!='bound':
                if not restart_initial_inventory_discovery(tx,_encoded(job),evidence.get('format_finished') or False):
                    _fence(tx,job['channel_id'],captured)
                    return _checkpoint_result(tx,job,checkpoint,evidence,'await_receipt',waiting_discovery=True,with_evidence=True)
                job=_value(tx.fetch_one(_RELOAD,(job['id'],)))
            if checkpoint!={}:
                if checkpoint.get('phase')=='bound':
                    action=tx.fetch_one(_ACTION,dict(evidence=_encoded(evidence)))['value']
                    _fence(tx,job['channel_id'],captured)
                    return _checkpoint_result(tx,job,checkpoint,evidence,action,with_evidence=True)
                if checkpoint.get('job_lease_token')==args['lease_token']:
                    _fence(tx,job['channel_id'],captured)
                    return _checkpoint_result(tx,job,checkpoint,evidence,'inventory')
                job=_value(tx.returning_one(_REFUND,(Jsonb(checkpoint),job['id'])))
            args['job']=_encoded(job)
            args['evidence']=_encoded(evidence)
            cap=tx.fetch_one(_CAP,args)
            if cap['exhausted'] is True:
                job=_value(tx.returning_one(_BLOCK,(job['id'],)))
                if cap['record_error'] is True:_record_cap_error(tx,job['channel_id'])
                _fence(tx,job['channel_id'],captured)
                return dict(action='blocked',job=job)
            args['reserved']=0 if evidence.get('format_finished') is True else 1
            if args['reserved']==1:
                tx.execute(_PLAYLIST_INSERT,(_encoded(job),))
                tx.execute(_WAKE,(_encoded(job),))
            args['transition']=tx.fetch_one('SELECT gen_random_uuid()::text AS value')['value']
            job=_value(tx.returning_one(_RESERVE,args))
            _fence(tx,job['channel_id'],captured)
            return dict(action='inventory',job=job,video_type=job['inventory_video_type'],
                scope='all' if len(job['initial_video_types'])==3 else 'long',page_limit=args['reserved'],reserved_units=args['reserved'],
                transition_id=args['transition'],scan_id=job['inventory_scan_id'],resume_page_token=None)
        recent=tx.fetch_one(_RECENT,(job['channel_id'],))['value']
        args['reserved']=tx.fetch_one(_STATS_RESERVED,(recent,recent))['value']
        job=_value(tx.returning_one(_RESERVE_STATS,args))
        _fence(tx,job['channel_id'],captured)
        return dict(action='initial_stats',job=job,page_limit=0,reserved_units=args['reserved'])
