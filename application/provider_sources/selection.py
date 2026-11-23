"""Field selection: requested parts and fields -> the ``part`` value and the ``fields`` mask.

A client never sends a raw ``fields`` mask. Without explicit fields the selection is
every ``default``-exposed field of the requested parts; ``on_request`` fields must be
named; ``never`` fields (envelope, deprecated, secret) cannot be selected. Owner-only
fields and parts need an owner context. The row identity is always selected.

Methods without a ``part`` parameter (videos.getRating, Analytics groups, Reporting)
take no parts: their fields are chosen directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import PlanRejected
from .families import policy

MAX_FIELDS = 256


@dataclass(frozen=True)
class FieldSelection:
    parts: frozenset
    fields: frozenset = frozenset()


@dataclass(frozen=True)
class ResolvedSelection:
    parts: tuple
    fields: tuple
    mask: str

    @property
    def part_param(self):
        return ",".join(self.parts)


def _segments(path):
    """Mask segments of a registry path: arrays select the list, maps the whole map."""
    result = []
    for segment in path.split("."):
        if segment == "{key}":
            break
        result.append(segment.removesuffix("[]"))
    return result


def _render(tree):
    items = []
    for name in sorted(tree):
        child = tree[name]
        items.append(name if child is None else f"{name}({_render(child)})")
    return ",".join(items)


def _insert(tree, segments):
    node = tree
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment in node and node[segment] is None:
            return  # the whole container is already selected
        if last:
            node[segment] = None
        else:
            node = node.setdefault(segment, {})


def fields_mask(method, row_paths):
    """The partial-response mask: envelope keys the parser needs plus the selected row fields."""
    top = {}
    if method.envelope_kind:
        top["kind"] = None
    if method.pagination.style == "pageToken":
        top["nextPageToken"] = None
    paging = {name.split(".", 1)[1]: None for name in method.envelope_paths("_envelope")
              if name.startswith("pageInfo.")}
    if paging:
        top["pageInfo"] = paging
    for extra in policy.ENVELOPE_EXTRAS.get(method.id, ()):
        top[extra] = None
    rows = {}
    for path in row_paths:
        _insert(rows, _segments(path))
    container = (method.row_path or "").removesuffix("[]")
    if not container or "." in container:
        raise PlanRejected("selection_not_supported")
    top[container] = rows
    return _render(top)


def parse_supported(field):
    """Every described value is parsed except raw bytes and untyped objects.

    Arrays of objects (``replies.comments[].id``) are parsed element by element
    (``parse._element_values``); described them only.
    """
    return field.value_kind not in ("base64_bytes", "object", "any")


def _resolve_partless(method, selection, *, owner):
    """Methods without parts: fields are named directly; the mask is sent only to the Data API."""
    if selection.parts:
        raise PlanRejected("parts_not_supported")
    row_fields = method.row_fields()
    if len(selection.fields) > MAX_FIELDS:
        raise PlanRejected("fields_too_many")
    chosen = set()
    for path in sorted(selection.fields):
        field = row_fields.get(path)
        if field is None:
            raise PlanRejected("field_unknown", path if path in method.fields else None)
        if field.exposure == "never":
            raise PlanRejected("field_not_selectable", path)
        if field.owner_only and not owner:
            raise PlanRejected("field_owner_only", path)
        if not parse_supported(field):
            raise PlanRejected("field_not_supported", path)
        chosen.add(path)
    if not chosen:
        chosen = {path for path, field in row_fields.items()
                  if field.row_field and field.exposure == "default" and (owner or not field.owner_only)
                  and parse_supported(field)}
    identity = method.identity_field
    if identity is not None:
        chosen.add(identity.path)
    if not chosen:
        raise PlanRejected("fields_empty")
    ordered = tuple(sorted(chosen))
    mask = fields_mask(method, ordered) if method.api == "data" else ""
    return ResolvedSelection(parts=(), fields=ordered, mask=mask)


def resolve(method, selection, *, owner):
    if not isinstance(selection, FieldSelection):
        raise PlanRejected("selection_invalid")
    if "part" not in method.parameters:
        return _resolve_partless(method, selection, owner=owner)
    allowed = set(method.parameters["part"].allowed)
    parts = set(selection.parts)
    if not parts or not allowed:
        raise PlanRejected("parts_required")
    for part in sorted(parts):
        if part not in allowed:
            raise PlanRejected("part_unknown")  # never echo an unknown client value
        if part in policy.SPECIAL_SCOPE_PARTS.get(method.id, ()):
            raise PlanRejected("context_not_supported", part)
        if part in method.owner_parts and not owner:
            raise PlanRejected("part_owner_only", part)
        code = policy.FORBIDDEN_PARTS.get(method.id, {}).get(part)
        if code:
            raise PlanRejected(code, part)
    identity = method.identity_field
    if identity is not None and identity.part in allowed:
        parts.add(identity.part)
    row_fields = method.row_fields()
    returned = parts | policy.ALWAYS_RETURNED_PARTS.get(method.id, frozenset())
    chosen = set()
    if len(selection.fields) > MAX_FIELDS:
        raise PlanRejected("fields_too_many")
    for path in sorted(selection.fields):
        field = row_fields.get(path)
        if field is None:
            raise PlanRejected("field_unknown", path if path in method.fields else None)
        if field.part not in returned:
            raise PlanRejected("field_part_not_requested", path)
        if field.exposure == "never":
            raise PlanRejected("field_not_selectable", path)
        if field.owner_only and not owner:
            raise PlanRejected("field_owner_only", path)
        if not parse_supported(field):
            raise PlanRejected("field_not_supported", path)
        chosen.add(path)
    if not chosen:
        chosen = {path for path, field in row_fields.items()
                  if field.part in returned and field.exposure == "default" and (owner or not field.owner_only)
                  and parse_supported(field)}
    if identity is not None:
        chosen.add(identity.path)
    for part in policy.ALWAYS_RETURNED_PARTS.get(method.id, ()):
        # The row identity lives there: the mask must keep it even when only other fields are named.
        chosen.update(path for path, field in row_fields.items() if field.part == part and field.has_role("identity")
                      or field.part == part and field.has_role("row_kind"))
    if not chosen:
        raise PlanRejected("fields_empty")
    ordered = tuple(sorted(chosen))
    return ResolvedSelection(parts=tuple(sorted(parts)), fields=ordered, mask=fields_mask(method, ordered))
