"""Pure request planning: the preview of a one-off acquisition.

``plan_request`` performs no I/O, imports no transport and spends no quota. It
refuses unknown, control and Discovery-only methods; unknown, reserved,
deprecated or context-restricted parameters; filter-rule, bound and value
violations; and any worst-case cost above the budget. The resulting plan is
fingerprinted (SHA-256 of its canonical form, no secrets) so execute can prove
that it runs exactly what the owner previewed and confirmed.

 (owner OAuth, Analytics, Reporting): IDs named in a registry path
(``captions/{id}``, ``jobs/{jobId}``) are checked and substituted here; the
Analytics combination rules and the Reporting media chain are planned by
``owner_sources`` (pure, no transport). Owner acquisitions have their own ceilings
(``OWNER_LIMITS``) and the owner binding must hold the method's scopes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import quote

from .credentials import OwnerContext, PublicKeyContext
from .errors import PlanRejected
from .families import policy
from .registry import OWNER, UNSUPPORTED_CONTEXTS
from .selection import FieldSelection, resolve

# Parameters a client may never set: the key, the mask, parts, page tokens and Google system parameters.
RESERVED = frozenset({"key", "fields", "part", "pageToken", "access_token", "oauth_token", "alt", "callback",
                      "prettyPrint", "quotaUser", "userIp", "upload_protocol", "uploadType", "$.xgafv"})
MAX_TEXT = 1024
IDENTITIES = {
    "video": re.compile(r"[A-Za-z0-9_-]{11}\Z"),
    "channel": re.compile(r"UC[A-Za-z0-9_-]{22}\Z"),
    "playlist": re.compile(r"[A-Za-z0-9_-]{2,64}\Z"),
    "playlist_item": re.compile(r"[A-Za-z0-9_=+/-]{1,256}\Z"),
    # Owner, Analytics and Reporting identities. Kinds used inside a URL path
    # (caption, reporting_job, reporting_report) never allow "/" or ".".
    "caption": re.compile(r"[A-Za-z0-9_=+-]{1,256}\Z"),
    "live_broadcast": re.compile(r"[A-Za-z0-9_-]{11}\Z"),
    "live_stream": re.compile(r"[A-Za-z0-9_-]{1,128}\Z"),
    "live_chat_moderator": re.compile(r"[A-Za-z0-9_=+./-]{1,256}\Z"),
    "membership_level": re.compile(r"[A-Za-z0-9_-]{1,128}\Z"),
    "playlist_image": re.compile(r"[A-Za-z0-9_-]{1,128}\Z"),
    "super_chat_event": re.compile(r"[A-Za-z0-9_=+./:-]{1,256}\Z"),
    "abuse_report_reason": re.compile(r"[A-Za-z0-9_-]{1,64}\Z"),
    "analytics_group": re.compile(r"[A-Za-z0-9_-]{1,64}\Z"),
    "analytics_group_item": re.compile(r"[A-Za-z0-9_=+./-]{1,128}\Z"),
    "reporting_job": re.compile(r"[A-Za-z0-9_-]{1,40}\Z"),
    "reporting_report": re.compile(r"[A-Za-z0-9_-]{1,64}\Z"),
    "report_type": re.compile(r"[a-z][a-z0-9_]{0,63}\Z"),
    # (public families). The reference pages give no format for these IDs:
    # the patterns are bounded character sets, not a claim about the provider's format.
    "activity": re.compile(r"[A-Za-z0-9_.=+/-]{1,256}\Z"),
    "channel_section": re.compile(r"[A-Za-z0-9_.-]{1,160}\Z"),
    "comment_thread": re.compile(r"[A-Za-z0-9_-]{1,128}\Z"),
    "comment": re.compile(r"[A-Za-z0-9_-]{1,128}(\.[A-Za-z0-9_-]{1,128})?\Z"),   # a reply is <parent>.<reply>
    "subscription": re.compile(r"[A-Za-z0-9_-]{1,128}\Z"),
    "video_category": re.compile(r"[0-9]{1,6}\Z"),
    "i18n_language": re.compile(r"[A-Za-z]{2,3}([_-][A-Za-z0-9]{1,8}){0,3}\Z"),
    "i18n_region": re.compile(r"[A-Za-z]{2}\Z"),
    "live_chat": re.compile(r"[A-Za-z0-9_=+/-]{1,256}\Z"),
    "live_chat_message": re.compile(r"[A-Za-z0-9_.=+/-]{1,256}\Z"),
}
# Checked text shapes of parameters (registry ``value_format``); Analytics lists are checked by ``analytics``.
FORMATS = {
    "date": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"),
    "rfc3339_utc": re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z\Z"),
    "language": re.compile(r"[A-Za-z]{2,3}([_-][A-Za-z0-9]{1,8}){0,3}\Z"),
    "currency": re.compile(r"[A-Z]{3}\Z"),
    "analytics_ids": re.compile(r"channel==(MINE|UC[A-Za-z0-9_-]{22})\Z"),
    "analytics_names": re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}(,[A-Za-z][A-Za-z0-9]{0,63}){0,63}\Z"),
    "analytics_sort": re.compile(r"-?[A-Za-z][A-Za-z0-9]{0,63}(,-?[A-Za-z][A-Za-z0-9]{0,63}){0,15}\Z"),
    "analytics_filters": re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}==[^;]{1,}(;[A-Za-z][A-Za-z0-9]{0,63}==[^;]{1,}){0,15}\Z"),
}
SUPPORTED_PAGINATION = ("pageToken", "none", "startIndex", "media")
_PATH_PARAMETER = re.compile(r"\{(\+?)([A-Za-z][A-Za-z0-9]*)\}")


# search.list has its own Search Queries bucket of 100 calls per day (1 unit each): one
# acquisition may spend at most 5 of them (250 hits); daily_cap bounds the day.
SEARCH_UNITS_PER_ACQUISITION = 5


@dataclass(frozen=True)
class Budget:
    """Bounds of one acquisition. The defaults are the installation ceilings."""
    max_requests: int = 10
    max_units: Mapping[str, int] = field(default_factory=lambda: MappingProxyType(
        {"general": 10, "video_batch_get_stats": 10, "search_queries": SEARCH_UNITS_PER_ACQUISITION}))
    max_rows: int = 5000
    max_response_bytes: int = 2 * 1024 * 1024
    deadline_seconds: float = 60.0


LIMITS = Budget()
# Owner context ceilings (a proposal next to, which sets 10 general units for key acquisitions): one
# captions.download costs 200 general units, captions.list and liveChatModerators.list 50, so an owner
# acquisition may reach 200 general units; the daily cap of 500 general units still counts every one of
# them. Analytics and Reporting spend no Data API units: at most 10 requests each.
OWNER_LIMITS = Budget(max_units=MappingProxyType({"general": 200, "video_batch_get_stats": 10,
                                                  "search_queries": SEARCH_UNITS_PER_ACQUISITION,
                                                  "youtube_analytics": 10, "youtube_reporting": 10}))


def ceilings(owner):
    return OWNER_LIMITS if owner else LIMITS


@dataclass(frozen=True)
class AcquisitionRequest:
    method_id: str
    parameters: Mapping[str, object]
    selection: FieldSelection
    auth: PublicKeyContext | OwnerContext = PublicKeyContext()
    budget: Budget | None = LIMITS      # None: the ceilings of the request's context


@dataclass(frozen=True)
class PlannedRequest:
    params: tuple            # ((name, value), ...) without part, fields, pageToken and maxResults
    targets: tuple           # IDs of an ID batch; empty otherwise


@dataclass(frozen=True)
class RequestPlan:
    method_id: str
    registry_revision: int
    api: str
    endpoint: str
    ledger_endpoint_label: str
    quota_bucket: str
    quota_units: int
    strategy: str            # id_batch | page_token | single | poll | start_index | media
    requests: tuple
    page_size: int | None
    max_pages: int
    parts: tuple
    fields: tuple
    fields_mask: str
    worst_cost: Mapping[str, int]
    max_requests: int
    max_rows: int
    max_response_bytes: int
    deadline_seconds: float
    context: str
    owner: bool
    owner_scopes: tuple
    filter_param: str | None
    filter_values: tuple
    tracks_requested_ids: bool
    order: str
    row_unit: str
    executable: bool
    fingerprint: str
    # Per-family planning output (Analytics columns and tables, media reader, polling, path target).
    options: Mapping = field(default_factory=lambda: MappingProxyType({}))

    @property
    def part_param(self):
        return ",".join(self.parts)

    def preview(self):
        """Safe projection: no provider data, no key, the planned parameters are the owner's own."""
        value = {
            "method": self.method_id, "registry_revision": self.registry_revision, "executable": self.executable,
            "plan_fingerprint": self.fingerprint, "strategy": self.strategy, "requests_planned": len(self.requests)
            if self.strategy not in ("page_token", "start_index", "media") else self.max_pages, "page_size": self.page_size,
            "worst_cost": dict(self.worst_cost), "quota_bucket": self.quota_bucket, "quota_units": self.quota_units,
            "ledger_endpoint_label": self.ledger_endpoint_label, "parts": list(self.parts),
            "fields": list(self.fields), "fields_mask": self.fields_mask, "context": self.context,
            "order": self.order, "row_unit": self.row_unit,
            "budget": {"max_requests": self.max_requests, "max_rows": self.max_rows,
                       "max_response_bytes": self.max_response_bytes, "deadline_seconds": self.deadline_seconds},
        }
        if "tables" in self.options:
            value["report_tables"] = list(self.options["tables"])
        return value


