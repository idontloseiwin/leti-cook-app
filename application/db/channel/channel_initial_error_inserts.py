"""Channel INSERT owners with native initial-error normalization.

Create captures one complete native row with the exact omitted defaults, then
normalizes before one final INSERT. The composed writer supplies estimate and
INSERT side effects; native integrity guards retain their statement order.
Restore's validated pending/manual/NULL statuses are a proved no-effect path
and keep its exact INSERT.
"""
from collections.abc import Mapping
from psycopg import sql
from psycopg.types.json import Jsonb
from .channel_initial_error_fields import normalize_initial_error_fields
from ..base.postgres import DatabaseError
from ..base.sql_values import identifier

# schema24 physical order/types are identical to the accepted schema23 oracle.
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
_DEFAULTS = {'deep_bypass':'false','deep_inventory_status':"'idle'::text",'is_hidden':'false',
 'channel_metadata_error_count':'0','initial_long_inventory_error_count':'0',
 'user_work_id':'gen_random_uuid()','user_work_restored':'false'}
_NOT_NULL = {'channel_id','is_hidden','channel_metadata_error_count','initial_long_inventory_error_count','user_work_id','user_work_restored'}
_SCHEMA_PIN = """WITH expected(position,name,kind,not_null,default_expression) AS (
 SELECT * FROM unnest(%s::integer[],%s::text[],%s::text[],%s::boolean[],%s::text[])
), actual AS MATERIALIZED (
 SELECT row_number() OVER (ORDER BY a.attnum) AS position,a.attname,pg_catalog.format_type(a.atttypid,a.atttypmod) AS kind,
 a.attnotnull,a.attidentity,a.attgenerated,pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS expression
 FROM pg_catalog.pg_attribute a LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE a.attrelid='public.channels'::regclass AND a.attnum>0 AND NOT a.attisdropped
), relation_guard AS (SELECT count(*) FROM public.channels WHERE false)
SELECT (SELECT count(*) FROM actual)=39
 AND EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE oid='public.channels'::regclass
  AND relkind='r' AND relrowsecurity AND pg_catalog.pg_get_userbyid(relowner)='postgres')
 AND NOT EXISTS(SELECT 1 FROM expected e LEFT JOIN actual a ON a.position=e.position
  WHERE a.attname IS DISTINCT FROM e.name OR a.kind IS DISTINCT FROM e.kind
   OR a.attnotnull IS DISTINCT FROM e.not_null OR a.attidentity IS DISTINCT FROM ''
   OR a.attgenerated IS DISTINCT FROM '' OR a.expression IS DISTINCT FROM e.default_expression) AS matches
FROM relation_guard"""
_SCHEMA_PARAMETERS = (list(range(1,40)),[name for name,_ in COLUMN_TYPES],
 ['timestamp with time zone' if kind=='timestamptz' else kind for _,kind in COLUMN_TYPES],
 [name in _NOT_NULL for name,_ in COLUMN_TYPES],[_DEFAULTS.get(name) for name,_ in COLUMN_TYPES])


def _proposal_statement(fields):
    supplied=set(fields)
    # Original JSONB SRF casts the complete input first. Omissions use the same
    # native defaults in physical destination order. MATERIALIZED captures the
    # volatile UUID once even though both full-row and JSONB texts are returned.
    terms=sql.SQL(',').join(sql.SQL('input.{}').format(identifier(name)) if name in supplied
        else sql.SQL(_DEFAULTS.get(name,'NULL::'+kind)) for name,kind in COLUMN_TYPES)
    return sql.SQL("""WITH proposed AS MATERIALIZED (
 SELECT ROW({})::public.channels AS value
 FROM jsonb_populate_record(NULL::public.channels,%s::jsonb) input)
 SELECT to_jsonb(value)::text AS columns_text,(value).initial_inventory_estimate::text AS estimate_text FROM proposed""").format(terms)


