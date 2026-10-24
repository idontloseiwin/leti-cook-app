"""Bounded, source-free update hold and signed read-only status protocol."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time

from release_version import parse_version
from schema_release import SchemaRelease

HOLD_FILENAME = 'update-hold.json'
MAX_HOLD_BYTES = 512
STATUS_PATH = '/internal/update-status'
MAX_STATUS_BYTES = 4096
TOKEN_SECONDS = 5
HEADER = 'X-LIC-Runtime-Proof'
HOT_REASONS = frozenset({'ready', 'validation_pending', 'database_unavailable',
    'schema_invalid', 'time_mismatch', 'clock_changed', 'startup_cleanup',
    'collection_priority', 'stopped', 'update_hold', 'update_hold_invalid'})
OUTCOMES = frozenset({'unobserved', 'idle', 'draining', 'normal', 'forced', 'unconfirmed'})
FIELDS = frozenset({'version', 'profile', 'database', 'runtime_epoch', 'release_version',
    'schema', 'hold', 'collector', 'setup_active', 'hot', 'issued_at', 'expires_at', 'signature'})


class UpdateRuntimeError(ValueError):
    """A fixed code; never preserve secret, response or filesystem content."""


def operation_id(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise UpdateRuntimeError('update_operation_invalid')
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _json(data, limit):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        if type(data) is not bytes or not 1 <= len(data) <= limit:
            raise ValueError()
        return json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise UpdateRuntimeError('update_document_invalid') from None


def parse_hold(data):
    value = _json(data, MAX_HOLD_BYTES)
    if type(value) is not dict or set(value) != {'operation_id'}:
        raise UpdateRuntimeError('update_hold_invalid')
    return operation_id(value['operation_id'])


def hold_bytes(value):
    return _canonical({'operation_id': operation_id(value)}) + b'\n'


def request_proof(key, *, now=None):
    stamp = str(int(time.time() if now is None else now))
    message = b'leti-cook-update-request-v1\0GET\0/internal/update-status\0' + stamp.encode('ascii')
    return stamp + '.' + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def valid_request_proof(value, key, *, now=None):
    if not key or type(value) is not str or re.fullmatch(r'[0-9]{1,12}\.[0-9a-f]{64}', value) is None:
        return False
    current = int(time.time() if now is None else now)
    stamp = int(value.split('.')[0])
    return current - 10 <= stamp <= current + 1 and hmac.compare_digest(value, request_proof(key, now=stamp))


def sign_status(value, key):
    payload = dict(value)
    payload['signature'] = hmac.new(key.encode(), b'leti-cook-update-status-v1\0' + _canonical(payload), hashlib.sha256).hexdigest()
    return payload


def _exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError()


def _nullable_id(value):
    if value is not None:
        operation_id(value)


def verify_status(data, *, identity, key, expected_operation=None, now=None):
    """Verify before returning even blocked states; no caller trusts unsigned JSON."""
    try:
        value = _json(data, MAX_STATUS_BYTES)
        _exact(value, FIELDS)
        signature = value['signature']
        payload = {name: item for name, item in value.items() if name != 'signature'}
        expected = sign_status(payload, key)['signature']
        if type(signature) is not str or not hmac.compare_digest(signature, expected):
            raise ValueError()
        if type(value['version']) is not int or value['version'] != 1 or value['profile'] != 'self-hosted' or value['database'] != identity:
            raise ValueError()
        operation_id(value['runtime_epoch'])
        parse_version(value['release_version'])
        schema = value['schema']
        _exact(schema, ('baseline_id', 'version', 'ready'))
        if type(schema['ready']) is not bool:
            raise ValueError()
        if schema['ready']:
            SchemaRelease(schema['baseline_id'], schema['version'])
        elif schema['baseline_id'] is not None or schema['version'] is not None:
            raise ValueError()
        hold = value['hold']
        _exact(hold, ('state', 'operation_id'))
        if hold['state'] not in ('disabled', 'absent', 'held', 'invalid'):
            raise ValueError()
        _nullable_id(hold['operation_id'])
        if (hold['state'] == 'held') != (hold['operation_id'] is not None):
            raise ValueError()
        collector = value['collector']
        _exact(collector, ('observed_operation_id', 'active', 'outcome'))
        _nullable_id(collector['observed_operation_id'])
        if type(collector['active']) is not bool or collector['outcome'] not in OUTCOMES:
            raise ValueError()
        if collector['outcome'] == 'unobserved' and collector['observed_operation_id'] is not None:
            raise ValueError()
        if collector['outcome'] in ('idle', 'normal', 'forced', 'draining') and collector['observed_operation_id'] is None:
            raise ValueError()
        if collector['active'] and collector['outcome'] in ('idle', 'normal'):
            raise ValueError()
        if type(value['setup_active']) is not bool:
            raise ValueError()
        _exact(value['hot'], ('api_read', 'collection', 'user_work'))
        for switch in value['hot'].values():
            _exact(switch, ('allowed', 'reason'))
            if type(switch['allowed']) is not bool or switch['reason'] not in HOT_REASONS or switch['allowed'] != (switch['reason'] == 'ready'):
                raise ValueError()
        if schema['ready'] != value['hot']['user_work']['allowed']:
            raise ValueError()
        if (value['hot']['api_read']['allowed'] or value['hot']['collection']['allowed']) and not value['hot']['user_work']['allowed']:
            raise ValueError()
        if hold['state'] in ('held', 'invalid') and value['hot']['collection']['allowed']:
            raise ValueError()
        issued, expires = value['issued_at'], value['expires_at']
        wall = time.time() if now is None else now
        if (any(type(stamp) not in (int, float) or not math.isfinite(stamp) for stamp in (issued, expires))
                or not issued <= wall + 1 or not issued < expires <= issued + TOKEN_SECONDS or not wall < expires):
            raise ValueError()
        if expected_operation is not None:
            operation_id(expected_operation)
            if hold['state'] != 'held' or hold['operation_id'] != expected_operation:
                raise UpdateRuntimeError('update_hold_mismatch')
        return payload
    except UpdateRuntimeError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise UpdateRuntimeError('update_status_invalid') from None
