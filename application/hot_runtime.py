"""Ephemeral self-hosted admission. No persisted readiness or provider data."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import uuid

from deployment_profile import selected_deployment_profile
import time_trust as _time_trust

MAINTENANCE_RPCS = frozenset({
    'get_hot_runtime_contract', 'cleanup_hot_current_source', 'cleanup_hot_evidence',
    'get_hot_startup_expiry', 'get_update_copy_window',
    'cleanup_hot_deleted_channels',
    'cleanup_hot_inventory', 'cleanup_hot_inventory_copies', 'cleanup_hot_operational_copies',
    'cleanup_hot_category_reference', 'cleanup_hot_priority',
    'cleanup_deleted_saved_objects',
})
RUNTIME_PATH = '/internal/hot-runtime'
TOKEN_SECONDS = 5
WATCHDOG_SECONDS = 20
# A failed maintenance step is retried after a pause that doubles per failure.
# Cleanup pages are idempotent: a retry re-reads the failed page's cursor.
RETRY_FIRST_SECONDS = 15
RETRY_MAX_SECONDS = 600
# Cleanup asks collection to pause when an expired row stays skipped this many
# passes in a row, or when its data is this close to YouTube's 30-day limit.
PAUSE_AFTER_PASSES = 3
PAUSE_MARGIN_SECONDS = 12 * 3600
# While startup cleanup holds API reads after downtime, one step runs up to this
# many pages, each its own short transaction, within the step's 2-second budget.
# Otherwise a step is one page.
CATCHUP_PAGES_PER_STEP = 20
_MAINTENANCE = ContextVar('hot_maintenance', default=False)
_COLLECTION_CONTROL = ContextVar('hot_collection_control', default=False)
_COLLECTION_INTERRUPTED = ContextVar('hot_collection_interrupted', default=False)
_REQUEST_ACCESS = ContextVar('hot_request_access', default='collection')
_SUPERVISED_HANDOFF = ContextVar('hot_supervised_handoff', default=None)
COLLECTION_CONTROL_RPCS = frozenset({
    'abort_hot_collection_claims', 'interrupt_hot_sync_run', 'abort_hot_reconnect_claim',
    'drain_hot_collection_claims', 'drain_hot_channel_metadata',
})
_local_controller = None
_remote = None
_remote_configuration = None
_remote_lock = threading.RLock()


class HotRuntimeUnavailable(RuntimeError):
    def __init__(self, code='hot_runtime_not_ready'):
        self.code = code
        super().__init__(code)


class HotDatabaseAdmissionDenied(HotRuntimeUnavailable):
    """The guarded DB request was rejected before transport was dispatched."""


@contextmanager
def supervised_collection_handoff(request_handoff):
    """Only the fixed worker entrypoint may finish a unit during a soft pause."""
    if not callable(request_handoff):
        raise TypeError('handoff callback must be callable')
    token = _SUPERVISED_HANDOFF.set(request_handoff)
    try:
        yield
    finally:
        _SUPERVISED_HANDOFF.reset(token)


def _admit_verified_switch(access, switch):
    if switch['allowed']:
        return True
    callback = _SUPERVISED_HANDOFF.get()
    if (access == 'collection' and switch['reason'] in ('update_hold', 'collection_priority')
            and callback is not None):
        try:
            # Request a boundary stop without denying the accepted unit's
            # provider completion, durable ACK, or final resource unwind.
            callback()
        except Exception:
            return False
        return True
    return False


@contextmanager
def runtime_collection_access():
    """Background work cannot inherit the initiating HTTP route's admission."""
    token = _REQUEST_ACCESS.set('collection')
    try:
        yield
    finally:
        _REQUEST_ACCESS.reset(token)


@contextmanager
def runtime_request_access(method, path):
    """HTTP database work uses its reviewed route; other processes collect."""
    from hot_runtime_routes import route_access
    from db.base.read_context import http_method_scope
    token = _REQUEST_ACCESS.set(route_access(method, path))
    try:
        with http_method_scope(method):
            yield
    finally:
        _REQUEST_ACCESS.reset(token)


@contextmanager
def collection_control_access():
    """Only source-free, token-owned abort RPCs may drain already claimed work."""
    token = _COLLECTION_CONTROL.set(True)
    try:
        yield
    finally:
        _COLLECTION_CONTROL.reset(token)


def mark_collection_interrupted():
    _COLLECTION_INTERRUPTED.set(True)


def collection_was_interrupted():
    return _COLLECTION_INTERRUPTED.get()


def database_identity(url):
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HotRuntimeUnavailable('hot_runtime_not_configured')
    return hashlib.sha256(url.rstrip('/').encode()).hexdigest()


