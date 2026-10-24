"""Root-owned uninstall intent, captured runtime and confined filesystem removal."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import secrets
import stat

from ops import update_copy_store as copy_store
from ops import update_journal as updates
from ops.uninstall_resources import validate_inventory

JOURNAL_FILENAME = 'uninstall-journal.json'
RUNTIME_DIRECTORY = 'uninstall-runtime'
HELPER_FILENAME = 'resume.py'
MAX_RECORD_BYTES = 512 * 1024
MAX_RUNTIME_BYTES = 4 * 1024 * 1024
PHASES = ('preparing', 'prepared', 'stop_intent', 'workers_stopped', 'copy_intent',
          'copy_removed', 'resources_intent', 'resources_removed', 'trees_intent',
          'trees_removed', 'scope_intent')
RUNTIME_FILES = (
    'install.py', 'ops/uninstall.py', 'ops/uninstall_resources.py', 'ops/uninstall_store.py',
    'ops/update_copy_store.py', 'ops/update_journal.py', 'ops/bundle_contract.py',
    'ops/self_hosted_credentials.py', 'ops/schema_manifest.py', 'ops/release_manifest.py',
    'application/release_version.py', 'application/update_copy_protocol.py',
    'application/schema_release.py',
)
GENERATED_FILES = ('ops/__init__.py', 'application/__init__.py', HELPER_FILENAME)
FIELDS = frozenset(('format', 'version', 'removal_id', 'installation', 'project', 'database_id',
                    'phase', 'inventory', 'trees', 'runtime'))
TREE_NAMES = frozenset(('installation', 'candidate', 'root', 'keys', 'owner', 'marker', 'cipher', 'helper'))


class RemovalError(ValueError):
    """Fixed diagnostic codes, never captured file contents or raw command output."""


@dataclass(frozen=True)
class RemovalOperation:
    layout: copy_store.StoreLayout
    _record: bytes

    @property
    def value(self):
        return json.loads(self._record)

    @property
    def inventory(self):
        return self.value['inventory']

    @property
    def phase(self):
        return self.value['phase']

    @property
    def helper_path(self):
        return self.layout.owner / RUNTIME_DIRECTORY / HELPER_FILENAME


def _fail(code):
    raise RemovalError(code)


def _paths(layout):
    return {'installation': layout.installation, 'candidate': layout.root / 'candidate',
            'root': layout.root, 'keys': layout.keys, 'owner': layout.owner,
            'marker': layout.marker_directory, 'cipher': layout.cipher_directory,
            'helper': layout.owner / RUNTIME_DIRECTORY}


def _scope_identity(layout, name, path):
    # U3 gives only the cipher directory group write+search so the backend can
    # unlink expired ciphertext. Preserve its exact canonical mode/GID; the
    # ordinary application-tree guard must not be loosened for that exception.
    canonical = {directory: (mode, gid) for directory, mode, gid in copy_store._directories(layout)}
    if path in canonical:
        mode, gid = canonical[path]
        info = copy_store._directory(path, mode, gid)
        return {'device': info.st_dev, 'inode': info.st_ino}
    if name == 'helper':
        info = copy_store._directory(path, 0o700)
        return {'device': info.st_dev, 'inode': info.st_ino}
    return updates.tree_identity(path)


def _inventory(value, layout):
    if type(value) is not dict or set(value) != {'docker', 'port', 'update_operation_id'}:
        _fail('uninstall_inventory_invalid')
    if type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
        _fail('uninstall_inventory_invalid')
    if value['update_operation_id'] is not None:
        updates._op(value['update_operation_id'])
    docker = validate_inventory(value['docker'])
    if (docker['project'] != layout.project or docker['installation'] != str(layout.installation)
            or docker['operation_id'] != value['update_operation_id']):
        _fail('uninstall_inventory_scope_mismatch')
    return {'docker': docker, 'port': value['port'], 'update_operation_id': value['update_operation_id']}


def _validate(value, layout):
    if type(value) is not dict or set(value) != FIELDS:
        _fail('uninstall_journal_invalid')
    if (value['format'] != 'leti-cook-uninstall' or type(value['version']) is not int
            or value['version'] != 1 or type(value['phase']) is not str or value['phase'] not in PHASES):
        _fail('uninstall_journal_invalid')
    updates._op(value['removal_id'])
    if (value['installation'], value['project'], value['database_id']) != (
            str(layout.installation), layout.project, layout.database_id):
        _fail('uninstall_scope_mismatch')
    copy_store._identity(value['project'], value['database_id'])
    _inventory(value['inventory'], layout)
    trees = value['trees']
    if type(trees) is not dict or set(trees) != TREE_NAMES:
        _fail('uninstall_tree_record_invalid')
    for name, identity in trees.items():
        if identity is None:
            if name != 'candidate' and not (name == 'helper' and value['phase'] == 'preparing'):
                _fail('uninstall_tree_record_invalid')
        else:
            updates._tree(identity)
    recorded = [(item['device'], item['inode']) for item in trees.values() if item is not None]
    if len(recorded) != len(set(recorded)):
        _fail('uninstall_tree_record_invalid')
    runtime = value['runtime']
    if type(runtime) is not dict or set(runtime) != set(RUNTIME_FILES) | set(GENERATED_FILES):
        _fail('uninstall_runtime_manifest_invalid')
    import re
    if any(type(digest) is not str or re.fullmatch('[0-9a-f]{64}', digest) is None for digest in runtime.values()):
        _fail('uninstall_runtime_manifest_invalid')
    raw = copy_store._json_bytes(value)
    if len(raw) > MAX_RECORD_BYTES:
        _fail('uninstall_journal_too_large')
    return json.loads(raw)


def _operation(layout, value):
    return RemovalOperation(layout, copy_store._json_bytes(_validate(value, layout)))


def _live_scope(operation):
    copy_store._require_root()
    layout, value = operation.layout, operation.value
    copy_store._installation_path(layout.installation)
    missing = set()
    if value['phase'] in ('trees_intent', 'trees_removed', 'scope_intent'):
        missing |= {'installation', 'candidate'}
    if value['phase'] == 'scope_intent':
        missing |= {'keys', 'marker', 'cipher'}
    if value['phase'] == 'preparing':
        missing.add('helper')
    for name, path in _paths(layout).items():
        expected = value['trees'][name]
        if expected is None:
            if os.path.lexists(path) and not (name == 'helper' and value['phase'] == 'preparing'):
                _fail('uninstall_unrecorded_tree')
            continue
        if not os.path.lexists(path):
            if name in missing:
                continue
            _fail('uninstall_tree_missing')
        if _scope_identity(layout, name, path) != expected:
            _fail('uninstall_tree_changed')
    # Scope remains untouched until the terminal scope phase. Thereafter the
    # confirmed removal journal and its captured inode map authorize remnants.
    if value['phase'] != 'scope_intent':
        copy_store._validate_layout(layout)


def load_uninstall(layout):
    copy_store._require_root()
    copy_store._installation_path(layout.installation)
    copy_store._directory(layout.root, 0o700)
    copy_store._directory(layout.owner, 0o700)
    try:
        raw = copy_store._read(layout.owner / JOURNAL_FILENAME, limit=MAX_RECORD_BYTES)
    except FileNotFoundError:
        return None
    operation = _operation(layout, copy_store._json(raw))
    _live_scope(operation)
    return operation


def _current(operation):
    current = load_uninstall(operation.layout)
    if current is None or current._record != operation._record:
        _fail('uninstall_operation_changed')
    updates._require_lock(operation.layout)
    return current


def _write(layout, value):
    updates._require_lock(layout)
    operation = _operation(layout, value)
    # update_journal's 16 KiB pre-read bound is intentionally not reused for
    # this larger, bounded Docker identity inventory.
    path = layout.owner / JOURNAL_FILENAME
    if os.path.lexists(path):
        copy_store._read(path, limit=MAX_RECORD_BYTES)
    temporary = layout.owner / ('.uninstall-' + secrets.token_hex(16) + '.tmp')
    copy_store._create_file(temporary, operation._record)
    try:
        os.replace(temporary, path)
        copy_store._fsync_directory(layout.owner)
    finally:
        if os.path.lexists(temporary):
            copy_store._fixed_unlink(layout.owner, temporary.name)
    return operation


def inspect_trees(layout):
    """Read-only before confirmation; never adopt an unexplained candidate."""
    copy_store._validate_layout(layout)
    updates._parents_same_device(layout)
    paths = _paths(layout)
    if os.path.lexists(paths['helper']):
        _fail('uninstall_runtime_exists')
    current = updates.load_journal(layout)
    trees = {name: (None if name in ('helper', 'candidate') else _scope_identity(layout, name, path))
             for name, path in paths.items()}
    if current is not None and trees['installation'] not in (current['old_tree'], current['new_tree']):
        _fail('uninstall_active_tree_unrecorded')
    if os.path.lexists(paths['candidate']):
        candidate = updates.tree_identity(paths['candidate'])
        if current is None:
            _fail('uninstall_candidate_unrecorded')
        if current['phase'] == 'preparing' and current['new_tree'] is None:
            if trees['installation'] != current['old_tree']:
                _fail('uninstall_candidate_unrecorded')
        elif candidate not in (current['old_tree'], current['new_tree']):
            _fail('uninstall_candidate_unrecorded')
        trees['candidate'] = candidate
    return trees


def _finish_scope(owner_path, expected=None):
    """Standalone terminal cleanup. This function is also embedded in resume.py."""
    import fcntl
    import json
    import os
    from pathlib import Path
    import re
    import stat
    import uuid
    def fail():
        raise ValueError('uninstall_terminal_scope_invalid')
    canonical_modes = {}
    def identity(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            fail()
        if path in canonical_modes:
            mode, gid = canonical_modes[path]
            if stat.S_IMODE(info.st_mode) != mode or info.st_gid != gid:
                fail()
        elif info.st_mode & 0o022:
            fail()
        return {'device': info.st_dev, 'inode': info.st_ino}
    def private(path, limit):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                    or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > limit):
                fail()
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            fail()
        return raw
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail()
            result[key] = value
        return result
    def mount(fd):
        for line in Path('/proc/self/fdinfo/' + str(fd)).read_text(encoding='ascii').splitlines():
            if line.startswith('mnt_id:') and line.split(':', 1)[1].strip().isdecimal():
                return int(line.split(':', 1)[1])
        fail()
    def clean(fd, device, mount_id, keep=()):
        for name in os.listdir(fd):
            if name in keep:
                continue
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if before.st_dev != device:
                fail()
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    info = os.fstat(child)
                    if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or mount(child) != mount_id:
                        fail()
                    clean(child, device, mount_id)
                    os.fsync(child)
                    after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                        fail()
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    def remove(path, wanted, keep=()):
        if not os.path.lexists(path):
            return
        if identity(path) != wanted:
            fail()
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        child = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(child)
            if {'device': info.st_dev, 'inode': info.st_ino} != wanted or mount(parent) != mount(child):
                fail()
            clean(child, info.st_dev, mount(child), keep)
            if not keep:
                if identity(path) != wanted:
                    fail()
                os.rmdir(path.name, dir_fd=parent)
                os.fsync(parent)
        finally:
            os.close(child)
            os.close(parent)
    if os.name != 'posix' or os.geteuid() != 0:
        fail()
    owner = Path(owner_path)
    if not owner.is_absolute() or owner.name != 'owner' or owner != Path(os.path.abspath(owner)):
        fail()
    for ancestor in (owner, *owner.parents):
        identity(ancestor)
    lock_path = owner / 'update.lock'
    if private(lock_path, 0) != b'':
        fail()
    lock = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        # The caller may already hold this same lock. In that case it supplies
        # expected bytes and retains its descriptor for the whole finalization.
        if expected is None:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('update_lock_busy') from None
        raw = private(owner / 'uninstall-journal.json', 512 * 1024)
        if expected is not None and raw != expected:
            fail()
        value = json.loads(raw, object_pairs_hook=pairs)
        keys = {'format', 'version', 'removal_id', 'installation', 'project', 'database_id', 'phase', 'inventory', 'trees', 'runtime'}
        if (type(value) is not dict or set(value) != keys or value['format'] != 'leti-cook-uninstall'
                or type(value['version']) is not int or value['version'] != 1 or value['phase'] != 'scope_intent'
                or re.fullmatch('[0-9a-f]{32}', value['removal_id']) is None
                or re.fullmatch('leti-cook-[0-9a-f]{12}', value['project']) is None
                or str(uuid.UUID(value['database_id'])) != value['database_id']):
            fail()
        installation = Path(value['installation'])
        if (not installation.is_absolute() or installation != Path(os.path.abspath(installation))
                or installation == installation.parent or '..' in installation.parts
                or owner != installation.with_name(installation.name + '.update') / 'owner'):
            fail()
        root = owner.parent
        paths = {'root': root, 'owner': owner, 'keys': installation.with_name(installation.name + '.update-keys'),
                 'marker': root / 'marker', 'cipher': root / 'cipher', 'helper': owner / 'uninstall-runtime'}
        # Standalone terminal execution cannot import modules being removed.
        # These are the same fixed U3 modes as update_copy_store._directories.
        canonical_modes.update({path: (0o700, 0) for path in paths.values()})
        canonical_modes[paths['marker']] = (0o750, 10001)
        canonical_modes[paths['cipher']] = (0o730, 10001)
        trees = value['trees']
        if type(trees) is not dict or set(trees) != set(paths) | {'installation', 'candidate'}:
            fail()
        for name, path in paths.items():
            wanted = trees[name]
            if (type(wanted) is not dict or set(wanted) != {'device', 'inode'}
                    or any(type(number) is not int or number < 0 for number in wanted.values())
                    or wanted['inode'] < 1):
                fail()
            if os.path.lexists(path) and identity(path) != wanted:
                fail()
        if identity(owner) != trees['owner'] or identity(root) != trees['root']:
            fail()
        if os.path.lexists(installation) or os.path.lexists(root / 'candidate'):
            fail()
        for path in (paths['keys'] / 'copy.key', paths['cipher'] / 'copy.enc', paths['cipher'] / 'copy.partial'):
            if os.path.lexists(path):
                fail()
        for name in ('keys', 'marker', 'cipher'):
            remove(paths[name], trees[name])
        # Keep this standalone helper and the journal/lock readable until every
        # other payload and copied module is gone. It can resume without imports.
        remove(paths['helper'], trees['helper'], keep=('resume.py',))
        remove(owner, trees['owner'], keep=('uninstall-runtime', 'uninstall-journal.json', 'update.lock'))
        helper = paths['helper']
        if os.path.lexists(helper):
            remove(helper, trees['helper'])
        # From here only private journal/lock and empty directories remain.
        remove(owner, trees['owner'])
        remove(root, trees['root'])
    finally:
        os.close(lock)
    return 0


def _runtime_sources():
    root = Path(__file__).resolve().parents[1]
    files = {name: (root / name).read_bytes() for name in RUNTIME_FILES}
    files['ops/__init__.py'] = b''
    files['application/__init__.py'] = b''
    helper = """import hashlib,json,os,stat,sys
