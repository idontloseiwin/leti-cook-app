"""Pure, versioned Saved query definitions; never persisted result data.

Channel scope is bound to the existing Saved object's channel, so portable
backups cannot silently bind a query to a different provider channel.
A v2 definition may carry one closed presentation block: where it opens and
how its rows are shown. Presentation never changes which rows match.
"""
from copy import deepcopy
import json
import re

from .contracts import QueryRequest, TIME_ZONE_PATTERN, time_zone
from .operations import OPERATIONS
from .validation import AccessContext, QueryRejected, canonical, validate

CHANNEL_BINDING = "$channel"
MAX_DEFINITION_BYTES = 8192
PRESENTATION_KEYS = frozenset({"version", "surface", "view", "density", "columns", "time_zone"})
# library: the channel's Library Videos tab with the familiar cards. Without a
# presentation block a v2 query opens in the research workshop.
SURFACES = frozenset({"library"})
VIEWS = frozenset({"grid", "table"})
DENSITIES = frozenset({"large", "compact"})


def normalize_presentation(value, query, *, allow_unknown=False):
    """Strict display block of a v2 query (the zone its dates were
    entered and are labelled in). Unknown keys or values are rejected."""
    if (type(value) is not dict or not {"version", "surface", "view"} <= set(value)
            or set(value) - PRESENTATION_KEYS or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("unsupported_saved_presentation")
    if value["surface"] not in SURFACES or value["view"] not in VIEWS:
        raise ValueError("invalid_saved_presentation")
    if value["surface"] == "library" and (query["scope"]["kind"] != "channel" or query["source"]["id"] != "public.videos.current.v1"):
        raise ValueError("library_presentation_requires_channel_videos")
    result = {"version": 1, "surface": value["surface"], "view": value["view"]}
    if "density" in value:
        if value["density"] not in DENSITIES:
            raise ValueError("invalid_saved_presentation")
        result["density"] = value["density"]
    if "columns" in value:
        columns = value["columns"]
        if (type(columns) is not list or not 1 <= len(columns) <= 50 or len(set(map(str, columns))) != len(columns)
                or any(type(column) is not str or column not in query["select"] for column in columns)):
            raise ValueError("invalid_saved_presentation")
        result["columns"] = list(columns)
    if "time_zone" in value:
        zone = value["time_zone"]
        if type(zone) is not str or len(zone) > 64 or re.fullmatch(TIME_ZONE_PATTERN, zone) is None:
            raise ValueError("invalid_saved_presentation")
        if not allow_unknown:
            time_zone(zone)  # Stored work keeps a zone this installation lacks.
        result["time_zone"] = zone
    return result


def normalize_definition(value, *, allow_unknown=False):
    if (type(value) is not dict or not {"version", "query"} <= set(value) or set(value) - {"version", "query", "presentation"}
            or type(value["version"]) is not int or value["version"] != 2):
        raise ValueError("unsupported_saved_definition")
    if len(canonical(value)) > MAX_DEFINITION_BYTES:
        raise ValueError("saved_definition_too_large")
    query = deepcopy(value["query"])
    if type(query) is not dict or type(query.get("scope")) is not dict:
        raise ValueError("invalid_saved_query")
    # The library scope (Home) is not saved: browsers before it parse every
    # saved scope with an id, and one unparsed object breaks the Saved list.
    if query["scope"].get("kind") == "library":
        raise ValueError("library_scope_not_saved")
    bound = query["scope"].get("kind") == "channel"
    if bound:
        if query["scope"].get("id") != CHANNEL_BINDING:
            raise ValueError("channel_binding_required")
        query["scope"]["id"] = "UC" + "0" * 22
    if query.get("page", {}).get("after") is not None:
        raise ValueError("saved_cursor_forbidden")
    # Shape validation is not execution permission. A disabled/gated query
    # remains readable user work; execute rechecks the real owner context.
    parsed = QueryRequest.model_validate(query)
    payload = parsed.model_dump(mode="json", by_alias=True, exclude_none=True)
    # A definition is saved, never the way it was read. The execution
    # mode and a frozen page anchor are dropped on write and on read, so work
    # stored before this rule opens as a current query (the page size stays).
    payload["consistency"] = "current"
    payload["page"] = {"size": payload["page"]["size"]}
    try:
        payload = json.loads(validate(canonical(payload), AccessContext("definition", "v1", enabled=True,
            allowed_operations=frozenset(OPERATIONS))).encoded)
    except QueryRejected as error:
        if not allow_unknown or error.code not in {"unsupported_field"}:
            raise ValueError("invalid_saved_query") from error
    payload["page"] = {"size": payload["page"]["size"]}  # The validated encoding spells out after: null.
    if bound:
        payload["scope"]["id"] = CHANNEL_BINDING
    result = {"version": 2, "query": payload}
    if "presentation" in value:
        result["presentation"] = normalize_presentation(value["presentation"], payload, allow_unknown=allow_unknown)
    if len(canonical(result)) > MAX_DEFINITION_BYTES:
        raise ValueError("saved_definition_too_large")
    return result


def from_query(query, presentation=None):
    query = deepcopy(query)
    source_channel = query.get("scope", {}).get("id") if query.get("scope", {}).get("kind") == "channel" else None
    if source_channel is not None:
        from saved_work import channel_id
        source_channel = channel_id(source_channel)
        query["scope"]["id"] = CHANNEL_BINDING
    definition = {"version": 2, "query": query, **({} if presentation is None else {"presentation": presentation})}
    return source_channel, normalize_definition(definition)


def bound_query(conditions, source_channel):
    query = deepcopy(normalize_definition(conditions, allow_unknown=True)["query"])
    if query["scope"]["kind"] == "channel":
        from saved_work import channel_id
        query["scope"]["id"] = channel_id(source_channel)
    return query


def translate_legacy(source_channel, conditions, *, time_zone="UTC"):
    """Saved v1 (Library) conditions -> one shared query; see library_v1."""
    from .library_v1 import translate
    return translate(conditions, channel_id=source_channel, time_zone=time_zone).payload()
