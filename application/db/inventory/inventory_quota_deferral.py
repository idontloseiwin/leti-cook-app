"""Quota deferral used by the direct typed-inventory repository.

The native transition lock serializes identical requests. Canonical row-by-row
locks retain the old post-wait clock and lease checks; no provider or automatic
retry occurs here. Scan/progress fields stay unchanged. Schema23 receipt rights are required;
the repository preserves its existing exact-transition acknowledgement retry.
"""
from contextlib import contextmanager
from datetime import datetime,timedelta
from psycopg.types.json import Jsonb
from collection_policy import youtube_quota_reset_at
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from .inventory_scan_steps import _HOT
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""WITH args AS MATERIALIZED(SELECT %s::uuid AS transition_id,
 %s::jsonb AS claims,%s::timestamptz AS source_checked_at)
 SELECT jsonb_build_object('transition_id',transition_id,'source_checked_at',source_checked_at) AS value,
 jsonb_typeof(claims) AS claims_type,
 CASE WHEN jsonb_typeof(claims)='array' THEN jsonb_array_length(claims) END AS claims_count FROM args"""
_VALID="""SELECT NOT EXISTS(SELECT 1 FROM jsonb_array_elements(%s::jsonb) requested(claim)
 WHERE jsonb_typeof(claim) IS DISTINCT FROM 'object' OR nullif(btrim(claim->>'channel_id'),'') IS NULL
 OR length(btrim(claim->>'channel_id'))>200 OR coalesce(claim->>'video_type','') NOT IN('long','short','stream')
 OR coalesce(claim->>'lease_token','') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') AS valid"""
_DUPLICATES="""SELECT EXISTS(SELECT 1 FROM jsonb_array_elements(%s::jsonb) requested(claim)
 GROUP BY btrim(claim->>'channel_id'),claim->>'video_type' HAVING count(*)>1) AS present"""
_CANONICAL="""SELECT jsonb_agg(jsonb_build_object('channel_id',btrim(claim->>'channel_id'),
 'video_type',claim->>'video_type','lease_token',(claim->>'lease_token')::uuid::text)
 ORDER BY btrim(claim->>'channel_id'),claim->>'video_type') AS claims FROM jsonb_array_elements(%s::jsonb) requested(claim)"""
_TRANSITION_LOCK="SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended(%s::uuid::text,781631247))"
_RECEIPT="""SELECT to_jsonb(receipt) AS value,receipt.claims IS NOT DISTINCT FROM %(claims)s::jsonb
 AND receipt.source_checked_at IS NOT DISTINCT FROM %(source_checked_at)s::timestamptz AS matches
 FROM public.typed_inventory_receipts receipt WHERE receipt.receipt_scope='quota_defer'
 AND receipt.transition_id=%(transition_id)s::uuid"""
_LOCK="""SELECT lease_channel_epoch FROM public.channel_playlist_state
 WHERE channel_id=%(channel_id)s::text AND video_type=%(video_type)s::text FOR UPDATE"""
_RELEASE="""UPDATE public.channel_playlist_state SET status='ready',
 next_due_at=greatest(coalesce(next_due_at,%(retry)s::timestamptz),%(retry)s::timestamptz),
 source_next_after=greatest(coalesce(source_next_after,%(retry)s::timestamptz),%(retry)s::timestamptz),
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,
 last_error_code='youtube_daily_quota_exceeded_error',updated_at=now()
 WHERE channel_id=%(channel_id)s::text AND video_type=%(video_type)s::text AND runtime_enabled
 AND status='leased' AND lease_token=%(lease_token)s::uuid AND lease_expires_at>clock_timestamp()"""
_RESULT="""SELECT jsonb_build_object('status','deferred','reason','youtube_daily_quota_exceeded',
 'transition_id',%(transition_id)s::uuid,'requested',%(requested)s::integer,'released',%(released)s::integer,
 'rejected',%(rejected)s::integer,'retry_not_before',%(retry)s::timestamptz,
 'active_scan_state_preserved',true,'replayed',false) AS value"""
_INSERT="""INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,claims,source_checked_at,result,created_at)
 VALUES('quota_defer',%(transition_id)s::uuid,%(claims)s::jsonb,%(source_checked_at)s::timestamptz,%(result)s::jsonb,now())"""
_SCOPES="""SELECT coalesce(jsonb_agg(scope ORDER BY scope->>'channel_id'),'[]'::jsonb) AS scopes
 FROM(SELECT DISTINCT value AS scope FROM jsonb_array_elements(%s::jsonb)) unique_scopes"""


def _fail(name):raise DatabaseServerError(name,code='P0001')


def _clock(tx,at):
    if tx.fetch_one(_HOT,(at,at))['valid'] is not True:
        raise DatabaseServerError('hot_source_clock_out_of_window',code='22023')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('defer_typed_inventory_claims_after_daily_quota')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as tx:yield tx


def defer_typed_inventory_claims_after_daily_quota(database,transition_id,claims,source_checked_at,*,transaction=None):
    """Return the durable deferral result after all writes and final scope checks."""
    with _transaction(database,transaction) as tx:
        raw=Jsonb(claims) if claims is not None else None
        header=tx.fetch_one(_INPUT,(transition_id,raw,source_checked_at))
        args=header['value']
        at=args['source_checked_at']
        _clock(tx,at)
        if args['transition_id'] is None:_fail('typed_inventory_quota_defer_transition_id_invalid')
        # The HOT check above already rejects NULL/nonfinite clocks, as in SQL.
        if header['claims_type']!='array' or not 1<=header['claims_count']<=500:
            _fail('typed_inventory_quota_defer_claims_invalid')
        if tx.fetch_one(_VALID,(raw,))['valid'] is not True:_fail('typed_inventory_quota_defer_claim_invalid')
        if tx.fetch_one(_DUPLICATES,(raw,))['present']:_fail('typed_inventory_quota_defer_claim_duplicate')
        canonical=tx.fetch_one(_CANONICAL,(raw,))['claims']
        args['claims']=Jsonb(canonical)
        # A validated HOT timestamp is recent and representable in datetime.
        observed=datetime.fromisoformat(at)
        args['retry']=max(observed+timedelta(hours=1),youtube_quota_reset_at(observed))
        tx.fetch_one(_TRANSITION_LOCK,(args['transition_id'],))
        receipt=tx.fetch_one(_RECEIPT,args)
        if receipt is not None:
            saved=receipt['value']
            if 'source_expired' in saved['result']:_fail('hot_inventory_receipt_source_expired')
            _clock(tx,saved['source_checked_at'])
            if receipt['matches'] is not True:_fail('typed_inventory_quota_defer_transition_id_reused')
            _clock(tx,at)
            # Keep the retained second receipt-clock pass before acknowledgement.
            _clock(tx,saved['source_checked_at'])
            return {**saved['result'],'replayed':True}
        released=0
        scopes=[]
        for claim in canonical:
            held=tx.fetch_one(_LOCK,claim)
            _clock(tx,at)
            if tx.execute(_RELEASE,{**claim,'retry':args['retry']}):
                released+=1
                scopes.append({'channel_id':claim['channel_id'],'control_epoch':held['lease_channel_epoch'] if held else None})
        args.update(requested=len(canonical),released=released,rejected=len(canonical)-released)
        result=tx.fetch_one(_RESULT,args)['value']
        tx.execute(_INSERT,{**args,'result':Jsonb(result)})
        _clock(tx,at)
        exact=tx.fetch_one(_SCOPES,(Jsonb(scopes),))['scopes']
        assert_channel_scopes(tx,exact)
        return result
