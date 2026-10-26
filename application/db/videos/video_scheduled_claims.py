"""Bounded scheduled claims used by the video collection repository.

The existing per-video queue remains the sole lease owner. Its raw1000-ID due
window follows advisory operation selection, leaves out rows under a live lease
(as the mixed window does, so abandoned leases cannot fill it) and precedes the
locked lease and identity checks, which stay authoritative.
Scoped common batches recheck the selected provider operation under locks; unrelated
due work stays queued. The fixed cursor retains PostgreSQL17's10/50-row fetch
batches and one selection snapshot. Sparse operations may inspect and briefly
lock more rows in the same bounded window; they never receive extra leases.

Shared channel and typed-policy projections retain native predicate placement,
clock comparisons and source freshness. Intrinsic database integrity guards
remain authoritative. No provider I/O belongs here.
"""
from .typed_video_read_policy import (TYPED_IDENTITY_POSITIONAL_SQL, typed_identity_row)
from ..channel.channel_read_policy import (CHANNEL_FACTS_RELATION_SQL, channel_pause_row)
from .video_initial_policy import VIDEO_HANDOFF_STATE_SQL, VIDEO_INITIAL_PENDING_STATE_SQL
from .video_identity_recovery import ELIGIBLE_SQL as _RECOVERY_IDENTITY
from .video_queue_initialization import LONG_SHORT_CONFLICT_SQL
from .collection_policy import require_cadence_snapshot
from collection_policy import unavailable_retry_policy, PolicyError
from contextlib import contextmanager
from uuid import UUID
from saved_work import SavedWorkError, channel_id as canonical_channel_id
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError,DatabaseServerError,Operation

_ARGS="""SELECT %s::text AS video_type,%s::integer AS limit,
 btrim(%s::text) AS worker,%s::uuid AS run_id,%s::integer AS lease_seconds"""
_RUNTIME="""SELECT EXISTS(SELECT 1 FROM public.typed_video_collection_runtime
 WHERE video_type=%s::text AND runtime_enabled) AS enabled"""
_RECOVERY_WINDOW = _RECOVERY_IDENTITY.replace('%s::text','state.video_id',1).replace('%s::text','state.channel_id',1)
_WINDOW_BASE="""SELECT coalesce(array_agg(page.video_id ORDER BY page.due_at,page.video_id),ARRAY[]::text[]) AS ids
 FROM (SELECT state.video_id,state.next_due_at AS due_at FROM public.video_collection_state state
 WHERE state.paused_reason IS NULL AND state.next_due_at<=now() {typed}
 AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND NOT """+VIDEO_INITIAL_PENDING_STATE_SQL+"""
 AND (state.video_type IS NOT NULL OR ("""+_RECOVERY_WINDOW+"""))
 ORDER BY state.next_due_at,state.video_id LIMIT 1000) page"""
_WINDOW={False:_WINDOW_BASE.format(typed=''),True:_WINDOW_BASE.format(typed="AND state.video_type=%s::text AND state.video_type IN ('short','stream')")}
# Every relation below is addressed by a <=50-row prefix already locked by
# the fixed cursor. Channel facts are shared within the prefix; per-video
# identity and initial handoff still use their canonical policy projections.
_BATCH_INPUT="""SELECT * FROM unnest(%(video_ids)s::text[],%(channel_ids)s::text[],
 %(video_types)s::text[]) AS input(video_id,channel_id,video_type)"""
_VIDEO_MODE = """CASE WHEN state.video_type IS NULL THEN 'identity_refresh'
 WHEN state.video_type='stream' AND state.typed_stream_actual_end_at IS NULL THEN 'stream_lifecycle'
 WHEN state.hydration_needed THEN 'hydrate'
 WHEN state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL
 OR state.metadata_due_at IS NULL OR state.metadata_due_at<=now() THEN 'metadata'
 ELSE 'counters' END"""
