"""Borrowed receipt-only chain settlement; parent lease and siblings survive."""
from . import operator_job_inventory_settlement as full
from .operator_job_inventory_evidence import _NativeObject, _encoded
from ..base.postgres import DatabaseServerError
from ..channel.channel_workflow_updates import apply_channel_stage

_FIELDS = """SELECT (%(evidence)s::jsonb->>'provider_attempts')::integer AS attempts,
 (%(evidence)s::jsonb->>'pages_processed')::integer AS pages,
 (%(evidence)s::jsonb->>'items_processed')::integer AS items,
 (%(evidence)s::jsonb->>'inserted')::integer AS inserted,
 (%(evidence)s::jsonb->>'updated')::integer AS updated,
 CASE WHEN %(evidence)s::jsonb->>'kind'='quota'
 AND isfinite((%(evidence)s::jsonb->>'retry_not_before')::timestamptz)
 THEN greatest(clock_timestamp()+interval '1 second',
 (%(evidence)s::jsonb->>'retry_not_before')::timestamptz)
 WHEN %(evidence)s::jsonb->>'kind'='failure'
 AND %(evidence)s::jsonb->>'outcome'='error'
 AND %(evidence)s::jsonb->>'error_code'='provider_rate_limited'
 AND isfinite((%(evidence)s::jsonb->>'retry_not_before')::timestamptz)
 THEN greatest(clock_timestamp()+interval '1 second',least(clock_timestamp()+interval '1 minute',
 (%(evidence)s::jsonb->>'retry_not_before')::timestamptz)) END::text AS retry"""
