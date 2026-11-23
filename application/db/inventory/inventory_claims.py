"""Typed playlist claims.

One finite ordered playlist selection owns all leases. Continuations require
the exact page receipt and current generation; initial work additionally owns
channel, job, playlist and optional scan locks. The final shared control fence
is last. Shared control-policy projections and native integrity guards remain
authoritative. No stored business function, provider work, internal retry or
acknowledgement before COMMIT.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..jobs.operator_job_scope import assert_full_initial_scope
from ..videos.collection_policy import require_cadence_snapshot
from ..base.postgres import DatabaseServerError, Operation
from ..base.sql_values import application_value
from channel_collection_scope import validate_channel_scopes
from saved_work import channel_id as canonical_channel_id
from .manual_check import MANUAL_PENDING_SQL
from .inventory_heads import HEAD_COUNT_DUE_SQL, HEAD_DUE_SQL, HEAD_BACKOFF_SQL, ABSENT_UNTIL_RECHECK_SQL

_INPUT="""WITH args AS MATERIALIZED(SELECT %s::integer AS claim_limit,%s::text[] AS channel_ids,
 %s::text[] AS video_types,%s::text AS worker,%s::text AS run_id,%s::integer AS lease_seconds,
 %s::jsonb AS scopes,%s::boolean AS only_due,%s::jsonb AS continuations,%s::boolean AS include_due,
 %s::uuid AS initial_job_id,%s::uuid AS initial_job_lease_token)
 SELECT args.*,scopes::text AS scopes_text,continuations::text AS continuations_text,
 claim_limit IS NULL OR claim_limit<1 OR claim_limit>500 OR only_due IS NULL AS limit_invalid,
 (NOT only_due AND channel_ids IS NULL) OR (channel_ids IS NOT NULL AND cardinality(channel_ids) NOT BETWEEN 1 AND 1000) AS channels_invalid,
 video_types IS NULL OR cardinality(video_types) NOT BETWEEN 1 AND 3
 OR EXISTS(SELECT 1 FROM unnest(video_types) x WHERE x IS NULL OR x NOT IN('long','short','stream')) AS types_invalid,
 nullif(btrim(worker),'') IS NULL OR length(btrim(worker))>200
 OR nullif(btrim(run_id),'') IS NULL OR length(btrim(run_id))>200 AS owner_invalid,
 lease_seconds IS NULL OR lease_seconds NOT BETWEEN 60 AND 3600 AS lease_invalid FROM args"""
_SCOPES="""WITH input AS(SELECT %(scopes_text)s::jsonb AS scopes,%(channel_ids)s::text[] AS ids)
 SELECT CASE WHEN jsonb_typeof(scopes)<>'array' THEN true WHEN jsonb_array_length(scopes)>1000 THEN true ELSE
 EXISTS(SELECT 1 FROM jsonb_array_elements(scopes) e WHERE CASE WHEN jsonb_typeof(e)<>'object' THEN true ELSE
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key) IS DISTINCT FROM ARRAY['channel_id','control_epoch']::text[]
 OR jsonb_typeof(e->'channel_id') IS DISTINCT FROM 'string' OR nullif(btrim(e->>'channel_id'),'') IS NULL
 OR e->>'channel_id'<>btrim(e->>'channel_id') OR length(e->>'channel_id')>128
 OR jsonb_typeof(e->'control_epoch') IS DISTINCT FROM 'number' OR (e->>'control_epoch')!~'^[1-9][0-9]*$'
 OR NOT pg_input_is_valid(e->>'control_epoch','bigint') END)
 OR (SELECT count(DISTINCT e->>'channel_id') FROM jsonb_array_elements(scopes) e)<>jsonb_array_length(scopes)
 OR (SELECT array_agg(e->>'channel_id' ORDER BY e->>'channel_id') FROM jsonb_array_elements(scopes) e)
 IS DISTINCT FROM (SELECT array_agg(channel_id ORDER BY channel_id) FROM
  (SELECT DISTINCT btrim(id) AS channel_id FROM unnest(ids) id WHERE nullif(btrim(id),'') IS NOT NULL) requested)
 END AS invalid FROM input"""
_CONTINUATIONS="""WITH input AS(SELECT %(continuations_text)s::jsonb AS proofs)
 SELECT CASE WHEN jsonb_typeof(proofs)<>'array' THEN true WHEN jsonb_array_length(proofs)>500 THEN true ELSE
 EXISTS(SELECT 1 FROM jsonb_array_elements(proofs) e WHERE CASE WHEN jsonb_typeof(e)<>'object' THEN true ELSE
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key)
 IS DISTINCT FROM ARRAY['channel_id','control_epoch','last_transition_id','scan_id','video_type']::text[]
 OR jsonb_typeof(e->'channel_id') IS DISTINCT FROM 'string' OR (e->>'channel_id')!~'^UC[A-Za-z0-9_-]{22}$'
 OR jsonb_typeof(e->'video_type') IS DISTINCT FROM 'string' OR e->>'video_type' NOT IN('long','short','stream')
 OR jsonb_typeof(e->'control_epoch') IS DISTINCT FROM 'number' OR (e->>'control_epoch')!~'^[1-9][0-9]*$'
 OR NOT pg_input_is_valid(e->>'control_epoch','bigint')
 OR jsonb_typeof(e->'scan_id') IS DISTINCT FROM 'string'
 OR (e->>'scan_id')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
 OR jsonb_typeof(e->'last_transition_id') IS DISTINCT FROM 'string'
 OR (e->>'last_transition_id')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' END)
 OR (SELECT count(DISTINCT(e->>'channel_id',e->>'video_type')) FROM jsonb_array_elements(proofs) e)<>jsonb_array_length(proofs)
 END AS invalid FROM input"""
_SIGNAL_DUE="""EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled)
           AND NOT (d.next_due_at>now() AND d.last_error_code IS NOT NULL
             AND EXISTS(SELECT 1 FROM public.typed_inventory_receipts failed
             JOIN public.channel_video_inventory_scans attempted USING(scan_id)
             JOIN public.channels signalled ON signalled.channel_id=d.channel_id
             WHERE failed.receipt_scope='scan_transition' AND failed.transition_kind='failure'
               AND failed.transition_id=d.last_failure_attempt_id AND attempted.channel_id=d.channel_id
               AND attempted.video_type=d.video_type
               AND coalesce(failed.result->'discovery_attempt'=jsonb_build_object(
                   'epoch',c.control_epoch,'count_token',signalled.count_reconcile_token,
                   'full_token',d.full_reconcile_required_token),
                 attempted.discovery_channel_epoch=c.control_epoch
                 AND attempted.count_reconcile_token IS NOT DISTINCT FROM signalled.count_reconcile_token
                 AND attempted.full_reconcile_token IS NOT DISTINCT FROM d.full_reconcile_required_token)))
           AND (d.full_reconcile_required_token IS DISTINCT FROM d.full_reconcile_served_token
             OR EXISTS(SELECT 1 FROM public.channels changed WHERE changed.channel_id=d.channel_id
               AND changed.count_reconcile_token IS NOT NULL
               AND changed.count_reconcile_token IS DISTINCT FROM changed.count_reconcile_settled_token
               AND (changed.count_reconcile_token IS DISTINCT FROM d.count_reconcile_served_token
                    OR d.count_reconcile_served_epoch IS DISTINCT FROM c.control_epoch)))"""
_MANUAL_DUE = '(' + MANUAL_PENDING_SQL + """ AND (d.active_scan_id IS NULL OR
 (d.active_scan_id::text=d.manual_list_check->>'scan' AND d.next_due_at<=now()))
 AND (d.manual_list_check->>'retry_at' IS NULL OR (d.manual_list_check->>'retry_at')::timestamptz<=now()))"""
_EVENT_DUE = '(((' + _SIGNAL_DUE + ') OR (' + HEAD_COUNT_DUE_SQL + ')) AND NOT (' + ABSENT_UNTIL_RECHECK_SQL + '))'
_SIGNAL_DUE = '(' + _EVENT_DUE + ' OR ' + _MANUAL_DUE + ')'
# A failed check of the list's start pauses new checks of the start, never the
# remaining pages of a scan already under way: those pages do not read it.
# A scan that has not finished its first page is still a check of the start.
_SCAN_UNDER_WAY="""EXISTS(SELECT 1 FROM public.channel_video_inventory_scans under_way
   WHERE under_way.scan_id=d.active_scan_id AND under_way.status='running'
     AND under_way.pages_completed>0 AND under_way.resume_page_token IS NOT NULL)"""
_CANDIDATE_PREDICATE=("""d.video_type=ANY(%(video_types)s::text[]) AND d.runtime_enabled
 AND (NOT %(only_due)s::boolean OR NOT ("""+ABSENT_UNTIL_RECHECK_SQL+""") OR """+_SCAN_UNDER_WAY+""" OR """+_MANUAL_DUE+""")
 AND (NOT ("""+HEAD_BACKOFF_SQL+""") OR """+_SCAN_UNDER_WAY+""" OR """+_MANUAL_DUE+""")
 AND coalesce((SELECT v.typed_runtime_eligible
   FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ v WHERE v.channel_id=d.channel_id),false)
 AND (%(channel_ids)s::text[] IS NULL OR d.channel_id=ANY(%(channel_ids)s::text[]))
 AND (%(scopes_text)s::jsonb IS NULL OR EXISTS(SELECT 1 FROM jsonb_array_elements(%(scopes_text)s::jsonb) e
   WHERE e->>'channel_id'=d.channel_id AND (e->>'control_epoch')::bigint=c.control_epoch))
 AND (d.status='ready' OR (d.status='leased' AND d.lease_expires_at<=now()))
 AND ((%(include_due)s::boolean AND (NOT %(only_due)s::boolean OR d.next_due_at<=now()
        OR ("""+_SIGNAL_DUE+""") OR ("""+HEAD_DUE_SQL+""")
       OR (d.status='leased' AND d.lease_expires_at<=now())))
   OR (%(continuations_text)s::jsonb IS NOT NULL AND d.last_error_code IS NULL AND EXISTS(
    SELECT 1 FROM jsonb_array_elements(%(continuations_text)s::jsonb) proof
    JOIN public.channel_video_inventory_scans own_scan ON own_scan.scan_id=(proof->>'scan_id')::uuid
    WHERE proof->>'channel_id'=d.channel_id AND proof->>'video_type'=d.video_type
     AND (proof->>'control_epoch')::bigint=c.control_epoch
     AND own_scan.scan_id=d.active_scan_id AND own_scan.channel_id=d.channel_id
     AND own_scan.video_type=d.video_type AND own_scan.playlist_id=d.playlist_id
     AND own_scan.status='running' AND own_scan.coverage='partial' AND own_scan.token_history_verified
     AND own_scan.last_transition_kind='page' AND own_scan.last_transition_id=(proof->>'last_transition_id')::uuid
     AND own_scan.resume_page_token IS NOT NULL AND own_scan.resume_page_token IS NOT DISTINCT FROM d.resume_page_token
     AND own_scan.pages_completed>0
     AND isfinite(own_scan.started_at) AND own_scan.started_at BETWEEN now()-interval '696 hours' AND now()
     AND isfinite(own_scan.scan_boundary_at) AND own_scan.scan_boundary_at BETWEEN now()-interval '696 hours' AND now()
     AND isfinite(own_scan.source_checked_at) AND own_scan.source_checked_at BETWEEN own_scan.scan_boundary_at AND now()
     AND EXISTS(SELECT 1 FROM public.typed_inventory_receipts r WHERE r.receipt_scope='scan_transition'
       AND r.transition_id=own_scan.last_transition_id AND r.scan_id=own_scan.scan_id AND r.transition_kind='page'
       AND NOT(r.result ? 'source_expired') AND r.next_page_token_hash IS NOT NULL
       AND r.next_page_token_hash=sha256(convert_to(own_scan.resume_page_token,'UTF8'))
       AND isfinite(r.source_checked_at) AND r.source_checked_at BETWEEN now()-interval '696 hours' AND now())
   )))
 AND (%(initial_job_id)s::uuid IS NOT NULL OR NOT EXISTS(SELECT 1 FROM public.operator_jobs j
   WHERE j.channel_id=d.channel_id AND j.initial_scope_version=1 AND j.phase='inventory'
    AND j.accepted_epoch=c.control_epoch AND j.id=c.initial_job_id AND j.status<>'completed'))
 AND (%(initial_job_id)s::uuid IS NULL OR EXISTS(SELECT 1 FROM public.operator_jobs j
   WHERE j.id=%(initial_job_id)s::uuid AND j.channel_id=d.channel_id AND j.inventory_video_type=d.video_type
    AND j.initial_scope_version=1 AND j.phase='inventory' AND j.status='running'
    AND j.lease_token=%(initial_job_lease_token)s::uuid AND j.lease_expires_at>clock_timestamp()
    AND j.accepted_epoch=c.control_epoch AND j.id=c.initial_job_id AND j.lease_channel_epoch=c.control_epoch))
 AND CASE WHEN d.last_error_code IN('youtube_daily_quota_exceeded_error','youtube_quota_exhausted') THEN
   coalesce(d.next_due_at,'infinity'::timestamptz)<=now() AND coalesce(d.source_next_after,'infinity'::timestamptz)<=now()
   ELSE true END""")
_CANDIDATES="""SELECT to_jsonb(d)::text AS source,c.control_epoch AS epoch,d.channel_id,d.video_type
 FROM public.channel_playlist_state d JOIN public.channel_collection_state c ON c.channel_id=d.channel_id
 WHERE """+_CANDIDATE_PREDICATE+"""
 ORDER BY d.dispatch_ordinal,d.priority DESC,d.channel_id,d.video_type
 FOR UPDATE OF d SKIP LOCKED LIMIT %(claim_limit)s::integer"""
_CLAIM="""UPDATE public.channel_playlist_state d SET status='leased',lease_token=gen_random_uuid(),
 lease_channel_epoch=%(epoch)s::bigint,lease_owner=btrim(%(worker)s::text),lease_run_id=btrim(%(run_id)s::text),
 lease_expires_at=now()+make_interval(secs=>%(lease_seconds)s::integer),last_claim_run_id=btrim(%(run_id)s::text),
 discovery_attempt_count_token=(SELECT changed.count_reconcile_token FROM public.channels changed WHERE changed.channel_id=d.channel_id
 AND changed.count_reconcile_token IS DISTINCT FROM changed.count_reconcile_settled_token),
 discovery_attempt_full_token=d.full_reconcile_required_token,discovery_attempt_epoch=%(epoch)s::bigint,
 attempt_count=d.attempt_count+1,dispatch_ordinal=nextval('operator_private.inventory_dispatch_seq'),updated_at=now()
 WHERE d.channel_id=%(channel_id)s::text AND d.video_type=%(video_type)s::text
 RETURNING lease_token::text AS token,to_jsonb(lease_expires_at)#>>'{}' AS expires"""
