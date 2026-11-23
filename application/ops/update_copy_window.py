"""Owner-launched fixed source-clock observation. No provider or cipher key."""
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def collect_window(environ=None, *, repository_factory=None):
    from db.base.client import load_database_config
    from db.hot.hot_runtime import maintenance_repository
    from runtime.collection_schedules import backend_schedule_environment, load_process_schedules, SNAPSHOT_ENV, DIGEST_ENV
    from hot_runtime import maintenance_access
    values = dict(os.environ if environ is None else environ)
    if values.get('LIC_DEPLOYMENT_PROFILE') != 'self-hosted':
        raise ValueError('update_copy_profile_invalid')
    if SNAPSHOT_ENV not in values and DIGEST_ENV not in values:
        values.update(backend_schedule_environment(values))
    schedules = load_process_schedules(values)
    factory = repository_factory or maintenance_repository
    with factory(load_database_config(values), cadence_snapshot=schedules.cadence_snapshot()) as repository:
        with maintenance_access():
            repository.contract()
            return repository.update_copy_window()


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv):
        return 2
    try:
        value = collect_window()
    except Exception:
        print(json.dumps({'code': 'update_copy_window_unavailable'}))
        return 1
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
