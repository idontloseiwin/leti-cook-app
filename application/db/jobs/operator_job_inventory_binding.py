"""Initial-inventory binding used by the direct operator-job repository.

A recorded exact binding is an acknowledgement even after its old owner
expires; it never authorizes another provider dispatch. Fresh binding owns
channel, job, playlist, then scan locks and fences its captured channel
generation last. Native JSONB/composite comparisons preserve clock domains and
checkpoint equivalence. Shared channel-control projections and intrinsic
source-scope guards remain authoritative.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from .operator_job_scope import assert_full_initial_scope
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""SELECT to_jsonb(args) AS value FROM(SELECT %s::uuid AS job_id,
 %s::uuid AS job_lease_token,%s::uuid AS playlist_lease_token,%s::uuid AS scan_id,%s::uuid AS transition_id) args"""
_REPLAY="""SELECT EXISTS(SELECT 1 FROM public.operator_jobs j WHERE j.id=%(job_id)s::uuid
 AND j.job_type='channel_initial_long_inventory' AND j.inventory_scan_id=%(scan_id)s::uuid
 AND j.inventory_checkpoint=jsonb_build_object('phase','bound','job_lease_token',%(job_lease_token)s::uuid,
 'transition_id',%(transition_id)s::uuid,'reserved_units',1,'playlist_lease_token',%(playlist_lease_token)s::uuid)) AS replayed"""
_CHANNEL_ID="SELECT channel_id FROM public.operator_jobs WHERE id=%s::uuid AND job_type='channel_initial_long_inventory'"
_CHANNEL="SELECT channel_id FROM public.channels WHERE channel_id=%s::text FOR UPDATE"
_JOB="""SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j
 WHERE j.id=%(job_id)s::uuid AND j.channel_id=%(channel_id)s::text
 AND j.job_type='channel_initial_long_inventory' FOR UPDATE"""
_JOB_CHECK=("""SELECT j.channel_id,j.lease_channel_epoch AS epoch,j.initial_scope_version,
 j.status<>'running' OR j.lease_token IS DISTINCT FROM %(job_lease_token)s::uuid
 OR j.lease_expires_at IS NULL OR j.lease_expires_at<=clock_timestamp()
 OR coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR NOT coalesce((SELECT c.typed_runtime_eligible
   FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ c WHERE c.channel_id=j.channel_id),false)
 OR (j.phase='inventory' AND j.summary_json ? 'inventory_chain_version')
 OR j.lease_channel_epoch IS NULL OR j.accepted_epoch IS DISTINCT FROM j.lease_channel_epoch AS lease_invalid,
 j.phase<>'inventory' OR j.inventory_checkpoint IS DISTINCT FROM jsonb_build_object('phase','prepared',
 'job_lease_token',%(job_lease_token)s::uuid,'transition_id',%(transition_id)s::uuid,'reserved_units',1) AS checkpoint_invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j""")
