"""Typed queue reconciliation through the public repository.

Python owns the bounded workflow and cursor/final-admission sequence. The raw
queue and inventory pages precede eligibility, and ON CONFLICT evaluates its
native schedule against the locked conflicting row, including concurrent
insert. No extra child/control locks or provider calls are introduced.
Materialized read CTEs keep source facts at the original UPDATE/INSERT
statement boundaries.

Scheduling uses one native UPSERT acquisition plus one Python-planned batch
settlement. The provisional conflict write adds MVCC/WAL inside the original
transaction. Intrinsic storage/scope integrity guards remain authoritative.
"""
from .typed_video_read_policy import (TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL, TYPED_CANDIDATE_INVENTORY_IDS_SQL)
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from .typed_reconciliation_schedule import reconcile_typed_rows_in_transaction

_INPUT = """SELECT %s::text AS video_type,%s::integer AS limit"""

_RUNTIME = """SELECT reconcile_queue_cursor AS queue_cursor,reconcile_inventory_cursor AS inventory_cursor FROM public.typed_video_collection_runtime WHERE video_type=%(video_type)s::text FOR UPDATE"""

_QUEUE_IDS = """select coalesce(array_agg(page.video_id order by page.video_id),array[]::text[])
    AS ids
    from (
        select state.video_id from public.video_collection_state as state
        where state.video_type = %(video_type)s::text
          and state.video_id > coalesce(%(queue_cursor)s,'')
        order by state.video_id limit %(limit)s::integer
    ) as page"""

_INVENTORY_IDS = """select coalesce(array_agg(page.video_id order by page.video_id),array[]::text[])
    AS ids
    from (
        -- Bound physical inventory IDs BEFORE checking queue gaps. The cursor
        -- follows this raw page, including healthy/ineligible IDs, so a healthy
        -- prefix cannot cause a whole-catalog search or starve later gaps.
        select inventory.video_id from public.video_inventory as inventory
        where inventory.video_type = %(video_type)s::text
          and inventory.video_id > coalesce(%(inventory_cursor)s,'')
        order by inventory.video_id limit %(limit)s::integer
    ) as page"""

_PAUSE = ("""update public.video_collection_state as state
        set hydration_needed=true,
            metadata_due_at=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
                OR state.paused_reason='availability_retry_expired' THEN state.metadata_due_at
                ELSE coalesce(state.metadata_due_at,state.next_due_at,now()) END,
            next_due_at=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
                OR state.paused_reason='availability_retry_expired' THEN state.next_due_at
                ELSE coalesce(state.next_due_at,state.metadata_due_at,now()) END,
            paused_reason=CASE WHEN state.paused_reason='typed_identity_not_eligible' THEN NULL
                ELSE state.paused_reason END,
            lease_channel_epoch = NULL, lease_token = NULL,lease_owner=null,lease_started_at=null,
            lease_expires_at=null,updated_at=now()
        from (with eligible as materialized (
        select candidate.video_id,candidate.channel_id,candidate.video_type
        from unnest(%(queue_ids)s::text[]) as input(video_id)
        cross join lateral (
            select c.video_id,c.channel_id,c.video_type
            from """ + "(" + TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL + ")" + """ as c
            where c.video_id=input.video_id and c.video_type=%(video_type)s::text
            offset 0
        ) as candidate
    ), invalid as materialized (
        select state.video_id from public.video_collection_state as state
        where state.video_id = any(%(queue_ids)s::text[])
          and state.video_type = %(video_type)s::text
          and (state.lease_token is null or state.lease_expires_at <= now())
          and (NOT state.hydration_needed or state.paused_reason='typed_identity_not_eligible'
               or state.lease_token is not null OR (state.next_due_at IS NULL
               AND state.availability_first_missing_at IS NULL AND state.availability_resume_observed_at IS NULL
               AND state.paused_reason IS DISTINCT FROM 'availability_retry_expired'))
          and not exists (
              select 1 from eligible as candidate
              where candidate.video_id=state.video_id
                and candidate.channel_id=state.channel_id
                and candidate.video_type=state.video_type
          )
        order by state.video_id for update of state skip locked
    ) SELECT video_id FROM invalid) invalid where state.video_id=invalid.video_id""")

