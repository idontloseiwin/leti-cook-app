"""Claimed-installation preparation with an explicitly refreshed DB category reference."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from self_hosted_configuration import ConfigurationStore, preparation_key, STARTER_CHANNEL_URLS
from self_hosted_settings import (SettingsError, CATEGORY_IDS, browser_environment, valid_api_key)

ERRORS = frozenset(('provider_key_rejected', 'provider_quota_exceeded', 'provider_access_denied',
    'provider_unavailable', 'provider_response_invalid', 'preparation_interrupted',
    'preparation_timeout', 'storage_unavailable', 'maintenance_not_ready'))
_MANAGER = None


def reference_status():
    from youtube_category_reference import load_reference_for_refresh, validate_reference
    try:
        value = load_reference_for_refresh()
        if value is None: return {'status': 'missing'}
        _, _, expires = validate_reference(value, now=datetime.now(timezone.utc))
        return {'status': 'current', 'languages': len(value['languages']),
                'expires_at': expires.isoformat()}
    except (ValueError, OSError):
        return {'status': 'unavailable'}


def provider_error_code(result, payload):
    """One fixed code for a refused YouTube response; never the provider's text."""
    from youtube_errors import provider_refusal_code
    code = provider_refusal_code(result.status_code, payload)
    if code:
        return code
    if result.status_code == 403:
        return 'provider_access_denied'
    return 'provider_key_rejected'


def check_api_key(key):
    """Send the first request preparation sends (1 quota unit): None when YouTube accepts the key."""
    from core import YOUTUBE_QUOTA_ATTEMPT_RECORDER
    from hot_runtime import require_hot_runtime_ready, HotRuntimeUnavailable
    from youtube_client import youtube_api_get
    import requests
    try:
        require_hot_runtime_ready(operation='provider')
        with preparation_key(key):
            result = youtube_api_get('i18nLanguages', phase='category_metadata', quota_bucket='general',
                attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER, params={'part': 'snippet', 'hl': 'en'},
                api_key=key, timeout_seconds=20, quota_units=1)
    except HotRuntimeUnavailable:
        return 'maintenance_not_ready'
    except requests.RequestException:
        return 'provider_unavailable'
    try: payload = result.json()
    except ValueError: return 'provider_response_invalid'
    if result.status_code != 200: return provider_error_code(result, payload)
    return None if isinstance(payload, dict) and not payload.get('error') else 'provider_response_invalid'


def fetch_reference(key, progress, stopping):
    from core import YOUTUBE_QUOTA_ATTEMPT_RECORDER
    from hot_runtime import require_hot_runtime_ready, HotRuntimeUnavailable
    from scripts.export_youtube_category_reference import (export_reference, ReferenceResponse, category_ids,
                                                           response_etag)
    from youtube_category_reference import load_reference_for_refresh, install_reference
    from youtube_client import youtube_api_get
    import requests
    started = time.monotonic()
    sent = 0

    def get(endpoint, params, *, if_none_match=None):
        nonlocal sent
        if stopping.is_set(): raise SettingsError('preparation_interrupted')
        if time.monotonic() - started >= 600 or sent >= 201:
            raise SettingsError('preparation_timeout')
        try:
            require_hot_runtime_ready(operation='provider')
            sent += 1
            progress(None, None, sent)
            result = youtube_api_get(endpoint, phase='category_metadata', quota_bucket='general',
                attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER, params=params, api_key=key,
                timeout_seconds=20, quota_units=1, if_none_match=if_none_match)
        except HotRuntimeUnavailable:
            raise SettingsError('maintenance_not_ready') from None
        except requests.RequestException:
            raise SettingsError('provider_unavailable') from None
        if result.status_code == 304: return ReferenceResponse(None)
        try: payload = result.json()
        except ValueError: raise SettingsError('provider_response_invalid') from None
        if result.status_code != 200:
            raise SettingsError(provider_error_code(result, payload))
        if not isinstance(payload, dict) or payload.get('error'):
            raise SettingsError('provider_response_invalid')
        return ReferenceResponse(payload, response_etag(result, payload))

    with preparation_key(key):
        value = export_reference(get, category_ids(CATEGORY_IDS), previous=load_reference_for_refresh(),
            progress=lambda done, total: progress(done, total, sent), clock=lambda: datetime.now(timezone.utc))
        if stopping.is_set(): raise SettingsError('preparation_interrupted')
        if time.monotonic() - started >= 600: raise SettingsError('preparation_timeout')
        require_hot_runtime_ready(operation='provider')
        install_reference(value)
    # The existing runtime owns recorder start/flush/close; no second lifecycle.


