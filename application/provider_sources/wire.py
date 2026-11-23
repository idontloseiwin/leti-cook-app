"""Strict JSON documents of the acquisition endpoints: no extra keys, no coercion.

A request document names one registry method, its parameters, the selection,
an optional lower budget and the context. There is no raw ``fields`` mask, no
URL and no provider body: execute re-plans from the same document and must
reproduce the previewed fingerprint and the confirmed worst-case cost.
"""
from __future__ import annotations

import json
import re
from types import MappingProxyType

from .credentials import PublicKeyContext
from .errors import PlanRejected
from .plan import AcquisitionRequest, Budget, ceilings
from .selection import FieldSelection

MAX_BODY_BYTES = 64 * 1024
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._:]{0,127}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_PART = re.compile(r"[A-Za-z][A-Za-z0-9]{0,63}\Z")
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9._{}\[\]]{0,159}\Z")


def load_document(raw):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BODY_BYTES:
        raise PlanRejected("request_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PlanRejected("request_invalid")
            result[key] = value
        return result
    def constant(_):
        raise ValueError("non_finite_number")
    try:
        document = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except PlanRejected:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise PlanRejected("request_invalid") from None
    if not isinstance(document, dict):
        raise PlanRejected("request_invalid")
    return document


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise PlanRejected("request_invalid")


def _names(value, limit, pattern):
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(item, str) and pattern.fullmatch(item)
                                                                    for item in value):
        raise PlanRejected("selection_invalid")
    return frozenset(value)


def _budget(value, owner=False):
    """No budget: the ceilings of the context (owner acquisitions have their own, plan.OWNER_LIMITS)."""
    limits = ceilings(owner)
    if value is None:
        return None
    _keys(value, (), ("max_requests", "max_units", "max_rows", "deadline_seconds"))
    units = value.get("max_units", dict(limits.max_units))
    if not isinstance(units, dict) or not all(isinstance(key, str) for key in units):
        raise PlanRejected("budget_invalid", "max_units")
    return Budget(max_requests=value.get("max_requests", limits.max_requests),
                  max_units=MappingProxyType(dict(units)), max_rows=value.get("max_rows", limits.max_rows),
                  max_response_bytes=limits.max_response_bytes,
                  deadline_seconds=value.get("deadline_seconds", limits.deadline_seconds))


def acquisition_request(document, *, owner_context=None):
    """``owner_context`` is the installation's owner binding context, or None when no owner sign-in exists."""
    _keys(document, ("version", "method", "parameters", "selection"), ("budget", "context"))
    if document["version"] != 1 or type(document["version"]) is not int:
        raise PlanRejected("request_version_unsupported")
    if not isinstance(document["method"], str) or _NAME.fullmatch(document["method"]) is None:
        raise PlanRejected("method_unknown")
    if not isinstance(document["parameters"], dict):
        raise PlanRejected("parameters_invalid")
    selection = document["selection"]
    _keys(selection, ("parts",), ("fields",))
    context = document.get("context", "public_key")
    if context == "public_key":
        auth = PublicKeyContext()
    elif context == "owner":
        if owner_context is None:
            from .credentials import OwnerAuthUnavailable
            raise OwnerAuthUnavailable()
        auth = owner_context
    else:
        raise PlanRejected("context_invalid")
    return AcquisitionRequest(method_id=document["method"], parameters=MappingProxyType(dict(document["parameters"])),
                              selection=FieldSelection(parts=_names(selection["parts"], 32, _PART),
                                                       fields=_names(selection.get("fields", []), 256, _FIELD)),
                              auth=auth, budget=_budget(document.get("budget"), owner=context == "owner"))


def execute_document(document, *, owner_context=None):
    _keys(document, ("version", "request", "plan_fingerprint", "confirmed_cost"), ("continuation",))
    if document["version"] != 1 or type(document["version"]) is not int:
        raise PlanRejected("request_version_unsupported")
    fingerprint, cost = document["plan_fingerprint"], document["confirmed_cost"]
    if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise PlanRejected("plan_fingerprint_invalid")
    if not isinstance(cost, dict) or not all(isinstance(key, str) and type(value) is int for key, value in cost.items()):
        raise PlanRejected("confirmed_cost_invalid")
    continuation = document.get("continuation")
    if continuation is not None and not isinstance(continuation, str):
        raise PlanRejected("continuation_invalid")
    return acquisition_request(document["request"], owner_context=owner_context), fingerprint, dict(cost), continuation
