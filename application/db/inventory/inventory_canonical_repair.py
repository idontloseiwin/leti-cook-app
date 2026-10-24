"""Canonical type repair under a fixed evidence lock set.

The expected count/fingerprint protects the reviewed preview. Actual native
source receipts feed queue/review effects under the original lock set. The
writer neither deletes profiles/snapshots nor changes canonical channel
identity.
"""
from contextlib import contextmanager
from ..base.operation_contracts import get_operation
from .inventory_canonical_preview import _CANDIDATES,inspect_typed_inventory_canonical_repair
from ..base.postgres import DatabaseServerError,Operation

_INPUT = "SELECT %s::integer AS expected_count,%s::text AS fingerprint"
_FINGERPRINT = "SELECT %s::text ~ '^[0-9a-f]{32}$' AS valid"
_CANDIDATE_IDS = "WITH candidates AS ("+_CANDIDATES+""" )
 SELECT array_agg(video_id ORDER BY video_id) AS video_ids,
 array_agg(authoritative_video_type ORDER BY video_id) AS video_types FROM candidates"""
_QUEUE_COUNT = "SELECT count(*)::integer AS count FROM public.video_collection_state WHERE video_id=ANY(%s::text[])"
_UPDATE = """UPDATE public.video_inventory inventory SET video_type=target.video_type,skeleton_since=NULL,updated_at=now()
 FROM unnest(%s::text[],%s::text[]) target(video_id,video_type)
 WHERE inventory.video_id=target.video_id AND(inventory.video_type IS NULL OR inventory.video_type='long')
 AND inventory.video_type IS DISTINCT FROM target.video_type"""
_UPDATE_EFFECT = _UPDATE.replace(' FROM unnest(', ' FROM public.video_inventory effect_previous,unnest(',1)+" AND effect_previous.video_id=inventory.video_id RETURNING to_jsonb(effect_previous)::text AS old_text,to_jsonb(inventory)::text AS new_text"
_UNRESOLVED = """SELECT count(*)::integer AS count FROM public.video_inventory_type_resolution
 WHERE video_id=ANY(%s::text[]) AND resolution_status<>'resolved'"""
_REMAINS = "SELECT EXISTS(SELECT 1 FROM public.video_collection_state WHERE video_id=ANY(%s::text[])) AS present"


def _fail(code):
    raise DatabaseServerError(code,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        spec=get_operation('repair_typed_inventory_canonical_types')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as owned:
            yield owned


def repair_typed_inventory_canonical_types(database,expected_count,expected_fingerprint,*,transaction=None):
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(expected_count,expected_fingerprint))
        count=args['expected_count']
        if count is None or not 1<=count<=5000:
            _fail('typed_inventory_canonical_repair_expected_count_invalid')
        if tx.fetch_one(_FINGERPRINT,(args['fingerprint'],))['valid'] is not True:
            _fail('typed_inventory_canonical_repair_fingerprint_invalid')
        tx.lock_typed_inventory_repair()
        plan=inspect_typed_inventory_canonical_repair(database,0,transaction=tx)
        if plan['status']!='ready':
            _fail('typed_inventory_canonical_repair_not_ready')
        if plan['repairable_count']!=count or plan['candidate_fingerprint']!=args['fingerprint']:
            _fail('typed_inventory_canonical_repair_plan_changed')
        candidates=tx.fetch_one(_CANDIDATE_IDS)
        ids,types=candidates['video_ids'],candidates['video_types']
        if len(ids or [])!=count or len(ids or [])!=len(types or []):
            _fail('typed_inventory_canonical_repair_candidate_drift')
        queue_rows=tx.fetch_one(_QUEUE_COUNT,(ids,))['count']
        receipts=tx.returning_all(_UPDATE_EFFECT,(ids,types),max_rows=5000)
        updated=len(receipts)
        if updated!=count:
            _fail('typed_inventory_canonical_repair_update_count_mismatch')
        from .canonical_repair_effects import after_canonical_repair_statement
        after_canonical_repair_statement(tx,receipts)
        if tx.fetch_one(_UNRESOLVED,(ids,))['count']!=0:
            _fail('typed_inventory_canonical_repair_postcondition_failed')
        if tx.fetch_one(_REMAINS,(ids,))['present']:
            _fail('typed_inventory_canonical_repair_collection_state_remains')
        return dict(status='completed',schema_version=2,updated=updated,collection_state_rows_removed=queue_rows,
            canonical_channel_mutation=False,videos_deleted=0,snapshots_deleted=0,youtube_requests=0,
            runtime_activation=False,product_exposure_changed=False)
