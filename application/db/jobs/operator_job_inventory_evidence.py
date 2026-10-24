"""Borrowed checkpoint evidence and legacy discovery restart.

The caller owns channel then job and its final captured control fence. These
protocols lock one playlist then one scan; receipt reads retain their separate
statement snapshots. Native JSONB, integer casts and wall clocks are
deliberate. No stored business helper, second transaction, or provider dispatch
is used.
"""
import json
from psycopg.types.json import Jsonb
from ..inventory.inventory_scan_steps import _hot
from .operator_job_scope import assert_full_initial_scope
from ..base.postgres import DatabaseServerError

_INPUT="""SELECT to_jsonb(j)::text AS job,j.initial_scope_version AS version,
 j.inventory_checkpoint->>'phase' AS phase FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_PLAYLIST="""SELECT to_jsonb(d)::text AS value FROM public.channel_playlist_state d
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE d.channel_id=j.channel_id AND d.video_type=j.inventory_video_type FOR UPDATE OF d"""
_SCAN_KEY="""SELECT to_jsonb(CASE WHEN j.initial_scope_version=1 THEN j.inventory_scan_id
 ELSE coalesce(j.inventory_scan_id,state.last_completed_scan_id,state.active_scan_id) END)#>>'{}' AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 CROSS JOIN jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) state"""
_SCAN="SELECT to_jsonb(s)::text AS value FROM public.channel_video_inventory_scans s WHERE scan_id=%s::uuid FOR UPDATE"
_SCAN_IDENTITY="""SELECT s.channel_id IS DISTINCT FROM j.channel_id OR s.video_type IS DISTINCT FROM j.inventory_video_type
 OR s.playlist_id IS DISTINCT FROM state.playlist_id AS invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 CROSS JOIN jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) state"""
_FULL_READY="""SELECT s.status='completed' AND s.coverage='complete' AND s.scan_kind IN('initial_inventory','full_reconciliation')
 AND isfinite(s.started_at) AND s.started_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND isfinite(s.scan_boundary_at) AND s.scan_boundary_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND isfinite(s.source_checked_at) AND isfinite(s.completed_at)
 AND s.source_checked_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND s.completed_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp() AS value
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%s::jsonb) s"""
_BASE="""WITH facts AS MATERIALIZED(SELECT state.status,state.lease_expires_at,state.next_due_at,
 j.inventory_checkpoint->>'phase'='bound' AND j.summary_json->'full_inventory_sources'->j.inventory_video_type->>'scan_id'
 IS DISTINCT FROM %(scan_id)s::uuid::text AS source_invalid,
 state.inventory_status='playlist_not_found' AND isfinite(state.source_checked_at)
 AND state.source_checked_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp() AS absent,
 state.status='leased' AND state.lease_token::text=j.inventory_checkpoint->>'playlist_lease_token'
 AND state.lease_expires_at>clock_timestamp() AS live
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 CROSS JOIN jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) state)
 SELECT jsonb_build_object('kind','none','scan_id',%(scan_id)s::uuid,
 'format_finished',coalesce(%(full_ready)s::boolean,false) OR coalesce(absent,false),
 'format_absent',coalesce(absent,false),'bound_live',coalesce(live,false),
 'retry_not_before',CASE WHEN status='leased' THEN lease_expires_at ELSE next_due_at END) AS value,source_invalid
 FROM facts"""
_TRANSITION="SELECT to_jsonb((%s::jsonb->'inventory_checkpoint'->>'transition_id')::uuid)#>>'{}' AS value"
_RECEIPT="SELECT to_jsonb(r)::text AS value FROM public.typed_inventory_receipts r WHERE receipt_scope=%s AND transition_id=%s::uuid"
_HEADER="""SELECT r.result ? 'source_expired' AS expired,to_jsonb(r.source_checked_at)#>>'{}' AS source_checked_at,
 r.transition_kind AS kind FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%s::jsonb) r"""
