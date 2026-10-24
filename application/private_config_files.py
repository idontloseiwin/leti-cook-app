"""Private local configuration files; no provider or database clients."""
import os
import stat
from pathlib import Path
from contextlib import contextmanager
from self_hosted_settings import SettingsError

def safe_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0)
            & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
            raise SettingsError('configuration_link_refused')
    return path

def config_directory(path):
    path = safe_path(path)
    if not path.is_dir(): raise SettingsError('configuration_directory_required')
    if os.name != 'nt' and (path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022):
        raise SettingsError('run_as_configuration_owner_in_private_directory')
    return path

def read_file(path):
    path = safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SettingsError('configuration_regular_file_required')
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise SettingsError('configuration_owner_only_permissions_required')
        data = stream.read(65537)
    if len(data) > 65536: raise SettingsError('configuration_file_too_large')
    return data

@contextmanager
def setup_lock(directory):
    path = safe_path(directory / '.setup.lock')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'r+b') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SettingsError('unsafe_configuration_lock')
        if os.name == 'nt':
            import msvcrt
            if info.st_size == 0: stream.write(b'0');stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try: yield
            finally: stream.seek(0);msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