def _insert_statement():
    fields=sql.SQL(',').join(identifier(name) for name,_ in COLUMN_TYPES)
    terms=sql.SQL(',').join(sql.SQL('%s::jsonb') if name=='initial_inventory_estimate'
        else sql.SQL('proposed.{}').format(identifier(name)) for name,_ in COLUMN_TYPES)
    return sql.SQL('INSERT INTO public.channels ({}) SELECT {} FROM jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed RETURNING channel_id').format(fields,terms)


_CREATE_INSERT = _insert_statement()


def insert_collection_channel(transaction, values):
    """Replace only create_channel_for_collection's validated final INSERT.

    The owner keeps its permission/shape checks, required channel_id, subsequent
    control permission update, result lookup and discovery-initialized check.
    This JSONB-composite input is not an ordinary assignment adapter and does
    not use the operational EXPLAIN preflight. Native parity remains required.
    """
    values=dict(values)
    fields=tuple(sorted(values))
    for name in fields:identifier(name)
    with transaction.borrow():
        contract=transaction.fetch_one(_SCHEMA_PIN,_SCHEMA_PARAMETERS)
        if not isinstance(contract,Mapping) or set(contract)!={'matches'} or type(contract['matches']) is not bool:
            raise DatabaseError('database_result_shape_invalid')
        if not contract['matches']:
            raise DatabaseError('database_operation_failed',code='P0001')
        row=transaction.fetch_one(_proposal_statement(fields),(Jsonb(values),))
        if (not isinstance(row,Mapping) or set(row)!={'columns_text','estimate_text'}
                or type(row['columns_text']) is not str
                or (row['estimate_text'] is not None and type(row['estimate_text']) is not str)):
            raise DatabaseError('database_result_shape_invalid')
        normalized=normalize_initial_error_fields(transaction,proposed_columns_text=row['columns_text'],inserted=True)
        return transaction.returning_one(_CREATE_INSERT,(row['estimate_text'],normalized))


# Exact existing restore INSERT text is appended below. Its owner validates the
# channel-data shape first: pending/manual_required and nullable SQL status can
# reach this site, completed/error cannot. The initial-error effect is identity.
# Preserve native omissions and clock_timestamp() evaluation in this one DML;
# the later control UPDATE still owns its distinct clock sample.

_RESTORE_INSERT = "INSERT INTO public.channels(channel_id,user_input,user_work_id,user_work_restored,folder_id,parent_channel_id,is_hidden,hidden_at,added_at,\n title,avatar_url,uploads_playlist_id,subscribers,views,video_count,country_code,country_collected_at,channel_created_at,\n channel_metadata_checked_at,channel_metadata_next_after,channel_metadata_status,channel_metadata_started_at,channel_metadata_error,channel_metadata_error_count,\n initial_inventory_estimate,initial_long_inventory_status,initial_28d_stats_status,deep_inventory_error)\n SELECT %s::text,%s::text,%s::uuid,true,%s::uuid,%s::text,%s::boolean,\n CASE WHEN %s::boolean THEN coalesce(%s::timestamptz,clock_timestamp()) END,%s::timestamptz,\n data->>'title',data->>'avatar_url',data->>'uploads_playlist_id',\n (data->>'subscribers')::bigint,(data->>'views')::bigint,(data->>'video_count')::bigint,\n data->>'country_code',(data->>'country_collected_at')::timestamptz,(data->>'channel_created_at')::timestamptz,\n %s::timestamptz,(data->>'channel_metadata_next_after')::timestamptz,'ok',NULL,NULL,0,\n data->'initial_inventory_estimate',data->>'initial_long_inventory_status',data->>'initial_28d_stats_status',NULL\n FROM (SELECT %s::jsonb AS data) input"


def insert_restored_channel(transaction, parameters):
    """Replace only validated restore-bind new-channel INSERT; return its count."""
    with transaction.borrow():
        return transaction.execute(_RESTORE_INSERT,parameters)
