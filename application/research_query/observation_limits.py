"""Which videos are refreshed often enough for an observed change window.

The limit follows the collection schedule the processes received (or the shipped
defaults): the oldest age up to which every counter age step refreshes at least
once per window. Older videos are not offered for that change at all, instead
of showing an empty value for most of them.
"""
from __future__ import annotations

import os

from collection_schedule import CollectionSchedules, ScheduleError, _LONG_COUNTER_BANDS

WINDOWS = (3600, 86400)


def process_schedules():
    from runtime.collection_schedules import load_process_schedules
    try:
        value = load_process_schedules(os.environ, required=False)
    except ScheduleError:
        value = None
    return value if value is not None else CollectionSchedules()


def age_limit_seconds(video_type: str, window_seconds: int, schedules: CollectionSchedules | None = None) -> int | None:
    """None means every age is refreshed at least once per window."""
    counters = getattr(schedules or process_schedules(), video_type + '_counters')
    limit = 0
    for key, _low, high in _LONG_COUNTER_BANDS:
        if getattr(counters, key) > window_seconds:
            return limit
        if high is None:
            return None
        limit = high
    return None


CHANGE_FIELDS = frozenset(('observation.views_delta', 'observation.views_per_hour'))


def change_age_limit(plan) -> int | None:
    """The limit a query applies: any reference to a change field (column, sort or condition)."""
    if not set(plan.referenced_fields) & CHANGE_FIELDS:
        return None
    return age_limit_seconds(plan.request.video_types[0], plan.request.observations.window_seconds)


def age_limits(video_types, schedules: CollectionSchedules | None = None) -> dict:
    schedules = schedules or process_schedules()
    return {kind: {str(window): age_limit_seconds(kind, window, schedules) for window in WINDOWS}
            for kind in video_types}
