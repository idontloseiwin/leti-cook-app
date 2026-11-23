"""Borrowed full-initial scope proofs and canonical target resolution.

The caller owns its transaction/job lock and final collection fence. These read
proofs retain native composite/JSONB parsing, at most three source entries in
PostgreSQL JSONB order, and fresh source-clock checks at each retained
boundary. Exceptions poison the borrowed transaction.
"""
from psycopg.types.json import Jsonb
from ..inventory.inventory_scan_steps import _hot
from ..base.postgres import DatabaseServerError
from ..videos.video_queue_initialization import LONG_SHORT_CONFLICT_SQL

_INPUT="""WITH input AS MATERIALIZED(SELECT %s::boolean AS require_frozen,%s::jsonb AS job),
 job AS MATERIALIZED(SELECT j.* FROM input CROSS JOIN LATERAL jsonb_populate_record(NULL::public.operator_jobs,input.job) j),
 entries AS MATERIALIZED(SELECT e.key,e.value,e.ordinality FROM job j CROSS JOIN LATERAL jsonb_each(
 CASE WHEN (j.initial_scope_version=1 OR j.initial_scope_version IS NULL) AND jsonb_typeof(j.summary_json->'full_inventory_sources')='object'
 AND (j.summary_json->'full_inventory_sources')-ARRAY['long','short','stream']::text[]='{}'::jsonb
 THEN j.summary_json->'full_inventory_sources' ELSE '{}'::jsonb END) WITH ORDINALITY e),
 parsed AS MATERIALIZED(SELECT e.*,
 (pg_input_error_info(e.value->>'scan_id','uuid')).sql_error_code AS scan_error,
 (pg_input_error_info(e.value->>'last_transition_id','uuid')).sql_error_code AS receipt_error,
 CASE WHEN pg_input_is_valid(e.value->>'scan_id','uuid') THEN (e.value->>'scan_id')::uuid END AS scan_id,
 CASE WHEN pg_input_is_valid(e.value->>'last_transition_id','uuid') THEN (e.value->>'last_transition_id')::uuid END AS transition_id FROM entries e)
 SELECT to_jsonb(j)::text AS job,j.initial_scope_version AS version,
 j.summary_json ?| ARRAY['source_scope_expired','inventory_checkpoint_expired'] AS expired,
 to_jsonb(j.requested_at)#>>'{}' AS requested_at,input.require_frozen,
 coalesce((SELECT jsonb_agg(jsonb_build_object('key',e.key,'value',e.value::text,'terminal',e.value->>'terminal_outcome',
  'scan_error',e.scan_error,'receipt_error',e.receipt_error,'scan',CASE WHEN s.scan_id IS NOT NULL THEN to_jsonb(s)::text END,
  'receipt',CASE WHEN r.transition_id IS NOT NULL THEN to_jsonb(r)::text END) ORDER BY e.ordinality)
 FROM parsed e LEFT JOIN public.channel_video_inventory_scans s ON s.scan_id=e.scan_id
 LEFT JOIN public.typed_inventory_receipts r ON r.receipt_scope='scan_transition' AND r.transition_id=e.transition_id),'[]'::jsonb) AS sources,
 EXISTS(SELECT 1 FROM public.operator_job_targets t WHERE t.job_id=j.id AND t.video_type IS NULL) AS unresolved
 FROM job j CROSS JOIN input"""
_SHAPE="""SELECT j.job_type<>'channel_initial_long_inventory' OR j.initial_video_types<>ARRAY['long','short','stream']::text[]
 OR j.max_items IS NOT NULL OR j.max_pages IS NOT NULL OR j.max_quota_units IS NOT NULL
 OR jsonb_typeof(j.summary_json->'full_inventory_sources') IS DISTINCT FROM 'object'
 OR (j.summary_json->'full_inventory_sources')-ARRAY['long','short','stream']::text[]<>'{}'::jsonb AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_SCAN="""SELECT to_jsonb(s)::text AS value,
 s.channel_id IS DISTINCT FROM j.channel_id OR s.video_type IS DISTINCT FROM %(kind)s::text
 OR s.scan_kind NOT IN ('initial_inventory','full_reconciliation') OR s.started_at<j.requested_at AS invalid,
 to_jsonb(s.started_at)#>>'{}' AS started_at,to_jsonb(s.scan_boundary_at)#>>'{}' AS boundary
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_IDENTITY="""SELECT s.started_at IS DISTINCT FROM (%(entry)s::jsonb->>'started_at')::timestamptz
 OR s.scan_boundary_at IS DISTINCT FROM (%(entry)s::jsonb->>'scan_boundary_at')::timestamptz
 OR coalesce(%(entry)s::jsonb->>'acked_pages','') !~ '^[0-9]+$' AS invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s"""
