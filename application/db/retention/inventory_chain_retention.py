"""Finite child-copy retention under the existing operator parent owner."""
import json
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseServerError
from ..inventory.inventory_scan_clock import SCAN_IS_HOT_SQL

# An expiry marker is local state only after every retained source copy is gone.
# The alias is fixed for the shared startup/update-copy registry. Child lookups
# use their (job_id, video_type) primary key; a marker never hides live children.
SOURCE_FREE_JOB_SQL = """source.resume_page_token IS NULL
 AND source.inventory_scan_id IS NULL AND source.inventory_checkpoint='{}'::jsonb
 AND source.status IN('completed','error') AND source.last_error_code='source_scope_expired'
 AND source.last_error_summary IS NULL AND source.lease_token IS NULL
 AND source.lease_channel_epoch IS NULL AND source.lease_owner IS NULL AND source.lease_expires_at IS NULL
 AND NOT EXISTS(SELECT 1 FROM public.operator_job_targets target WHERE target.job_id=source.id)
 AND ((source.summary_json='{"source_scope_expired":true}'::jsonb
   AND NOT EXISTS(SELECT 1 FROM public.operator_job_inventory_chains child WHERE child.job_id=source.id))
  OR (source.summary_json='{"source_scope_expired":true,"inventory_chain_version":1}'::jsonb
   AND (SELECT count(*)=3 AND bool_and(child.channel_id=source.channel_id
     AND child.video_type IN('long','short','stream') AND child.scan_id IS NULL
     AND child.checkpoint='{}'::jsonb AND child.last_ack='{}'::jsonb AND child.terminal_outcome IS NULL)
    FROM public.operator_job_inventory_chains child WHERE child.job_id=source.id)))"""

_READ = """SELECT to_jsonb(c)::text AS value FROM public.operator_job_inventory_chains c
 WHERE job_id=%(id)s::uuid ORDER BY video_type"""
_PLAYLIST_LOCKS = """SELECT video_type FROM public.channel_playlist_state
 WHERE channel_id=%(channel_id)s::text AND video_type=ANY(ARRAY['long','short','stream']::text[])
 ORDER BY video_type FOR UPDATE NOWAIT"""
_SCAN_LOCKS = """SELECT scan_id FROM public.channel_video_inventory_scans
 WHERE scan_id=ANY(%s::uuid[]) ORDER BY scan_id FOR UPDATE NOWAIT"""
_RECEIPT_LOCKS = """SELECT r.receipt_scope,r.transition_id FROM public.typed_inventory_receipts r
 JOIN jsonb_to_recordset(%s::jsonb) k(scope text,transition uuid)
 ON r.receipt_scope=k.scope AND r.transition_id=k.transition
 ORDER BY r.receipt_scope,r.transition_id FOR UPDATE OF r NOWAIT"""
_SCAN_HOT = """SELECT scan.channel_id=%(channel)s::text AND scan.video_type=%(kind)s::text
 AND """ + SCAN_IS_HOT_SQL.replace('%(scan_at)s::timestamptz','native.at') + """ AS hot
 FROM public.channel_video_inventory_scans scan
 CROSS JOIN (SELECT clock_timestamp() AS at OFFSET 0) native WHERE scan_id=%(scan)s::uuid"""
_RECEIPT_HOT = """SELECT NOT(result ? 'source_expired') AND isfinite(source_checked_at)
 AND source_checked_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp() AS hot
 FROM public.typed_inventory_receipts WHERE receipt_scope=%s AND transition_id=%s::uuid"""
_BOUND_BUSY = """SELECT status='leased' AND lease_token=%(token)s::uuid
 AND active_scan_id=%(scan)s::uuid AND lease_expires_at>clock_timestamp() AS busy
 FROM public.channel_playlist_state WHERE channel_id=%(channel)s::text AND video_type=%(kind)s::text"""
_CLEAR_ACK = """UPDATE public.operator_job_inventory_chains SET last_ack='{}'::jsonb,updated_at=clock_timestamp()
 WHERE job_id=%(id)s::uuid AND video_type=ANY(%(clear_ack_types)s::text[]) AND last_ack<>'{}'::jsonb"""
_SCRUB = """UPDATE public.operator_job_inventory_chains SET scan_id=NULL,checkpoint='{}'::jsonb,
 last_ack='{}'::jsonb,terminal_outcome=NULL,updated_at=clock_timestamp()
 WHERE job_id=%(id)s::uuid AND (scan_id IS NOT NULL OR checkpoint<>'{}'::jsonb
 OR last_ack<>'{}'::jsonb OR terminal_outcome IS NOT NULL)"""

PIN_SCAN = """SELECT EXISTS(SELECT 1 FROM public.operator_job_inventory_chains
 WHERE scan_id=%(chain_scan)s::uuid) OR EXISTS(SELECT 1 FROM public.operator_job_inventory_chains
 WHERE jsonb_typeof(last_ack->'scan_id')='string' AND last_ack->>'scan_id'=%(chain_scan)s::uuid::text) AS pinned"""
PIN_RECEIPT = """SELECT EXISTS(SELECT 1 FROM public.operator_job_inventory_chains
 WHERE checkpoint->>'phase'='bound' AND checkpoint->>'transition_id'=%(chain_transition)s::uuid::text)
 OR EXISTS(SELECT 1 FROM public.operator_job_inventory_chains WHERE last_ack<>'{}'::jsonb
 AND last_ack->>'transition_id'=%(chain_transition)s::uuid::text)
 OR EXISTS(SELECT 1 FROM public.operator_job_inventory_chains WHERE scan_id=%(chain_scan)s::uuid)
 OR EXISTS(SELECT 1 FROM public.operator_job_inventory_chains
 WHERE jsonb_typeof(last_ack->'scan_id')='string' AND last_ack->>'scan_id'=%(chain_scan)s::uuid::text) AS pinned"""


