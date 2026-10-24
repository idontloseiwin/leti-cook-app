"""Finite, immutable schedule-file values and bounded local validation.

The managed process owner reads the file and captures this model once. Existing
queue deadlines are not rewritten. Child digests detect changed transfer bytes;
they are not signatures or database configuration generations. Selection and
clock algorithms remain in collection_policy.
"""
from __future__ import annotations

import configparser
from dataclasses import asdict, dataclass, field, fields
import hashlib
import hmac
import json
import re

from collection_schedule_model import (
    MAX_INTERVAL_SECONDS, RecheckSchedules, ScheduleError, UnavailableSchedule,
    UnavailableSchedules, InventorySchedule, InventorySchedules, long_inventory_schedule,
)

FORMAT_VERSION = 4
MAX_SCHEDULE_BYTES = 16_384


def _intervals(value):
    for item in fields(value):
        if item.name == 'jitter_seconds':
            continue
        number = getattr(value, item.name)
        if type(number) is not int or not 1 <= number <= MAX_INTERVAL_SECONDS:
            raise ScheduleError('schedule_interval_invalid')


@dataclass(frozen=True, slots=True)
class LongCounters:
    age_0_1d_seconds: int = 3600
    age_1_7d_seconds: int = 7200
    age_7_28d_seconds: int = 21600
    age_28_90d_seconds: int = 43200
    age_90_365d_seconds: int = 86400
    age_365d_plus_seconds: int = 604800
    unlisted_seconds: int = MAX_INTERVAL_SECONDS

    def __post_init__(self):
        _intervals(self)


@dataclass(frozen=True, slots=True)
class LongMetadata:
    age_0_7d_seconds: int = 21600
    age_7_28d_seconds: int = 86400
    age_28_365d_seconds: int = 604800
    age_365d_plus_seconds: int = MAX_INTERVAL_SECONDS
    unlisted_seconds: int = MAX_INTERVAL_SECONDS

    def __post_init__(self):
        _intervals(self)


@dataclass(frozen=True, slots=True)
class TypedMetadata:
    age_through_90d_seconds: int = 604800
    age_over_90d_seconds: int = MAX_INTERVAL_SECONDS
    jitter_seconds: int = 21600

    def __post_init__(self):
        _intervals(self)
        if (type(self.jitter_seconds) is not int
                or not 0 <= self.jitter_seconds <= min(
                    self.age_through_90d_seconds, self.age_over_90d_seconds)):
            raise ScheduleError('schedule_jitter_invalid')


@dataclass(frozen=True, slots=True)
class CollectionSchedules:
    long_counters: LongCounters = field(default_factory=LongCounters)
    long_metadata: LongMetadata = field(default_factory=LongMetadata)
    short_metadata: TypedMetadata = field(default_factory=TypedMetadata)
    stream_metadata: TypedMetadata = field(default_factory=TypedMetadata)
    rechecks: RecheckSchedules = field(default_factory=RecheckSchedules)
    short_counters: LongCounters = field(default_factory=LongCounters)
    stream_counters: LongCounters = field(default_factory=LongCounters)
    long_unavailable: UnavailableSchedule = field(default_factory=UnavailableSchedule)
    short_unavailable: UnavailableSchedule = field(default_factory=UnavailableSchedule)
    stream_unavailable: UnavailableSchedule = field(default_factory=UnavailableSchedule)
    long_inventory: InventorySchedule = field(default_factory=long_inventory_schedule)
    short_inventory: InventorySchedule = field(default_factory=InventorySchedule)
    stream_inventory: InventorySchedule = field(default_factory=InventorySchedule)

    def __post_init__(self):
        for _, attribute, record_type in _SECTIONS:
            if type(getattr(self, attribute)) is not record_type:
                raise ScheduleError('schedule_model_invalid')

    def cadence_snapshot(self):
        """Compile the fixed30-rule vocabulary, preserving existing metadata."""
        from collection_policy import CadenceRule, CadenceSnapshot

        rules = []
        for kind, values, bands in (
            ('counters', self.long_counters, _LONG_COUNTER_BANDS),
            ('metadata', self.long_metadata, _LONG_METADATA_BANDS),
        ):
            for key, minimum, maximum in bands:
                rules.append(CadenceRule('long', kind, 'public', minimum, maximum,
                    getattr(values, key), 1, True, None, 0))
            rules.append(CadenceRule('long', kind, 'unlisted', 0, None,
                values.unlisted_seconds, 1, True, None, 0))
        for kind, values in (('short', self.short_metadata), ('stream', self.stream_metadata)):
            rules.append(CadenceRule(kind, 'metadata', 'all', 0, 7776000,
                values.age_through_90d_seconds, 1, True, 'age_0_90d', values.jitter_seconds))
            rules.append(CadenceRule(kind, 'metadata', 'all', 7776000, None,
                values.age_over_90d_seconds, 1, True, 'age_91d_plus', values.jitter_seconds))
        for kind, values in (('short', self.short_counters), ('stream', self.stream_counters)):
            for key, minimum, maximum in _LONG_COUNTER_BANDS:
                rules.append(CadenceRule(kind, 'counters', 'public', minimum, maximum,
                    getattr(values, key), 1, True, None, 0))
            rules.append(CadenceRule(kind, 'counters', 'unlisted', 0, None,
                values.unlisted_seconds, 1, True, None, 0))
        return CadenceSnapshot(tuple(rules), rechecks=self.rechecks,
            unavailable_by_type=UnavailableSchedules(
                self.long_unavailable, self.short_unavailable, self.stream_unavailable),
            inventory_by_type=InventorySchedules(
                self.long_inventory, self.short_inventory, self.stream_inventory))


