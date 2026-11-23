"""Versioned saved-work identity, errors and catalogue-condition validation.

Saved work is private single-operator application data, not an auth boundary or
a copy of YouTube observations. Interaction state never belongs in conditions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from hot_storage import utc_instant
from research_query.library_v1 import (JS_WHITESPACE, STATIC_BOUND_KEYS, STATIC_PRESETS, check_query,
                                       normalize_static_bounds)

CONTRACT_VERSION = 1
MAX_BODY_BYTES = 32768
FILTER_KEYS = (
    "q", "category_id", "published_from", "published_before", "min_views",
    "max_views", "min_duration_seconds", "max_duration_seconds", "sort_by", "sort_order",
)


class SavedWorkError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message


def invalid(message="Saved view request is invalid."):
    return SavedWorkError(422, "invalid_request", message)


def unavailable():
    return SavedWorkError(503, "saved_work_unavailable", "Saved views are unavailable. No save is confirmed.")


def exact_object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise invalid()
    return value


def saved_id(value):
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value.lower():
        raise invalid("A UUIDv4 saved view ID is required.")
    return str(parsed)


def saved_name(value):
    if not isinstance(value, str):
        raise invalid("Name must contain 1 to 120 characters.")
    name = value.strip(JS_WHITESPACE)
    if not 1 <= len(name) <= 120 or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in name):
        raise invalid("Name must contain 1 to 120 characters.")
    return name


def channel_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
        raise invalid("A valid channel ID is required.")
    return value


def revision(value):
    if type(value) is not int or not 1 <= value <= 9007199254740991:
        raise invalid("A positive expected revision is required.")
    return value


def saved_scopes(conditions):
    """The scopes a normalized Saved definition reads: one query's, or every source of a canvas."""
    from research_query.graph import saved_scopes
    return saved_scopes(conditions)


