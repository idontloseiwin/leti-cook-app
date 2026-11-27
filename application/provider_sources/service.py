"""Process-level one-off acquisition service behind the /sources routes.

* One acquisition at a time per installation (the backend runs one process).
* Preview = ``plan_request``: no network, no quota; it also shows today's use of
  the daily cap read from the quota ledger.
* Execute re-plans the same document, requires the previewed fingerprint and
  the confirmed worst-case cost, checks HOT readiness and the daily cap, then
  runs ``acquire``. The answer is the result JSON (at most 2 MiB); nothing is
  stored or cached, and no value, ID, token or parameter is logged.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import threading
import time

from .acquire import OUTPUT_LIMIT_BYTES, acquire
from .continuation import ContinuationCodec
from .credentials import OwnerAuthUnavailable, OwnerContext, resolve_credential
from .daily_cap import (CAPPED_BUCKET, DAILY_GENERAL_CAP_UNITS, DAILY_SEARCH_CAP_UNITS, SEARCH_BUCKET, CapSessions,
                        CapUnavailable, DailyCap, next_reset)
from .errors import PlanRejected
from .families import policy
from .plan import LIMITS, OWNER_LIMITS, plan_request
from .registry import STATE_NAMES, load_registry
from .results import to_wire
from .selection import parse_supported
from .wire import acquisition_request, execute_document, load_document


class ServiceRefused(Exception):
    def __init__(self, code, status, *, retry_after=None, subject=None):
        super().__init__(code)
        self.code, self.status, self.retry_after, self.subject = code, status, retry_after, subject

    def payload(self):
        value = {"code": self.code}
        if self.subject is not None:
            value["subject"] = self.subject
        return value


def utc_now():
    return datetime.now(timezone.utc)


def limits():
    return {"max_requests": LIMITS.max_requests, "max_units": dict(LIMITS.max_units), "max_rows": LIMITS.max_rows,
            "max_response_bytes": LIMITS.max_response_bytes, "deadline_seconds": LIMITS.deadline_seconds,
            "output_limit_bytes": OUTPUT_LIMIT_BYTES, "owner_max_units": dict(OWNER_LIMITS.max_units),
            "daily_cap_units": {CAPPED_BUCKET: DAILY_GENERAL_CAP_UNITS, SEARCH_BUCKET: DAILY_SEARCH_CAP_UNITS}}


class AcquisitionService:
    def __init__(self, *, attempt_recorder, ledger_reader, api_key=None, owner_provider=None, send=None,
                 clock=utc_now, registry_loader=load_registry, monotonic=time.monotonic):
        self.attempt_recorder, self.api_key = attempt_recorder, api_key
        self.owner_provider, self.send = owner_provider, send
        self.clock, self.registry_loader, self.monotonic = clock, registry_loader, monotonic
        self.cap = DailyCap(ledger_reader, clock=clock)
        # ``ledger_reader(day, quota_bucket=...)`` reads the same phase in another bucket.
        self.search_cap = DailyCap(lambda day: ledger_reader(day, quota_bucket=SEARCH_BUCKET),
                                   limit=DAILY_SEARCH_CAP_UNITS, clock=clock, bucket=SEARCH_BUCKET)
        self.caps = {CAPPED_BUCKET: self.cap, SEARCH_BUCKET: self.search_cap}
        self.codec = ContinuationCodec()
        self._busy = threading.Lock()

    def _owner_context(self):
        if self.owner_provider is None:
            return None
        return OwnerContext(self.owner_provider.binding())

    # ------------------------------------------------------------ reads
    def registry_projection(self, method_id=None):
        registry = self.registry_loader()
        def method_view(method):
            return {"id": method.id, "api": method.api, "family": method.family, "row_unit": method.row_unit,
                    "auth_contexts": list(method.auth_contexts), "owner_parts": list(method.owner_parts),
                    "quota": {"units": method.quota.units, "bucket": method.quota.bucket, "status": method.quota.status},
                    "pagination": method.pagination.style, "parts": list(method.parts),
                    "executable": method.id in policy.EXECUTABLE, "states": dict(method.states),
                    "semantic_traps": list(method.semantic_traps), "reference": method.reference}
        if method_id is None:
            return {"registry_revision": registry.revision, "state_names": list(STATE_NAMES),
                    "state_values": dict(registry.state_values), "limits": limits(),
                    "methods": [method_view(method) for _, method in sorted(registry.methods.items())]}
        method = registry.methods.get(method_id)
        if method is None:
            raise ServiceRefused("method_unknown", 404)
        view = method_view(method)
        view["parameters"] = [{"name": p.name, "type": p.type, "required": p.required, "repeated": p.repeated,
                               "min": p.minimum, "max": p.maximum, "values": list(p.values or p.allowed),
                               "context": p.context, "deprecated": p.deprecated, "discovery_only": p.discovery_only,
                               "identity": p.identity}
                              for _, p in sorted(method.parameters.items()) if not p.standard]
        states = registry.field_states.get(method.id, {})
        view["fields"] = [{"path": f.path, "part": f.part, "json_type": f.json_type, "value_kind": f.value_kind,
                           "repeated": f.repeated, "map": f.map, "enum": list(f.enum), "deprecated": f.deprecated,
                           "absence": f.absence, "roles": list(f.roles), "exposure": f.exposure,
                           "owner_only": f.owner_only, "personal_data": f.personal_data,
                           "reference_status": f.reference_status,
                           "selectable": f.row_field and f.exposure != "never" and parse_supported(f),
                           **({"states": dict(states[f.path])} if f.path in states else {})}
                          for _, f in sorted(method.fields.items())]
        view["envelope_fields"] = [{"path": f.path, "part": f.part, "json_type": f.json_type, "absence": f.absence,
                                    "roles": list(f.roles), "reference_status": f.reference_status}
                                   for _, f in sorted(method.envelope_fields.items())]
        return view

    def daily_caps(self):
        return {bucket: cap.reading() for bucket, cap in sorted(self.caps.items())}

    def status(self):
        return {"daily_cap": self.cap.reading(), "daily_caps": self.daily_caps(), "limits": limits(),
                "active": self._busy.locked()}

    # ------------------------------------------------------------ preview / execute
    def _plan(self, request):
        try:
            return plan_request(request, self.registry_loader())
        except PlanRejected as error:
            raise ServiceRefused(error.code, 422, subject=error.subject) from None

    def preview(self, raw):
        try:
            request = acquisition_request(load_document(raw), owner_context=self._owner_context())
        except PlanRejected as error:
            raise ServiceRefused(error.code, 422, subject=error.subject) from None
        except OwnerAuthUnavailable as error:
            raise ServiceRefused(error.code, 409) from None
        plan = self._plan(request)
        return dict(plan.preview(), daily_cap=self.cap.reading(), daily_caps=self.daily_caps(), network_used=False)

    def execute(self, raw, *, cancelled):
        try:
            request, fingerprint, confirmed, continuation = execute_document(load_document(raw),
                                                                             owner_context=self._owner_context())
        except PlanRejected as error:
            raise ServiceRefused(error.code, 422, subject=error.subject) from None
        except OwnerAuthUnavailable as error:
            raise ServiceRefused(error.code, 409) from None
        plan = self._plan(request)
        if not plan.executable:
            raise ServiceRefused("method_not_executable", 422)
        if fingerprint != plan.fingerprint:
            raise ServiceRefused("plan_changed", 409)
        if confirmed != dict(plan.worst_cost):
            raise ServiceRefused("cost_not_confirmed", 409)
        with self.admit(plan.worst_cost) as session:
            return self.run_plan(request, plan, cancelled, session, self.monotonic() + plan.deadline_seconds, continuation)

    @contextmanager
    def admit(self, worst_cost):
        """Share one admission and the daily cap sessions across a bounded owner action."""
        if not self._busy.acquire(blocking=False):
            raise ServiceRefused("acquisition_busy", 409)
        try:
            from hot_runtime import require_hot_runtime_ready
            require_hot_runtime_ready(operation="provider")   # pauses and update holds, before any work
            # The general cap is always read (fail closed); a capped bucket of the plan as well.
            try:
                sessions = {bucket: cap.session() for bucket, cap in self.caps.items()
                            if bucket == CAPPED_BUCKET or bucket in worst_cost}
            except CapUnavailable:
                raise ServiceRefused("daily_cap_unavailable", 503) from None
            for bucket, session in sessions.items():
                if worst_cost.get(bucket, 0) > session.remaining:
                    seconds = max(1, int((next_reset(self.clock()) - self.clock()).total_seconds()))
                    raise ServiceRefused("daily_cap_reached", 429, retry_after=seconds, subject=bucket)
            yield CapSessions(sessions.values())
        finally:
            self._busy.release()

    def run_plan(self, request, plan, cancelled, session, deadline, continuation=None):
        try:
            credential = resolve_credential(request.auth, owner_provider=self.owner_provider,
                                            scopes=frozenset(plan.owner_scopes), deadline=deadline)
        except OwnerAuthUnavailable as error:
            raise ServiceRefused(error.code, 409) from None
        options = {} if self.send is None else {"send": self.send}
        try:
            result = acquire(plan, registry=self.registry_loader(), credential=credential, cancelled=cancelled,
                             deadline=deadline, attempt_recorder=self.attempt_recorder, api_key=self.api_key,
                             cap=session, codec=self.codec, continuation=continuation, clock=self.clock,
                             monotonic=self.monotonic, **options)
        except PlanRejected as error:
            raise ServiceRefused(error.code, 409 if error.code.startswith("continuation") else 422) from None
        return to_wire(result, max_bytes=OUTPUT_LIMIT_BYTES)