_ACTIVE_SCAN="""SELECT to_jsonb(s)::text AS scan,s.scan_kind FROM public.channel_video_inventory_scans s
 WHERE s.scan_id=(%(source)s::jsonb->>'active_scan_id')::uuid AND s.status='running'"""
# Shared with the supervisor's bounded signal probe. Aliases d (playlist), c
# (control) and a frozen cadence parameter are supplied by the trusted owner.
_AUTOMATIC_FULL_REQUIRED_SQL="""coalesce((SELECT active.scan_kind IN('initial_inventory','full_reconciliation')
 FROM public.channel_video_inventory_scans active WHERE active.scan_id=d.active_scan_id AND active.status='running'),
 (""" + MANUAL_PENDING_SQL + """) OR d.full_reconcile_required_token IS DISTINCT FROM d.full_reconcile_served_token
 OR NOT EXISTS(SELECT 1 FROM public.channel_video_inventory_scans history
   WHERE history.channel_id=d.channel_id AND history.video_type=d.video_type
     AND history.status='completed' AND history.coverage='complete'
     AND history.scan_kind IN('initial_inventory','full_reconciliation')
     AND history.started_at>now()-make_interval(secs=>%(reconciliation_seconds)s::integer)
     AND (NOT EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled)
       OR history.discovery_channel_epoch=c.control_epoch)))"""