def _budget(value, ceiling=LIMITS):
    if not isinstance(value, Budget):
        raise PlanRejected("budget_invalid")
    for name in ("max_requests", "max_rows", "max_response_bytes"):
        number = getattr(value, name)
        if type(number) is not int or not 1 <= number <= getattr(ceiling, name):
            raise PlanRejected("budget_invalid", name)
    seconds = value.deadline_seconds
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= ceiling.deadline_seconds:
        raise PlanRejected("budget_invalid", "deadline_seconds")
    units = dict(value.max_units)
    for bucket, number in units.items():
        if (bucket not in ceiling.max_units or type(number) is not int
                or not 0 <= number <= ceiling.max_units[bucket]):
            raise PlanRejected("budget_invalid", "max_units")
    return units


def _text(value, name):
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT or value != value.strip() \
            or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PlanRejected("parameter_invalid", name)
    return value


def _identity(kind, value, name):
    if kind == "handle":
        from user_channel_input import parse_user_channel_input
        try:
            _, lookup, handle = parse_user_channel_input(value)
        except ValueError:
            raise PlanRejected("parameter_invalid", name) from None
        if lookup != "handle":
            raise PlanRejected("parameter_invalid", name)
        return handle
    pattern = IDENTITIES.get(kind)
    if pattern is None or pattern.fullmatch(value) is None:
        raise PlanRejected("parameter_invalid", name)
    return value


