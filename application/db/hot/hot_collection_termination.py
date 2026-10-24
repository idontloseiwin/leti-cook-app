"""Source-free HOT termination; only a closed driver entry may own it.

These borrowed protocols release exact existing capabilities and interrupt an
exact running sync row. They neither admit new work nor retry an unknown ACK.
The owner must retain the native 5s total/statement and 250ms lock budgets and
the fixed HOT control capability; a generic transaction is intentionally
denied.
"""
from psycopg.types.json import Jsonb

from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError
from ..runs.sync_run_write_normalization import interrupt_sync_run

ABORT_ERRORS = get_operation('abort_hot_collection_claims').error_codes
INTERRUPT_ERRORS = get_operation('interrupt_hot_sync_run').error_codes

_ABORT_INPUT = """WITH native AS MATERIALIZED (
 SELECT %s::jsonb AS videos,%s::jsonb AS playlists,%s::jsonb AS operators),
 clock AS MATERIALIZED (SELECT clock_timestamp() AS at FROM native)
 SELECT videos::text AS videos,playlists::text AS playlists,operators::text AS operators,
 to_jsonb(at)#>>'{}' AS at,
 jsonb_typeof(videos)='array' AND jsonb_typeof(playlists)='array'
 AND jsonb_typeof(operators)='array' AS arrays,
 CASE WHEN jsonb_typeof(videos)='array' THEN jsonb_array_length(videos) END AS nv,
 CASE WHEN jsonb_typeof(playlists)='array' THEN jsonb_array_length(playlists) END AS np,
 CASE WHEN jsonb_typeof(operators)='array' THEN jsonb_array_length(operators) END AS no
 FROM native CROSS JOIN clock"""

# Facts are bounded by the validated 50/500/1 envelope. CASE guards subtraction
# on scalars; the first invalid item retains group and input order. UUID accepts
# exactly PostgreSQL's syntax, including its noncanonical accepted spellings.
_CLAIM_ERROR = """WITH items AS (
 SELECT 0 AS kind,ordinal,item FROM jsonb_array_elements(%(videos)s::jsonb) WITH ORDINALITY x(item,ordinal)
 UNION ALL SELECT 1,ordinal,item FROM jsonb_array_elements(%(playlists)s::jsonb) WITH ORDINALITY x(item,ordinal)
 UNION ALL SELECT 2,ordinal,item FROM jsonb_array_elements(%(operators)s::jsonb) WITH ORDINALITY x(item,ordinal)),
 facts AS (SELECT kind,ordinal,item,CASE WHEN jsonb_typeof(item)='object' THEN
 CASE WHEN kind=2 THEN item-ARRAY['job_id','lease_token']='{}'::jsonb
 AND jsonb_typeof(item->'job_id')='string' AND jsonb_typeof(item->'lease_token')='string'
 ELSE item-(CASE WHEN kind=0 THEN ARRAY['video_id','video_type','lease_token']
 ELSE ARRAY['channel_id','video_type','lease_token'] END)='{}'::jsonb
 AND jsonb_typeof(item->(CASE WHEN kind=0 THEN 'video_id' ELSE 'channel_id' END))='string'
 AND length(item->>(CASE WHEN kind=0 THEN 'video_id' ELSE 'channel_id' END)) BETWEEN 1 AND 128
 AND jsonb_typeof(item->'video_type')='string' AND (item->>'video_type' IN('long','short','stream')
 OR (kind=0 AND item->>'video_type'='unknown'))
 AND jsonb_typeof(item->'lease_token')='string' END ELSE false END AS shape FROM items),
 errors AS (SELECT kind,ordinal,CASE WHEN shape IS NOT TRUE THEN
 CASE kind WHEN 0 THEN 'hot_abort_invalid_video_claim' WHEN 1 THEN 'hot_abort_invalid_playlist_claim'
 ELSE 'hot_abort_invalid_operator_claim' END
 WHEN NOT pg_input_is_valid(item->>'lease_token','uuid')
 OR (kind=2 AND NOT pg_input_is_valid(item->>'job_id','uuid')) THEN 'hot_abort_invalid_token' END AS error FROM facts)
 SELECT error FROM errors WHERE error IS NOT NULL ORDER BY kind,ordinal LIMIT 1"""
