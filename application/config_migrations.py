"""Read config.ini and identify its format version, using only stdlib."""
from __future__ import annotations

import configparser

MAX_CONFIG_BYTES = 16384
CONFIG_VERSION = 3


class ConfigMigrationError(ValueError):
    """Fixed codes only: settings content must never appear in an error."""


def parse_config_bytes(raw: bytes) -> configparser.ConfigParser:
    if not isinstance(raw, bytes) or len(raw) > MAX_CONFIG_BYTES:
        raise ConfigMigrationError('invalid_config_size')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(raw.decode('utf-8'))
    except (UnicodeError, configparser.Error):
        raise ConfigMigrationError('invalid_config_format') from None
    if parser.defaults() or not {'collection', 'limits'} <= set(parser.sections()):
        raise ConfigMigrationError('invalid_config_sections')
    return parser


def config_version(parser: configparser.ConfigParser) -> int:
    if not parser.has_section('format'):
        return 1
    value = parser.get('format', 'version', fallback='')
    if value not in ('1', '2', '3'):
        raise ConfigMigrationError('unsupported_config_version')
    return int(value)
