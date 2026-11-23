"""Explicit process schedule handoff; file reads belong to backend startup only."""
from pathlib import Path

from collection_schedule import (
    CollectionSchedules, ScheduleError, deserialize_snapshot, read_schedule_file,
    serialize_snapshot, snapshot_digest,
)
from self_hosted_settings import SETTINGS_ENV

SNAPSHOT_ENV = 'LIC_COLLECTION_SCHEDULE_SNAPSHOT'
DIGEST_ENV = 'LIC_COLLECTION_SCHEDULE_SHA256'


def schedule_environment(schedules):
    """Send the captured nonsecret values, never a filename for a child to reread."""
    return {SNAPSHOT_ENV: serialize_snapshot(schedules).decode('ascii'),
            DIGEST_ENV: snapshot_digest(schedules)}


def backend_schedule_environment(environ):
    """Called once by the backend launcher, after health/check-settings dispatch."""
    path = environ.get(SETTINGS_ENV)
    if not isinstance(path, str) or not path:
        raise ScheduleError('schedule_path_required')
    schedules = read_schedule_file(Path(path).with_name('collection-schedules.ini'))
    return schedule_environment(schedules)


def load_process_schedules(environ, *, required=True):
    """Nonscheduling owner tools may omit both values; collectors require them."""
    raw, digest = environ.get(SNAPSHOT_ENV), environ.get(DIGEST_ENV)
    if not required and SNAPSHOT_ENV not in environ and DIGEST_ENV not in environ:
        return None
    if not isinstance(raw, str) or not isinstance(digest, str):
        raise ScheduleError('schedule_snapshot_required')
    try:
        payload = raw.encode('ascii')
    except UnicodeError:
        raise ScheduleError('schedule_snapshot_invalid') from None
    return deserialize_snapshot(payload, digest)


def require_schedule_model(schedules):
    if type(schedules) is not CollectionSchedules:
        raise ScheduleError('schedule_snapshot_required')
    return schedules