def _formatted(kind, text, name):
    pattern = FORMATS.get(kind)
    if pattern is None or pattern.fullmatch(text) is None:
        raise PlanRejected("parameter_invalid", name)
    try:
        if kind == "date":
            date.fromisoformat(text)
        elif kind == "rfc3339_utc":
            datetime.fromisoformat(text[:19] + "+00:00")
    except ValueError:
        raise PlanRejected("parameter_invalid", name) from None
    return text


def _values(parameter, value, *, batch=False):
    """Canonical query text of one parameter value (lists comma-joined, duplicates dropped)."""
    name = parameter.name
    if parameter.type == "boolean":
        if type(value) is not bool:
            raise PlanRejected("parameter_invalid", name)
        return ("true" if value else "false",)
    if parameter.type in ("integer", "number"):
        if type(value) is not int or (parameter.minimum is not None and value < parameter.minimum) \
                or (parameter.maximum is not None and value > parameter.maximum):
            raise PlanRejected("parameter_invalid", name)
        return (str(value),)
    if parameter.type != "string":
        raise PlanRejected("parameter_not_supported", name)
    if parameter.repeated:
        items = value if isinstance(value, list) else [value]
        if not items or not all(isinstance(item, str) for item in items):
            raise PlanRejected("parameter_invalid", name)
        # Trim and drop repeats (youtube_batch_stats.unique_video_ids); empty items are refused.
        texts = list(dict.fromkeys(_text(item.strip(), name) for item in
                                   (piece for item in items for piece in item.split(","))))
    else:
        texts = [_text(value, name)]
    if parameter.values and any(text not in parameter.values for text in texts):
        raise PlanRejected("parameter_value_unknown", name)
    if parameter.identity:
        texts = [_identity(parameter.identity, text, name) for text in texts]
    if parameter.value_format:
        texts = [_formatted(parameter.value_format, text, name) for text in texts]
    # An ID batch is split into requests of at most max_items; any other list is refused above its bound.
    if parameter.max_items is not None and len(texts) > parameter.max_items and not batch:
        raise PlanRejected("parameter_too_many_values", name)
    return tuple(texts)


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                          .encode("ascii")).hexdigest()


