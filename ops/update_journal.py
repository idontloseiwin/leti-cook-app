"""Durable update intent and root-owned filesystem transitions (host stdlib only)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import secrets
import stat

from application.release_version import parse_version
from application.update_copy_protocol import Marker, MAX_UTC_SECONDS
from ops import update_copy_store as store

JOURNAL_FILENAME = 'update-journal.json'
LOCK_FILENAME = 'update.lock'
HOLD_FILENAME = 'update-hold.json'
MAX_JOURNAL_BYTES = 16384
PHASES = frozenset({
    'preparing', 'prepared', 'hold_requested', 'drained', 'backend_stop_intent',
    'backend_stopped', 'db_stop_intent', 'db_stopped', 'copy_intent', 'copy_verified',
    'swap_intent', 'swapped', 'schema_intent', 'schema_applied', 'new_start_intent',
    'new_started', 'verified', 'rollback_intent', 'rollback_restore_intent',
    'rollback_restored', 'rollback_start_intent', 'rollback_started',
    'rollback_verified', 'rollback_cleanup', 'rolled_back', 'cleanup', 'complete', 'failed',
})
TERMINAL = frozenset({'complete', 'rolled_back'})
NULL_CANDIDATE_PHASES = frozenset({'preparing', 'failed', 'rollback_verified',
                                   'rollback_cleanup', 'rolled_back'})
FIELDS = frozenset({
    'format', 'version', 'operation_id', 'project', 'database_id', 'phase',
    'old_version', 'new_version', 'old_tree', 'new_tree', 'old_images', 'new_images',
    'old_volume', 'restore_volume', 'runtime_epoch', 'started_at', 'stopped_at',
    'copy', 'failure_code',
})
_IMMUTABLE = ('operation_id', 'project', 'database_id', 'old_version', 'new_version',
              'old_tree', 'old_images', 'old_volume', 'started_at')
_ONCE = ('new_tree', 'new_images', 'runtime_epoch', 'stopped_at', 'copy')
_HELD = ContextVar('leti_cook_update_lock', default=None)


class JournalError(ValueError):
    """Fixed diagnostic codes only; never include paths or secret contents."""


def _fail(code):
    raise JournalError(code)


def _check_layout(layout):
    store._validate_layout(layout)


def _op(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        _fail('update_operation_invalid')
    return value


def _integer(value, lower=0, upper=2**64 - 1):
    if type(value) is not int or not lower <= value <= upper:
        _fail('update_journal_invalid')
    return value


def _tree(value):
    if type(value) is not dict or set(value) != {'device', 'inode'}:
        _fail('update_tree_identity_invalid')
    return {'device': _integer(value['device']), 'inode': _integer(value['inode'], 1)}


def _images(value):
    if type(value) is not dict or set(value) != {'db', 'backend'}:
        _fail('update_images_invalid')
    if any(type(item) is not str or re.fullmatch(r'sha256:[0-9a-f]{64}', item) is None
           for item in value.values()):
        _fail('update_images_invalid')


def _volume(value, project):
    if (type(value) is not str or not value.startswith(project + '_')
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value) is None):
        _fail('update_volume_invalid')


def validate_journal(value, layout=None):
    """Return a detached strict JSON value; no filesystem or Docker effects."""
    if type(value) is not dict or set(value) != FIELDS:
        _fail('update_journal_invalid')
    if (value['format'] != 'leti-cook-update' or type(value['version']) is not int
            or value['version'] != 1 or type(value['phase']) is not str or value['phase'] not in PHASES):
        _fail('update_journal_invalid')
    _op(value['operation_id'])
    store._identity(value['project'], value['database_id'])
    if layout is not None and (value['project'], value['database_id']) != (layout.project, layout.database_id):
        _fail('update_journal_scope_mismatch')
    for name in ('old_version', 'new_version'):
        try:
            parse_version(value[name])
        except (TypeError, ValueError):
            _fail('update_version_invalid')
    _tree(value['old_tree'])
    _images(value['old_images'])
    for name, check in (('new_tree', _tree), ('new_images', _images)):
        if value[name] is None:
            if value['phase'] not in NULL_CANDIDATE_PHASES:
                _fail('update_candidate_identity_missing')
        else:
            check(value[name])
    if value['new_tree'] is not None and value['new_tree'] == value['old_tree']:
        _fail('update_tree_identity_invalid')
    _volume(value['old_volume'], value['project'])
    if value['restore_volume'] is not None:
        _volume(value['restore_volume'], value['project'])
        if value['restore_volume'] == value['old_volume']:
            _fail('update_volume_invalid')
    if value['runtime_epoch'] is not None:
        _op(value['runtime_epoch'])
    _integer(value['started_at'], 1, MAX_UTC_SECONDS)
    if value['stopped_at'] is not None:
        _integer(value['stopped_at'], value['started_at'], MAX_UTC_SECONDS)
    if value['copy'] is not None:
        item = value['copy']
        if type(item) is not dict or set(item) != {'schema_version', 'created_at', 'expires_at', 'max_bytes', 'max_files'}:
            _fail('update_copy_metadata_invalid')
        _integer(item['schema_version'], 1, 2147483647)
        _integer(item['max_bytes'], 2048, 1024**4)
        _integer(item['max_files'], 2, 1_000_000)
        try:
            Marker(item['created_at'], item['expires_at'])
        except ValueError:
            _fail('update_copy_metadata_invalid')
        if item['created_at'] < value['started_at']:
            _fail('update_copy_metadata_invalid')
    if value['failure_code'] is not None and (type(value['failure_code']) is not str
            or re.fullmatch(r'[a-z][a-z0-9_]{0,95}', value['failure_code']) is None):
        _fail('update_failure_code_invalid')
    data = store._json_bytes(value)
    if len(data) > MAX_JOURNAL_BYTES:
        _fail('update_journal_invalid')
    return json.loads(data)


def load_journal(layout):
    _check_layout(layout)
    path = layout.owner / JOURNAL_FILENAME
    try:
        content = store._read(path, limit=MAX_JOURNAL_BYTES)
    except FileNotFoundError:
        return None
    return validate_journal(store._json(content), layout)


def _require_lock(layout):
    held = _HELD.get()
    if held is None or held[0] != layout.owner:
        _fail('update_lock_required')
    info = os.fstat(held[1])
    path = layout.owner / LOCK_FILENAME
    current = path.lstat()
    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino) or info.st_nlink != 1:
        _fail('update_lock_changed')
    store._owner_mode(info, 0o600)


@contextmanager
def update_lock(layout):
    """Nonblocking, distinct from the short independent copy-purge lock."""
    _check_layout(layout)
    if _HELD.get() is not None:
        _fail('update_lock_busy')
    path = layout.owner / LOCK_FILENAME
    try:
        store._create_file(path, b'')
    except FileExistsError:
        pass
    store._read(path, limit=0)
    descriptor = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    token = None
    try:
        import fcntl
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail('update_lock_busy')
        token = _HELD.set((layout.owner, descriptor))
        _require_lock(layout)
        yield
    finally:
        if token is not None:
            _HELD.reset(token)
        os.close(descriptor)


def _atomic_write(directory, name, data, *, mode=0o600, gid=0):
    """Complete content reaches its fixed path only after file fsync."""
    target = directory / name
    if os.path.lexists(target):
        store._read(target, mode=mode, gid=gid, limit=MAX_JOURNAL_BYTES)
    temporary = directory / ('.' + name + '.' + secrets.token_hex(16) + '.tmp')
    store._create_file(temporary, data, mode=mode, gid=gid)
    try:
        os.replace(temporary, target)
        store._fsync_directory(directory)
    finally:
        if os.path.lexists(temporary):
            store._fixed_unlink(directory, temporary.name)


def save_journal(layout, value):
    _check_layout(layout)
    _require_lock(layout)
    value = validate_journal(value, layout)
    previous = load_journal(layout)
    starting = previous is None or previous['operation_id'] != value['operation_id']
    if starting:
        if previous is not None and previous['phase'] not in TERMINAL:
            _fail('update_operation_pending')
        if value['phase'] != 'preparing' or value['new_tree'] is not None or value['new_images'] is not None:
            _fail('update_initial_intent_required')
        if os.path.lexists(layout.root / 'candidate') or os.path.lexists(layout.marker_directory / HOLD_FILENAME):
            _fail('update_preparation_path_occupied')
        if any(os.path.lexists(path) for path in (layout.key_path, layout.cipher_path,
                layout.partial_path, layout.marker_path, layout.owner / store.OPERATION_FILENAME)):
            _fail('update_copy_operation_pending')
        if tree_identity(layout.installation) != value['old_tree']:
            _fail('update_tree_identity_mismatch')
    else:
        for name in _IMMUTABLE:
            if value[name] != previous[name]:
                _fail('update_journal_identity_changed')
        for name in _ONCE:
            if previous[name] is not None and value[name] != previous[name]:
                _fail('update_journal_identity_changed')
        if value['restore_volume'] != previous['restore_volume'] and not (
                previous['phase'] == 'rollback_restore_intent' and value['phase'] == 'rollback_restored'):
            _fail('update_restore_intent_required')
        if previous['phase'] in TERMINAL and value != previous:
            _fail('update_operation_complete')
    _atomic_write(layout.owner, JOURNAL_FILENAME, store._json_bytes(value))
    return value


def _journal_for_operation(layout, operation_id):
    _op(operation_id)
    journal = load_journal(layout)
    if journal is None or journal['operation_id'] != operation_id:
        _fail('update_operation_mismatch')
    return journal


def _hold(path):
    value = store._json(store._read(path, mode=0o640, gid=store.BACKEND_GID, limit=512))
    if type(value) is not dict or set(value) != {'operation_id'}:
        _fail('update_hold_invalid')
    _op(value['operation_id'])
    return value['operation_id']


def write_hold(layout, operation_id):
    _check_layout(layout)
    _require_lock(layout)
    journal = _journal_for_operation(layout, operation_id)
    if journal['phase'] != 'hold_requested':
        _fail('update_hold_intent_required')
    path = layout.marker_directory / HOLD_FILENAME
    if os.path.lexists(path):
        if _hold(path) != operation_id:
            _fail('update_hold_operation_mismatch')
        return False
    _atomic_write(layout.marker_directory, HOLD_FILENAME,
                  store._json_bytes({'operation_id': operation_id}), mode=0o640, gid=store.BACKEND_GID)
    return True


def remove_hold(layout, operation_id):
    _check_layout(layout)
    _require_lock(layout)
    _journal_for_operation(layout, operation_id)
    path = layout.marker_directory / HOLD_FILENAME
    if not os.path.lexists(path):
        return False
    if _hold(path) != operation_id:
        _fail('update_hold_operation_mismatch')
    store._fixed_unlink(layout.marker_directory, HOLD_FILENAME)
    return True


def tree_identity(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _fail('update_tree_invalid')
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        _fail('update_tree_permissions_invalid')
    return {'device': info.st_dev, 'inode': info.st_ino}


def _parents_same_device(layout):
    if layout.installation.parent.stat().st_dev != layout.root.stat().st_dev:
        _fail('update_exchange_different_filesystem')
    # os.path.ismount alone misses bind mounts with the same device. Mountinfo
    # is a kernel observation, not a shell command or candidate-controlled file.
    for path in (layout.installation, layout.root / 'candidate'):
        if path.exists():
            if tree_identity(path)['device'] != layout.root.stat().st_dev:
                _fail('update_exchange_different_filesystem')
            if os.path.ismount(path):
                _fail('update_tree_mountpoint')
            if os.name == 'posix':
                for line in Path('/proc/self/mountinfo').read_text(encoding='utf-8').splitlines():
                    fields = line.split(' ')
                    if len(fields) > 4 and fields[4] == path.as_posix():
                        _fail('update_tree_mountpoint')


def _rename_exchange(left, right):
    """Linux atomic exchange only. No non-atomic rename fallback."""
    if os.name != 'posix':
        _fail('update_exchange_unsupported')
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = library.renameat2
    except AttributeError:
        _fail('update_exchange_unsupported')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    descriptors = []
    try:
        for path in (left.parent, right.parent):
            descriptors.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        result = function(descriptors[0], os.fsencode(left.name), descriptors[1], os.fsencode(right.name), 2)
        if result != 0:
            error = ctypes.get_errno()
            if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV, errno.EBUSY):
                _fail('update_exchange_unsupported')
            raise OSError(error, 'update_exchange_failed')
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def preflight_exchange(layout):
    """Exercise the exact pair of parent filesystems before any service stops."""
    _check_layout(layout)
    _require_lock(layout)
    _parents_same_device(layout)
    token = secrets.token_hex(16)
    left = layout.installation.parent / ('.leti-cook-exchange-' + token)
    right = layout.root / ('.exchange-' + token)
    created = {}
    try:
        for path in (left, right):
            store._mkdir(path, 0o700)
            created[path] = tree_identity(path)
        identities = tuple(created.values())
        _rename_exchange(left, right)
        store._fsync_directory(left.parent)
        store._fsync_directory(right.parent)
        if tree_identity(left) != identities[1] or tree_identity(right) != identities[0]:
            _fail('update_exchange_identity_mismatch')
        return True
    finally:
        for path in created:
            if path.exists():
                observed = tree_identity(path)
                if observed not in created.values():
                    _fail('update_preflight_cleanup_identity_mismatch')
                path.rmdir()  # Empty private probe directory only, never recurse.
                store._fsync_directory(path.parent)


def tree_position(layout, old_tree, new_tree):
    _check_layout(layout)
    old_tree, new_tree = _tree(old_tree), _tree(new_tree)
    pair = (tree_identity(layout.installation), tree_identity(layout.root / 'candidate'))
    if pair == (old_tree, new_tree):
        return 'old'
    if pair == (new_tree, old_tree):
        return 'new'
    _fail('update_tree_identity_mismatch')


def exchange_trees(layout, old_tree, new_tree, *, desired='new'):
    _check_layout(layout)
    _require_lock(layout)
    if desired not in ('old', 'new'):
        _fail('update_exchange_direction_invalid')
    journal = load_journal(layout)
    expected_phase = 'swap_intent' if desired == 'new' else 'rollback_intent'
    if (journal is None or journal['phase'] != expected_phase
            or journal['old_tree'] != old_tree or journal['new_tree'] != new_tree):
        _fail('update_exchange_intent_required')
    _parents_same_device(layout)
    position = tree_position(layout, old_tree, new_tree)
    if position != desired:
        _rename_exchange(layout.installation, layout.root / 'candidate')
    # Also fsync after reconciling a lost ACK: the rename may have succeeded but
    # not reached the original call's directory fsync before interruption.
    store._fsync_directory(layout.installation.parent)
    store._fsync_directory(layout.root)
    if tree_position(layout, old_tree, new_tree) != desired:
        _fail('update_tree_identity_mismatch')
    return desired


def _mount_id(descriptor):
    # Device identity alone does not distinguish a bind mount of the same FS.
    for line in Path('/proc/self/fdinfo/' + str(descriptor)).read_text(encoding='ascii').splitlines():
        if line.startswith('mnt_id:'):
            value = line.split(':', 1)[1].strip()
            if value.isdecimal():
                return int(value)
    _fail('update_cleanup_mount_identity_unavailable')


def _remove_contents(directory_fd, device, mount):
    for name in os.listdir(directory_fd):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_dev != device:
            _fail('update_cleanup_mountpoint')
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    _fail('update_cleanup_identity_changed')
                if _mount_id(child) != mount:
                    _fail('update_cleanup_mountpoint')
                _remove_contents(child, device, mount)
                os.fsync(child)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    _fail('update_cleanup_identity_changed')
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)  # A leaf symlink is never followed.


def _remove_candidate(layout, expected_tree):
    path = layout.root / 'candidate'
    if not os.path.lexists(path):
        return False
    if tree_identity(path) != _tree(expected_tree):
        _fail('update_cleanup_identity_mismatch')
    if tree_identity(layout.installation) == expected_tree:
        _fail('update_cleanup_active_tree')
    if os.name != 'posix':
        _fail('update_cleanup_linux_required')
    _parents_same_device(layout)
    parent = os.open(layout.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        descriptor = os.open('candidate', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        if {'device': opened.st_dev, 'inode': opened.st_ino} != expected_tree:
            _fail('update_cleanup_identity_changed')
        mount = _mount_id(parent)
        if _mount_id(descriptor) != mount:
            _fail('update_cleanup_mountpoint')
        _remove_contents(descriptor, opened.st_dev, mount)
        os.fsync(descriptor)
        current = os.stat('candidate', dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            _fail('update_cleanup_identity_changed')
        os.rmdir('candidate', dir_fd=parent)
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
    return True


def remove_tree(layout, expected_tree):
    """Remove only the recorded retired/candidate root, never an arbitrary path."""
    _check_layout(layout)
    _require_lock(layout)
    expected_tree = _tree(expected_tree)
    journal = load_journal(layout)
    if journal is None or expected_tree not in (journal['old_tree'], journal['new_tree']):
        _fail('update_cleanup_identity_unrecorded')
    if journal['phase'] not in ('cleanup', 'rollback_cleanup'):
        _fail('update_cleanup_intent_required')
    return _remove_candidate(layout, expected_tree)


def cleanup_preparing_candidate(layout):
    """Only the durable initial absent-path intent permits an unpinned partial."""
    _check_layout(layout)
    _require_lock(layout)
    journal = load_journal(layout)
    if journal is None or journal['phase'] != 'preparing' or journal['new_tree'] is not None:
        _fail('update_preparing_cleanup_refused')
    candidate = layout.root / 'candidate'
    if not os.path.lexists(candidate):
        return False
    return _remove_candidate(layout, tree_identity(candidate))
