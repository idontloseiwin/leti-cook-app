"""Direct quota ledger writers sharing their public owner transaction.

Only native typed labels cross to the Python vocabulary owner. The supplied
writer transaction owns locks, defaults, constraints and acknowledgement. This
module does not reserve quota, call YouTube, retry, or change journal semantics.
"""
from collections.abc import Mapping
from psycopg import sql
from ..base.postgres import DatabaseError, DatabaseServerError
from ..base.sql_values import assignments, identifier, insert_values
from operational_copy_policy import quota_label

COLUMN_TYPES = (
    ('request_attempt_id','uuid'), ('project_id','text'), ('quota_bucket','text'),
    ('quota_day','date'), ('phase','text'), ('endpoint','text'), ('quota_units','integer'),
    ('status','text'), ('started_at','timestamptz'), ('finished_at','timestamptz'),
    ('duration_ms','integer'), ('http_status','integer'), ('error_type','text'),
    ('retry_of_request_attempt_id','uuid'),
)
_TYPES = dict(COLUMN_TYPES)
_LABELS = ('phase','endpoint','quota_bucket','error_type')
_DEFAULTS = {name:'NULL::'+kind for name,kind in COLUMN_TYPES}
_DEFAULTS.update(request_attempt_id='gen_random_uuid()',
    quota_day="(now() AT TIME ZONE 'America/Los_Angeles')::date",
    status="'started'::text",started_at='now()')
_RUNNING = "ledger.request_attempt_id=%s::uuid AND ledger.status='started'"


def _select_native():
    return sql.SQL('SELECT to_jsonb(value)::text AS columns_text,{} FROM proposed').format(
        sql.SQL(',').join(sql.SQL('(value).{} AS {}').format(identifier(name),identifier(name)) for name in _LABELS))


def _proposal(values,*,insert):
    supplied=sql.SQL(',').join(sql.SQL('%s::{} AS {}').format(sql.SQL(_TYPES.get(name,'text')),identifier(name)) for name in values)
    unknown=tuple(name for name in values if name not in _TYPES)
    if insert:
        checks=(sql.SQL(',unknown_columns AS (SELECT {} FROM public.youtube_api_request_attempts ledger WHERE false)').format(
            sql.SQL(',').join(sql.SQL('ledger.{}').format(identifier(name)) for name in unknown)) if unknown else sql.SQL(''))
        fields=sql.SQL(',').join(sql.SQL('supplied.{}').format(identifier(name)) if name in values else sql.SQL(_DEFAULTS[name]) for name,_ in COLUMN_TYPES)
        ctes=sql.SQL('WITH supplied AS (SELECT {}){},proposed AS MATERIALIZED (SELECT ROW({})::public.youtube_api_request_attempts AS value FROM supplied) ').format(supplied,checks,fields)
    else:
        checks=sql.SQL('').join(sql.SQL(',ledger.{}').format(identifier(name)) for name in unknown)
        fields=sql.SQL(',').join(sql.SQL('locked.{}').format(identifier(name)) if name in values else sql.SQL('(locked.current).{}').format(identifier(name)) for name,_ in COLUMN_TYPES)
        lock = 'UPDATE' if 'request_attempt_id' in values else 'NO KEY UPDATE'
        ctes=sql.SQL('WITH locked AS MATERIALIZED (SELECT ledger AS current,{}{} FROM public.youtube_api_request_attempts ledger WHERE '+_RUNNING+' FOR '+lock+' OF ledger),proposed AS (SELECT ROW({})::public.youtube_api_request_attempts AS value FROM locked) ').format(supplied,checks,fields)
    return ctes+_select_native()


def _labels(row):
    if (not isinstance(row,Mapping) or set(row)!={'columns_text',*_LABELS}
            or type(row['columns_text']) is not str
            or any(row[name] is not None and type(row[name]) is not str for name in _LABELS)):
        raise DatabaseError('database_result_shape_invalid')
    return tuple(quota_label(name,row[name]) for name in _LABELS)


def _insert_statement():
    fields=sql.SQL(',').join(identifier(name) for name,_ in COLUMN_TYPES)
    # Bind canonical labels in one stable order, independent of physical layout.
    expressions=sql.SQL(',').join(sql.SQL('safe.{}').format(identifier(name)) if name in _LABELS else sql.SQL('proposed.{}').format(identifier(name)) for name,_ in COLUMN_TYPES)
    return sql.SQL('INSERT INTO public.youtube_api_request_attempts ({}) SELECT {} FROM (SELECT %s::text AS phase,%s::text AS endpoint,%s::text AS quota_bucket,%s::text AS error_type) safe CROSS JOIN jsonb_populate_record(NULL::public.youtube_api_request_attempts,%s::jsonb) proposed').format(fields,expressions)


_INSERT=_insert_statement()


def start_quota_attempt(transaction,values):
    """INSERT with native defaults evaluated once before normalization."""
    values=dict(values)
    _,_,params=insert_values([values])
    with transaction.borrow():
        transaction.validate_operational_assignments('quota_start', values)
        row=transaction.fetch_one(_proposal(values,insert=True),params)
        if any(name not in _TYPES for name in values):
            raise DatabaseServerError('database_operation_failed',code='42703')
        labels=_labels(row)
        return transaction.execute(_INSERT,(*labels,row['columns_text']))


def finish_quota_attempt(transaction,attempt_id,values):
    """Locked started-row UPDATE; public id trimming stays outside."""
    values=dict(values)
    _,params=assignments(values)
    with transaction.borrow():
        transaction.validate_operational_assignments('quota_finish', values, identity=attempt_id)
        row=transaction.fetch_one(_proposal(values,insert=False),params+(attempt_id,))
        if any(name not in _TYPES for name in values):
            raise DatabaseServerError('database_operation_failed',code='42703')
        if row is None:return 0
        labels=_labels(row)
        columns=(*values,*(name for name in _LABELS if name not in values))
        terms=sql.SQL(',').join(sql.SQL('{}={}').format(identifier(name),
            sql.SQL('safe.{}').format(identifier(name)) if name in _LABELS else sql.SQL('proposed.{}').format(identifier(name))) for name in columns)
        statement=sql.SQL('UPDATE public.youtube_api_request_attempts ledger SET {} FROM (SELECT %s::text AS phase,%s::text AS endpoint,%s::text AS quota_bucket,%s::text AS error_type) safe CROSS JOIN jsonb_populate_record(NULL::public.youtube_api_request_attempts,%s::jsonb) proposed WHERE '+_RUNNING).format(terms)
        return transaction.execute(statement,(*labels,row['columns_text'],attempt_id))
