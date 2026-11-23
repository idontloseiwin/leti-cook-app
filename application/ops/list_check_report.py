"""Print one anonymous operator observation report on stdout."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collection_formats import shown_formats
from db.base.client import fresh_database_gateway
from db.inventory.list_check_report import report
from runtime.collection_schedules import backend_schedule_environment, load_process_schedules


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError('Use an ISO timestamp including its UTC offset.') from None


def previous_week_end(now):
    midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight-timedelta(days=midnight.weekday())


def archive_week(directory, value):
    """Publish a complete aggregate file atomically, without overwriting an earlier export."""
    start, end = timestamp(value['start']), timestamp(value['end_exclusive'])
    if end-start != timedelta(days=7) or start.time() != datetime.min.time():
        raise ValueError('archive_week_invalid')
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"list-checks-{start.date()}--{end.date()}.json"
    temporary = directory / f'.list-checks-{uuid4().hex}.tmp'
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, allow_nan=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, dest)
        except FileExistsError:
            previous = json.loads(dest.read_text(encoding='utf-8'))
            if (not isinstance(previous, dict) or previous.keys() != value.keys()
                    or any(previous.get(key) != value[key] for key in ('version','start','end_exclusive'))):
                raise ValueError('archive_week_conflict') from None
    finally:
        temporary.unlink(missing_ok=True)
    return dest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', type=timestamp, help='Inclusive time (default: seven days before end)')
    parser.add_argument('--end', type=timestamp, help='Exclusive time (default: now)')
    parser.add_argument('--archive-week', type=Path, metavar='DIR',
                        help='Save a seven-day UTC aggregate. Default end: last Monday at 00:00 UTC.')
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    end = args.end or (previous_week_end(now) if args.archive_week else now)
    start = args.start or end-timedelta(days=7)
    try:
        if args.archive_week and (end-start != timedelta(days=7) or start.time() != datetime.min.time()):
            raise ValueError('archive_week_invalid')
        env = backend_schedule_environment(os.environ)
        schedules = load_process_schedules(env)
        with fresh_database_gateway(cadence_snapshot=schedules.cadence_snapshot()) as database:
            value = report(database, start=start, end=end, formats=shown_formats())
        if args.archive_week:
            print(archive_week(args.archive_week, value))
        else:
            print(json.dumps(value, allow_nan=False, indent=2))
    except ValueError as error:
        code = str(error)
        print(code if code in ('report_window_invalid','report_outside_observation_window','archive_week_invalid','archive_week_conflict')
              else 'list_check_report_configuration_unavailable', file=sys.stderr)
        return 1
    except Exception:
        print('list_check_report_unavailable', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
