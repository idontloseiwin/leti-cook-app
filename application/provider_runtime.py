"""Process composition for the installation-wide provider gate.

The backend is the sole initializer. A supervised child opens its captured gate;
a standalone provider command joins the existing gate under its guard. Neither
a request nor another process can silently create another admission owner.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import os
from pathlib import Path
import threading

from provider_admission import AdmissionConfig, AdmissionGate, AdmissionUnavailable, KINDS

SNAPSHOT_ENV = 'LIC_PROVIDER_ADMISSION_SNAPSHOT'
_scope = ContextVar('provider_request_scope', default=('interactive', None))
_deadline = ContextVar('provider_request_deadline', default=None)
_runtime = None
_guard = threading.Lock()


class ProviderRuntime:
    def __init__(self, gate):
        self.gate = gate
        self.stop = threading.Event()
        self.pid = os.getpid()

    def environment(self):
        value = self.gate.config
        return {SNAPSHOT_ENV: json.dumps({
            'installation_id': value.installation_id, 'generation': value.generation,
            'boot_id': value.boot_id, 'concurrency': value.concurrency,
        }, separators=(',', ':')), 'LIC_YOUTUBE_REQUEST_CONCURRENCY': str(value.concurrency)}

    def close(self):
        self.stop.set()
        self.gate._notify()


class _UnregulatedRequest:
    """Preserve cancellation for profiles which do not own this shared gate."""
    def __init__(self, cancel):
        self.cancel = cancel

    def is_set(self):
        return self.cancel is not None and self.cancel.is_set()

    def observe(self, *, success=False, rate_seconds=None):
        pass


class _Cancelled:
    def __init__(self, runtime, *actions):
        self.runtime, self.actions = runtime, actions
        self.permit = None

    def is_set(self):
        return self.runtime.stop.is_set() or any(action is not None and action.is_set() for action in self.actions)

    def observe(self, *, success=False, rate_seconds=None):
        if self.permit is None:
            raise AdmissionUnavailable('provider_admission_outcome_invalid')
        self.permit.observe(success=success, rate_seconds=rate_seconds)


def _directory(environ):
    value = environ.get('LIC_AUTH_DATABASE')
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise AdmissionUnavailable('provider_admission_path_required')
    return Path(value).parent / 'provider-admission'


def _boot_id():
    try:
        with Path('/proc/sys/kernel/random/boot_id').open('r', encoding='ascii') as stream:
            value = stream.read(80).strip()
        if len(value) != 36:
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise AdmissionUnavailable('provider_admission_boot_identity_unavailable') from None


def _install(gate):
    global _runtime
    with _guard:
        if _runtime is not None:
            raise AdmissionUnavailable('provider_admission_already_installed')
        _runtime = ProviderRuntime(gate)
        return _runtime


def start_backend(environ, *, concurrency):
    gate = AdmissionGate.for_backend(_directory(environ),
        installation_id=environ.get('LIC_DATABASE_ID'), boot_id=_boot_id(),
        concurrency=concurrency)
    return _install(gate)


def start_command(environ):
    """A standalone provider command joins the existing backend's captured C."""
    gate = AdmissionGate.join_existing(_directory(environ),
        installation_id=environ.get('LIC_DATABASE_ID'), boot_id=_boot_id())
    return _install(gate)


def start_worker(environ):
    raw = environ.get(SNAPSHOT_ENV)
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1024:
        raise AdmissionUnavailable('provider_admission_snapshot_required')
    try:
        def unique(pairs):
            result = dict(pairs)
            if len(result) != len(pairs):
                raise ValueError()
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if type(value) is not dict or set(value) != {'installation_id', 'generation', 'boot_id', 'concurrency'}:
            raise ValueError()
        config = AdmissionConfig(**value)
        if (config.installation_id != environ.get('LIC_DATABASE_ID') or config.boot_id != _boot_id()
                or str(config.concurrency) != environ.get('LIC_YOUTUBE_REQUEST_CONCURRENCY')):
            raise ValueError()
        gate = AdmissionGate(_directory(environ), config)
        gate.snapshot()  # Open and verify only; never initialize or repair.
    except AdmissionUnavailable:
        raise
    except (ValueError, TypeError):
        raise AdmissionUnavailable('provider_admission_snapshot_invalid') from None
    return _install(gate)


def stop_runtime(runtime):
    global _runtime
    if runtime is None:
        return
    runtime.close()
    with _guard:
        if _runtime is runtime:
            _runtime = None


@contextmanager
def request_scope(kind, cancel=None, *, deadline=None):
    if kind not in KINDS:
        raise ValueError('provider_admission_kind_invalid')
    if (deadline is not None and (type(deadline) not in (int, float)
            or not math.isfinite(deadline) or deadline < 0)):
        raise ValueError('provider_request_deadline_invalid')
    token = _scope.set((kind, cancel))
    deadline_token = _deadline.set(request_deadline(deadline))
    try:
        yield
    finally:
        _deadline.reset(deadline_token)
        _scope.reset(token)


def request_deadline(explicit=None):
    """A provider call or nested scope can shorten, never extend its unit."""
    inherited = _deadline.get()
    if inherited is None:
        return explicit
    return inherited if explicit is None else min(inherited, explicit)


@contextmanager
def admit_request(*, timeout=30, cancel=None):
    """One permit for one HTTP attempt including its bounded body read."""
    runtime = _runtime
    if runtime is None:
        from deployment_profile import selected_deployment_profile
        if selected_deployment_profile() == 'self-hosted':
            raise AdmissionUnavailable('provider_admission_not_initialized')
        yield _UnregulatedRequest(cancel)
        return
    if runtime.pid != os.getpid():
        raise AdmissionUnavailable('provider_admission_process_changed')
    kind, scoped_cancel = _scope.get()
    cancellation = _Cancelled(runtime, scoped_cancel, cancel)
    with runtime.gate.acquire(kind, timeout=timeout, cancel=cancellation) as permit:
        cancellation.permit = permit
        yield cancellation
