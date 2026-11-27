"""Only registered field mappings may become SQL; all values are parameters.

No caller-provided SQL/identifiers, OFFSET, table names or ordering fragments.
This first current-page executor uses existing protected views, not raw tables.
"""
from functools import lru_cache
from . import research_growth
from .research_observations import prepared_sql, proof_now, requested as observations_requested
import unicodedata

from psycopg.types.json import Jsonb

from hot_storage import RETENTION_HOURS
from research_query.cell_layout import frozen_video_columns
from research_query.contracts import MAX_FROZEN_ROWS, MAX_ORDER_IDS
from research_query.frozen import FROZEN_TTL_SECONDS
from research_query import growth
from research_query.registry import FIELDS, UNICODE_VERSION, CHANNEL_SOURCE_ID
from research_query.validation import QueryRejected, ValidatedQuery
from db.inventory.automatic_type_policy import AUTOMATIC_TYPE_RULE_VERSION, AUTOMATIC_TYPE_MAX_SHORT_SECONDS


MAPPINGS = {
    **{f"observation.{name}": (f"observation_{name}", "numeric") for name in ("views_delta", "views_per_hour", "views_at_age")},
    # 28-day growth (db/reads/research_growth.py owns these prepared columns).
    "observation.growth_28d_status": ("observation_growth_28d_status", "text"),
    **{f"observation.{name}": (f"observation_{name}", "numeric") for name in (
        "views_gained_28d", "views_per_day_28d", "growth_28d_interval_seconds", "growth_28d_tracked_days")},
    "video.description": ("description", "text"), "video.paid_promotion": ("paid_promotion", "boolean"),
    "video.id": ("video_id", "text"), "video.channel_id": ("channel_id", "text"),
    "video.type": ("video_type", "text"), "video.title": ("title", "text"),
    "video.published_at": ("published_at", "timestamptz"),
    "video.duration_seconds": ("duration_seconds", "numeric"),
    "video.views": ("views", "numeric"),
    "video.likes": ("likes", "numeric"), "video.comments": ("comments", "numeric"),
    "video.tags": ("tags", "text[]"), "video.topic_urls": ("topic_urls", "text[]"),
    "video.category_id": ("category_id", "text"),
    "video.audio_language": ("audio_language", "text"),
    "video.metadata_language": ("metadata_language", "text"),
    "video.caption_available": ("caption_available", "boolean"),
    "video.scheduled_start_at": ("scheduled_start_at", "timestamptz"),
    "video.actual_end_at": ("actual_end_at", "timestamptz"),
    "video.thumbnail_url": ("thumbnail_url", "text"),
    "video.title_length": ("title_length", "numeric"), "video.tag_count": ("tag_count", "numeric"),
    # Exact text value; filters compare its numerator and denominator instead.
    "video.like_rate": ("like_rate", "rate"), "video.comment_rate": ("comment_rate", "rate"),
    "video.published_date": ("published_date", "date"), "video.published_year": ("published_year", "numeric"),
    "video.lifetime_views_per_day": ("lifetime_views_per_day", "numeric"),
    "channel.id": ("channel_id", "text"),
    "channel.title": ("channel_title", "text"),
    "channel.avatar_url": ("channel_avatar_url", "text"),
    "channel.subscribers": ("channel_subscribers", "numeric"),
    "channel.views": ("channel_views", "numeric"),
    "channel.video_count": ("channel_video_count", "numeric"),
    "channel.country_code": ("channel_country_code", "text"),
    "channel.created_at": ("channel_created_at", "timestamptz"),
}

# Protected payload of readable.videos, read only when a query references it.
# These fields do not belong in the cached type/duration/title projection;
# readable.video_search_projection appends them from its own readable.videos
# row, masked there, in the same statement snapshot. A read keeps them for the
# video's own channel only; a one-channel read looks them up by key
# (_extra_fields).
_EXTRA_COLUMNS = {
    "video.likes": "like_count", "video.comments": "comment_count",
    "video.tags": "tags", "video.topic_urls": "youtube_topic_categories",
    "video.category_id": "category_id", "video.audio_language": "default_audio_language",
    "video.metadata_language": "default_language", "video.caption_available": "caption_available",
    "video.scheduled_start_at": "scheduled_start_at", "video.actual_end_at": "stream_actual_end_at",
}


_OPTIONAL = ('video.description', 'video.paid_promotion')


def frozen(plan):
    """A frozen read admits only sources that stay current until its end.

    args.fresh_until = evaluation + FROZEN_TTL_SECONDS. Every freshness lower
    bound and proof expiry is compared with it instead of the evaluation time,
    so a value or row that would expire inside the frozen result is withheld
    (state expired, or the row counted as expiring), never shown and never a
    reason to end the result early. Future clocks still use evaluation time.
    A current read keeps exactly its own SQL text.
    """
    return plan.request.consistency == 'frozen'


def _horizon(plan, column=False):
    """The freshness horizon: as an args column (a.) or a carried column."""
    if frozen(plan):
        return 'fresh_until' if column else 'a.fresh_until'
    return 'evaluated_at' if column else 'a.evaluated_at'


def _filtering_fields(request):
    """Fields that decide which rows match or their order."""
    fields = {item.field for item in request.order_by} | set(request.coverage.required_fields)
    stack = [request.model_dump(mode='json', by_alias=True)['where']]
    while stack:
        node = stack.pop()
        if not node:
            continue
        if 'field' in node:
            fields.add(node['field'])
        else:
            stack.extend(node.get('all', node.get('any', [node.get('not')])))
    return fields


def _optional_fields(plan, population=False):
    fields=sorted(set(plan.referenced_fields)&set(_OPTIONAL))
    # A field that is only displayed cannot change which rows match or their
    # order, so it is read for the returned page only, like the type evidence.
    # Reading it for the whole scope cost ~1.6 s at 250,000 videos.
    page_only=[] if population else [f for f in fields if f not in _filtering_fields(plan.request)]
    scope=[f for f in fields if f not in page_only]
    result=dict(optional_raw='',optional_join='',optional_normalized='',optional_deadlines='',
                optional_page_columns='',optional_page_join='',optional_page_deadline='')
    raw=[];normalized=[];deadlines=[];selected=[]
    fresh=_horizon(plan,column=True)
    for field in scope:
        col=field.removeprefix('video.');clock=col+'_checked_at'
        # The protected view masks values by the current time only; a frozen
        # read also masks one that expires before the frozen result does.
        current=(f"CASE WHEN isfinite({clock}) AND {clock}>fresh_until-%(window)s::interval"
                 f" AND {clock}<=evaluated_at THEN ") if frozen(plan) else None
        selected.extend(['o.'+col,'o.'+clock])
        raw.extend([',optional.'+col,',optional.'+clock])
        normalized.extend([','+(current+col+' END AS '+col if current else col),','+clock,
            f",CASE WHEN isfinite({clock}) AND {clock}<={fresh}-%(window)s::interval THEN 'expired'"
            f" WHEN {clock}>evaluated_at OR NOT isfinite({clock}) THEN 'unavailable'"
            f" WHEN {col} IS NULL THEN 'unknown' ELSE 'known' END AS {col}_state"])
        deadlines.extend([','+future_deadline(f'({clock})'),','+future_deadline(f'({clock}+%(window)s::interval)')])
        if col=='description':
            selected.append('o.description_folded')
            raw.append(',optional.description_folded')
            normalized.append(','+(current+'description_folded END' if current else 'description_folded')+' AS description_key')
    if scope:
        # One protected-view row per population row. A plain join let the planner
        # misestimate the view and compare every row with every description.
        result.update(optional_raw=''.join(raw),optional_normalized=''.join(normalized),optional_deadlines=''.join(deadlines),
            optional_join="LEFT JOIN LATERAL (SELECT "+",".join(selected)+" FROM readable.video_optional_metadata o"
            " WHERE o.video_id=p.video_id AND o.video_type=%(video_type)s::text LIMIT 1) optional ON true")
    columns=[];selected=[];bounds=[]
    page_fresh='page_args.'+fresh
    for field in page_only:
        col=field.removeprefix('video.');clock='page_optional.'+col+'_checked_at';value='page_optional.'+col
        selected.extend(['o.'+col,'o.'+col+'_checked_at'])
        shown=(f"CASE WHEN isfinite({clock}) AND {clock}>page_args.fresh_until-%(window)s::interval"
               f" AND {clock}<=page_args.evaluated_at THEN {value} END AS {col}") if frozen(plan) else value
        columns.extend([','+shown,','+clock,
            f",CASE WHEN isfinite({clock}) AND {clock}<={page_fresh}-%(window)s::interval THEN 'expired'"
            f" WHEN {clock}>page_args.evaluated_at OR NOT isfinite({clock}) THEN 'unavailable'"
            f" WHEN {value} IS NULL THEN 'unknown' ELSE 'known' END AS {col}_state"])
        bounds.extend([f'({clock})',f'({clock}+%(window)s::interval)'])
    if page_only:
        # The page's own clocks bound its expiry; other rows cannot change it.
        columns.append(",(SELECT min(boundary) FROM (VALUES"+",".join(bounds)+") deadlines(boundary)"
                       " WHERE isfinite(boundary) AND boundary>page_args.evaluated_at) AS optional_expiry")
        result.update(optional_page_columns=''.join(columns),optional_page_deadline=',(SELECT min(optional_expiry) FROM details)',
            optional_page_join=" CROSS JOIN args page_args LEFT JOIN LATERAL (SELECT "+",".join(selected)+
            " FROM readable.video_optional_metadata o WHERE o.video_id=page.video_id"
            " AND o.video_type=%(video_type)s::text LIMIT 1) page_optional ON true")
    return result