_HISTORY="""SELECT CASE WHEN %(initial_job_id)s::uuid IS NOT NULL THEN 'initial_inventory'
 WHEN NOT ("""+_AUTOMATIC_FULL_REQUIRED_SQL+""") THEN 'discovery'
 WHEN d.full_reconcile_required_token IS DISTINCT FROM d.full_reconcile_served_token
   OR d.inventory_status='completed' OR d.product_exposure_ready OR d.last_completed_scan_id IS NOT NULL
   OR EXISTS(SELECT 1 FROM public.channel_video_inventory_scans old
     WHERE old.channel_id=d.channel_id AND old.video_type=d.video_type AND old.status='completed'
       AND old.coverage='complete' AND old.scan_kind IN('initial_inventory','full_reconciliation'))
 THEN 'full_reconciliation' ELSE 'initial_inventory' END AS scan_kind
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(source)s::jsonb) d
 JOIN public.channel_collection_state c USING(channel_id)"""
_RESULT="""SELECT d.channel_id,d.video_type,d.playlist_id,%(token)s::uuid AS lease_token,
 %(expires)s::timestamptz AS lease_expires_at,d.no_new_streak,s.scan_id AS active_scan_id,
 s.resume_page_token,s.scan_boundary_at,%(epoch)s::bigint AS lease_channel_epoch,
 %(scan_kind)s::text AS scan_kind,
 coalesce(d.manual_list_check->'epoch'=to_jsonb(%(epoch)s::bigint)
 AND d.manual_list_check->>'state' IN ('queued','running'),false) AS manual_requested
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(source)s::jsonb) d
 CROSS JOIN jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s"""
