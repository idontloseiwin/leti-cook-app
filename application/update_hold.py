"""Read a root-owned persistent update hold; never modify or auto-expire it."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import threading

from update_runtime_protocol import HOLD_FILENAME, MAX_HOLD_BYTES, parse_hold
from update_copy_guard import MARKER_ENV, MARKER_DIRECTORY, CIPHER_ENV, CIPHER_DIRECTORY


class UpdateHoldGuard:
    def __init__(self, directory=None, *, configured=True, strict_owner=True):
        self.directory = Path(directory) if directory is not None else None
        self.configured, self.strict_owner = configured, strict_owner
        self._lock = threading.Lock()
        self._status = {'state': 'invalid' if configured else 'disabled', 'operation_id': None}

    @classmethod
    def from_environment(cls, environ=None):
        values = os.environ if environ is None else environ
        if MARKER_ENV not in values and CIPHER_ENV not in values:
            return cls(configured=False)
        if values.get(MARKER_ENV) != MARKER_DIRECTORY or values.get(CIPHER_ENV) != CIPHER_DIRECTORY:
            return cls()
        return cls(MARKER_DIRECTORY)

    def status(self):
        return dict(self._status)

    def check(self):
        if not self.configured:
            return self.status()
        with self._lock:
            try:
                self._status = self._read()
            except (OSError, ValueError, TypeError):
                self._status = {'state': 'invalid', 'operation_id': None}
            return self.status()

    def _directory(self):
        if self.directory is None or self.directory.is_symlink():
            raise ValueError()
        flags = getattr(os, 'O_PATH', os.O_RDONLY) | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        directory = os.open(self.directory, flags)
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or (self.strict_owner and (getattr(info, 'st_uid', -1) != 0 or info.st_mode & 0o022)):
            os.close(directory)
            raise ValueError()
        return directory

    def _read(self):
        directory = self._directory()
        fd = None
        try:
            try:
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                if os.open in os.supports_dir_fd:
                    fd = os.open(HOLD_FILENAME, flags, dir_fd=directory)
                else:
                    # Portable injection for offline tests; installed Linux uses dirfd.
                    target = self.directory / HOLD_FILENAME
                    if target.is_symlink():
                        raise ValueError()
                    fd = os.open(target, flags)
            except FileNotFoundError:
                return {'state': 'absent', 'operation_id': None}
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HOLD_BYTES or info.st_nlink != 1
                    or (self.strict_owner and (getattr(info, 'st_uid', -1) != 0 or info.st_mode & 0o022))):
                raise ValueError()
            value = parse_hold(os.read(fd, MAX_HOLD_BYTES + 1))
            return {'state': 'held', 'operation_id': value}
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)