_V2_SECTIONS = (
    ('schedule.long.counters', 'long_counters', LongCounters),
    ('schedule.long.metadata', 'long_metadata', LongMetadata),
    ('schedule.short.metadata', 'short_metadata', TypedMetadata),
    ('schedule.stream.metadata', 'stream_metadata', TypedMetadata),
    ('schedule.rechecks', 'rechecks', RecheckSchedules),
)
_V3_SECTIONS = _V2_SECTIONS + (
    ('schedule.short.counters', 'short_counters', LongCounters),
    ('schedule.stream.counters', 'stream_counters', LongCounters),
    ('schedule.long.unavailable', 'long_unavailable', UnavailableSchedule),
    ('schedule.short.unavailable', 'short_unavailable', UnavailableSchedule),
    ('schedule.stream.unavailable', 'stream_unavailable', UnavailableSchedule),
)
_SECTIONS = _V3_SECTIONS + (
    ('schedule.long.inventory', 'long_inventory', InventorySchedule),
    ('schedule.short.inventory', 'short_inventory', InventorySchedule),
    ('schedule.stream.inventory', 'stream_inventory', InventorySchedule),
)
_VERSION_SECTIONS = {'2': _V2_SECTIONS, '3': _V3_SECTIONS, str(FORMAT_VERSION): _SECTIONS}
_LONG_COUNTER_BANDS = (
    ('age_0_1d_seconds', 0, 86400),
    ('age_1_7d_seconds', 86400, 604800),
    ('age_7_28d_seconds', 604800, 2419200),
    ('age_28_90d_seconds', 2419200, 7776000),
    ('age_90_365d_seconds', 7776000, 31536000),
    ('age_365d_plus_seconds', 31536000, None),
)
_LONG_METADATA_BANDS = (
    ('age_0_7d_seconds', 0, 604800),
    ('age_7_28d_seconds', 604800, 2419200),
    ('age_28_365d_seconds', 2419200, 31536000),
    ('age_365d_plus_seconds', 31536000, None),
)


def _bounded_bytes(raw):
    if type(raw) is not bytes or len(raw) > MAX_SCHEDULE_BYTES:
        raise ScheduleError('schedule_bytes_invalid')


def parse_schedule_file(raw: bytes) -> CollectionSchedules:
    """Read strict v4 or extend strict installed v2/v3 with only new defaults."""
    _bounded_bytes(raw)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # The finite key vocabulary is case sensitive.
    try:
        text = raw.decode('utf-8')
        if '\x00' in text or re.search(r'(?m)^\s*\[DEFAULT\]', text):
            raise ScheduleError('schedule_format_invalid')
        parser.read_string(text)
    except (UnicodeError, configparser.Error):
        raise ScheduleError('schedule_format_invalid') from None
    if ('format' not in parser or set(parser['format']) != {'version'}
            or parser['format']['version'] not in _VERSION_SECTIONS):
        raise ScheduleError('schedule_version_invalid')
    sections = _VERSION_SECTIONS[parser['format']['version']]
    if parser.defaults() or set(parser.sections()) != {'format', *(s[0] for s in sections)}:
        raise ScheduleError('schedule_sections_invalid')
    values = {}
    for section, attribute, record_type in sections:
        data = parser[section]
        if set(data) != {item.name for item in fields(record_type)}:
            raise ScheduleError('schedule_fields_invalid')
        numbers = {}
        for name, raw_number in data.items():
            if re.fullmatch(r'[0-9]{1,7}', raw_number) is None:
                raise ScheduleError('schedule_integer_invalid')
            numbers[name] = int(raw_number)
        values[attribute] = record_type(**numbers)
    return CollectionSchedules(**values)