_LOCK_QUEUE = """SELECT state.video_id FROM public.video_collection_state state
    WHERE state.video_id=ANY(%(inventory_ids)s::text[]) ORDER BY state.video_id FOR UPDATE OF state"""

_LOCK_INVENTORY = """SELECT identity.video_id FROM public.video_inventory identity
    WHERE identity.video_id=ANY(%(inventory_ids)s::text[]) ORDER BY identity.video_id FOR SHARE OF identity"""

_SCOPES = ("""SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
        'channel_id',inventory.channel_id,'control_epoch',control.control_epoch)), '[]'::jsonb)
    AS scopes FROM """ + "(" + TYPED_CANDIDATE_INVENTORY_IDS_SQL + ")" + """ inventory
    LEFT JOIN public.channel_collection_state control ON control.channel_id=inventory.channel_id
    WHERE inventory.video_id=ANY(%(inventory_ids)s::text[]) AND control.owner_permission='accepted' AND NOT control.is_hidden""")


_PROGRESS = """update public.typed_video_collection_runtime
    set reconcile_queue_cursor=case when cardinality(%(queue_ids)s::text[])<%(limit)s::integer then null
                                   else (%(queue_ids)s::text[])[cardinality(%(queue_ids)s::text[])] end,
        reconcile_inventory_cursor=case when cardinality(%(inventory_ids)s::text[])<%(limit)s::integer then null
                                       else (%(inventory_ids)s::text[])[cardinality(%(inventory_ids)s::text[])] end,
        updated_at=now()
    where video_type=%(video_type)s::text"""

@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('reconcile_typed_video_collection_state')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as tx:yield tx


def reconcile_typed_video_collection_state(database,video_type,limit=1000,*,transaction=None):
    """Advance one raw queue/inventory page atomically; a full page is not EOF."""
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(video_type,limit))
        if args['video_type'] not in ('short','stream') or args['limit'] is None or not 1<=args['limit']<=1000:
            raise DatabaseServerError('typed_video_collection_reconcile_input_invalid',code='P0001')
        runtime=tx.fetch_one(_RUNTIME,args)
        if runtime is None:raise DatabaseServerError('common_pool_reconcile_cursor_missing',code='P0001')
        args.update(runtime)
        args['queue_ids']=tx.fetch_one(_QUEUE_IDS,args)['ids']
        args['inventory_ids']=tx.fetch_one(_INVENTORY_IDS,args)['ids']
        from ..inventory.automatic_type_reconciliation import reconcile_automatic_video_types
        reconcile_automatic_video_types(tx,args['queue_ids'])
        queue_ids=set(args['queue_ids'])
        reconcile_automatic_video_types(tx,[identity for identity in args['inventory_ids'] if identity not in queue_ids])
        from .video_queue_initialization import ensure_typed_collection_state_in_transaction
        ensure_typed_collection_state_in_transaction(tx,args['inventory_ids'])
        paused=tx.execute(_PAUSE,args)
        tx.fetch_all(_LOCK_QUEUE,args,max_rows=1000)
        tx.fetch_all(_LOCK_INVENTORY,args,max_rows=1000)
        scopes=tx.fetch_one(_SCOPES,args)['scopes']
        args['scopes']=Jsonb(scopes)
        affected=reconcile_typed_rows_in_transaction(tx,args)
        tx.execute(_PROGRESS,args)
        # No source read, child lock or progress mutation follows final admission.
        assert_channel_scopes(tx,scopes)
        return dict(enabled=True,video_type=args['video_type'],selected_count=affected,affected_count=affected,
            paused_count=paused,queue_examined_count=len(args['queue_ids']),inventory_examined_count=len(args['inventory_ids']),
            queue_scan_complete=len(args['queue_ids'])<args['limit'],inventory_scan_complete=len(args['inventory_ids'])<args['limit'],
            remaining_count=None,remaining_count_exact=False)