def _extra_valid(field_id):
    """Column, clock, freshness flag and validity of one stored payload field."""
    field = FIELDS[field_id]
    column, kind = MAPPINGS[field_id]
    clock, fresh = ("checked_at", "counters_fresh") if field.clock_family == "counters" else ("metadata_checked_at", "metadata_fresh")
    valid = "TRUE"
    if kind == "numeric":
        valid = f"{column}>=0"
    elif kind == "text":
        valid = f"char_length({column})<={field.max_text_length}"
    elif kind == "text[]":
        # Guard array_position on malformed multidimensional input;
        # NULL is unknown, but an empty, collected array is known.
        valid = (f"CASE WHEN coalesce(array_ndims({column}),1)=1 THEN cardinality({column})<={field.max_items}"
                 f" AND array_position({column},NULL) IS NULL"
                 f" AND octet_length(array_to_string({column},''))<={field.max_total_bytes} ELSE false END")
    elif kind == "timestamptz":
        valid = _valid_instant(column)
    return column, clock, fresh, valid


def _state(clock, fresh, missing, valid, horizon):
    """A value's state; expired against the freshness horizon (_horizon), future
    clocks against the evaluation itself."""
    return (f"CASE WHEN isfinite({clock}) AND {clock}<={horizon}-%(window)s::interval THEN 'expired'"
            f" WHEN {clock}>evaluated_at THEN 'unavailable'"
            f" WHEN NOT {fresh} OR {missing} THEN 'unknown'"
            f" WHEN NOT ({valid}) THEN 'unavailable' ELSE 'known' END")


# Calculated fields read stored payload of the same row in the same snapshot;
# an input is fetched without being exposed when only the calculation needs it.
_DERIVED_INPUTS = {"video.tag_count": "video.tags", "video.like_rate": "video.likes", "video.comment_rate": "video.comments"}
RATE_FIELDS = ("video.like_rate", "video.comment_rate")
LOCAL_PUBLICATION_FIELDS = ("video.published_date", "video.published_year")


def half_even_text(numerator, denominator):
    """numerator/denominator (nonnegative numerics, denominator > 0) rounded
    half to even at six places, as canonical decimal text. Integer arithmetic
    only: a numeric division keeps too few digits for large counters."""
    scaled = f"({numerator})*1000000"
    quotient = f"div({scaled},{denominator})"
    twice_rest = f"2*({scaled}-{quotient}*{denominator})"
    rounded = (f"({quotient}+CASE WHEN {twice_rest}>{denominator} OR ({twice_rest}={denominator}"
               f" AND mod({quotient},2)=1) THEN 1 ELSE 0 END)")
    text = f"div({rounded},1000000)::text||'.'||lpad(mod({rounded},1000000)::text,6,'0')"
    return f"rtrim(rtrim({text},'0'),'.')"


def _derived_columns(referenced, horizon):
    """Calculated columns of the normalized row: the Library's static values
    and the local publication date and year.
    They read the row's own freshness flags, so a frozen read hides them too."""
    columns = []
    if "video.title_length" in referenced:
        # Unicode code points, like Python len(); the stored title is at most 1000.
        valid = "char_length(title)<=1000"
        columns += [f",CASE WHEN metadata_fresh AND {valid} THEN char_length(title)::numeric END AS title_length",
                    "," + _state("metadata_checked_at", "metadata_fresh", "title IS NULL", valid, horizon) + " AS title_length_state"]
    if "video.tag_count" in referenced:
        column, clock, fresh, valid = _extra_valid("video.tags")
        columns += [f",CASE WHEN {fresh} AND ({valid}) THEN cardinality({column})::numeric END AS tag_count",
                    "," + _state(clock, fresh, f"{column} IS NULL", valid, horizon) + " AS tag_count_state"]
    for field_id in RATE_FIELDS:
        if field_id in referenced:
            name, counter = MAPPINGS[field_id][0], MAPPINGS[_DERIVED_INPUTS[field_id]][0]
            numerator, denominator = f"{counter}::numeric*100", "views::numeric"
            # Zero views give no rate; a rate beyond the exact wire range (2**64-1) is
            # unavailable rather than capped, for its value and its filters alike.
            valid = f"{counter}>=0 AND views>0 AND {numerator}<=18446744073709551615*{denominator}"
            known = f"counters_fresh AND {valid}"
            # Filters compare numerator and denominator exactly; the value is shown.
            columns += [f",CASE WHEN {known} THEN {numerator} END AS {name}_numerator",
                        f",CASE WHEN {known} THEN {denominator} END AS {name}_denominator",
                        f",CASE WHEN {known} THEN {half_even_text(numerator, denominator)} END AS {name}",
                        "," + _state("checked_at", "counters_fresh", f"{counter} IS NULL OR views IS NULL",
                                     valid, horizon) + f" AS {name}_state"]
    if referenced & set(LOCAL_PUBLICATION_FIELDS):
        local, instant, in_range = local_publication("published_at", "%(query_time_zone)s::text")
        for field_id, value in (("video.published_date", f"{local}::date"),
                                ("video.published_year", f"extract(year FROM {local})")):
            if field_id in referenced:
                name = MAPPINGS[field_id][0]
                columns += [f",CASE WHEN metadata_fresh AND {instant} AND {in_range} THEN {value} END AS {name}",
                            "," + _state("metadata_checked_at", "metadata_fresh",
                                         f"published_at IS NULL OR NOT ({instant})", in_range, horizon) + f" AS {name}_state"]
    return "\n ".join(columns)


def _extra_fields(plan):
    referenced = set(plan.referenced_fields)
    stream_observations = observations_requested(plan) and plan.request.video_types == ['stream']
    unused_end = ',NULL::timestamptz AS actual_end_at' if observations_requested(plan) and not stream_observations and 'video.actual_end_at' not in plan.referenced_fields else ''
    exposed = (referenced & _EXTRA_COLUMNS.keys()) | ({"video.actual_end_at"} if stream_observations else set())
    fields = sorted(exposed | {_DERIVED_INPUTS[f] for f in referenced if f in _DERIVED_INPUTS})
    horizon = _horizon(plan, column=True)
    derived = _derived_columns(referenced, horizon)
    if not fields:
        return {"extra_raw": "", "extra_join": "", "extra_normalized": derived + unused_end}
    # One channel: its rows reach readable.videos by key, a few thousand probes, while the
    # view's own join masks the payload of every catalogue video before it meets the
    # channel's rows (the projection view's join is not parameterized). Several channels:
    # the view's join, not one probe per row of the scope.
    lookup = plan.request.scope.kind == "channel"
    selected, raw, normalized = [], [], []
    for field_id in fields:
        column, clock, fresh, valid = _extra_valid(field_id)
        if lookup:
            selected.append(f"v.{_EXTRA_COLUMNS[field_id]} AS {column}")
            raw.append(f",extra.{column}")
        else:
            # p.v_channel_id is the channel of the same readable.videos row.
            raw.append(f",CASE WHEN p.v_channel_id=p.channel_id THEN p.{_EXTRA_COLUMNS[field_id]} END AS {column}")
        if field_id in exposed:
            normalized.append(f",CASE WHEN {fresh} AND ({valid}) THEN {column} END AS {column}")
            normalized.append("," + _state(clock, fresh, f"{column} IS NULL", valid, horizon) + f" AS {column}_state")
    join = ("LEFT JOIN LATERAL (SELECT " + ",".join(selected) + " FROM readable.videos v WHERE v.video_id=p.video_id"
            " AND v.channel_id=p.channel_id LIMIT 1) extra ON true") if lookup else ""
    return {"extra_raw": "".join(raw), "extra_join": join,
            "extra_normalized": "\n ".join(normalized + ([derived] if derived else [])) + unused_end}


def _valid_instant(column):
    return f"isfinite({column}) AND {column}>='0001-01-01Z'::timestamptz AND {column}<'10000-01-01Z'::timestamptz"


def local_publication(instant, zone):
    """The canonical local publication rule of video.published_date and
    video.published_year, for every reader of local publication times: the wall
    time of `instant` in `zone`, the instant's validity and the local year
    1..9999. Returns (local wall time, valid instant, local year in range) as SQL."""
    local = f"({instant} AT TIME ZONE {zone})"
    return local, _valid_instant(instant), f"extract(year FROM {local}) BETWEEN 1 AND 9999"


def stream_end_order_sql(end, counters, metadata, now):
    """A Stream counts as completed only when its actual end precedes the
    evaluation time and both real clocks: the catalogue, the shared
    population and the channel header totals use this one rule."""
    return f"{end} IS NOT NULL AND {end}<={now}\n AND {end}<={counters} AND {end}<={metadata}"


def stream_completion_sql(end, counters, metadata, now, until, window):
    """The end order plus counter and metadata clocks current until `until`
    (the evaluation, or a frozen horizon) and never later than `now`."""
    return (stream_end_order_sql(end, counters, metadata, now) +
            f"\n AND {counters}>{until}-{window} AND {metadata}>{until}-{window}"
            f"\n AND {counters}<={now} AND {metadata}<={now}")



