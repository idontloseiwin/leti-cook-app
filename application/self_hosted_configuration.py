"""Encrypted owner configuration, independent of API-data storage and backups."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import json
import os
import re
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from private_config_files import config_directory, read_file, safe_path, setup_lock
from self_hosted_settings import SettingsError, browser_settings, preset, valid_api_key, SETTINGS_ENV, settings_ini, read_settings_file, configured_settings_path
from youtube_errors import KEY_REFUSALS

CONFIG_ENV = 'LIC_SETUP_DIRECTORY'
_SETUP_KEY = ContextVar('lic_setup_key', default=None)
STARTER_CHANNEL_URLS = frozenset('https://www.youtube.com/@' + handle for handle in
    ('MrBeast', 'MarkRober', 'veritasium', 'kurzgesagt', 'mkbhd'))


def validate_workspace(value):
    # Only the owner's selected channel identities, never API payloads/progress.
    if (not isinstance(value, dict) or set(value) != {'phase', 'channels'}
            or value['phase'] not in ('choosing', 'loading', 'complete')
            or not isinstance(value['channels'], dict)
            or not set(value['channels']) <= STARTER_CHANNEL_URLS):
        raise ValueError('invalid_workspace_preparation')
    for channel_id in value['channels'].values():
        if channel_id is not None and (not isinstance(channel_id, str)
                or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id)):
            raise ValueError('invalid_workspace_channel')
    return value


def default_settings():
    return {'preset': 'standard', 'limits': preset('standard'),
            'automatic_collection': True, 'interval_minutes': 30, 'manual_stats_quota_units': 200}


class ConfigurationStore:
    def __init__(self, directory):
        self.directory = config_directory(directory)
        if os.name != 'nt' and self.directory.stat().st_mode & 0o077:
            raise SettingsError('configuration_directory_must_be_private')
        self.path = self.directory / 'settings.enc'
        self.key_path = self.directory / 'settings.key'
        selected_settings = os.getenv(SETTINGS_ENV)
        self.settings_path = configured_settings_path(os.environ, directory=self.directory)
        self._settings_file_selected = bool(selected_settings)

    @classmethod
    def initialize(cls, directory):
        store = cls(directory)
        with setup_lock(store.directory):
            if store.path.exists() or store.key_path.exists():
                raise SettingsError('configuration_already_exists')
            fd = os.open(safe_path(store.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                output.write(Fernet.generate_key())
                output.flush()
                os.fsync(output.fileno())
            if not os.getenv(SETTINGS_ENV):
                fd=os.open(safe_path(store.settings_path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as output:
                    output.write(settings_ini(default_settings()))
                    output.flush()
                    os.fsync(output.fileno())
            read_settings_file(store.settings_path)
            store._write({'version': 2, 'revision': 0, 'api_key': None,
                          'settings': default_settings(), 'prepared': False,
                          'phase': 'needs_key', 'error': None,
                          'workspace': {'phase': 'choosing', 'channels': {}}})
        return store

    def read(self):
        try:
            value = json.loads(Fernet(read_file(self.key_path)).decrypt(read_file(self.path)))
            if not isinstance(value,dict): raise ValueError()
            fields={'version','revision','api_key','prepared','phase','error'}
            if value.get('version')==1: fields.add('settings')
            if 'workspace' in value: fields.add('workspace')
            # A new key waits here until YouTube accepts it; the running installation keeps using api_key.
            if 'pending_api_key' in value: fields.add('pending_api_key')
            if set(value)!=fields: raise ValueError()
            if value['version'] not in (1,2) or type(value['revision']) is not int or value['revision'] < 0:
                raise ValueError()
            if value['api_key'] is not None and not valid_api_key(value['api_key']): raise ValueError()
            if type(value['prepared']) is not bool or value['phase'] not in ('needs_key', 'saved', 'preparing', 'ready', 'error'):
                raise ValueError()
            if value['prepared'] and not value['api_key']: raise ValueError()
            if 'pending_api_key' in value and not (value['prepared'] and valid_api_key(value['pending_api_key'])):
                raise ValueError()
            if (value['version']==2 or self._settings_file_selected
                    or self.settings_path.exists() or self.settings_path.is_symlink()):
                # A managed file is authoritative even for a legacy encrypted
                # record. Invalid/missing selected files never fall back.
                value['settings']=read_settings_file(self.settings_path)
            else:
                settings = value['settings']
                # Preserve the two previous standards in supported embedded-only
                # records; changed shipped defaults must never replace owner limits.
                previous_standards = tuple(preset('standard', {
                    'inventory_page_budget': 6000, 'stats_limit': stats_limit,
                }) for stats_limit in (6000, 20000))
                if (isinstance(settings, dict) and settings.get('preset') == 'standard'
                        and settings.get('limits') in previous_standards):
                    settings = {**settings, 'preset': 'custom'}
                value['settings']=browser_settings(settings)
            browser_settings(value['settings'])
            # Existing installations keep access; only fresh installs start onboarding.
            value['workspace'] = validate_workspace(value.get('workspace', {'phase': 'complete', 'channels': {}}))
            return value
        except (InvalidToken, ValueError, TypeError, KeyError):
            raise SettingsError('configuration_unreadable') from None

    def _write(self, value):
        # The token authenticates configuration as well as encrypting the API key.
        if 'workspace' in value: validate_workspace(value['workspace'])
        stored={k:v for k,v in value.items() if value['version']!=2 or k!='settings'}
        content = Fernet(read_file(self.key_path)).encrypt(json.dumps(stored, separators=(',', ':')).encode())
        safe_path(self.path)
        if self.path.exists(): read_file(self.path)
        fd, name = tempfile.mkstemp(prefix='.configuration-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            if os.name != 'nt':
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
        finally:
            if os.path.exists(name): os.unlink(name)

    def update(self, revision, change):
        with setup_lock(self.directory):
            value = self.read()
            if type(revision) is not int or revision != value['revision']:
                raise SettingsError('configuration_changed_reload')
            before=copy.deepcopy(value['settings'])
            change(value)
            if value['settings']!=before: raise SettingsError('settings_are_managed_in_config_file')
            value['revision'] += 1
            self._write(value)
            return value


@contextmanager
def preparation_key(key):
    token = _SETUP_KEY.set(key)
    try: yield
    finally: _SETUP_KEY.reset(token)


def provider_key(supplied):
    # A configured installation obtains its key from the encrypted setup store.
    if os.getenv('LIC_DEPLOYMENT_PROFILE') != 'self-hosted' or not os.getenv(CONFIG_ENV):
        return supplied
    if _SETUP_KEY.get() is not None:
        return _SETUP_KEY.get()
    value = ConfigurationStore(os.environ[CONFIG_ENV]).read()
    if not value['prepared'] or not value['api_key']:
        raise SettingsError('setup_required')
    # YouTube refused this key: nothing is sent until set-key stores one it accepts.
    if value['error'] in KEY_REFUSALS:
        raise SettingsError(value['error'])
    return value['api_key']


def key_refusal():
    """provider_key_rejected or provider_access_denied while the stored key is marked refused; never raises."""
    if os.getenv('LIC_DEPLOYMENT_PROFILE') != 'self-hosted' or not os.getenv(CONFIG_ENV):
        return None
    try:
        value = ConfigurationStore(os.environ[CONFIG_ENV]).read()
    except Exception:
        return None
    return value['error'] if value['prepared'] and value['error'] in KEY_REFUSALS else None


def collection_paused():
    """True while automatic collection is turned off in config.ini; never raises."""
    if os.getenv('LIC_DEPLOYMENT_PROFILE') != 'self-hosted' or not os.getenv(CONFIG_ENV):
        return False
    try:
        value = ConfigurationStore(os.environ[CONFIG_ENV]).read()
    except Exception:
        return False
    return value['prepared'] and value['settings'].get('automatic_collection') is False


def record_key_refusal(key, code):
    """Mark the stored key refused when YouTube refused exactly that key. Never raises.

    A key being checked by set-key, or one already replaced, is not the stored
    key, so a late answer about it marks nothing.
    """
    if (code not in KEY_REFUSALS or not key or os.getenv('LIC_DEPLOYMENT_PROFILE') != 'self-hosted'
            or not os.getenv(CONFIG_ENV)):
        return False
    try:
        store = ConfigurationStore(os.environ[CONFIG_ENV])
        for _ in range(3):
            value = store.read()
            if not value['prepared'] or value['api_key'] != key or value['error'] == code:
                return False
            try:
                store.update(value['revision'], lambda v: v.update(error=code))
                return True
            except SettingsError:
                continue  # Changed meanwhile: decide again on the current record.
    except Exception:
        return False
    return False