_OPERATION = "CASE WHEN (" + _VIDEO_MODE + ")='counters' THEN 'counters' ELSE 'metadata' END"
_OPERATION_FILTER = " AND (%(operation)s::text IS NULL OR (" + _OPERATION + ")=%(operation)s::text)"
_LONG_FACTS="""WITH batch AS MATERIALIZED("""+_BATCH_INPUT+"""),
 channel_facts AS MATERIALIZED(SELECT channel.* FROM ("""+CHANNEL_FACTS_RELATION_SQL+""") channel
 JOIN (SELECT DISTINCT channel_id FROM batch) addressed USING(channel_id))
 SELECT batch.video_id,channel.policy_facts,""" + _VIDEO_MODE + """ AS collection_mode,("""+VIDEO_HANDOFF_STATE_SQL+""") AS initial_handoff,
 EXISTS(SELECT 1 FROM public.video_inventory inventory WHERE inventory.video_id=batch.video_id
 AND inventory.channel_id=batch.channel_id AND inventory.video_type='long'
 AND (NOT ("""+LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','inventory.video_id').replace(
 '%(channel_id)s::text','inventory.channel_id')+""")
 OR EXISTS(SELECT 1 FROM public.video_inventory_type_resolution proof WHERE proof.video_id=inventory.video_id
 AND proof.resolution_status='resolved' AND proof.canonical_video_type='long'))) AS eligible
 FROM batch LEFT JOIN channel_facts channel USING(channel_id)
 JOIN public.video_collection_state state ON state.video_id=batch.video_id AND state.channel_id=batch.channel_id"""
_TYPED_FACTS="""SELECT batch.video_id,identity.identity_facts,""" + _VIDEO_MODE + """ AS collection_mode
 FROM ("""+_BATCH_INPUT+""") batch
 JOIN public.video_collection_state state ON state.video_id=batch.video_id AND state.channel_id=batch.channel_id
 CROSS JOIN LATERAL ("""+TYPED_IDENTITY_POSITIONAL_SQL.replace('%s::text','batch.video_id',1).replace(
 '%s::text','batch.channel_id',1).replace('%s::text','batch.video_type',1)+") identity"
_RECOVERY_FACTS="""SELECT batch.video_id,identity.eligible FROM ("""+_BATCH_INPUT+""") batch
 CROSS JOIN LATERAL ("""+_RECOVERY_IDENTITY.replace('%s::text','batch.video_id',1).replace(
 '%s::text','batch.channel_id',1)+") identity"
_PAUSE="""UPDATE public.video_collection_state AS state SET next_due_at=NULL,paused_reason=input.pause_reason,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,
 lease_expires_at=NULL,updated_at=now()
 FROM unnest(%(video_ids)s::text[],%(channel_ids)s::text[],%(video_types)s::text[],
 %(pause_reasons)s::text[]) AS input(video_id,channel_id,video_type,pause_reason)
 WHERE state.video_id=input.video_id AND state.channel_id=input.channel_id
 AND state.video_type IS NOT DISTINCT FROM input.video_type RETURNING state.video_id"""
_PROMOTE_METADATA="""UPDATE public.video_collection_state state SET metadata_due_at=now(),updated_at=now()
 FROM ("""+_BATCH_INPUT+""") input
 WHERE state.video_id=input.video_id AND state.channel_id=input.channel_id
 AND state.video_type IS NOT DISTINCT FROM input.video_type RETURNING state.video_id"""
