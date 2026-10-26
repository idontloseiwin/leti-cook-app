"""Bounded retention of API evidence; Python owns the cleanup protocol.

Fixed SQL projections and predicates keep filtering in Postgres. Existing
owners/leases remain authoritative; no retained cleanup call is used.
"""
from __future__ import annotations
from dataclasses import dataclass
from types import MappingProxyType
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseServerError
from .queue_retention import QUEUE_LEASE_SQL,busy_videos

ERRORS=frozenset({'hot_cleanup_invalid_target','hot_cleanup_invalid_limit','hot_cleanup_invalid_cursor'})
_CLOCK="""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT to_jsonb(at)#>>'{}' AS at,to_jsonb(at-interval '696 hours')#>>'{}' AS cutoff FROM clock"""

@dataclass(frozen=True)
class Evidence:
    table: str
    clock: str
    key: str
    primary: str
    video: str
    predicate: str
    immutable: bool=False
    expires: bool=False
    payload: str='true'


def _expired(column):
    return f"({column} IS NULL OR NOT isfinite({column}) OR {column}<%(cutoff)s::timestamptz OR {column}>%(now)s::timestamptz)"


_REVIEW_PAYLOAD='(t.observed_canonical_type IS NOT NULL OR cardinality(t.observed_types)>0 OR cardinality(t.observed_channels)>0 OR t.blocked_reason IS NOT NULL)'
_REVIEW_EXPIRED="(t.evidence_expires_at IS NULL OR NOT isfinite(t.evidence_expires_at) OR t.evidence_expires_at<=%(now)s::timestamptz OR t.evidence_expires_at>%(now)s::timestamptz+interval '696 hours')"


_TARGETS=MappingProxyType({
 'video_snapshots':Evidence('video_snapshots','recorded_at',"lpad(t.id::text,20,'0')","t.id=(%(row)s::jsonb->>'id')::bigint",'t.video_id',_expired('t.recorded_at'),True),
 'channel_snapshots':Evidence('channel_snapshots','recorded_at','t.channel_id',"t.channel_id=%(row)s::jsonb->>'channel_id' AND t.recorded_at=(%(row)s::jsonb->>'recorded_at')::timestamptz",'NULL::text',_expired('t.recorded_at'),True),
 'typed_video_collection_receipts':Evidence('typed_video_collection_receipts','recorded_at',"t.video_id||':'||t.collection_attempt_id::text","t.video_id=%(row)s::jsonb->>'video_id' AND t.collection_attempt_id=(%(row)s::jsonb->>'collection_attempt_id')::uuid",'t.video_id',_expired('t.recorded_at'),True),
 'availability_events':Evidence('video_availability_events','observed_at',"lpad(t.id::text,20,'0')","t.id=(%(row)s::jsonb->>'id')::bigint",'t.video_id',_expired('t.observed_at')+' OR t.from_status IS NOT NULL'),
 'availability_incidents':Evidence('video_availability_incidents','first_seen_at',"lpad(t.id::text,20,'0')","t.id=(%(row)s::jsonb->>'id')::bigint",'t.video_id',
    '(t.restored_at IS NOT NULL AND '+_expired('t.restored_at')+' AND '+_expired('t.first_seen_at')+' AND '+_expired('t.last_seen_at')+') OR ('
    +_expired('t.first_seen_at')+' AND (t.first_status IS NOT NULL OR t.source_first_seen IS NOT NULL)) OR ('
    +_expired('t.last_seen_at')+' AND (t.latest_status IS NOT NULL OR t.source_latest_seen IS NOT NULL)) OR ('
    +_expired('t.restored_at')+' AND (t.restored_status IS NOT NULL OR t.source_restored IS NOT NULL))'),
 'review_evidence':Evidence('video_type_review_cases','evidence_expires_at','t.case_id::text',"t.case_id=(%(row)s::jsonb->>'case_id')::uuid",'t.video_id',
    _REVIEW_EXPIRED+' AND '+_REVIEW_PAYLOAD, expires=True, payload=_REVIEW_PAYLOAD),
})


def _reject(code):
    raise DatabaseServerError(code,code='P0001')


def _cursor(tx,value,target):
    if value is None:return None,None
    if len(value)>2048:_reject('hot_cleanup_invalid_cursor')
    try:
        with tx.savepoint():
            row=tx.fetch_one("""WITH cursor AS(SELECT %s::jsonb AS value)
                SELECT jsonb_typeof(value)='object' AND value->>'target' IS NOT DISTINCT FROM %s::text AS valid,
                 to_jsonb((value->>'at')::timestamptz)#>>'{}' AS at,value->>'key' AS key FROM cursor""",(value,target))
            if not row['valid'] or row['at'] is None or row['key'] is None:_reject('hot_cleanup_invalid_cursor')
            return row['at'],row['key']
    except DatabaseServerError as error:
        # PL/pgSQL WHEN OTHERS excludes cancellation and assertion failure.
        if error.code in ('57014','P0004'):raise
        _reject('hot_cleanup_invalid_cursor')