_QUOTA_CHECK="""SELECT r.claims IS DISTINCT FROM jsonb_build_array(jsonb_build_object('channel_id',j.channel_id,
 'video_type',j.inventory_video_type,'lease_token',j.inventory_checkpoint->>'playlist_lease_token'))
 OR r.result->>'released' IS DISTINCT FROM '1' OR r.result->>'rejected' IS DISTINCT FROM '0' AS invalid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_QUOTA_RESULT="""SELECT %(result)s::jsonb||jsonb_build_object('kind','quota','provider_attempts',1,'pages_processed',0,
 'items_processed',0,'inserted',0,'updated',0,'error_code','initial_inventory_quota_exhausted',
 'retry_not_before',r.result->'retry_not_before','transition_id',%(transition)s::uuid) AS value
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r"""
_PAGE_IDENTITY="""SELECT r.scan_id IS DISTINCT FROM j.inventory_scan_id OR r.result->>'scan_id' IS DISTINCT FROM j.inventory_scan_id::text
 OR r.result->>'channel_id' IS DISTINCT FROM j.channel_id OR r.result->>'video_type' IS DISTINCT FROM j.inventory_video_type
 OR r.result->>'lease_token' IS DISTINCT FROM j.inventory_checkpoint->>'playlist_lease_token'
 OR r.result->>'transition_id' IS DISTINCT FROM %(transition)s::uuid::text OR r.transition_kind NOT IN('page','failure') AS invalid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_FAILURE_CHECK="""SELECT r.result->>'outcome' NOT IN('error','playlist_not_found') OR r.result->>'error_code' IS NULL
 OR r.result->>'error_code' !~ '^[a-z0-9_]{1,120}$' AS invalid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%s::jsonb) r"""
_FAILURE_CLOCK="""SELECT r.source_checked_at<s.scan_boundary_at AS invalid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r
 CROSS JOIN jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s"""
_FAILURE_COVERAGE="""SELECT s.pages_completed IS DISTINCT FROM
 (j.summary_json->'full_inventory_sources'->j.inventory_video_type->>'acked_pages')::integer AS invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_FAILURE_RESULT="""SELECT %(result)s::jsonb||CASE WHEN %(full)s::boolean THEN jsonb_build_object(
 'format_finished',r.result->>'outcome'='playlist_not_found','format_absent',r.result->>'outcome'='playlist_not_found',
 'source_checked_at',r.source_checked_at,'observed_video_ids','[]'::jsonb) ELSE '{}'::jsonb END
 ||jsonb_build_object('kind','failure','provider_attempts',1,'pages_processed',0,'items_processed',0,'inserted',0,'updated',0,
 'error_code',r.result->>'error_code','outcome',r.result->>'outcome','transition_id',%(transition)s::uuid)
 ||CASE WHEN r.result->>'error_code'='provider_rate_limited' THEN jsonb_build_object(
 'scan_retained',r.result->'scan_retained','retry_not_before',r.result->'next_due_at') ELSE '{}'::jsonb END AS value
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r"""
_PAGE_CHECK="""SELECT r.result->>'page_items' !~ '^[0-9]{1,2}$' OR (r.result->>'page_items')::integer>50
 OR r.result->>'page_items' IS NULL OR r.result->>'inserted' IS NULL OR r.result->>'already_matching' IS NULL
 OR r.result->>'inserted' !~ '^[0-9]{1,2}$' OR r.result->>'already_matching' !~ '^[0-9]{1,2}$'
 OR (r.result->>'inserted')::integer+(r.result->>'already_matching')::integer>(r.result->>'page_items')::integer AS invalid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%s::jsonb) r"""
_PAGE_COVERAGE="""SELECT jsonb_typeof(r.result->'observed_video_ids') IS DISTINCT FROM 'array'
 OR jsonb_array_length(r.result->'observed_video_ids')>50
 OR jsonb_array_length(r.result->'observed_video_ids') IS DISTINCT FROM (r.result->>'page_items')::integer
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(r.result->'observed_video_ids') x(v)
 WHERE jsonb_typeof(v)<>'string' OR v#>>'{}' !~ '^[A-Za-z0-9_-]{11}$')
 OR (SELECT count(DISTINCT v) FROM jsonb_array_elements_text(r.result->'observed_video_ids') x(v))<>jsonb_array_length(r.result->'observed_video_ids')
 OR s.pages_completed IS DISTINCT FROM 1+(j.summary_json->'full_inventory_sources'->j.inventory_video_type->>'acked_pages')::integer
 OR s.last_transition_id IS DISTINCT FROM %(transition)s::uuid AS invalid
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r
 CROSS JOIN jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_PAGE_RESULT="""SELECT %(result)s::jsonb||CASE WHEN %(full)s::boolean THEN jsonb_build_object(
 'format_finished',coalesce(%(full_ready)s::boolean,false),'format_absent',false,
 'source_checked_at',r.source_checked_at,'observed_video_ids',r.result->'observed_video_ids') ELSE '{}'::jsonb END
 ||jsonb_build_object('kind','page','provider_attempts',1,'pages_processed',1,
 'items_processed',(r.result->>'page_items')::integer,'inserted',(r.result->>'inserted')::integer,
 'updated',(r.result->>'already_matching')::integer,'transition_id',%(transition)s::uuid) AS value
 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r"""

