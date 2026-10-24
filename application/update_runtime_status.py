"""Authenticated source-free runtime status for the installation updater."""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit, urlunsplit

from hot_runtime import HotAdmissionConfig
from release_version import read_version
from schema_release import load_schema_release
from update_runtime_protocol import (HEADER, MAX_STATUS_BYTES, STATUS_PATH, TOKEN_SECONDS,
    UpdateRuntimeError, operation_id, request_proof, sign_status, verify_status)


def signed_update_status(runtime, manager, *, admission, now=None):
    if runtime is None or manager is None or manager.collection is None:
        raise UpdateRuntimeError('update_runtime_unavailable')
    # Same ordering as supervisor/prepare: manager -> filesystem -> runtime lock.
    # Reading status never initiates handoff or modifies the hold file.
    with manager.lock:
        state = runtime.status()
        collector = manager.collection.update_status()
        setup_active = bool(manager.thread and manager.thread.is_alive())
    schema = load_schema_release()
    ready = state['user_work']['allowed']
    timestamp = time.time() if now is None else now
    value = {'version': 1, 'profile': 'self-hosted', 'database': admission.identity,
             'runtime_epoch': runtime.update_epoch, 'release_version': read_version(),
             'schema': {'baseline_id': schema.baseline_id if ready else None,
                        'version': schema.schema_version if ready else None, 'ready': ready},
             'hold': state['update_hold'], 'collector': collector, 'setup_active': setup_active,
             'hot': {name: state[name] for name in ('api_read', 'collection', 'user_work')},
             'issued_at': timestamp, 'expires_at': timestamp + TOKEN_SECONDS}
    result = sign_status(value, admission.secret)
    # The producer must obey the same finite contract as the host-side consumer.
    verify_status(json.dumps(result).encode(), identity=admission.identity, key=admission.secret, now=timestamp)
    return result


class UpdateRuntimeClient:
    """One short uncached read; no database, provider, owner credential or write."""
    def __init__(self, runtime_url, *, admission, fetch=None, clock=time.time, monotonic=time.monotonic):
        parsed = urlsplit(runtime_url or '')
        if (type(admission) is not HotAdmissionConfig or parsed.scheme != 'http'
                or parsed.hostname not in ('127.0.0.1', '::1') or parsed.port is None
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path != '/internal/hot-runtime'):
            raise UpdateRuntimeError('update_runtime_not_configured')
        self.url = urlunsplit(parsed._replace(path=STATUS_PATH))
        self.admission, self.clock, self.monotonic = admission, clock, monotonic
        self.fetch = fetch or self._fetch

    def _fetch(self):
        import httpx
        start = self.monotonic()
        headers = {'Accept': 'application/json', HEADER: request_proof(self.admission.secret, now=self.clock())}
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
            with client.stream('GET', self.url, headers=headers) as response:
                if response.status_code != 200:
                    raise UpdateRuntimeError('update_status_unavailable')
                data = bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data) > MAX_STATUS_BYTES or self.monotonic() - start > 3:
                        raise UpdateRuntimeError('update_status_unavailable')
        return bytes(data)

    def status(self, *, expected_operation=None):
        if expected_operation is not None:
            operation_id(expected_operation)
        try:
            start = self.monotonic()
            data = self.fetch()
            if self.monotonic() - start > 3:
                raise UpdateRuntimeError('update_status_unavailable')
            return verify_status(data, identity=self.admission.identity, key=self.admission.secret,
                                 expected_operation=expected_operation, now=self.clock())
        except UpdateRuntimeError:
            raise
        except Exception:
            raise UpdateRuntimeError('update_status_unavailable') from None
