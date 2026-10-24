"""Finite channel UPDATE owners for initial-error normalization.

Call at the original DML site inside its existing transaction/savepoint. Owner
argument checks, job/control locks, receipts and final scope fences stay there.
This helper owns initial-error normalization; the composed workflow supplies
estimate, snapshot and discovery effects at their original write stages.
"""
from collections.abc import Mapping
from dataclasses import dataclass

from psycopg import sql
from psycopg.types.json import Jsonb

from .channel_initial_error_fields import ERROR_FIELDS, normalize_initial_error_fields
from ..base.postgres import DatabaseError
from ..base.sql_values import identifier

COLUMN_TYPES = (
    ('channel_id','text'),('title','text'),('subscribers','bigint'),('views','bigint'),
    ('video_count','bigint'),('added_at','timestamptz'),('avatar_url','text'),('folder_id','uuid'),
    ('parent_channel_id','text'),('uploads_playlist_id','text'),('deep_bypass','boolean'),
    ('deep_bypass_reason','text'),('deep_inventory_status','text'),('deep_inventory_started_at','timestamptz'),
    ('deep_inventory_checked_at','timestamptz'),('deep_inventory_error','text'),
    ('initial_long_inventory_status','text'),('initial_28d_stats_status','text'),('is_hidden','boolean'),
    ('hidden_at','timestamptz'),('initial_long_inventory_started_at','timestamptz'),
    ('initial_28d_stats_started_at','timestamptz'),('channel_metadata_checked_at','timestamptz'),
    ('channel_metadata_next_after','timestamptz'),('channel_metadata_status','text'),
    ('channel_metadata_started_at','timestamptz'),('channel_metadata_error','text'),
    ('channel_metadata_error_count','integer'),('channel_video_count_changed_at','timestamptz'),
    ('country_code','text'),('country_collected_at','timestamptz'),
    ('initial_long_inventory_error_code','text'),('initial_long_inventory_error_at','timestamptz'),
    ('initial_long_inventory_error_count','integer'),('channel_created_at','timestamptz'),
    ('initial_inventory_estimate','jsonb'),('user_input','text'),('user_work_id','uuid'),
    ('user_work_restored','boolean'),
)
_TYPES = dict(COLUMN_TYPES)
_STATUS = 'initial_long_inventory_status'
_ESTIMATE = 'initial_inventory_estimate'
_RETURN_ERROR = """jsonb_build_object('channel_id',channel.channel_id,'error_code',channel.initial_long_inventory_error_code,
 'error_at',channel.initial_long_inventory_error_at,'error_count',channel.initial_long_inventory_error_count) AS value"""


def _native_row(expressions):
    return sql.SQL('ROW({})::public.channels').format(sql.SQL(',').join(
        expressions.get(name,sql.SQL('(locked.current).{}').format(identifier(name))) for name,_ in COLUMN_TYPES))


def _proposal(expressions, source, predicate, *, key_update=False, checks=()):
    # The original assignment expressions remain in their original order in the
    # locked target list; PostgreSQL evaluates casts/now()/int32 arithmetic.
    supplied=sql.SQL(',').join(sql.SQL('({})::{} AS {}').format(value,sql.SQL(_TYPES[name]),identifier(name)) for name,value in expressions.items())
    unknown=sql.SQL('').join(sql.SQL(',channel.{}').format(identifier(name)) for name in checks)
    complete=_native_row({name:sql.SQL('locked.{}').format(identifier(name)) for name in expressions})
    return sql.SQL("""WITH locked AS MATERIALIZED (
 SELECT channel AS current,{}{} FROM public.channels channel{} WHERE {} FOR {} OF channel),
 proposed AS (SELECT (locked.current).channel_id AS identity,to_jsonb(locked.current)::text AS old_text,
 {} AS value FROM locked)
 SELECT identity,old_text,to_jsonb(value)::text AS proposed_text,
 (value).initial_inventory_estimate::text AS estimate_text FROM proposed""").format(
        supplied,unknown,sql.SQL(source),sql.SQL(predicate),sql.SQL('UPDATE' if key_update else 'NO KEY UPDATE'),complete)


def _final_statement(names, *, returning=False):
    # Only original SET targets plus the three effect-owned fields are written.
    # Separate native JSONB text retains SQL NULL versus JSON null for estimate.
    fields=tuple(dict.fromkeys((*names,*ERROR_FIELDS)))
    terms=sql.SQL(',').join(sql.SQL('{}={}').format(identifier(name),
        sql.SQL('%s::jsonb') if name==_ESTIMATE else sql.SQL('proposed.{}').format(identifier(name))) for name in fields)
    statement=sql.SQL('UPDATE public.channels channel SET {} FROM jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed WHERE channel.channel_id=%s').format(terms)
    return statement+sql.SQL(' RETURNING '+_RETURN_ERROR if returning else '')


def _write_proposal(tx, row, names, *, returning=False):
    if row is None:
        return None if returning else 0
    if (not isinstance(row,Mapping) or set(row)!={'identity','old_text','proposed_text','estimate_text'}
            or type(row['identity']) is not str or type(row['old_text']) is not str
            or type(row['proposed_text']) is not str
            or (row['estimate_text'] is not None and type(row['estimate_text']) is not str)):
        raise DatabaseError('database_result_shape_invalid')
    normalized=normalize_initial_error_fields(tx,proposed_columns_text=row['proposed_text'],old_columns_text=row['old_text'],status_targeted=True)
    parameters=((row['estimate_text'],) if _ESTIMATE in names else ())+(normalized,row['identity'])
    method=tx.returning_one if returning else tx.execute
    return method(_final_statement(names,returning=returning),parameters)


