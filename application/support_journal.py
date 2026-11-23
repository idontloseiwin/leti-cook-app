"""Small passive discovery journal. It never owns admission, receipts or freshness.

Only closed boundary facts and installation-keyed references reach the existing
bounded JSONL sink. Initialization does local I/O; producers only enqueue. A
missing/broken journal loses diagnostics, never changes collection or a read.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import atexit
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import threading
import uuid

from telemetry_sink import JsonlTelemetryJournal, TelemetryEmitter, journal_file_lock

MAX_BYTES = 4 * 1024 * 1024
EVENT_BYTES = 2048
QUEUE_SIZE = 256
READ_LIMIT = 200
RECEIPT_LIMIT = 128
HEADER = 'X-Leti-Discovery-Read'
_VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_CHANNEL = re.compile(r'UC[A-Za-z0-9_-]{22}\Z')
_UUID = re.compile(r'[a-f0-9]{32}\Z')
_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_REASONS = frozenset(('websub', 'repair', 'count', 'head', 'initial', 'client_report',
    'writer_receipt', 'owner_busy', 'held', 'format_off', 'quota', 'budget', 'retry',
    'scheduled_handoff', 'phase_complete', 'provider_rate_limited', 'quota_state_unavailable',
    'availability_retry_expired', 'discovery_metadata', 'discovery_membership', 'discovery_hydration',
    'transport_error', 'channel_mismatch', 'publication_waiting', 'not_yet_visible', 'no_membership',
    'malformed_response', 'membership_conflict', 'membership_missing', 'expired', 'worker_start'))
_STAGES = frozenset(('signal', 'claim', 'wait', 'recovery', 'provider', 'writer',
                     'projection', 'api', 'browser', 'worker'))
_STATUSES = frozenset(('accepted', 'started', 'received', 'confirmed', 'waiting',
                       'unknown', 'recovered', 'rejected', 'failed', 'rendered', 'stopped', 'prepared', 'delivered'))
_emitter = None
_key = None
_state = 'off'
_lock = threading.Lock()
_receipts = OrderedDict()


def _private_file(path, *, create=None):
    if create is not None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(create)
    if path.is_symlink():
        raise ValueError('journal_path_invalid')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or (os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('journal_permissions_invalid')


def initialize(*, environ=None, path=None, key=None):
    """Once per backend/child; explicit path/key injection is for disposable tests."""
    global _emitter, _key, _state
    if _emitter is not None:
        return
    environment = os.environ if environ is None else environ
    if environment.get('LIC_SUPPORT_JOURNAL_ENABLED', 'true').lower() in ('0', 'false', 'off', 'no'):
        return
    try:
        if path is None:
            auth = Path(environment.get('LIC_AUTH_DATABASE', ''))
            if not auth.is_absolute():
                return
            path = auth.parent / 'discovery-support.jsonl'
        path = Path(path)
        if not path.is_absolute() or not path.parent.is_dir():
            raise ValueError('journal_path_invalid')
        _private_file(path, create=b'')
        _private_file(path.with_name(path.name + '.lock'), create=b'')
        if key is None:
            # The same existing process/file lock also prevents another child
            # reading a just-created, not-yet-written key.
            with journal_file_lock(path, timeout=.05):
                secret = path.with_suffix('.key')
                _private_file(secret, create=os.urandom(32))
                with secret.open('rb') as handle:
                    key = handle.read(33)
        if type(key) is not bytes or len(key) != 32:
            raise ValueError('journal_key_invalid')
        journal = JsonlTelemetryJournal(path, max_bytes=MAX_BYTES,
                                       event_bytes=EVENT_BYTES, lock_timeout=.05)
        journal.recover_rotation()
        _key = key
        _emitter = TelemetryEmitter(journal, queue_size=QUEUE_SIZE, batch_size=16)
        _state = 'ok'
    except Exception:
        _key = None
        _state = 'unavailable'


def reference(kind, identity):
    try:
        pattern = _VIDEO if kind == 'video' else _CHANNEL if kind == 'channel' else None
        if _key is None or pattern is None or type(identity) is not str or not pattern.fullmatch(identity):
            return None
        return hmac.new(_key, (kind + ':' + identity).encode('ascii'), hashlib.sha256).hexdigest()[:32]
    except Exception:
        return None


def event(stage, status, *, video_id=None, channel_id=None, subject_ref=None,
          reason=None, read_id=None, attempt_id=None, work_id=None, origin=None,
          operation_id=None, count=None, http_status=None):
    try:
        if _emitter is None or stage not in _STAGES or status not in _STATUSES:
            return
        facts = {}
        subject = reference('video', video_id) if video_id is not None else subject_ref
        channel = reference('channel', channel_id)
        if type(subject) is str and _UUID.fullmatch(subject):
            facts['subject_ref'] = subject
        if channel:
            facts['channel_ref'] = channel
        if type(read_id) is str and _UUID.fullmatch(read_id):
            facts['read_id'] = read_id
        if origin in ('websub', 'repair') and type(work_id) is str and re.fullmatch(r'[a-fA-F0-9-]{1,36}', work_id):
            facts['work_ref'] = hmac.new(_key, (origin + ':' + work_id).encode('ascii'), hashlib.sha256).hexdigest()[:32]
        elif origin == 'inventory' and channel and work_id in ('long', 'short', 'stream'):
            # A source reference, never a lease, page token or count generation.
            facts['work_ref'] = hmac.new(_key, ('inventory:' + channel_id + ':' + work_id).encode('ascii'), hashlib.sha256).hexdigest()[:32]
        if operation_id is not None:
            try:
                facts['operation_id'] = uuid.UUID(str(operation_id)).hex
            except (ValueError, TypeError, AttributeError):
                pass
        if attempt_id is not None:
            try:
                facts['request_attempt_id'] = uuid.UUID(str(attempt_id)).hex
            except (ValueError, TypeError, AttributeError):
                pass
        # Callers supply fixed application codes, never exception/provider text.
        if type(reason) is str and reason in _REASONS:
            facts['reason'] = reason
        if type(count) is int and 0 <= count <= 10_000:
            facts['count'] = count
        if type(http_status) is int and 100 <= http_status <= 599:
            facts['http_status'] = http_status
        _emitter.emit('support', 'browser' if stage == 'browser' else 'collector',
                      'browser' if stage == 'api' else 'journal', stage, status=status, **facts)
    except Exception:
        pass


def rows(stage, status, values, **facts):
    try:
        for row in values[:50]:
            event(stage, status, video_id=row.get('video_id'), channel_id=row.get('channel_id'),
                  work_id=row.get('work_id'), origin=row.get('origin'), **facts)
    except Exception:
        pass


def inventory_event(stage, status, *, channel_id, video_type, operation_id=None,
                    attempt_id=None, cause=None, http_status=None):
    """Only typed source identity and an operational UUID leave this boundary."""
    try:
        if video_type not in ('long', 'short', 'stream') or not reference('channel', channel_id):
            return
        cause = {'daily_budget': 'budget', 'window_budget': 'budget',
                 'campaign_budget': 'budget', 'page_budget_exhausted': 'budget',
                 'provider_quota': 'quota', 'quota_deferred': 'quota',
                 'grant_not_dispatchable': 'retry', 'initial_inventory': 'initial',
                 'head_observation': 'head', 'discovery': 'head',
                 'cancelled': 'scheduled_handoff', 'hot_runtime_interrupted': 'held',
                 'playlist_not_found': 'retry', 'error': 'retry'}.get(cause, cause)
        event(stage, status, channel_id=channel_id, origin='inventory', work_id=video_type,
              operation_id=operation_id, attempt_id=attempt_id, reason=cause, http_status=http_status)
    except Exception:
        pass


def observe_pipeline(operation, status, facts):
    """Reuse exact HTTP and writer observations, excluding the old verbose stream."""
    try:
        if operation in ('inventory_prepare', 'inventory_write', 'inventory_page'):
            boundary = ({'prepared': ('claim', 'prepared')} if operation == 'inventory_prepare' else
                        {'started': ('writer', 'started'), 'confirmed': ('writer', 'confirmed'),
                         'unknown': ('writer', 'unknown')} if operation == 'inventory_write' else
                        {'skipped': ('wait', 'waiting'), 'unknown': ('writer', 'unknown')}).get(status)
            if boundary is not None:
                inventory_event(*boundary, channel_id=facts.get('channel_id'),
                    video_type=facts.get('video_type'), operation_id=facts.get('operation_id'),
                    cause=facts.get('reason'))
            return
        if operation == 'channel_count_change' and status == 'confirmed':
            # Existing metadata owner emits this only after accepted persistence
            # or reconciliation. It is an observed comparison, not a claim that
            # a particular locked DB count generation was created or consumed.
            event('signal', 'accepted', channel_id=facts.get('channel_id'), reason='count')
            return
        if operation in ('http_started', 'http_finished'):
            stage = 'provider'
            outcome = 'started' if operation == 'http_started' else ('failed' if status == 'error' else 'received')
        elif operation == 'video_write' and status in ('started', 'confirmed', 'unknown', 'rejected'):
            stage, outcome = 'writer', status
        else:
            return
        identities = facts.get('video_ids') if operation.startswith('http_') else (facts.get('video_id'),)
        identities = [identity for identity in (identities or ())[:50]
                      if type(identity) is str and _VIDEO.fullmatch(identity)]
        if not identities and operation.startswith('http_') and reference('channel', facts.get('channel_id')):
            # A typed head request discovers IDs; its provider boundary can
            # initially correlate only to the owning channel signal.
            identities = [None]
        for identity in identities:
            if identity is None and facts.get('video_type') in ('long', 'short', 'stream'):
                inventory_event(stage, outcome, channel_id=facts.get('channel_id'),
                    video_type=facts.get('video_type'), operation_id=facts.get('operation_id'),
                    attempt_id=facts.get('request_attempt_id'), cause=facts.get('reason'),
                    http_status=facts.get('http_status'))
                continue
            event(stage, outcome, video_id=identity, reason=facts.get('reason'),
                  channel_id=facts.get('channel_id'),
                  attempt_id=facts.get('request_attempt_id'), operation_id=facts.get('operation_id'),
                  http_status=facts.get('http_status'))
    except Exception:
        pass


def api_result(result):
    """Observe exact returned catalogue identities; do not traverse arbitrary payloads."""
    try:
        if _emitter is None or not isinstance(result, dict):
            return None
        values = result.get('rows')
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            return None
        subjects = tuple(reference('video', row.get('id')) if isinstance(row, dict) else None for row in values)
        if not all(subjects):
            return None
        expires = datetime.fromisoformat(result['context']['valid_before'].replace('Z', '+00:00')).timestamp()
        if expires <= datetime.now(timezone.utc).timestamp():
            return None
        receipt = uuid.uuid4().hex
        with _lock:
            _receipts[receipt] = (subjects, expires, set())
            while len(_receipts) > RECEIPT_LIMIT:
                _receipts.popitem(last=False)
        for subject in subjects:
            event('api', 'prepared', subject_ref=subject, read_id=receipt)
        return receipt
    except Exception:
        return None


def api_delivery(receipt, status):
    try:
        if status not in ('delivered', 'unknown'):
            return
        with _lock:
            stored = _receipts.get(receipt)
            if stored is None:
                return
            subjects, _expires, seen = stored
            if status in seen:
                return
            seen.add(status)
        for subject in subjects:
            event('api', status, subject_ref=subject, read_id=receipt)
    except Exception:
        pass


async def observe_http(app, scope, receive, send):
    """Observe completed ASGI body delivery without reading or buffering a body."""
    receipt, delivered = None, False
    async def observed_send(message):
        nonlocal receipt, delivered
        if message.get('type') == 'http.response.start':
            for key, value in message.get('headers', ()):
                if key.lower() == HEADER.lower().encode('ascii'):
                    candidate = value.decode('ascii', errors='ignore')
                    if _UUID.fullmatch(candidate):
                        receipt = candidate
        await send(message)
        if message.get('type') == 'http.response.body' and not message.get('more_body', False):
            delivered = True
            if receipt:
                api_delivery(receipt, 'delivered')
    try:
        await app(scope, receive, observed_send)
    finally:
        if receipt and not delivered:
            api_delivery(receipt, 'unknown')


def browser_report(value):
    """Untrusted reports cannot create identities or confirm a writer/projection."""
    try:
        if not isinstance(value, dict) or set(value) != {'read_id', 'status', 'indices'}:
            return
        receipt, status, indices = value['read_id'], value['status'], value['indices']
        if (type(receipt) is not str or not _UUID.fullmatch(receipt) or status not in ('received', 'rendered')
                or not isinstance(indices, list) or not 1 <= len(indices) <= 50
                or any(type(index) is not int or not 0 <= index < 50 for index in indices)):
            return
        with _lock:
            stored = _receipts.get(receipt)
            if stored is None:
                return
            subjects, expires, seen = stored
            if 'delivered' not in seen:
                return
            if expires <= datetime.now(timezone.utc).timestamp():
                _receipts.pop(receipt, None)
                return
            accepted = []
            for index in set(indices):
                if index < len(subjects) and (status, index) not in seen:
                    seen.add((status, index))
                    accepted.append(subjects[index])
        for subject in accepted:
            event('browser', status, subject_ref=subject, read_id=receipt, reason='client_report')
    except Exception:
        pass


def snapshot():
    try:
        if _emitter is None:
            return dict(status=_state, events=[], metrics={})
        result = _emitter.journal.tail_snapshot(READ_LIMIT)
        return dict(status='unavailable' if result.get('read_error') else 'ok',
                    events=result['events'], metrics=_emitter.metrics())
    except Exception:
        return dict(status='unavailable', events=[], metrics={})


def close():
    global _emitter, _key, _state
    emitter, _emitter = _emitter, None
    _key, _state = None, 'off'
    with _lock:
        _receipts.clear()
    if emitter is not None:
        try:
            emitter.close(.75)
        except Exception:
            pass


atexit.register(close)