def _page(spec):
    # Every interpolated fragment comes exclusively from the immutable six-item
    # catalog above. Targets/cursors/times are parameters, never identifiers.
    clock=f"coalesce(t.{spec.clock},'-infinity'::timestamptz)"
    base=f"SELECT to_jsonb(t) AS row_data,{clock} AS work_at,{spec.key} AS work_key,{spec.video} AS video_id FROM public.{spec.table} t WHERE ({spec.payload}) AND "
    cursor=f"(%(after_at)s::timestamptz IS NULL OR ({clock},{spec.key})>(%(after_at)s::timestamptz,%(after_key)s::text))"
    order=f" ORDER BY {clock},{spec.key} LIMIT %(limit)s::integer"
    if spec.immutable:
        source='('+base+clock+'<%(cutoff)s::timestamptz AND '+cursor+order+') UNION ALL ('+base+clock+'>%(now)s::timestamptz AND '+cursor+order+')'
    else:source=base+cursor+order
    source_at="work_at-interval '696 hours'" if spec.expires else 'work_at'
    boundary='work_at<=%(now)s::timestamptz' if spec.expires else 'work_at<%(cutoff)s::timestamptz'
    upper="%(now)s::timestamptz+interval '696 hours'" if spec.expires else '%(now)s::timestamptz'
    return """SELECT row_data,to_jsonb("""+source_at+""")#>>'{}' AS work_at,work_key,video_id,
        isfinite(work_at) AND """+boundary+""" AS expired,
        NOT isfinite(work_at) OR work_at>"""+upper+""" AS invalid_clock,
        jsonb_build_object('target',%(target)s::text,'at',work_at,'key',work_key)::text AS cursor
        FROM ("""+source+") candidates ORDER BY candidates.work_at,candidates.work_key LIMIT %(limit)s::integer"


def _eligible(tx,spec,row,cutoff,at=None):
    # The initial no-op proof uses a fresh clock exactly as the original did.
    now='clock.at' if at is None else '%(now)s::timestamptz'
    predicate=spec.predicate.replace('%(now)s::timestamptz',now)
    prefix='WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at) ' if at is None else ''
    source=' CROSS JOIN clock' if at is None else ''
    return tx.fetch_one(prefix+f'SELECT ({predicate}) AS eligible FROM jsonb_populate_record(NULL::public.{spec.table},%(row)s::jsonb) t'+source,
         dict(row=Jsonb(row),cutoff=cutoff,now=at))['eligible'] is True


def _mutate(tx,spec,target,row,cutoff,at):
    params=dict(row=Jsonb(row),cutoff=cutoff,now=at)
    if spec.immutable:
        return tx.execute(f'DELETE FROM public.{spec.table} t WHERE {spec.primary}',params),0
    if target=='availability_events':
        expired=tx.fetch_one('SELECT '+_expired('t.observed_at')+' AS expired FROM jsonb_populate_record(NULL::public.video_availability_events,%(row)s::jsonb) t',params)['expired']
        if expired:return tx.execute(f'DELETE FROM public.video_availability_events t WHERE {spec.primary}',params),0
        tx.execute(f'UPDATE public.video_availability_events t SET from_status=NULL WHERE {spec.primary}',params)
    elif target=='availability_incidents':
        expired=tx.fetch_one("""SELECT t.restored_at IS NOT NULL
            AND NOT coalesce(t.restored_at BETWEEN %(cutoff)s::timestamptz AND %(now)s::timestamptz,false)
            AND NOT coalesce(t.first_seen_at BETWEEN %(cutoff)s::timestamptz AND %(now)s::timestamptz,false)
            AND NOT coalesce(t.last_seen_at BETWEEN %(cutoff)s::timestamptz AND %(now)s::timestamptz,false) AS expired
            FROM jsonb_populate_record(NULL::public.video_availability_incidents,%(row)s::jsonb) t""",params)['expired']
        if expired:return tx.execute(f'DELETE FROM public.video_availability_incidents t WHERE {spec.primary}',params),0
        terms=','.join(f'{column}=CASE WHEN {clock} BETWEEN %(cutoff)s::timestamptz AND %(now)s::timestamptz THEN {column} END'
            for column,clock in (('first_status','first_seen_at'),('source_first_seen','first_seen_at'),('latest_status','last_seen_at'),
                ('source_latest_seen','last_seen_at'),('restored_status','restored_at'),('source_restored','restored_at')))
        tx.execute(f'UPDATE public.video_availability_incidents t SET {terms} WHERE {spec.primary}',params)
    else:
        tx.execute(f"UPDATE public.video_type_review_cases t SET observed_canonical_type=NULL,observed_types=ARRAY[]::text[],observed_channels=ARRAY[]::text[],blocked_reason=NULL,evidence_expires_at=NULL,revision=revision+1 WHERE {spec.primary}",params)
    # The original mixed-row UPDATE counters count the locked candidate, rather
    # than ROW_COUNT; preserve that detail in the returned page receipt.
    return 0,1


