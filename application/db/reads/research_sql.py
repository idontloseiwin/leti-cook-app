"""Only registered field mappings may become SQL; all values are parameters.

No caller-provided SQL/identifiers, OFFSET, table names or ordering fragments.
This first current-page executor uses existing protected views, not raw tables.
"""
from functools import lru_cache
import unicodedata

from psycopg.types.json import Jsonb

from research_query.registry import UNICODE_VERSION
from research_query.validation import QueryRejected, ValidatedQuery


MAPPINGS = {
    "video.id": ("video_id", "text"), "video.channel_id": ("channel_id", "text"),
    "video.type": ("video_type", "text"), "video.title": ("title", "text"),
    "video.published_at": ("published_at", "timestamptz"),
    "video.duration_seconds": ("duration_seconds", "numeric"),
    "video.views": ("views", "numeric"),
}


@lru_cache(maxsize=1)
def casefold_map():
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise QueryRejected("backend_unavailable")
    # A normalization definition, never rows read from the database. PG17 lower
    # depends on locale and is not Unicode casefold (e.g. Straße / STRASSE).
    return {chr(n): chr(n).casefold() for n in range(0x110000)
            if chr(n).casefold() != chr(n)}


def _value(field, *, sort=False):
    column, kind = MAPPINGS[field]
    if sort and field == "video.title":
        column = "title_key"
    return column + (' COLLATE "C"' if kind == "text" else "")


def _bind(params, value, kind):
    key = "p" + str(len(params))
    params[key] = value
    return "%(" + key + ")s::" + kind


def _condition(node, params):
    if node is None:
        return "TRUE"
    for boolean, join in (("all", " AND "), ("any", " OR ")):
        if boolean in node:
            return "(" + join.join(_condition(v, params) for v in node[boolean]) + ")"
    if "not" in node:
        return "(NOT " + _condition(node["not"], params) + ")"
    field, op = node["field"], node["op"]
    value = _value(field)
    column, kind = MAPPINGS[field]
    if op in {"is_known", "is_unknown"}:
        return column + (" IS NOT NULL" if op == "is_known" else " IS NULL")
    if op == "state_is":
        state = "'known'" if field in {"video.id", "video.channel_id", "video.type"} else column + "_state"
        return state + " = " + _bind(params, node["value"], "text")
    if op == "contains_casefold_v1":
        return "strpos(title_key COLLATE \"C\", " + _bind(params, node["value"].casefold(), "text") + ") > 0"
    if op == "in":
        return value + " = ANY(" + _bind(params, node["value"], kind + "[]") + ")"
    symbol = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    return value + " " + symbol + " " + _bind(params, node["value"], kind)


def sort_definition(request):
    values = [(v.field, v.direction) for v in request.order_by]
    if not any(field == "video.id" for field, _ in values):
        values.append(("video.id", "asc"))
    return values


def _continuation(order, keys, params):
    if keys is None:
        return "TRUE"
    if len(keys) != len(order):
        raise QueryRejected("cursor_context_changed")
    terms, prefix = [], []
    for (field, direction), key in zip(order, keys):
        col, kind = _value(field, sort=True), MAPPINGS[field][1]
        bound = _bind(params, key, kind)
        if key is not None:
            comparator = ">" if direction == "asc" else "<"
            terms.append("(" + " AND ".join(prefix + [f"({col} {comparator} {bound} OR {col} IS NULL)"]) + ")")
        prefix.append(f"{col} IS NOT DISTINCT FROM {bound}")
    return "(" + " OR ".join(terms) + ")" if terms else "FALSE"


