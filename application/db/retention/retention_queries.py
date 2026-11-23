"""Bounded Python-owned retention protocols; one maintenance transaction owns each
operation.

The caller owns the single maintenance transaction. Native SQL owns clock/UUID
semantics and locked predicates; Python owns the bounded loop and page receipt.
"""
from __future__ import annotations
from ..base.postgres import DatabaseServerError
from psycopg.types.json import Jsonb

SAVED_ERRORS=frozenset({'hot_operational_target_invalid','hot_operational_limit_invalid','hot_operational_cursor_invalid'})
_CLOCK="""WITH source AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT to_jsonb(at)#>>'{}' AS at,to_jsonb(at-interval '720 hours')#>>'{}' AS cutoff FROM source"""
_PAGE="""SELECT kind,id::text AS id,to_jsonb(deleted_at)#>>'{}' AS deleted_at,
 NOT isfinite(deleted_at) OR deleted_at>%s::timestamptz AS invalid_clock,
 deleted_at<%s::timestamptz AS expired
 FROM public.saved_objects WHERE deleted_at IS NOT NULL AND (%s::uuid IS NULL OR id>%s::uuid)
 ORDER BY id LIMIT %s"""
_LOCK="""SELECT deleted_at IS NOT NULL AND deleted_at<%s::timestamptz AND deleted_at<=%s::timestamptz AS expired
 FROM public.saved_objects WHERE kind=%s AND id=%s::uuid FOR UPDATE NOWAIT"""


def _reject(message):
    raise DatabaseServerError(message,code='P0001')


_EXACT_PAGE="""SELECT t.kind,t.id::text AS id,to_jsonb(t.deleted_at)#>>'{}' AS deleted_at,
 NOT isfinite(t.deleted_at) OR t.deleted_at>%(at)s::timestamptz AS invalid_clock,
 t.deleted_at<%(cutoff)s::timestamptz AS expired
 FROM jsonb_to_recordset(%(references)s::jsonb) k(kind text,id uuid)
 JOIN public.saved_objects t ON t.kind=k.kind AND t.id=k.id
 WHERE t.deleted_at IS NOT NULL ORDER BY t.id,t.kind"""


def cleanup_deleted_saved_objects(transaction,*,target,after_id=None,limit=100,references=None):
    """One page, row-local NOWAIT savepoints, unchanged 30-day tombstone rule.

    A concurrent restore is rechecked under its row lock. Lock conflicts defer
    that item and retain its old clock in the page receipt; unknown errors abort
    the whole page. No write is retried after an uncertain acknowledgement.
    """
    tx=transaction
    with tx.borrow():
        # Function-argument native casts precede the retained body validation.
        args=tx.fetch_one('SELECT %s::text AS target,%s::text AS after_id,%s::integer AS limit',
                          (target,after_id,limit))
        stamp=tx.fetch_one(_CLOCK)
        if args['target']!='deleted_saved_objects':_reject('hot_operational_target_invalid')
        if args['limit'] is None or not 1<=args['limit']<=500:_reject('hot_operational_limit_invalid')
        cursor=args['after_id']
        if cursor is not None:
            try:
                with tx.savepoint():
                    cursor=tx.fetch_one('SELECT %s::uuid::text AS id',(cursor,))['id']
            except DatabaseServerError as error:
                if isinstance(error.code,str) and error.code.startswith('22'):_reject('hot_operational_cursor_invalid')
                raise
        if references is None:
            rows=tx.fetch_all(_PAGE,(stamp['at'],stamp['cutoff'],cursor,cursor,args['limit']),max_rows=args['limit'])
        else:
            params={**stamp,'references':Jsonb([dict(kind=key[0],id=key[1]) for key in references])}
            rows=tx.fetch_all(_EXACT_PAGE,params,max_rows=len(references))
        deleted=invalid=0
        deferred=[]
        unresolved=[]
        for item in rows:
            if item['invalid_clock']:
                invalid+=1
                unresolved.append(dict(target=target,key=[item['kind'],item['id']]))
                continue
            if not item['expired']:continue
            try:
                with tx.savepoint():
                    locked=tx.fetch_one(_LOCK,(stamp['cutoff'],stamp['at'],item['kind'],item['id']))
                    if not locked or locked['expired'] is not True:continue
                    tx.execute('DELETE FROM public.saved_collection_commands WHERE collection_kind=%s AND collection_id=%s::uuid',
                               (item['kind'],item['id']))
                    deleted+=tx.execute('DELETE FROM public.saved_objects WHERE kind=%s AND id=%s::uuid AND deleted_at IS NOT NULL',
                                        (item['kind'],item['id']))
            except DatabaseServerError as error:
                if error.code!='55P03':raise
                deferred.append(item['deleted_at'])
                unresolved.append(dict(target=target,key=[item['kind'],item['id']]))
        oldest=None
        if deferred:
            # PostgreSQL orders BC/wide-year clocks correctly; never lexicographic Python MIN.
            oldest=tx.fetch_one("SELECT to_jsonb(min(value::timestamptz))#>>'{}' AS value FROM unnest(%s::text[]) AS clocks(value)",
                                (deferred,))['value']
        complete=references is not None or len(rows)<args['limit']
        return dict(status='ok',target=args['target'],as_of=stamp['at'],cutoff=stamp['cutoff'],
                    scanned=len(rows),deleted=deleted,scrubbed=0,deferred=len(deferred),complete=complete,
                    next_cursor=None if complete else rows[-1]['id'],oldest_remaining_expired_source_at=oldest,
                    invalid_clock_remaining=invalid,priority_complete=len(unresolved)<=100,priority_references=unresolved[:100])