_CLAIM="""UPDATE public.video_collection_state AS state SET lease_token=gen_random_uuid(),
 lease_owner=%(worker)s::text,lease_channel_epoch=(SELECT control.control_epoch
 FROM public.channel_collection_state control WHERE control.channel_id=state.channel_id),
 lease_started_at=now(),lease_expires_at=now()+make_interval(secs=>%(lease_seconds)s::integer),
 attempt_count=state.attempt_count+1,last_claim_run_id=%(run_id)s::uuid,last_attempt_at=now(),updated_at=now()
 FROM ("""+_BATCH_INPUT+""") input
 WHERE state.video_id=input.video_id AND state.channel_id=input.channel_id
 AND state.video_type IS NOT DISTINCT FROM input.video_type
 RETURNING jsonb_build_object('video_id',state.video_id,'channel_id',state.channel_id,
 'video_type',state.video_type,'lease_token',state.lease_token,
 'lease_expires_at',state.lease_expires_at,'next_due_at',state.next_due_at) AS value,
 state.lease_channel_epoch AS epoch,
 """ + _VIDEO_MODE + """ AS collection_mode"""


# Opt-in raw window: retain every legacy predicate and the existing cursor,
# while bounding one chosen channel/format before the physical1000-ID cap.
_SCOPED_WINDOW = {
    False: _WINDOW_BASE.format(typed='AND state.channel_id=%s::text AND state.video_type=%s::text'),
    True: _WINDOW_BASE.format(typed='AND state.channel_id=%s::text AND state.video_type IS NULL'),
}
# Each LATERAL branch uses the existing channel/type/due index. The first
# allocation shares the raw cap equally; one keyset refill reuses space left by
# small scopes. Only these at-most-1000 rows are ranked, never the whole queue.
# Preserve channel turns; older heads lead each fair round without letting a
# large older channel consume every turn before a small one is considered.
_MIXED_RAW = """SELECT state.video_id,state.next_due_at AS due_at
 FROM public.video_collection_state state
 WHERE state.channel_id=scope.channel_id AND {typed}
 AND state.paused_reason IS NULL AND state.next_due_at<=now() {after}
 AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND state.last_claim_run_id IS DISTINCT FROM %(run_id)s::uuid
 """ + _OPERATION_FILTER + """
 AND NOT """ + VIDEO_INITIAL_PENDING_STATE_SQL + """
 AND (state.video_type IS NOT NULL OR (""" + _RECOVERY_WINDOW + """))
 ORDER BY state.next_due_at,state.video_id LIMIT scope.window_limit"""
_MIXED_PAGES = {
    refill: ' UNION ALL '.join('(' + _MIXED_RAW.format(typed=predicate,
        after=('AND (state.next_due_at,state.video_id)>(scope.after_due_at,scope.after_id)'
               if refill else '')) + ')' for predicate in (
            "scope.video_type<>'unknown' AND state.video_type=scope.video_type",
            "scope.video_type='unknown' AND state.video_type IS NULL"))
    for refill in (False, True)
}
_MIXED_WINDOW = """WITH scopes AS MATERIALIZED(
 SELECT input.channel_id,input.video_type,input.scope_order,%(scope_limit)s::integer AS window_limit
 FROM unnest(%(channels)s::text[],%(types)s::text[]) WITH ORDINALITY
 input(channel_id,video_type,scope_order)),
 first_window AS MATERIALIZED(
 SELECT scope.scope_order,page.* FROM scopes scope CROSS JOIN LATERAL (""" + _MIXED_PAGES[False] + """) page),
 first_counts AS MATERIALIZED(
 SELECT scope.*,count(page.video_id)::integer AS first_count,
 (array_agg(page.due_at ORDER BY page.due_at DESC,page.video_id DESC))[1] AS after_due_at,
 (array_agg(page.video_id ORDER BY page.due_at DESC,page.video_id DESC))[1] AS after_id
 FROM scopes scope LEFT JOIN first_window page USING(scope_order)
 GROUP BY scope.channel_id,scope.video_type,scope.scope_order,scope.window_limit),
 refill AS MATERIALIZED(
 SELECT first_counts.*,
 ((1000-sum(first_count) OVER()) / greatest(count(*) FILTER(
 WHERE first_count=window_limit) OVER(),1))::integer AS refill_limit FROM first_counts),
 second_window AS MATERIALIZED(
 SELECT scope.scope_order,page.* FROM (SELECT channel_id,video_type,scope_order,
 after_due_at,after_id,refill_limit AS window_limit FROM refill
 WHERE first_count=window_limit AND refill_limit>0) scope
 CROSS JOIN LATERAL (""" + _MIXED_PAGES[True] + """) page),
 bounded_window AS MATERIALIZED(
 SELECT * FROM first_window UNION ALL SELECT * FROM second_window),
 ranked AS (SELECT video_id,due_at,scope_order,row_number() OVER(
 PARTITION BY scope_order ORDER BY due_at,video_id) AS turn FROM bounded_window)
 SELECT (SELECT coalesce(array_agg(video_id ORDER BY turn,due_at,scope_order,video_id),ARRAY[]::text[])
 FROM ranked) AS ids,NOT EXISTS(SELECT 1 FROM refill scope
 WHERE first_count=window_limit AND (refill_limit=0 OR
 (SELECT count(*) FROM second_window page WHERE page.scope_order=scope.scope_order)=refill_limit))
 AS scan_exhausted"""


