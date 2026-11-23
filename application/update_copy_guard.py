"""Keyless filesystem watcher for the installation's expiring update copy."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import threading
import time

from update_copy_protocol import (CIPHER_FILENAME, PARTIAL_FILENAME, MARKER_FILENAME,
    MAX_MARKER_BYTES, Deadline, UpdateCopyError, parse_marker)

MARKER_ENV = 'LIC_UPDATE_MARKER_DIRECTORY'
CIPHER_ENV = 'LIC_UPDATE_CIPHER_DIRECTORY'
MARKER_DIRECTORY = '/run/leti-cook-update/marker'
CIPHER_DIRECTORY = '/run/leti-cook-update/cipher'


def warning_for_status(value):
    state = value.get('status') if isinstance(value, dict) else None
    if state == 'current':
        return {'code': 'update_copy_present', 'message': 'A temporary database copy exists while an update is being completed.'}
    if state == 'error':
        return {'code': 'update_copy_cleanup_failed', 'message': 'The temporary update copy needs attention. Check the installation.'}
    return None


class UpdateCopyGuard:
    """Trusted path injection is for composition/tests; environment paths are fixed."""
    def __init__(self, marker_directory=None, cipher_directory=None, *, configured=True,
                 strict_owner=True, wall_time=time.time, monotonic=time.monotonic):
        self.marker_directory = Path(marker_directory) if marker_directory is not None else None
        self.cipher_directory = Path(cipher_directory) if cipher_directory is not None else None
        self.configured, self.strict_owner = configured, strict_owner
        self.wall_time, self.monotonic = wall_time, monotonic
        self._deadline = None
        self._next_check = 0.0
        self._last_check = None
        self._status = {'status': 'absent' if configured else 'disabled', 'expires_at': None}
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls, environ=None):
        values = os.environ if environ is None else environ
        if MARKER_ENV not in values and CIPHER_ENV not in values:
            return cls(configured=False)
        if values.get(MARKER_ENV) != MARKER_DIRECTORY or values.get(CIPHER_ENV) != CIPHER_DIRECTORY:
            # Never unlink an arbitrary directory supplied through environment.
            return cls()
        return cls(MARKER_DIRECTORY, CIPHER_DIRECTORY)

    def status(self):
        # Assignment is atomic; never take the filesystem lock to serve HTTP.
        return dict(self._status)

    def _directory(self, path):
        if path is None:
            raise UpdateCopyError('update_copy_configuration_invalid')
        flags = getattr(os, 'O_PATH', os.O_RDONLY) | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if path.is_symlink():
            raise UpdateCopyError('update_copy_directory_invalid')
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or (self.strict_owner and getattr(info, 'st_uid', -1) != 0):
            os.close(fd)
            raise UpdateCopyError('update_copy_directory_invalid')
        return fd

    @staticmethod
    def _stat(directory_fd, path, filename):
        if os.stat in os.supports_dir_fd:
            return os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        return (path / filename).lstat()

    @staticmethod
    def _unlink(directory_fd, path, filename):
        if os.unlink in os.supports_dir_fd:
            os.unlink(filename, dir_fd=directory_fd)
        else:
            (path / filename).unlink()

    def _marker(self, directory_fd):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        if os.open in os.supports_dir_fd:
            fd = os.open(MARKER_FILENAME, flags, dir_fd=directory_fd)
        else:
            path = self.marker_directory / MARKER_FILENAME
            if path.is_symlink():
                raise UpdateCopyError('update_copy_marker_invalid')
            fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MARKER_BYTES
                    or (self.strict_owner and (getattr(info, 'st_uid', -1) != 0 or info.st_mode & 0o022))):
                raise UpdateCopyError('update_copy_marker_invalid')
            return parse_marker(os.read(fd, MAX_MARKER_BYTES + 1))
        finally:
            os.close(fd)

    def check(self, *, force=False):
        if not self.configured:
            return self.status()
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            now = self.monotonic()
            if not force and self._last_check is not None and self._last_check <= now < self._next_check:
                return self.status()
            self._last_check = now
            self._next_check = now + 1.0
            self._status = self._observe()
        except Exception:
            self._status = {'status': 'error', 'expires_at': None}
        finally:
            self._lock.release()
        return self.status()

    def _observe(self):
        marker_fd = cipher_fd = None
        try:
            # Cipher dir is opened first: an unreadable marker must not turn a
            # known ciphertext into an unlimited copy.
            cipher_fd = self._directory(self.cipher_directory)
            files = []
            for name in (CIPHER_FILENAME, PARTIAL_FILENAME):
                try:
                    self._stat(cipher_fd, self.cipher_directory, name)
                except FileNotFoundError:
                    continue
                files.append(name)
            marker = None
            invalid = False
            try:
                marker_fd = self._directory(self.marker_directory)
                marker = self._marker(marker_fd)
                if self._deadline is None or self._deadline.marker != marker:
                    self._deadline = Deadline(marker, wall_time=self.wall_time, monotonic=self.monotonic)
                else:
                    self._deadline.check()
            except (OSError, UpdateCopyError, ValueError):
                invalid = True
            if not files:
                return {'status': 'absent', 'expires_at': None}
            if invalid:
                for name in files:
                    try:
                        self._unlink(cipher_fd, self.cipher_directory, name)
                    except FileNotFoundError:
                        pass  # Root and the system timer are independent owners.
                return {'status': 'removed', 'expires_at': None}
            return {'status': 'current', 'expires_at': marker.expires_at}
        finally:
            for fd in (marker_fd, cipher_fd):
                if fd is not None:
                    os.close(fd)