def _channel_fields(plan):
    """Normalize related profiles once per scoped channel, with their own clocks.

    No arbitrary joins: this fixed relation is one channel per admitted video.
    Only referenced families affect page lifetime, including filter-only fields.
    """
    fields = sorted(f for f in plan.referenced_fields if f.startswith('channel.') and f!='channel.id')
    primary = plan.request.source.id==CHANNEL_SOURCE_ID
    if not fields and not primary:
        return dict(channel_cte='', channel_join='', channel_raw='', channel_normalized='', channel_deadlines='',
                    channel_deadline_terms='')
    columns = {'channel.title':'title', 'channel.avatar_url':'avatar_url', 'channel.subscribers':'subscribers',
               'channel.views':'views', 'channel.video_count':'video_count', 'channel.country_code':'country_code',
               'channel.created_at':'channel_created_at'}
    families = {FIELDS[f].clock_family for f in fields} | ({'channel_profile'} if primary else set())
    clocks = {'channel_profile':'channel_metadata_checked_at', 'channel_country':'country_collected_at'}
    horizon = _horizon(plan)
    selected, raw, normalized, deadlines = [], [], [], []
    for family in sorted(families):
        clock = clocks[family]
        selected.append(f'CASE WHEN {_valid_instant("c."+clock)} THEN c.{clock} END AS {clock}')
        raw.append(f',cp.{clock}')
        normalized.append(f',{clock}')
        deadlines += [f'({clock})', f'({clock}+%(window)s::interval)']
    if primary:
        selected.append(f"coalesce(c.channel_metadata_status='ok' AND isfinite(c.channel_metadata_checked_at) AND c.channel_metadata_checked_at>{horizon}-%(window)s::interval AND c.channel_metadata_checked_at<=a.evaluated_at,false) AS profile_complete")
        raw.append(',cp.profile_complete');normalized.append(',profile_complete')
    for field_id in fields:
        field = FIELDS[field_id]
        column, kind = MAPPINGS[field_id]
        value, clock = 'c.' + columns[field_id], 'c.' + clocks[field.clock_family]
        fresh = f'isfinite({clock}) AND {clock}>{horizon}-%(window)s::interval AND {clock}<=a.evaluated_at'
        if field.clock_family == 'channel_profile':
            fresh += " AND c.channel_metadata_status='ok'"
        valid = f'{value}>=0' if kind == 'numeric' else _valid_instant(value) if kind == 'timestamptz' else f'char_length({value})<={field.max_text_length}'
        if field_id == 'channel.avatar_url':
            valid += f" AND {value} ~ '^https://[A-Za-z0-9.-]+(:[0-9]+)?/[^[:space:]]*$'"
        selected.append(f'CASE WHEN {fresh} AND ({valid}) THEN {value} END AS {column}')
        selected.append(f"CASE WHEN isfinite({clock}) AND {clock}<={horizon}-%(window)s::interval THEN 'expired'"
                        f" WHEN {clock}>a.evaluated_at THEN 'unavailable' WHEN NOT coalesce({fresh},false) OR {value} IS NULL THEN 'unknown'"
                        f" WHEN NOT ({valid}) THEN 'unavailable' ELSE 'known' END AS {column}_state")
        for name in (column, column+'_state'):
            raw.append(f',cp.{name}')
            normalized.append(f',{name}')
    if 'channel.title' in fields:
        expression = casefold_sql('channel_title') if _needs_text_key(plan.request, 'channel.title') else 'NULL::text'
        normalized.append(',(' + expression + ') AS channel_title_key')
    return dict(channel_cte='channel_payload AS MATERIALIZED (SELECT c.channel_id,' + ','.join(selected) +
                ' FROM scope JOIN readable.channels c USING(channel_id) CROSS JOIN args a),',
                channel_join='LEFT JOIN channel_payload cp ON cp.channel_id=p.channel_id', channel_raw=''.join(raw),
                channel_normalized=''.join(normalized), channel_deadlines=','+','.join(deadlines),
                # The same clocks for a video scope's raw_summary (research_channels lists them as VALUES).
                channel_deadline_terms=''.join(','+future_deadline(d) for d in deadlines))


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


def _needs_text_key(request, field):
    def contains(node):
        if not isinstance(node, dict):
            return False
        if node.get('field') == field and node.get('op') == 'contains_casefold_v1':
            return True
        return any(contains(child) for key in ('all', 'any') for child in node.get(key, [])) or contains(node.get('not'))
    return any(item.field == field for item in request.order_by) or contains(request.model_dump(mode='json', by_alias=True)['where'])


def _value(field, *, sort=False):
    column, kind = MAPPINGS[field]
    if sort and FIELDS[field].value_type == "text":
        # An empty text orders with the missing values, last in both directions,
        # as Library ordered titles (the old channel_explorer._sort_rows).
        return "nullif(" + column + "_key,'') COLLATE \"C\""
    return column + (' COLLATE "C"' if kind in {"text", "text[]"} else "")


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
    if op == "within_last_hours_v1":
        return window_condition(column, node["value"], lambda value, kind: _bind(params, value, kind))
    if op == "contains_casefold_v1":
        return "strpos(" + column + "_key COLLATE \"C\", " + _bind(params, node["value"].casefold(), "text") + ") > 0"
    if op == "in":
        return value + " = ANY(" + _bind(params, node["value"], kind + "[]") + ")"
    if op in {"has_any", "has_all"}:
        return value + (" && " if op == "has_any" else " @> ") + _bind(params, node["value"], "text[]")
    symbol = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    if kind == "rate":
        # counter*100/views <op> bound, exactly: views > 0 whenever it is known.
        return (column + "_numerator " + symbol + " " + _bind(params, node["value"], "numeric")
                + "*" + column + "_denominator")
    return value + " " + symbol + " " + _bind(params, node["value"], kind)


def sort_definition(request):
    values = [(v.field, v.direction) for v in request.order_by]
    identity='channel.id' if request.source.id==CHANNEL_SOURCE_ID else 'video.id'
    if not any(field == identity for field, _ in values):
        values.append((identity, "asc"))
    return values


def _after_keys(order, bounds, *, nullable_keys=False):
    """Lexicographic suffix for the query's ASC/DESC keys, always NULLS LAST."""
    terms, prefix = [], []
    for (field, direction), (bound, can_advance) in zip(order, bounds):
        col = _value(field, sort=True)
        if can_advance:
            comparator = ">" if direction == "asc" else "<"
            after = f"({col} {comparator} {bound} OR {col} IS NULL)"
            if nullable_keys:
                after = f"({bound} IS NOT NULL AND {after})"
            terms.append("(" + " AND ".join(prefix + [after]) + ")")
        prefix.append(f"{col} IS NOT DISTINCT FROM {bound}")
    return "(" + " OR ".join(terms) + ")" if terms else "FALSE"


def _continuation(order, keys, params):
    if keys is None:
        return "TRUE"
    if len(keys) != len(order):
        raise QueryRejected("cursor_context_changed")
    return _after_keys(order, [(_bind(params, key, MAPPINGS[field][1]), key is not None)
                               for (field, _), key in zip(order, keys)])


def _scope_ctes(kind):
    """One definition for page and final recheck; no API payload reads here.

    Membership digests include durable owner bindings, including unavailable
    collection references, and an owner a read binds to an unbound member.
    Losing a member must not silently broaden a page.
    Only a validated discriminant selects these fixed SQL shapes.
    """
    prefix, membership_revision, identity_revision = "", "''", "''"
    exists = "EXISTS(SELECT 1 FROM scope)"
    if kind == "channel":
        predicate = "c.channel_id=%(scope_id)s::text"
    elif kind == "library":
        # Every visible channel; the library itself always exists (an empty
        # one is an empty result, not an unavailable source).
        predicate = "TRUE"
        exists = "TRUE"
    elif kind in {"folder", "label", "collection"}:
        source = {
            "folder": "SELECT extract(epoch FROM created_at)::text AS revision FROM public.folders WHERE id=%(scope_id)s::uuid",
            "label": "SELECT extract(epoch FROM created_at)::text AS revision FROM public.channel_labels WHERE label_key=%(scope_id)s::text",
            "collection": "SELECT extract(epoch FROM created_at)::text||':'||revision::text AS revision FROM public.saved_objects WHERE kind='collection' AND id=%(scope_id)s::uuid AND deleted_at IS NULL",
        }[kind]
        prefix = "scope_source AS MATERIALIZED (" + source + "),\n"
        exists = "EXISTS(SELECT 1 FROM scope_source)"
        identity_revision = "coalesce((SELECT revision FROM scope_source),'')"
        if kind == "folder":
            predicate = "c.folder_id=%(scope_id)s::uuid"
        elif kind == "label":
            prefix += """scope_members AS MATERIALIZED (
 SELECT channel_id,created_at FROM public.channel_label_assignments
 WHERE label_key=%(scope_id)s::text AND EXISTS(SELECT 1 FROM scope_source)),
"""
            predicate = "EXISTS(SELECT 1 FROM scope_members m WHERE m.channel_id=c.channel_id)"
            membership_revision = "coalesce((SELECT string_agg(channel_id||':'||extract(epoch FROM created_at)::text,',' ORDER BY channel_id COLLATE \"C\") FROM scope_members),'')"
        else:
            # A member saved before its video was in the inventory has no
            # owner (the insert trigger found none). Each read binds it as that
            # trigger would now: to the user channel whose inventory holds the
            # video. A restore-pending owner and a stored binding are kept.
            prefix += """scope_members AS MATERIALIZED (
 SELECT b.video_id,b.user_channel_ref,b.restore_owner_ref,b.added_at,c.is_hidden AS hidden_owner
 FROM (SELECT m.video_id,m.restore_owner_ref,m.added_at,coalesce(m.user_channel_ref,CASE WHEN m.restore_owner_ref IS NULL THEN
 (SELECT u.user_work_id FROM readable.video_inventory k JOIN readable.channel_user_work u ON u.channel_id=k.channel_id
 WHERE k.video_id=m.video_id LIMIT 1) END) AS user_channel_ref
 FROM public.saved_collection_members m
 WHERE m.collection_id=%(scope_id)s::uuid AND EXISTS(SELECT 1 FROM scope_source)) b
 LEFT JOIN readable.channel_user_work c ON c.user_work_id=b.user_channel_ref),
"""
            predicate = "EXISTS(SELECT 1 FROM scope_members m WHERE m.user_channel_ref=c.user_work_id)"
            membership_revision = """coalesce((SELECT string_agg(video_id||':'||coalesce(user_channel_ref::text,'')||':'||
 coalesce(restore_owner_ref::text,'')||':'||coalesce(hidden_owner::text,'unbound')||':'||extract(epoch FROM added_at)::text,',' ORDER BY video_id COLLATE "C") FROM scope_members),'')"""
    else:
        raise QueryRejected("invalid_query")
    return prefix + """scope AS MATERIALIZED (
 SELECT c.channel_id,c.user_work_id FROM readable.channel_user_work c
 WHERE NOT c.is_hidden AND """ + predicate + """),
scope_info AS MATERIALIZED (
 SELECT """ + exists + """ AS source_exists,
 (SELECT count(*) FROM scope) AS channels_in_scope,
 encode(sha256(convert_to(""" + identity_revision + "||'|'||" + membership_revision + """||'|'||
 coalesce((SELECT string_agg(channel_id||':'||user_work_id::text,',' ORDER BY channel_id COLLATE "C")
 FROM scope),''),'UTF8')),'hex') AS scope_revision)"""


