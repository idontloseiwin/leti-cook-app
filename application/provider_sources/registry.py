"""Load and verify the registry of supported provider methods, fields and states."""
from __future__ import annotations

from dataclasses import dataclass
import functools
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

REGISTRY_DIR = Path(__file__).resolve().parent / "registry"
STATE_NAMES = ("described", "request_supported", "parse_supported", "live_verified",
               "storage_supported", "query_supported", "ui_supported")
# Context names a parameter or part may require; partner/developer are never supported here.
OWNER = "owner_oauth"
UNSUPPORTED_CONTEXTS = ("partner", "developer", "special_scope")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*\Z")


class RegistryError(RuntimeError):
    """The shipped registry is damaged or inconsistent; nothing may be planned from it."""


@dataclass(frozen=True)
class Parameter:
    name: str
    type: str
    required: bool
    repeated: bool
    minimum: int | None
    maximum: int | None
    default: str | None
    allowed: tuple[str, ...]
    values: tuple[str, ...]
    not_with: tuple[str, ...]
    only_with: tuple[str, ...]
    context: str | None
    deprecated: bool
    discovery_only: bool
    doc_only: bool
    standard: bool
    identity: str | None
    max_items: int | None
    value_format: str | None = None      # plan.FORMATS: a checked text shape (date, timestamp, language, ...)


@dataclass(frozen=True)
class Quota:
    units: int | None
    bucket: str
    status: str


@dataclass(frozen=True)
class Pagination:
    style: str
    token_param: str | None
    next_field: str | None
    page_size_param: str | None
    minimum: int | None
    maximum: int | None
    default: int | None


@dataclass(frozen=True)
class Field:
    path: str
    part: str
    response_path: str
    json_type: str
    format: str | None
    value_kind: str
    repeated: bool
    map: bool
    enum: tuple[str, ...]
    deprecated: bool
    reference_status: str
    personal_data: bool
    sensitive: bool
    absence: str
    roles: tuple[str, ...]
    owner_only: bool
    exposure: str
    normalizer: str | None
    note: str | None

    @property
    def row_field(self):
        return not self.part.startswith("_") or self.part in ("_resource", "_rows")

    def has_role(self, prefix):
        return any(role == prefix or role.startswith(prefix + ":") for role in self.roles)

    def role_value(self, prefix):
        for role in self.roles:
            if role.startswith(prefix + ":"):
                return role.split(":", 1)[1]
        return None


@dataclass(frozen=True)
class Method:
    id: str
    api: str
    family: str
    http_method: str
    host: str
    path: str
    ledger_endpoint_label: str
    auth_contexts: tuple[str, ...]
    owner_parts: tuple[str, ...]
    quota: Quota
    pagination: Pagination
    id_batch_param: str | None
    id_batch_max: int | None
    row_unit: str
    row_path: str | None
    envelope_kind: str | None
    envelope_kind_aliases: tuple[str, ...]
    parts: tuple[str, ...]
    exactly_one_of: tuple[str, ...]
    parameters: Mapping[str, Parameter]
    semantic_traps: tuple[str, ...]
    status: str
    reference: str | None
    oauth_scopes: tuple[str, ...]
    media: Mapping | None                  # media methods: documented content type and formats
    fields: Mapping[str, Field]            # row fields by path relative to the row
    envelope_fields: Mapping[str, Field]   # every other field by "<part>/<path>"
    states: Mapping[str, str]

    @property
    def endpoint(self):
        """Path relative to the API base of the shared transport."""
        prefix = {"data": "youtube/v3/", "analytics": "v2/", "reporting": "v1/"}[self.api]
        return self.path[len(prefix):] if self.path.startswith(prefix) else self.path

    @property
    def identity_field(self):
        for field in self.fields.values():
            if field.row_field and field.has_role("identity") and field.absence == "row_identity":
                return field
        return None

    def row_fields(self):
        return dict(self.fields)

    def envelope_paths(self, part):
        return [field.path for key, field in self.envelope_fields.items() if field.part == part]


@dataclass(frozen=True)
class Registry:
    revision: int
    methods: Mapping[str, Method]
    control_methods: frozenset
    buckets: Mapping[str, Mapping]
    state_values: Mapping[str, str]
    field_states: Mapping[str, Mapping[str, Mapping[str, str]]]


