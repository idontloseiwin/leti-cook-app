"""Bounded queue backfill and addressed prioritization.

Python owns operation order, bounded IDs and final channel admission. Shared
typed-policy projections preserve native identity/source facts; intrinsic
integrity guards remain authoritative. No provider request or automatic retry
is introduced.
"""
from .typed_video_read_policy import (TYPED_CANDIDATE_INPUT_ID_SQL)
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, CHANNEL_CONTROL_SQL, channel_mode_row)
from contextlib import contextmanager
from .video_initial_policy import VIDEO_HANDOFF_STATE_SQL
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError, Operation
from .video_queue_initialization import ensure_video_collection_state_in_transaction
from .collection_policy import cadence_policy_version
from .video_queue_ensure import _LOCK_QUEUE, _LOCK_INVENTORY, _IDENTITY

_LIMIT = "SELECT %s::integer AS limit"
# Both physical scans retain the original predicate and snapshots. SQL evaluates
# joins and filtering; only the selected <=1000 IDs cross into Python.
_GAPS = ("""FROM public.video_inventory inventory
 LEFT JOIN public.video_collection_state state ON state.video_id=inventory.video_id
 LEFT JOIN public.videos video ON video.video_id=inventory.video_id
 WHERE inventory.video_type='long'
 AND state.availability_first_missing_at IS NULL AND state.availability_resume_observed_at IS NULL
 AND state.paused_reason IS DISTINCT FROM 'availability_retry_expired'
 AND EXISTS(SELECT 1 FROM public.channel_collection_state control
 WHERE control.channel_id=inventory.channel_id AND control.owner_permission='accepted' AND NOT control.is_hidden)
 AND (state.video_id IS NULL OR (state.paused_reason IN ('channel_hidden','channel_no_long_playlist',
 'channel_inventory_not_ready','channel_inventory_manual_required','channel_inventory_error')
 AND (EXISTS(SELECT 1 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ control
 WHERE control.channel_id=inventory.channel_id AND control.collection_mode='active')
 OR """ + VIDEO_HANDOFF_STATE_SQL + """))
 OR state.channel_id IS DISTINCT FROM inventory.channel_id
 OR state.stats_source_checked_at IS DISTINCT FROM video.checked_at
 OR state.metadata_source_checked_at IS DISTINCT FROM video.metadata_checked_at
 OR state.policy_version<%s::integer)""")
_SELECT = "SELECT inventory.video_id " + _GAPS + " ORDER BY inventory.video_id LIMIT %s::integer"
_REMAINING = "SELECT count(*)::bigint AS count " + _GAPS
_SCOPES = """SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
 'channel_id',inventory.channel_id,'control_epoch',control.control_epoch)),'[]'::jsonb) AS scopes
 FROM public.video_inventory inventory LEFT JOIN public.channel_collection_state control
 ON control.channel_id=inventory.channel_id WHERE inventory.video_id=ANY(%s::text[])"""
_PROGRESS = """UPDATE public.video_collection_control SET reconciliation_required=%s::boolean,
 last_reconciled_at=CASE WHEN %s::boolean THEN now() ELSE last_reconciled_at END,
 updated_at=now() WHERE singleton=true"""
_INPUT = """WITH args AS MATERIALIZED(SELECT %s::text[] AS ids,%s::jsonb AS scopes)
 SELECT args.*,cardinality(ids) AS count,EXISTS(SELECT 1 FROM unnest(ids) v(id)
 WHERE nullif(btrim(id),'') IS NULL) AS invalid_id FROM args"""
_LONG_IDS = ('SELECT inventory.video_id,(SELECT policy_facts FROM (' + CHANNEL_CONTROL_FACTS_SQL + ") control WHERE control.channel_id=channel.channel_id) AS policy_facts FROM public.video_inventory inventory JOIN public.channels channel ON channel.channel_id=inventory.channel_id WHERE inventory.video_id=ANY(%s::text[]) AND inventory.video_type='long'")
_PRIORITIZE_LONG = """UPDATE public.video_collection_state state SET metadata_due_at=now(),
 next_due_at=least(state.stats_due_at,now()),paused_reason=NULL,updated_at=now()
 WHERE state.video_id=ANY(%s::text[]) AND state.video_type='long' AND state.lease_token IS NULL
 AND state.availability_first_missing_at IS NULL AND state.availability_resume_observed_at IS NULL
 AND state.paused_reason IS DISTINCT FROM 'availability_retry_expired'"""