def compile_scope_recheck(plan):
    if not isinstance(plan, ValidatedQuery):
        raise QueryRejected("invalid_query")
    return ("WITH " + _scope_ctes(plan.request.scope.kind) +
            " SELECT source_exists,scope_revision FROM scope_info", {"scope_id": plan.request.scope.id})


# Members absent from raw may be unavailable, unbound after restore, hidden or
# of another format. Only a fresh canonical proof of another format makes their
# exclusion complete. Its deadline also limits the lifetime of the whole page.
_COLLECTION_COVERAGE = """collection_coverage AS MATERIALIZED (
 SELECT count(*) FILTER (WHERE m.hidden_owner IS NOT TRUE AND (h.video_type IS NULL OR h.video_type=a.video_type
 OR h.source_expires_at<={proof_until})) AS unresolved_collection_members,
 count(*) FILTER (WHERE m.hidden_owner IS TRUE) AS hidden_collection_members,
 min(h.source_expires_at) FILTER (WHERE h.source_expires_at>a.evaluated_at) AS valid_before
 FROM scope_members m CROSS JOIN args a
 LEFT JOIN scope c ON c.user_work_id=m.user_channel_ref
 LEFT JOIN LATERAL (SELECT video_type,source_expires_at FROM readable.hot_video_inventory h0
 WHERE h0.video_id=m.video_id AND h0.channel_id=c.channel_id LIMIT 1) h ON true
 WHERE NOT EXISTS(SELECT 1 FROM raw r WHERE r.video_id=m.video_id)),"""


# Admission of public.videos.current.v1 rows, one named stage per reason, in
# the order the collection member read reports the first stage that fails. A
# row is admitted only when every stage holds; the order never changes that.
# Aliases: p projection and video row, h identity proof, i canonical type, a args.
ADMISSION_STAGES = (
    ("identity", "h.video_id IS NOT NULL AND h.source_expires_at>{until}"),
    ("source_missing", "p.v_video_id IS NOT NULL"),
    ("source_mismatch", "p.v_channel_id=p.channel_id"),
    ("source_stale", "p.availability_checked_at<=a.evaluated_at"
                     " AND p.availability_checked_at>{until}-%(window)s::interval"),
    ("source_unavailable", "p.availability_status='public'"),
    ("source_unpublished", "p.publication_status IS NULL AND (p.published_at IS NULL OR p.published_at<=a.evaluated_at)"),
    ("stream_incomplete", "(i.video_type<>'stream' OR (" + stream_completion_sql(
        'p.stream_actual_end_at', 'p.checked_at', 'p.metadata_checked_at', 'a.evaluated_at', '{until}',
        '%(window)s::interval') + "))"),
)
# Row admission as of a horizon {until}: every core source must still be
# current then (a.evaluated_at for current reads, a.fresh_until for frozen
# results). Clocks may never be later than the evaluation itself.
_ADMISSION = "coalesce(" + "\n AND ".join(sql for _, sql in ADMISSION_STAGES) + ",false)"
ADMITTED = _ADMISSION.replace("{until}", "a.evaluated_at")
# The stages as of now, for the collection member read.
_STAGES_NOW = tuple((reason, sql.replace("{until}", "a.evaluated_at")) for reason, sql in ADMISSION_STAGES)
# Why a scanned row is not admitted now: the first stage that fails, except that
# a row a current availability check found not public is always
# source_unavailable (one reason, whatever else lapsed with it, such as
# its playlist identity proof), and a waiting publication is named as such (its
# identity proof is withheld too). Only operation populations carry it, as
# raw.rejection (coverage evidence).
REJECTION_REASONS = tuple(reason for reason, _ in ADMISSION_STAGES)
_REJECTION = (",CASE WHEN p.availability_status IS DISTINCT FROM 'public' AND coalesce("
              + dict(_STAGES_NOW)["source_stale"] + ",false) THEN 'source_unavailable'"
              " WHEN p.publication_status IS NOT NULL OR p.published_at>a.evaluated_at THEN 'source_unpublished'"
              + "".join(" WHEN NOT coalesce(" + sql + ",false) THEN '" + reason + "'" for reason, sql in _STAGES_NOW)
              + " END AS rejection")

# Canonical type (i) and fresh identity proof (h) of a projection row p: the
# read model's proof while it is current, otherwise the protected views live.
_TYPE_JOIN = """LEFT JOIN LATERAL (SELECT x.video_type FROM readable.video_inventory x
 WHERE NOT p.proof_current AND x.video_id=p.video_id LIMIT 1) live ON true
 CROSS JOIN LATERAL (SELECT CASE WHEN p.proof_current THEN p.inventory_type ELSE live.video_type END AS video_type) i"""
_IDENTITY_JOIN = """LEFT JOIN LATERAL ((SELECT p.video_id AS video_id,p.identity_clock AS source_checked_at,p.identity_expiry AS source_expires_at
 WHERE p.proof_current AND p.hot_present)
 UNION ALL (SELECT h0.video_id,h0.source_checked_at,h0.source_expires_at FROM readable.hot_video_inventory h0
 WHERE NOT p.proof_current AND h0.video_id=p.video_id AND h0.channel_id=p.channel_id AND h0.video_type=i.video_type LIMIT 1)) h ON true"""
# The same type (i), identity proof (h) and stored duration for a catalogue scope,
# with ONE lateral for the rows read live and nothing per current row:
# _TYPE_JOIN, the duration parse and _IDENTITY_JOIN cost four nested laterals for
# every row, current or not. A current row takes the read model's values; a live row
# its inventory type, its parsed duration and, only when that type is the one queried
# (no other row is kept), its identity proof. LIMIT 1 on m keeps one regexp per row.
_LIVE_JOIN = """LEFT JOIN LATERAL (SELECT x.video_type,d.seconds,hl.video_id AS h_video_id,hl.source_checked_at AS h_checked_at,
 hl.source_expires_at AS h_expires_at FROM readable.video_inventory x
 CROSS JOIN LATERAL (SELECT upper(btrim(p.duration)) AS text) t
 CROSS JOIN LATERAL (SELECT regexp_match(t.text,%(duration_pattern)s) AS parts LIMIT 1) m
 CROSS JOIN LATERAL (SELECT """ + duration_seconds_sql("t.text", "m.parts") + """ AS seconds) d
 LEFT JOIN LATERAL (SELECT h0.video_id,h0.source_checked_at,h0.source_expires_at FROM readable.hot_video_inventory h0
 WHERE x.video_type=%(video_type)s::text AND h0.video_id=p.video_id AND h0.channel_id=p.channel_id AND h0.video_type=x.video_type LIMIT 1) hl ON true
 WHERE NOT p.proof_current AND x.video_id=p.video_id LIMIT 1) live ON true
 CROSS JOIN LATERAL (SELECT CASE WHEN p.proof_current THEN p.inventory_type ELSE live.video_type END AS video_type) i
 CROSS JOIN LATERAL (SELECT CASE WHEN p.proof_current THEN CASE WHEN p.hot_present THEN p.video_id END ELSE live.h_video_id END AS video_id,
 CASE WHEN p.proof_current THEN CASE WHEN p.hot_present THEN p.identity_clock END ELSE live.h_checked_at END AS source_checked_at,
 CASE WHEN p.proof_current THEN CASE WHEN p.hot_present THEN p.identity_expiry END ELSE live.h_expires_at END AS source_expires_at) h"""