_RESTART_INPUT="""SELECT to_jsonb(j)::text AS job,%s::boolean AS finished,j.inventory_checkpoint->>'phase' AS phase
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_ACTIVE="""SELECT to_jsonb(state.active_scan_id)#>>'{}' AS value
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%s::jsonb) state"""
_RESTART_SCAN="""SELECT s.status<>'running' AS invalid,s.scan_kind<>'discovery' AS keep
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%s::jsonb) s"""
_LIVE="""SELECT state.status='leased' AND state.lease_token IS NOT NULL AND state.lease_expires_at>clock_timestamp() AS value
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%s::jsonb) state"""
_ABANDON="""UPDATE public.channel_video_inventory_scans SET status='abandoned',coverage='partial',completed_at=NULL,resume_page_token=NULL,
 error_code='initial_inventory_full_required',error_at=clock_timestamp(),updated_at=clock_timestamp() WHERE scan_id=%s::uuid"""
_DETACH_PLAYLIST="""UPDATE public.channel_playlist_state d SET active_scan_id=NULL,resume_page_token=NULL,resume_started_at=NULL,scan_boundary_at=NULL,
 inventory_status=CASE WHEN d.inventory_status='completed' OR d.product_exposure_ready OR d.last_completed_scan_id IS NOT NULL THEN 'completed' ELSE 'pending' END,
 status=CASE WHEN d.runtime_enabled THEN 'ready' ELSE 'paused' END,
 next_due_at=CASE WHEN d.runtime_enabled THEN coalesce(d.next_due_at,clock_timestamp()) ELSE d.next_due_at END,
 lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,lease_channel_epoch=NULL,discovery_started_at=NULL,
 updated_at=clock_timestamp(),inventory_updated_at=clock_timestamp()
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%s::jsonb) state
 WHERE d.channel_id=state.channel_id AND d.video_type=state.video_type"""
_DETACH_JOB="""UPDATE public.operator_jobs target SET inventory_scan_id=NULL
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE target.id=j.id AND target.inventory_scan_id=%s::uuid"""


class _NativeObject(dict):
    """Internal read-only JSON snapshot with its exact PostgreSQL representation.

    The dict view has the ordinary RPC decoding shape. Passing it back into SQL
    uses encoded text, so large native numerics never round-trip through float.
    Composing helpers replace snapshots after writes rather than mutating them.
    """
    def __init__(self,encoded):
        super().__init__(json.loads(encoded))
        self.encoded=encoded


def _encoded(job):
    if isinstance(job,_NativeObject):return job.encoded
    return job if isinstance(job,(str,Jsonb)) else Jsonb(job) if job is not None else None
def _fail(code):raise DatabaseServerError(code,code='P0001')
def _value(row):return row['value'] if row is not None else None
def _native_result(tx,statement,values):
    # Only repository-owned complete SELECT constants enter this fixed wrapper.
    row=tx.fetch_one('SELECT captured.value::text AS value FROM ('+statement+') captured',values)
    return _NativeObject(row['value'])


def initial_inventory_checkpoint_evidence(transaction,job):
    """Read the exact bounded checkpoint proof; no claim or acknowledgement write."""
    tx=transaction
    with tx.borrow():
        args=tx.fetch_one(_INPUT,(_encoded(job),))
        captured=args['job']
        playlist=_value(tx.fetch_one(_PLAYLIST,(captured,)))
        if playlist is None:
            if args['phase']=='bound':_fail('initial_inventory_state_missing')
            return dict(kind='none',format_finished=False,format_absent=False,bound_live=False)
        full=args['version']==1
        if full:assert_full_initial_scope(tx,captured,False)
        values=dict(job=captured,playlist=playlist,scan=None,full=full,full_ready=False)
        values['scan_id']=tx.fetch_one(_SCAN_KEY,values)['value']
        if values['scan_id'] is not None:
            values['scan']=_value(tx.fetch_one(_SCAN,(values['scan_id'],)))
            if values['scan'] is None or tx.fetch_one(_SCAN_IDENTITY,values)['invalid'] is True:_fail('initial_inventory_scan_identity_invalid')
            values['full_ready']=tx.fetch_one(_FULL_READY,(values['scan'],))['value']
        base=tx.fetch_one(_BASE,values)
        result=base['value']
        if full:
            result.update(format_finished=False,format_absent=False)
            if base['source_invalid'] is True:_fail('full_initial_source_identity_invalid')
        if args['phase']!='bound':return result
        values['transition']=tx.fetch_one(_TRANSITION,(captured,))['value']
        page=_value(tx.fetch_one(_RECEIPT,('scan_transition',values['transition'])))
        quota=_value(tx.fetch_one(_RECEIPT,('quota_defer',values['transition'])))
        if page is not None and quota is not None:_fail('initial_inventory_receipt_conflict')
        if quota is None and page is None:return result
        values['receipt']=quota if quota is not None else page
        values['result']=Jsonb(result)
        header=tx.fetch_one(_HEADER,(values['receipt'],))
        if header['expired'] is True:_fail('initial_inventory_receipt_expired')
        _hot(tx,header['source_checked_at'])
        if quota is not None:
            if tx.fetch_one(_QUOTA_CHECK,values)['invalid'] is True:_fail('initial_inventory_quota_receipt_invalid')
            return _native_result(tx,_QUOTA_RESULT,values)
        if tx.fetch_one(_PAGE_IDENTITY,values)['invalid'] is True:_fail('initial_inventory_receipt_identity_invalid')
        if header['kind']=='failure':
            if tx.fetch_one(_FAILURE_CHECK,(page,))['invalid'] is True:_fail('initial_inventory_failure_receipt_invalid')
            if full:
                if tx.fetch_one(_FAILURE_CLOCK,values)['invalid'] is True:_fail('full_initial_source_clock_invalid')
                if tx.fetch_one(_FAILURE_COVERAGE,values)['invalid'] is True:_fail('full_initial_source_coverage_invalid')
            return _native_result(tx,_FAILURE_RESULT,values)
        if tx.fetch_one(_PAGE_CHECK,(page,))['invalid'] is True:_fail('initial_inventory_page_receipt_invalid')
        if full and tx.fetch_one(_PAGE_COVERAGE,values)['invalid'] is True:_fail('full_initial_page_coverage_invalid')
        return _native_result(tx,_PAGE_RESULT,values)


def restart_initial_inventory_discovery(transaction,job,format_finished):
    """Detach an idle legacy discovery generation under the caller's owner locks."""
    tx=transaction
    with tx.borrow():
        args=tx.fetch_one(_RESTART_INPUT,(format_finished,_encoded(job)))
        if args['finished'] is True:return True
        if args['phase']=='bound':_fail('initial_inventory_receipt_reconciliation_required')
        playlist=_value(tx.fetch_one(_PLAYLIST,(args['job'],)))
        if playlist is None:return True
        scan_id=tx.fetch_one(_ACTIVE,(playlist,))['value']
        if scan_id is None:return True
        scan=_value(tx.fetch_one(_SCAN,(scan_id,)))
        values=dict(job=args['job'],playlist=playlist,scan=scan)
        if scan is None or tx.fetch_one(_SCAN_IDENTITY,values)['invalid'] is True:_fail('initial_inventory_scan_identity_invalid')
        facts=tx.fetch_one(_RESTART_SCAN,(scan,))
        if facts['invalid'] is True:_fail('initial_inventory_active_scan_invalid')
        if facts['keep'] is True:return True
        if tx.fetch_one(_LIVE,(playlist,))['value'] is True:return False
        tx.execute(_ABANDON,(scan_id,))
        tx.execute(_DETACH_PLAYLIST,(playlist,))
        tx.execute(_DETACH_JOB,(args['job'],scan_id))
        return True