_SOURCE="SELECT to_jsonb((%s::jsonb->>'source_checked_at')::timestamptz)#>>'{}' AS at"
_COVERAGE="""SELECT (%(entry)s::jsonb->>'acked_pages')::integer IS DISTINCT FROM s.pages_completed
 OR (%(entry)s::jsonb->>'terminal_outcome'='completed' AND (s.status<>'completed' OR s.coverage<>'complete'))
 OR %(entry)s::jsonb->>'terminal_outcome' NOT IN ('completed','playlist_not_found') AS invalid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) s"""
_RECEIPT="""SELECT EXISTS(SELECT 1 FROM jsonb_populate_record(NULL::public.typed_inventory_receipts,%(receipt)s::jsonb) r
 WHERE r.receipt_scope='scan_transition' AND r.transition_id=(%(entry)s::jsonb->>'last_transition_id')::uuid
 AND r.scan_id=(%(scan)s::jsonb->>'scan_id')::uuid AND NOT(r.result ? 'source_expired')
 AND r.source_checked_at=(%(entry)s::jsonb->>'source_checked_at')::timestamptz) AS valid"""
_FROZEN="""SELECT j.targets_frozen_at IS NULL OR j.phase NOT IN ('hydrate_videos','verify') OR %s::integer<>3
 OR %s::boolean AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_TYPE="""SELECT i.video_type FROM public.video_inventory i WHERE i.video_id=%(video_id)s::text AND i.channel_id=%(channel_id)s::text
 AND i.video_type IN ('long','short','stream')
 AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=i.video_id
 AND m.membership_status='present' AND m.channel_id<>%(channel_id)s::text)
 AND (NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=i.video_id
 AND m.channel_id=%(channel_id)s::text AND m.membership_status='present' AND m.video_type<>i.video_type)
 OR EXISTS(SELECT 1 FROM public.video_type_review_cases r WHERE r.video_id=i.video_id AND r.channel_id=%(channel_id)s::text
 AND r.decision_type=i.video_type AND r.blocked_reason IS NULL)
 OR EXISTS(SELECT 1 FROM public.video_type_automatic_candidates automatic
 WHERE automatic.video_id=i.video_id AND automatic.channel_id=i.channel_id AND automatic.automatic_type=i.video_type)
 OR ("""+LONG_SHORT_CONFLICT_SQL+"""))"""


def _reject(code):raise DatabaseServerError(code,code='P0001')


def assert_full_initial_scope(transaction,job,require_frozen=False):
    """Validate a captured native job composite without acquiring a second owner."""
    tx=transaction
    with tx.borrow():
        encoded=job if isinstance(job,(str,Jsonb)) else Jsonb(job) if job is not None else None
        args=tx.fetch_one(_INPUT,(require_frozen,encoded))
        captured=args['job']
        if args['version'] is not None and args['version']!=1:return
        if args['expired'] is True:_reject('operator_job_source_scope_expired')
        _hot(tx,args['requested_at'])
        if tx.fetch_one(_SHAPE,(captured,))['invalid'] is True:_reject('full_initial_scope_invalid')
        entries=args['sources']
        for entry in entries:
            if entry['scan_error'] is not None:raise DatabaseServerError('database_operation_failed',code=entry['scan_error'])
            if entry['scan'] is None:_reject('full_initial_source_identity_invalid')
            parameters=dict(job=captured,kind=entry['key'],entry=entry['value'],scan=entry['scan'],receipt=entry['receipt'])
            scan=tx.fetch_one(_SCAN,parameters)
            if scan is None or scan['invalid'] is True:_reject('full_initial_source_identity_invalid')
            parameters['scan']=scan['value']
            if tx.fetch_one(_IDENTITY,parameters)['invalid'] is True:_reject('full_initial_source_identity_invalid')
            _hot(tx,scan['started_at'])
            _hot(tx,scan['boundary'])
            if entry['terminal'] is not None:
                source=tx.fetch_one(_SOURCE,(entry['value'],))['at']
                _hot(tx,source)
                if tx.fetch_one('SELECT %s::timestamptz<%s::timestamptz AS invalid',(source,scan['boundary']))['invalid'] is True:
                    _reject('full_initial_source_clock_invalid')
                if tx.fetch_one(_COVERAGE,parameters)['invalid'] is True:_reject('full_initial_source_coverage_invalid')
                if entry['receipt_error'] is not None:raise DatabaseServerError('database_operation_failed',code=entry['receipt_error'])
                if tx.fetch_one(_RECEIPT,parameters)['valid'] is not True:_reject('full_initial_source_coverage_invalid')
            elif args['require_frozen'] is True:_reject('full_initial_targets_not_frozen')
        if args['require_frozen'] is True and tx.fetch_one(_FROZEN,(len(entries),args['unresolved'],captured))['invalid'] is True:
            _reject('full_initial_targets_not_frozen')


def full_initial_target_type(transaction,channel_id,video_id):
    """Return canonical typed identity under the retained conflict/manual rules."""
    with transaction.borrow():
        row=transaction.fetch_one(_TYPE,dict(channel_id=channel_id,video_id=video_id))
        return row['video_type'] if row else None
