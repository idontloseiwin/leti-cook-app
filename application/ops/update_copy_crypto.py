"""Root-only, network-free container helper for temporary update copies."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
# The CLI has the same basename as its library; prefer the library directory
# even when PYTHONPATH already includes it later than the entrypoint directory.
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

from update_copy_crypto import CopyIdentity, CopyLimits, create_copy, restore_copy, verify_copy
from update_copy_protocol import MAX_MARKER_BYTES, UpdateCopyError, parse_marker


def _root_file(path, limit, *, key=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        mode = stat.S_IMODE(value.st_mode)
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != 0
                or mode & 0o022 or (key and mode != 0o600) or value.st_size > limit):
            raise UpdateCopyError('update_copy_key_invalid' if key else 'update_copy_marker_invalid')
        with os.fdopen(fd, 'rb', buffering=0) as source:
            fd = None
            data = source.read(limit + 1)
            after = os.fstat(source.fileno())
            if (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise UpdateCopyError('update_copy_control_changed')
            if len(data) > limit:
                raise UpdateCopyError('update_copy_control_invalid')
            return data
    finally:
        if fd is not None:
            os.close(fd)


def _parser():
    result = argparse.ArgumentParser(description='Verify a temporary stopped PostgreSQL 17 copy.')
    result.add_argument('mode', choices=('create', 'verify', 'restore'))
    result.add_argument('--source')
    result.add_argument('--target')
    result.add_argument('--ciphertext', required=True)
    result.add_argument('--key-file', required=True)
    result.add_argument('--marker-file', required=True)
    result.add_argument('--copy-id', required=True)
    result.add_argument('--database-id', required=True)
    result.add_argument('--schema-version', required=True, type=int)
    result.add_argument('--max-bytes', required=True, type=int)
    result.add_argument('--max-files', required=True, type=int)
    return result


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise UpdateCopyError('update_copy_root_linux_required')
        if ((args.mode == 'create') != (args.source is not None)
                or (args.mode == 'restore') != (args.target is not None)):
            raise UpdateCopyError('update_copy_arguments_invalid')
        paths = (args.ciphertext, args.key_file, args.marker_file, args.source, args.target)
        if any(path is not None and (not Path(path).is_absolute() or '..' in Path(path).parts) for path in paths):
            raise UpdateCopyError('update_copy_arguments_invalid')
        key = _root_file(args.key_file, 32, key=True)
        marker = parse_marker(_root_file(args.marker_file, MAX_MARKER_BYTES))
        identity = CopyIdentity(args.copy_id, args.database_id, args.schema_version)
        limits = CopyLimits(args.max_bytes, args.max_files)
        if args.mode == 'create':
            proof = create_copy(args.source, args.ciphertext, key, marker, identity, limits)
        elif args.mode == 'restore':
            proof = restore_copy(args.ciphertext, args.target, key, marker, identity, limits)
        else:
            proof = verify_copy(args.ciphertext, key, marker, identity, limits)
        print(json.dumps(proof.as_dict(), sort_keys=True, separators=(',', ':')))
        return 0
    except UpdateCopyError as exc:
        print(json.dumps({'error': str(exc)}, separators=(',', ':')), file=sys.stderr)
        return 2
    except (OSError, ValueError, OverflowError):
        print('{"error":"update_copy_io_failed"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