def update_channel_fields(transaction, channel_id, values):
    """Control/metadata JSONB assignment stage, after each owner's validation.

    The exact original SRF input cast remains ahead of the native proposal. An
    untargeted SET list executes the original statement without reading OLD or
    normalizing it. Keep the caller's final fence and per-row savepoint outside.
    """
    values=dict(values)  # Match control owner Mapping-to-dict JSON adaptation.
    fields=tuple(sorted(values))
    terms=sql.SQL(',').join(sql.SQL('{}=proposed.{}').format(identifier(name),identifier(name)) for name in fields)
    original=sql.SQL('UPDATE public.channels channel SET {} FROM jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed WHERE channel.channel_id=%s').format(terms)
    parameters=(Jsonb(values),channel_id)
    with transaction.borrow():
        if _STATUS not in fields:
            return transaction.execute(original,parameters)
        expressions={name:sql.SQL('proposed.{}').format(identifier(name)) for name in fields}
        statement=_proposal(expressions,' CROSS JOIN jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed','channel.channel_id=%s',
                            key_update=bool({'channel_id','user_work_id'}&set(fields)))
        row=transaction.fetch_one(statement,parameters)
        return _write_proposal(transaction,row,fields)



@dataclass(frozen=True)
class _Stage:
    expressions: tuple
    predicate: str = 'channel.channel_id=%s::text'
    source: str = ''
    returning: bool = False
    no_effect: bool = False

    def original(self):
        terms=sql.SQL(',').join(sql.SQL('{}={}').format(identifier(name),sql.SQL(value)) for name,value in self.expressions)
        statement=sql.SQL('UPDATE public.channels channel SET {}{} WHERE {}').format(terms,sql.SQL(self.source.replace(' CROSS JOIN ',' FROM ',1)),sql.SQL(self.predicate))
        return statement+sql.SQL(' RETURNING '+_RETURN_ERROR if self.returning else '')

    def proposal(self):
        return _proposal({name:sql.SQL(value) for name,value in self.expressions},self.source,self.predicate)


_START='initial_long_inventory_started_at'
_STATS='initial_28d_stats_status'
_STATS_START='initial_28d_stats_started_at'
_ALL_STATUS="CASE WHEN %s::jsonb->'long'->>'status'='not_available' THEN 'skipped_no_long_playlist' ELSE 'completed' END"
_RETRY_FINISH="CASE WHEN cardinality(j.initial_video_types)=3 AND j.summary_json->'inventory_results'->'long'->>'status'='not_available' THEN 'skipped_no_long_playlist' ELSE 'completed' END"
_RETRY_PENDING="CASE WHEN cardinality(j.initial_video_types)=3 AND j.inventory_video_type<>'long' AND j.summary_json->'inventory_results' ? 'long' THEN CASE WHEN j.summary_json->'inventory_results'->'long'->>'status'='not_available' THEN 'skipped_no_long_playlist' ELSE 'completed' END ELSE 'pending' END"
_JOB_SOURCE=' CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j'
_STAGES={
    'enqueue_pending':_Stage(((_STATUS,"'pending'"),(_START,'NULL'),(_STATS,"'pending'")),no_effect=True),
    'claim_running':_Stage(((_STATUS,"'running'"),(_START,'coalesce(channel.initial_long_inventory_started_at,now())')),predicate='channel.channel_id=%s',no_effect=True),
    'page_absent':_Stage(((_STATUS,"'skipped_no_long_playlist'"),(_START,'NULL'),(ERROR_FIELDS[0],'NULL'),(ERROR_FIELDS[1],'NULL'),(ERROR_FIELDS[2],'0'),(_STATS,"'skipped'")),no_effect=True),
    'page_pending':_Stage(((_STATUS,"'pending'"),(_START,'NULL')),no_effect=True),
    'page_finish':_Stage(((_STATUS,"'completed'"),(_START,'NULL'),(_STATS,"'pending'"))),
    'page_all':_Stage(((_STATUS,_ALL_STATUS),(_START,'NULL'),(_STATS,"'pending'"))),
    'full_settlement':_Stage(((_STATUS,_ALL_STATUS),(_START,'NULL'),(_STATS,"'pending'"))),
    'retry_finish':_Stage(((_STATUS,_RETRY_FINISH),(_START,'NULL'),(_STATS,"'pending'"),(_STATS_START,'NULL')),source=_JOB_SOURCE,predicate='channel.channel_id=j.channel_id'),
    'retry_pending':_Stage(((_STATUS,_RETRY_PENDING),(_START,'NULL')),source=_JOB_SOURCE,predicate='channel.channel_id=j.channel_id'),
    'record_error':_Stage(((_STATUS,"'error'"),(_START,'NULL'),(ERROR_FIELDS[0],'%s::text'),(ERROR_FIELDS[1],'now()'),(ERROR_FIELDS[2],'channel.initial_long_inventory_error_count+1')),predicate='channel.channel_id=%s',returning=True),
}


def apply_channel_stage(transaction, stage, parameters):
    """Execute one finite existing DML site with its original parameter values.

    Parameters: channel id for enqueue/claim/page absent/pending/finish; native
    results JSONB text then id for page_all/full_settlement; native job JSONB text
    for retry modes; error code then id for record_error. The record-error result
    is the original RETURNING row, including None for no match. Other modes
    return affected count. Never open/retry a transaction or absorb an error.
    """
    spec=_STAGES[stage]
    with transaction.borrow():
        if spec.no_effect:
            return transaction.execute(spec.original(),parameters)
        row=transaction.fetch_one(spec.proposal(),parameters)
        return _write_proposal(transaction,row,tuple(name for name,_ in spec.expressions),returning=spec.returning)