# Native primary keys only; casts apply to parameters, never indexed columns.
_REFERENCE_KEYS={
 'video_snapshots':(('id','bigint'),),
 'channel_snapshots':(('channel_id','text'),('recorded_at','timestamptz')),
 'typed_video_collection_receipts':(('video_id','text'),('collection_attempt_id','uuid')),
 'availability_events':(('id','bigint'),),
 'availability_incidents':(('id','bigint'),),
 'review_evidence':(('case_id','uuid'),),
}


def _exact_page(spec,target):
    keys=_REFERENCE_KEYS[target]
    shape=','.join(name+' '+cast for name,cast in keys)
    join=' AND '.join('t.'+name+'=k.'+name for name,cast in keys)
    clock=f"coalesce(t.{spec.clock},'-infinity'::timestamptz)"
    source_at=f"{clock}-interval '696 hours'" if spec.expires else clock
    boundary=f'{clock}<=%(now)s::timestamptz' if spec.expires else f'{clock}<%(cutoff)s::timestamptz'
    upper="%(now)s::timestamptz+interval '696 hours'" if spec.expires else '%(now)s::timestamptz'
    return f"""SELECT to_jsonb(t) AS row_data,to_jsonb({source_at})#>>'{{}}' AS work_at,
        {spec.key} AS work_key,{spec.video} AS video_id,
        isfinite({clock}) AND {boundary} AS expired,
        NOT isfinite({clock}) OR {clock}>{upper} AS invalid_clock,NULL::text AS cursor
        FROM jsonb_to_recordset(%(references)s::jsonb) k({shape})
        JOIN public.{spec.table} t ON {join} ORDER BY {clock},{spec.key}"""


# Immutable evidence: the same protocol as the row loop below, one statement per
# step for the whole page. A row never changes, so its locked copy decides.


def _batch_eligible(spec):
    predicate=spec.predicate.replace('%(now)s::timestamptz','clock.at')
    return f"""WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT c.ord,({predicate}) AS eligible FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY c(value,ord)
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.{spec.table},c.value) t CROSS JOIN clock ORDER BY c.ord"""


def _batch_lock(spec):
    primary=spec.primary.replace('%(row)s::jsonb','c.value')
    return f"""SELECT c.ord,l.value FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY c(value,ord)
 CROSS JOIN LATERAL(SELECT to_jsonb(t) AS value FROM public.{spec.table} t WHERE {primary} LIMIT 1 FOR UPDATE SKIP LOCKED) l ORDER BY c.ord"""


def _batch_delete(spec):
    return f"""DELETE FROM public.{spec.table} t USING jsonb_array_elements(%(rows)s::jsonb) c(value)
 WHERE {spec.primary.replace('%(row)s::jsonb','c.value')} AND ({spec.predicate})"""


def _immutable_page(tx,spec,rows,cutoff):
    """Delete one page of immutable evidence; returns (deleted, deferred rows in page order)."""
    if not rows:return 0,[]
    proof=tx.fetch_all(_batch_eligible(spec),dict(rows=Jsonb([row['row_data'] for row in rows]),cutoff=cutoff),max_rows=len(rows))
    eligible={check['ord'] for check in proof if check['eligible'] is True}
    candidates=[row for ord,row in enumerate(rows,1) if ord in eligible]
    busy=busy_videos(tx,[row['video_id'] for row in candidates if row['video_id'] is not None])
    free=[index for index,row in enumerate(candidates) if row['video_id'] not in busy]
    locked=tx.fetch_all(_batch_lock(spec),dict(rows=Jsonb([candidates[index]['row_data'] for index in free])),max_rows=len(free)) if free else []
    # A row another transaction holds, or one already gone, is deferred like in the row loop.
    owned={free[row['ord']-1] for row in locked}
    deferred=[row for index,row in enumerate(candidates) if index not in owned]
    if not locked:return 0,deferred
    at=tx.fetch_one("SELECT to_jsonb(clock_timestamp())#>>'{}' AS at")['at']
    return tx.execute(_batch_delete(spec),dict(rows=Jsonb([row['value'] for row in locked]),cutoff=cutoff,now=at)),deferred