# Rows come from readable.video_search_projection: the protected views' type
# proof as of its last refresh, used only while proof_current, plus the exact
# stored duration seconds and casefold title key, masked by readable.videos.
# A row that is queued after a write or past a source clock is read from the
# protected views instead, which stay the sole owners of type policy. Their
# keys are leakproof and reach indexes; video_inventory is keyed by video_id,
# so each yields at most one row, and LIMIT 1 only prevents flattening. The
# source lifetime is one bound parameter, the HOT window in hours.
# coverage.data_valid_before is the earliest deadline of the scope's own data
# clocks; a current page also ends 120 seconds after evaluation, while a whole
# population summary may live as long as its inputs.
_SOURCE = """WITH args AS MATERIALIZED (
 SELECT statement_timestamp() AS evaluated_at, %(scope_id)s::text AS scope_id,
 %(scope_kind)s::text AS scope_kind, %(video_type)s::text AS video_type{args_fresh}),
{scope_ctes},
{channel_cte}
inventory_state AS MATERIALIZED (
 SELECT scope.channel_id,CASE WHEN state.inventory_status='playlist_not_found'
 AND state.source_checked_at<=a.evaluated_at AND {inventory_until} THEN true
 WHEN state.inventory_status IN('completed','running')
 AND state.source_checked_at<=a.evaluated_at AND {inventory_until}
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
 p.video_expiry,p.availability_status,p.publication_status,p.stream_actual_end_at{extra_raw}{channel_raw}{optional_raw},
 {stored_title_key} AS stored_title_key,
 CASE WHEN p.proof_current THEN p.duration_seconds ELSE live.seconds END AS stored_duration_seconds,
 a.evaluated_at{raw_rejection},{raw_fresh}
 coalesce(isfinite(p.metadata_checked_at) AND p.metadata_checked_at>{fresh_a}-%(window)s::interval
 AND p.metadata_checked_at<=a.evaluated_at,false) AS metadata_fresh,
 coalesce(isfinite(p.checked_at) AND p.checked_at>{fresh_a}-%(window)s::interval
 AND p.checked_at<=a.evaluated_at,false) AS counters_fresh,
 {admission}
 {inventory_from}
 {extra_join}
 {channel_join}
 {optional_join}
 {live_join}
 WHERE i.video_type=%(video_type)s::text{inventory_scope} AND p.video_id ~ '^[A-Za-z0-9_-]{11}$'),
normalized AS NOT MATERIALIZED (
 SELECT {title_key} AS title_key,video_id,channel_id,video_type,identity_clock,identity_expiry,metadata_checked_at,checked_at,
 least(identity_expiry,availability_checked_at+%(window)s::interval) AS core_expiry,
 CASE WHEN metadata_fresh AND char_length(title)<=1000 THEN title END AS title,
 CASE WHEN metadata_fresh AND isfinite(published_at) AND published_at>='0001-01-01Z'::timestamptz
 AND published_at<'10000-01-01Z'::timestamptz THEN published_at END AS published_at,
 CASE WHEN metadata_fresh AND parsed.seconds BETWEEN 0 AND 18446744073709551615 THEN parsed.seconds END AS duration_seconds,
 CASE WHEN counters_fresh AND views>=0 THEN views::numeric END AS views,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<={fresh}-%(window)s::interval THEN 'expired'
 WHEN metadata_checked_at>evaluated_at OR (title IS NOT NULL AND char_length(title)>1000) THEN 'unavailable'
 WHEN NOT metadata_fresh OR title IS NULL THEN 'unknown' ELSE 'known' END AS title_state,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<={fresh}-%(window)s::interval THEN 'expired'
 WHEN metadata_checked_at>evaluated_at THEN 'unavailable' WHEN NOT metadata_fresh OR published_at IS NULL OR NOT isfinite(published_at)
 OR published_at<'0001-01-01Z'::timestamptz OR published_at>='10000-01-01Z'::timestamptz THEN 'unknown' ELSE 'known' END AS published_at_state,
 CASE WHEN isfinite(metadata_checked_at) AND metadata_checked_at<={fresh}-%(window)s::interval THEN 'expired'
 WHEN metadata_checked_at>evaluated_at THEN 'unavailable' WHEN NOT metadata_fresh OR parsed.seconds IS NULL
 OR parsed.seconds NOT BETWEEN 0 AND 18446744073709551615 THEN 'unknown' ELSE 'known' END AS duration_seconds_state,
 CASE WHEN isfinite(checked_at) AND checked_at<={fresh}-%(window)s::interval THEN 'expired'
 WHEN checked_at>evaluated_at THEN 'unavailable' WHEN NOT counters_fresh OR views IS NULL OR views<0 THEN 'unknown' ELSE 'known' END AS views_state
 {extra_normalized}
 {channel_normalized}
 {optional_normalized}
 {thumbnail_normalized}{pace_normalized}
 FROM raw CROSS JOIN LATERAL (SELECT CASE WHEN raw.metadata_fresh THEN raw.stored_duration_seconds END AS seconds) parsed
 WHERE admitted),
{prepared_sql},
{collection_coverage}
raw_summary AS MATERIALIZED (
 SELECT {raw_counts},
 min(least({raw_deadlines}{channel_deadline_terms}{optional_deadlines})) AS data_deadline FROM raw),
coverage AS MATERIALIZED (
 SELECT c.*,least((SELECT evaluated_at+interval '120 seconds' FROM args),c.data_valid_before) AS valid_before FROM (
 SELECT (SELECT count(*) FROM inventory_state WHERE complete) AS channels_complete,
 {rejected},
 (SELECT count(*) FROM prepared WHERE NOT ({required})) AS required_missing,
 {unresolved_members} AS unresolved_collection_members,
 {hidden_members} AS hidden_collection_members,
 least({collection_deadline},{observation_deadline},
 (SELECT min(source_expires_at) FROM inventory_state WHERE source_expires_at>(SELECT evaluated_at FROM args)),
 (SELECT data_deadline FROM raw_summary)) AS data_valid_before OFFSET 0) c),
page AS MATERIALIZED (
 {anchor_cte}SELECT prepared.*,{sort_keys} AS sort_keys FROM prepared{anchor_join}
 WHERE ({required}) AND ({predicate}) AND ({continuation}) ORDER BY {order} LIMIT %(limit)s),
details AS ({details}){final}"""

# Coverage reads c.data_valid_before twice. OFFSET 0 keeps the subquery c from being
# pulled up, so its deadline aggregate over every row runs once;
# the same snapshot gives the same value either way.
#
# raw_summary reads every scanned row once for both the rejected count and the
# earliest data deadline: per row, the earliest of its clocks that is
# finite and later than the evaluation, then the earliest over the rows. That is the
# minimum over all (row, clock) pairs the earlier VALUES lateral listed, five clocks
# per row; NULLs drop out of least() and min() alike.
def future_deadline(clock):
    """A data clock that can still end a result: finite and later than raw.evaluated_at."""
    return f"CASE WHEN isfinite({clock}) AND {clock}>evaluated_at THEN {clock} END"


RAW_DEADLINES = ",".join(future_deadline(clock) for clock in (
    "identity_expiry", "video_expiry", "(checked_at+%(window)s::interval)", "(metadata_checked_at+%(window)s::interval)",
    "(availability_checked_at+%(window)s::interval)", "checked_at", "metadata_checked_at", "availability_checked_at",
    "published_at"))
_PAGE_FINAL = """
SELECT scope_info.*,coverage.channels_complete,coverage.rejected_rows,coverage.required_missing,
 coverage.unresolved_collection_members,coverage.hidden_collection_members,least(coverage.valid_before,(SELECT min(type_expiry) FROM details){optional_page_deadline}) AS valid_before,a.evaluated_at{order_columns},
 coalesce((SELECT jsonb_agg({json_row} ORDER BY {page_order}) FROM details),'[]'::jsonb) AS rows
FROM scope_info CROSS JOIN coverage CROSS JOIN args a{order_join}"""
# page.order = "ids": every row the page CTE could return (the same required
# fields and predicate, no position), in the page CTE's own ORDER BY: one
# snapshot, one order, so the page is a slice of this list by construction. Past
# MAX_ORDER_IDS only the count is returned; the list is withheld, never cut.
_ORDER_LIST = """ CROSS JOIN (SELECT count(*) AS order_total,CASE WHEN count(*)<=%(order_limit)s
 THEN coalesce(string_agg(video_id,'' ORDER BY {order}),'') END AS order_ids
 FROM prepared WHERE ({required}) AND ({predicate})) matched_order"""
_SOURCE = _SOURCE.replace("{live_join}", _LIVE_JOIN)


# Why a saved member is not shown, in the order the shared collection scope and
# admission decide it: its owner binding, its row under that owner, then the
# admission stages. Shown members also need current counters and metadata.
COLLECTION_MEMBER_REASONS = tuple(dict.fromkeys(("channel_hidden", "channel_unavailable", "inventory_missing", "source_mismatch",
                                                  "source_stale", "type_conflict", "type_unverified",
                                                  *(reason for reason, _ in ADMISSION_STAGES[1:]))))
_MEMBER_REASON = ("CASE WHEN NOT member.saved THEN 'not_saved'"
    " WHEN member.hidden_owner IS TRUE THEN 'channel_hidden'"
    " WHEN member.owner_channel IS NULL THEN CASE WHEN member.user_channel_ref IS NULL AND member.restore_owner_ref IS NULL"
    " THEN 'inventory_missing' ELSE 'channel_unavailable' END"
    "\n WHEN p.video_id IS NULL THEN CASE WHEN EXISTS(SELECT 1 FROM readable.video_inventory k WHERE k.video_id=member.video_id)"
    " THEN 'source_mismatch' ELSE 'inventory_missing' END"
    # Identity proof is withheld while a video is scheduled or live, when its
    # evidence expired, and while its type is in conflict or unconfirmed.
    "\n WHEN NOT coalesce(" + _STAGES_NOW[0][1] + ",false) THEN CASE WHEN p.publication_status IS NOT NULL"
    " THEN 'source_unpublished' WHEN h.video_id IS NOT NULL OR i.video_type IS NULL"
    " OR resolution.source_expires_at IS NULL THEN 'source_stale' WHEN resolution.resolution_status='conflict'"
    " THEN 'type_conflict' ELSE 'type_unverified' END"
    + "".join(f"\n WHEN NOT coalesce({sql},false) THEN '{reason}'" for reason, sql in _STAGES_NOW[1:]) +
    "\n WHEN NOT coalesce(isfinite(p.checked_at) AND p.checked_at>a.evaluated_at-%(window)s::interval AND p.checked_at<=a.evaluated_at"
    " AND isfinite(p.metadata_checked_at) AND p.metadata_checked_at>a.evaluated_at-%(window)s::interval"
    " AND p.metadata_checked_at<=a.evaluated_at,false) THEN 'source_stale' END")
