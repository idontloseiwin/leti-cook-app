"""Pure self-hosted collection configuration; never loads credentials or clients."""
from __future__ import annotations
from urllib.parse import urlsplit
import ipaddress
import re
from config_migrations import CONFIG_VERSION, ConfigMigrationError, parse_config_bytes, config_version

# name: environment variable, minimum, maximum, standard preset, test preset.
# These bound passes/chunks, not an account quota or a catalogue limit.
FIELDS = {
    'metadata_limit': ('LIC_AUTO_SYNC_METADATA_LIMIT', 0, 100000, 1000, 2),
    'inventory_page_budget': ('LIC_AUTO_SYNC_INVENTORY_PAGE_BUDGET', 0, 100000, 20000, 12),
    'stats_limit': ('LIC_AUTO_SYNC_STATS_LIMIT', 1, 100000, 20000, 100),
    'new_video_stats_limit': ('LIC_AUTO_SYNC_NEW_VIDEO_STATS_LIMIT', 0, 100000, 2500, 100),
    'operator_chunk_size': ('OPERATOR_JOB_CHUNK_SIZE', 1, 250, 50, 50),
    'inventory_pages_per_chunk': ('OPERATOR_INITIAL_INVENTORY_PAGES_PER_CHUNK', 1, 10, 5, 2),
    'drain_max_chunks': ('OPERATOR_JOB_DRAIN_MAX_CHUNKS', 1, 512, 128, 16),
    'drain_max_seconds': ('OPERATOR_JOB_DRAIN_MAX_SECONDS', 1, 1200, 900, 240),
}
DEFAULT_HANDOFF_GRACE_SECONDS = 120
DEFAULT_YOUTUBE_REQUEST_CONCURRENCY = 8
YOUTUBE_REQUEST_CONCURRENCY_ENV = 'LIC_YOUTUBE_REQUEST_CONCURRENCY'
PROFILE = 'LIC_SELF_HOSTED_LIMITS_PROFILE'
FLAGS = dict.fromkeys(('DATABASE_VIDEO_COLLECTION_ENABLED', 'DATABASE_CHANNEL_DISCOVERY_ENABLED',
    'YOUTUBE_BATCH_STATS_ENABLED', 'TYPED_PLAYLIST_INVENTORY_ENABLED',
    'SHORT_VIDEO_COLLECTION_ENABLED', 'COMPLETED_STREAM_COLLECTION_ENABLED'), 'true')
FLAGS['OPERATOR_JOBS_MODE'] = 'durable'

class SettingsError(ValueError):
    """Messages are fixed codes/field names, never user-supplied values."""

def checked_limits(values):
    if set(values) != set(FIELDS): raise SettingsError('collection_fields_mismatch')
    result = {}
    for name, (_, minimum, maximum, _, _) in FIELDS.items():
        value = values[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise SettingsError('invalid_' + name)
        result[name] = value
    if result['new_video_stats_limit'] > result['stats_limit']:
        raise SettingsError('new_stats_exceeds_common_pool')
    return result

def preset(name, overrides=None):
    if name not in ('test', 'standard'): raise SettingsError('invalid_limits_preset')
    values = {key: row[4 if name == 'test' else 3] for key, row in FIELDS.items()}
    for key, value in (overrides or {}).items():
        if key not in FIELDS: raise SettingsError('unknown_limit')
        values[key] = value
    return checked_limits(values)

def collection_environment(name, overrides=None):
    values = preset(name, overrides)
    return {PROFILE: name, **FLAGS, **{FIELDS[k][0]: str(v) for k, v in values.items()}}

def limits_from_environment(environ):
    if environ.get(PROFILE) not in ('test', 'standard', 'custom'):
        raise SettingsError('limits_profile_required')
    values = {}
    for name, (key, _, _, _, _) in FIELDS.items():
        value = environ.get(key, '')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,7}', value):
            raise SettingsError('missing_or_invalid_' + name)
        values[name] = int(value)
    return checked_limits(values)

