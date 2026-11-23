"""Bounded manual-review readers.

Two statements for validated pages, one for get/has-open, maximum101 rows.
Native UUID/integer casts and JSONB projection preserve the retained contracts.
No stored business-function calls, per-row database calls or provider clocks.
"""
from contextlib import contextmanager
from hot_storage import iso_utc
from ..base.operation_contracts import get_operation
from ..base.repository import DatabaseRepository
from ..base.postgres import DatabaseError,DatabaseServerError,Operation

CASE_FIELDS=frozenset(('case_id','source_detached_at','video_id','channel_id','canonical_type',
    'observed_types','review_status','state','revision','first_seen_at','last_seen_at','can_decide','blocked_reason','decision'))
HISTORY_FIELDS=frozenset(('revision','previous_type','video_type','reason','decided_at','request_id'))
# Current checks are derived from protected, fresh membership evidence. Stored
# open cases are history, not proof that a contradiction still needs a person.
_REVIEW_STATUS = """CASE
 WHEN c.source_detached_at IS NOT NULL THEN 'history'
 WHEN r.present_type_count>0 AND r.source_expires_at IS NOT NULL AND r.source_expires_at>statement_timestamp()
 AND (r.present_channel_count>1 OR r.present_membership_channels<>ARRAY[c.channel_id]::text[]
 OR c.decision_unblocked IS FALSE)
 THEN 'channel_conflict'
 WHEN c.decision_type IS NOT NULL THEN 'history'
 WHEN r.source_expires_at IS NULL OR r.source_expires_at<=statement_timestamp()
 OR r.present_type_count=0 THEN 'awaiting_refresh'
 WHEN r.resolution_status='resolved' THEN 'confirmed'
 WHEN r.present_membership_types=ARRAY['long','short']::text[]
 AND (i.video_type IS NULL OR i.video_type IN('long','short')) THEN 'awaiting_refresh'
 WHEN r.present_type_count>1 THEN 'needs_review'
 ELSE 'awaiting_refresh' END"""
_CASE_PRESENTATION = """
 CROSS JOIN LATERAL (SELECT """+_REVIEW_STATUS+""" AS review_status) presentation
 CROSS JOIN LATERAL (SELECT c.decision_unblocked AND
 (presentation.review_status='needs_review' OR c.decision_type IS NOT NULL)
 AND presentation.review_status<>'channel_conflict' AS can_decide) permission"""
_CASE_FROM = """ FROM readable.video_type_review_cases c
 LEFT JOIN readable.video_inventory i ON i.video_id=c.video_id
 LEFT JOIN readable.video_inventory_type_resolution r ON r.video_id=c.video_id"""+_CASE_PRESENTATION
CASE_JSON="""jsonb_build_object('case_id',c.case_id,'source_detached_at',c.source_detached_at,
 'video_id',c.video_id,'channel_id',c.channel_id,'canonical_type',case when c.video_id is null then null when c.state='resolved' then c.decision_type else i.video_type end,
 'observed_types',coalesce(r.present_membership_types,ARRAY[]::text[]),
 'review_status',presentation.review_status,'state',c.state,'revision',c.revision,
 'first_seen_at',c.first_seen_at,'last_seen_at',c.last_seen_at,
 'can_decide',permission.can_decide,
 'blocked_reason',case when permission.can_decide then null
 when presentation.review_status='channel_conflict' then 'channel_conflict' else 'source_unavailable' end,
 'decision',case when c.decision_type is null then null else jsonb_build_object(
 'video_type',c.decision_type,'reason',c.decision_reason,'decided_at',c.decision_at,'revision',c.decision_revision) end)"""
_CASE_SELECT='SELECT '+CASE_JSON+' AS value,least(r.source_expires_at,i.source_expires_at) AS source_expires_at'+_CASE_FROM
_GET=_CASE_SELECT+' WHERE c.case_id=%s::uuid'
_PAGE_INPUT="SELECT %s::text AS state,%s::integer AS page_limit,%s::uuid::text AS after_id"
_LIST=_CASE_SELECT+"""
 WHERE (%(state)s::text='all'
 OR (%(state)s::text='open' AND presentation.review_status IN('needs_review','channel_conflict'))
 OR (%(state)s::text='awaiting' AND presentation.review_status='awaiting_refresh')
 OR (%(state)s::text='resolved' AND c.decision_type IS NOT NULL))
 AND (%(after_id)s::uuid IS NULL OR c.case_id>%(after_id)s::uuid)
 ORDER BY c.case_id LIMIT %(page_limit)s::integer"""
_HISTORY_INPUT="SELECT %s::uuid::text AS case_id,%s::integer AS page_limit,%s::bigint AS before_revision"
_HISTORY="""SELECT jsonb_build_object('revision',h.revision,'previous_type',
 (SELECT prior.video_type FROM public.video_type_review_history prior WHERE prior.case_id=h.case_id
  AND prior.revision<h.revision ORDER BY prior.revision DESC LIMIT 1),
 'video_type',h.video_type,'reason',h.reason,'decided_at',h.decided_at,'request_id',h.request_id) AS value
 FROM public.video_type_review_history h WHERE h.case_id=%(case_id)s::uuid
 AND (%(before_revision)s::bigint IS NULL OR h.revision<%(before_revision)s::bigint)
 ORDER BY h.revision DESC LIMIT %(page_limit)s::integer"""