_CANDIDATE_TYPES = ('long', 'short', 'stream', 'unknown')
_SEED_FRONTIER_FILTER = """ AND (%(operation)s::text IS NOT NULL OR
 ((""" + _OPERATION + """)='metadata' AND %(metadata_enabled)s::boolean
 AND state.channel_id COLLATE "C">coalesce(%(metadata_after)s::text,'')) OR
 ((""" + _OPERATION + """)='counters' AND %(counters_enabled)s::boolean
 AND state.channel_id COLLATE "C">coalesce(%(counters_after)s::text,'')))"""
_CANDIDATE_PAGE = """WITH page AS MATERIALIZED(
 SELECT control.channel_id,control.is_hidden,control.owner_permission
 FROM public.channel_collection_state control
 WHERE control.channel_id COLLATE "C">coalesce(%(after)s::text,'')
 ORDER BY control.channel_id COLLATE "C" LIMIT %(limit)s::integer)
 SELECT page.channel_id,(head.ready_count>0) AS eligible,head.*
 FROM page CROSS JOIN LATERAL (
 SELECT count(*)::integer AS ready_count,min(due_at) AS due_at,
 (array_agg(operation ORDER BY due_at,video_id))[1] AS operation
 FROM (SELECT state.video_id,state.next_due_at AS due_at,""" + _OPERATION + """ AS operation
 FROM public.video_collection_state state WHERE state.channel_id=page.channel_id
 AND NOT page.is_hidden AND page.owner_permission='accepted'
 AND {typed} AND state.paused_reason IS NULL AND state.next_due_at<=now()
 AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND state.last_claim_run_id IS DISTINCT FROM %(run_id)s::uuid
 """ + _OPERATION_FILTER + _SEED_FRONTIER_FILTER + """
 AND NOT """ + VIDEO_INITIAL_PENDING_STATE_SQL + """
 AND (state.video_type IS NOT NULL OR (""" + _RECOVERY_WINDOW + """))
 ORDER BY state.next_due_at,state.video_id LIMIT CASE WHEN %(operation)s::text IS NULL THEN 1 ELSE 50 END) ready) head
 ORDER BY page.channel_id COLLATE "C"
"""
_CANDIDATE_PAGES = {
    False: _CANDIDATE_PAGE.format(typed='state.video_type=%(video_type)s::text'),
    True: _CANDIDATE_PAGE.format(typed='state.video_type IS NULL'),
}


def _candidate_channel(value):
    try:
        return canonical_channel_id(value)
    except SavedWorkError:
        raise ValueError('common_video_scope_invalid') from None


def validate_scope(scope):
    """Optional exact scheduled scope; unknown retains the existing NULL owner."""
    if scope is None:
        return None
    if (not isinstance(scope, dict) or set(scope) != {'channel_id', 'video_type'}
            or scope['video_type'] not in _CANDIDATE_TYPES):
        raise ValueError('common_video_scope_invalid')
    return dict(channel_id=_candidate_channel(scope['channel_id']), video_type=scope['video_type'])