def normalize_conditions(value):
    # A canvas has no Library filters; an old v1-shaped object with version 4 keeps its old refusal below.
    if type(value) is dict and value.get("version")==4 and "filters" not in value:
        from research_query.graph import CANVAS_NOTES_BYTES, MAX_DEFINITION, DefinitionTooLarge, normalize_saved_canvas
        try:
            return normalize_saved_canvas(value)
        except DefinitionTooLarge as error:
            raise invalid(f"Notes are limited to {CANVAS_NOTES_BYTES:,} bytes (about {CANVAS_NOTES_BYTES // 2:,} Cyrillic letters)."
                          if error.path == "graph.notes" else f"A canvas is limited to {MAX_DEFINITION:,} bytes.") from error
        except (ValueError,TypeError,RecursionError) as error:
            raise invalid("Research definition is invalid.") from error
    if type(value) is dict and value.get("version")==3:
        try:
            from research_query.graph import normalize_saved_research
            return normalize_saved_research(value)
        except (ValueError,TypeError,RecursionError) as error:
            raise invalid("Research definition is invalid.") from error
    if type(value) is dict and value.get("version") == 2:
        try:
            from research_query.saved import normalize_definition
            return normalize_definition(value, allow_unknown=True)
        except (ValueError, TypeError, RecursionError) as error:
            raise invalid("Saved query definition is invalid.") from error
    try:
        exact_object(value, ("version", "filters", "facets"))
        if type(value["version"]) is not int or value["version"] != 1:
            raise invalid("Unsupported catalogue condition version.")
        raw = value["filters"]
        # Library's further filters belong to the one translator that gives them meaning.
        from research_query.library_v1 import MORE_FILTER_KEYS, normalize_more_filters
        if not isinstance(raw, dict) or set(FILTER_KEYS) - set(raw) or set(raw) - set(FILTER_KEYS) - set(STATIC_BOUND_KEYS) - set(MORE_FILTER_KEYS):
            raise invalid()
        if any(not isinstance(raw[key], str) for key in FILTER_KEYS):
            raise invalid()
        filters = {key: raw[key].strip(JS_WHITESPACE) if key not in {"sort_by", "sort_order"} else raw[key] for key in FILTER_KEYS}
        bounds = normalize_static_bounds({key: raw[key] for key in STATIC_BOUND_KEYS if key in raw})
        more = normalize_more_filters({key: raw[key] for key in MORE_FILTER_KEYS if key in raw})
        # TS validates the uncollapsed query with String.length (UTF-16 units).
        # A saved value must remain readable by that exact existing codec.
        if len(filters["q"].encode("utf-16-le")) // 2 > 100 or "\x00" in filters["q"]:
            raise invalid("Search is limited to 100 characters.")
        if filters["sort_order"] not in {"asc", "desc"}:
            raise invalid()
        if filters["category_id"] and not re.fullmatch(r"[0-9]{1,3}", filters["category_id"]):
            raise invalid()
        for key in ("category_id", "min_views", "max_views", "min_duration_seconds", "max_duration_seconds"):
            if filters[key]:
                if not re.fullmatch(r"[0-9]+", filters[key]) or len(filters[key]) > 32:
                    raise invalid()
                filters[key] = str(int(filters[key]))
        for key in ("published_from", "published_before"):
            if filters[key]:
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", filters[key]):
                    raise invalid()
                parsed = date.fromisoformat(filters[key])
                if key == "published_before":
                    # It is already the exclusive API end; never add a day.
                    parsed - timedelta(days=1)
        facets = value["facets"]
        # The TS codec accepts repeated preset keys up to 100, then deduplicates.
        if not isinstance(facets, dict):
            raise invalid()
        facets = dict(facets)
        # Bounds belong to Saved filters, not the transport-only facets member.
        if "bounds" in facets:
            raise invalid()
        for key in ("view_buckets", "duration_buckets", *STATIC_PRESETS):
            if key in facets:
                items = facets[key]
                if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, str) for item in items):
                    raise invalid()
                facets[key] = list(dict.fromkeys(items))
        # Shorts and Streams duration presets are kept beside the Library's own
        # (Long) validation and translated by the tab of their type.
        from research_query.library_v1 import ordered_duration, split_typed_duration
        typed_duration = []
        if "duration_buckets" in facets:
            facets["duration_buckets"], typed_duration = split_typed_duration(facets["duration_buckets"])
            if typed_duration and (filters["min_duration_seconds"] or filters["max_duration_seconds"]):
                raise invalid()
        # Validate stored v1 conditions without reading catalogue rows.
        transport = {**facets, **({"bounds": bounds} if bounds else {})}
        sort_by, canonical_facets = check_query(filters, transport)
        if filters["sort_by"] != sort_by:
            raise invalid()
        canonical_facets.pop("bounds", None)
        if typed_duration:
            canonical_facets["duration_buckets"] = ordered_duration([*canonical_facets["duration_buckets"], *typed_duration])
        # Omitting empty extensions preserves every old create fingerprint.
        filters.update(bounds)
        filters.update(more)
        cohort = canonical_facets["cohort"]
        if cohort:
            for key in ("published_from", "published_through"):
                if cohort[key] is not None:
                    cohort[key] = utc_instant(cohort[key]).isoformat(timespec="microseconds").replace("+00:00", "Z")
        result = {"version": 1, "filters": filters, "facets": canonical_facets}
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > 8192:
            raise invalid("Saved conditions exceed the supported size.")
        return result
    except SavedWorkError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise invalid("Saved catalogue conditions are invalid.") from error