def _reference(target,candidate):
    return dict(target=target,key=[str(candidate['row_data'][name]) for name,cast in _REFERENCE_KEYS[target]])


def cleanup_hot_evidence(transaction,*,target,after_id=None,limit=100,references=None):
    tx=transaction
    with tx.borrow():
        args=tx.fetch_one('SELECT %s::text AS target,%s::text AS after_id,%s::integer AS limit',(target,after_id,limit))
        stamp=tx.fetch_one(_CLOCK)
        target=args['target']
        limit=args['limit']
        if limit is None or not 1<=limit<=500:_reject('hot_cleanup_invalid_limit')
        after_at,after_key=_cursor(tx,args['after_id'],target)
        if target not in _TARGETS:_reject('hot_cleanup_invalid_target')
        spec=_TARGETS[target]
        params=dict(target=target,cutoff=stamp['cutoff'],now=stamp['at'],after_at=after_at,after_key=after_key,limit=limit)
        if references is None:
            rows=tx.fetch_all(_page(spec),params,max_rows=limit)
        else:
            params['references']=Jsonb([dict(zip((name for name,cast in _REFERENCE_KEYS[target]),key)) for key in references])
            rows=tx.fetch_all(_exact_page(spec,target),params,max_rows=len(references))
        deleted=scrubbed=invalid=0
        deferred=[]
        if spec.immutable:deleted,deferred=_immutable_page(tx,spec,rows,stamp['cutoff'])
        for candidate in () if spec.immutable else rows:
            try:
                with tx.savepoint():
                    if not _eligible(tx,spec,candidate['row_data'],stamp['cutoff']):continue
                    video_id=candidate['video_id']
                    if target=='review_evidence' and video_id is not None:
                        locked=tx.fetch_one('SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,962106)) AS acquired',(video_id,))['acquired']
                        if not locked:deferred.append(candidate);continue
                    if video_id is not None:
                        lease=tx.fetch_one(QUEUE_LEASE_SQL+' FOR UPDATE NOWAIT',dict(video_id=video_id))
                        # The source protocol samples the lease clock AFTER its
                        # row lock. Do not evaluate expiry in the locking SELECT.
                        if lease and lease['has_token']:
                            active=tx.fetch_one('SELECT %s::timestamptz>clock_timestamp() AS active',(lease['expires_at'],))['active']
                            if active is True:deferred.append(candidate);continue
                    locked=tx.fetch_one(f'SELECT to_jsonb(t) AS value FROM public.{spec.table} t WHERE {spec.primary} LIMIT 1 FOR UPDATE SKIP LOCKED',dict(row=Jsonb(candidate['row_data'])))
                    if locked is None:deferred.append(candidate);continue
                    at=tx.fetch_one("SELECT to_jsonb(clock_timestamp())#>>'{}' AS at")['at']
                    if not _eligible(tx,spec,locked['value'],stamp['cutoff'],at):continue
                    removed,cleared=_mutate(tx,spec,target,locked['value'],stamp['cutoff'],at)
                    deleted+=removed
                    scrubbed+=cleared
            except DatabaseServerError as error:
                if error.code!='55P03':raise
                deferred.append(candidate)
        expired=[row['work_at'] for row in deferred if row['expired']]
        invalid=sum(bool(row['invalid_clock']) for row in deferred)
        oldest=None
        if expired:oldest=tx.fetch_one("SELECT to_jsonb(min(value::timestamptz))#>>'{}' AS value FROM unnest(%s::text[]) clocks(value)",(expired,))['value']
        complete=references is not None or len(rows)<limit
        return dict(target=target,as_of=stamp['at'],cutoff=stamp['cutoff'],scanned=len(rows),deleted=deleted,scrubbed=scrubbed,
            deferred=len(deferred),next_cursor=None if complete else rows[-1]['cursor'],complete=complete,remaining=None,
            completion_scope='cursor_traversal',global_remaining_unknown=True,telemetry_scope='scanned_page',
            oldest_remaining_expired_source_at=oldest,invalid_clock_remaining=invalid,
            priority_complete=len(deferred)<=100,
            priority_references=[_reference(target,row) for row in deferred[:100]])