def validate_scopes(scopes):
    """A finite ordered fair-pick window; duplicate scopes have no extra share."""
    if scopes is None:
        return None
    if not isinstance(scopes, (list, tuple)) or not 1 <= len(scopes) <= 50:
        raise ValueError('common_video_scope_invalid')
    result = [validate_scope(scope) for scope in scopes]
    if any(scope is None for scope in result) or len({
            (scope['channel_id'], scope['video_type']) for scope in result}) != len(result):
        raise ValueError('common_video_scope_invalid')
    return result


def due_candidate_page(database, *, video_type, run_id, after=None, limit=16, operation=None, frontiers=None):
    """Read a physical channel keyset page, never a lease or source payload.

    Empty eligible results still advance after to the last inspected channel.
    The caller bounds total sweep reads separately from raw-video scan budget.
    This coarse admission is advisory; the scoped claim rechecks all policies.
    """
    if (video_type not in _CANDIDATE_TYPES or type(limit) is not int or not 1 <= limit <= 100
            or operation not in (None, 'metadata', 'counters')):
        raise ValueError('common_video_candidate_input_invalid')
    if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
        raise ValueError('common_video_candidate_input_invalid')
    if after is not None:
        after = _candidate_channel(after)
    if frontiers is None:
        frontiers = {kind:dict(after=None,exhausted=False) for kind in ('metadata','counters')}
    if (not isinstance(frontiers, dict) or set(frontiers) != {'metadata','counters'}
            or any(not isinstance(value,dict) or set(value) != {'after','exhausted'}
                   or type(value['exhausted']) is not bool for value in frontiers.values())):
        raise ValueError('common_video_candidate_input_invalid')
    args = dict(video_type=video_type, run_id=run_id, after=after, limit=limit, operation=operation)
    for kind, position in frontiers.items():
        args[kind+'_after'] = None if position['after'] is None else _candidate_channel(position['after'])
        args[kind+'_enabled'] = not position['exhausted']
    # These small indexed windows are repeatedly executed. On large queues,
    # planner cost triggers JIT whose compilation dominates the actual read.
    # Disable it only within this owner; never change the connection default.
    with database.transaction(Operation('common_video_candidates', read_only=True, disable_jit=True,
            statement_timeout_ms=5000, lock_timeout_ms=1000, total_timeout_ms=8000)) as tx:
        rows = tx.fetch_all(_CANDIDATE_PAGES[video_type == 'unknown'], args, max_rows=limit)
        previous = after or ''
        candidates = []
        if len(rows) > limit:
            raise ValueError('common_video_candidate_result_invalid')
        for row in rows:
            identity = _candidate_channel(row['channel_id'])
            if identity <= previous or type(row.get('eligible')) is not bool:
                raise ValueError('common_video_candidate_result_invalid')
            previous = identity
            if row['eligible']:
                if (type(row.get('ready_count')) is not int or not 1 <= row['ready_count'] <= 50
                        or row.get('operation') not in ('metadata', 'counters')
                        or row.get('due_at') is None
                        or (operation is not None and row['operation'] != operation)):
                    raise ValueError('common_video_candidate_result_invalid')
                candidates.append(dict(channel_id=identity, video_type=video_type,
                    operation=row['operation'], ready_count=row['ready_count'], due_at=row['due_at']))
        return dict(rows=candidates, next_after=previous if rows else after,
                    inspected_count=len(rows), scan_exhausted=len(rows)<limit)


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        # The bounded raw window/claim is short; compiling its repeated policy
        # expressions costs more than executing them. Borrowed owners retain
        # their own transaction settings; this setting cannot escape commit.
        with database.transaction(Operation(name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                disable_jit=True,error_codes=spec.error_codes)) as owned:yield owned


def _batch_parameters(states, *, video_type=None):
    return dict(video_ids=[row['video_id'] for row in states],
                channel_ids=[row['channel_id'] for row in states],
                video_types=[video_type if video_type is not None else row['video_type'] for row in states])