def translate_library_conditions(body):
    """Library v1 conditions (a Saved view or an old ?videos= address), the
    viewer's time zone and the Library tab's video type -> one shared-backend
    query, or the named fields it still needs. Pure: no database, provider or
    Saved object is touched. A Saved v1 view belongs to the Long-form tab."""
    data = exact_object(body, ("channel_id", "conditions", "time_zone", "video_type"))
    if not isinstance(data["time_zone"], str) or len(data["time_zone"]) > 64:
        raise invalid("A valid IANA time zone is required.")
    if data["video_type"] not in ("long", "short", "stream"):
        raise invalid("A Library video type (long, short or stream) is required.")
    from research_query.library_v1 import translate
    try:
        result = translate(data["conditions"], channel_id=channel_id(data["channel_id"]), time_zone=data["time_zone"],
                           video_type=data["video_type"])
    except SavedWorkError:
        raise
    except ValueError as error:
        raise invalid("Only Library catalogue conditions with a valid time zone can be translated.") from error
    return {"contract_version": 1, "translation": result.payload()}


def create_fingerprint(name, source_channel_id, conditions):
    value = {"name": name, "channel_id": source_channel_id, "conditions": conditions}
    return fingerprint(value)


def encode_cursor(created_at, view_id):
    value = json.dumps([created_at, saved_id(view_id)], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_cursor(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise invalid()
        pair = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            raise invalid()
        parsed = utc_instant(pair[0])
        return parsed.isoformat(timespec="microseconds"), saved_id(pair[1])
    except (ValueError, TypeError, OverflowError) as error:
        raise invalid("Saved views cursor is invalid.") from error


def _validate_origin(headers, scheme, *, write):
    """Scoped browser-origin defense, not authentication.

    When Host lost a non-default tunnel port, browser same-origin Fetch Metadata
    is required. Caller-supplied X-Forwarded-Host never authorizes a request.
    """
    headers = {key.lower(): value for key, value in headers.items()}
    if write and headers.get("x-saved-work") != "1":
        raise SavedWorkError(403, "write_guard", "Saved view write header is required.")
    fetch_site = headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise SavedWorkError(403, "cross_origin", "Cross-origin Saved view requests are not allowed.")
    origin = headers.get("origin")
    if origin is None:
        if not write and fetch_site == "same-origin":
            return
        if any(key.startswith("sec-fetch-") for key in headers):
            raise SavedWorkError(403, "origin_unverifiable", "Saved view request origin cannot be verified.")
        return
    try:
        source = urlsplit(origin)
        target = urlsplit(f"{scheme}://{headers.get('host', '')}")
        if source.scheme not in {"http", "https"} or source.scheme != target.scheme or not source.hostname or source.hostname != target.hostname or source.username or source.password or source.path or source.query or source.fragment:
            raise ValueError()
        default_port = 443 if source.scheme == "https" else 80
        if target.port is not None:
            if (source.port or default_port) != target.port:
                raise ValueError()
        elif (source.port or default_port) != default_port and fetch_site != "same-origin":
            raise SavedWorkError(403, "origin_unverifiable", "Saved view request origin cannot be verified.")
    except ValueError as error:
        raise SavedWorkError(403, "cross_origin", "Cross-origin Saved view requests are not allowed.") from error


def validate_mutation_headers(headers, scheme="http"):
    _validate_origin(headers, scheme, write=True)


def validate_read_headers(headers, scheme="http"):
    _validate_origin(headers, scheme, write=False)


def fingerprint(value):
    """Keep original Saved canonical bytes stable across storage consolidation."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class SavedObjectService:
    """Shared lifecycle for the two existing Saved kinds; wire projections stay specific."""
    kind: str
    unavailable_error = staticmethod(unavailable)

    def __init__(self, repository):
        self.repository = repository

    def _call(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception as error:
            if getattr(error, "message", None) == "user_work_limit_reached":
                raise SavedWorkError(413, "saved_work_limit_reached",
                                     "This change exceeds the portable backup capacity (size or item limit). "
                                     "Shorten or remove some saved work before adding more.") from None
            raise self.unavailable_error() from error

    def _identity(self, row, expected_id=None):
        try:
            if not isinstance(row, dict) or row.get("kind") != self.kind:
                raise invalid()
            identity = saved_id(row.get("id"))
            if expected_id is not None and identity != expected_id:
                raise invalid()
            return identity
        except SavedWorkError as error:
            raise self.unavailable_error() from error

    def _lifecycle_result(self, result, identity):
        result = self._outcome(result)
        if result["outcome"] == "deleted":
            self._outcome({"outcome": "gone"})
        return self._envelope(result.get("object"), identity)

    def create(self, body):
        keys = ("id", "name", "channel_id", "conditions") if self.kind == "view" else ("id", "name")
        data = exact_object(body, keys)
        identity, name = saved_id(data["id"]), saved_name(data["name"])
        source_id = channel_id(data["channel_id"]) if self.kind == "view" and data["channel_id"] is not None else None
        conditions = normalize_conditions(data["conditions"]) if self.kind == "view" else None
        if self.kind == "view":
            # A canvas (version 4) names its sources inside the graph and is bound to no channel.
            bound = conditions["version"] == 1 or conditions["version"] in (2, 3) and conditions["query"]["scope"]["kind"] == "channel"
            if bound != (source_id is not None):
                raise invalid("Saved source binding does not match its query.")
        digest = create_fingerprint(name, source_id, conditions) if self.kind == "view" else fingerprint({"name": name})
        result = self._call(self.repository.create_object, identity, name, digest, source_id, conditions)
        return self._lifecycle_result(result, identity)

    def rename(self, object_id, body):
        identity = saved_id(object_id)
        data = exact_object(body, ("name", "expected_revision"))
        result = self._call(self.repository.rename_object, identity, saved_name(data["name"]), revision(data["expected_revision"]))
        return self._lifecycle_result(result, identity)

    def delete(self, object_id, expected_revision):
        identity = saved_id(object_id)
        result = self._outcome(self._call(self.repository.delete_object, identity, revision(expected_revision)))
        if result["outcome"] != "deleted" or result.get("id") != identity:
            raise self.unavailable_error()
        return {"contract_version": 1, "id": identity, "deleted": True}

    def get(self, object_id):
        identity = saved_id(object_id)
        row = self._call(self.repository.get_object, identity)
        if row is None:
            self._outcome({"outcome": "not_found"})
        return self._envelope(row, identity)


async def saved_response(request, action, *args, body=False, write=False, noun="Saved view"):
    """One bounded same-origin/no-store HTTP boundary for both Saved routers."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    try:
        (validate_mutation_headers if write else validate_read_headers)(request.headers, request.url.scheme)
        if body:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise invalid(f"{noun} writes require JSON.")
            payload = bytearray()
            async for chunk in request.stream():
                if len(payload) + len(chunk) > MAX_BODY_BYTES:
                    raise SavedWorkError(413, "request_too_large", f"{noun} request is too large.")
                payload.extend(chunk)
            try:
                parsed = json.loads(payload)
            except (ValueError, UnicodeDecodeError, RecursionError) as error:
                raise invalid(f"{noun} request must contain valid JSON.") from error
            args = (*args, parsed)
        result = await run_in_threadpool(action, *args)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except SavedWorkError as error:
        return JSONResponse({"detail": {"code": error.code, "message": error.message}},
                            status_code=error.status_code, headers={"Cache-Control": "no-store"})


def saved_pagination(request):
    if set(request.query_params) - {"limit", "cursor"} or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise invalid()
    value = request.query_params.get("limit", "50")
    if not value.isascii() or not value.isdigit() or len(value) > 3:
        raise invalid()
    return int(value), request.query_params.get("cursor")


def saved_delete_revision(request):
    values = request.query_params.getlist("expected_revision")
    if set(request.query_params) != {"expected_revision"} or len(values) != 1 or not values[0].isascii() or not values[0].isdigit() or len(values[0]) > 16:
        raise invalid()
    return int(values[0])
