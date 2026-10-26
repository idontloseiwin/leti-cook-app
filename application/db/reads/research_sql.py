"""Only registered field mappings may become SQL; all values are parameters.

No caller-provided SQL/identifiers, OFFSET, table names or ordering fragments.
This first current-page executor uses existing protected views, not raw tables.
"""
from functools import lru_cache
import unicodedata

from psycopg.types.json import Jsonb

from hot_storage import RETENTION_HOURS
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


@lru_cache(maxsize=1)
def casefold_special():
    """Characters whose casefold differs from PostgreSQL 17's builtin simple
    lowercase (collation pg_c_utf8): full folds such as ß, final sigma, µ, İ.
    Any other title folds exactly as lower(title COLLATE pg_c_utf8)."""
    casefold_map()  # same pinned Unicode version
    return "".join(chr(n) for n in range(1, 0x110000) if not 0xD800 <= n <= 0xDFFF
                   and (chr(n).casefold() != chr(n).lower() or len(chr(n).lower()) != 1))


# Normalization rules shared by the catalogue and the read-model refresh: the
# same SQL text computes a stored row and a row that is read live.
DURATION_PATTERN = "^P(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)S)?)?$"


def duration_seconds_sql(text, parts):
    """Seconds of an upper-cased, trimmed ISO 8601 duration `text`, whose
    regexp_match against DURATION_PATTERN is `parts`. At most 20 digits per part
    (leading zeros included); a bounded {1,20} repeat is several times slower."""
    return (f"CASE WHEN {text} ~ '^[0-9]+$' THEN CASE WHEN char_length({text})<=20 THEN {text}::numeric END"
            f" WHEN {parts} IS NOT NULL AND coalesce(char_length({parts}[1]),0)<=20 AND coalesce(char_length({parts}[2]),0)<=20"
            f" AND coalesce(char_length({parts}[3]),0)<=20 AND coalesce(char_length({parts}[4]),0)<=20"
            f" AND {text} ~ '[0-9]' AND (strpos({text},'T')=0 OR {text} ~ 'T.*[0-9]')"
            f" THEN coalesce({parts}[1]::numeric,0)*86400+coalesce({parts}[2]::numeric,0)*3600"
            f"+coalesce({parts}[3]::numeric,0)*60+coalesce({parts}[4]::numeric,0) END")


def casefold_sql(title):
    """Unicode 15.0 casefold of `title`. PostgreSQL 17's builtin pg_c_utf8
    lowercase is exact except for casefold_special(); only a title containing
    one of those takes the per-character map, about eight times slower."""
    return (f"CASE WHEN {title} IS NULL THEN NULL WHEN {title} !~ '[^ -~]' THEN"
            f" translate({title},'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
            f" WHEN {title} !~ %(fold_special)s THEN lower({title} COLLATE pg_c_utf8) ELSE"
            f" (SELECT coalesce(string_agg(coalesce(%(fold_map)s::jsonb->>ch,ch),'' ORDER BY ordinal),'')"
            f" FROM regexp_split_to_table({title},'') WITH ORDINALITY AS chars(ch,ordinal)) END")


def casefold_parameters():
    return {"fold_map": Jsonb(casefold_map()), "fold_special": "[" + casefold_special() + "]"}


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