_TYPED_INPUT = "SELECT %s::text AS video_type,ids,cardinality(ids) AS count FROM (SELECT %s::text[] AS ids) args"
_PRIORITIZE_TYPED = ("""INSERT INTO public.video_collection_state AS state(video_id,channel_id,video_type,
 stats_due_at,metadata_due_at,hydration_needed,policy_version,paused_reason,typed_source_published_at,typed_source_published_acquired_at,
 next_due_at,typed_collection_status,updated_at)
 SELECT candidate.video_id,candidate.channel_id,candidate.video_type,NULL,NULL,video.video_id IS NULL,
 1,NULL,candidate.source_published_at,candidate.source_published_acquired_at,now(),'pending',now()
 FROM (SELECT DISTINCT id FROM unnest(%s::text[]) input(id)) input
 CROSS JOIN LATERAL(SELECT c.* FROM """ + "(" + TYPED_CANDIDATE_INPUT_ID_SQL + ")" + """ c
 WHERE c.video_id=input.id AND c.video_type=%s::text OFFSET 0) candidate
 LEFT JOIN public.videos video ON video.video_id=candidate.video_id
 ON CONFLICT(video_id) DO UPDATE SET channel_id=excluded.channel_id,video_type=excluded.video_type,
 metadata_due_at=now(),hydration_needed=state.hydration_needed OR excluded.hydration_needed,
 paused_reason=NULL,typed_source_published_at=coalesce(excluded.typed_source_published_at,state.typed_source_published_at),
 typed_source_published_acquired_at=CASE WHEN excluded.typed_source_published_at IS NOT NULL
 THEN excluded.typed_source_published_acquired_at ELSE state.typed_source_published_acquired_at END,
 next_due_at=now(),updated_at=now() WHERE state.lease_token IS NULL
 AND state.availability_first_missing_at IS NULL AND state.availability_resume_observed_at IS NULL
 AND state.paused_reason IS DISTINCT FROM 'availability_retry_expired'""")


def _reject(message):
    raise DatabaseServerError(message, code='P0001')


@contextmanager
def _transaction(database, name, transaction):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        spec = get_operation(name)
        with database.transaction(Operation(spec.name, statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms, total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as tx:
            yield tx


def backfill_video_collection_state(database, limit=1000, *, transaction=None):
    """Return the single TABLE row only after bounded initialization and fencing."""
    with _transaction(database, 'backfill_video_collection_state', transaction) as tx:
        limit = tx.fetch_one(_LIMIT, (limit,))['limit']
        if limit is None or not 1 <= limit <= 1000:
            _reject('database_operation_failed')
        version = cadence_policy_version(tx)
        ids = [row['video_id'] for row in tx.fetch_all(_SELECT, (version, limit), max_rows=limit)]
        tx.fetch_all(_LOCK_QUEUE, (ids,), max_rows=1000)
        tx.fetch_all(_LOCK_INVENTORY, (ids,), max_rows=1000)
        scopes = tx.fetch_one(_SCOPES, (ids,))['scopes']
        affected = ensure_video_collection_state_in_transaction(tx, ids)
        remaining = tx.fetch_one(_REMAINING, (version,))['count']
        tx.execute(_PROGRESS, (remaining != 0, remaining == 0))
        assert_channel_scopes(tx, scopes)
        return [dict(selected_count=len(ids), affected_count=affected, remaining_count=remaining)]


def prioritize_typed_video_ids_in_transaction(transaction, video_type, video_ids):
    """Borrowed provisional effect; caller owns locks and final channel scope."""
    with transaction.borrow():
        args = transaction.fetch_one(_TYPED_INPUT, (video_type, video_ids))
        kind, ids, count = args['video_type'], args['ids'], args['count']
        # Native IF treats a NULL type condition as unknown, not true.
        if (kind is not None and kind not in ('short', 'stream')) or count is None or count > 1000:
            _reject('typed_video_collection_priority_input_invalid')
        if count == 0:
            return 0
        return transaction.execute(_PRIORITIZE_TYPED, (ids, kind))


def enqueue_video_collection_ids(database, video_ids, channel_scopes, *, transaction=None):
    """Keep Long then Short then Stream effects under the same final scope."""
    with _transaction(database, 'enqueue_video_collection_ids', transaction) as tx:
        raw = channel_scopes if channel_scopes is None or isinstance(channel_scopes, Jsonb) else Jsonb(channel_scopes)
        args = tx.fetch_one(_INPUT, (video_ids, raw))
        ids, scopes, count = args['ids'], args['scopes'], args['count']
        if count is None or count > 1000 or args['invalid_id']:
            _reject('common_pool_enqueue_input_invalid')
        long_ids = [row['video_id'] for row in tx.fetch_all(_LONG_IDS, (ids,), max_rows=1000)
                    if channel_mode_row(row)['collection_mode'] == 'active']
        if not isinstance(scopes, list) or len(scopes) > 1000:
            _reject('channel_collection_scope_invalid')
        tx.fetch_all(_LOCK_QUEUE, (ids,), max_rows=1000)
        tx.fetch_all(_LOCK_INVENTORY, (ids,), max_rows=1000)
        if tx.fetch_one(_IDENTITY, dict(ids=ids, scopes=Jsonb(scopes)))['matches'] is not True:
            _reject('channel_collection_scope_invalid')
        ensure_video_collection_state_in_transaction(tx, long_ids)
        affected = tx.execute(_PRIORITIZE_LONG, (long_ids,))
        for kind in ('short', 'stream'):
            affected += prioritize_typed_video_ids_in_transaction(tx, kind, ids)
        assert_channel_scopes(tx, scopes)
        return affected
