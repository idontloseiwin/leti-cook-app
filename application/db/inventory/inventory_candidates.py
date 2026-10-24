"""Bounded advisory inventory scopes; the existing claim owner owns all leases."""
from dataclasses import dataclass
from collections.abc import Mapping
from uuid import UUID
from psycopg.types.json import Jsonb

from channel_collection_scope import require_control_epoch
from saved_work import SavedWorkError, channel_id as canonical_channel_id
from ..base.operation_contracts import get_operation
from ..base.postgres import Operation
from .inventory_claims import _CANDIDATE_PREDICATE, _CONTINUATIONS

_TYPES = ('long', 'short', 'stream')
_MAX_ORDINAL = 2**63-1
_CEILING = """SELECT coalesce((SELECT dispatch_ordinal FROM public.channel_playlist_state
 WHERE runtime_enabled ORDER BY dispatch_ordinal DESC LIMIT 1),0)::bigint AS ceiling"""
_PAGE = """WITH physical AS MATERIALIZED (
 SELECT channel_id,video_type,dispatch_ordinal,priority FROM public.channel_playlist_state
 WHERE runtime_enabled AND dispatch_ordinal<=%(ceiling)s::bigint
 AND (%(after_ordinal)s::bigint IS NULL OR
  (dispatch_ordinal,-(priority::bigint),channel_id COLLATE "C",video_type COLLATE "C")>
  (%(after_ordinal)s::bigint,-(%(after_priority)s::bigint),
   %(after_channel)s::text COLLATE "C",%(after_type)s::text COLLATE "C"))
 ORDER BY dispatch_ordinal,-(priority::bigint),channel_id COLLATE "C",video_type COLLATE "C"
 LIMIT %(limit)s::integer)
 SELECT physical.channel_id,physical.video_type,physical.dispatch_ordinal,physical.priority,
 c.control_epoch,coalesce(d.next_due_at<=now()
  OR (d.status='leased' AND d.lease_expires_at<=now()),false) AS due,
 coalesce(("""+_CANDIDATE_PREDICATE+"""),false) AS eligible
 FROM physical LEFT JOIN public.channel_playlist_state d
 ON d.channel_id=physical.channel_id AND d.video_type=physical.video_type
 LEFT JOIN public.channel_collection_state c ON c.channel_id=physical.channel_id
 ORDER BY physical.dispatch_ordinal,-(physical.priority::bigint),
 physical.channel_id COLLATE "C",physical.video_type COLLATE "C"
"""


def _channel(value):
    try:
        return canonical_channel_id(value)
    except SavedWorkError:
        raise ValueError('inventory_candidate_identity_invalid') from None


@dataclass(frozen=True)
class InventoryCandidateCursor:
    dispatch_ordinal: int
    priority: int
    channel_id: str
    video_type: str

    def __post_init__(self):
        if (type(self.dispatch_ordinal) is not int or not 1<=self.dispatch_ordinal<=_MAX_ORDINAL
                or type(self.priority) is not int or not -(2**31)<=self.priority<2**31
                or self.video_type not in _TYPES):
            raise ValueError('inventory_candidate_cursor_invalid')
        _channel(self.channel_id)

    @property
    def key(self):
        return (self.dispatch_ordinal,-self.priority,self.channel_id,self.video_type)


def _proofs(values):
    """Bound every JSON scalar before serialization; native owner rechecks proof."""
    required={'channel_id','video_type','control_epoch','scan_id','last_transition_id'}
    clean=[]
    seen=set()
    for value in values:
        if not isinstance(value,Mapping) or set(value)!=required:
            raise ValueError('inventory_candidate_continuations_invalid')
        row=dict(value)
        _channel(row['channel_id'])
        identity=(row['channel_id'],row['video_type'])
        if row['video_type'] not in _TYPES or identity in seen:
            raise ValueError('inventory_candidate_continuations_invalid')
        require_control_epoch(row['control_epoch'])
        for name in ('scan_id','last_transition_id'):
            value=row[name]
            if not isinstance(value,str) or len(value)!=36 or str(UUID(value))!=value:
                raise ValueError('inventory_candidate_continuations_invalid')
        clean.append(row)
        seen.add(identity)
    return clean


def candidate_page(database, *, limit=16, after=None, ceiling=None, continuations=()):
    """Inspect at most limit physical scopes, even when none are currently eligible.

    limit must also fit the caller's free ready capacity and remaining scan
    allowance. A native ordinal ceiling bounds this sweep, not authorization or
    a repeatable snapshot. Claims move to a later sweep, retaining durable order.
    """
    if (type(limit) is not int or not 1<=limit<=100
            or (after is not None and type(after) is not InventoryCandidateCursor)
            or (ceiling is not None and (type(ceiling) is not int or not 0<=ceiling<=_MAX_ORDINAL))
            or (after is not None and (ceiling is None or after.dispatch_ordinal>ceiling))
            or not isinstance(continuations,(list,tuple)) or len(continuations)>500):
        raise ValueError('inventory_candidate_input_invalid')
    values=dict(limit=limit,ceiling=ceiling,
        after_ordinal=None if after is None else after.dispatch_ordinal,
        after_priority=None if after is None else after.priority,
        after_channel=None if after is None else after.channel_id,
        after_type=None if after is None else after.video_type,
        video_types=list(_TYPES),channel_ids=None,scopes_text=None,only_due=True,
        continuations_text=Jsonb(_proofs(continuations)),include_due=True,
        initial_job_id=None,initial_job_lease_token=None)
    spec=get_operation('inventory_candidate_page')
    with database.transaction(Operation(spec.name,read_only=True,
            statement_timeout_ms=spec.statement_timeout_ms,lock_timeout_ms=spec.lock_timeout_ms,
            total_timeout_ms=spec.total_timeout_ms)) as tx:
        if tx.fetch_one(_CONTINUATIONS,values)['invalid'] is not False:
            raise ValueError('inventory_candidate_continuations_invalid')
        if ceiling is None:
            ceiling=tx.fetch_one(_CEILING)['ceiling']
            if type(ceiling) is not int or not 0<=ceiling<=_MAX_ORDINAL:
                raise ValueError('inventory_candidate_result_invalid')
            values['ceiling']=ceiling
        rows=tx.fetch_all(_PAGE,values,max_rows=limit)
        if len(rows)>limit:
            raise ValueError('inventory_candidate_result_invalid')
        previous=after
        candidates=[]
        for row in rows:
            cursor=InventoryCandidateCursor(row['dispatch_ordinal'],row['priority'],
                                            row['channel_id'],row['video_type'])
            if (cursor.dispatch_ordinal>ceiling or (previous is not None and cursor.key<=previous.key)
                    or type(row.get('eligible')) is not bool or type(row.get('due')) is not bool):
                raise ValueError('inventory_candidate_result_invalid')
            previous=cursor
            if row['eligible']:
                candidates.append(dict(channel_id=cursor.channel_id,video_type=cursor.video_type,
                    control_epoch=require_control_epoch(row['control_epoch']),due=row['due']))
        result=dict(rows=candidates,next_after=previous,ceiling=ceiling,
                    inspected_count=len(rows),scan_exhausted=len(rows)<limit)
    return result