class SetupManager:
    def __init__(self, store, *, fetch=fetch_reference, reference=reference_status,
                 apply=lambda value: os.environ.update(browser_environment(value))):
        self.store, self.fetch, self.reference, self.apply = store, fetch, reference, apply
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.collection = None
        self.progress = {'languages_done': 0, 'languages_total': None, 'requests_sent': 0}
        value = store.read()
        if value['phase'] == 'preparing':
            value = store.update(value['revision'], lambda v: v.update(phase='error', error='preparation_interrupted'))
        self.apply(value['settings'])

    def public(self):
        with self.lock:
            value = self.store.read()
            return {'revision': value['revision'], 'configured': bool(value['api_key']),
                    'prepared': value['prepared'], 'phase': value['phase'],
                    'error': value['error'] if value['error'] in ERRORS else None,
                    'settings': value['settings'], 'progress': dict(self.progress),
                    'reference': self.reference(),
                    'collection': self.collection.status() if self.collection else {'state': 'starting'},
                    'settings_source': 'config.ini', 'maximum_reference_requests': 201,
                    'workspace': value['workspace']}

    def ready(self):
        with self.lock: return self.store.read()['prepared']

    def workspace_pending(self):
        with self.lock: return self.store.read()['workspace']['phase'] != 'complete'

    def _workspace_change(self, change):
        with self.lock:
            value = self.store.read()
            if not value['prepared'] or value['phase'] == 'preparing':
                raise SettingsError('setup_required')
            return self.store.update(value['revision'], lambda v: change(v['workspace']))

    def begin_starter(self, url):
        if url not in STARTER_CHANNEL_URLS: raise SettingsError('unsupported_starter_channel')
        def change(workspace):
            if workspace['phase'] == 'complete':
                workspace.update(phase='choosing', channels={})
            if workspace['phase'] == 'loading' and url not in workspace['channels']:
                raise SettingsError('workspace_selection_closed')
            workspace['channels'].setdefault(url, None)
        self._workspace_change(change)

    def record_starter(self, url, result):
        if result.get('status') not in ('added', 'exists', 'restored'): return
        channel_id = (result.get('channel') or {}).get('channel_id')
        if not channel_id: return
        def change(workspace):
            if url not in workspace['channels']: raise SettingsError('workspace_selection_changed')
            workspace['channels'][url] = channel_id
        self._workspace_change(change)

    def start_workspace(self, channels):
        if (not isinstance(channels, list) or len(channels) > 5
                or any(not isinstance(url, str) or url not in STARTER_CHANNEL_URLS for url in channels)
                or len(set(channels)) != len(channels)):
            raise SettingsError('invalid_starter_selection')
        def change(workspace):
            if workspace['phase'] == 'complete':
                workspace['channels'] = {}
            workspace['phase'] = 'loading'
            for url in channels: workspace['channels'].setdefault(url, None)
        self._workspace_change(change)
        return self.public()

    def complete_workspace(self, revision):
        # Caller has re-read every selected durable job under HOT admission.
        with self.lock:
            def change(value):
                if value['workspace']['phase'] != 'loading':
                    raise SettingsError('workspace_not_started')
                value['workspace'] = {'phase': 'complete', 'channels': {}}
            self.store.update(revision, change)
        return {'ready': True}

    def save(self, data):
        if not isinstance(data, dict) or set(data) != {'revision', 'api_key'}:
            raise SettingsError('invalid_setup_request')
        key = data['api_key']
        if key is not None and not valid_api_key(key): raise SettingsError('invalid_api_key')
        with self.lock:
            if self.stopping.is_set(): raise SettingsError('setup_stopping')
            if self.thread and self.thread.is_alive(): raise SettingsError('preparation_busy')
            if self.collection and self.collection.busy(): raise SettingsError('collection_busy')
            def change(value):
                if not key and not value['api_key']: raise SettingsError('api_key_required')
                changed = key is not None and key != value['api_key']
                value.update(api_key=key or value['api_key'], error=None)
                if changed: value['prepared'] = False
                value['phase'] = 'ready' if value['prepared'] else 'saved'
            value = self.store.update(data['revision'], change)
            self.apply(value['settings'])
            return self.public()

    def prepare(self, revision):
        from hot_runtime import require_hot_runtime_ready, HotRuntimeUnavailable
        try:
            require_hot_runtime_ready(operation='collection')
        except HotRuntimeUnavailable:
            raise SettingsError('maintenance_not_ready') from None
        with self.lock:
            # Recheck after waiting: the root may have installed a hold meanwhile.
            try:
                require_hot_runtime_ready(operation='collection')
            except HotRuntimeUnavailable:
                raise SettingsError('maintenance_not_ready') from None
            if self.stopping.is_set(): raise SettingsError('setup_stopping')
            if self.thread and self.thread.is_alive(): return self.public()
            if self.collection and self.collection.busy(): raise SettingsError('collection_busy')
            value = self.store.read()
            if not value['api_key']: raise SettingsError('api_key_required')
            value = self.store.update(revision, lambda v: v.update(phase='preparing', error=None))
            self.progress = {'languages_done': 0, 'languages_total': None, 'requests_sent': 0}
            self.thread = threading.Thread(target=self._prepare, args=(value,), name='lic-setup-reference', daemon=True)
            self.thread.start()
            return self.public()

    def _prepare(self, value):
        def progress(done, total, sent):
            with self.lock:
                if done is not None: self.progress.update(languages_done=done, languages_total=total)
                self.progress['requests_sent'] = sent
        try:
            self.fetch(value['api_key'], progress, self.stopping)
            if self.stopping.is_set(): raise SettingsError('preparation_interrupted')
            if self.reference()['status'] != 'current': raise SettingsError('provider_response_invalid')
            changes = {'phase': 'ready', 'prepared': True, 'error': None}
        except Exception as error:
            code = str(error) if isinstance(error, SettingsError) and str(error) in ERRORS else (
                'storage_unavailable' if isinstance(error, OSError) else 'provider_response_invalid')
            changes = {'phase': 'error', 'error': code}
        try:
            with self.lock: self.store.update(value['revision'], lambda v: v.update(**changes))
        except (OSError, ValueError):
            # An interrupted durable state is recovered on the next start.
            pass

    def close(self):
        self.stopping.set()
        try:
            if self.collection: self.collection.close()
        finally:
            if self.thread: self.thread.join(timeout=25)