from pathlib import Path
sys.dont_write_bytecode=True
""" + inspect.getsource(_finish_scope) + """
root=Path(__file__).absolute().parent
owner=root.parent
if os.name!='posix' or os.geteuid()!=0: raise SystemExit('uninstall_root_required')
for path in (root,owner,*owner.parents):
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022: raise SystemExit('uninstall_helper_untrusted')
def pairs(items):
    result={}
    for key,value in items:
        if key in result: raise ValueError('uninstall_journal_invalid')
        result[key]=value
    return result
def read(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or info.st_gid!=0 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>limit: raise ValueError('uninstall_helper_untrusted')
        data=stream.read(limit+1)
    if len(data)>limit: raise ValueError('uninstall_helper_untrusted')
    return data
value=json.loads(read(owner/'uninstall-journal.json',512*1024),object_pairs_hook=pairs)
if value.get('phase')=='scope_intent':
    raise SystemExit(_finish_scope(owner))
expected=""" + repr(tuple(sorted(files.keys() | {HELPER_FILENAME}))) + """
if type(value.get('runtime')) is not dict or set(value['runtime'])!=set(expected): raise SystemExit('uninstall_runtime_invalid')
for name,digest in value['runtime'].items():
    if hashlib.sha256(read(root/name,4*1024*1024)).hexdigest()!=digest: raise SystemExit('uninstall_runtime_changed')
sys.path.insert(0,str(root))
from ops.uninstall import resume
raise SystemExit(resume(root))
"""
    files[HELPER_FILENAME] = helper.encode('utf-8')
    if sum(map(len, files.values())) > MAX_RUNTIME_BYTES:
        _fail('uninstall_runtime_too_large')
    return files


def _complete_runtime(operation):
    _current(operation)
    files = _runtime_sources()
    value, root = operation.value, operation.helper_path.parent
    if {name: hashlib.sha256(data).hexdigest() for name, data in files.items()} != value['runtime']:
        _fail('uninstall_runtime_source_changed')
    if value['trees']['helper'] is None:
        if not os.path.lexists(root):
            copy_store._mkdir(root, 0o700)
        value['trees']['helper'] = updates.tree_identity(root)
        operation = _write(operation.layout, value)
    allowed_directories = {root, root / 'ops', root / 'application'}
    for directory in allowed_directories:
        if not directory.exists():
            copy_store._mkdir(directory, 0o700)
        copy_store._directory(directory, 0o700)
    pending_names = {'.pending-' + hashlib.sha256(name.encode('ascii')).hexdigest() for name in files}
    for path in root.rglob('*'):
        if path in allowed_directories:
            continue
        name = path.relative_to(root).as_posix()
        if path.parent == root and name in pending_names:
            # Exact derived staging names are covered by the prior preparing
            # intent. A partial root-owned regular file is disposable, unlike
            # a published module whose hash must always match.
            copy_store._read(path, limit=MAX_RUNTIME_BYTES)
            continue
        if name not in files or copy_store._read(path, limit=MAX_RUNTIME_BYTES) != files[name]:
            _fail('uninstall_runtime_changed')
    for name, data in files.items():
        path = root / name
        if os.path.lexists(path):
            if copy_store._read(path, limit=MAX_RUNTIME_BYTES) != data:
                _fail('uninstall_runtime_changed')
        else:
            pending = root / ('.pending-' + hashlib.sha256(name.encode('ascii')).hexdigest())
            if os.path.lexists(pending):
                copy_store._read(pending, limit=MAX_RUNTIME_BYTES)
                copy_store._fixed_unlink(root, pending.name)
            copy_store._create_file(pending, data)
            if os.path.lexists(path):
                _fail('uninstall_runtime_changed')
            os.rename(pending, path)
            copy_store._fsync_directory(path.parent)
            copy_store._fsync_directory(root)
    # A crash after rename leaves no staging file; a stale staging file next
    # to an already valid module is harmless but must not enter prepared.
    for name in pending_names:
        pending = root / name
        if os.path.lexists(pending):
            copy_store._read(pending, limit=MAX_RUNTIME_BYTES)
            copy_store._fixed_unlink(root, name)
    return advance(operation, 'prepared')


def prepare_uninstall(layout, inventory):
    """Called only after exact project confirmation, while update_lock is held."""
    copy_store._validate_layout(layout)
    updates._require_lock(layout)
    inventory = _inventory(inventory, layout)
    existing = load_uninstall(layout)
    if existing is not None:
        if existing.inventory != inventory:
            _fail('uninstall_operation_changed')
        if existing.phase == 'preparing':
            return _complete_runtime(existing)
        return existing
    trees = inspect_trees(layout)
    files = _runtime_sources()
    value = {'format': 'leti-cook-uninstall', 'version': 1, 'removal_id': secrets.token_hex(16),
             'installation': str(layout.installation), 'project': layout.project, 'database_id': layout.database_id,
             'phase': 'preparing', 'inventory': inventory, 'trees': trees,
             'runtime': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    operation = _write(layout, value)
    return _complete_runtime(operation)


def advance(operation, phase):
    _current(operation)
    if type(phase) is not str or phase not in PHASES:
        _fail('uninstall_phase_invalid')
    old, new = PHASES.index(operation.phase), PHASES.index(phase)
    if new not in (old, old + 1):
        _fail('uninstall_phase_order_invalid')
    value = operation.value
    value['phase'] = phase
    return _write(operation.layout, value)


@contextmanager
def removal_lock(operation):
    """Same update lock, including terminal remnants missing other scope dirs."""
    _live_scope(operation)
    layout = operation.layout
    if updates._HELD.get() is not None:
        _fail('update_lock_busy')
    path = layout.owner / updates.LOCK_FILENAME
    copy_store._read(path, limit=0)
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    token = None
    try:
        import fcntl
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail('update_lock_busy')
        token = updates._HELD.set((layout.owner, descriptor))
        updates._require_lock(layout)
        _current(operation)
        yield
    finally:
        if token is not None:
            updates._HELD.reset(token)
        os.close(descriptor)


def _remove_recorded(path, expected):
    if not os.path.lexists(path):
        return False
    if updates.tree_identity(path) != expected:
        _fail('uninstall_tree_changed')
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    child = None
    try:
        child = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(child)
        if {'device': info.st_dev, 'inode': info.st_ino} != expected:
            _fail('uninstall_tree_changed')
        mount = updates._mount_id(parent)
        if updates._mount_id(child) != mount:
            _fail('uninstall_tree_mountpoint')
        updates._remove_contents(child, info.st_dev, mount)
        os.fsync(child)
        if updates.tree_identity(path) != expected:
            _fail('uninstall_tree_changed')
        os.rmdir(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        if child is not None:
            os.close(child)
        os.close(parent)
    return True


def remove_installation_trees(operation):
    _current(operation)
    if operation.phase == 'resources_removed':
        operation = advance(operation, 'trees_intent')
    if operation.phase != 'trees_intent':
        _fail('uninstall_tree_removal_intent_required')
    for path in (operation.layout.key_path, operation.layout.cipher_path, operation.layout.partial_path):
        if os.path.lexists(path):
            _fail('uninstall_copy_not_removed')
    paths = _paths(operation.layout)
    for name in ('candidate', 'installation'):
        expected = operation.value['trees'][name]
        if expected is None:
            if os.path.lexists(paths[name]):
                _fail('uninstall_unrecorded_tree')
        else:
            _remove_recorded(paths[name], expected)
    return advance(operation, 'trees_removed')


def finalize_uninstall(operation):
    _current(operation)
    if operation.phase == 'trees_removed':
        operation = advance(operation, 'scope_intent')
    if operation.phase != 'scope_intent':
        _fail('uninstall_scope_removal_intent_required')
    _finish_scope(operation.layout.owner, operation._record)


def resume_scope(helper_root):
    """Read only the independent journal, never files beneath installation I."""
    helper = Path(helper_root)
    copy_store._require_root()
    copy_store._safe_ancestors(helper)
    if helper.name != RUNTIME_DIRECTORY or helper.parent.name != 'owner':
        _fail('uninstall_helper_path_invalid')
    value = copy_store._json(copy_store._read(helper.parent / JOURNAL_FILENAME, limit=MAX_RECORD_BYTES))
    if type(value) is not dict or set(value) != FIELDS:
        _fail('uninstall_journal_invalid')
    layout = copy_store.StoreLayout(copy_store._installation_path(value['installation']),
                                   value['project'], value['database_id'])
    if helper != layout.owner / RUNTIME_DIRECTORY:
        _fail('uninstall_helper_scope_mismatch')
    operation = load_uninstall(layout)
    if operation is None:
        _fail('uninstall_journal_missing')
    return operation