def _owner_scopes(method, auth, options):
    """The scopes the credential may use; an owner binding without them is refused before any network."""
    groups = options.get("scope_groups") or ((tuple(method.oauth_scopes),) if method.oauth_scopes else ())
    if not isinstance(auth, OwnerContext):
        return tuple(sorted({scope for group in groups for scope in group}))
    held = auth.binding.scopes
    usable = set()
    for group in groups:
        found = held & set(group)
        if not found:
            raise PlanRejected("owner_scope_missing")
        usable |= found
    return tuple(sorted(usable))


def _endpoint(method, values, managed):
    """Substitute checked path IDs; the path never carries a client value unchecked."""
    names = []

    def substitute(match):
        plus, name = match.groups()
        if name in managed:
            return match.group(0)          # filled by the layer during execution (media chain)
        texts = values.get(name)
        if not texts or len(texts) != 1:
            raise PlanRejected("parameter_required", name)
        names.append(name)
        return quote(texts[0], safe="/" if plus else "")
    return _PATH_PARAMETER.sub(substitute, method.endpoint), names


def plan_request(request, registry):
    if not isinstance(request, AcquisitionRequest):
        raise PlanRejected("request_invalid")
    method = registry.methods.get(request.method_id) if isinstance(request.method_id, str) else None
    if method is None:
        raise PlanRejected("method_control_not_readable" if request.method_id in registry.control_methods
                           else "method_unknown")
    if method.status == "discovery_only" or method.reference is None:
        raise PlanRejected("method_discovery_only")
    if method.http_method != "GET":
        raise PlanRejected("method_not_readable")
    if method.pagination.style == "server_stream":
        # liveChatMessages.streamList: the REST framing of the stream is not documented and the
        # shared transport reads bounded whole bodies; liveChatMessages.list polls the same chat.
        raise PlanRejected("stream_reader_not_available")
    if method.pagination.style not in SUPPORTED_PAGINATION:
        raise PlanRejected("pagination_not_supported")
    if method.quota.units is None or method.quota.status == "unverified":
        raise PlanRejected("quota_unverified")
    owner = isinstance(request.auth, OwnerContext)
    if not owner and not isinstance(request.auth, PublicKeyContext):
        raise PlanRejected("context_invalid")
    if (OWNER if owner else "public_key") not in method.auth_contexts:
        raise PlanRejected("context_not_supported")
    budget = request.budget if request.budget is not None else ceilings(owner)
    units_budget = _budget(budget, ceilings(owner))

    given = request.parameters if isinstance(request.parameters, Mapping) else None
    if given is None or len(given) > 32:
        raise PlanRejected("parameters_invalid")
    chain_inputs = policy.CHAIN_INPUTS.get(method.id, {})
    managed = policy.LAYER_MANAGED.get(method.id, frozenset())
    values, chain = {}, {}
    for name, value in given.items():
        if not isinstance(name, str):
            raise PlanRejected("parameters_invalid")
        if name in RESERVED:
            raise PlanRejected("parameter_reserved", name)
        if name in chain_inputs:
            chain[name] = _identity(chain_inputs[name], _text(value, name), name)
            continue
        if name in managed:
            raise PlanRejected("parameter_layer_managed", name)
        parameter = method.parameters.get(name)
        if parameter is None:
            raise PlanRejected("parameter_unknown")
        if parameter.deprecated:
            raise PlanRejected("parameter_deprecated", name)
        if parameter.discovery_only:
            raise PlanRejected("parameter_discovery_only", name)
        if parameter.context in UNSUPPORTED_CONTEXTS:
            raise PlanRejected("context_not_supported", name)
        if parameter.context == OWNER and not owner:
            raise PlanRejected("parameter_owner_only", name)
        values[name] = _values(parameter, value, batch=name == method.id_batch_param)
    for name in chain_inputs:
        if name not in chain:
            raise PlanRejected("parameter_required", name)
    for name in values:
        parameter = method.parameters[name]
        for other in parameter.not_with:
            if other in values:
                raise PlanRejected("parameter_conflict", name)
        if parameter.only_with and not any(other in values for other in parameter.only_with):
            raise PlanRejected("parameter_needs_filter", name)
    policy.check_parameters(method.id, values)
    filters = [name for name in method.exactly_one_of if name in values]
    if method.exactly_one_of and len(filters) != 1:
        raise PlanRejected("filter_exactly_one_required")
    for name, parameter in method.parameters.items():
        if parameter.required and name not in ("part", "fields") and name not in values and name not in managed:
            raise PlanRejected("parameter_required", name)
    if method.id_batch_param and method.id_batch_param in values and not method.exactly_one_of:
        filters = [method.id_batch_param]
    filter_param = filters[0] if filters else None
    if filter_param and method.parameters[filter_param].type == "boolean" and values[filter_param] != ("true",):
        raise PlanRejected("parameter_invalid", filter_param)   # mine / mySubscribers / ...: only true selects
    endpoint, path_names = _endpoint(method, values, managed)
    options = {}
    if path_names:
        # The resource the path names is the target of the request (404/403 are about it).
        if filter_param is None:
            filter_param = path_names[-1]
        options["path_target"] = path_names[-1]
    path_values = {name: values.pop(name) for name in path_names}
    if filter_param is None:
        # A required ID parameter names the object a refusal is about (captions.list videoId, ...).
        filter_param = next((name for name, parameter in sorted(method.parameters.items())
                             if parameter.required and parameter.identity and name in values), None)

    if method.api == "analytics" and method.id == "youtubeAnalytics.reports.query":
        from .owner_sources import plan_report_query
        options.update(plan_report_query(values, binding=request.auth.binding if owner else None,
                                         max_rows=budget.max_rows))
    elif method.id in policy.MEDIA_READERS:
        from .owner_sources import plan_media
        options.update(plan_media(method, values, chain))
    if chain and filter_param is None:
        filter_param = policy.CHAIN_TARGETS[method.id]
        path_values[filter_param] = (chain[filter_param],)
    poll = policy.POLLING_MODES.get(method.id)
    if poll and values.get(poll[0], ()) == (poll[1],):
        options["poll"] = True
    if method.pagination.style == "media" or method.id == "youtubeAnalytics.reports.query":
        from .owner_sources import resolve_special_selection
        selection = resolve_special_selection(method, request.selection, options)
    else:
        selection = resolve(method, request.selection, owner=owner)
    owner_scopes = _owner_scopes(method, request.auth, options)

    bucket, units = method.quota.bucket, method.quota.units
    page_param = method.pagination.page_size_param
    page_size, requests = None, []
    # Analytics maxResults is a report-table bound, not only a page size: it travels unless pages are planned.
    paged = page_param if method.id != "youtubeAnalytics.reports.query" or options.get("page_size") else None
    base = tuple(sorted((name, ",".join(texts)) for name, texts in values.items()
                        if name != method.id_batch_param and name != paged))
    if method.id in policy.POLLED_METHODS:
        # A live chat is polled, never paged: one request per acquisition, the next poll is a new
        # explicit action with the continuation, no earlier than pollingIntervalMillis.
        strategy = "poll"
        page_size = int(values[page_param][0]) if page_param in values else None
        max_pages = 1
        requests.append(PlannedRequest(params=base, targets=()))
    elif method.id_batch_param and filter_param == method.id_batch_param:
        strategy = "id_batch"
        ids = values[method.id_batch_param]
        size = method.id_batch_max or method.parameters[method.id_batch_param].max_items or 50
        if len(ids) > budget.max_rows:
            raise PlanRejected("budget_rows_exceeded")
        for offset in range(0, len(ids), size):
            batch = ids[offset:offset + size]
            requests.append(PlannedRequest(params=base + ((method.id_batch_param, ",".join(batch)),), targets=batch))
        max_pages = len(requests)
        if max_pages > budget.max_requests:
            raise PlanRejected("budget_requests_exceeded")
    elif method.pagination.style == "media":
        strategy = "media"
        requests.append(PlannedRequest(params=base, targets=()))
        max_pages = options.get("requests", 1)
        if max_pages > budget.max_requests:
            raise PlanRejected("budget_requests_exceeded")
    elif method.pagination.style == "startIndex" and options.get("page_size"):
        strategy = "start_index"
        page_size = min(options["page_size"], budget.max_rows)
        base = tuple(item for item in base if item[0] != "maxResults")
        max_pages = min(budget.max_requests, math.ceil(budget.max_rows / page_size), units_budget.get(bucket, 0) // units)
        if max_pages < 1:
            raise PlanRejected("budget_units_exceeded")
        requests.append(PlannedRequest(params=base, targets=()))
    elif method.id == "youtube.search.list" and values.get("maxResults") == ("0",):
        # An explicit zero requests only the search envelope, even when Google supplies a token.
        strategy, page_size, max_pages = "single", 0, 1
        requests.append(PlannedRequest(params=base + (("maxResults", "0"),), targets=()))
    elif (method.pagination.style == "pageToken" and filter_param not in policy.SINGLE_RESULT_FILTERS.get(method.id, ())
          and (not method.parameters.get("pageToken") or not method.parameters["pageToken"].only_with
               or filter_param in method.parameters["pageToken"].only_with)):
        strategy = "page_token"
        if page_param:
            maximum = method.pagination.maximum or 50
            page_size = int(values[page_param][0]) if page_param in values else maximum
            page_size = min(page_size, maximum, budget.max_rows)
            if page_size < 1:
                raise PlanRejected("parameter_invalid", page_param)
            max_pages = min(budget.max_requests, math.ceil(budget.max_rows / page_size),
                            units_budget.get(bucket, 0) // units)
        else:
            max_pages = min(budget.max_requests, units_budget.get(bucket, 0) // units)   # server-sized pages
        if options.get("poll"):
            max_pages = min(max_pages, 1)    # members.list mode=updates: one poll, then a continuation
        if max_pages < 1:
            raise PlanRejected("budget_units_exceeded")
        requests.append(PlannedRequest(params=base, targets=()))
    else:
        strategy = "single"     # one resource (forHandle) or a method without pages
        max_pages = 1
        requests.append(PlannedRequest(params=base, targets=()))
    if strategy in ("page_token", "start_index"):
        worst = max_pages * units
    elif strategy == "media":
        worst = max_pages * units
    else:
        worst = len(requests) * units
    if worst > units_budget.get(bucket, 0):
        raise PlanRejected("budget_units_exceeded")
    identity = method.identity_field
    filter_parameter = method.parameters.get(filter_param) if filter_param else None
    # Only a repeated ID filter lists the rows themselves; a single ID of the same kind names a
    # relation (comments.list parentId: the parent, whose replies are the rows).
    tracks = bool(identity is not None and filter_parameter is not None and filter_parameter.identity
                  and filter_parameter.repeated and identity.role_value("identity") == filter_parameter.identity)
    # A resource named by the request path is the row itself (jobs.get, jobs.reports.get).
    tracks = tracks or bool(identity is not None and filter_parameter is not None
                            and filter_parameter.identity and options.get("path_target") == filter_param
                            and identity.role_value("identity") == filter_parameter.identity)
    filter_values = values.get(filter_param) or path_values.get(filter_param, ())
    context = request.auth.label
    order = (f"provider_param:{values['order'][0]}" if "order" in values else "provider")
    options_view = json.loads(json.dumps(options, sort_keys=True))
    canonical = {
        "v": 1, "method": method.id, "revision": registry.revision, "context": context, "strategy": strategy,
        "requests": [list(map(list, item.params)) for item in requests], "page_size": page_size,
        "max_pages": max_pages, "parts": list(selection.parts), "fields": list(selection.fields),
        "mask": selection.mask,
        "bucket": bucket, "units": units, "budget": {"max_requests": budget.max_requests, "max_rows": budget.max_rows,
        "max_response_bytes": budget.max_response_bytes, "deadline_seconds": budget.deadline_seconds,
        "max_units": dict(sorted(units_budget.items()))},
    }
    if endpoint != method.endpoint or options_view or chain:
        canonical.update(endpoint=endpoint, options=options_view, chain=dict(sorted(chain.items())))
    return RequestPlan(
        method_id=method.id, registry_revision=registry.revision, api=method.api, endpoint=endpoint,
        ledger_endpoint_label=method.ledger_endpoint_label, quota_bucket=bucket, quota_units=units,
        strategy=strategy, requests=tuple(requests), page_size=page_size, max_pages=max_pages,
        parts=selection.parts, fields=selection.fields, fields_mask=selection.mask,
        worst_cost=MappingProxyType({bucket: worst}), max_requests=budget.max_requests, max_rows=budget.max_rows,
        max_response_bytes=budget.max_response_bytes, deadline_seconds=float(budget.deadline_seconds),
        context=context, owner=owner, owner_scopes=owner_scopes, filter_param=filter_param,
        filter_values=tuple(filter_values), tracks_requested_ids=tracks, order=order,
        row_unit=method.row_unit, executable=method.id in policy.EXECUTABLE, fingerprint=_fingerprint(canonical),
        options=MappingProxyType(dict(options, chain=MappingProxyType(dict(chain))) if chain else dict(options)))
