"""Three atomic video-failure protocols behind the queue repository.

No lease-expiry check is added: the retained protocol releases an exact token
until another owner replaces it. Queue writes, count validation and the final
channel generation fence share one transaction. No uncertain outcome is retried.
"""
from __future__ import annotations
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from collection_policy import failure_policy, STATS_AVAILABILITY_CHECK_REQUIRED
from ..base.postgres import DatabaseServerError, Operation

_DEFINITION = "video_id text,video_type text,lease_token uuid,error_type text,retry_after_seconds integer"
_PARSE = "SELECT raw.ordinal,input.*,btrim(video_id) AS trimmed_id,btrim(error_type) AS trimmed_error FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinal) CROSS JOIN LATERAL jsonb_to_record(raw.value) input("+_DEFINITION+") ORDER BY raw.ordinal"
_LOCK = """SELECT state.video_id,state.channel_id,state.video_type,state.lease_token,
 state.lease_channel_epoch,state.consecutive_failures,now() AS transaction_clock,
 raw.ordinal
 FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY raw(value,ordinal)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) input(video_id text,video_type text,lease_token uuid)
 JOIN public.video_collection_state state ON state.video_id=input.video_id
 AND state.lease_token=input.lease_token
 WHERE (%(mode)s::text='long' OR (%(mode)s::text='typed' AND state.video_type=%(video_type)s::text)
        OR (%(mode)s::text='common' AND state.video_type IS NOT DISTINCT FROM nullif(input.video_type,'unknown')))
 ORDER BY state.video_id FOR UPDATE OF state
"""
_UPDATE = """UPDATE public.video_collection_state AS state SET
 next_due_at=input.retry_at,lease_channel_epoch=NULL,lease_token=NULL,
 lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=input.failures,last_error_type=input.error_type,updated_at=now()
 FROM jsonb_to_recordset(%s::jsonb) input(video_id text,lease_token uuid,retry_at timestamptz,failures integer,error_type text)
 WHERE state.video_id=input.video_id AND state.lease_token=input.lease_token"""
_UPDATE_COMMON = _UPDATE.replace(
    "next_due_at=input.retry_at,",
    "metadata_due_at=CASE WHEN input.check_availability THEN now() ELSE state.metadata_due_at END,"
    "next_due_at=input.retry_at,",
).replace("failures integer,error_type text)", "failures integer,error_type text,check_availability boolean)")
_UPDATE_TYPED = _UPDATE.replace("next_due_at=input.retry_at,", "stats_due_at=NULL,metadata_due_at=NULL,next_due_at=input.retry_at,")
_ERRORS = frozenset({"common_video_collection_fail_input_invalid","common_video_collection_fail_limit",
 "common_video_collection_fail_row_invalid","common_video_collection_fail_stale_ownership",
 "typed_video_collection_fail_input_invalid","typed_video_collection_fail_too_many_rows",
 "typed_video_collection_fail_stale_claim","channel_collection_scope_invalid","channel_collection_scope_lost"})


def _reject(message=None):
    raise DatabaseServerError(message or "database_operation_failed",code="P0001")


@contextmanager
def _transaction(database,tx,mode):
    if tx is not None:
        with tx.borrow():
            yield tx
    else:
        timeout=8000 if mode=='common' else 60000
        with database.transaction(Operation('fail_'+mode+'_video_claims',statement_timeout_ms=timeout,
                total_timeout_ms=timeout,error_codes=_ERRORS)) as owned:
            yield owned


def _fail(database,rows,mode,*,video_type=None,transaction=None):
    from ..channel.channel_control import assert_channel_scopes
    with _transaction(database,transaction,mode) as tx:
        if mode=='typed':
            video_type=tx.fetch_one('SELECT %s::text AS video_type',(video_type,))['video_type']
        if not isinstance(rows,list) or (mode=='typed' and video_type is not None and video_type not in ('short','stream')):
            _reject(None if mode=='long' else mode+'_video_collection_fail_input_invalid')
        count=len(rows)
        maximum=1000 if mode=='long' else 50
        # Long's original jsonb_to_recordset count casts fields before its cap.
        # Preserve those native errors even for oversized or empty payloads.
        if mode=='long':
            tx.fetch_one('SELECT count(*) AS count FROM jsonb_to_recordset(%s::jsonb) input('+_DEFINITION+')',(Jsonb(rows),))
        if count>maximum:
            _reject(None if mode=='long' else 'typed_video_collection_fail_too_many_rows' if mode=='typed' else 'common_video_collection_fail_limit')
        if not count and mode!='common':return 0
        if mode in ('long','common'):
            parsed=tx.fetch_all(_PARSE,(Jsonb(rows),),max_rows=maximum)
            invalid=any(row['trimmed_id'] in (None,'') or row['lease_token'] is None
                or row['trimmed_error'] in (None,'')
                or (row['retry_after_seconds'] is not None and not 30<=row['retry_after_seconds']<=86400)
                or (mode=='common' and row['video_type'] not in ('long','short','stream','unknown')) for row in parsed)
            if mode=='common' and len({row['video_id'] for row in parsed})!=count:invalid=True
            if invalid:_reject('common_video_collection_fail_row_invalid' if mode=='common' else None)
        locked=tx.fetch_all(_LOCK,{'rows':Jsonb(rows),'mode':mode,'video_type':video_type},max_rows=maximum)
        if mode=='typed':
            # This path's retained writer casts retry/error only AFTER queue locks.
            parsed=tx.fetch_all(_PARSE,(Jsonb(rows),),max_rows=maximum)
        by_ordinal={item['ordinal']:item for item in parsed}
        updates=[]
        scopes=set()
        for row in locked:
            payload=by_ordinal[row['ordinal']]
            policy=failure_policy(now=row['transaction_clock'],consecutive_failures=row['consecutive_failures'],
                error_type=payload['error_type'],retry_after_seconds=payload['retry_after_seconds'],typed=mode=='typed')
            updates.append(dict(video_id=row['video_id'],lease_token=str(row['lease_token']),
                retry_at=policy.retry_at.isoformat(),failures=policy.consecutive_failures,error_type=policy.error_type,
                check_availability=(mode=='common' and policy.error_type==STATS_AVAILABILITY_CHECK_REQUIRED)))
            scopes.add((row['channel_id'],row['lease_channel_epoch']))
        changed=tx.execute(_UPDATE_TYPED if mode=='typed' else _UPDATE_COMMON if mode=='common' else _UPDATE,(Jsonb(updates),))
        if changed!=count:
            _reject(None if mode=='long' else 'typed_video_collection_fail_stale_claim' if mode=='typed' else 'common_video_collection_fail_stale_ownership')
        # No further child writes follow this shared fence; lock order is stable.
        assert_channel_scopes(tx,[{'channel_id':channel,'control_epoch':epoch} for channel,epoch in scopes])
    return changed


def fail_video_collection_claims(database,rows,*,transaction=None):
    return _fail(database,rows,'long',transaction=transaction)


def fail_video_collection_batch(database,rows,*,transaction=None):
    return _fail(database,rows,'common',transaction=transaction)


def fail_typed_video_collection_claims(database,video_type,rows,*,transaction=None):
    return _fail(database,rows,'typed',video_type=video_type,transaction=transaction)