_DUPLICATES = """SELECT
 (SELECT count(DISTINCT value->>'video_id') FROM jsonb_array_elements(%(videos)s::jsonb))<>%(nv)s
 OR (SELECT count(DISTINCT (value->>'channel_id',value->>'video_type'))
 FROM jsonb_array_elements(%(playlists)s::jsonb))<>%(np)s AS duplicate"""

_PLAYLISTS = """UPDATE public.channel_playlist_state q SET
 status='ready',lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,
 next_due_at=greatest(q.next_due_at,%(at)s::timestamptz+interval '30 seconds'),last_error_code='hot_runtime_interrupted',
 last_result=CASE WHEN q.video_type='long' THEN 'error' ELSE q.last_result END,
 discovery_started_at=NULL,updated_at=%(at)s::timestamptz
 FROM (WITH wanted AS MATERIALIZED (
 SELECT * FROM jsonb_to_recordset(%(playlists)s::jsonb) AS x(channel_id text,video_type text,lease_token uuid)),
 owned AS MATERIALIZED (SELECT q.channel_id,q.video_type,q.lease_token FROM public.channel_playlist_state q JOIN wanted w
 ON q.channel_id=w.channel_id AND q.video_type=w.video_type AND q.lease_token=w.lease_token
 WHERE q.status='leased' ORDER BY q.channel_id,q.video_type FOR UPDATE OF q SKIP LOCKED)
 SELECT * FROM owned) o
 WHERE q.channel_id=o.channel_id AND q.video_type=o.video_type AND q.lease_token=o.lease_token"""
_VIDEOS = """UPDATE public.video_collection_state q SET
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 next_due_at=greatest(q.next_due_at,%(at)s::timestamptz+interval '30 seconds'),
 last_error_type='hot_runtime_interrupted',updated_at=%(at)s::timestamptz
 FROM (WITH wanted AS MATERIALIZED (
 SELECT * FROM jsonb_to_recordset(%(videos)s::jsonb) AS x(video_id text,video_type text,lease_token uuid)),
 owned AS MATERIALIZED (SELECT q.video_id,q.lease_token FROM public.video_collection_state q JOIN wanted w
 ON q.video_id=w.video_id AND q.video_type IS NOT DISTINCT FROM nullif(w.video_type,'unknown') AND q.lease_token=w.lease_token
 ORDER BY q.video_id FOR UPDATE OF q SKIP LOCKED) SELECT * FROM owned) o
 WHERE q.video_id=o.video_id AND q.lease_token=o.lease_token"""
_OPERATORS = """UPDATE public.operator_jobs j SET status=CASE WHEN EXISTS(
 SELECT 1 FROM public.channel_collection_state control WHERE control.channel_id=j.channel_id
 AND NOT control.is_hidden AND control.owner_permission='accepted'
 AND control.control_epoch=j.accepted_epoch AND j.lease_channel_epoch=j.accepted_epoch)
 AND isfinite(j.requested_at) AND j.requested_at BETWEEN clock_timestamp()-interval '696 hours'
 AND clock_timestamp() THEN 'queued' ELSE 'suspended' END,
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,
 last_error_code='hot_runtime_interrupted',updated_at=%(at)s::timestamptz
 FROM (WITH wanted AS MATERIALIZED (
 SELECT * FROM jsonb_to_recordset(%(operators)s::jsonb) AS x(job_id uuid,lease_token uuid)),
 owned AS MATERIALIZED (SELECT j.id,j.lease_token FROM public.operator_jobs j JOIN wanted w
 ON j.id=w.job_id AND j.lease_token=w.lease_token WHERE j.status='running'
 ORDER BY j.id FOR UPDATE OF j SKIP LOCKED) SELECT * FROM owned) o
 WHERE j.id=o.id AND j.lease_token=o.lease_token"""

_INTERRUPT_INPUT = """WITH native AS MATERIALIZED (
 SELECT %s::bigint AS run_id,%s::timestamptz AS started_at),
 clock AS MATERIALIZED (SELECT clock_timestamp() AS at FROM native)
 SELECT run_id,to_jsonb(started_at)#>>'{}' AS started_at,to_jsonb(at)#>>'{}' AS at,
 run_id IS NOT NULL AND run_id>=1 AND started_at IS NOT NULL AND isfinite(started_at)
 AND started_at<=at AS valid FROM native CROSS JOIN clock"""