def _verified(name):
    path = REGISTRY_DIR / name
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        document = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=unique)
        body = {key: value for key, value in document.items() if key != "content_sha256"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
    except (OSError, ValueError, UnicodeError) as error:
        raise RegistryError("provider_registry_unreadable") from error
    if document.get("content_sha256") != digest:
        raise RegistryError("provider_registry_checksum_mismatch")
    return document


def _parameter_context(name, entry, auth):
    if entry.get("auth"):
        return entry["auth"]
    for context in UNSUPPORTED_CONTEXTS:
        if name in auth.get(context, ()):
            return context
    if name in auth.get(OWNER, ()) and name not in auth.get("public_key", ()):
        return OWNER
    return None


def _parameter(name, entry, auth):
    values = entry.get("documented_values") or entry.get("discovery_values") or ()
    def integer(value):
        return value if type(value) is int else None
    return Parameter(
        name=name, type=entry["type"], required=entry["required"], repeated=entry["repeated"],
        minimum=integer(entry.get("min")), maximum=integer(entry.get("max")), default=entry.get("default"),
        allowed=tuple(entry.get("allowed", ())), values=tuple(values),
        not_with=tuple(entry.get("not_with", ())), only_with=tuple(entry.get("only_with", ())),
        context=_parameter_context(name, entry, auth), deprecated=bool(entry.get("deprecated")),
        discovery_only=bool(entry.get("discovery_only")), doc_only=bool(entry.get("doc_only")),
        standard=bool(entry.get("standard")), identity=entry.get("identity"),
        max_items=integer(entry.get("max_items")), value_format=entry.get("value_format"))


def _field(path, part, entry):
    return Field(path=path, part=part, response_path=entry["response_path"], json_type=entry["json_type"],
                 format=entry["format"], value_kind=entry["value_kind"], repeated=entry["repeated"],
                 map=entry["map"], enum=tuple(entry["enum"]), deprecated=entry["deprecated"],
                 reference_status=entry["reference_status"], personal_data=entry["personal_data"],
                 sensitive=entry["sensitive"], absence=entry["absence"], roles=tuple(entry["roles"]),
                 owner_only=bool(entry.get("owner_only")), exposure=entry["exposure"],
                 normalizer=entry.get("normalizer"), note=entry.get("note"))


def _method(method_id, entry, fields, states):
    auth = entry["auth"]
    owner_parts = sorted({value.split("=", 1)[1] for value in auth.get(OWNER, ()) if value.startswith("part=")}
                         | {value for value in entry.get("owner_only", ()) if value in entry["parts"]})
    pagination = entry["pagination"]
    batch = entry.get("id_batch") or {}
    return Method(
        id=method_id, api=entry["api"], family=entry["family"], http_method=entry["http_method"],
        host=entry["host"], path=entry["path"], ledger_endpoint_label=entry["ledger_endpoint_label"],
        auth_contexts=tuple(entry["auth_contexts"]), owner_parts=tuple(owner_parts),
        quota=Quota(entry["quota"]["units"], entry["quota"]["bucket"], entry["quota"]["status"]),
        pagination=Pagination(pagination["style"], pagination.get("token_param"), pagination.get("next_field"),
                              pagination.get("page_size_param"), pagination.get("min"), pagination.get("max"),
                              pagination.get("default")),
        id_batch_param=batch.get("param"), id_batch_max=batch.get("max_ids"),
        row_unit=entry["row_unit"], row_path=entry.get("row_path"), envelope_kind=entry.get("envelope_kind"),
        envelope_kind_aliases=tuple(entry.get("envelope_kind_aliases", ())),
        parts=tuple(entry["parts"]), exactly_one_of=tuple((entry.get("filters") or {}).get("exactly_one_of", ())),
        parameters=MappingProxyType({name: _parameter(name, value, auth)
                                     for name, value in entry["parameters"].items()}),
        semantic_traps=tuple(entry["semantic_traps"]), status=entry["status"], reference=entry.get("reference"),
        oauth_scopes=tuple(entry.get("oauth_scopes_declared", ())),
        media=MappingProxyType(dict(entry["media"])) if entry.get("media") else None,
        fields=MappingProxyType({path: _field(path, part, value)
                                 for part, entries in fields["parts"].items() for path, value in entries.items()
                                 if not part.startswith("_") or part in ("_resource", "_rows")}),
        envelope_fields=MappingProxyType({f"{part}/{path}": _field(path, part, value)
                                          for part, entries in fields["parts"].items() for path, value in entries.items()
                                          if part.startswith("_") and part not in ("_resource", "_rows")}),
        states=MappingProxyType({name: states[name]["value"] for name in STATE_NAMES}))


@functools.lru_cache(maxsize=1)
def load_registry():
    methods_doc = _verified("youtube.methods.json")
    fields_doc = _verified("youtube.fields.json")
    states_doc = _verified("youtube.states.json")
    revision = methods_doc.get("registry_revision")
    if type(revision) is not int or revision < 1 or fields_doc.get("registry_revision") != revision:
        raise RegistryError("provider_registry_revision_mismatch")
    if set(methods_doc["read_methods"]) != set(fields_doc["methods"]) or set(states_doc["methods"]) != set(fields_doc["methods"]):
        raise RegistryError("provider_registry_methods_mismatch")
    methods = {method_id: _method(method_id, entry, fields_doc["methods"][method_id], states_doc["methods"][method_id])
               for method_id, entry in methods_doc["read_methods"].items()}
    field_states = {method_id: MappingProxyType({path: MappingProxyType({name: row[name]["value"] for name in STATE_NAMES})
                                                 for path, row in rows.items()})
                    for method_id, rows in states_doc["fields"].items()}
    return Registry(revision=revision, methods=MappingProxyType(methods),
                    control_methods=frozenset(methods_doc["control_methods"]),
                    buckets=MappingProxyType(methods_doc["buckets"]),
                    state_values=MappingProxyType(states_doc["values"]),
                    field_states=MappingProxyType(field_states))
