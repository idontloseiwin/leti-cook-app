"""Pure immutable schedule values shared by file validation and collection policy."""
from dataclasses import dataclass, field, fields

MAX_INTERVAL_SECONDS = 2_419_200
MAX_UNAVAILABLE_SECONDS = 2_505_600


class ScheduleError(ValueError):
    """Fixed codes only; never include configuration text in an exception."""


@dataclass(frozen=True, slots=True)
class RecheckSchedules:
    unavailable_long_max_seconds: int = MAX_INTERVAL_SECONDS
    identity_seconds: int = MAX_INTERVAL_SECONDS

    def __post_init__(self):
        for value in (self.unavailable_long_max_seconds, self.identity_seconds):
            if type(value) is not int or not 1 <= value <= MAX_INTERVAL_SECONDS:
                raise ScheduleError('schedule_interval_invalid')


@dataclass(frozen=True, slots=True)
class UnavailableSchedule:
    daily_seconds: int = 86_400
    daily_window_seconds: int = 604_800
    weekly_seconds: int = 604_800
    stop_after_seconds: int = MAX_UNAVAILABLE_SECONDS

    def __post_init__(self):
        for value in (self.daily_seconds, self.daily_window_seconds, self.weekly_seconds):
            if type(value) is not int or not 1 <= value <= MAX_INTERVAL_SECONDS:
                raise ScheduleError('schedule_interval_invalid')
        if (type(self.stop_after_seconds) is not int
                or not 1 <= self.stop_after_seconds <= MAX_UNAVAILABLE_SECONDS
                or self.daily_window_seconds > self.stop_after_seconds):
            raise ScheduleError('schedule_unavailable_window_invalid')


@dataclass(frozen=True, slots=True)
class UnavailableSchedules:
    long: UnavailableSchedule = field(default_factory=UnavailableSchedule)
    short: UnavailableSchedule = field(default_factory=UnavailableSchedule)
    stream: UnavailableSchedule = field(default_factory=UnavailableSchedule)

    def __post_init__(self):
        if any(type(getattr(self, kind)) is not UnavailableSchedule
               for kind in ('long', 'short', 'stream')):
            raise ScheduleError('schedule_model_invalid')


@dataclass(frozen=True, slots=True)
class InventorySchedule:
    """Per-format delays; claim eligibility and scan protocol stay code-owned."""
    missing_playlist_seconds: int = 1_814_400
    found_new_seconds: int = 3600
    discovery_continuation_seconds: int = 0
    reconciliation_seconds: int = 1_814_400
    quiet_1_seconds: int = 7200
    quiet_2_seconds: int = 14400
    quiet_3_seconds: int = 28800
    quiet_4_seconds: int = 86400
    quiet_5_seconds: int = 86400
    quiet_6_plus_seconds: int = 86400
    error_0_seconds: int = 3600
    error_1_seconds: int = 3600
    error_2_seconds: int = 7200
    error_3_seconds: int = 14400
    error_4_seconds: int = 28800
    error_5_plus_seconds: int = 86400

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            minimum = 0 if item.name == 'discovery_continuation_seconds' else 1
            if type(value) is not int or not minimum <= value <= MAX_INTERVAL_SECONDS:
                raise ScheduleError('schedule_interval_invalid')
        for names in (
            ('quiet_1_seconds', 'quiet_2_seconds', 'quiet_3_seconds',
             'quiet_4_seconds', 'quiet_5_seconds', 'quiet_6_plus_seconds'),
            ('error_0_seconds', 'error_1_seconds', 'error_2_seconds',
             'error_3_seconds', 'error_4_seconds', 'error_5_plus_seconds'),
        ):
            values = [getattr(self, name) for name in names]
            if values != sorted(values):
                raise ScheduleError('schedule_backoff_order_invalid')


def long_inventory_schedule():
    """Preserve the shipped Long delays when older files gain this section."""
    return InventorySchedule(missing_playlist_seconds=86400,
        discovery_continuation_seconds=1800, quiet_4_seconds=28800,
        quiet_5_seconds=28800, error_1_seconds=7200, error_2_seconds=14400,
        error_3_seconds=28800, error_4_seconds=57600)


@dataclass(frozen=True, slots=True)
class InventorySchedules:
    long: InventorySchedule = field(default_factory=long_inventory_schedule)
    short: InventorySchedule = field(default_factory=InventorySchedule)
    stream: InventorySchedule = field(default_factory=InventorySchedule)

    def __post_init__(self):
        if any(type(getattr(self, kind)) is not InventorySchedule
               for kind in ('long', 'short', 'stream')):
            raise ScheduleError('schedule_model_invalid')