def validate_collection_environment(environ):
    values = limits_from_environment(environ)
    if any(environ.get(key) != value for key, value in FLAGS.items()):
        raise SettingsError('collection_flags_mismatch')
    return values

def valid_api_key(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{20,200}', value) is not None

def validate_local_runtime(environ, *, require_key=True):
    if environ.get('LIC_DEPLOYMENT_PROFILE') != 'self-hosted':
        raise SettingsError('self_hosted_profile_required')
    limits = validate_collection_environment(environ)
    # DB login and signed runtime identity are validated by their own owners.
    from db.base.client import load_database_config
    from hot_runtime import load_hot_admission_config, HotRuntimeUnavailable
    try:
        load_database_config(environ)
        load_hot_admission_config(environ)
    except (ValueError, HotRuntimeUnavailable):
        raise SettingsError('database_configuration_invalid') from None
    try:
        url = urlsplit(environ.get('LIC_HOT_RUNTIME_URL', ''))
        if (url.scheme != 'http' or not ipaddress.ip_address(url.hostname).is_loopback
            or url.username is not None or url.password is not None or url.query or url.fragment
            or url.path != '/internal/hot-runtime' or url.port is None
            or not 1024 <= url.port <= 65535):
            raise ValueError()
    except (ValueError, TypeError):
        raise SettingsError('invalid_local_lic_hot_runtime_url') from None
    if require_key and not valid_api_key(environ.get('YOUTUBE_API_KEY')):
        raise SettingsError('youtube_key_missing_or_invalid')
    return limits

# Provider identifiers only; labels are fetched into the expiring managed file.
CATEGORY_IDS = '1,2,10,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44'
AUTO_SYNC_INTERVALS = (6, 10, 15, 20, 30)


def browser_settings(value):
    required = {'preset', 'limits', 'automatic_collection', 'interval_minutes', 'manual_stats_quota_units'}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {'handoff_grace_seconds', 'initial_load_concurrency', 'youtube_request_concurrency'}:
        raise SettingsError('invalid_collection_settings')
    if value['preset'] not in ('test', 'standard', 'custom'):
        raise SettingsError('invalid_limits_preset')
    if type(value['automatic_collection']) is not bool:
        raise SettingsError('invalid_collection_switch')
    if type(value['interval_minutes']) is not int or value['interval_minutes'] not in AUTO_SYNC_INTERVALS:
        raise SettingsError('invalid_collection_interval')
    if type(value['manual_stats_quota_units']) is not int or not 1 <= value['manual_stats_quota_units'] <= 1000000:
        raise SettingsError('invalid_manual_stats_quota_units')
    grace = value.get('handoff_grace_seconds', DEFAULT_HANDOFF_GRACE_SECONDS)
    if type(grace) is not int or not 10 <= grace <= 600:
        raise SettingsError('invalid_handoff_grace_seconds')
    if 'initial_load_concurrency' in value and 'youtube_request_concurrency' in value:
        raise SettingsError('conflicting_request_concurrency')
    legacy = 'initial_load_concurrency' in value
    concurrency = value.get('initial_load_concurrency' if legacy else 'youtube_request_concurrency',
                            DEFAULT_YOUTUBE_REQUEST_CONCURRENCY)
    if type(concurrency) is not int or not 1 <= concurrency <= (8 if legacy else 50):
        raise SettingsError('invalid_initial_load_concurrency' if legacy else 'invalid_youtube_request_concurrency')
    limits = checked_limits(value['limits'])
    if value['preset'] != 'custom' and limits != preset(value['preset']):
        raise SettingsError('preset_values_mismatch')
    return {**{key: item for key, item in value.items() if key != 'initial_load_concurrency'},
            'limits': limits, 'handoff_grace_seconds': grace,
            'youtube_request_concurrency': concurrency}


def browser_environment(value):
    value = browser_settings(value)
    return {**FLAGS, PROFILE: value['preset'],
            'OPERATOR_JOB_MAX_QUOTA_UNITS': str(value['manual_stats_quota_units']),
            YOUTUBE_REQUEST_CONCURRENCY_ENV: str(value['youtube_request_concurrency']),
            **{FIELDS[k][0]: str(v) for k, v in value['limits'].items()}}


def worker_environment(value, environ):
    """Reload live settings while preserving the supervisor's restart-only bound."""
    applied = browser_environment(value)
    if environ.get('LIC_COLLECTION_SUPERVISED') == '1':
        captured = environ.get(YOUTUBE_REQUEST_CONCURRENCY_ENV, '')
        if captured not in tuple(str(number) for number in range(1, 51)):
            raise SettingsError('youtube_request_concurrency_snapshot_required')
        applied[YOUTUBE_REQUEST_CONCURRENCY_ENV] = captured
    return applied


# Human-editable nonsecret configuration. No provider credentials belong here.
SETTINGS_ENV = 'LIC_SETTINGS_FILE'

def settings_ini(value):
    value=browser_settings(value)
    descriptions={
        'metadata_limit':'Channels whose YouTube metadata is refreshed per automatic pass',
        'inventory_page_budget':'YouTube playlist page attempts per automatic pass, all three types together',
        'stats_limit':'Videos selected for General or Stats per automatic pass, all types together',
        'new_video_stats_limit':'Deprecated collection limit; caps the in-memory sample of newly found IDs only (must not exceed stats_limit)',
        'operator_chunk_size':'Videos processed per job chunk',
        'inventory_pages_per_chunk':'Playlist pages processed per inventory chunk',
        'drain_max_chunks':'Maximum job chunks per worker pass',
        'drain_max_seconds':'Maximum worker-pass time budget in seconds',
    }
    lines=['# Leti Cook collection settings. Contains no passwords or API keys.',
           '# Limits apply to the next worker run; no restart is needed.',
           '# Restart the backend after changing youtube_request_concurrency.',
           '# Limits bound one pass or chunk, not the total channel/video library.',
           '[format]', 'version = '+str(CONFIG_VERSION), '', '[collection]',
           '# false pauses collection. Required data maintenance continues.',
           'automatic_collection = '+str(value['automatic_collection']).lower(),
           '# Automatic pass interval in minutes: 6, 10, 15, 20 or 30.',
           'interval_minutes = '+str(value['interval_minutes']),
           '# Seconds to finish the current work unit at a scheduled handoff: 10..600.',
           'handoff_grace_seconds = '+str(value['handoff_grace_seconds']),
           '# Per-manual-statistics-job admission bound, 1..1000000; not daily Google quota.',
           'manual_stats_quota_units = '+str(value['manual_stats_quota_units']),
           '# Maximum simultaneous YouTube requests across the application: 1..50.',
           '# Applies after restart. Start with 8; increase only after measuring.',
           'youtube_request_concurrency = '+str(value['youtube_request_concurrency']),
           '', '[limits]']
    for name,(_,minimum,maximum,_,_) in FIELDS.items():
        zero_hint = ('. 0 disables the sample, never collection.' if name == 'new_video_stats_limit'
                     else '. 0 disables this phase; it is not unlimited.' if minimum == 0 else '.')
        lines.extend(['# '+descriptions[name]+': '+str(minimum)+'..'+str(maximum)+
                      zero_hint,
                      name+' = '+str(value['limits'][name])])
    return '\n'.join(lines)+'\n'

def configured_settings_path(environ, *, directory=None):
    """The same nonsecret INI path for the collector store and health readers."""
    from pathlib import Path
    selected = environ.get(SETTINGS_ENV)
    if selected:
        return Path(selected)
    directory = directory if directory is not None else environ.get('LIC_SETUP_DIRECTORY')
    if not directory:
        raise SettingsError('settings_path_required')
    return Path(directory) / 'config.ini'


def read_configured_limits(environ):
    """Read current validated INI limits without reading credentials or stale child env."""
    return read_settings_file(configured_settings_path(environ))['limits']


def read_settings_file(path):
    import os,stat
    from private_config_files import safe_path
    path=safe_path(path)
    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or (os.name!='nt' and info.st_mode & 0o022):
            raise SettingsError('unsafe_settings_file')
        raw=stream.read(16385)
    if len(raw)>16384: raise SettingsError('settings_file_too_large')
    return read_settings_bytes(raw)


def read_settings_bytes(raw):
    """Validate the current format without opening files or discarding unknown text."""
    try:
        parser = parse_config_bytes(raw)
        version = config_version(parser)
    except ConfigMigrationError as error:
        raise SettingsError(str(error)) from None
    if version != CONFIG_VERSION:
        raise SettingsError('unsupported_config_version')
    if any(parser.has_option('limits', key) for key in ('channel_limit', 'typed_inventory_limit')):
        raise SettingsError('mixed_inventory_config')
    if parser.has_option('limits', 'operator_max_items'):
        raise SettingsError('retired_operator_max_items')
    required={'automatic_collection','interval_minutes','manual_stats_quota_units'}
    if not required <= set(parser['collection']) or not set(FIELDS) <= set(parser['limits']):
        raise SettingsError('invalid_settings_file')
    automatic=parser['collection']['automatic_collection']
    if automatic not in ('true','false'): raise SettingsError('invalid_collection_switch')
    # Each value fails with its own code, so settings_problem can name the field.
    def integer(value, code):
        if not re.fullmatch(r'[0-9]{1,7}',value): raise SettingsError(code)
        return int(value)
    limits={k:integer(parser['limits'][k],'invalid_'+k) for k in FIELDS}
    collection=parser['collection']
    value={'preset':'standard' if limits==preset('standard') else 'custom','limits':limits,
           'automatic_collection':automatic=='true',
           'interval_minutes':integer(collection['interval_minutes'],'invalid_collection_interval'),
           'handoff_grace_seconds':integer(collection.get('handoff_grace_seconds', str(DEFAULT_HANDOFF_GRACE_SECONDS)),
                                           'invalid_handoff_grace_seconds'),
           'manual_stats_quota_units':integer(collection['manual_stats_quota_units'],'invalid_manual_stats_quota_units')}
    for key in ('initial_load_concurrency', 'youtube_request_concurrency'):
        if key in collection:
            value[key] = integer(collection[key], 'invalid_' + key)
    return browser_settings(value)


def settings_problem(code):
    """One sentence about config.ini for the operator: field names and allowed values, never file content."""
    for name,(_,minimum,maximum,_,_) in FIELDS.items():
        if code=='invalid_'+name:
            return f'{name} must be a whole number from {minimum} to {maximum}.'
    intervals=', '.join(map(str,AUTO_SYNC_INTERVALS[:-1]))+' or '+str(AUTO_SYNC_INTERVALS[-1])
    return {
        'invalid_collection_switch':'automatic_collection must be true or false.',
        'invalid_collection_interval':f'interval_minutes must be {intervals}.',
        'invalid_handoff_grace_seconds':'handoff_grace_seconds must be a whole number from 10 to 600.',
        'invalid_initial_load_concurrency':'initial_load_concurrency must be a whole number from 1 to 8.',
        'invalid_youtube_request_concurrency':'youtube_request_concurrency must be a whole number from 1 to 50.',
        'conflicting_request_concurrency':'Keep youtube_request_concurrency and remove the old initial_load_concurrency setting.',
        'invalid_manual_stats_quota_units':'manual_stats_quota_units must be a whole number from 1 to 1000000.',
        'new_stats_exceeds_common_pool':'new_video_stats_limit must not exceed stats_limit.',
        'retired_operator_max_items':'operator_max_items is retired; remove its active line or keep it as a comment.',
        'settings_file_too_large':'The file must not be larger than 16 KB.',
        'unsafe_settings_file':'The file must be a regular file that only its owner can change.',
        'mixed_inventory_config':'Keep inventory_page_budget; old channel_limit and typed_inventory_limit cannot be combined with it.',
        'unsupported_config_version':'This config.ini format is not supported by this release.',
        'invalid_config_size':'The file must not be larger than 16 KB.',
    }.get(code,'The file must keep the [collection] and [limits] sections with the documented keys, one value each.')