_COLLECTION_MEMBERS = """WITH args AS MATERIALIZED (
 SELECT least(statement_timestamp(),%(as_of)s::timestamptz) AS evaluated_at),
{scope_ctes},
requested AS MATERIALIZED (
 SELECT r.item AS video_id,r.position FROM unnest(%(video_ids)s::text[]) WITH ORDINALITY AS r(item,position)),
member AS MATERIALIZED (
 SELECT r.video_id,r.position,m.video_id IS NOT NULL AS saved,m.hidden_owner,m.user_channel_ref,m.restore_owner_ref,
 s.channel_id AS owner_channel
 FROM requested r LEFT JOIN scope_members m ON m.video_id=r.video_id
 LEFT JOIN scope s ON s.user_work_id=m.user_channel_ref),
judged AS MATERIALIZED (
 SELECT member.video_id,member.position,p.channel_id,i.video_type,p.title,p.published_at,p.duration,p.views,p.checked_at,
 p.metadata_checked_at,p.availability_checked_at,h.source_checked_at AS membership_checked_at,
 least(h.source_expires_at,p.video_expiry,p.checked_at+%(window)s::interval,p.metadata_checked_at+%(window)s::interval,
 p.availability_checked_at+%(window)s::interval) AS source_expires_at,
 {reason} AS reason
 FROM member CROSS JOIN args a
 LEFT JOIN LATERAL (SELECT p0.* FROM readable.video_search_projection p0
 WHERE p0.video_id=member.video_id AND p0.channel_id=member.owner_channel LIMIT 1) p ON true
 {type_join}
 {identity_join}
 LEFT JOIN LATERAL (SELECT r.resolution_status,r.source_expires_at FROM readable.video_inventory_type_resolution r
 WHERE r.video_id=p.video_id LIMIT 1) resolution ON true)
SELECT judged.video_id,CASE WHEN judged.reason IS NULL THEN judged.channel_id END AS channel_id,
 CASE WHEN judged.reason IS NULL THEN 'available' ELSE 'unavailable' END AS source_status,judged.reason AS source_reason,
 CASE WHEN judged.reason IS NULL THEN jsonb_build_object('video_id',judged.video_id,'video_type',judged.video_type,
 'title',judged.title,'thumbnail_url','https://img.youtube.com/vi/'||judged.video_id||'/hqdefault.jpg',
 'published_at',judged.published_at,'checked_at',judged.checked_at,'duration',judged.duration,
 'metadata_checked_at',judged.metadata_checked_at,'availability_checked_at',judged.availability_checked_at,
 'membership_checked_at',judged.membership_checked_at,'source_expires_at',judged.source_expires_at,'views',judged.views,
 'like_count',extra.like_count,'comment_count',extra.comment_count,'category_id',extra.category_id) END AS video_row,
 CASE WHEN judged.reason IS NULL THEN judged.source_expires_at END AS source_expires_at
FROM judged LEFT JOIN LATERAL (SELECT v.like_count,v.comment_count,v.category_id FROM readable.videos v
 WHERE judged.reason IS NULL AND v.video_id=judged.video_id AND v.channel_id=judged.channel_id LIMIT 1) extra ON true
ORDER BY judged.position"""


def compile_collection_members(collection_id, video_ids, as_of):
    """One page of saved member IDs of every type through the shared
    collection scope: each gets its type and values or the first reason."""
    statement = (_COLLECTION_MEMBERS.replace("{scope_ctes}", _scope_ctes("collection"))
                 .replace("{reason}", _MEMBER_REASON).replace("{type_join}", _TYPE_JOIN)
                 .replace("{identity_join}", _IDENTITY_JOIN))
    return statement, {"scope_id": collection_id, "video_ids": list(video_ids), "as_of": as_of,
                       "window": f"{RETENTION_HOURS} hours"}

