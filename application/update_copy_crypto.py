"""Authenticated streaming copies of a stopped PostgreSQL 17 volume.

The binary codec is bounded by trusted caller limits, never by its own header.
Privileged filesystem work is Linux-only and uses directory descriptors. No
plaintext archive is written to disk. The container CLI owns the root boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
import re
import stat
import struct
import tarfile
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from update_copy_protocol import Deadline, Marker, UpdateCopyError

CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 ** 4
MAX_ENTRIES = 1_000_000
MAX_DEPTH = 64
MAGIC = b'LICUPD01'
HEADER = struct.Struct('>8s16s16sIQQI4s')
RECORD = struct.Struct('>cQI')
FINAL = struct.Struct('>QQ32s')
ZERO_BLOCK = bytes(512)


@dataclass(frozen=True, slots=True)
class CopyIdentity:
    copy_id: str
    database_id: str
    schema_version: int

    def __post_init__(self):
        try:
            valid = (type(self.copy_id) is str
                     and re.fullmatch(r'[0-9a-f]{32}', self.copy_id) is not None
                     and type(self.database_id) is str
                     and str(uuid.UUID(self.database_id)) == self.database_id
                     and type(self.schema_version) is int
                     and 1 <= self.schema_version <= 2147483647)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise UpdateCopyError('update_copy_identity_invalid')


@dataclass(frozen=True, slots=True)
class CopyLimits:
    max_bytes: int
    max_files: int

    def __post_init__(self):
        if (type(self.max_bytes) is not int or not 2048 <= self.max_bytes <= MAX_ARCHIVE_BYTES
                or type(self.max_files) is not int or not 2 <= self.max_files <= MAX_ENTRIES):
            raise UpdateCopyError('update_copy_limits_invalid')


@dataclass(frozen=True, slots=True)
class CopyProof:
    identity: CopyIdentity
    marker: Marker
    archive_bytes: int
    entry_count: int
    file_bytes: int
    sha256: str

    def as_dict(self):
        return {'copy_id': self.identity.copy_id, 'database_id': self.identity.database_id,
                'schema_version': self.identity.schema_version,
                'created_at': self.marker.created_at, 'expires_at': self.marker.expires_at,
                'archive_bytes': self.archive_bytes, 'entry_count': self.entry_count,
                'file_bytes': self.file_bytes, 'sha256': self.sha256}


def _parameters(key, marker, identity, limits, deadline):
    if type(key) is not bytes or len(key) != 32:
        raise UpdateCopyError('update_copy_key_invalid')
    if type(marker) is not Marker or type(identity) is not CopyIdentity or type(limits) is not CopyLimits:
        raise UpdateCopyError('update_copy_parameters_invalid')
    if deadline is None:
        deadline = Deadline(marker)
    if type(deadline) is not Deadline or deadline.marker != marker:
        raise UpdateCopyError('update_copy_deadline_invalid')
    deadline.check()
    return deadline


def _read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise UpdateCopyError('update_copy_truncated')
        result.extend(part)
    return bytes(result)


def _write_all(stream, value):
    view = memoryview(value)
    while view:
        count = stream.write(view)
        if count is None or count <= 0:
            raise UpdateCopyError('update_copy_write_failed')
        view = view[count:]


class _EncryptWriter:
    def __init__(self, output, key, marker, identity, limits, deadline):
        self.deadline = _parameters(key, marker, identity, limits, deadline)
        self.output, self.limits = output, limits
        self.aead = AESGCM(key)
        self.prefix = os.urandom(4)
        self.header = HEADER.pack(MAGIC, bytes.fromhex(identity.copy_id),
                                  uuid.UUID(identity.database_id).bytes, identity.schema_version,
                                  marker.created_at, marker.expires_at, CHUNK_BYTES, self.prefix)
        self.buffer = bytearray()
        self.count = self.total = 0
        self.digest = hashlib.sha256()
        self.finished = False
        _write_all(output, self.header + self.aead.encrypt(self.prefix + bytes(8), b'', self.header))

    def write(self, data):
        self.deadline.check()
        if self.finished or self.total + len(self.buffer) + len(data) > self.limits.max_bytes:
            raise UpdateCopyError('update_copy_size_limit')
        # Callers feed at most one chunk. Keep the writer bounded independently.
        if len(data) > CHUNK_BYTES:
            raise UpdateCopyError('update_copy_chunk_invalid')
        self.buffer.extend(data)
        while len(self.buffer) >= CHUNK_BYTES:
            chunk = bytes(self.buffer[:CHUNK_BYTES])
            del self.buffer[:CHUNK_BYTES]
            self._data(chunk)
        return len(data)

    def _data(self, data):
        self.deadline.check()
        self.count += 1
        self.total += len(data)
        self.digest.update(data)
        frame = RECORD.pack(b'D', self.count, len(data))
        nonce = self.prefix + self.count.to_bytes(8, 'big')
        _write_all(self.output, frame + self.aead.encrypt(nonce, data, self.header + frame))

    def finish(self):
        if self.finished:
            raise UpdateCopyError('update_copy_already_final')
        if self.buffer:
            self._data(bytes(self.buffer))
            self.buffer.clear()
        self.deadline.check()
        final = FINAL.pack(self.count, self.total, self.digest.digest())
        frame = RECORD.pack(b'F', self.count + 1, len(final))
        nonce = self.prefix + (self.count + 1).to_bytes(8, 'big')
        _write_all(self.output, frame + self.aead.encrypt(nonce, final, self.header + frame))
        self.finished = True
        self.deadline.check()


class _DecryptReader:
    def __init__(self, source, key, marker, identity, limits, deadline):
        self.deadline = _parameters(key, marker, identity, limits, deadline)
        self.source, self.limits = source, limits
        self.header = _read_exact(source, HEADER.size)
        try:
            magic, copy_id, database_id, version, created, expires, chunk_size, self.prefix = HEADER.unpack(self.header)
            actual_identity = CopyIdentity(copy_id.hex(), str(uuid.UUID(bytes=database_id)), version)
            actual_marker = Marker(created, expires)
            if magic != MAGIC or chunk_size != CHUNK_BYTES:
                raise ValueError
        except (ValueError, TypeError, struct.error) as exc:
            raise UpdateCopyError('update_copy_header_invalid') from exc
        self.aead = AESGCM(key)
        try:
            self.aead.decrypt(self.prefix + bytes(8), _read_exact(source, 16), self.header)
        except InvalidTag as exc:
            raise UpdateCopyError('update_copy_authentication_failed') from exc
        if actual_marker != marker or actual_identity != identity:
            raise UpdateCopyError('update_copy_identity_mismatch')
        self.buffer = memoryview(b'')
        self.count = self.total = 0
        self.digest = hashlib.sha256()
        self.finished = self.short_chunk = False

    def _next(self):
        self.deadline.check()
        frame = _read_exact(self.source, RECORD.size)
        kind, index, size = RECORD.unpack(frame)
        if index != self.count + 1:
            raise UpdateCopyError('update_copy_sequence_invalid')
        if kind == b'D':
            if self.short_chunk or not 0 < size <= CHUNK_BYTES or self.total + size > self.limits.max_bytes:
                raise UpdateCopyError('update_copy_size_limit')
        elif kind == b'F':
            if size != FINAL.size:
                raise UpdateCopyError('update_copy_final_invalid')
        else:
            raise UpdateCopyError('update_copy_record_invalid')
        try:
            data = self.aead.decrypt(self.prefix + index.to_bytes(8, 'big'),
                                     _read_exact(self.source, size + 16), self.header + frame)
        except InvalidTag as exc:
            raise UpdateCopyError('update_copy_authentication_failed') from exc
        self.deadline.check()
        if kind == b'D':
            self.count += 1
            self.total += size
            self.digest.update(data)
            self.short_chunk = size < CHUNK_BYTES
            self.buffer = memoryview(data)
        else:
            count, total, digest = FINAL.unpack(data)
            if count != self.count or total != self.total or not hmac.compare_digest(digest, self.digest.digest()):
                raise UpdateCopyError('update_copy_final_invalid')
            if self.source.read(1):
                raise UpdateCopyError('update_copy_trailing_data')
            self.deadline.check()
            self.finished = True

    def read(self, size):
        if not 0 <= size <= CHUNK_BYTES:
            raise UpdateCopyError('update_copy_read_invalid')
        self.deadline.check()
        if size and not self.buffer and not self.finished:
            self._next()
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result.tobytes()


def _archive_path(info, first):
    name = info.name.rstrip('/') if info.isdir() else info.name
    if first and info.isdir() and name == '.':
        return ()
    parts = name.split('/')
    if (not name or len(name.encode('utf-8')) > 255 or len(parts) > MAX_DEPTH
            or any(part in ('', '.', '..') for part in parts)
            or '\\' in name or ':' in name or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise UpdateCopyError('update_copy_archive_path_invalid')
    return tuple(parts)


def _consume_archive(reader, limits, deadline, target=None):
    directories = {()}
    seen = set()
    entries = file_bytes = 0
    pg_version = None
    while True:
        deadline.check()
        block = _read_exact(reader, 512)
        if block == ZERO_BLOCK:
            if _read_exact(reader, 512) != ZERO_BLOCK or reader.read(1):
                raise UpdateCopyError('update_copy_archive_trailing_data')
            break
        if block[257:263] != b'ustar\0' or block[263:265] != b'00':
            raise UpdateCopyError('update_copy_archive_format_invalid')
        try:
            info = tarfile.TarInfo.frombuf(block, 'utf-8', 'strict')
        except (ValueError, UnicodeError, tarfile.HeaderError) as exc:
            raise UpdateCopyError('update_copy_archive_invalid') from exc
        if (info.type not in (tarfile.REGTYPE, tarfile.DIRTYPE) or info.linkname
                or not 0 <= info.uid <= 2147483647 or not 0 <= info.gid <= 2147483647
                or not 0 <= info.mode <= 0o777 or not 0 <= info.mtime <= 8589934591
                or info.size < 0 or info.size > limits.max_bytes
                or (info.isdir() and info.size != 0)):
            raise UpdateCopyError('update_copy_archive_entry_invalid')
        parts = _archive_path(info, entries == 0)
        if (entries == 0 and parts != ()) or parts in seen or parts[:-1] not in directories:
            raise UpdateCopyError('update_copy_archive_structure_invalid')
        if parts == ('postmaster.pid',):
            raise UpdateCopyError('update_copy_database_not_stopped')
        seen.add(parts)
        entries += 1
        if entries > limits.max_files:
            raise UpdateCopyError('update_copy_file_limit')
        if info.isdir():
            directories.add(parts)
            if target is not None:
                target.directory(parts, info)
            continue
        file_bytes += info.size
        if file_bytes > limits.max_bytes:
            raise UpdateCopyError('update_copy_size_limit')
        if parts == ('PG_VERSION',) and info.size != 3:
            raise UpdateCopyError('update_copy_postgres_version_invalid')
        output = target.file(parts) if target is not None else None
        version = bytearray()
        try:
            remaining = info.size
            while remaining:
                deadline.check()
                chunk = _read_exact(reader, min(remaining, CHUNK_BYTES))
                remaining -= len(chunk)
                if parts == ('PG_VERSION',):
                    version.extend(chunk)
                if output is not None:
                    _write_all(output, chunk)
            padding = (-info.size) % 512
            if padding and _read_exact(reader, padding) != bytes(padding):
                raise UpdateCopyError('update_copy_archive_padding_invalid')
            if output is not None:
                target.finish_file(output, info)
        finally:
            if output is not None:
                output.close()
        if parts == ('PG_VERSION',):
            pg_version = bytes(version)
    if pg_version != b'17\n' or entries < 2:
        raise UpdateCopyError('update_copy_postgres_version_invalid')
    if not reader.finished:
        raise UpdateCopyError('update_copy_final_invalid')
    deadline.check()
    return entries, file_bytes


def verify_stream(source, key, marker, identity, limits, *, deadline=None):
    """Verify from the current stream offset, without writing plaintext anywhere."""
    deadline = _parameters(key, marker, identity, limits, deadline)
    reader = _DecryptReader(source, key, marker, identity, limits, deadline)
    entries, file_bytes = _consume_archive(reader, limits, deadline)
    return CopyProof(identity, marker, reader.total, entries, file_bytes, reader.digest.hexdigest())


def _linux_filesystem():
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise UpdateCopyError('update_copy_linux_required')


def _dir_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _same_file(before, after):
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)


def _safe_cipher(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or value.st_uid != 0 or stat.S_IMODE(value.st_mode) != 0o600):
            raise UpdateCopyError('update_copy_cipher_invalid')
        return os.fdopen(fd, 'rb', buffering=0)
    except BaseException:
        os.close(fd)
        raise


def _tar_info(name, value, directory):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else value.st_size
    info.uid, info.gid = value.st_uid, value.st_gid
    info.mode, info.mtime = stat.S_IMODE(value.st_mode), int(value.st_mtime)
    if (info.mode & ~0o777 or not 0 <= info.mtime <= 8589934591
            or not 0 <= info.uid <= 2147483647 or not 0 <= info.gid <= 2147483647):
        raise UpdateCopyError('update_copy_source_metadata_invalid')
    try:
        block = info.tobuf(tarfile.USTAR_FORMAT, 'utf-8', 'strict')
    except (ValueError, UnicodeError) as exc:
        raise UpdateCopyError('update_copy_archive_path_invalid') from exc
    _archive_path(info, name == '.')
    return block


def _write_source(source, writer, limits, deadline):
    root = os.open(source, _dir_flags())
    count = file_bytes = 0
    pg_version = None
    try:
        def walk(directory, parts):
            nonlocal count, file_bytes, pg_version
            deadline.check()
            metadata = os.fstat(directory)
            count += 1
            if count > limits.max_files:
                raise UpdateCopyError('update_copy_file_limit')
            writer.write(_tar_info('/'.join(parts) or '.', metadata, True))
            names = []
            with os.scandir(directory) as scan:
                for entry in scan:
                    names.append(entry.name)
                    if count + len(names) > limits.max_files:
                        raise UpdateCopyError('update_copy_file_limit')
            for name in sorted(names):
                deadline.check()
                child_parts = (*parts, name)
                if child_parts == ('postmaster.pid',):
                    raise UpdateCopyError('update_copy_database_not_stopped')
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child = os.open(name, _dir_flags(), dir_fd=directory)
                    try:
                        if not _same_file(before, os.fstat(child)):
                            raise UpdateCopyError('update_copy_source_changed')
                        walk(child, child_parts)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(child, 'rb', buffering=0) as stream:
                        if not _same_file(before, os.fstat(stream.fileno())):
                            raise UpdateCopyError('update_copy_source_changed')
                        count += 1
                        if count > limits.max_files:
                            raise UpdateCopyError('update_copy_file_limit')
                        writer.write(_tar_info('/'.join(child_parts), before, False))
                        file_bytes += before.st_size
                        if file_bytes > limits.max_bytes:
                            raise UpdateCopyError('update_copy_size_limit')
                        if child_parts == ('PG_VERSION',) and before.st_size != 3:
                            raise UpdateCopyError('update_copy_postgres_version_invalid')
                        version = bytearray()
                        remaining = before.st_size
                        while remaining:
                            deadline.check()
                            data = stream.read(min(remaining, CHUNK_BYTES))
                            if not data:
                                raise UpdateCopyError('update_copy_source_changed')
                            remaining -= len(data)
                            if child_parts == ('PG_VERSION',):
                                version.extend(data)
                            writer.write(data)
                        if stream.read(1) or not _same_file(before, os.fstat(stream.fileno())):
                            raise UpdateCopyError('update_copy_source_changed')
                        if child_parts == ('PG_VERSION',):
                            pg_version = bytes(version)
                        padding = (-before.st_size) % 512
                        if padding:
                            writer.write(bytes(padding))
                else:
                    raise UpdateCopyError('update_copy_source_type_invalid')
            if not _same_file(metadata, os.fstat(directory)):
                raise UpdateCopyError('update_copy_source_changed')
        walk(root, ())
        if pg_version != b'17\n':
            raise UpdateCopyError('update_copy_postgres_version_invalid')
        writer.write(ZERO_BLOCK)
        writer.write(ZERO_BLOCK)
        return count, file_bytes
    finally:
        os.close(root)


def create_copy(source, ciphertext, key, marker, identity, limits, *, deadline=None):
    """Create a new partial; failure is never resumed with this key or nonce."""
    _linux_filesystem()
    deadline = _parameters(key, marker, identity, limits, deadline)
    fd = os.open(ciphertext, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'wb', buffering=0) as output:
        os.fchmod(output.fileno(), 0o600)
        writer = _EncryptWriter(output, key, marker, identity, limits, deadline)
        count, size = _write_source(source, writer, limits, deadline)
        writer.finish()
        os.fsync(output.fileno())
        deadline.check()
        return CopyProof(identity, marker, writer.total, count, size, writer.digest.hexdigest())


def verify_copy(ciphertext, key, marker, identity, limits, *, deadline=None):
    _linux_filesystem()
    deadline = _parameters(key, marker, identity, limits, deadline)
    with _safe_cipher(ciphertext) as source:
        before = os.fstat(source.fileno())
        proof = verify_stream(source, key, marker, identity, limits, deadline=deadline)
        if not _same_file(before, os.fstat(source.fileno())):
            raise UpdateCopyError('update_copy_cipher_changed')
        deadline.check()
        return proof


class _RestoreTarget:
    def __init__(self, path, deadline):
        self.fd = os.open(path, _dir_flags())
        self.deadline = deadline
        self.directories = []
        try:
            info = os.fstat(self.fd)
            if (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
                    or os.listdir(self.fd)):
                raise UpdateCopyError('update_copy_target_not_empty_owned')
        except BaseException:
            os.close(self.fd)
            raise

    def _parent(self, parts):
        fd = os.dup(self.fd)
        try:
            for part in parts:
                child = os.open(part, _dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def directory(self, parts, info):
        if parts:
            fd = self._parent(parts[:-1])
            try:
                os.mkdir(parts[-1], 0o700, dir_fd=fd)
            finally:
                os.close(fd)
        self.directories.append((parts, info))

    def file(self, parts):
        fd = self._parent(parts[:-1])
        try:
            child = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                            0o600, dir_fd=fd)
            return os.fdopen(child, 'wb', buffering=0)
        finally:
            os.close(fd)

    @staticmethod
    def _metadata(fd, info):
        os.fchown(fd, info.uid, info.gid)
        os.fchmod(fd, info.mode)
        os.utime(fd, (info.mtime, info.mtime))
        os.fsync(fd)

    def finish_file(self, output, info):
        self.deadline.check()
        self._metadata(output.fileno(), info)

    def finish(self):
        for parts, info in reversed(self.directories):
            self.deadline.check()
            fd = self._parent(parts)
            try:
                self._metadata(fd, info)
            finally:
                os.close(fd)
        self.deadline.check()

    def close(self):
        os.close(self.fd)


def restore_copy(ciphertext, target, key, marker, identity, limits, *, deadline=None):
    """Full verify first; repeat authentication while restoring from the same FD.

A second-pass I/O/tamper/deadline failure leaves an unusable partial target. The
caller must discard that new volume, never start PostgreSQL or reuse it.
"""
    _linux_filesystem()
    deadline = _parameters(key, marker, identity, limits, deadline)
    with _safe_cipher(ciphertext) as source:
        before = os.fstat(source.fileno())
        proof = verify_stream(source, key, marker, identity, limits, deadline=deadline)
        if not _same_file(before, os.fstat(source.fileno())):
            raise UpdateCopyError('update_copy_cipher_changed')
        source.seek(0)
        destination = _RestoreTarget(target, deadline)
        try:
            reader = _DecryptReader(source, key, marker, identity, limits, deadline)
            entries, size = _consume_archive(reader, limits, deadline, destination)
            restored = CopyProof(identity, marker, reader.total, entries, size, reader.digest.hexdigest())
            if restored != proof or not _same_file(before, os.fstat(source.fileno())):
                raise UpdateCopyError('update_copy_cipher_changed')
            destination.finish()
            return restored
        finally:
            destination.close()