# The summary starts from the small durable case inventory, then asks the same
# protected views for each exact case/video. OFFSET 0 retains the correlation:
# without it the planner can resolve every catalogue video's type before joining
# the handful of cases. User-work fields are identities here, never evidence.
_HAS_FROM = """ FROM readable.video_type_user_work candidate
 CROSS JOIN LATERAL (SELECT current_case.* FROM readable.video_type_review_cases current_case
 WHERE current_case.case_id=candidate.case_id OFFSET 0) c
 LEFT JOIN LATERAL (SELECT inventory.* FROM readable.video_inventory inventory
 WHERE inventory.video_id=c.video_id OFFSET 0) i ON true
 LEFT JOIN LATERAL (SELECT resolution.* FROM readable.video_inventory_type_resolution resolution
 WHERE resolution.video_id=c.video_id OFFSET 0) r ON true"""+_CASE_PRESENTATION
_HAS="SELECT EXISTS(SELECT 1"+_HAS_FROM+" WHERE presentation.review_status IN('needs_review','channel_conflict') LIMIT 1) AS value"
_HIDDEN = """WITH args AS (SELECT %s::text AS channel_id,%s::text AS after_id,%s::integer AS size),
 page AS MATERIALIZED(SELECT i.video_id,i.channel_id FROM readable.retained_video_identities i CROSS JOIN args
 WHERE i.is_skeleton AND (args.channel_id IS NULL OR i.channel_id=args.channel_id)
 AND (args.after_id IS NULL OR i.video_id>args.after_id) ORDER BY i.video_id
 LIMIT (SELECT CASE WHEN size=0 THEN 0 ELSE size+1 END FROM args)),
 shown AS (SELECT * FROM page ORDER BY video_id LIMIT (SELECT size FROM args))
 SELECT jsonb_build_object('contract_version',1,
 'count',(SELECT count(*) FROM readable.retained_video_identities i WHERE i.is_skeleton
 AND (args.channel_id IS NULL OR i.channel_id=args.channel_id)),
 'videos',coalesce((SELECT jsonb_agg(to_jsonb(shown) ORDER BY video_id) FROM shown),'[]'::jsonb),
 'next_cursor',CASE WHEN (SELECT count(*) FROM page)>args.size THEN (SELECT max(video_id) FROM shown) END) AS value
 FROM args"""


def read_hidden_video_identities(database, *, channel_id=None, after_id=None, limit=50):
    """Only preserved identities; no borrowed title, type or thumbnail."""
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError('hidden_video_limit_invalid')
    return database.read_row('hidden_video_identities', _HIDDEN, (channel_id, after_id, limit))['value']

@contextmanager
def _read(database,name):
    spec=get_operation(name)
    with database.transaction(Operation(name,read_only=True,statement_timeout_ms=spec.statement_timeout_ms,
            lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:
        yield tx

def _value(row,fields):
    expected={'value','source_expires_at'} if fields is CASE_FIELDS else {'value'}
    if not isinstance(row,dict) or set(row)!=expected or not isinstance(row['value'],dict) or set(row['value'])!=fields:
        raise DatabaseError('database_result_shape_invalid')
    DatabaseRepository(None).admit_source_deadlines([row])
    if fields is CASE_FIELDS:
        deadline = row['source_expires_at']
        return {**row['value'], 'source_expires_at': iso_utc(deadline) if deadline is not None else None}
    return row['value']

def get_case_in_transaction(tx,case_id):
    row=tx.fetch_one(_GET,(case_id,))
    return None if row is None else _value(row,CASE_FIELDS)

def get_video_type_review_case(database,case_id):
    with _read(database,'get_video_type_review_case') as tx:return get_case_in_transaction(tx,case_id)

def list_video_type_review_cases(database,state,limit,after_case_id=None):
    with _read(database,'list_video_type_review_cases') as tx:
        args=tx.fetch_one(_PAGE_INPUT,(state,limit,after_case_id))
        if args['state'] not in ('open','awaiting','resolved','all') or args['page_limit'] is None or not 1<=args['page_limit']<=101:
            raise DatabaseServerError('invalid_video_type_review_page',code='22023')
        return [_value(row,CASE_FIELDS) for row in tx.fetch_all(_LIST,args,max_rows=args['page_limit'])]

def list_video_type_review_history(database,case_id,limit,before_revision=None):
    with _read(database,'list_video_type_review_history') as tx:
        args=tx.fetch_one(_HISTORY_INPUT,(case_id,limit,before_revision))
        if args['page_limit'] is None or not 1<=args['page_limit']<=101 or (args['before_revision'] is not None and args['before_revision']<1):
            raise DatabaseServerError('invalid_video_type_review_history_page',code='22023')
        return [_value(row,HISTORY_FIELDS) for row in tx.fetch_all(_HISTORY,args,max_rows=args['page_limit'])]

def has_open_video_type_reviews(database):
    with _read(database,'has_open_video_type_reviews') as tx:
        row=tx.fetch_one(_HAS)
        if not isinstance(row,dict) or set(row)!={'value'} or type(row['value']) is not bool:
            raise DatabaseError('database_result_shape_invalid')
        return row['value']