def _rows_by_id(rows, states, *, claimed=False):
    """A partial, duplicate or foreign RETURNING result aborts the whole owner."""
    expected={state['video_id']:state for state in states}
    if len(expected)!=len(states) or not isinstance(rows,(list,tuple)):
        raise DatabaseError('database_result_shape_invalid')
    indexed={}
    for row in rows:
        value=row.get('value') if claimed and isinstance(row,dict) else row
        if not isinstance(value,dict) or not isinstance(value.get('video_id'),str):
            raise DatabaseError('database_result_shape_invalid')
        identity=value['video_id']
        if identity not in expected or identity in indexed:
            raise DatabaseError('database_result_shape_invalid')
        if claimed and (value.get('channel_id')!=expected[identity]['channel_id']
                        or value.get('video_type')!=expected[identity]['video_type']):
            raise DatabaseError('database_result_shape_invalid')
        indexed[identity]=row
    if set(indexed)!=set(expected):raise DatabaseError('database_result_shape_invalid')
    return indexed


def _facts(tx, sql, states, *, video_type=None):
    if not states:return {}
    return _rows_by_id(tx.fetch_all(sql,_batch_parameters(states,video_type=video_type),
                                   max_rows=len(states)),states)


def _inspect_prefix(tx, states, args, *, typed, group=None, limit=50):
    """Check one locked prefix; the caller grants the admitted rows together.

    A group exists only for the fair common feeder. It constrains the selected
    operation, or takes the first eligible row when no operation was supplied.
    Mode checks read rows already held by the cursor. A different operation receives no lease, attempt or schedule write.
    """
    longs=[row for row in states if not typed and row['video_type']=='long']
    typed_rows=[row for row in states if typed or row['video_type'] in ('short','stream')]
    long_facts=_facts(tx,_LONG_FACTS,longs)
    typed_facts=_facts(tx,_TYPED_FACTS,typed_rows,video_type=args['video_type'] if typed else None)
    proof = {identity: facts['eligible'] is True for identity,facts in long_facts.items()}
    proof.update({identity: typed_identity_row({'identity_facts':facts['identity_facts']})['eligible']
                  for identity,facts in typed_facts.items()})
    recovery_rows=[state for state in states if not proof.get(state['video_id'],False)]
    recovery_facts=_facts(tx,_RECOVERY_FACTS,recovery_rows)
    mode_facts={**long_facts,**typed_facts}
    eligible=[]
    paused=[]
    reasons=[]
    promote=[]
    for state in states:
        if len(eligible) == limit:
            break
        identity=state['video_id']
        pause=None
        first_missing=state.get('availability_first_missing_at')
        if first_missing is not None and state.get('availability_resume_observed_at') is None:
            try:
                episode=unavailable_retry_policy(require_cadence_snapshot(tx),
                    state.get('availability_schedule_type') or state['video_type'],
                    first_missing,state['observed_at'])
            except PolicyError as error:
                raise DatabaseServerError(error.code,code=error.sqlstate) from None
            if episode.stopped:
                paused.append(state)
                reasons.append('availability_retry_expired')
                continue
        if state['video_type']=='long':
            facts=long_facts[identity]
            reason=channel_pause_row(facts,field='reason')
            pause=reason['reason']
            if reason.get('initial_handoff') is True:pause=None
        if not proof.get(identity,False):
            if recovery_facts[identity]['eligible']:
                mode_facts[identity]={'collection_mode':'identity_refresh'}
                pause=None
            elif state['video_type'] is None:
                continue
            elif pause is None:
                pause='inventory_not_long' if state['video_type']=='long' else 'typed_identity_not_eligible'
        if pause is None:
            mode=mode_facts[identity]['collection_mode']
            if mode not in ('hydrate','metadata','counters','stream_lifecycle','identity_refresh'):
                raise DatabaseError('database_result_shape_invalid')
            if group is not None:
                operation = 'counters' if mode == 'counters' else 'metadata'
                if group['operation'] is None:
                    group['operation'] = operation
                elif operation != group['operation']:
                    group['deferred'] = True
                    if mode=='identity_refresh' and state['video_type'] is not None:
                        promote.append(state)
                    continue
            eligible.append(state)
        else:
            # A channel hold is temporary. The availability episode owns its
            # retry deadline; queue initialization intentionally leaves that
            # episode alone, so replacing its deadline with a generic pause
            # would strand it after the channel becomes eligible again.
            if first_missing is not None or state.get('availability_resume_observed_at') is not None:
                continue
            paused.append(state)
            reasons.append(pause)
    if paused:
        _rows_by_id(tx.returning_all(_PAUSE,{**_batch_parameters(paused),'pause_reasons':reasons},
                                    max_rows=len(paused)),paused)
    if promote:
        _rows_by_id(tx.returning_all(_PROMOTE_METADATA,_batch_parameters(promote),
                                    max_rows=len(promote)),promote)
    return eligible, mode_facts, len(paused)