_SOURCE = """WITH args AS MATERIALIZED (
 SELECT statement_timestamp() AS evaluated_at, %(scope_id)s::text AS scope_id,
 %(scope_kind)s::text AS scope_kind, %(video_type)s::text AS video_type),
scope AS MATERIALIZED (
 SELECT c.channel_id,c.user_work_id FROM readable.channel_user_work c CROSS JOIN args a
 WHERE NOT c.is_hidden AND CASE WHEN a.scope_kind='channel' THEN c.channel_id=a.scope_id
 ELSE c.folder_id=a.scope_id::uuid END),
scope_info AS MATERIALIZED (
 SELECT CASE WHEN a.scope_kind='channel' THEN EXISTS(SELECT 1 FROM scope)
 ELSE EXISTS(SELECT 1 FROM public.folders WHERE id=a.scope_id::uuid) END AS source_exists,
 (SELECT count(*) FROM scope) AS channels_in_scope,
 encode(sha256(convert_to(coalesce((SELECT string_agg(channel_id||':'||user_work_id::text,',' ORDER BY channel_id COLLATE "C")
 FROM scope),''),'UTF8')),'hex') AS scope_revision FROM args a),
inventory_state AS MATERIALIZED (
 SELECT scope.channel_id,CASE WHEN state.inventory_status='playlist_not_found'
 AND state.source_checked_at<=a.evaluated_at AND state.source_expires_at>a.evaluated_at THEN true
 WHEN state.inventory_status IN('completed','running')
 AND state.source_checked_at<=a.evaluated_at AND state.source_expires_at>a.evaluated_at
 AND completed.scan_id IS NOT NULL AND full_scan.scan_id IS NOT NULL THEN true ELSE false END AS complete,
 least(state.source_expires_at,completed.source_expires_at,full_scan.source_expires_at) AS source_expires_at
 FROM scope CROSS JOIN args a
 LEFT JOIN readable.channel_video_inventory_state state ON state.channel_id=scope.channel_id AND state.video_type=a.video_type
 LEFT JOIN readable.channel_scan_sources completed ON completed.scan_id=state.last_completed_scan_id
 AND completed.channel_id=scope.channel_id AND completed.video_type=a.video_type AND completed.playlist_id=state.playlist_id
 AND completed.status='completed' AND completed.coverage='complete' AND completed.completed_at<=a.evaluated_at
 LEFT JOIN LATERAL (SELECT scan_id,source_expires_at FROM readable.channel_scan_sources s
 WHERE s.channel_id=scope.channel_id AND s.video_type=a.video_type AND s.playlist_id=state.playlist_id
 AND s.status='completed' AND s.coverage='complete' AND s.scan_kind IN('initial_inventory','full_reconciliation')
 AND s.completed_at<=a.evaluated_at ORDER BY s.completed_at DESC,s.scan_id DESC LIMIT 1) full_scan ON true),
raw AS MATERIALIZED (
 SELECT i.video_id,i.channel_id,i.video_type,h.source_checked_at AS identity_clock,h.source_expires_at AS identity_expiry,
 v.title,v.published_at,v.duration,v.views,v.checked_at,v.metadata_checked_at,v.availability_checked_at,
 v.source_expires_at AS video_expiry,v.availability_status,v.publication_status,v.stream_actual_end_at,
 a.evaluated_at,
 coalesce(isfinite(v.metadata_checked_at) AND v.metadata_checked_at>a.evaluated_at-interval '696 hours'
 AND v.metadata_checked_at<=a.evaluated_at,false) AS metadata_fresh,
 coalesce(isfinite(v.checked_at) AND v.checked_at>a.evaluated_at-interval '696 hours'
 AND v.checked_at<=a.evaluated_at,false) AS counters_fresh,
 coalesce(h.video_id IS NOT NULL AND h.source_expires_at>a.evaluated_at
 AND v.video_id IS NOT NULL AND v.channel_id=i.channel_id
 AND v.availability_status='public' AND v.availability_checked_at<=a.evaluated_at
 AND v.availability_checked_at>a.evaluated_at-interval '696 hours'
 AND v.publication_status IS NULL AND (v.published_at IS NULL OR v.published_at<=a.evaluated_at)
 AND (i.video_type<>'stream' OR (v.stream_actual_end_at IS NOT NULL AND v.stream_actual_end_at<=a.evaluated_at
 AND v.stream_actual_end_at<=v.checked_at AND v.stream_actual_end_at<=v.metadata_checked_at
 AND v.checked_at>a.evaluated_at-interval '696 hours' AND v.metadata_checked_at>a.evaluated_at-interval '696 hours'
 AND v.checked_at<=a.evaluated_at AND v.metadata_checked_at<=a.evaluated_at)),false) AS admitted
 FROM scope JOIN readable.video_inventory i USING(channel_id) CROSS JOIN args a
 LEFT JOIN readable.hot_video_inventory h ON h.video_id=i.video_id AND h.channel_id=i.channel_id AND h.video_type=i.video_type
 LEFT JOIN readable.videos v ON v.video_id=i.video_id
 WHERE i.video_type=a.video_type AND i.video_id ~ '^[A-Za-z0-9_-]{11}$'),
normalized AS MATERIALIZED (
 SELECT video_id,channel_id,video_type,identity_clock,identity_expiry,metadata_checked_at,checked_at,
 least(identity_expiry,availability_checked_at+interval '696 hours') AS core_expiry,
 CASE WHEN metadata_fresh AND char_length(title)<=1000 THEN title END AS title,
 CASE WHEN metadata_fresh AND isfinite(published_at) AND published_at>='0001-01-01Z'::timestamptz
 AND published_at<'10000-01-01Z'::timestamptz THEN published_at END AS published_at,
 CASE WHEN metadata_fresh AND parsed.seconds BETWEEN 0 AND 18446744073709551615 THEN parsed.seconds END AS duration_seconds,
 CASE WHEN counters_fresh AND views>=0 THEN views::numeric END AS views,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<=evaluated_at-interval '696 hours' THEN 'expired'
 WHEN metadata_checked_at>evaluated_at OR (title IS NOT NULL AND char_length(title)>1000) THEN 'unavailable'
 WHEN NOT metadata_fresh OR title IS NULL THEN 'unknown' ELSE 'known' END AS title_state,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<=evaluated_at-interval '696 hours' THEN 'expired'
 WHEN metadata_checked_at>evaluated_at THEN 'unavailable' WHEN NOT metadata_fresh OR published_at IS NULL OR NOT isfinite(published_at)
 OR published_at<'0001-01-01Z'::timestamptz OR published_at>='10000-01-01Z'::timestamptz THEN 'unknown' ELSE 'known' END AS published_at_state,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<=evaluated_at-interval '696 hours' THEN 'expired'
 WHEN metadata_checked_at>evaluated_at THEN 'unavailable' WHEN NOT metadata_fresh OR parsed.seconds IS NULL
 OR parsed.seconds NOT BETWEEN 0 AND 18446744073709551615 THEN 'unknown' ELSE 'known' END AS duration_seconds_state,
 CASE WHEN isfinite(checked_at) AND checked_at<=evaluated_at-interval '696 hours' THEN 'expired'
 WHEN checked_at>evaluated_at THEN 'unavailable' WHEN NOT counters_fresh OR views IS NULL OR views<0 THEN 'unknown' ELSE 'known' END AS views_state
 FROM raw LEFT JOIN LATERAL (SELECT upper(btrim(duration)) AS text) duration_text ON metadata_fresh
 LEFT JOIN LATERAL (SELECT regexp_match(text,'^P(?:([0-9]{1,20})D)?(?:T(?:([0-9]{1,20})H)?(?:([0-9]{1,20})M)?(?:([0-9]{1,20})S)?)?$') AS parts) duration_parts ON true
 LEFT JOIN LATERAL (SELECT CASE WHEN text ~ '^[0-9]{1,20}$' THEN text::numeric
 WHEN parts IS NOT NULL AND text ~ '[0-9]' AND (strpos(text,'T')=0 OR text ~ 'T.*[0-9]')
 THEN coalesce(parts[1]::numeric,0)*86400+coalesce(parts[2]::numeric,0)*3600+coalesce(parts[3]::numeric,0)*60+coalesce(parts[4]::numeric,0) END AS seconds) parsed ON true
 WHERE admitted),
prepared AS MATERIALIZED (SELECT normalized.*, {fold} AS title_key FROM normalized),
coverage AS MATERIALIZED (
 SELECT (SELECT count(*) FROM inventory_state WHERE complete) AS channels_complete,
 (SELECT count(*) FROM raw WHERE NOT admitted) AS rejected_rows,
 (SELECT count(*) FROM prepared WHERE NOT ({required})) AS required_missing,
 least((SELECT evaluated_at+interval '120 seconds' FROM args),
 (SELECT min(source_expires_at) FROM inventory_state WHERE source_expires_at>(SELECT evaluated_at FROM args)),
 (SELECT min(boundary) FROM raw CROSS JOIN LATERAL (VALUES(identity_expiry),(video_expiry),
 (checked_at+interval '696 hours'),(metadata_checked_at+interval '696 hours'),(availability_checked_at+interval '696 hours'),
 (checked_at),(metadata_checked_at),(availability_checked_at),(published_at)) deadlines(boundary)
 WHERE isfinite(boundary) AND boundary>evaluated_at)) AS valid_before),
page AS MATERIALIZED (
 SELECT prepared.*,{sort_keys} AS sort_keys FROM prepared
 WHERE ({required}) AND ({predicate}) AND ({continuation}) ORDER BY {order} LIMIT %(limit)s)
SELECT scope_info.*,coverage.*,a.evaluated_at,
 coalesce((SELECT jsonb_agg(to_jsonb(page) ORDER BY {page_order}) FROM page),'[]'::jsonb) AS rows
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""


def compile_query(plan, continuation=None):
    if not isinstance(plan, ValidatedQuery):
        raise QueryRejected("invalid_query")
    request = plan.request
    params = {"scope_id": request.scope.id, "scope_kind": request.scope.kind,
              "video_type": request.video_types[0], "limit": request.page.size + 1}
    obj = request.model_dump(mode="json", by_alias=True)
    order = sort_definition(request)
    need_fold = any(field == "video.title" for field, _ in order) or b'"contains_casefold_v1"' in plan.encoded
    fold = "NULL::text"
    if need_fold:
        params["fold_map"] = Jsonb(casefold_map())
        fold = """CASE WHEN title IS NULL THEN NULL WHEN title !~ '[^ -~]' THEN
 translate(title,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') ELSE
 (SELECT coalesce(string_agg(coalesce(%(fold_map)s::jsonb->>ch,ch),'' ORDER BY ordinal),'')
 FROM regexp_split_to_table(title,'') WITH ORDINALITY AS chars(ch,ordinal)) END"""
    required = " AND ".join(MAPPINGS[f][0] + " IS NOT NULL" for f in request.coverage.required_fields) or "TRUE"
    predicate = _condition(obj["where"], params)
    keyset = _continuation(order, continuation["keys"] if continuation else None, params)
    ordering = ",".join(_value(f, sort=True) + " " + d.upper() + " NULLS LAST" for f, d in order)
    key_values = ",".join("(" + _value(f, sort=True) + ")::text" for f, _ in order)
    statement = _SOURCE
    for key, value in {"fold": fold, "required": required, "predicate": predicate,
                       "continuation": keyset, "order": ordering, "page_order": ordering,
                       "sort_keys": "jsonb_build_array("+key_values+")"}.items():
        statement = statement.replace("{"+key+"}", value)
    return statement, params