_INITIAL_INPUT="""SELECT %s::uuid::text AS job_id,%s::uuid::text AS job_lease_token,
 %s::text AS worker,%s::text AS run_id,%s::integer AS lease_seconds"""
_CHANNEL_ID="SELECT channel_id FROM public.operator_jobs WHERE id=%s::uuid"
_CHANNEL="SELECT channel_id FROM public.channels WHERE channel_id=%s::text FOR UPDATE"
_JOB="SELECT to_jsonb(j)::text AS job FROM public.operator_jobs j WHERE id=%s::uuid FOR UPDATE"
_JOB_CHECK=("""SELECT j.lease_channel_epoch AS epoch,j.inventory_video_type AS video_type,
 j.initial_scope_version<>1 OR j.status<>'running' OR j.phase<>'inventory'
 OR j.lease_token IS DISTINCT FROM %(job_lease_token)s::uuid OR j.lease_expires_at<=clock_timestamp()
 OR j.lease_channel_epoch IS NULL OR j.accepted_epoch IS DISTINCT FROM j.lease_channel_epoch
 OR NOT coalesce((SELECT v.typed_runtime_eligible
  FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ v WHERE v.channel_id=%(channel_id)s::text),false)
 OR NOT EXISTS(SELECT 1 FROM public.channel_collection_state c WHERE c.channel_id=%(channel_id)s::text
  AND c.initial_job_id=j.id AND c.control_epoch=j.accepted_epoch)
 OR j.inventory_checkpoint->>'phase' IS DISTINCT FROM 'prepared'
 OR j.inventory_checkpoint->>'job_lease_token' IS DISTINCT FROM %(job_lease_token)s::uuid::text
 OR j.inventory_checkpoint->>'reserved_units' IS DISTINCT FROM '1' AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j""")