# Rows come from readable.video_search_projection: the protected views' type
# proof as of its last refresh, used only while proof_current, plus the exact
# stored duration seconds and casefold title key, masked by readable.videos.
# A row that is queued after a write or past a source clock is read from the
# protected views instead, which stay the sole owners of type policy. Their
# keys are leakproof and reach indexes; video_inventory is keyed by video_id,
# so each yields at most one row, and LIMIT 1 only prevents flattening. The
# source lifetime is one bound parameter, the HOT window in hours.
_SOURCE = """WITH args AS MATERIALIZED (
 SELECT statement_timestamp() AS evaluated_at, %(scope_id)s::text AS scope_id,
 %(scope_kind)s::text AS scope_kind, %(video_type)s::text AS video_type),
scope AS MATERIALIZED (
 SELECT c.channel_id,c.user_work_id FROM readable.channel_user_work c CROSS JOIN args a
 WHERE NOT c.is_hidden AND {scope_predicate}),
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
 SELECT p.video_id,p.channel_id,i.video_type,h.source_checked_at AS identity_clock,h.source_expires_at AS identity_expiry,
 p.title,p.published_at,p.duration,p.views,p.checked_at,p.metadata_checked_at,p.availability_checked_at,
 p.video_expiry,p.availability_status,p.publication_status,p.stream_actual_end_at,
 {stored_title_key} AS stored_title_key,
 CASE WHEN p.proof_current THEN p.duration_seconds ELSE {live_duration} END AS stored_duration_seconds,
 a.evaluated_at,
 coalesce(isfinite(p.metadata_checked_at) AND p.metadata_checked_at>a.evaluated_at-%(window)s::interval
 AND p.metadata_checked_at<=a.evaluated_at,false) AS metadata_fresh,
 coalesce(isfinite(p.checked_at) AND p.checked_at>a.evaluated_at-%(window)s::interval
 AND p.checked_at<=a.evaluated_at,false) AS counters_fresh,
 coalesce(h.video_id IS NOT NULL AND h.source_expires_at>a.evaluated_at
 AND p.v_video_id IS NOT NULL AND p.v_channel_id=p.channel_id
 AND p.availability_status='public' AND p.availability_checked_at<=a.evaluated_at
 AND p.availability_checked_at>a.evaluated_at-%(window)s::interval
 AND p.publication_status IS NULL AND (p.published_at IS NULL OR p.published_at<=a.evaluated_at)
 AND (i.video_type<>'stream' OR (p.stream_actual_end_at IS NOT NULL AND p.stream_actual_end_at<=a.evaluated_at
 AND p.stream_actual_end_at<=p.checked_at AND p.stream_actual_end_at<=p.metadata_checked_at
 AND p.checked_at>a.evaluated_at-%(window)s::interval AND p.metadata_checked_at>a.evaluated_at-%(window)s::interval
 AND p.checked_at<=a.evaluated_at AND p.metadata_checked_at<=a.evaluated_at)),false) AS admitted
 FROM scope JOIN readable.video_search_projection p USING(channel_id) CROSS JOIN args a
 LEFT JOIN LATERAL (SELECT x.video_type FROM readable.video_inventory x
 WHERE NOT p.proof_current AND x.video_id=p.video_id LIMIT 1) live ON true
 CROSS JOIN LATERAL (SELECT CASE WHEN p.proof_current THEN p.inventory_type ELSE live.video_type END AS video_type) i
 LEFT JOIN LATERAL (SELECT upper(btrim(p.duration)) AS text WHERE NOT p.proof_current LIMIT 1) duration_text ON true
 LEFT JOIN LATERAL (SELECT regexp_match(duration_text.text,%(duration_pattern)s) AS parts
 WHERE duration_text.text IS NOT NULL LIMIT 1) duration_parts ON true
 LEFT JOIN LATERAL ((SELECT p.video_id AS video_id,p.identity_clock AS source_checked_at,p.identity_expiry AS source_expires_at
 WHERE p.proof_current AND p.hot_present)
 UNION ALL (SELECT h0.video_id,h0.source_checked_at,h0.source_expires_at FROM readable.hot_video_inventory h0
 WHERE NOT p.proof_current AND h0.video_id=p.video_id AND h0.channel_id=p.channel_id AND h0.video_type=i.video_type LIMIT 1)) h ON true
 WHERE i.video_type=%(video_type)s::text{inventory_scope} AND p.video_id ~ '^[A-Za-z0-9_-]{11}$'),
normalized AS MATERIALIZED (
 SELECT {title_key} AS title_key,video_id,channel_id,video_type,identity_clock,identity_expiry,metadata_checked_at,checked_at,
 least(identity_expiry,availability_checked_at+%(window)s::interval) AS core_expiry,
 CASE WHEN metadata_fresh AND char_length(title)<=1000 THEN title END AS title,
 CASE WHEN metadata_fresh AND isfinite(published_at) AND published_at>='0001-01-01Z'::timestamptz
 AND published_at<'10000-01-01Z'::timestamptz THEN published_at END AS published_at,
 CASE WHEN metadata_fresh AND parsed.seconds BETWEEN 0 AND 18446744073709551615 THEN parsed.seconds END AS duration_seconds,
 CASE WHEN counters_fresh AND views>=0 THEN views::numeric END AS views,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<=evaluated_at-%(window)s::interval THEN 'expired'
 WHEN metadata_checked_at>evaluated_at OR (title IS NOT NULL AND char_length(title)>1000) THEN 'unavailable'
 WHEN NOT metadata_fresh OR title IS NULL THEN 'unknown' ELSE 'known' END AS title_state,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<=evaluated_at-%(window)s::interval THEN 'expired'
 WHEN metadata_checked_at>evaluated_at THEN 'unavailable' WHEN NOT metadata_fresh OR published_at IS NULL OR NOT isfinite(published_at)
 OR published_at<'0001-01-01Z'::timestamptz OR published_at>='10000-01-01Z'::timestamptz THEN 'unknown' ELSE 'known' END AS published_at_state,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<=evaluated_at-%(window)s::interval THEN 'expired'
 WHEN metadata_checked_at>evaluated_at THEN 'unavailable' WHEN NOT metadata_fresh OR parsed.seconds IS NULL
 OR parsed.seconds NOT BETWEEN 0 AND 18446744073709551615 THEN 'unknown' ELSE 'known' END AS duration_seconds_state,
 CASE WHEN isfinite(checked_at) AND checked_at<=evaluated_at-%(window)s::interval THEN 'expired'
 WHEN checked_at>evaluated_at THEN 'unavailable' WHEN NOT counters_fresh OR views IS NULL OR views<0 THEN 'unknown' ELSE 'known' END AS views_state
 FROM raw CROSS JOIN LATERAL (SELECT CASE WHEN raw.metadata_fresh THEN raw.stored_duration_seconds END AS seconds) parsed
 WHERE admitted),
prepared AS MATERIALIZED (SELECT normalized.* FROM normalized),
coverage AS MATERIALIZED (
 SELECT (SELECT count(*) FROM inventory_state WHERE complete) AS channels_complete,
 (SELECT count(*) FROM raw WHERE NOT admitted) AS rejected_rows,
 (SELECT count(*) FROM prepared WHERE NOT ({required})) AS required_missing,
 least((SELECT evaluated_at+interval '120 seconds' FROM args),
 (SELECT min(source_expires_at) FROM inventory_state WHERE source_expires_at>(SELECT evaluated_at FROM args)),
 (SELECT min(boundary) FROM raw CROSS JOIN LATERAL (VALUES(identity_expiry),(video_expiry),
 (checked_at+%(window)s::interval),(metadata_checked_at+%(window)s::interval),(availability_checked_at+%(window)s::interval),
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
              "video_type": request.video_types[0], "limit": request.page.size + 1,
              # Exact hours: an interval of days follows the session time zone
              # across daylight-saving changes, so 29 days is not always 696 hours.
              "window": f"{RETENTION_HOURS} hours"}
    obj = request.model_dump(mode="json", by_alias=True)
    order = sort_definition(request)
    need_fold = any(field == "video.title" for field, _ in order) or b'"contains_casefold_v1"' in plan.encoded
    # public.casefold_v1 stores the key when a title is written; it is exposed
    # only with the same gating as the title itself.
    title_key = ("CASE WHEN metadata_fresh AND char_length(title)<=1000 THEN stored_title_key END"
                 if need_fold else "NULL::text")
    stored_title_key = "NULL::text"
    if need_fold:
        # Literals fold in Python: casefold_map refuses another Unicode version.
        params.update(casefold_parameters())
        stored_title_key = "CASE WHEN p.proof_current THEN p.title_key ELSE " + casefold_sql("p.title") + " END"
    params["duration_pattern"] = DURATION_PATTERN
    required = " AND ".join(MAPPINGS[f][0] + " IS NOT NULL" for f in request.coverage.required_fields) or "TRUE"
    predicate = _condition(obj["where"], params)
    keyset = _continuation(order, continuation["keys"] if continuation else None, params)
    ordering = ",".join(_value(f, sort=True) + " " + d.upper() + " NULLS LAST" for f, d in order)
    key_values = ",".join("(" + _value(f, sort=True) + ")::text" for f, _ in order)
    # A validated enum selects one of two fixed shapes. Bind the scope directly
    # so PostgreSQL can estimate its cardinality; a CASE over a CTE hides it.
    # For a channel the inventory bound reaches the protected view directly.
    channel = request.scope.kind == "channel"
    scope_predicate = "c.channel_id=%(scope_id)s::text" if channel else "c.folder_id=%(scope_id)s::uuid"
    statement = _SOURCE.replace("{scope_predicate}", scope_predicate)
    statement = statement.replace("{inventory_scope}", " AND p.channel_id=%(scope_id)s::text" if channel else "")
    for key, value in {"title_key": title_key, "stored_title_key": stored_title_key,
                       "live_duration": duration_seconds_sql("duration_text.text", "duration_parts.parts"),
                       "required": required, "predicate": predicate,
                       "continuation": keyset, "order": ordering, "page_order": ordering,
                       "sort_keys": "jsonb_build_array("+key_values+")"}.items():
        statement = statement.replace("{"+key+"}", value)
    return statement, params