_PLAYLIST="""SELECT to_jsonb(state)::text AS value FROM public.channel_playlist_state state
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE state.channel_id=j.channel_id AND state.video_type=j.inventory_video_type FOR UPDATE OF state"""
_PLAYLIST_CHECK="""SELECT state.status<>'leased' OR state.lease_token IS DISTINCT FROM %(playlist_lease_token)s::uuid
 OR state.lease_expires_at<=clock_timestamp() OR state.lease_channel_epoch IS DISTINCT FROM %(epoch)s::bigint
 OR state.active_scan_id IS DISTINCT FROM %(scan_id)s::uuid AS invalid
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) state"""
_SCAN="SELECT to_jsonb(scan)::text AS value FROM public.channel_video_inventory_scans scan WHERE scan_id=%s::uuid FOR UPDATE"
_SCAN_CHECK="""SELECT scan.channel_id IS DISTINCT FROM j.channel_id OR scan.video_type IS DISTINCT FROM j.inventory_video_type
 OR scan.playlist_id IS DISTINCT FROM state.playlist_id OR scan.status<>'running' OR scan.coverage<>'partial'
 OR NOT scan.token_history_verified AS identity_invalid,
 scan.scan_kind NOT IN('initial_inventory','full_reconciliation') AS kind_invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) scan
 CROSS JOIN jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) state
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_FULL_CHECK="""SELECT j.summary_json->'full_inventory_sources' ? j.inventory_video_type AS has_source,
 CASE WHEN j.summary_json->'full_inventory_sources' ? j.inventory_video_type THEN
 j.summary_json->'full_inventory_sources'->j.inventory_video_type->>'scan_id' IS DISTINCT FROM %(scan_id)s::uuid::text
 OR scan.pages_completed IS DISTINCT FROM (j.summary_json->'full_inventory_sources'->j.inventory_video_type->>'acked_pages')::integer
 ELSE scan.pages_completed<>0 OR scan.resume_page_token IS NOT NULL OR scan.started_at<j.requested_at
 OR scan.scan_boundary_at<j.requested_at END AS invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) scan
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_REGISTER_SOURCE="""UPDATE public.operator_jobs target SET summary_json=jsonb_set(target.summary_json,
 ARRAY['full_inventory_sources',j.inventory_video_type],jsonb_build_object('scan_id',scan.scan_id,
 'started_at',scan.started_at,'scan_boundary_at',scan.scan_boundary_at,'acked_pages',0,
 'last_transition_id',NULL,'terminal_outcome',NULL,'source_checked_at',NULL))
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) scan
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 WHERE target.id=%(job_id)s::uuid"""
_BIND="""UPDATE public.operator_jobs SET inventory_scan_id=%(scan_id)s::uuid,
 inventory_checkpoint=inventory_checkpoint||jsonb_build_object('phase','bound','playlist_lease_token',%(playlist_lease_token)s::uuid),
 updated_at=now(),heartbeat_at=now() WHERE id=%(job_id)s::uuid"""


def _fail(code):raise DatabaseServerError(code,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('bind_initial_inventory_scan')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:
            yield tx


def bind_initial_inventory_scan(database,job_id,job_lease_token,playlist_lease_token,scan_id,transition_id,*,transaction=None):
    """Bind one prepared page; return dispatch permission only after known COMMIT."""
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(job_id,job_lease_token,playlist_lease_token,scan_id,transition_id))['value']
        if any(value is None for value in args.values()):_fail('initial_inventory_bind_identity_invalid')
        result=dict(status='bound',job_id=args['job_id'],scan_id=args['scan_id'],transition_id=args['transition_id'])
        if tx.fetch_one(_REPLAY,args)['replayed'] is True:
            return dict(result,replayed=True,dispatch_allowed=False)
        channel=tx.fetch_one(_CHANNEL_ID,(args['job_id'],))
        if channel is None:_fail('operator_job_lease_lost')
        args['channel_id']=channel['channel_id']
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail('operator_job_lease_lost')
        row=tx.fetch_one(_JOB,args)
        if row is None:_fail('operator_job_lease_lost')
        args['job']=row['value']
        # Native clocks are evaluated after each blocking lock has returned.
        held=tx.fetch_one(_JOB_CHECK,args)
        if held['lease_invalid'] is True:_fail('operator_job_lease_lost')
        if held['checkpoint_invalid'] is True:_fail('initial_inventory_bind_conflict')
        args['epoch']=held['epoch']
        row=tx.fetch_one(_PLAYLIST,(args['job'],))
        if row is None:_fail('initial_inventory_playlist_lease_lost')
        args['playlist']=row['value']
        if tx.fetch_one(_PLAYLIST_CHECK,args)['invalid'] is True:_fail('initial_inventory_playlist_lease_lost')
        row=tx.fetch_one(_SCAN,(args['scan_id'],))
        if row is None:_fail('initial_inventory_scan_identity_invalid')
        args['scan']=row['value']
        scan=tx.fetch_one(_SCAN_CHECK,args)
        if scan['identity_invalid'] is True:_fail('initial_inventory_scan_identity_invalid')
        if scan['kind_invalid'] is True:_fail('initial_inventory_scan_kind_invalid')
        if held['initial_scope_version']==1:
            assert_full_initial_scope(tx,args['job'],False)
            full=tx.fetch_one(_FULL_CHECK,args)
            if full['invalid'] is True:
                _fail('full_initial_source_coverage_invalid' if full['has_source'] is True else 'full_initial_fresh_generation_required')
            if full['has_source'] is not True:tx.execute(_REGISTER_SOURCE,args)
        tx.execute(_BIND,args)
        assert_channel_scope(tx,args['channel_id'],args['epoch'])
        return dict(result,replayed=False,dispatch_allowed=True)
