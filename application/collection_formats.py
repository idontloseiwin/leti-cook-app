"""Video formats an installation collects and shows.

The owner sets one list, config.ini [collection] formats; a missing line means
DEFAULT_FORMATS, so Streams are off unless the owner adds them. The settings
parser validates the line like every other setting. Collection takes the list
from its pass environment (worker_environment writes FORMATS_ENV from the
file at the start of every pass), so a change applies to the next pass and a
running pass keeps the formats it started with. Pages, research reads and
agents take it from the file on every request, so they show the change on
their next load. Neither needs a restart.

The fixed internal flags SHORT_VIDEO_COLLECTION_ENABLED and
COMPLETED_STREAM_COLLECTION_ENABLED keep applying on top of this list in the
collection policy. A process without installation settings (development,
tests) keeps every format: the internal flags alone decide there.
Pure: no database, provider or credentials.
"""
from __future__ import annotations

import os

VIDEO_FORMATS = ('long', 'short', 'stream')
DEFAULT_FORMATS = ('long', 'short')
FORMATS_ENV = 'LIC_COLLECTION_FORMATS'
# Owner-facing names, used in refusals and the interface stub.
FORMAT_LABELS = {'long': 'Long-form videos', 'short': 'Shorts', 'stream': 'Streams'}
# The refusal code of a read or agent request for a turned-off format.
FORMAT_TURNED_OFF = 'format_turned_off'
# Only Streams can be turned off in this release; turning off
# Long-form or Shorts is not proven safe on every Long-only path. Only the
# owner's settings (self_hosted_settings) enforce this; collection code passes
# subsets of formats through these helpers.
REQUIRED_FORMATS = ('long', 'short')
REQUIRED_REFUSAL = 'collection_formats_require_long_and_short'


class FormatsError(ValueError):
    """Fixed codes only, never the supplied text."""


def parse_formats(value):
    """'stream, long' -> ('long', 'stream').

    Spaces around names and their order are normalised. Names are lower case;
    an unknown, empty or repeated name and an empty list are refused.
    """
    if not isinstance(value, str) or len(value) > 64:
        raise FormatsError('invalid_collection_formats')
    names = [name.strip() for name in value.split(',')]
    if any(name not in VIDEO_FORMATS for name in names) or len(set(names)) != len(names):
        raise FormatsError('invalid_collection_formats')
    return tuple(kind for kind in VIDEO_FORMATS if kind in names)


def checked_formats(value):
    """A settings value (list or tuple of names) in canonical order, or a refusal."""
    if (not isinstance(value, (list, tuple)) or not value
            or any(type(kind) is not str or kind not in VIDEO_FORMATS for kind in value)
            or len(set(value)) != len(value)):
        raise FormatsError('invalid_collection_formats')
    return tuple(kind for kind in VIDEO_FORMATS if kind in value)


def formats_text(formats):
    return ', '.join(checked_formats(formats))


def disabled_formats(formats):
    return tuple(kind for kind in VIDEO_FORMATS if kind not in formats)


_cache = {}


def _installation_formats(environ):
    """The config.ini list of a self-hosted installation; None outside one.

    An unreadable or invalid file keeps the default: collection is held until
    the file is corrected (check-settings names the problem), and pages must
    not show a format the owner may have turned off.
    """
    from self_hosted_settings import SETTINGS_ENV, SettingsError, configured_settings_path, read_settings_file
    if environ.get('LIC_DEPLOYMENT_PROFILE') != 'self-hosted':
        return None
    if not (environ.get(SETTINGS_ENV) or environ.get('LIC_SETUP_DIRECTORY')):
        return None
    try:
        path = configured_settings_path(environ)
        info = os.stat(path)
        key = (str(path), info.st_mtime_ns, info.st_size, info.st_ino)
        if key not in _cache:
            formats = tuple(read_settings_file(path)['formats'])
            _cache.clear()
            _cache[key] = formats
        return _cache[key]
    except (SettingsError, OSError, ValueError, KeyError, TypeError):
        return DEFAULT_FORMATS


def _environment_formats(environ):
    try:
        return parse_formats(environ[FORMATS_ENV])
    except FormatsError:
        return DEFAULT_FORMATS


def collection_formats(environ=None):
    """The formats one collection pass works on: its own snapshot first."""
    environ = os.environ if environ is None else environ
    if FORMATS_ENV in environ:
        return _environment_formats(environ)
    installed = _installation_formats(environ)
    return VIDEO_FORMATS if installed is None else installed


def shown_formats(environ=None):
    """The formats pages, research reads and agents show: the file first.

    The backend's own environment holds the snapshot of its start; the file
    is what the owner edits, so reads follow it without a restart.
    """
    environ = os.environ if environ is None else environ
    installed = _installation_formats(environ)
    if installed is not None:
        return installed
    if FORMATS_ENV in environ:
        return _environment_formats(environ)
    return VIDEO_FORMATS


def require_owner_formats(formats):
    """The owner's setting must keep long and short."""
    if any(kind not in formats for kind in REQUIRED_FORMATS):
        raise FormatsError(REQUIRED_REFUSAL)
    return formats