_PLAYLIST="""SELECT to_jsonb(d)::text AS playlist FROM public.channel_playlist_state d
 WHERE channel_id=%(channel_id)s::text AND video_type=%(video_type)s::text FOR UPDATE"""
_PROOF="""SELECT (proof->>'scan_id')::uuid::text AS own_id,proof::text AS proof FROM
 (SELECT j.summary_json->'full_inventory_sources'->j.inventory_video_type AS proof
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j) source"""
_WAIT="""SELECT d.active_scan_id::text AS active_scan_id,
 (d.status='leased' AND d.lease_expires_at>clock_timestamp())
 OR (%(own_id)s::uuid IS NOT NULL AND d.last_error_code IS NOT NULL AND d.next_due_at>clock_timestamp())
 OR NOT CASE WHEN d.last_error_code IN('youtube_daily_quota_exceeded_error','youtube_quota_exhausted') THEN
  (SELECT coalesce(d.next_due_at,'infinity'::timestamptz)<=at
   AND coalesce(d.source_next_after,'infinity'::timestamptz)<=at FROM(SELECT clock_timestamp() AS at) clock)
  ELSE true END AS wait,
 d.active_scan_id IS DISTINCT FROM %(own_id)s::uuid OR %(proof)s::jsonb->>'terminal_outcome' IS NOT NULL AS owned_invalid
 FROM jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) d"""
