"""Bounded, pure query admission. A caller must bind trusted access first.

No SQL, I/O, environment settings or client-provided grants. The SQL repository
accepts only the immutable plan produced here, never a request dictionary.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
import time
import unicodedata

from pydantic import ValidationError

from .contracts import (QueryRequest, BUDGET_LIMITS, ERROR_STATUS, MAX_BODY_BYTES, MAX_DEPTH, MAX_FROZEN_ROWS,
                        MAX_LEAVES, MAX_LIST_VALUES, uint_decimal, signed_decimal)
from .operations import OPERATIONS as CALCULATIONS, FIELD_GRANTS
from .registry import DEPENDENCIES, FIELDS, OPERATORS, SOURCE, SOURCES, CHANNEL_SOURCE_ID, REGISTRY_VERSION, UNICODE_VERSION, supports_source, source_field


class QueryRejected(ValueError):
    """Fixed public code/path only. Never retain input, SQL or provider details.

    A query_budget_exceeded refusal may also name the bound it reached
    (`limit`, one of BUDGET_LIMITS); the key is present only then."""
    def __init__(self, code, path=None, limit=None):
        if code not in ERROR_STATUS:
            raise ValueError("unknown_query_error")
        if limit is not None and (code != "query_budget_exceeded" or limit not in BUDGET_LIMITS):
            raise ValueError("unknown_query_limit")
        self.code, self.path, self.limit, self.status = code, path, limit, ERROR_STATUS[code]
        super().__init__(code)

    def payload(self):
        value = {"code": self.code, "path": self.path}
        if self.limit is not None:
            value["limit"] = self.limit
        return value


def check_budget(deadline, cancelled):
    """Stop a read that is past its deadline or that its caller stopped.

    Past the deadline the refusal names the time limit. A stopped read (the
    client left, or a research graph out of its own time, which names its
    limit itself) gets the bare code: no client reads that answer."""
    if time.monotonic() >= deadline:
        raise QueryRejected("query_budget_exceeded", limit="time")
    if cancelled():
        raise QueryRejected("query_budget_exceeded")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class AccessContext:
    """Server-owned installation binding; NOT an authentication implementation.

    Integration must construct this after the existing owner/session and HOT
    admission. A separate installation needs its own id and cursor key. Only
    the existing public 696-hour lifecycle is executable in this slice.
    """
    workspace_id: str
    access_revision: str
    enabled: bool = False
    allow_frozen: bool = True
    allowed_fields: frozenset[str] = frozenset(FIELDS)
    allowed_operators: frozenset[str] = frozenset(OPERATORS)
    allowed_operations: frozenset[str] = frozenset({"video.observed_age.v1"})
    disabled_dependencies: frozenset[str] = frozenset()
    disabled_type_fields: frozenset[tuple[str,str]] = frozenset()
    # Video formats the owner turned off in config.ini. A
    # video query of one is refused with format_turned_off, not answered empty.
    disabled_video_types: frozenset[str] = frozenset()
    preferences_revision: str = "existing-v1"
    type_revision: str = "canonical-membership-v1"
    lifecycle_revision: str = "public-696h-v1"

    def __post_init__(self):
        for name in ("workspace_id", "access_revision", "preferences_revision",
                     "type_revision", "lifecycle_revision"):
            value = getattr(self, name)
            if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
                raise ValueError("invalid_trusted_query_context")
        if type(self.enabled) is not bool or type(self.allow_frozen) is not bool or any(type(getattr(self, n)) is not frozenset
                for n in ("allowed_fields", "allowed_operators", "allowed_operations", "disabled_dependencies")):
            raise ValueError("invalid_trusted_query_context")
        if type(self.disabled_type_fields) is not frozenset or any(type(item) is not tuple or len(item)!=2 or item[0] not in SOURCE.video_types or item[1] not in FIELDS for item in self.disabled_type_fields):
            raise ValueError("invalid_trusted_query_context")
        if (type(self.disabled_video_types) is not frozenset
                or not self.disabled_video_types <= set(SOURCE.video_types)
                or self.disabled_video_types == set(SOURCE.video_types)):
            raise ValueError("invalid_trusted_query_context")
        for values, registry in ((self.allowed_fields, FIELDS), (self.allowed_operators, OPERATORS),
                                 (self.disabled_dependencies, DEPENDENCIES), (self.allowed_operations, CALCULATIONS)):
            if any(type(value) is not str or value not in registry for value in values):
                raise ValueError("invalid_trusted_query_context")

    def fingerprint(self):
        return hashlib.sha256(canonical({
            "workspace": self.workspace_id, "access": self.access_revision,
            "enabled": self.enabled, "allow_frozen": self.allow_frozen, "fields": sorted(self.allowed_fields),
            "operators": sorted(self.allowed_operators), "operations": sorted(self.allowed_operations),
            "disabled": sorted(self.disabled_dependencies),
            "disabled_type_fields": sorted(self.disabled_type_fields),
            "preferences": self.preferences_revision, "type": self.type_revision,
            "lifecycle": self.lifecycle_revision, "registry": REGISTRY_VERSION,
            "unicode": UNICODE_VERSION,
            # Present only when a format is off, so every earlier fingerprint stays.
            **({"turned_off": sorted(self.disabled_video_types)} if self.disabled_video_types else {}),
        })).hexdigest()


@dataclass(frozen=True)
class ValidatedQuery:
    encoded: bytes
    fingerprint: str
    context_fingerprint: str
    referenced_fields: tuple[str, ...]
    shape_hash: str

    @property
    def request(self):
        # Each consumer gets an independent copy; frozen Pydantic alone leaves
        # nested lists mutable and must not serve as a validation seal.
        return QueryRequest.model_validate_json(self.encoded)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise QueryRejected("invalid_query")
        result[key] = value
    return result


def _reject_constant(value):
    raise QueryRejected("invalid_query")


def _json_budget(raw):
    """Bound parser nesting before json/Pydantic recursion; strings are opaque."""
    level, quoted, escape = 0, False, False
    for char in raw:
        if quoted:
            if escape:
                escape = False
            elif char == 92:
                escape = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            level += 1
            if level > 24:
                raise QueryRejected("query_budget_exceeded", limit="request")
        elif char in (93, 125):
            level -= 1


def _text(value):
    if type(value) is not str or len(value) > 1000 or "\x00" in value:
        raise QueryRejected("invalid_query")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise QueryRejected("invalid_query") from None
    return value


def literal(field, value):
    """Canonical values, interpreted exactly as the SQL field mapping."""
    definition = FIELDS[field]
    kind = definition.value_type
    if kind == "boolean":
        if type(value) is not bool:
            raise QueryRejected("invalid_query")
        return value
    value = _text(value)
    if len(value) > definition.max_text_length:
        raise QueryRejected("invalid_query")
    if definition.allowed_values is not None and value not in definition.allowed_values:
        raise QueryRejected("invalid_query")
    if kind == "signed_decimal":
        try: signed_decimal(value)
        except ValueError: raise QueryRejected("invalid_query") from None
    elif kind == "uint_decimal":
        try:
            uint_decimal(value)
        except ValueError:
            raise QueryRejected("invalid_query") from None
    elif kind in {"video_id", "channel_id"}:
        pattern = r"[A-Za-z0-9_-]{11}" if kind == "video_id" else r"UC[A-Za-z0-9_-]{22}"
        if re.fullmatch(pattern, value) is None:
            raise QueryRejected("invalid_query")
    elif kind == "video_type" and value not in SOURCE.video_types:
        raise QueryRejected("invalid_query")
    elif kind == "utc_instant":
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value) is None:
            raise QueryRejected("invalid_query")
        try:
            value = datetime.fromisoformat(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            raise QueryRejected("invalid_query") from None
    elif kind == "local_date":
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise QueryRejected("invalid_query")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise QueryRejected("invalid_query") from None
    return value


def search_literal(value):
    """The Library title search text: trimmed, each run of Unicode whitespace
    (str.isspace) one space. The stored text is never collapsed."""
    return re.sub(r"\s+", " ", value.strip())


MAX_WINDOW_HOURS = 87600  # Ten years of whole hours.


def window_hours(value):
    """The canonical whole-hour count of within_last_hours_v1: "1".."87600"."""
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]{0,4}", value) is None or int(value) > MAX_WINDOW_HOURS:
        raise QueryRejected("invalid_query")
    return value


def _field(field, context, source_id=SOURCE.id):
    if not supports_source(field,source_id):
        raise QueryRejected("unsupported_field")
    if field in FIELD_GRANTS and FIELD_GRANTS[field] not in context.allowed_operations:
        raise QueryRejected("grant_required")
    if field not in context.allowed_fields:
        raise QueryRejected("field_disabled")
    if set(source_field(field,source_id).dependencies) & context.disabled_dependencies:
        raise QueryRejected("dependency_disabled")


def capabilities(context, source_id=SOURCE.id):
    """The same definitions and effective restrictions used by admission."""
    if not context.enabled:
        raise QueryRejected("policy_denied")
    if source_id not in SOURCES:raise QueryRejected("unsupported_source")
    source=SOURCES[source_id]
    values = []
    for field in FIELDS.values():
        try:
            _field(field.id, context, source_id)
        except QueryRejected:
            continue
        values.append({"id": field.id, "value_type": field.value_type,
                       "nullable": field.nullable, "sortable": field.sortable,
                       "operators": [op for op in field.operators if op in context.allowed_operators],
                       "enabled_video_types": [kind for kind in source.video_types if (kind,field.id) not in context.disabled_type_fields],
                       **({"values": list(field.allowed_values)} if field.allowed_values is not None else {})})
    types = [] if set(source.admission_dependencies) & context.disabled_dependencies else list(source.video_types)
    if set(SOURCE.stream_dependencies) & context.disabled_dependencies and "stream" in types:
        types.remove("stream")
    types = [kind for kind in types if kind not in context.disabled_video_types]
    from .observation_limits import age_limits
    from .frozen import FROZEN_TTL_SECONDS
    from .registry import describe_presets as presets
    return {"query_version": 1, "registry_version": REGISTRY_VERSION,
            "source_id": source.id, "grain":source.grain, "video_types": types, "scopes": list(source.scopes),
            "max_video_types": source.max_video_types, "fields": values, "operations": sorted(op for op in context.allowed_operations if op.startswith("channel." if source.id==CHANNEL_SOURCE_ID else "video.")), "consistency": "current",
            # "frozen": the whole matching population read once and kept for
            # at most ttl_seconds; larger results are refused, never truncated.
            "consistency_modes": ["current", "frozen"] if context.allow_frozen else ["current"],
            "frozen": {"ttl_seconds": FROZEN_TTL_SECONDS, "max_rows": MAX_FROZEN_ROWS, "max_page_size": 200} if context.allow_frozen else None,
            # Oldest video age (seconds) offered per observed change window; null: every age.
            "observation_age_limits": age_limits(types) if source.id != CHANNEL_SOURCE_ID else {},
            # Shared preset ranges per video type, for the fields offered above.
            "presets": presets({value["id"] for value in values}) if source.id != CHANNEL_SOURCE_ID else {},
            # Formats the owner turned off; the key is absent while all are on.
            **({"turned_off_video_types": [kind for kind in SOURCE.video_types if kind in context.disabled_video_types]}
               if context.disabled_video_types else {})}


def validate(raw: bytes, context: AccessContext) -> ValidatedQuery:
    if not isinstance(context, AccessContext) or not context.enabled:
        raise QueryRejected("policy_denied")
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise QueryRejected("backend_unavailable")
    if type(raw) is not bytes:
        raise QueryRejected("invalid_query")
    if len(raw) > MAX_BODY_BYTES:
        raise QueryRejected("query_budget_exceeded", limit="request")
    _json_budget(raw)
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, QueryRejected):
            raise
        raise QueryRejected("invalid_query") from None
    if not isinstance(obj, dict):
        raise QueryRejected("invalid_query")
    if type(obj.get("query_version")) is not int or obj["query_version"] != 1:
        raise QueryRejected("unsupported_version")
    source_value=obj.get('source')
    if type(source_value) is not dict or set(source_value)!={'id'} or type(source_value['id']) is not str or source_value['id'] not in SOURCES:
        raise QueryRejected('unsupported_source')
    source=SOURCES[source_value['id']]
    if source.grain=='video' and obj.get('video_types') not in (['long'],['short'],['stream']):
        raise QueryRejected('unsupported_video_types')
    if source.grain=='video' and obj['video_types'][0] in context.disabled_video_types:
        raise QueryRejected('format_turned_off', 'video_types')
    if set(source.admission_dependencies)&context.disabled_dependencies:raise QueryRejected('dependency_disabled')
    stack, leaves = [(obj.get("where"), 1)], 0
    while stack:
        node, depth = stack.pop()
        if node is None:
            continue
        if depth > MAX_DEPTH:
            raise QueryRejected("query_budget_exceeded", "where", limit="request")
        if not isinstance(node, dict):
            raise QueryRejected("invalid_query", "where")
        if "field" in node:
            if type(node.get("op")) is str and node["op"] not in OPERATORS:
                raise QueryRejected("unsupported_operator", "where")
            leaves += 1
            if leaves > MAX_LEAVES:
                raise QueryRejected("query_budget_exceeded", "where", limit="request")
        else:
            children = node.get("all", node.get("any", [node.get("not")]))
            if not isinstance(children, list) or len(children) > MAX_LEAVES:
                raise QueryRejected("query_budget_exceeded", "where", limit="request")
            stack.extend((child, depth + 1) for child in children)
    try:
        request = QueryRequest.model_validate(obj)
    except (ValidationError, ValueError, RecursionError):
        raise QueryRejected("invalid_query") from None
    if request.consistency == "frozen" and not context.allow_frozen:
        raise QueryRejected("policy_denied", "consistency")
    obj = request.model_dump(mode="json", by_alias=True)
    if request.page.after is not None and re.fullmatch(r"[A-Za-z0-9_-]+", request.page.after) is None:
        raise QueryRejected("cursor_expired")
    fields = set(obj["select"]) | set(obj["coverage"]["required_fields"])
    for values in (obj["select"], obj["coverage"]["required_fields"], [v["field"] for v in obj["order_by"]]):
        if len(values) != len(set(values)):
            raise QueryRejected("invalid_query")
    for item in obj["order_by"]:
        _field(item["field"], context, source.id)
        if not FIELDS[item["field"]].sortable:
            raise QueryRejected("unsupported_operator", "order_by")
        fields.add(item["field"])
    shape = []
    stack = [obj["where"]]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if "field" not in node:
            key = next(k for k in ("all", "any", "not") if k in node)
            shape.append(key)
            stack.extend(node[key] if key != "not" else [node[key]])
            continue
        field, op = node["field"], node["op"]
        _field(field, context, source.id)
        fields.add(field)
        if op not in FIELDS[field].operators:
            raise QueryRejected("unsupported_operator")
        if op not in context.allowed_operators:
            raise QueryRejected("policy_denied")
        shape.append((field, op))
        if op in {"in", "has_any", "has_all"}:
            values = node["value"]
            if type(values) is not list or not 1 <= len(values) <= MAX_LIST_VALUES:
                raise QueryRejected("query_budget_exceeded", limit="request")
            node["value"] = sorted(set(literal(field, value) for value in values))
        elif op == "state_is":
            if type(node["value"]) is not str or node["value"] not in {"known", "unknown", "expired", "unavailable"}:
                raise QueryRejected("invalid_query")
        elif op == "contains_casefold_v1":
            node["value"] = search_literal(literal(field, node["value"]))
        elif op == "within_last_hours_v1":
            node["value"] = window_hours(node["value"])
        elif op not in {"is_known", "is_unknown"}:
            node["value"] = literal(field, node["value"])
    for field in fields:
        _field(field, context, source.id)
        if any((kind,field) in context.disabled_type_fields for kind in obj.get("video_types",[])):
            raise QueryRejected("field_disabled")
    if set(SOURCE.stream_dependencies if obj.get("video_types") == ["stream"] else ()) & context.disabled_dependencies:
        raise QueryRejected("dependency_disabled")
    # Omit absent unary values again: the wire contract forbids explicit null.
    stack = [obj["where"]]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if "field" in node and node["op"] in {"is_known", "is_unknown"}:
            node.pop("value", None)
        elif "field" not in node:
            stack.extend(node.get("all", node.get("any", [node.get("not")])))
    encoded = canonical(obj)
    # Position, the ordered ID list and its slices belong to one query: a slice
    # continues the list its first page returned.
    identity = {**obj, "page": {key: value for key, value in obj["page"].items()
                                if key not in {"after", "anchor_id", "order", "ids"}}}
    shape_hash = hashlib.sha256(canonical({"scope": request.scope.kind,
        "source":source.id, "type": request.video_types, "fields": sorted(fields), "where": shape,
        "sort": obj["order_by"]})).hexdigest()
    return ValidatedQuery(encoded, hashlib.sha256(canonical(identity)).hexdigest(),
                          context.fingerprint(), tuple(sorted(fields)), shape_hash)