def _grant(tx, eligible, args, *, typed, mode_facts, group=None):
    """One atomic lease write for the <=50 admitted, already locked rows."""
    if not eligible:
        return []
    claimed=_rows_by_id(tx.returning_all(_CLAIM,{**args,**_batch_parameters(eligible)},
                                        max_rows=len(eligible)),eligible,claimed=True)
    ordered=[]
    for state in eligible:
        item=claimed[state['video_id']]
        row=item['value']
        mode=mode_facts[state['video_id']]['collection_mode']
        if mode!='identity_refresh' and item['collection_mode']!=mode:
            raise DatabaseError('database_result_shape_invalid')
        row['collection_mode']=mode
        if row['video_type'] is None:row['video_type']='unknown'
        if typed:row['priority']=0
        ordered.append(item)
    return ordered


def _claim(database,video_type,limit,worker_id,run_id,lease_seconds,*,typed,table,transaction,scope=None,scopes=None,operation=None):
    if operation not in (None, 'metadata', 'counters') or (operation is not None and scopes is None):
        raise ValueError('common_video_operation_invalid')
    requested_scope=validate_scope(scope)
    requested_scopes=validate_scopes(scopes)
    if ((requested_scope is not None and requested_scopes is not None)
            or (typed and (requested_scope is not None or requested_scopes is not None))):
        raise ValueError('common_video_scope_invalid')
    name=('claim_due_typed_video_collection' if table else 'claim_due_typed_video_collection_batch') if typed else 'claim_due_video_collection_batch'
    error='typed_video_collection_claim_input_invalid' if typed else 'common_video_collection_claim_input_invalid'
    with _transaction(database,transaction,name) as tx:
        args=tx.fetch_one(_ARGS,(video_type,limit,worker_id,run_id,lease_seconds))
        if ((typed and args['video_type'] not in ('short','stream')) or args['limit'] is None
                or not 0<=args['limit']<=50 or not args['worker'] or args['run_id'] is None
                or args['lease_seconds'] is None or not 30<=args['lease_seconds']<=3600):
            raise DatabaseServerError(error,code='P0001')
        result=dict(rows=[],inspected_count=0,paused_count=0,scan_exhausted=True)
        if args['limit']==0:return [] if table else result
        if typed and tx.fetch_one(_RUNTIME,(args['video_type'],))['enabled'] is not True:
            return [] if table else result
        if requested_scopes is not None:
            window=tx.fetch_one(_MIXED_WINDOW,dict(
                channels=[item['channel_id'] for item in requested_scopes],
                types=[item['video_type'] for item in requested_scopes],
                scope_limit=1000//len(requested_scopes), operation=operation, run_id=args['run_id']))
            ids=window['ids']
            result['scan_exhausted']=window['scan_exhausted']
        elif requested_scope is None:
            ids=tx.fetch_one(_WINDOW[typed],(args['video_type'],) if typed else ())['ids']
        else:
            unknown=requested_scope['video_type']=='unknown'
            parameters=(requested_scope['channel_id'],) if unknown else (requested_scope['channel_id'],requested_scope['video_type'])
            ids=tx.fetch_one(_SCOPED_WINDOW[unknown],parameters)['ids']
        result['inspected_count']=len(ids)
        if requested_scopes is None:
            result['scan_exhausted']=len(ids)<1000
        allowed_scopes={(item['channel_id'],None if item['video_type']=='unknown' else item['video_type'])
                        for item in (requested_scopes or ([requested_scope] if requested_scope else []))}
        scopes=[]
        seen=set()
        group = dict(operation=operation, deferred=False) if requested_scopes is not None else None
        admitted = []
        admitted_mode_facts = {}
        with tx.video_scheduled_candidates(ids,args['run_id'],video_type=args['video_type'] if typed else None) as candidates:
            while len(admitted)<args['limit']:
                remaining = args['limit'] - len(admitted)
                # Sparse operations still inspect facts in batches, never one
                # SQL query per rejected row as the lease allowance runs low.
                prefix=candidates.locked_prefix(50 if group is not None else remaining)
                if not prefix:break
                # The cursor now owns these row locks. Do not adopt or pause a
                # row whose owner/type moved after the advisory raw snapshot.
                states=[state for state in prefix if not allowed_scopes
                        or (state['channel_id'],state['video_type']) in allowed_scopes]
                eligible,mode_facts,paused=_inspect_prefix(tx,states,args,typed=typed,group=group,limit=remaining)
                result['paused_count']+=paused
                admitted.extend(eligible)
                admitted_mode_facts.update({row['video_id']: mode_facts[row['video_id']]
                    for row in eligible})
                if len(admitted)==args['limit']:
                    result['scan_exhausted']=False
                    break
            # All admitted rows remain locked in this same transaction. Keep
            # the cursor's native 10/50 fetch pattern but grant their <=50 leases
            # together; any missing RETURNING row aborts the entire batch.
            claimed = _grant(tx,admitted,args,typed=typed,mode_facts=admitted_mode_facts,group=group)
            for item in claimed:
                row=item['value']
                scope=(row['channel_id'],item['epoch'])
                if scope not in seen:
                    scopes.append(dict(channel_id=scope[0],control_epoch=scope[1]))
                    seen.add(scope)
                result['rows'].append(row)
        if group is not None and group['deferred']:
            # A short operation batch is not EOF for its scopes. Existing
            # acknowledged sweep continuation must return to the other mode.
            result['scan_exhausted'] = False
        # Cursor is closed before final control locks. No child read/write follows.
        assert_channel_scopes(tx,scopes)
        if operation is not None:
            result['claim_stop_reason'] = ('full' if len(result['rows']) == args['limit'] else
                'raw_scan_bound' if len(ids) == 1000 else
                'admission_changed_or_locked' if len(result['rows'])+result['paused_count'] < len(ids)
                or (group is not None and group['deferred']) else 'eligible_window_exhausted')
        return result['rows'] if table else result


def claim_due_video_collection_batch(database,limit,worker_id,run_id,lease_seconds=300,*,transaction=None,scope=None,scopes=None,operation=None):
    return _claim(database,None,limit,worker_id,run_id,lease_seconds,typed=False,table=False,
                  transaction=transaction,scope=scope,scopes=scopes,operation=operation)


def claim_due_typed_video_collection_batch(database,video_type,limit,worker_id,run_id,lease_seconds=300,*,transaction=None):
    return _claim(database,video_type,limit,worker_id,run_id,lease_seconds,typed=True,table=False,transaction=transaction)


def claim_due_typed_video_collection(database,video_type,limit,worker_id,run_id,lease_seconds=300,*,transaction=None):
    return _claim(database,video_type,limit,worker_id,run_id,lease_seconds,typed=True,table=True,transaction=transaction)