# The whole matching population in final order, or no rows at all past the
# frozen row limit: a refused result is never truncated. The frozen lifetime
# is not read from SQL; every admitted source outlives args.fresh_until.
_FROZEN_FINAL = """
SELECT scope_info.*,coverage.channels_complete,coverage.rejected_rows,coverage.expiring_rows,coverage.required_missing,
 coverage.unresolved_collection_members,coverage.hidden_collection_members,a.evaluated_at,a.fresh_until,
 true AS packed_video_rows,(SELECT count(*) FROM page) AS frozen_count,
 coalesce((SELECT array_agg(channel_id ORDER BY channel_id COLLATE "C") FROM scope),ARRAY[]::text[]) AS scope_channels,
 CASE WHEN (SELECT count(*) FROM page)<=%(frozen_rows)s THEN
 coalesce((SELECT json_agg({json_row} ORDER BY {page_order}) FROM details),'[]'::json)::text END AS rows
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""



def frozen_parameters():
    # One more row than the limit tells an over-limit result from a full one.
    return {"horizon": f"{FROZEN_TTL_SECONDS} seconds", "frozen_rows": MAX_FROZEN_ROWS,
            "limit": MAX_FROZEN_ROWS + 1}


def frozen_terms(plan):
    """Template terms that differ between a current page and a frozen read."""
    if not frozen(plan):
        return {"args_fresh": "", "raw_fresh": "", "fresh_a": "a.evaluated_at", "fresh": "evaluated_at",
                "inventory_until": "state.source_expires_at>a.evaluated_at",
                "admission": _ADMISSION.replace("{until}", "a.evaluated_at") + " AS admitted",
                "raw_counts": "count(*) FILTER (WHERE NOT admitted) AS rejected_rows",
                "rejected": "(SELECT rejected_rows FROM raw_summary) AS rejected_rows",
                "final": _PAGE_FINAL}
    # A row admissible now that expires inside the frozen result is counted
    # apart from rows that are not admissible at all (both are withheld).
    return {"args_fresh": ",statement_timestamp()+%(horizon)s::interval AS fresh_until",
            "raw_fresh": "a.fresh_until,", "fresh_a": "a.fresh_until", "fresh": "fresh_until",
            "inventory_until": "least(state.source_expires_at,completed.source_expires_at,full_scan.source_expires_at)>a.fresh_until",
            "admission": _ADMISSION.replace("{until}", "a.fresh_until") + " AS admitted,\n "
                         + _ADMISSION.replace("{until}", "a.evaluated_at") + " AS admitted_now",
            "raw_counts": "count(*) FILTER (WHERE NOT admitted_now) AS rejected_rows,"
                          "count(*) FILTER (WHERE admitted_now AND NOT admitted) AS expiring_rows",
            "rejected": "(SELECT rejected_rows FROM raw_summary) AS rejected_rows,\n"
                        " (SELECT expiring_rows FROM raw_summary) AS expiring_rows",
            "final": _FROZEN_FINAL}


def page_parts(plan,continuation,params):
    """Shared typed predicates and keyset ordering for each registered source."""
    request=plan.request;obj=request.model_dump(mode='json',by_alias=True)
    order=sort_definition(request)
    required = " AND ".join(MAPPINGS[f][0] + " IS NOT NULL" for f in request.coverage.required_fields) or "TRUE"
    predicate = _condition(obj["where"], params)
    keyset = _continuation(order, continuation["keys"] if continuation else None, params)
    ordering = ",".join(_value(f, sort=True) + " " + d.upper() + " NULLS LAST" for f, d in order)
    key_values = ",".join("(" + _value(f, sort=True) + ")::text" for f, _ in order)
    return required,predicate,keyset,ordering,key_values


def compile_query(plan, continuation=None, *, population=False):
    if not isinstance(plan, ValidatedQuery):
        raise QueryRejected("invalid_query")
    request = plan.request
    if frozen(plan) and (population or continuation is not None):
        # A frozen read is one complete statement. Operations and keyset pages
        # read current data; they never mix in a frozen horizon.
        raise QueryRejected("invalid_query")
    if request.source.id==CHANNEL_SOURCE_ID:
        from .research_channels import compile_channels
        return compile_channels(plan,continuation,population=population)
    params = {"scope_id": request.scope.id, "scope_kind": request.scope.kind,
              "video_type": request.video_types[0], "limit": request.page.size + 1,
              # Exact hours: an interval of days follows the session time zone
              # across daylight-saving changes, so 29 days is not always 696 hours.
              "window": f"{RETENTION_HOURS} hours"}
    if frozen(plan):
        params.update(frozen_parameters())
    obj = request.model_dump(mode="json", by_alias=True)
    order = sort_definition(request)
    need_fold = _needs_text_key(request, 'video.title')
    # public.casefold_v1 stores the key when a title is written; it is exposed
    # only with the same gating as the title itself.
    title_key = ("CASE WHEN metadata_fresh AND char_length(title)<=1000 THEN stored_title_key END"
                 if need_fold else "NULL::text")
    stored_title_key = "NULL::text"
    if need_fold:
        # Literals fold in Python: casefold_map refuses another Unicode version.
        params.update(casefold_parameters())
        stored_title_key = "CASE WHEN p.proof_current THEN p.title_key ELSE " + casefold_sql("p.title") + " END"
    if _needs_text_key(request, 'channel.title'):
        params.update(casefold_parameters())
    if set(plan.referenced_fields) & set(LOCAL_PUBLICATION_FIELDS):
        # Local dates and years are counted in the request's validated zone.
        params["query_time_zone"] = request.time.timezone
    params["duration_pattern"] = DURATION_PATTERN
    required,predicate,keyset,ordering,key_values = page_parts(plan,continuation,params)
    # A validated enum selects fixed shapes. Bind the scope directly
    # so PostgreSQL can estimate its cardinality; a CASE over a CTE hides it.
    # For a channel the inventory bound reaches the protected view directly.
    channel = request.scope.kind == "channel"
    collection = request.scope.kind == "collection"
    inventory_scope = " AND p.channel_id=%(scope_id)s::text" if channel else ""
    # A current row's type is its stored type (i.video_type): a current row of another
    # type can never match, so it is dropped before the per-row work rather than after.
    # The condition is implied by the type match itself; the same rows match.
    inventory_scope += " AND (p.proof_current IS NOT TRUE OR p.inventory_type=%(video_type)s::text)"
    # A relative publication window every match satisfies also drops raw rows
    # early (see window_pushdown); on a current page of several channels it also
    # chooses the rows read (_WINDOW_FROM).
    window = window_pushdown(request, params)
    inventory_scope += window
    inventory_from = "FROM scope JOIN readable.video_search_projection p USING(channel_id) CROSS JOIN args a"
    if channel and (frozen(plan) or not population):
        # Rows with no masked video for this channel still contribute to
        # coverage. Fetch those exceptions by ID with the original left join.
        inventory_from = """FROM scope CROSS JOIN args a JOIN LATERAL (
 SELECT matched.* FROM readable.video_search_projection matched
 WHERE matched.channel_id=scope.channel_id AND matched.v_channel_id=scope.channel_id
 UNION ALL
 SELECT other.* FROM readable.video_search_projection_types seed
 CROSS JOIN LATERAL (SELECT v.* FROM readable.video_search_projection v
 WHERE v.video_id=seed.video_id AND v.channel_id=seed.channel_id LIMIT 1) other
 WHERE seed.channel_id=scope.channel_id AND NOT EXISTS (
   SELECT 1 FROM readable.videos vid WHERE vid.channel_id=scope.channel_id AND vid.video_id=seed.video_id)
 ) p ON true"""
    if window and not population and not frozen(plan) and request.scope.kind in WINDOW_SCOPES:
        inventory_from = _WINDOW_FROM
        inventory_scope += _WINDOW_SCOPE
    if collection:
        # A collection owns exact saved IDs. Start with those indexed identities
        # rather than scanning its channels and repeatedly probing a materialized
        # member list. Durable owner and protected projection checks still apply.
        inventory_from = """FROM scope_members chosen
 JOIN scope ON scope.user_work_id=chosen.user_channel_ref CROSS JOIN args a
 JOIN LATERAL (SELECT p0.* FROM readable.video_search_projection p0
 WHERE p0.video_id=chosen.video_id AND p0.channel_id=scope.channel_id LIMIT 1) p ON true"""
    anchor_cte, anchor_join = "", ""
    if not population and not frozen(plan) and request.page.anchor_id:
        # Only a member of this exact, current filtered population can position
        # the page. A missing anchor leaves the first page and its cursor intact.
        params["page_anchor_id"] = request.page.anchor_id
        columns = ",".join(f"{_value(field, sort=True)} AS k{i}" for i, (field, _) in enumerate(order))
        anchor_cte = ("WITH page_anchor AS MATERIALIZED (SELECT true AS found," + columns + " FROM prepared "
                      "WHERE video_id=%(page_anchor_id)s AND (" + required + ") AND (" + predicate + ") LIMIT 1) ")
        anchor_join = " LEFT JOIN page_anchor ON true"
        suffix = _after_keys(order, [(f"page_anchor.k{i}", True) for i in range(len(order))], nullable_keys=True)
        keyset = "page_anchor.found IS NULL OR video_id=%(page_anchor_id)s OR " + suffix
    page_order = ordering
    if not population and not frozen(plan) and request.page.ids:
        # A slice of an ordered list: exactly these rows, if they still match,
        # in the list's order. Everything else of the statement is unchanged.
        params["page_ids"] = list(request.page.ids)
        keyset = "video_id=ANY(%(page_ids)s::text[])"
        page_order = "array_position(%(page_ids)s::text[],video_id)"
    statement = _SOURCE.replace("{anchor_cte}", anchor_cte).replace("{anchor_join}", anchor_join)
    for key, value in frozen_terms(plan).items():
        statement = statement.replace("{"+key+"}", value)
    order_list = not population and not frozen(plan) and request.page.order == "ids"
    if order_list:
        params["order_limit"] = MAX_ORDER_IDS
    statement = statement.replace("{order_columns}", ",matched_order.order_total,matched_order.order_ids" if order_list else "")
    statement = statement.replace("{order_join}", _ORDER_LIST if order_list else "")
    # An operation population also says why each scanned row was not admitted.
    statement = statement.replace("{raw_rejection}", _REJECTION if population else "")
    statement = statement.replace("{scope_ctes}", _scope_ctes(request.scope.kind))
    statement = statement.replace("{inventory_from}", inventory_from)
    statement = statement.replace("{inventory_scope}", inventory_scope)
    statement = statement.replace("{collection_coverage}", _COLLECTION_COVERAGE.replace("{proof_until}", _horizon(plan)) if collection else "")
    statement = statement.replace("{hidden_members}", "(SELECT hidden_collection_members FROM collection_coverage)" if collection else "0")
    statement = statement.replace("{unresolved_members}", "(SELECT unresolved_collection_members FROM collection_coverage)" if collection else "0")
    statement = statement.replace("{collection_deadline}", "(SELECT valid_before FROM collection_coverage)" if collection else "NULL::timestamptz")
    details = 'SELECT page.*,NULL::timestamptz AS type_expiry FROM page'
    if 'video.type' in request.select:
        # Evidence is enriched only for the bounded output page, never the full
        # scope. The protected resolver remains the type-policy owner.
        details = """SELECT page.*,proof.source_expires_at AS type_expiry,
 CASE WHEN proof.manual_valid THEN 'manual_decision_v1' WHEN proof.automatic_valid THEN 'automatic_long_short_v1'
 WHEN proof.present_membership_types=ARRAY[page.video_type] AND proof.present_membership_channels=ARRAY[page.channel_id]
 THEN 'typed_playlist_membership_v1' ELSE 'unknown' END AS type_method,
 CASE WHEN proof.automatic_valid AND NOT proof.manual_valid THEN %(type_threshold)s::integer END AS type_threshold,
 CASE WHEN proof.automatic_valid AND NOT proof.manual_valid THEN %(type_rule_version)s::integer END AS type_rule_version
 FROM page LEFT JOIN LATERAL (SELECT r.* FROM readable.video_inventory_type_resolution r
 WHERE r.video_id=page.video_id AND r.canonical_channel_id=page.channel_id AND r.canonical_video_type=page.video_type
 AND r.resolution_status='resolved' AND r.source_expires_at>""" + proof_now(plan) + """ LIMIT 1) proof ON true"""
        params.update(type_threshold=AUTOMATIC_TYPE_MAX_SHORT_SECONDS, type_rule_version=AUTOMATIC_TYPE_RULE_VERSION)
    thumbnail = (",'https://img.youtube.com/vi/'||video_id||'/hqdefault.jpg' AS thumbnail_url,'known'::text AS thumbnail_url_state"
                 if 'video.thumbnail_url' in plan.referenced_fields else '')
    optional = _optional_fields(plan, population)
    details = details.replace('SELECT page.*', 'SELECT page.*' + optional.pop('optional_page_columns'), 1) + optional.pop('optional_page_join')
    # Projection-only growth cannot affect membership or cursor order. The
    # page's growth evidence can shorten its lifetime when a snapshot has
    # older engagement. Growth used by a predicate, sort or required field
    # keeps the population-wide lifetime. Aggregates retain their complete input.
    observation_fields = {f for f in plan.referenced_fields if f.startswith("observation.")}
    channel_growth = (channel and not population and not frozen(plan) and observation_fields
                      and observation_fields <= set(growth.FIELDS))
    deciding_growth = observation_fields & _filtering_fields(request) if channel_growth else set()
    if channel_growth and not deciding_growth and request.page.growth_lifetime == "page":
        params.update(research_growth.parameters(plan))
        details = ("SELECT n.*," + ",".join(research_growth.FIELDS)
                   + " FROM (" + details + ") n " + research_growth.joins(plan))
        statement = statement.replace("{optional_page_deadline}",
            ",(SELECT min(observation_growth_28d_expires_at) FROM details){optional_page_deadline}")
        prepared = "prepared AS NOT MATERIALIZED (SELECT normalized.* FROM normalized)"
        observation_deadline = "NULL::timestamptz"
    elif deciding_growth:
        # Sorting or filtering by growth still visits the whole channel. Build
        # the detailed evidence JSON and remaining metrics for the output page.
        names = {MAPPINGS[field][0] + suffix for field in deciding_growth for suffix in ("", "_state")}
        population_columns, remaining = research_growth.split_columns(names)
        prepared, observation_deadline = prepared_sql(plan, params, growth_columns=population_columns)
        details = ("SELECT n.*," + ",".join(remaining)
                   + " FROM (" + details + ") n " + research_growth.joins(plan))
    else:
        prepared, observation_deadline = prepared_sql(plan, params)
    # Time alone moves rows across a relative publication window.
    observation_deadline += window_deadline(request, params)
    # JSON numeric fractions otherwise become binary floats before the wire
    # serializer sees them. Keep exact SQL decimal arithmetic as decimal text.
    json_row = "to_jsonb(details)"
    for fraction in ('views_per_hour', 'views_per_day_28d'):
        if 'observation.' + fraction in plan.referenced_fields:
            json_row += f" || jsonb_build_object('observation_{fraction}',details.observation_{fraction}::text)"
    json_row += pace_json(plan)
    if frozen(plan):
        expressions = []
        for name in frozen_video_columns(request):
            value = "details." + name
            if name in {"observation_views_per_hour", "observation_views_per_day_28d"}:
                value += "::text"
            elif name == "lifetime_views_per_day":
                value = _pace_text()
            expressions.append(value)
        # A bounded JSON array per row. Wide selections may exceed the SQL
        # function argument limit, so join only those rare rows' arrays.
        parts = ["json_build_array(" + ",".join(expressions[i:i + 90]) + ")"
                 for i in range(0, len(expressions), 90)]
        json_row = parts[0] if len(parts) == 1 else " || ".join(part + "::jsonb" for part in parts)
    statement = statement.replace("{pace_normalized}", pace_columns(plan, _horizon(plan, column=True)))
    for key, value in {"json_row":json_row, "prepared_sql": prepared, "observation_deadline": observation_deadline, **_extra_fields(plan), **_channel_fields(plan), **optional, "details":details, "thumbnail_normalized":thumbnail,
                       "title_key": title_key, "stored_title_key": stored_title_key, "raw_deadlines": RAW_DEADLINES,
                       "required": required, "predicate": predicate,
                       "continuation": keyset, "order": page_order, "page_order": page_order,
                       # A frozen result is addressed by position, not by keys.
                       "sort_keys": "NULL::jsonb" if frozen(plan) else "jsonb_build_array("+key_values+")"}.items():
        statement = statement.replace("{"+key+"}", value)
    if population:
        statement = statement.partition("\npage AS MATERIALIZED (")[0].rstrip().removesuffix(",")
        statement += " ,population AS MATERIALIZED (SELECT * FROM prepared WHERE ("+required+") AND ("+predicate+"))"
    return statement, params


# --- Home "Latest videos" parts ---------------------------------
# Shared query parts first needed by Home "Latest videos".
#
# - within_last_hours_v1: a publication window that ends at the server's
#   evaluation time. Each read evaluates its own
#   window.
# - The window is also added to the raw rows' condition when it holds for every
#   matching row (an `all` path from the root). A row it drops could never
#   match: the normalized row shows that same masked value or nothing.
# - On a current page of several channels (WINDOW_SCOPES) that window also
# chooses the rows read: readable.video_publication_times (schema
#   48) is exactly the publication times readable.videos shows, as rows, so
#   the window reaches idx_videos_published_at_desc and only the videos
#   published inside it are looked up in the read model. Every row the raw
#   condition keeps shows such a time, so the same rows are read. A channel
#   keeps its channel index; operations and frozen reads keep the full scope,
#   whose much smaller row estimate would change their own joins' plans.
# - Time alone ends a windowed result when its oldest row leaves the window;
#   that instant bounds the lifetime (coverage valid_before), like other clocks.
# - video.lifetime_views_per_day: views per day with the Overview's age (from
#   publication to the counter observation) and the Home feed's one-hour minimum age.
#
# Fixed SQL text only; values are bound parameters. No ';' anywhere.

PACE_FIELD = "video.lifetime_views_per_day"
WINDOW_OPERATOR = "within_last_hours_v1"
# One hour, in microseconds: the Home feed's minimum age.
MINIMUM_AGE_MICROSECONDS = 3_600_000_000


def _predicates(node, *, conjunctive_only):
    """within_last_hours_v1 hour values in a validated where tree."""
    if not isinstance(node, dict):
        return []
    if "field" in node:
        return [node["value"]] if node.get("op") == WINDOW_OPERATOR and node.get("field") == "video.published_at" else []
    if "all" in node:
        return [value for child in node["all"] for value in _predicates(child, conjunctive_only=conjunctive_only)]
    if conjunctive_only:
        return []
    children = node.get("any") or [node.get("not")]
    return [value for child in children for value in _predicates(child, conjunctive_only=False)]


def window_condition(column, hours, bind):
    """The predicate itself: `column` at or after evaluation minus `hours`."""
    return column + " >= (SELECT evaluated_at FROM args)-" + bind(hours + " hours", "interval")


WINDOW_SCOPES = frozenset({"library", "folder", "label"})
# The rows of a windowed current page: videos whose shown publication time is in
# the window, by index, each looked up in the read model by its key (the LIMIT 1
# keeps that lookup an index probe). The scope is a set of channels checked per row:
# IS TRUE keeps the IN a hashed subplan rather than a join, which the planner,
# misled by the small row estimate, otherwise repeats over all scope channels for
# every video. scope has one row per channel, so the set is the same as the join.
_WINDOW_FROM = """FROM readable.video_publication_times w
 JOIN LATERAL (SELECT p0.* FROM readable.video_search_projection p0 WHERE p0.video_id=w.video_id LIMIT 1) p ON true
 CROSS JOIN args a"""
_WINDOW_SCOPE = (" AND w.published_at>=statement_timestamp()-%(window_scan)s::interval"
                 " AND (p.channel_id IN (SELECT channel_id FROM scope)) IS TRUE")


def window_pushdown(request, params):
    """An extra condition on the raw rows, or ''. Only a window every match
    must satisfy is added; the narrowest one is enough. It is a filter on the
    masked publication time; _WINDOW_FROM uses the same window as an index
    bound (statement_timestamp() is args.evaluated_at)."""
    hours = _predicates(request.model_dump(mode="json", by_alias=True)["where"], conjunctive_only=True)
    if not hours:
        return ""
    params["window_scan"] = str(min(int(value) for value in hours)) + " hours"
    return " AND p.published_at>=a.evaluated_at-%(window_scan)s::interval"


def window_deadline(request, params):
    """Deadline terms (each starting with ',') at which time alone moves a row
    across a window of the where tree: the earliest admitted row inside it
    leaves it. Strictly later than the evaluation."""
    hours = sorted(set(_predicates(request.model_dump(mode="json", by_alias=True)["where"], conjunctive_only=False)), key=int)
    terms = []
    for index, value in enumerate(hours):
        key = "window_edge" + str(index)
        params[key] = value + " hours"
        terms.append(",(SELECT min(published_at)+%(" + key + ")s::interval FROM prepared"
                     " WHERE published_at>(SELECT evaluated_at FROM args)-%(" + key + ")s::interval)")
    return "".join(terms)


def _half_even(numerator, denominator):
    """numerator/denominator (nonnegative whole numerics, denominator > 0)
    rounded half to even at six places, as numeric of scale 6. Whole-number
    arithmetic only: a numeric division keeps too few digits for large counters."""
    scaled = f"({numerator})*1000000"
    quotient = f"div({scaled},{denominator})"
    twice_rest = f"2*({scaled}-{quotient}*{denominator})"
    rounded = (f"({quotient}+CASE WHEN {twice_rest}>{denominator} OR ({twice_rest}={denominator}"
               f" AND mod({quotient},2)=1) THEN 1 ELSE 0 END)")
    return f"round({rounded}/1000000.0,6)"


def pace_columns(plan, horizon):
    """Normalized columns of video.lifetime_views_per_day when referenced.

    Known only while both the counter and the metadata are fresh (to the
    horizon of the read, like every other value), the views are known and the
    counter was observed at or after publication. Expired when either clock
    has expired, unavailable for future clocks or a counter observed before
    publication, unknown otherwise.
    """
    if PACE_FIELD not in plan.referenced_fields:
        return ""
    instant = ("isfinite(published_at) AND published_at>='0001-01-01Z'::timestamptz"
               " AND published_at<'10000-01-01Z'::timestamptz")
    known = f"counters_fresh AND metadata_fresh AND views>=0 AND {instant} AND checked_at>=published_at"
    age = ("greatest((extract(epoch FROM checked_at)-extract(epoch FROM published_at))*1000000,"
           + str(MINIMUM_AGE_MICROSECONDS) + ")")
    # views * 86400 s per day * 10**6 microseconds per second / age in microseconds.
    value = _half_even("views::numeric*86400000000", f"({age})")
    state = (f"CASE WHEN (isfinite(checked_at) AND checked_at<={horizon}-%(window)s::interval)"
             f" OR (isfinite(metadata_checked_at) AND metadata_checked_at<={horizon}-%(window)s::interval) THEN 'expired'"
             " WHEN checked_at>evaluated_at OR metadata_checked_at>evaluated_at THEN 'unavailable'"
             f" WHEN NOT counters_fresh OR NOT metadata_fresh OR views IS NULL OR views<0 OR published_at IS NULL OR NOT ({instant}) THEN 'unknown'"
             " WHEN checked_at<published_at THEN 'unavailable' ELSE 'known' END")
    return (f"\n ,CASE WHEN {known} THEN {value} END AS lifetime_views_per_day"
            f"\n ,{state} AS lifetime_views_per_day_state")


def _pace_text():
    return "rtrim(rtrim(details.lifetime_views_per_day::text,'0'),'.')"


def pace_json(plan):
    """The pace as canonical decimal text in a result row (never a JSON float)."""
    if PACE_FIELD not in plan.referenced_fields:
        return ""
    return " || jsonb_build_object('lifetime_views_per_day'," + _pace_text() + ")"