def get_manager():
    if _MANAGER is None: raise SettingsError('setup_unavailable')
    return _MANAGER


def initial_loading_complete(value):
    if value.get('format_readiness_error') or value.get('action_required'):
        return False
    if 'all_initial_checks_complete' in value:
        job = value.get('job') or {}
        return (value['all_initial_checks_complete'] is True
                and job.get('status') == 'completed' and job.get('phase') == 'verify'
                and job.get('errors') == 0)
    # Older completed channels may have only the original long-form contract.
    return value.get('status') == 'ready' and value.get('current_profile_ready') is True


def install_manager(manager):
    global _MANAGER
    _MANAGER = manager


class SetupMiddleware:
    def __init__(self, app, *, manager=None):
        self.app, self.manager = app, manager

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http': return await self.app(scope, receive, send)
        path = scope.get('path', '')
        if path in ('/internal/hot-runtime', '/internal/update-status'):
            return await self.app(scope, receive, send)
        # Auth middleware must run before this boundary, including static files.
        if not (scope.get('state', {}).get('installation_access') or scope.get('state', {}).get('owner_username')):
            return await JSONResponse({'code': 'authentication_required'}, status_code=401)(scope, receive, send)
        manager = self.manager or _MANAGER
        request = Request(scope, receive)
        try:
            if manager is None: raise SettingsError('setup_unavailable')
            assets = {'/setup': ('setup.html', 'text/html'),
                      '/setup/assets/setup.js': ('setup.js', 'text/javascript'),
                      '/setup/assets/setup.css': ('setup.css', 'text/css')}
            if path in assets and request.method in ('GET', 'HEAD'):
                name, media = assets[path]
                response = FileResponse(Path(__file__).parent / 'self_hosted_ui' / name, media_type=media,
                    headers={'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
            elif path == '/setup/status' and request.method == 'GET':
                response = JSONResponse(await run_in_threadpool(manager.public))
            elif path in ('/setup/save', '/setup/prepare', '/setup/workspace/start') and request.method == 'POST':
                data = await asyncio.wait_for(self.body(request), timeout=5)
                if path.endswith('/start'):
                    if set(data) != {'channels'}: raise SettingsError('invalid_setup_request')
                    result = await run_in_threadpool(manager.start_workspace, data['channels'])
                elif path.endswith('/save'):
                    result = await run_in_threadpool(manager.save, data)
                else:
                    if set(data) != {'revision'}: raise SettingsError('invalid_setup_request')
                    result = await run_in_threadpool(manager.prepare, data['revision'])
                response = JSONResponse(result)
            elif path.startswith('/setup/') or path == '/setup':
                response = JSONResponse({'code': 'setup_route_not_found'}, status_code=404)
            elif not await run_in_threadpool(manager.ready):
                response = (RedirectResponse('/setup', status_code=303) if request.method in ('GET', 'HEAD')
                    and 'text/html' in request.headers.get('accept', '') else
                    JSONResponse({'code': 'setup_required'}, status_code=409))
            elif (request.method in ('GET', 'HEAD')
                    and (path in ('/', '/app-explorer-v3') or path.startswith('/app-explorer-v3/'))
                    and not path.startswith('/app-explorer-v3/assets/')
                    and await run_in_threadpool(manager.workspace_pending)):
                response = RedirectResponse('/setup', status_code=303)
            else:
                return await self.app(scope, receive, send)
        except SettingsError as error:
            code = str(error)
            # Codes originate from fixed validators; never echo the request body.
            response = JSONResponse({'code': code}, status_code=409)
        except (OSError, ValueError, TypeError, asyncio.TimeoutError):
            response = JSONResponse({'code': 'setup_unavailable_or_invalid_request'}, status_code=400)
        return await response(scope, receive, send)

    @staticmethod
    async def body(request):
        if request.headers.get('x-lic-setup') != '1' or request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise SettingsError('json_setup_request_required')
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > 8192: raise SettingsError('setup_request_too_large')
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value: raise SettingsError('duplicate_setup_field')
                value[key] = item
            return value
        value = json.loads(chunks, object_pairs_hook=unique)
        if not isinstance(value, dict): raise SettingsError('invalid_setup_request')
        return value