@dataclass(frozen=True)
class HotAdmissionConfig:
    """Stable installation identity and an internal secret, independent of DB login.

    The installer generates the UUID once per database installation. Secret or
    connection credential rotation must keep that UUID. The secret is deliberately
    absent from repr so a configuration diagnostic cannot disclose it.
    """
    database_id: str
    secret: str = field(repr=False)

    def __post_init__(self):
        try:
            identity = uuid.UUID(self.database_id)
            valid_identity = identity.int != 0 and str(identity) == self.database_id
        except (ValueError, TypeError, AttributeError):
            valid_identity = False
        if (not valid_identity or type(self.secret) is not str
                or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', self.secret) is None):
            raise HotRuntimeUnavailable('hot_runtime_not_configured')

    @property
    def identity(self):
        return hashlib.sha256(b'lic-hot-database-v1\0' + self.database_id.encode('ascii')).hexdigest()


def load_hot_admission_config(environ=None):
    """Require the installed direct-connection identity; never fall back to JWT."""
    values = os.environ if environ is None else environ
    if values.get('LIC_DATABASE_TRANSPORT') != 'postgres':
        raise HotRuntimeUnavailable('hot_runtime_not_configured')
    return HotAdmissionConfig(values.get('LIC_DATABASE_ID'), values.get('LIC_HOT_RUNTIME_SECRET'))


def _admission_credentials(database_url, key, admission):
    if admission is not None:
        if type(admission) is not HotAdmissionConfig or database_url is not None or key is not None:
            raise HotRuntimeUnavailable('hot_runtime_not_configured')
        return admission.identity, admission.secret
    if type(key) is not str or not key:
        raise HotRuntimeUnavailable('hot_runtime_not_configured')
    return database_identity(database_url), key


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def signed_status(controller, database_url=None, key=None, *, admission=None, now=None, diagnostics=False):
    identity, key = _admission_credentials(database_url, key, admission)
    timestamp = time.time() if now is None else now
    state = controller.status()
    value = {'version': 2, 'profile': 'self-hosted', 'database': identity,
             'status': state['status'], 'epoch': state['epoch'], 'issued_at': timestamp,
             'expires_at': timestamp + TOKEN_SECONDS,
             'api_read': state['api_read'], 'collection': state['collection'],
             'user_work': state['user_work']}
    if diagnostics:
        value['diagnostics'] = state
    value['signature'] = hmac.new(key.encode(), b'lic-hot-runtime-v2\0' + _canonical(value), hashlib.sha256).hexdigest()
    return value


_ADMISSION_DIAGNOSTIC_REASONS = frozenset({
    'transport_failed', 'http_rejected', 'response_too_large', 'response_too_slow',
    'response_invalid', 'proof_shape', 'proof_signature', 'proof_identity',
    'proof_blocked', 'proof_clock', 'proof_expired',
    'local_maintenance_failed', 'local_stopped', 'local_clock_changed',
    'local_maintenance_stale', 'local_validation_pending',
    'local_database_unavailable', 'local_schema_invalid', 'local_time_mismatch',
    'local_startup_cleanup', 'local_collection_priority', 'local_update_hold', 'local_update_hold_invalid',
})

ACCESS_REASONS = frozenset({'ready', 'validation_pending', 'database_unavailable',
    'schema_invalid', 'time_mismatch', 'clock_changed', 'startup_cleanup',
    'collection_priority', 'stopped', 'update_hold', 'update_hold_invalid'})


class _AdmissionProofError(RuntimeError):
    """Finite internal diagnostics; never preserve a response or exception text."""
    def __init__(self, reason, *, http_status=None):
        self.reason, self.http_status = reason, http_status
        super().__init__('hot_runtime_not_ready')


def _record_admission_denial(owner, reason, *, now, http_status=None, duration_ms=None):
    """Bounded fail-soft envelope. It never changes admission or retry policy."""
    if reason not in _ADMISSION_DIAGNOSTIC_REASONS:
        reason = 'transport_failed'
    if (getattr(owner, '_denial_reason', None) == reason
            and now - getattr(owner, '_denial_at', float('-inf')) < 5):
        return
    owner._denial_reason, owner._denial_at = reason, now
    try:
        from telemetry import emit_event
        emit_event('runtime', 'hot_admission', 'hot_runtime', 'hot_runtime_admission',
                   phase=reason, direction='internal', status='error',
                   http_status=http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
                   duration_ms=duration_ms)
    except Exception:
        pass


class RemoteAdmission:
    """Workers verify a five-second proof; at most one second is cached in RAM."""
    def __init__(self, url, database_url=None, key=None, *, admission=None, fetch=None, clock=time.time, monotonic=time.monotonic):
        identity, key = _admission_credentials(database_url, key, admission)
        parsed = urlsplit(url or '')
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1') or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path != RUNTIME_PATH or not key):
            raise HotRuntimeUnavailable('hot_runtime_not_configured')
        self.url, self.identity, self.key = url, identity, key
        self.fetch, self.clock, self.monotonic = fetch or self._fetch, clock, monotonic
        self._until = self._expires = self._wall = self._mono = None
        self._access = None
        self._lock = threading.RLock()

    def _request_headers(self):
        from self_hosted_auth_protocol import HEADER, worker_proof
        return {HEADER: worker_proof(self.key, now=self.clock())}

    def _fetch(self):
        import httpx
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
            with client.stream('GET', self.url, headers={'Accept': 'application/json', **self._request_headers()}) as response:
                if response.status_code != 200:
                    raise _AdmissionProofError('http_rejected', http_status=response.status_code)
                data = bytearray()
                deadline = time.monotonic() + 3
                for part in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise _AdmissionProofError('response_too_slow')
                    data.extend(part)
                    if len(data) > 2048:
                        raise _AdmissionProofError('response_too_large')
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise _AdmissionProofError('response_invalid') from None

    def require_database_ready(self):
        return self.require_ready(access='user_work')

    def require_collection_ready(self):
        return self.require_ready(access='collection')

    def require_api_ready(self):
        return self.require_ready(access='api_read')

    def require_ready(self, *, access='collection'):
        if access not in ('api_read', 'collection', 'user_work'):
            raise HotRuntimeUnavailable()
        with self._lock:
            wall, mono = self.clock(), self.monotonic()
            drift = self._wall is not None and abs((wall - self._wall) - (mono - self._mono)) > 2
            if not drift and self._until is not None and mono < self._until and wall < self._expires:
                if _admit_verified_switch(access, self._access[access]):
                    return
                _record_admission_denial(self, 'proof_blocked', now=mono)
                raise HotRuntimeUnavailable()
            self._until = None
            started = mono
            reason = 'transport_failed'
            try:
                value = self.fetch()
                reason = 'proof_shape'
                fields = {'version', 'profile', 'database', 'status', 'epoch', 'issued_at', 'expires_at', 'signature',
                          'api_read', 'collection', 'user_work'}
                if type(value) is not dict or set(value) != fields:
                    raise ValueError()
                payload = {name: value[name] for name in fields - {'signature'}}
                expected = hmac.new(self.key.encode(), b'lic-hot-runtime-v2\0' + _canonical(payload), hashlib.sha256).hexdigest()
                reason = 'proof_signature'
                if not isinstance(value['signature'], str) or not hmac.compare_digest(expected, value['signature']):
                    raise ValueError()
                reason = 'proof_identity'
                if (type(value['version']) is not int or value['version'] != 2 or value['profile'] != 'self-hosted'
                        or value['database'] != self.identity
                        or type(value['epoch']) is not str or re.fullmatch(r'[0-9a-f]{32}', value['epoch']) is None):
                    raise ValueError()
                reason = 'proof_shape'
                switches = {name: value[name] for name in ('api_read', 'collection', 'user_work')}
                for switch in switches.values():
                    if (type(switch) is not dict or set(switch) != {'allowed', 'reason'}
                            or type(switch['allowed']) is not bool or switch['reason'] not in ACCESS_REASONS
                            or switch['allowed'] != (switch['reason'] == 'ready')):
                        raise ValueError()
                if value['status'] != ('ready' if switches['api_read']['allowed'] else 'blocked'):
                    raise ValueError()
                if ((switches['api_read']['allowed'] or switches['collection']['allowed'])
                        and not switches['user_work']['allowed']):
                    raise ValueError()
                reason = 'proof_clock'
                issued, expires = value['issued_at'], value['expires_at']
                if type(issued) not in (int, float) or type(expires) not in (int, float):
                    raise ValueError()
                wall, mono = self.clock(), self.monotonic()
                if not issued <= wall + 1 or not expires <= issued + TOKEN_SECONDS:
                    raise ValueError()
                reason = 'proof_expired'
                if not wall < expires:
                    raise ValueError()
                self._wall, self._mono, self._expires = wall, mono, expires
                self._until = mono + min(1, expires - wall)
                self._access = switches
                reason = 'proof_blocked'
                if not _admit_verified_switch(access, switches[access]):
                    raise ValueError()
            except Exception as error:
                if isinstance(error, _AdmissionProofError):
                    reason = error.reason
                _record_admission_denial(self, reason, now=self.monotonic(),
                    http_status=error.http_status if isinstance(error, _AdmissionProofError) else None,
                    duration_ms=round(max(0, self.monotonic() - started) * 1000, 3))
                raise HotRuntimeUnavailable() from None


@contextmanager
def maintenance_access():
    token = _MAINTENANCE.set(True)
    try:
        yield
    finally:
        _MAINTENANCE.reset(token)


def require_hot_runtime_ready(*, operation='database', rpc_name=None):
    if selected_deployment_profile() != 'self-hosted':
        return
    admission = load_hot_admission_config()
    if _COLLECTION_CONTROL.get():
        if operation == 'database' and rpc_name in COLLECTION_CONTROL_RPCS:
            return
        raise HotRuntimeUnavailable('hot_runtime_control_scope_denied')
    if _MAINTENANCE.get():
        if operation == 'database' and rpc_name in MAINTENANCE_RPCS:
            return
        raise HotRuntimeUnavailable('hot_runtime_maintenance_scope_denied')
    access = (_REQUEST_ACCESS.get() if operation == 'database' else
              'collection' if operation in ('provider', 'collection') else
              'user_work' if operation == 'user_work' else 'api_read')
    if operation not in ('database', 'provider', 'collection', 'user_work', 'http'):
        raise HotRuntimeUnavailable('hot_runtime_operation_denied')
    if _local_controller is not None:
        method = {'collection': 'require_collection_ready', 'user_work': 'require_database_ready',
                  'api_read': 'require_ready'}[access]
        getattr(_local_controller, method)()
        return
    global _remote, _remote_configuration
    configuration = (os.getenv('LIC_HOT_RUNTIME_URL'), admission)
    with _remote_lock:
        if _remote is None or configuration != _remote_configuration:
            _remote = RemoteAdmission(configuration[0], admission=admission)
            _remote_configuration = configuration
        remote = _remote
    remote.require_ready(access=access)


def current_update_copy_status():
    guard = getattr(_local_controller, 'update_copy_guard', None)
    return guard.status() if guard is not None else {'status': 'disabled', 'expires_at': None}


def install_local_runtime(controller):
    global _local_controller
    if _local_controller is not None:
        raise HotRuntimeUnavailable('hot_runtime_owner_exists')
    _local_controller = controller


def remove_local_runtime(controller):
    global _local_controller
    if _local_controller is controller:
        _local_controller = None


class RuntimeOwnerLock:
    """A lock, never a readiness marker. Only one backend owns maintenance."""
    def __init__(self, database_url=None, *, admission=None, directory=None):
        if admission is not None:
            identity, _ = _admission_credentials(database_url, None, admission)
        else:
            identity = database_identity(database_url)
        self.directory = Path(directory) if directory else Path.home() / '.local/state/leti-cook'
        self.name = 'hot-runtime-' + identity[:24] + '.lock'
        self.stream = None

    def __enter__(self):
        for path in (self.directory, *self.directory.parents):
            if path.is_symlink():
                raise HotRuntimeUnavailable('hot_runtime_lock_unavailable')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / self.name
        if path.is_symlink():
            raise HotRuntimeUnavailable('hot_runtime_lock_unavailable')
        stream = path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if not stream.tell():
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                os.fchmod(stream.fileno(), 0o600)
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            stream.close()
            raise HotRuntimeUnavailable('hot_runtime_lock_unavailable') from None
        self.stream = stream
        return self

    def __exit__(self, *args):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def _utc_seconds(value):
    from hot_storage import utc_instant
    return utc_instant(value).timestamp()


class HotRuntimeController:
    """Separate database, API reading and collection admission from maintenance."""
    def __init__(self, repository, *, local_cleanup, clear_caches, on_ready=lambda: None,
                 clock=time.time, monotonic=time.monotonic, time_trust=None, update_copy_guard=None, update_hold_guard=None):
        from hot_retention import HotCleanupCycle, PriorityRetrySet
        self.repository, self.local_cleanup, self.clear_caches = repository, local_cleanup, clear_caches
        self.update_copy_guard = update_copy_guard
        self.update_hold_guard = update_hold_guard
        self.update_epoch = uuid.uuid4().hex
        self.time_trust = _time_trust.monitor() if time_trust is None else time_trust
        self.on_ready, self._ready_notified = on_ready, False
        self.clock, self.monotonic = clock, monotonic
        self.cycle = HotCleanupCycle(repository, clock=monotonic)
        self._priority = PriorityRetrySet()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._validated = False
        self._database_reason = 'validation_pending'
        self._database_as_of = None
        self._clock_invalid = False
        self._recheck_pending = False
        self._failed = False
        self._failure_code = None
        self._failures = 0
        self._retry_at = 0
        self._last_tick = self._last_wall = None
        self._next_cycle = 0
        self._epoch = uuid.uuid4().hex
        self._summary = None
        self._held_passes = 0
        self._pause_requested = False
        self._startup_checked = False
        self._catchup = False
        self._catchup_cutoff = None
        self._notice = None
        self._notice_records = 0
        self._cycle_removed = 0
        self._round_pending = set()
        self._last_switches = {}

    def _drift(self):
        return (self._last_tick is not None and
                abs((self.clock() - self._last_wall) -
                    (self.monotonic() - self._last_tick)) > 2)

    @staticmethod
    def _switch(reason):
        return {'allowed': reason == 'ready', 'reason': reason}

    def status(self):
        from datetime import datetime, timezone
        hold = (self.update_hold_guard.check() if self.update_hold_guard is not None
                else {'state': 'disabled', 'operation_id': None})
        with self._lock:
            now = self.monotonic()
            time_state = self.time_trust.status()
            database_reason = 'stopped' if self._stop.is_set() else self._database_reason
            gate_reason = database_reason
            if gate_reason == 'ready':
                if self._drift() or self._recheck_pending:
                    gate_reason = 'clock_changed'
                elif self._clock_invalid or time_state.get('state') == 'mismatch':
                    gate_reason = 'time_mismatch'
            api_reason = gate_reason
            # Readable views remain the authority while a broken cleaner retries.
            if api_reason == 'ready' and not self._failed:
                if not self._startup_checked:
                    api_reason = 'validation_pending'
                elif self._catchup:
                    api_reason = 'startup_cleanup'
            collection_reason = gate_reason
            if collection_reason == 'ready' and self._pause_requested:
                collection_reason = 'collection_priority'
            if collection_reason == 'ready' and hold['state'] in ('held', 'invalid'):
                collection_reason = 'update_hold' if hold['state'] == 'held' else 'update_hold_invalid'
            if api_reason != 'ready':
                _record_admission_denial(self, 'local_' + api_reason, now=now)
            self._record_switches(api_reason, collection_reason, database_reason)
            return {
                'status': 'ready' if api_reason == 'ready' else 'blocked',
                'epoch': self._epoch, 'reason': api_reason,
                'observed_at': datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                'gate': {'open': gate_reason == 'ready', 'reason': gate_reason},
                'api_read': self._switch(api_reason),
                'collection': self._switch(collection_reason),
                'user_work': self._switch(database_reason),
                'heartbeat_age_seconds': (round(max(0, now - self._last_tick), 3)
                                          if self._last_tick is not None else None),
                'maintenance_failed': self._failed, 'failure_code': self._failure_code,
                'scan': self._summary, 'collection_pause_requested': self._pause_requested,
                'collection_priority': self._priority_diagnostics(),
                'time': time_state, 'startup_notice': dict(self._notice) if self._notice else None,
                'whole_database_clean_verified': False,
                'update_hold': hold,
                'update_copy': self.update_copy_guard.status() if self.update_copy_guard else {'status': 'disabled', 'expires_at': None},
            }

    def _priority_diagnostics(self):
        targets = {}
        for ref in self._priority.references:
            targets[ref.target] = targets.get(ref.target, 0) + 1
        near_limit = self._near_storage_limit(
            {'oldest_observed_expired_source_at': self._priority.oldest})
        return {'full_passes': self._held_passes, 'tracked_rows': len(self._priority.references),
                'targets': targets, 'incomplete': self._priority.incomplete,
                'cause': ('storage_deadline_near' if near_limit else 'unsafe_sources_after_full_passes')
                         if self._pause_requested else None}

    def _record_switches(self, api_reason, collection_reason, database_reason):
        # Only transitions, bounded counts and fixed vocabulary; no row IDs,
        # provider payloads, tokens or exception text enter telemetry.
        for access, reason in (('api_read', api_reason), ('collection', collection_reason),
                               ('user_work', database_reason)):
            previous = self._last_switches.get(access)
            if previous == reason:
                continue
            self._last_switches[access] = reason
            try:
                from telemetry import emit_event
                diagnostic = self._priority_diagnostics() if access == 'collection' else {}
                emit_event('runtime', 'hot_admission', access, 'hot_runtime_switch',
                    phase=reason, direction='internal', status='ok' if reason == 'ready' else 'paused',
                    count=diagnostic.get('tracked_rows'),
                    detail=json.dumps({'previous': previous,
                        **{key: value for key, value in diagnostic.items() if key != 'targets'}},
                        separators=(',', ':')))
                if reason == 'collection_priority':
                    for target, count in diagnostic['targets'].items():
                        emit_event('runtime', 'hot_admission', target, 'hot_runtime_priority',
                            phase=diagnostic['cause'], direction='internal', status='paused', count=count)
            except Exception:
                pass

    def require_ready(self):
        if not self.status()['api_read']['allowed']:
            raise HotRuntimeUnavailable()

    def require_collection_ready(self):
        if not self.status()['collection']['allowed']:
            raise HotRuntimeUnavailable()

    def require_database_ready(self):
        if not self.status()['user_work']['allowed']:
            raise HotRuntimeUnavailable()

    def _validate(self):
        with maintenance_access():
            contract = self.repository.contract()
        if (type(contract) is not dict or set(contract) != {'version', 'schema_ready', 'as_of'}
                or type(contract['version']) is not int or contract['version'] != 1
                or contract['schema_ready'] is not True):
            raise HotRuntimeUnavailable('hot_runtime_schema_invalid')
        # Schema/connection validity is independent from the source clock.
        bad_clock = False
        try:
            self._check_db_clock(contract['as_of'])
        except Exception:
            bad_clock = True
        with self._lock:
            self._validated = True
            self._database_reason = 'ready'
            self._database_as_of = contract['as_of']
            self._clock_invalid = bad_clock
        if bad_clock:
            raise HotRuntimeUnavailable('hot_runtime_clock_invalid') from None

    def _check_startup(self):
        from datetime import timedelta
        from hot_storage import HOT_DATA_WINDOW, STARTUP_CATCHUP_GRACE_HOURS, utc_instant
        # A startup held by external time verification needs a fresh DB T0
        # when it resumes, even when wall/monotonic time never jumped.
        if self._database_as_of is None:
            self._validate()
        # The successful contract check supplies database T0. The Python wall
        # clock validates it, but never chooses the retention boundary.
        reference = utc_instant(self._database_as_of)
        cutoff = reference - HOT_DATA_WINDOW
        with maintenance_access():
            result = self.repository.startup_expiry(
                cutoff=cutoff - timedelta(hours=STARTUP_CATCHUP_GRACE_HOURS), strict_entry=True)
        if (type(result) is not dict or set(result) != {'as_of', 'expired'}
                or type(result['expired']) is not bool):
            raise HotRuntimeUnavailable('hot_runtime_cleanup_failed')
        self._check_db_clock(result['as_of'])
        with self._lock:
            self._startup_checked = True
            if result['expired']:
                if not self._catchup:
                    self._catchup_cutoff = cutoff
                self._catchup = True
                if self._notice is None or self._notice['complete']:
                    self._notice_records = 0
                    self._notice = {'id': uuid.uuid4().hex, 'records': 0, 'complete': False}

    def initialize(self):
        """Bounded contract and expiry checks; never run a full cleanup pass."""
        if self.update_hold_guard is not None:
            self.update_hold_guard.check()
        if self.update_copy_guard is not None:
            self.update_copy_guard.check(force=True)
        with self._lock:
            if self._stop.is_set():
                return
            self._last_tick, self._last_wall = self.monotonic(), self.clock()
        try:
            self._validate()
            self.clear_caches()
            if self.time_trust.status().get('state') != 'mismatch':
                self._check_startup()
            else:
                self._database_as_of = None
        except Exception as error:
            self._handle_failure(error)

    def _reset_after_pause(self):
        try:
            self.clear_caches()
        except Exception:
            raise HotRuntimeUnavailable('hot_runtime_cache_clear_failed') from None
        self._validated = False
        self._startup_checked = False
        self._next_cycle = 0
        self._epoch = uuid.uuid4().hex
        self.cycle.reset()
        if self._priority.unresolved or self._pause_requested:
            self._priority.invalidate()
        self._round_pending.clear()
        self._cycle_removed = 0

    def _retry_priority(self):
        generation, requested = self._priority.request()
        with maintenance_access():
            result = self.repository.cleanup_priority(requested)
        # An interrupted or wrong-clock proof cannot release collection, even
        # when its transaction may have already completed.
        drift = self._drift()
        interrupted = self._last_tick is not None and self.monotonic() - self._last_tick > WATCHDOG_SECONDS
        if self._stop.is_set() or drift or interrupted:
            self._priority.invalidate()
            self._clock_invalid = self._clock_invalid or drift
            self._recheck_pending = True
            self._startup_checked = False
            return
        self._check_db_clock(result.as_of)
        with self._lock:
            now = self.monotonic()
            clean = self._priority.acknowledge(generation, requested, result, now)
            self._last_tick, self._last_wall = now, self.clock()
            if self._catchup:
                self._notice_records += result.deleted + result.scrubbed
                self._notice['records'] = self._notice_records
            if clean:
                self._pause_requested = False
                self._held_passes = 0
            elif self._priority.unresolved and result.complete:
                # A targeted retry is not a full cleanup pass. It can still
                # notice that an already observed old source nears its deadline.
                if self._near_storage_limit(
                        {'oldest_observed_expired_source_at': self._priority.oldest}):
                    self._pause_requested = True
            self._failed = False
            self._failure_code = None
            self._failures = 0

    def step(self):
        """One bounded cleanup page; connection/schema errors have their own gate."""
        if self.update_hold_guard is not None and not self._stop.is_set():
            self.update_hold_guard.check()
        if self.update_copy_guard is not None and not self._stop.is_set():
            self.update_copy_guard.check()
        with self._lock:
            now, wall = self.monotonic(), self.clock()
            interrupted = self._last_tick is not None and (
                now - self._last_tick > WATCHDOG_SECONDS or self._drift())
            if self._stop.is_set():
                return
            if interrupted:
                # Latch before advancing the observation baseline, even when a
                # failed cleanup page is still in its retry backoff.
                self._recheck_pending = True
                self._startup_checked = False
            self._last_tick, self._last_wall = now, wall
            if self._failed and now < self._retry_at:
                return
        try:
            if self._recheck_pending:
                self._reset_after_pause()
            if self._failed:
                # A page may have committed: resume its original cursor only.
                self._validated = False
                if self.cycle.summary()['status'] == 'error':
                    self.cycle.retry_failed()
            if not self._validated:
                self._validate()
            if self.time_trust.status().get('state') == 'mismatch':
                # Do not delete using a clock already known to be wrong, or
                # reuse its old startup timestamp after time is trusted again.
                self._database_as_of = None
                self._next_cycle = self.monotonic() + RETRY_FIRST_SECONDS
                return
            if not self._startup_checked:
                self._check_startup()
            with self._lock:
                self._recheck_pending = False
            if self._priority.due(self.monotonic()):
                self._retry_priority()
                return
            if now < self._next_cycle:
                return
            self._failed = False
            with maintenance_access():
                before = self.cycle.summary()
                if before['batches'] == 0:
                    self._validate()
                    result = self.local_cleanup()
                    if result.get('status') not in ('current', 'absent', 'expired_or_invalid'):
                        raise HotRuntimeUnavailable('hot_runtime_files_unavailable')
                if not self._round_pending:
                    self._round_pending = set(before.get('targets_pending', ()))
                def observe_priority(page):
                    # Status reads the same bounded cause registry from HTTP
                    # threads. Serialize that snapshot with in-memory changes;
                    # the repository call has already finished before callback.
                    with self._lock:
                        self._priority.observe(page, self.monotonic())
                summary = self.cycle.run_slice(max_batches=CATCHUP_PAGES_PER_STEP if self._catchup else 1,
                                               max_seconds=10, on_page=observe_priority)
            if self._drift():
                self._clock_invalid = True
                self._recheck_pending = True
                self._startup_checked = False
                return
            for page in summary['pages']:
                self._check_db_clock(page['as_of'])
            if summary['status'] == 'error':
                self._summary = {'status': 'error', 'failed_target': summary.get('failed_target')}
                raise HotRuntimeUnavailable('hot_runtime_cleanup_failed')
            expiry_after = None
            if summary['status'] == 'scan_exhausted' and self._catchup:
                # Do not hold the status lock over I/O: HTTP admission and
                # signed worker proofs must remain responsive during this read.
                with maintenance_access():
                    expiry_after = self.repository.startup_expiry(cutoff=self._catchup_cutoff)
                if (type(expiry_after) is not dict or set(expiry_after) != {'as_of', 'expired'}
                        or type(expiry_after['expired']) is not bool):
                    raise HotRuntimeUnavailable('hot_runtime_cleanup_failed')
                self._check_db_clock(expiry_after['as_of'])
            with self._lock:
                self._summary = {key: summary[key] for key in (
                    'status', 'batches', 'counts', 'targets_not_visited',
                    'targets_pending', 'targets_with_observed_deferrals',
                    'invalid_clocks_observed', 'unknown_expiry_observed',
                    'oldest_observed_expired_source_at')}
                self._summary['confirmed_expired_observed'] = summary.get('confirmed_expired_observed', 0)
                # A target that keeps failing waits behind the others; keep it visible.
                self._summary['targets_failing'] = list(summary.get('targets_failing', ()))
                self._last_tick, self._last_wall = self.monotonic(), self.clock()
                removed = summary['counts']['deleted'] + summary['counts']['scrubbed']
                if self._catchup:
                    self._notice_records += max(0, removed - self._cycle_removed)
                    self._notice['records'] = self._notice_records
                self._cycle_removed = removed
                historical_retention = bool(summary['oldest_observed_expired_source_at']
                                            or summary['invalid_clocks_observed']
                                            or summary.get('confirmed_expired_observed', 0))
                # A legacy cycle without private receipts gets only the existing
                # full-scan fallback. Historical counts never revive resolved IDs.
                if not summary.get('priority_observation_complete', False) and historical_retention:
                    self._priority.invalidate(summary['oldest_observed_expired_source_at'])
                retention = self._priority.unresolved
                unresolved = bool(summary['counts']['deferred'] or retention)
                for page in summary['pages']:
                    # Reading one page of a still advancing target is not a
                    # traversal. Count it only after exhaustion or lock stall.
                    if page.get('scan_exhausted') or page.get('target') in summary.get('targets_stalled', ()):
                        self._round_pending.discard(page.get('target'))
                exhausted = summary['status'] == 'scan_exhausted'
                # A fully visited round with held rows counts even when its cursor
                # cannot advance. Keep those cursors, and avoid spinning on locks.
                held_round = (bool(before.get('targets_pending')) and not self._round_pending
                              and not summary.get('targets_not_visited') and unresolved)
                if exhausted or held_round:
                    self._held_passes = self._held_passes + 1 if retention else 0
                    self._next_cycle = self.monotonic() + (5 if unresolved else 60)
                    self._round_pending.clear()
                if retention and (self._held_passes >= PAUSE_AFTER_PASSES or self._near_storage_limit(
                        {'oldest_observed_expired_source_at': self._priority.oldest})):
                    self._pause_requested = True
                if exhausted and not historical_retention:
                    # This existing conservative fallback clears incomplete or
                    # overflowed evidence only after an entire clean traversal.
                    self._priority.reset()
                    self._pause_requested = False
                    self._held_passes = 0
                if exhausted:
                    if self._catchup:
                        # Recheck the original backlog, not a moving boundary.
                        # Newly expired sources stay hidden and are handled by
                        # background cleanup; old held rows still block exit.
                        if not expiry_after['expired']:
                            self._catchup = False
                            self._catchup_cutoff = None
                            self._notice['complete'] = True
                    self.cycle.reset()
                    self._cycle_removed = 0
                self._failed = False
                self._failure_code = None
                self._failures = 0
                notify = self.status()['api_read']['allowed'] and not self._ready_notified
            if notify:
                self.on_ready()
                self._ready_notified = True
        except Exception as error:
            self._handle_failure(error)

    def _near_storage_limit(self, summary):
        oldest = summary['oldest_observed_expired_source_at']
        from hot_storage import YOUTUBE_STORAGE_LIMIT
        limit = YOUTUBE_STORAGE_LIMIT.total_seconds() - PAUSE_MARGIN_SECONDS
        return oldest is not None and self.clock() - _utc_seconds(oldest) >= limit

    def _handle_failure(self, error):
        code = getattr(error, 'code', None)
        database_errors = {'runtime_contract_unavailable', 'maintenance_client_unavailable'}
        schema_errors = {'hot_runtime_schema_invalid', 'runtime_contract_schema_invalid', 'invalid_runtime_contract'}
        allowed = database_errors | schema_errors | {
            'hot_runtime_clock_invalid', 'hot_runtime_cleanup_failed', 'hot_runtime_files_unavailable',
            'hot_runtime_cache_clear_failed'}
        code = code if code in allowed else 'hot_runtime_maintenance_failed'
        # A cleanup exception can hide a lost connection. Check the independent
        # contract once before allowing the fail-soft cleanup path.
        if code not in database_errors | schema_errors | {'hot_runtime_clock_invalid'}:
            try:
                self._validate()
            except Exception as contract_error:
                contract_code = getattr(contract_error, 'code', None)
                code = contract_code if contract_code in allowed else 'runtime_contract_unavailable'
        with self._lock:
            if code in database_errors:
                self._validated = False
                self._database_reason = 'database_unavailable'
            elif code in schema_errors:
                self._validated = False
                self._database_reason = 'schema_invalid'
            elif code == 'hot_runtime_clock_invalid':
                self._clock_invalid = True
                self._validated = False
            elif code != 'hot_runtime_cache_clear_failed':
                # The independent retry check has confirmed DB/schema/time.
                # A failed expiry/cleanup query is then only a warning; views
                # still protect reads. Never bypass a failed cache reset.
                self._recheck_pending = False
            # Different failure classes must not inherit each other's retry
            # streak. An outage is probed at a fixed interval; a repeatedly
            # broken cleanup retains its existing increasing delay.
            if code != self._failure_code:
                self._failures = 0
            self._failure_code = code
            self._failed = True
            self._failures += 1
            delay = RETRY_FIRST_SECONDS if code in database_errors else min(
                RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** min(20, self._failures - 1))
            self._retry_at = self.monotonic() + delay

    def _check_db_clock(self, value):
        from hot_storage import utc_instant
        if abs(utc_instant(value).timestamp() - self.clock()) > 10:
            raise HotRuntimeUnavailable('hot_runtime_clock_invalid')

    def start(self):
        if self._thread is not None:
            raise HotRuntimeUnavailable('hot_runtime_owner_exists')
        def run():
            while not self._stop.is_set():
                self.time_trust.probe_if_due()
                self.step()
                self._stop.wait(0.1 if self.monotonic() >= self._next_cycle and not self._failed else 1)
        self._thread = threading.Thread(target=run, name='hot-source-maintenance', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.repository.close()
        if self._thread is not None:
            self._thread.join(timeout=10)


WORKSPACE_PATH = '/app-explorer-v3'
RETIRED_APP_PATHS = ('/app-next', '/app-explorer', '/app-explorer-v2')


def workspace_page_path(path):
    """A V3 page or deep link; its assets keep the plain JSON refusal."""
    return ((path == WORKSPACE_PATH or path.startswith(WORKSPACE_PATH + '/'))
            and not path.startswith(WORKSPACE_PATH + '/assets/'))


def retired_app_path(path):
    """Mirrors the retired interface routes in server.py, which answer 410."""
    return path in ('/app', '/app/') or any(
        path == base or path.startswith(base + '/') for base in RETIRED_APP_PATHS)


class HotRuntimeMiddleware:
    """Protect cached HTTP reads too; never rewrite an acknowledged mutation."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from hot_runtime_routes import route_access
        path = scope.get('path', '')
        if (scope['type'] != 'http' or selected_deployment_profile() != 'self-hosted'
                or route_access(scope.get('method', ''), path) == 'user_work'):
            # Shell/auth pages need no DB. Actual user-work transactions still
            # check database/schema admission through their request context.
            return await self.app(scope, receive, send)
        async def blocked():
            body = b'{"code":"hot_runtime_not_ready","detail":"Stored data is being checked. See /system/hot-runtime."}'
            content_type = b'application/json'
            await send({'type': 'http.response.start', 'status': 503,
                        'headers': [(b'content-type', content_type), (b'cache-control', b'no-store'),
                                    (b'content-security-policy', b"default-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
                                    (b'retry-after', b'5')]})
            await send({'type': 'http.response.body', 'body': body})
        try:
            require_hot_runtime_ready(operation='http')
        except HotRuntimeUnavailable:
            return await blocked()
        denied = False
        async def admitted_send(message):
            nonlocal denied
            if denied:
                return
            if message['type'] == 'http.response.start' and (scope.get('method') in ('GET', 'HEAD')
                    or (scope.get('method') == 'POST' and path == '/research/queries/execute')):
                try:
                    require_hot_runtime_ready(operation='http')
                except HotRuntimeUnavailable:
                    denied = True
                    return await blocked()
            await send(message)
        return await self.app(scope, receive, admitted_send)