_SCAN="SELECT to_jsonb(s)::text AS scan FROM public.channel_video_inventory_scans s WHERE scan_id=%s::uuid FOR UPDATE"
_FOREIGN="""SELECT s.channel_id IS DISTINCT FROM %(channel_id)s::text OR s.video_type IS DISTINCT FROM %(video_type)s::text
 OR s.status<>'running' AS invalid FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s"""
_COVERAGE="""SELECT s.status<>'running' OR s.pages_completed IS DISTINCT FROM (%(proof)s::jsonb->>'acked_pages')::integer
 OR s.resume_page_token IS DISTINCT FROM d.resume_page_token AS invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s
 CROSS JOIN jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) d"""
_ABANDON="""UPDATE public.channel_video_inventory_scans SET status='abandoned',coverage='partial',completed_at=NULL,resume_page_token=NULL,
 error_code='full_initial_generation_replaced',error_at=clock_timestamp(),updated_at=clock_timestamp() WHERE scan_id=%s::uuid"""
_CLEAR="""UPDATE public.channel_playlist_state d SET active_scan_id=NULL,resume_page_token=NULL,resume_started_at=NULL,scan_boundary_at=NULL,
 inventory_status=CASE WHEN d.product_exposure_ready OR d.last_completed_scan_id IS NOT NULL OR d.inventory_status='completed'
 THEN 'completed' ELSE 'pending' END,updated_at=clock_timestamp()
 WHERE d.channel_id=%(channel_id)s::text AND d.video_type=%(video_type)s::text"""


def _fail(code):raise DatabaseServerError(code,code='P0001')