_ACK = """SELECT jsonb_build_object('version',1,'kind',CASE WHEN %(success)s::boolean THEN 'settled' ELSE 'failed' END,
 'channel_id',j.channel_id,'video_type',j.inventory_video_type,'transition_id',%(transition)s::uuid,
 'job_lease_token',%(lease_token)s::uuid,'scan_id',j.inventory_scan_id,
 'playlist_lease_token',j.inventory_checkpoint->'playlist_lease_token',
 'request_hash',md5(jsonb_build_array(j.id,j.inventory_video_type,%(lease_token)s::uuid,%(transition)s::uuid)::text),
 'provider_attempts',%(attempts)s::integer,'quota_estimated',true,
 'outcome',CASE WHEN %(absent)s::boolean THEN 'playlist_not_found'
 WHEN %(success)s::boolean THEN 'page' WHEN %(quota)s::boolean THEN 'quota'
 WHEN %(unknown)s::boolean THEN 'unknown' ELSE 'error' END,
 'error_code',%(evidence)s::jsonb->'error_code')::text AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_CHAIN = """UPDATE public.operator_job_inventory_chains SET checkpoint='{}'::jsonb,
 last_ack=%(ack)s::jsonb,terminal_outcome=CASE WHEN %(done)s::boolean
 THEN CASE WHEN %(absent)s::boolean THEN 'playlist_not_found' ELSE 'completed' END END,
 updated_at=now() WHERE job_id=%(job_id)s::uuid AND video_type=%(video_type)s::text"""
_PARENT = """UPDATE public.operator_jobs x SET target_count=x.target_count+%(added)s::integer,
 pages_processed=x.pages_processed+%(pages)s::integer,items_processed=x.items_processed+%(items)s::integer,
 inventory_inserted_count=x.inventory_inserted_count+%(inserted)s::integer,
 inventory_updated_count=x.inventory_updated_count+%(updated)s::integer,
 quota_units_reserved=greatest(x.quota_units_reserved-1+%(attempts)s::integer,0),
 quota_units_observed_estimate=x.quota_units_observed_estimate+%(attempts)s::integer,
 error_count=x.error_count+CASE WHEN %(success)s::boolean OR %(quota)s::boolean OR %(rate_limited)s::boolean THEN 0 ELSE 1 END,
 summary_json=CASE WHEN %(success)s::boolean THEN
 jsonb_set(x.summary_json,ARRAY['full_inventory_sources',%(video_type)s::text],%(proof)s::jsonb)
 ||jsonb_build_object('inventory_results',%(results)s::jsonb,'inventory_complete',false)
 ELSE x.summary_json END,
 retry_not_before=CASE WHEN %(quota)s::boolean OR %(rate_limited)s::boolean THEN greatest(x.retry_not_before,%(retry)s::timestamptz)
 ELSE x.retry_not_before END,
 last_error_code=CASE WHEN NOT %(success)s::boolean AND NOT %(quota)s::boolean AND NOT %(rate_limited)s::boolean THEN 'initial_inventory_restart_required'
 WHEN x.last_error_code='initial_inventory_restart_required' THEN x.last_error_code
 WHEN x.target_count+%(added)s::integer>10000 AND NOT coalesce((x.summary_json->>'confirmed')::boolean,false)
 THEN 'initial_inventory_confirmation_required' ELSE x.last_error_code END,
 updated_at=now(),heartbeat_at=now() WHERE x.id=%(job_id)s::uuid"""


def settle_inventory_chain_receipt(transaction, args, evidence):
    """Derive every counter from the existing verified receipt, never a caller."""
    tx = transaction
    with tx.borrow():
        values = dict(args, evidence=_encoded(evidence))
        values.update(tx.fetch_one(_FIELDS, values))
        if (values['attempts'] != 1 or values['pages'] not in (0, 1)
                or any(type(values[k]) is not int or not 0 <= values[k] <= 50
                       for k in ('items', 'inserted', 'updated'))):
            raise DatabaseServerError('initial_inventory_job_receipt_counts_mismatch', code='P0001')
        kind = evidence['kind']
        if kind == 'quota' and values['retry'] is None:
            raise DatabaseServerError('initial_inventory_quota_retry_invalid', code='P0001')
        absent = kind == 'failure' and evidence.get('outcome') == 'playlist_not_found'
        if absent and evidence.get('format_absent') is not True:
            raise DatabaseServerError('initial_inventory_absence_receipt_required', code='P0001')
        rate_limited = (kind == 'failure' and evidence.get('outcome') == 'error'
                        and evidence.get('error_code') == 'provider_rate_limited')
        if rate_limited and (evidence.get('scan_retained') is not True or values['retry'] is None):
            raise DatabaseServerError('initial_inventory_outcome_unresolved', code='P0001')
        success = kind == 'page' or absent
        values.update(success=success, absent=absent, quota=kind == 'quota', rate_limited=rate_limited, unknown=kind == 'unknown',
            done=success and evidence.get('format_finished') is True,
            added=0, proof=None, results=None, has_long=False)
        if ((kind == 'page') != (values['pages'] == 1)
                or (kind != 'page' and any(values[k] != 0 for k in ('items', 'inserted', 'updated')))):
            raise DatabaseServerError('initial_inventory_job_receipt_counts_mismatch', code='P0001')
        values['ack'] = tx.fetch_one(_ACK, values)['value']
        if success:
            page = tx.fetch_one(full._IDS, (values['evidence'],))
            if page['oversized'] is True:
                raise DatabaseServerError('full_initial_page_coverage_invalid', code='P0001')
            values['ids'] = page['ids']
            values['added'] = tx.execute(full._INSERT, values)
            tx.execute(full._TARGETS, values)
            values.update(tx.fetch_one(full._PROOF, values))
            values.update(tx.fetch_one(full._RESULTS, values))
        if tx.execute(_CHAIN, values) != 1 or tx.execute(_PARENT, values) != 1:
            raise DatabaseServerError('initial_inventory_chain_state_invalid', code='P0001')
        if success and values['has_long'] is True:
            apply_channel_stage(tx, 'full_settlement', (values['results'], values['channel_id']))
        return _NativeObject(values['ack'])