def marked(job):
    summary=json.loads(job['summary'])
    marker=summary.get('inventory_chain_version')
    if marker is None:return False
    if type(marker) is not int or marker!=1:
        raise DatabaseServerError('hot_inventory_chain_state_invalid',code='P0001')
    return True


def _children(tx,p,lock=False):
    rows=tx.fetch_all(_READ+(' FOR UPDATE NOWAIT' if lock else ''),p,max_rows=3)
    children=[json.loads(row['value']) for row in rows]
    if len(children)!=3 or {child['video_type'] for child in children}!={'long','short','stream'}:
        raise DatabaseServerError('hot_inventory_chain_state_invalid',code='P0001')
    return children


def inspect_copies(tx,job,p,*,lock=False):
    """Inspect <=3 children, <=6 scans and <=12 exact receipts; no mutation."""
    if not marked(job):return dict(linked=False,clear_ack_types=[],busy=False)
    children=_children(tx,p,lock)
    summary=json.loads(job['summary'])
    scans=set()
    receipt_keys=set()
    for child in children:
        ack,cp=child['last_ack'],child['checkpoint']
        scans.update(value for value in (child['scan_id'],ack.get('scan_id')) if value is not None)
        if cp.get('phase')=='bound':
            receipt_keys.update((scope,cp['transition_id']) for scope in ('scan_transition','quota_defer'))
        if ack and ack.get('outcome') not in ('none','deferred','unknown'):
            receipt_keys.add(('quota_defer' if ack.get('outcome')=='quota' else 'scan_transition',ack['transition_id']))
        source=summary.get('full_inventory_sources',{}).get(child['video_type'],{})
        if source.get('terminal_outcome') is not None:
            receipt_keys.add(('scan_transition',source['last_transition_id']))
    if len(scans)>6 or len(receipt_keys)>12:
        raise DatabaseServerError('hot_inventory_chain_state_invalid',code='P0001')
    if lock:
        tx.fetch_all(_PLAYLIST_LOCKS,{**p,**job},max_rows=3)
        tx.fetch_all(_SCAN_LOCKS,(sorted(scans),),max_rows=6)
        tx.fetch_all(_RECEIPT_LOCKS,(Jsonb([dict(scope=s,transition=t) for s,t in sorted(receipt_keys)]),),max_rows=12)
        children=_children(tx,p)
    scan_cache,receipt_cache={},{}
    def scan_hot(scan,kind):
        if scan is None:return True
        key=(scan,kind)
        if key not in scan_cache:
            row=tx.fetch_one(_SCAN_HOT,dict(scan=scan,channel=job['channel_id'],kind=kind))
            scan_cache[key]=row is not None and row['hot'] is True
        return scan_cache[key]
    def receipt_hot(scope,transition,optional=False):
        key=(scope,transition)
        if key not in receipt_cache:receipt_cache[key]=tx.fetch_one(_RECEIPT_HOT,key)
        row=receipt_cache[key]
        return optional if row is None else row['hot'] is True
    linked,busy,clear=False,False,[]
    for child in children:
        kind,ack,cp=child['video_type'],child['last_ack'],child['checkpoint']
        if child['channel_id']!=job['channel_id']:
            raise DatabaseServerError('hot_inventory_chain_state_invalid',code='P0001')
        linked|=not scan_hot(child['scan_id'],kind)
        source=summary.get('full_inventory_sources',{}).get(kind,{})
        if source.get('terminal_outcome') is not None:
            linked|=not receipt_hot('scan_transition',source['last_transition_id'])
        if cp.get('phase')=='bound':
            state=tx.fetch_one(_BOUND_BUSY,dict(token=cp['playlist_lease_token'],scan=child['scan_id'],
                channel=job['channel_id'],kind=kind))
            busy|=state is not None and state['busy'] is True
            for scope in ('scan_transition','quota_defer'):
                linked|=not receipt_hot(scope,cp['transition_id'],optional=True)
        if ack:
            expired=not scan_hot(ack.get('scan_id'),kind)
            if ack.get('outcome') not in ('none','deferred','unknown'):
                expired|=not receipt_hot('quota_defer' if ack['outcome']=='quota' else 'scan_transition',ack['transition_id'])
            if expired:
                if cp or child['terminal_outcome'] is not None or ack.get('scan_id')==child['scan_id']:
                    linked=True
                else:clear.append(kind)
    return dict(linked=linked,clear_ack_types=clear,busy=busy)


def pinned_scan(tx,scan):
    return scan is not None and tx.fetch_one(PIN_SCAN,dict(chain_scan=scan))['pinned'] is True


def pinned_receipt(tx,scan,transition):
    return tx.fetch_one(PIN_RECEIPT,dict(chain_scan=scan,chain_transition=transition))['pinned'] is True


def clear_historical_acks(tx,p,facts):
    return tx.execute(_CLEAR_ACK,{**p,'clear_ack_types':facts['clear_ack_types']}) if facts['clear_ack_types'] else 0


def scrub_copies(tx,p):
    return tx.execute(_SCRUB,p)