def _json(value):return value if value is None or isinstance(value,Jsonb) else Jsonb(value)


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        with database.transaction(Operation(name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


# Opt-in single-chain query only. The scalar/automatic query above is unchanged
# and never references the new table before its schema is accepted.
_CHAIN_CANDIDATES = _CANDIDATES.replace(
    'AND j.inventory_video_type=d.video_type',
    """AND j.summary_json->'inventory_chain_version'='1'::jsonb
    AND EXISTS(SELECT 1 FROM public.operator_job_inventory_chains chain
     WHERE chain.job_id=j.id AND chain.channel_id=j.channel_id AND chain.video_type=d.video_type
      AND chain.terminal_outcome IS NULL
      AND chain.checkpoint=jsonb_build_object('phase','prepared',
       'job_lease_token',%(initial_job_lease_token)s::uuid,
       'transition_id',%(chain_transition)s::uuid,'reserved_units',1))""")


@contextmanager
def _signal_candidates(transaction, args):
    # Narrow idle discovery uses the same claim guards and one bounded lock.
    statement = _CANDIDATES.replace(' ORDER BY d.dispatch_ordinal',
        ' AND (' + _SIGNAL_DUE + ') ORDER BY d.dispatch_ordinal')
    yield transaction.fetch_all(statement, args, max_rows=args['claim_limit'])


@contextmanager
def _chain_candidates(transaction, args):
    if (args['claim_limit'] != 1 or args['initial_job_id'] is None
            or args['initial_job_lease_token'] is None
            or args['channel_ids'] is None or len(args['channel_ids']) != 1
            or len(args['video_types']) != 1):
        _fail('typed_inventory_claim_limit_invalid')
    row = transaction.fetch_one(_CHAIN_CANDIDATES, args)
    yield [] if row is None else [row]


def claim_inventory_states(transaction,limit,channel_ids,video_types,worker_id,run_id,lease_seconds=900,
                           expected_channel_scopes=None,only_due=True,continuations=None,include_due=True,
                           initial_job_id=None,initial_job_lease_token=None,*,require_continuations=False,chain_transition=None,signals_only=False):
    """Borrow a transaction; private callers get the same finite twelve-arg core."""
    if type(signals_only) is not bool:
        raise ValueError("inventory_signal_scope_invalid")
    with transaction.borrow():
        tx=transaction
        args=tx.fetch_one(_INPUT,(limit,channel_ids,video_types,worker_id,run_id,lease_seconds,_json(expected_channel_scopes),
            only_due,_json(continuations),include_due,initial_job_id,initial_job_lease_token))
        # claim_inventory_pass's public null envelope check precedes core validation.
        if require_continuations and (args['continuations_text'] is None or args['include_due'] is None):_fail('inventory_continuations_invalid')
        for key,code in (('limit','claim_limit'),('channels','channel_ids'),('types','video_types'),('owner','claim_owner'),('lease','claim_lease')):
            if args[key+'_invalid'] is True:_fail('typed_inventory_'+code+'_invalid')
        if args['scopes_text'] is not None and tx.fetch_one(_SCOPES,args)['invalid'] is True:_fail('channel_collection_scope_invalid')
        if args['include_due'] is None or (args['include_due'] is False and args['continuations_text'] is None):_fail('inventory_continuations_invalid')
        if args['continuations_text'] is not None:
            if (args['only_due'] is False or args['channel_ids'] is not None or args['scopes_text'] is not None
                    or tx.fetch_one(_CONTINUATIONS,args)['invalid'] is True):_fail('inventory_continuations_invalid')
        schedules=require_cadence_snapshot(tx).inventory_by_type
        rows=[]
        scopes={}
        # Native FOR-query prefetch locks10, then50; do not lock all500 eagerly.
        args['chain_transition']=chain_transition
        with (_signal_candidates(tx,args) if signals_only else tx.inventory_claim_candidates(args) if chain_transition is None
              else _chain_candidates(tx,args)) as candidates:
            for candidate in candidates:
                values={**args,**candidate}
                claimed=tx.returning_one(_CLAIM,values)
                if claimed is None:continue
                active=tx.fetch_one(_ACTIVE_SCAN,values)
                if active is None:
                    values['reconciliation_seconds']=getattr(schedules,candidate['video_type']).reconciliation_seconds
                    active={'scan':None,**tx.fetch_one(_HISTORY,values)}
                rows.append(application_value(tx.fetch_one(_RESULT,{**values,**claimed,**active})))
                scopes[candidate['channel_id']]=candidate['epoch']
        expected=args['scopes'] if args['scopes_text'] is not None else [dict(channel_id=key,control_epoch=value) for key,value in sorted(scopes.items())]
        assert_channel_scopes(tx,expected)
        return rows


# Scheduled playlist claims take only the pass's formats; a
# turned-off format's due playlist stays as it is, unread and unleased.
def _scheduled_types(video_types):
    types=['long','short','stream'] if video_types is None else [kind for kind in ('long','short','stream') if kind in video_types]
    if video_types is not None and (not isinstance(video_types,(list,tuple)) or len(types)!=len(video_types) or not types):
        _fail('typed_inventory_video_types_invalid')
    return types


def claim_due_typed_inventory(database,limit,worker_id,run_id,lease_seconds=900,*,transaction=None,video_types=None):
    with _transaction(database,transaction,'claim_due_typed_inventory') as tx:
        return claim_inventory_states(tx,limit,None,_scheduled_types(video_types),worker_id,run_id,lease_seconds)


def claim_inventory_pass(database,continuations,limit,worker_id,run_id,lease_seconds=900,include_due=True,*,transaction=None,video_types=None,signals_only=False):
    with _transaction(database,transaction,'claim_inventory_pass') as tx:
        return claim_inventory_states(tx,limit,None,_scheduled_types(video_types),worker_id,run_id,lease_seconds,
            continuations=continuations,include_due=include_due,require_continuations=True,signals_only=signals_only)


def claim_typed_inventory_channels(database,channel_ids,video_types,worker_id,run_id,lease_seconds=900,
                                   expected_channel_scopes=None,*,transaction=None,only_due=False,signals_only=False):
    if type(only_due) is not bool:
        raise ValueError('typed_inventory_due_scope_invalid')
    if only_due:
        if (not isinstance(channel_ids,(list,tuple)) or len(channel_ids)!=1
                or not isinstance(video_types,(list,tuple)) or len(video_types)!=1
                or video_types[0] not in ('long','short','stream')):
            raise ValueError('typed_inventory_due_scope_invalid')
        canonical_channel_id(channel_ids[0])
        expected_channel_scopes=validate_channel_scopes(expected_channel_scopes,channel_ids=channel_ids)
    with _transaction(database,transaction,'claim_typed_inventory_channels') as tx:
        return claim_inventory_states(tx,500,channel_ids,video_types,worker_id,run_id,lease_seconds,
            expected_channel_scopes,only_due,signals_only=signals_only)


def claim_initial_inventory_source(database,job_id,job_lease_token,worker_id,run_id,lease_seconds=900,*,transaction=None):
    with _transaction(database,transaction,'claim_initial_inventory_source') as tx:
        args=tx.fetch_one(_INITIAL_INPUT,(job_id,job_lease_token,worker_id,run_id,lease_seconds))
        channel=tx.fetch_one(_CHANNEL_ID,(args['job_id'],))
        if channel is None:_fail('operator_job_lease_lost')
        args.update(channel)
        tx.fetch_one(_CHANNEL,(args['channel_id'],))
        job=tx.fetch_one(_JOB,(args['job_id'],))
        if job is None:_fail('operator_job_lease_lost')
        args.update(job)
        held=tx.fetch_one(_JOB_CHECK,args)
        if held['invalid'] is True:_fail('operator_job_lease_lost')
        args.update(held)
        assert_full_initial_scope(tx,args['job'],False)
        return claim_prepared_initial_source(tx,args)


def claim_prepared_initial_source(transaction,args,*,chain_transition=None):
    """Borrow the already locked/checked parent (and optional exact chain)."""
    tx=transaction
    with tx.borrow():
        scopes=[dict(channel_id=args['channel_id'],control_epoch=args['epoch'])]
        source=tx.fetch_one(_PLAYLIST,args)
        if source is None:_fail('initial_inventory_discovery_state_missing')
        args.update(source)
        args.update(tx.fetch_one(_PROOF,args))
        waiting=tx.fetch_one(_WAIT,args)
        if waiting['wait'] is True:
            assert_channel_scopes(tx,scopes)
            return []
        if args['own_id'] is None:
            if waiting['active_scan_id'] is not None:
                scan=tx.fetch_one(_SCAN,(waiting['active_scan_id'],))
                if scan is None:_fail('full_initial_foreign_scan_invalid')
                args.update(scan)
                if tx.fetch_one(_FOREIGN,args)['invalid'] is True:_fail('full_initial_foreign_scan_invalid')
                tx.execute(_ABANDON,(waiting['active_scan_id'],))
                tx.execute(_CLEAR,args)
        else:
            if waiting['owned_invalid'] is True:_fail('full_initial_owned_scan_unavailable')
            scan=tx.fetch_one(_SCAN,(args['own_id'],))
            args['scan']=scan['scan'] if scan else None
            if tx.fetch_one(_COVERAGE,args)['invalid'] is True:_fail('full_initial_source_coverage_invalid')
        extra={} if chain_transition is None else dict(chain_transition=chain_transition)
        return claim_inventory_states(tx,1,[args['channel_id']],[args['video_type']],args['worker'],args['run_id'],
            args['lease_seconds'],scopes,False,None,True,args['job_id'],args['job_lease_token'],**extra)
