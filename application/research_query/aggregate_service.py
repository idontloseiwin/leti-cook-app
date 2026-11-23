"""Bounded orchestration of registered whole-population operations."""
from contextlib import nullcontext
from dataclasses import replace
from decimal import InvalidOperation
import time

from .aggregate import validate_operation
from .operation_kinds import OPERATION_KINDS
from .service import instant, iso, coverage_from
from .validation import QueryRejected, canonical, check_budget
from .registry import CHANNEL_SOURCE_ID, REGISTRY_VERSION
from .observation_limits import change_age_limit

DEADLINE_SECONDS = 8  # Validation, admission wait, SQL and output together.
MAX_OUTPUT_BYTES = 512*1024  # Canonical JSON of the whole response.


def execute_operation(service, raw, *, cancelled=lambda: False):
    deadline = time.monotonic() + DEADLINE_SECONDS
    context = service.context_provider()
    plan = validate_operation(raw, context)
    kind = OPERATION_KINDS[plan.operation]
    channel = plan.query.request.source.id == CHANNEL_SOURCE_ID
    if not channel:
        service.preparation_check()
    if kind.bind is not None:
        # Before the read slot: a bound frozen reference rechecks its scope in its own slot.
        plan = replace(plan, binding=kind.bind(service, plan, cancelled, deadline))
    slot = service.admission.enter(deadline=deadline, cancelled=cancelled) if service.admission else nullcontext()
    with slot:
        if service.context_provider().fingerprint() != context.fingerprint():
            raise QueryRejected('cursor_context_changed')
        if not channel:
            service.preparation_check()
        data = service.repository.read_operation(plan, deadline=deadline, cancelled=cancelled)
        try:
            coverage = coverage_from(data, plan.query.request)
            until = min(instant(data['valid_before']), instant(data.get('operation_valid_before', data['valid_before'])))
            if plan.binding is not None and plan.binding.valid_before is not None:
                until = min(until, instant(plan.binding.valid_before))
            result = data['result']
            if type(result) is not dict:
                raise ValueError()
            result = kind.check(result)
            if kind.turned_off is not None and context.disabled_video_types:
                result = kind.turned_off(result, context.disabled_video_types)
            bound = kind.lifetime(result) if kind.lifetime is not None else None
            if bound is not None:
                until = min(until, instant(bound))
            output = dict(version=1, operation=plan.operation, operation_fingerprint=plan.fingerprint,
                context=dict(source_id=plan.query.request.source.id, registry_version=REGISTRY_VERSION,
                    evaluated_at=iso(data['evaluated_at']), valid_before=iso(until),
                    preferences_revision=context.preferences_revision, consistency='current',
                    observation_age_limit_seconds=change_age_limit(plan.query)),
                coverage=coverage, result=result, provenance=kind.provenance(plan),
                warnings=['current_population_may_change'])
            if len(canonical(output)) > MAX_OUTPUT_BYTES:
                raise QueryRejected('query_budget_exceeded', limit='response')
        except QueryRejected:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, InvalidOperation):
            raise QueryRejected('backend_unavailable') from None
        service.repository.recheck_scope(plan.query, data['scope_revision'], deadline=deadline, cancelled=cancelled)
        if plan.binding is not None:
            plan.binding.finish()
        if service.context_provider().fingerprint() != context.fingerprint():
            raise QueryRejected('cursor_context_changed')
        if instant(service.clock()) >= until:
            raise QueryRejected('cursor_expired')
        check_budget(deadline, cancelled)
        return output
