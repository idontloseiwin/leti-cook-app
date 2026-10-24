"""Single-row sync-run INSERT owners with native assignment validation.

The caller supplies one writer transaction. Incoming values retain the original
insert_values/Jsonb boundary; omission uses fixed native defaults. A full typed
proposed row is captured once, normalized before table constraints and inserted
without resampling its sequence or clocks.
"""
from collections.abc import Mapping

from psycopg import sql

from ..base.postgres import DatabaseError, DatabaseServerError
from ..base.sql_values import identifier, insert_values
from .sync_run_write_normalization import COLUMN_TYPES, _final_summary

_TYPES = dict(COLUMN_TYPES)
_DEFAULTS = {
    'id': "nextval('public.sync_runs_id_seq'::regclass)",
    'run_type': "'auto_sync'::text", 'status': "'running'::text",
    'started_at': 'now()', 'finished_at': 'NULL::timestamptz',
    'duration_seconds': 'NULL::double precision',
    **{name:'0' for name,kind in COLUMN_TYPES if kind == 'integer'},
    'summary_json': "'{}'::jsonb", 'created_at': 'now()',
}
# The catalog pin is finite metadata only. The zero-row relation reference also
# acquires PostgreSQL's relation lock before defaults are checked/captured.
# No source rows are read and no id/default is consumed by this first statement.
_SCHEMA_PIN = """WITH expected(position,name,kind,not_null,default_expression) AS (
 SELECT * FROM unnest(%s::integer[],%s::text[],%s::text[],%s::boolean[],%s::text[])
), actual AS MATERIALIZED (
 SELECT a.attnum,a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod) AS kind,
 a.attnotnull,a.attidentity,a.attgenerated,pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS expression
 FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE a.attrelid='public.sync_runs'::regclass AND a.attnum>0 AND NOT a.attisdropped
), relation_guard AS (SELECT count(*) FROM public.sync_runs WHERE false)
SELECT (SELECT count(*) FROM actual)=20
 AND EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE oid='public.sync_runs'::regclass
  AND relkind='r' AND relrowsecurity AND pg_catalog.pg_get_userbyid(relowner)='postgres')
 AND NOT EXISTS(SELECT 1 FROM expected e LEFT JOIN actual a ON a.attnum=e.position
  WHERE a.attname IS DISTINCT FROM e.name OR a.kind IS DISTINCT FROM e.kind
   OR a.attnotnull IS DISTINCT FROM e.not_null OR a.attidentity IS DISTINCT FROM ''
   OR a.attgenerated IS DISTINCT FROM '' OR (CASE WHEN e.name='id'
     THEN a.expression IN ('nextval(''sync_runs_id_seq''::regclass)','nextval(''public.sync_runs_id_seq''::regclass)')
     ELSE a.expression IS NOT DISTINCT FROM e.default_expression END) IS DISTINCT FROM true) AS matches
FROM relation_guard"""
_SCHEMA_PARAMETERS = (
    list(range(1,21)), [name for name,_ in COLUMN_TYPES],
    ['timestamp with time zone' if kind=='timestamptz' else kind for _,kind in COLUMN_TYPES],
    [name not in {'finished_at','duration_seconds'} for name,_ in COLUMN_TYPES],
    [None if name in {'finished_at','duration_seconds'} else _DEFAULTS[name] for name,_ in COLUMN_TYPES],
)


def _proposal_statement(values):
    # Keep incoming parameter order for adaptation. The nonmaterialized supplied
    # CTE is projected into the physical row order; the outer materialization
    # evaluates volatile id/default expressions once even when two texts are read.
    supplied = sql.SQL(',').join(sql.SQL('%s::{} AS {}').format(sql.SQL(_TYPES.get(name,'text')),identifier(name))
                               for name in values)
    fields = sql.SQL(',').join(sql.SQL('supplied.{}').format(identifier(name)) if name in values
                              else sql.SQL(_DEFAULTS[name]) for name,_ in COLUMN_TYPES)
    unknown = [name for name in values if name not in _TYPES]
    unknown_parse = (sql.SQL(', unknown_columns AS (SELECT {} FROM public.sync_runs run WHERE false)').format(
        sql.SQL(',').join(sql.SQL('run.{}').format(identifier(name)) for name in unknown)) if unknown else sql.SQL(''))
    return sql.SQL("""WITH supplied AS (SELECT {}){}, proposed AS MATERIALIZED (
 SELECT ROW({})::public.sync_runs AS value FROM supplied)
 SELECT to_jsonb(value)::text AS columns_text,(value).summary_json::text AS summary_text FROM proposed""").format(
        supplied,unknown_parse,fields)


def _insert_statement(*,returning):
    fields = sql.SQL(',').join(identifier(name) for name,_ in COLUMN_TYPES)
    expressions = sql.SQL(',').join(sql.SQL('%s::jsonb') if name=='summary_json'
        else sql.SQL('proposed.{}').format(identifier(name)) for name,_ in COLUMN_TYPES)
    result = sql.SQL('INSERT INTO public.sync_runs ({}) SELECT {} FROM jsonb_populate_record(NULL::public.sync_runs,%s::jsonb) proposed').format(fields,expressions)
    return result + sql.SQL(' RETURNING id' if returning else '')


_START_INSERT = _insert_statement(returning=True)
_FINISH_INSERT = _insert_statement(returning=False)


def _insert(transaction,values,*,returning):
    values = dict(values)
    # Preserve empty, >256 and malformed-identifier rejection before any SQL.
    _,_,parameters = insert_values([values],json_columns={'summary_json'})
    with transaction.borrow():
        transaction.validate_operational_assignments('sync_start' if returning else 'sync_finish_insert', values)
        contract = transaction.fetch_one(_SCHEMA_PIN,_SCHEMA_PARAMETERS)
        if not isinstance(contract,Mapping) or set(contract)!={'matches'} or type(contract['matches']) is not bool:
            raise DatabaseError('database_result_shape_invalid')
        if not contract['matches']:
            raise DatabaseError('database_operation_failed',code='P0001')
        proposed = transaction.fetch_one(_proposal_statement(values),parameters)
        if any(name not in _TYPES for name in values):
            raise DatabaseServerError('database_operation_failed',code='42703')
        summary = _final_summary(transaction,proposed)
        parameters = (summary,proposed['columns_text'])
        if not returning:
            return transaction.execute(_FINISH_INSERT,parameters)
        row = transaction.returning_one(_START_INSERT,parameters)
        if row is None:
            return None
        if not isinstance(row,Mapping) or set(row)!={'id'} or (row['id'] is not None and type(row['id']) is not int):
            raise DatabaseError('database_result_shape_invalid')
        return row['id']


def start_sync_run(transaction,values):
    """Start INSERT with the original id-or-None receipt."""
    return _insert(transaction,values,returning=True)


def finish_insert_sync_run(transaction,values):
    """Missing-id finish INSERT; return matched count to its owner."""
    return _insert(transaction,values,returning=False)
