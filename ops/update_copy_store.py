"""Root-owned, installation-independent lifetime store for temporary update copies.

All cleanup names are fixed. This module is copied with its pure protocol beside
an immutable scope receipt; the timer never imports the replaceable installation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid

# The installed purge helper loads only the adjacent root-owned protocol copy.
if __name__ == '__main__':
    _protocol_path = Path(__file__).absolute().parent / 'update_copy_protocol.py'
    _spec = importlib.util.spec_from_file_location('_leti_copy_protocol', _protocol_path)
    _protocol = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _protocol
    _spec.loader.exec_module(_protocol)
    Marker = _protocol.Marker
    parse_marker = _protocol.parse_marker
    marker_bytes = _protocol.marker_bytes
    UpdateCopyError = _protocol.UpdateCopyError
    MARKER_FILENAME = _protocol.MARKER_FILENAME
    CIPHER_FILENAME = _protocol.CIPHER_FILENAME
    PARTIAL_FILENAME = _protocol.PARTIAL_FILENAME
    KEY_FILENAME = _protocol.KEY_FILENAME
else:
    from application.update_copy_protocol import (
        Marker, parse_marker, marker_bytes, UpdateCopyError, MARKER_FILENAME,
        CIPHER_FILENAME, PARTIAL_FILENAME, KEY_FILENAME,
    )

SYSTEMD_DIRECTORY = Path('/etc/systemd/system')
SCOPE_FILENAME = 'scope.json'
OPERATION_FILENAME = 'operation.json'
HELPER_FILENAME = 'purge-helper.py'
PROTOCOL_FILENAME = 'update_copy_protocol.py'
LOCK_FILENAME = 'store.lock'
BACKEND_GID = 10001
MAX_RECEIPT_BYTES = 8192


@dataclass(frozen=True)
class StoreLayout:
    installation: Path
    project: str
    database_id: str
    # A test-only override; production and the independent helper use the fixed
    # systemd directory. It is deliberately absent from the persisted receipt.
    systemd_directory: Path = SYSTEMD_DIRECTORY

    @property
    def root(self):
        return self.installation.with_name(self.installation.name + '.update')

    @property
    def keys(self):
        return self.installation.with_name(self.installation.name + '.update-keys')

    @property
    def owner(self):
        return self.root / 'owner'

    @property
    def marker_directory(self):
        return self.root / 'marker'

    @property
    def cipher_directory(self):
        return self.root / 'cipher'

    @property
    def marker_path(self):
        return self.marker_directory / MARKER_FILENAME

    @property
    def key_path(self):
        return self.keys / KEY_FILENAME

    @property
    def cipher_path(self):
        return self.cipher_directory / CIPHER_FILENAME

    @property
    def partial_path(self):
        return self.cipher_directory / PARTIAL_FILENAME


@dataclass(frozen=True)
class CopyReservation:
    layout: StoreLayout
    copy_id: str
    schema_version: int
    marker: Marker

    @property
    def key_path(self):
        return self.layout.key_path

    @property
    def cipher_path(self):
        return self.layout.cipher_path

    @property
    def partial_path(self):
        return self.layout.partial_path

    @property
    def marker_path(self):
        return self.layout.marker_path


def _fail(code):
    raise UpdateCopyError(code)


def _require_root():
    if os.name != 'posix' or os.geteuid() != 0:
        _fail('update_copy_root_required')


def _chown(path, uid, gid):
    os.chown(path, uid, gid, follow_symlinks=False)


def _owner_mode(info, mode, gid=0):
    if info.st_uid != 0 or info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode:
        _fail('update_copy_permissions_invalid')


def _directory(path, mode, gid=0):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _fail('update_copy_path_invalid')
    _owner_mode(info, mode, gid)
    return info


def _safe_ancestors(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            _fail('update_copy_path_invalid')
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            _fail('update_copy_parent_untrusted')


def _installation_path(value):
    path = Path(value)
    if (not path.is_absolute() or path == path.parent or path.name in ('', '.', '..')
            or any(part in ('.', '..') for part in path.parts)
            or any(char.isspace() or char in '%\"\'`$;\\' for char in path.as_posix())
            or path != Path(os.path.abspath(path))):
        _fail('update_copy_path_invalid')
    # I itself may be absent during atomic replacement. Only stable ancestors
    # are a prerequisite; neither scope loading nor purge reads I's contents.
    _safe_ancestors(path.parent)
    return path


def _identity(project, database_id):
    if type(project) is not str or re.fullmatch(r'leti-cook-[0-9a-f]{12}', project) is None:
        _fail('update_copy_project_invalid')
    try:
        parsed = uuid.UUID(database_id)
        if parsed.int == 0 or str(parsed) != database_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _fail('update_copy_database_identity_invalid')


def _copy_id(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        _fail('update_copy_identity_invalid')
    return value


def _schema_version(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        _fail('update_copy_schema_invalid')
    return value


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('update_copy_receipt_invalid')
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        _fail('update_copy_receipt_invalid')


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                         | getattr(os, 'O_NOFOLLOW', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path, *, mode=0o600, gid=0, limit=MAX_RECEIPT_BYTES):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _fail('update_copy_file_invalid')
    _owner_mode(info, mode, gid)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            _fail('update_copy_file_changed')
        if opened.st_size > limit:
            _fail('update_copy_file_invalid')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            _fail('update_copy_file_invalid')
        return data
    finally:
        os.close(descriptor)


def _create_file(path, data, *, mode=0o600, gid=0):
    """Exclusive fixed-name write: even a failed secret write stays timer-covered."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), mode)
    try:
        _chown(path, 0, gid)
        os.chmod(path, mode, follow_symlinks=False)
        with os.fdopen(descriptor, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _mkdir(path, mode, gid=0):
    path.mkdir(mode=mode)
    _chown(path, 0, gid)
    path.chmod(mode)
    _fsync_directory(path.parent)


def _stamp(path):
    info = path.lstat()
    return {'device': info.st_dev, 'inode': info.st_ino}


def _directories(layout):
    return ((layout.root, 0o700, 0), (layout.keys, 0o700, 0),
            (layout.owner, 0o700, 0), (layout.marker_directory, 0o750, BACKEND_GID),
            (layout.cipher_directory, 0o730, BACKEND_GID))


def _scope(layout):
    return {'format': 'leti-cook-update-scope', 'version': 1,
            'installation': str(layout.installation), 'project': layout.project,
            'database_id': layout.database_id,
            'directories': {name: _stamp(path) for name, path in (
                ('root', layout.root), ('keys', layout.keys), ('owner', layout.owner),
                ('marker', layout.marker_directory), ('cipher', layout.cipher_directory))}}


def _validate_layout(layout):
    _require_root()
    _identity(layout.project, layout.database_id)
    if _installation_path(layout.installation) != layout.installation:
        _fail('update_copy_scope_invalid')
    for path, mode, gid in _directories(layout):
        _directory(path, mode, gid)
    receipt = _json(_read(layout.owner / SCOPE_FILENAME))
    if _json_bytes(receipt) != _json_bytes(_scope(layout)):
        _fail('update_copy_scope_invalid')


def validate_new_layout(installation):
    """Read-only preflight, usable before creating a new installation tree."""
    _require_root()
    path = _installation_path(installation)
    if path.is_symlink():
        _fail('update_copy_path_invalid')
    for sibling in (path.with_name(path.name + '.update'),
                    path.with_name(path.name + '.update-keys')):
        if os.path.lexists(sibling):
            _fail('update_copy_scope_exists')
    return path


def prepare_layout(installation, project, database_id, *, _systemd_directory=SYSTEMD_DIRECTORY):
    _require_root()
    _identity(project, database_id)
    path = _installation_path(installation)
    layout = StoreLayout(path, project, database_id, Path(_systemd_directory))
    if os.path.lexists(layout.root) or os.path.lexists(layout.keys):
        _validate_layout(layout)
        return layout
    validate_new_layout(path)
    for directory, mode, gid in _directories(layout):
        _mkdir(directory, mode, gid)
    _create_file(layout.owner / LOCK_FILENAME, b'')
    _create_file(layout.owner / HELPER_FILENAME, Path(__file__).read_bytes())
    protocol_source = Path(__file__).resolve().parents[1] / 'application' / PROTOCOL_FILENAME
    _create_file(layout.owner / PROTOCOL_FILENAME, protocol_source.read_bytes())
    # Receipt is last: an interrupted partial preparation is never adopted.
    _create_file(layout.owner / SCOPE_FILENAME, _json_bytes(_scope(layout)))
    _validate_layout(layout)
    return layout


def load_layout(installation, project, database_id, *, _systemd_directory=SYSTEMD_DIRECTORY):
    layout = StoreLayout(_installation_path(installation), project, database_id,
                         Path(_systemd_directory))
    _validate_layout(layout)
    return layout


@contextmanager
def _metadata_lock(layout):
    """No waiting on the long-running updater; timer retries short contention."""
    _read(layout.owner / LOCK_FILENAME, limit=0)
    descriptor = os.open(layout.owner / LOCK_FILENAME, os.O_RDWR
                         | getattr(os, 'O_NOFOLLOW', 0))
    try:
        import fcntl
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail('update_copy_store_busy')
        yield
    finally:
        os.close(descriptor)


def _now_value(now):
    value = time.time() if now is None else now
    if type(value) not in (float, int) or not math.isfinite(value):
        _fail('update_copy_clock_invalid')
    return value


def _live(marker, now):
    value = _now_value(now)
    if value < marker.created_at:
        _fail('update_copy_clock_invalid')
    if value >= marker.expires_at:
        _fail('update_copy_expired')


def _operation_value(reservation):
    return {'format': 'leti-cook-update-copy', 'version': 1,
            'copy_id': reservation.copy_id, 'schema_version': reservation.schema_version,
            'database_id': reservation.layout.database_id,
            'created_at': reservation.marker.created_at, 'expires_at': reservation.marker.expires_at}


def _reservation(layout):
    value = _json(_read(layout.owner / OPERATION_FILENAME))
    if type(value) is not dict or set(value) != {
            'format', 'version', 'copy_id', 'schema_version', 'database_id', 'created_at', 'expires_at'}:
        _fail('update_copy_receipt_invalid')
    reservation = CopyReservation(layout, _copy_id(value['copy_id']),
                                  _schema_version(value['schema_version']),
                                  Marker(value['created_at'], value['expires_at']))
    if value != _operation_value(reservation) or type(value['version']) is not int:
        _fail('update_copy_receipt_invalid')
    return reservation


def _unit_name(copy_id):
    return 'leti-cook-copy-' + _copy_id(copy_id)


def _unit_data(reservation):
    layout = reservation.layout
    name = _unit_name(reservation.copy_id)
    stamp = datetime.fromtimestamp(reservation.marker.expires_at, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    helper = (layout.owner / HELPER_FILENAME).as_posix()
    service = (f'[Unit]\nDescription=Remove expired Leti Cook update copy\nStartLimitIntervalSec=0\n'
               f'[Service]\nType=oneshot\nUser=root\nGroup=root\n'
               f'ExecStart=/usr/bin/python3 -I -B {helper} --copy-id {reservation.copy_id}\n'
               f'Restart=on-failure\nRestartSec=5s\nTimeoutStartSec=45s\n')
    timer = (f'[Unit]\nDescription=Leti Cook update copy expiry\n[Timer]\n'
             f'OnCalendar={stamp}\nAccuracySec=1s\nRandomizedDelaySec=0\nPersistent=true\n'
             f'Unit={name}.service\n[Install]\nWantedBy=timers.target\n')
    return {name + '.service': service.encode('utf-8'), name + '.timer': timer.encode('utf-8')}


def _systemctl(arguments):
    subprocess.run(['/usr/bin/systemctl', *arguments], check=True, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, timeout=30)


def arm_copy(layout, *, copy_id, marker, schema_version, run_systemctl=_systemctl, now=None):
    """Arm durable independent expiry before creating any new encryption key.

    ``now`` is an offline-test clock override; production leaves it unset so
    checks after systemd/filesystem calls observe elapsed wall-clock time.
    """
    _validate_layout(layout)
    reservation = CopyReservation(layout, _copy_id(copy_id), _schema_version(schema_version),
                                  parse_marker(marker_bytes(marker)))
    _live(marker, now)
    with _metadata_lock(layout):
        for path in (layout.owner / OPERATION_FILENAME, layout.marker_path,
                     layout.key_path, layout.cipher_path, layout.partial_path):
            if os.path.lexists(path):
                _fail('update_copy_operation_exists')
        _safe_ancestors(layout.systemd_directory)
        units = _unit_data(reservation)
        for name in units:
            if os.path.lexists(layout.systemd_directory / name):
                _fail('update_copy_unit_exists')
        _create_file(layout.owner / OPERATION_FILENAME, _json_bytes(_operation_value(reservation)))
        _create_file(layout.marker_path, marker_bytes(marker), mode=0o640, gid=BACKEND_GID)
        for name, data in units.items():
            _create_file(layout.systemd_directory / name, data, mode=0o644)
        run_systemctl(['daemon-reload'])
        run_systemctl(['enable', '--now', _unit_name(copy_id) + '.timer'])
        run_systemctl(['is-active', '--quiet', _unit_name(copy_id) + '.timer'])
        _live(marker, now)
        _create_file(layout.key_path, secrets.token_bytes(32))
        _live(marker, now)
    return reservation


def _fixed_unlink(directory, name):
    """Unlink a fixed entry without following it; never recurse into anything."""
    path = directory / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        _fail('update_copy_cleanup_blocked')
    if os.name == 'posix':
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    else:  # Only reached in filesystem unit tests; entry points require Linux root.
        path.unlink()
        _fsync_directory(directory)
    if os.path.lexists(path):
        _fail('update_copy_cleanup_incomplete')


def _remove_units(reservation, run_systemctl):
    layout = reservation.layout
    _safe_ancestors(layout.systemd_directory)
    units = _unit_data(reservation)
    for name, expected in units.items():
        path = layout.systemd_directory / name
        if os.path.lexists(path) and _read(path, mode=0o644) != expected:
            _fail('update_copy_unit_changed')
    run_systemctl(['disable', '--now', _unit_name(reservation.copy_id) + '.timer'])
    for name in units:
        _fixed_unlink(layout.systemd_directory, name)
    run_systemctl(['daemon-reload'])


def purge_copy(layout, *, expected_copy_id=None, force=False, run_systemctl=_systemctl, now=None):
    """Key first. A stale timer cannot purge a later copy; a failure keeps its watcher.

    ``force`` is explicit root cancellation (also the timer's conservative firing
    decision). The default public check only purges an expired or invalid marker.
    """
    _validate_layout(layout)
    if type(force) is not bool:
        _fail('update_copy_cleanup_mode_invalid')
    if expected_copy_id is not None:
        _copy_id(expected_copy_id)
    with _metadata_lock(layout):
        operation = layout.owner / OPERATION_FILENAME
        if not os.path.lexists(operation) and not any(
                os.path.lexists(path) for path in (layout.key_path, layout.cipher_path,
                                                  layout.partial_path, layout.marker_path)):
            return False
        try:
            reservation = _reservation(layout)
        except (OSError, UpdateCopyError):
            # The immutable scope already proves these fixed locations belong
            # to this installation. A damaged journal makes restore impossible;
            # remove the private material, but never infer a unit name to stop.
            _fixed_unlink(layout.keys, KEY_FILENAME)
            _fixed_unlink(layout.cipher_directory, CIPHER_FILENAME)
            _fixed_unlink(layout.cipher_directory, PARTIAL_FILENAME)
            _fail('update_copy_receipt_invalid')
        if expected_copy_id is not None and reservation.copy_id != expected_copy_id:
            _fail('update_copy_identity_mismatch')
        marker_valid = False
        try:
            observed = parse_marker(_read(layout.marker_path, mode=0o640, gid=BACKEND_GID, limit=512))
            marker_valid = observed == reservation.marker
        except (OSError, UpdateCopyError):
            pass
        if not force and marker_valid:
            try:
                current = _now_value(now)
            except UpdateCopyError:
                current = None
            if current is not None and reservation.marker.created_at <= current < reservation.marker.expires_at:
                return False
        _fixed_unlink(layout.keys, KEY_FILENAME)
        _fixed_unlink(layout.cipher_directory, CIPHER_FILENAME)
        _fixed_unlink(layout.cipher_directory, PARTIAL_FILENAME)
        # A failed unlink above leaves marker, operation and persistent watcher.
        _remove_units(reservation, run_systemctl)
        _fixed_unlink(layout.marker_directory, MARKER_FILENAME)
        _fixed_unlink(layout.owner, OPERATION_FILENAME)
    return True


def validate_restore(layout, *, expected_copy_id, expected_schema_version, expected_marker,
                     time_trusted, now=None):
    """Host preflight only; the crypto helper must authenticate the same identity."""
    _validate_layout(layout)
    if time_trusted is not True:
        _fail('update_copy_clock_untrusted')
    with _metadata_lock(layout):
        reservation = _reservation(layout)
        if (reservation.copy_id != _copy_id(expected_copy_id)
                or reservation.schema_version != _schema_version(expected_schema_version)
                or reservation.marker != expected_marker):
            _fail('update_copy_identity_mismatch')
        marker = parse_marker(_read(layout.marker_path, mode=0o640, gid=BACKEND_GID, limit=512))
        if marker != reservation.marker:
            _fail('update_copy_identity_mismatch')
        _live(marker, now)
        if len(_read(layout.key_path, limit=32)) != 32:
            _fail('update_copy_key_invalid')
        if os.path.lexists(layout.partial_path):
            _fail('update_copy_partial_present')
        # Do not read ciphertext into host memory; just validate its fixed file.
        info = layout.cipher_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
            _fail('update_copy_file_invalid')
        _owner_mode(info, 0o600)
    return reservation


def publish_copy(reservation, *, now=None):
    """Publish after the caller's successful crypto verification; no overwrite.

    The production caller leaves the offline-test ``now`` override unset.
    """
    layout = reservation.layout
    _validate_layout(layout)
    with _metadata_lock(layout):
        if _reservation(layout) != reservation:
            _fail('update_copy_identity_mismatch')
        _live(reservation.marker, now)
        if os.path.lexists(layout.cipher_path):
            _fail('update_copy_operation_exists')
        info = layout.partial_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
            _fail('update_copy_file_invalid')
        _owner_mode(info, 0o600)
        # link is atomic and refuses an existing destination; the temporary
        # second link stays inside the same timer-covered cipher directory.
        os.link(layout.partial_path, layout.cipher_path, follow_symlinks=False)
        _fsync_directory(layout.cipher_directory)
        _fixed_unlink(layout.cipher_directory, PARTIAL_FILENAME)
        final = layout.cipher_path.lstat()
        if (final.st_dev, final.st_ino) != (info.st_dev, info.st_ino) or final.st_nlink != 1:
            _fail('update_copy_file_changed')
        _live(reservation.marker, now)


def _helper_main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--copy-id', required=True)
    args = parser.parse_args()
    _require_root()
    owner = Path(__file__).absolute().parent
    _directory(owner, 0o700)
    scope = _json(_read(owner / SCOPE_FILENAME))
    if type(scope) is not dict or type(scope.get('installation')) is not str:
        _fail('update_copy_scope_invalid')
    layout = load_layout(scope['installation'], scope.get('project'), scope.get('database_id'))
    if layout.owner != owner:
        _fail('update_copy_scope_invalid')
    purge_copy(layout, expected_copy_id=args.copy_id, force=True)


if __name__ == '__main__':
    try:
        _helper_main()
    except (UpdateCopyError, OSError, subprocess.SubprocessError) as error:
        # No exception detail or filesystem/secret contents in systemd logs.
        print(str(error) if isinstance(error, UpdateCopyError) else 'update_copy_cleanup_failed', file=sys.stderr)
        raise SystemExit(1)