def serialize_snapshot(value: CollectionSchedules) -> bytes:
    """Bounded canonical nonsecret child payload, with no clocks or DB revision."""
    if type(value) is not CollectionSchedules:
        raise ScheduleError('schedule_model_invalid')
    payload = {'format': FORMAT_VERSION, 'schedules': {
        section: asdict(getattr(value, attribute)) for section, attribute, _ in _SECTIONS}}
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def snapshot_digest(value: CollectionSchedules) -> str:
    return hashlib.sha256(serialize_snapshot(value)).hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ScheduleError('schedule_snapshot_invalid')
        value[key] = item
    return value


def deserialize_snapshot(raw: bytes, expected_digest: str) -> CollectionSchedules:
    """Validate canonical parent bytes; never reload a file or supply defaults."""
    _bounded_bytes(raw)
    if (type(expected_digest) is not str or re.fullmatch(r'[0-9a-f]{64}', expected_digest) is None
            or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_digest)):
        raise ScheduleError('schedule_snapshot_digest_mismatch')
    try:
        payload = json.loads(raw.decode('ascii'), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise ScheduleError('schedule_snapshot_invalid') from None
    if (type(payload) is not dict or set(payload) != {'format', 'schedules'}
            or type(payload['format']) is not int or payload['format'] != FORMAT_VERSION
            or type(payload['schedules']) is not dict
            or set(payload['schedules']) != {s[0] for s in _SECTIONS}):
        raise ScheduleError('schedule_snapshot_invalid')
    values = {}
    for section, attribute, record_type in _SECTIONS:
        data = payload['schedules'][section]
        if type(data) is not dict or set(data) != {item.name for item in fields(record_type)}:
            raise ScheduleError('schedule_snapshot_invalid')
        values[attribute] = record_type(**data)
    value = CollectionSchedules(**values)
    if serialize_snapshot(value) != raw:
        raise ScheduleError('schedule_snapshot_invalid')
    return value


def read_schedule_file(path) -> CollectionSchedules:
    """Read one regular nonsecret file; no fallback or shared mutable cache."""
    import os
    from pathlib import Path
    import stat

    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat() if item.exists() or item.is_symlink() else None
        if (item.is_symlink() or (info is not None and
                getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))):
            raise ScheduleError('schedule_link_refused')
    # O_NONBLOCK keeps a raced FIFO from blocking before the fstat check.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (os.name != 'nt' and info.st_mode & 0o022)):
            raise ScheduleError('schedule_file_unsafe')
        raw = stream.read(MAX_SCHEDULE_BYTES + 1)
    return parse_schedule_file(raw)


def schedule_problem(code: str) -> str:
    """Fixed admin guidance; never return file content or an unknown error."""
    return {
        'schedule_link_refused': 'The schedule file and its parent directories must not be links.',
        'schedule_file_unsafe': 'The schedule file must be a single regular file, not writable by other users.',
        'schedule_bytes_invalid': 'The schedule file must be UTF-8 and at most 16384 bytes.',
        'schedule_format_invalid': 'Use the collection-schedules.ini template with no duplicate sections or keys.',
        'schedule_sections_invalid': 'Use exactly the schedule sections for the selected format version.',
        'schedule_fields_invalid': 'Every schedule section must contain exactly the template settings.',
        'schedule_version_invalid': 'Use [format] version = 4, or an unchanged version 2 or 3 file.',
        'schedule_integer_invalid': 'Schedule intervals and jitter must be whole seconds.',
        'schedule_interval_invalid': 'Intervals must be 1 to 2419200 seconds; inventory continuation may also be 0.',
        'schedule_jitter_invalid': 'Jitter must be between 0 and both intervals for its video type.',
        'schedule_unavailable_window_invalid': 'The unavailable retry window must fit within 29 days and include its daily period.',
        'schedule_backoff_order_invalid': 'Inventory quiet and error delays must not decrease at later steps.',
    }.get(code, 'The schedule file could not be validated.')
