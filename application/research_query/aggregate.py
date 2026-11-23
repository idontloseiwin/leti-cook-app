"""Closed operation requests on the existing query population and access binding.

These definitions are not grants. No provider calls, stored results or executable
user expressions. The SQL owner evaluates all matching rows before pagination.
"""
from dataclasses import dataclass
import hashlib
import json
from zoneinfo import ZoneInfoNotFoundError

from .operation_kinds import OPERATION_KINDS
from .validation import QueryRejected, canonical, validate, _json_budget, _pairs, _reject_constant
# Names kept for existing importers; each operation's own module owns them.
from .operation_kinds.kind import round_summary
from .operation_kinds.summary import ID as SUMMARY, SUMMARY_FIELDS, METRICS
from .operation_kinds.calendar import ID as CALENDAR
from .operation_kinds.channel_history import ID as HISTORY


@dataclass(frozen=True)
class AggregatePlan:
    query: object
    operation: str
    parameters_json: bytes
    fingerprint: str
    # Set by the service from the kind's bind (operation_kinds.kind.Binding), never by a client.
    binding: object = None

    @property
    def parameters(self):
        return json.loads(self.parameters_json)


def operation_dependencies(operation, params, source, scope):
    """Field ids a registered operation reads; ValueError for anything it does not accept."""
    kind = OPERATION_KINDS.get(operation)
    if kind is None:
        raise ValueError()
    return kind.dependencies(params, source, scope)


def validate_operation(raw, context):
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise QueryRejected('query_budget_exceeded', limit='request')
    _json_budget(raw)
    try:
        obj = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if type(obj) is not dict or set(obj) != {'version', 'operation', 'query', 'parameters'} or type(obj['version']) is not int or obj['version'] != 1:
            raise ValueError()
        operation, params = obj['operation'], obj['parameters']
        if type(operation) is not str or operation not in OPERATION_KINDS or type(params) is not dict:
            raise ValueError()
        if operation not in context.allowed_operations:
            raise QueryRejected('policy_denied')
        base = validate(canonical(obj['query']), context)
        q = json.loads(base.encoded)
        if q['page'].get('after') is not None:
            raise ValueError()
        fields = operation_dependencies(operation, params, q['source']['id'], q['scope']['kind'])
        if not context.allow_frozen and params.get('population', {}).get('kind') == 'frozen':
            raise QueryRejected('policy_denied', 'parameters.population')
        # Validate every operation dependency through the same field registry.
        # Preserve the original predicates/required-field rules, not the page.
        q['select'] = list(dict.fromkeys([*q['select'], *fields]))
        plan = validate(canonical(q), context)
        normalized = {**obj, 'query': json.loads(base.encoded)}
        return AggregatePlan(plan, operation, canonical(params), hashlib.sha256(canonical(normalized)).hexdigest())
    except QueryRejected:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, ZoneInfoNotFoundError):
        raise QueryRejected('invalid_query') from None
