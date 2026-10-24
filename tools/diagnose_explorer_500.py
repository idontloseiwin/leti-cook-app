"""Read-only Channel Explorer diagnosis through configured direct-PG resources.

Run explicitly inside the configured backend environment. Reports contain stage
names, bounded counts and sanitized error codes, never rows or credentials.
Import and --help perform no runtime initialization. No collector or quota
recorder is started. Exit codes: 0 passed, 1 failed, 124 hard process deadline.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import threading
import traceback

TIME_LIMIT_SECONDS = 60
DEFAULT_CHANNEL = 'UCBJycsmduvYEL83R_U4JriQ'
_CHANNEL = re.compile(r'UC[A-Za-z0-9_-]{22}\Z', re.ASCII)
_LONG_STATUSES = frozenset({'ready', 'stale', 'incomplete', 'not_collected'})


def safe(error):
    code = getattr(error, 'code', None)
    if not (type(code) is int or isinstance(code, str)
            and len(code) <= 12 and code.replace('_', '').isalnum()):
        code = None
    frames = [f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
              for frame in traceback.extract_tb(error.__traceback__)
              if 'site-packages' not in frame.filename][-4:]
    return f'type={type(error).__name__} code={code} at={" < ".join(reversed(frames)) or "library"}'


def diagnose(repository, channel, explorer, cursor_values, state, *, emit=print):
    """Use only repository reads; the process owner owns resources and deadline."""
    failed = 0
    rows = []

    def stage(name, action):
        nonlocal failed
        state['stage'] = name
        try:
            emit(f'stage={name} ok {action()}')
        except Exception as error:
            failed += 1
            emit(f'stage={name} error {safe(error)}')

    def first_page():
        nonlocal rows
        rows = repository._inventory_page(channel, video_type='long', page_size=1000,
            cursor_published=None, cursor_video_id=None, strict=True)
        return f'rows={len(rows)} null_published={sum(row.get("published_at") is None for row in rows)}'

    def second_page():
        published, video_id = cursor_values(rows[-1])
        result = repository._inventory_page(channel, video_type='long', page_size=1000,
            cursor_published=published, cursor_video_id=video_id, strict=True)
        return f'rows={len(result)} cursor_published_null={published is None}'

    def totals():
        result = explorer.build_channel_format_totals(repository, channel)
        status = result['formats']['long']['status']
        return 'long_status=' + (status if status in _LONG_STATUSES else 'unknown')

    stage('load_channel', lambda: f'found={repository.load_channel(channel, columns=explorer.CHANNEL_COLUMNS) is not None}')
    stage('inventory_page_1', first_page)
    if len(rows) == 1000:
        stage('inventory_page_2_as_app', second_page)
    else:
        emit('stage=inventory_page_2 skipped fewer_than_1000_rows')
    stage('count_canonical_long', lambda: f'count={repository.count_canonical_inventory(channel, video_type="long")}')
    ids = [str(row['video_id']) for row in rows[:500]]
    stage('videos_batch_format_columns', lambda: f'ids={len(ids)} rows={len(repository.load_videos_by_ids(ids, columns=explorer.FORMAT_TOTAL_VIDEO_COLUMNS))}')
    stage('videos_batch_explorer_columns', lambda: f'ids={len(ids)} rows={len(repository.load_videos_by_ids(ids, columns=explorer.VIDEO_COLUMNS))}')
    stage('format_totals', totals)
    stage('explorer_long_limit_1', lambda: f'fields={len(explorer.build_channel_explorer(repository, channel, limit=1))}')
    emit(f'result failed_stages={failed}')
    return 1 if failed else 0


def _load_runtime():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
    from core import process_database_resources
    from db.reads.channel_explorer import ChannelExplorerRepository, _cursor_values
    import channel_explorer
    return process_database_resources, ChannelExplorerRepository, _cursor_values, channel_explorer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', default=os.environ.get('DIAGNOSE_CHANNEL', DEFAULT_CHANNEL))
    parser.add_argument('--time-limit-seconds', default=os.environ.get('DIAGNOSE_TIME_LIMIT_SECONDS', TIME_LIMIT_SECONDS))
    args = parser.parse_args(argv)
    if not isinstance(args.channel, str) or not _CHANNEL.fullmatch(args.channel):
        parser.error('diagnose_channel_invalid')
    try:
        limit = int(args.time_limit_seconds)
    except (TypeError, ValueError):
        parser.error('diagnose_time_limit_invalid')
    if not 1 <= limit <= TIME_LIMIT_SECONDS:
        parser.error('diagnose_time_limit_invalid')
    state = {'stage': 'startup'}

    def timed_out():
        # Final emergency bound; process exit closes its connections even if a
        # native socket read cannot return. Normal success/failure uses finally.
        print(f'stage={state["stage"]} timeout limit_seconds={limit}', flush=True)
        os._exit(124)

    timer = threading.Timer(limit, timed_out)
    timer.daemon = True
    timer.start()
    try:
        resources, repository_class, cursor_values, explorer = _load_runtime()
        with resources(record_quota=False) as database:
            repository = repository_class(database)
            return diagnose(repository, args.channel, explorer, cursor_values, state,
                            emit=lambda value: print(value, flush=True))
    except Exception as error:
        print(f'stage={state["stage"]} error {safe(error)}', flush=True)
        print('result failed_stages=1', flush=True)
        return 1
    finally:
        timer.cancel()


if __name__ == '__main__':
    raise SystemExit(main())