def _json(value):
    return value if value is None or isinstance(value, Jsonb) else Jsonb(value)


def _reject(error):
    raise DatabaseServerError(error, code='P0001')


def abort_hot_collection_claims(transaction, video_claims, playlist_claims, operator_claims):
    """Borrow one closed control transaction; return the exact seven-field receipt."""
    with transaction.borrow():
        values = transaction.fetch_one(_ABORT_INPUT, tuple(map(_json, (video_claims, playlist_claims, operator_claims))))
        if values['arrays'] is not True:
            _reject('hot_abort_invalid_input')
        if values['nv'] > 50 or values['np'] > 500 or values['no'] > 1:
            _reject('hot_abort_input_bound')
        invalid = transaction.fetch_one(_CLAIM_ERROR, values)
        if invalid is not None:
            _reject(invalid['error'])
        if transaction.fetch_one(_DUPLICATES, values)['duplicate']:
            _reject('hot_abort_duplicate_claim')
        playlists = transaction.execute(_PLAYLISTS, values)
        videos = transaction.execute(_VIDEOS, values)
        operators = transaction.execute(_OPERATORS, values)
        return {'status': 'aborted', 'video_requested': values['nv'], 'video_released': videos,
                'playlist_requested': values['np'], 'playlist_released': playlists,
                'operator_requested': values['no'], 'operator_released': operators}


def interrupt_hot_sync_run(transaction, run_id, started_at):
    """Interrupt one exact running row; summary filtering remains the shared codec."""
    with transaction.borrow():
        values = transaction.fetch_one(_INTERRUPT_INPUT, (run_id, started_at))
        if values['valid'] is not True:
            _reject('hot_interrupt_invalid_run')
        matched = interrupt_sync_run(transaction, run_id=values['run_id'],
                                     started_at=values['started_at'], at=values['at'])
        return {'status': 'interrupted', 'matched': matched}


_REMAINING_VIDEO = '''SELECT q.video_id FROM public.video_collection_state q
 JOIN jsonb_to_recordset(%s::jsonb) AS w(video_id text,lease_token uuid)
 ON q.video_id=w.video_id AND q.lease_token=w.lease_token
 ORDER BY q.video_id LIMIT 1 FOR UPDATE OF q NOWAIT'''
_REMAINING_PLAYLIST = '''SELECT q.channel_id FROM public.channel_playlist_state q
 JOIN jsonb_to_recordset(%s::jsonb) AS w(channel_id text,video_type text,lease_token uuid)
 ON q.channel_id=w.channel_id AND q.video_type=w.video_type AND q.lease_token=w.lease_token
 ORDER BY q.channel_id,q.video_type LIMIT 1 FOR UPDATE OF q NOWAIT'''
_REMAINING_OPERATOR = '''SELECT q.id FROM public.operator_jobs q
 JOIN jsonb_to_recordset(%s::jsonb) AS w(job_id uuid,lease_token uuid)
 ON q.id=w.job_id AND q.lease_token=w.lease_token
 ORDER BY q.id LIMIT 1 FOR UPDATE OF q NOWAIT'''


def drain_hot_collection_claims(transaction, video_claims, playlist_claims, operator_claims):
    """Abort, then prove no supplied token remains owned in the same transaction.

    The older counts-only receipt cannot certify this: SKIP LOCKED can leave a
    still-owned row untouched. A busy/remaining token fails without ACK. Already
    completed or replaced tokens are safe, but are not claimed as new releases.
    This operation preserves the original seven-field abort contract unchanged.
    """
    with transaction.borrow():
        receipt = abort_hot_collection_claims(transaction, video_claims, playlist_claims, operator_claims)
        for rows, query in ((playlist_claims, _REMAINING_PLAYLIST),
                            (video_claims, _REMAINING_VIDEO), (operator_claims, _REMAINING_OPERATOR)):
            if transaction.fetch_one(query, (_json(rows),)) is not None:
                raise RuntimeError('hot_drain_ownership_unconfirmed')
        return {**receipt, 'status': 'drained', 'remaining': 0}
