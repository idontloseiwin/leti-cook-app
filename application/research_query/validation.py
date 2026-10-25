"""Bounded, pure query admission. A caller must bind trusted access first.

No SQL, I/O, environment settings or client-provided grants. The SQL repository
accepts only the immutable plan produced here, never a request dictionary.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata

from pydantic import ValidationError

from .contracts import (QueryRequest, ERROR_STATUS, MAX_BODY_BYTES, MAX_DEPTH,
                        MAX_LEAVES, MAX_LIST_VALUES, uint_decimal)
from .registry import DEPENDENCIES, FIELDS, OPERATORS, SOURCE, REGISTRY_VERSION, UNICODE_VERSION


class QueryRejected(ValueError):
    """Fixed public code/path only. Never retain input, SQL or provider details."""
    def __init__(self, code, path=None):
        if code not in ERROR_STATUS:
            raise ValueError("unknown_query_error")
        self.code, self.path, self.status = code, path, ERROR_STATUS[code]
        super().__init__(code)

    def payload(self):
        return {"code": self.code, "path": self.path}


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
    allowed_fields: frozenset[str] = frozenset(FIELDS)
    allowed_operators: frozenset[str] = frozenset(OPERATORS)
    disabled_dependencies: frozenset[str] = frozenset()
    preferences_revision: str = "existing-v1"
    type_revision: str = "canonical-membership-v1"
    lifecycle_revision: str = "public-696h-v1"

    def __post_init__(self):
        for name in ("workspace_id", "access_revision", "preferences_revision",
                     "type_revision", "lifecycle_revision"):
            value = getattr(self, name)
            if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
                raise ValueError("invalid_trusted_query_context")
        if type(self.enabled) is not bool or any(type(getattr(self, n)) is not frozenset
                for n in ("allowed_fields", "allowed_operators", "disabled_dependencies")):
            raise ValueError("invalid_trusted_query_context")
        for values, registry in ((self.allowed_fields, FIELDS), (self.allowed_operators, OPERATORS),
                                 (self.disabled_dependencies, DEPENDENCIES)):
            if any(type(value) is not str or value not in registry for value in values):
                raise ValueError("invalid_trusted_query_context")

    def fingerprint(self):
        return hashlib.sha256(canonical({
            "workspace": self.workspace_id, "access": self.access_revision,
            "enabled": self.enabled, "fields": sorted(self.allowed_fields),
            "operators": sorted(self.allowed_operators),
            "disabled": sorted(self.disabled_dependencies),
            "preferences": self.preferences_revision, "type": self.type_revision,
            "lifecycle": self.lifecycle_revision, "registry": REGISTRY_VERSION,
            "unicode": UNICODE_VERSION,
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
                raise QueryRejected("query_budget_exceeded")
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
    value = _text(value)
    kind = definition.value_type
    if kind == "uint_decimal":
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
    return value


def _field(field, context):
    if field not in FIELDS:
        raise QueryRejected("unsupported_field")
    if field not in context.allowed_fields:
        raise QueryRejected("field_disabled")
    if set(FIELDS[field].dependencies) & context.disabled_dependencies:
        raise QueryRejected("dependency_disabled")


def capabilities(context):
    """The same definitions and effective restrictions used by admission."""
    if not context.enabled:
        raise QueryRejected("policy_denied")
    values = []
    for field in FIELDS.values():
        try:
            _field(field.id, context)
        except QueryRejected:
            continue
        values.append({"id": field.id, "value_type": field.value_type,
                       "nullable": field.nullable, "sortable": field.sortable,
                       "operators": [op for op in field.operators if op in context.allowed_operators]})
    types = [] if set(SOURCE.admission_dependencies) & context.disabled_dependencies else list(SOURCE.video_types)
    if set(SOURCE.stream_dependencies) & context.disabled_dependencies and "stream" in types:
        types.remove("stream")
    return {"query_version": 1, "registry_version": REGISTRY_VERSION,
            "source_id": SOURCE.id, "video_types": types,
            "max_video_types": 1, "fields": values, "consistency": "current"}


def validate(raw: bytes, context: AccessContext) -> ValidatedQuery:
    if not isinstance(context, AccessContext) or not context.enabled:
        raise QueryRejected("policy_denied")
    if unicodedata.unidata_version != UNICODE_VERSION:
        raise QueryRejected("backend_unavailable")
    if type(raw) is not bytes:
        raise QueryRejected("invalid_query")
    if len(raw) > MAX_BODY_BYTES:
        raise QueryRejected("query_budget_exceeded")
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
    if obj.get("source") != {"id": SOURCE.id}:
        raise QueryRejected("unsupported_source")
    if obj.get("video_types") not in (["long"], ["short"], ["stream"]):
        raise QueryRejected("unsupported_video_types")
    stack, leaves = [(obj.get("where"), 1)], 0
    while stack:
        node, depth = stack.pop()
        if node is None:
            continue
        if depth > MAX_DEPTH:
            raise QueryRejected("query_budget_exceeded", "where")
        if not isinstance(node, dict):
            raise QueryRejected("invalid_query", "where")
        if "field" in node:
            if type(node.get("op")) is str and node["op"] not in OPERATORS:
                raise QueryRejected("unsupported_operator", "where")
            leaves += 1
            if leaves > MAX_LEAVES:
                raise QueryRejected("query_budget_exceeded", "where")
        else:
            children = node.get("all", node.get("any", [node.get("not")]))
            if not isinstance(children, list) or len(children) > MAX_LEAVES:
                raise QueryRejected("query_budget_exceeded", "where")
            stack.extend((child, depth + 1) for child in children)
    try:
        request = QueryRequest.model_validate(obj)
    except (ValidationError, ValueError, RecursionError):
        raise QueryRejected("invalid_query") from None
    obj = request.model_dump(mode="json", by_alias=True)
    if request.page.after is not None and re.fullmatch(r"[A-Za-z0-9_-]+", request.page.after) is None:
        raise QueryRejected("cursor_expired")
    fields = set(obj["select"]) | set(obj["coverage"]["required_fields"])
    for values in (obj["select"], obj["coverage"]["required_fields"], [v["field"] for v in obj["order_by"]]):
        if len(values) != len(set(values)):
            raise QueryRejected("invalid_query")
    for item in obj["order_by"]:
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
        _field(field, context)
        fields.add(field)
        if op not in FIELDS[field].operators:
            raise QueryRejected("unsupported_operator")
        if op not in context.allowed_operators:
            raise QueryRejected("policy_denied")
        shape.append((field, op))
        if op == "in":
            values = node["value"]
            if type(values) is not list or not 1 <= len(values) <= MAX_LIST_VALUES:
                raise QueryRejected("query_budget_exceeded")
            node["value"] = sorted(set(literal(field, value) for value in values))
        elif op == "state_is":
            if type(node["value"]) is not str or node["value"] not in {"known", "unknown", "expired", "unavailable"}:
                raise QueryRejected("invalid_query")
        elif op not in {"is_known", "is_unknown"}:
            node["value"] = literal(field, node["value"])
    for field in fields:
        _field(field, context)
    if set(SOURCE.stream_dependencies if obj["video_types"] == ["stream"] else ()) & context.disabled_dependencies:
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
    identity = {**obj, "page": {"size": obj["page"]["size"]}}
    shape_hash = hashlib.sha256(canonical({"scope": request.scope.kind,
        "type": request.video_types, "fields": sorted(fields), "where": shape,
        "sort": obj["order_by"]})).hexdigest()
    return ValidatedQuery(encoded, hashlib.sha256(canonical(identity)).hexdigest(),
                          context.fingerprint(), tuple(sorted(fields)), shape_hash)
