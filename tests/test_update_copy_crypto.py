"""Offline lifetime, authenticated-stream and two-pass restore acceptance."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import subprocess
import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import update_copy_crypto as crypto
from update_copy_protocol import Deadline, Marker, UpdateCopyError, WINDOW_READ_SECONDS, marker_bytes, marker_from_window, parse_marker

KEY = b'k' * 32
IDENTITY = crypto.CopyIdentity('0123456789abcdef' * 2, '12345678-1234-5678-1234-567812345678', 25)
MARKER = Marker(100, 200)
LIMITS = crypto.CopyLimits(8 * 1024 * 1024, 100)


def deadline():
    return Deadline(MARKER, wall_time=lambda: 120, monotonic=lambda: 1000)


def header(name, *, directory=False, size=0, kind=None, link=''):
    value = tarfile.TarInfo(name)
    value.type = kind if kind is not None else (tarfile.DIRTYPE if directory else tarfile.REGTYPE)
    value.size = size
    value.mode = 0o700 if directory else 0o600
    value.uid = value.gid = 999
    value.mtime = 100
    value.linkname = link
    return value.tobuf(tarfile.USTAR_FORMAT, 'utf-8', 'strict')


def entry(name, content=b'', **kwargs):
    return header(name, size=len(content), **kwargs) + content + bytes((-len(content)) % 512)


def archive(extra=b''):
    return header('.', directory=True) + entry('PG_VERSION', b'17\n') + extra + bytes(1024)


def encrypted(data, *, key=KEY, marker=MARKER, identity=IDENTITY):
    output = io.BytesIO()
    timer = Deadline(marker, wall_time=lambda: 120, monotonic=lambda: 1000)
    writer = crypto._EncryptWriter(output, key, marker, identity, LIMITS, timer)
    for position in range(0, len(data), crypto.CHUNK_BYTES):
        writer.write(data[position:position + crypto.CHUNK_BYTES])
    writer.finish()
    return output.getvalue()


def verify(data, *, key=KEY, marker=MARKER, identity=IDENTITY, limits=LIMITS, timer=None):
    return crypto.verify_stream(io.BytesIO(data), key, marker, identity, limits, deadline=timer or deadline())


def frames(data):
    offset = crypto.HEADER.size + 16
    result = []
    while offset < len(data):
        _, _, size = crypto.RECORD.unpack_from(data, offset)
        end = offset + crypto.RECORD.size + size + 16
        result.append(data[offset:end])
        offset = end
    return data[:crypto.HEADER.size + 16], result


class MarkerTests(unittest.TestCase):
    def test_exact_roundtrip(self):
        self.assertEqual(parse_marker(marker_bytes(MARKER)), MARKER)
        self.assertEqual(marker_bytes(MARKER), b'{"created_at":100,"expires_at":200}\n')

    def test_rejects_wrong_fields_types_and_duplicates(self):
        bad = (b'{"created_at":true,"expires_at":200}', b'{"created_at":100.0,"expires_at":200}',
               b'{"created_at":100,"expires_at":200,"extra":0}',
               b'{"created_at":100,"created_at":101,"expires_at":200}',
               b'{"created_at":100,"expires_at":100}', b'{"created_at":100,"expires_at":86501}',
               b'{"created_at":NaN,"expires_at":200}', b'[]', b'\xff', b' ' * 513)
        for value in bad:
            with self.subTest(value=value), self.assertRaisesRegex(UpdateCopyError, 'marker_invalid'):
                parse_marker(value)

    def test_exact_expiry_boundary_and_restart(self):
        for wall in (200, 201):
            with self.assertRaisesRegex(UpdateCopyError, 'expired'):
                Deadline(MARKER, wall_time=lambda: wall, monotonic=lambda: 0)
        remaining = [199, 199]
        mono = [50, 50]
        timer = Deadline(MARKER, wall_time=lambda: remaining[0], monotonic=lambda: mono[0])
        mono[0] = 51
        with self.assertRaisesRegex(UpdateCopyError, 'expired'):
            timer.check()

    def test_rollback_cannot_extend_and_bad_clocks_refused(self):
        wall, mono = [150], [0]
        timer = Deadline(MARKER, wall_time=lambda: wall[0], monotonic=lambda: mono[0])
        wall[0], mono[0] = 110, 50
        with self.assertRaisesRegex(UpdateCopyError, 'expired'):
            timer.check()
        for bad in (float('nan'), float('inf'), True, 99):
            with self.subTest(bad=bad), self.assertRaises(UpdateCopyError):
                Deadline(MARKER, wall_time=lambda: bad, monotonic=lambda: 0)

    @staticmethod
    def window(age=0):
        created = 1791547200
        timestamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
        return created, {'as_of': timestamp(created), 'oldest_source_at': timestamp(created - age),
                         'expired': False, 'invalid_source_clock': False}

    def test_window_no_api_and_29_day_boundary(self):
        now, window = self.window()
        window['oldest_source_at'] = None
        self.assertEqual(marker_from_window(window, created_at=now), Marker(now, now + 86400))
        now, window = self.window(29 * 86400)
        self.assertEqual(marker_from_window(window, created_at=now), Marker(now, now + 23 * 3600))

    def test_window_floors_fraction(self):
        now, window = self.window(29 * 86400)
        window['oldest_source_at'] = window['oldest_source_at'].replace('+00:00', '.999999+00:00')
        self.assertEqual(marker_from_window(window, created_at=now).expires_at, now + 23 * 3600)

    def test_window_rejects_stale_false_proof_and_invalid_types(self):
        now, original = self.window()
        changes = ({'expired': True}, {'invalid_source_clock': True}, {'expired': 0}, {'extra': 1},
                   {'as_of': '2026-01-01T00:00:00'}, {'oldest_source_at': 'infinity'},
                   {'oldest_source_at': datetime.fromtimestamp(now + 1, timezone.utc).isoformat()},
                   {'oldest_source_at': datetime.fromtimestamp(now - 29 * 86400 - 1, timezone.utc).isoformat()})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(UpdateCopyError):
                marker_from_window(dict(original, **change), created_at=now)
        # RR-24: a slow source-age read is allowed for its own duration only.
        with self.assertRaisesRegex(UpdateCopyError, 'stale'):
            marker_from_window(original, created_at=now - 6)  # proof from the future
        with self.assertRaisesRegex(UpdateCopyError, 'stale'):
            marker_from_window(original, created_at=now + WINDOW_READ_SECONDS + 6)

    def test_slow_read_keeps_expiry_from_oldest_source(self):
        # A 60-second read on a large catalogue: the proof is older, but the
        # copy still expires by the oldest source's limit, never later.
        now, window = self.window(29 * 86400)
        late = now + WINDOW_READ_SECONDS
        self.assertEqual(marker_from_window(window, created_at=late), Marker(late, now + 23 * 3600))


class CodecTests(unittest.TestCase):
    def test_small_and_multiple_chunk_roundtrip(self):
        for content in (b'', b'x' * (crypto.CHUNK_BYTES * 2 + 123)):
            with self.subTest(size=len(content)):
                data = archive(entry('base', directory=True) + entry('base/1', content))
                proof = verify(encrypted(data))
                self.assertEqual(proof.archive_bytes, len(data))
                self.assertEqual(proof.file_bytes, 3 + len(content))
                self.assertEqual(proof.entry_count, 4)
                self.assertEqual(proof.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(set(proof.as_dict()), {'copy_id', 'database_id', 'schema_version',
                    'created_at', 'expires_at', 'archive_bytes', 'entry_count', 'file_bytes', 'sha256'})

    def test_cipher_contains_no_plaintext_and_nonce_is_fresh(self):
        source = archive(entry('base', b'private source bytes never on disk'))
        first, second = encrypted(source), encrypted(source)
        self.assertNotIn(b'private source bytes', first)
        self.assertNotEqual(first, second)

    def test_wrong_key_and_identity_and_marker(self):
        value = encrypted(archive())
        with self.assertRaisesRegex(UpdateCopyError, 'authentication_failed'):
            verify(value, key=b'q' * 32)
        for identity in (replace(IDENTITY, copy_id='a' * 32), replace(IDENTITY, schema_version=26),
                         replace(IDENTITY, database_id='00000000-0000-0000-0000-000000000000')):
            with self.subTest(identity=identity), self.assertRaisesRegex(UpdateCopyError, 'identity_mismatch'):
                verify(value, identity=identity)
        other = Marker(100, 201)
        with self.assertRaisesRegex(UpdateCopyError, 'identity_mismatch'):
            verify(value, marker=other, timer=Deadline(other, wall_time=lambda: 120, monotonic=lambda: 0))

    def test_header_data_and_final_tamper(self):
        encoded = encrypted(archive())
        for position in (10, crypto.HEADER.size, crypto.HEADER.size + 16 + crypto.RECORD.size + 700, len(encoded) - 1):
            broken = bytearray(encoded)
            broken[position] ^= 1
            with self.subTest(position=position), self.assertRaises(UpdateCopyError):
                verify(bytes(broken))

    def test_truncation_every_structural_boundary_and_append(self):
        value = encrypted(archive())
        positions = (0, 1, crypto.HEADER.size - 1, crypto.HEADER.size + 15,
                     crypto.HEADER.size + 16, len(value) - 1, len(value) - crypto.FINAL.size - 16)
        for position in positions:
            with self.subTest(position=position), self.assertRaises(UpdateCopyError):
                verify(value[:position])
        with self.assertRaisesRegex(UpdateCopyError, 'trailing_data'):
            verify(value + b'X')

    def test_reorder_repeat_omit_and_unknown_frame(self):
        value = encrypted(archive(entry('large', b'x' * (crypto.CHUNK_BYTES * 2))))
        top, records = frames(value)
        variants = (records[1:2] + records[:1] + records[2:], records[:1] + records,
                    records[1:], records[:-1])
        for variant in variants:
            with self.subTest(length=len(variant)), self.assertRaises(UpdateCopyError):
                verify(top + b''.join(variant))
        broken = bytearray(value)
        broken[len(top)] = ord('Q')
        with self.assertRaisesRegex(UpdateCopyError, 'record_invalid'):
            verify(bytes(broken))

    def test_frame_bounds_checked_before_read_or_decrypt(self):
        value = bytearray(encrypted(archive()))
        size_offset = crypto.HEADER.size + 16 + 9
        struct.pack_into('>I', value, size_offset, 0xffffffff)
        with self.assertRaisesRegex(UpdateCopyError, 'size_limit'):
            verify(bytes(value))

    def test_archive_and_entry_limits(self):
        data = encrypted(archive(entry('extra', b'x' * 1000)))
        with self.assertRaisesRegex(UpdateCopyError, 'size_limit'):
            verify(data, limits=crypto.CopyLimits(2048, 10))
        with self.assertRaisesRegex(UpdateCopyError, 'file_limit'):
            verify(data, limits=crypto.CopyLimits(10000, 2))
        for args in ((True, 2), (1024, 2), (4096, True), (4096, 1), (4096, 1000001)):
            with self.subTest(args=args), self.assertRaises(UpdateCopyError):
                crypto.CopyLimits(*args)

    def test_expiry_during_stream(self):
        wall = [120]
        timer = Deadline(MARKER, wall_time=lambda: wall[0], monotonic=lambda: 0)
        class Expiring(io.BytesIO):
            def read(self, count):
                data = super().read(count)
                if self.tell() > crypto.HEADER.size + 16:
                    wall[0] = 200
                return data
        with self.assertRaisesRegex(UpdateCopyError, 'expired'):
            crypto.verify_stream(Expiring(encrypted(archive())), KEY, MARKER, IDENTITY, LIMITS, deadline=timer)

    def test_authenticated_but_wrong_final_digest_and_short_chunk_sequence(self):
        value = encrypted(archive())
        top, records = frames(value)
        unpacked = crypto.HEADER.unpack(top[:crypto.HEADER.size])
        prefix = unpacked[-1]
        frame = records[-1][:crypto.RECORD.size]
        _, index, _ = crypto.RECORD.unpack(frame)
        aead = crypto.AESGCM(KEY)
        final = aead.decrypt(prefix + index.to_bytes(8, 'big'), records[-1][crypto.RECORD.size:], top[:crypto.HEADER.size] + frame)
        altered = bytearray(final)
        altered[-1] ^= 1
        bad_final = frame + aead.encrypt(prefix + index.to_bytes(8, 'big'), bytes(altered), top[:crypto.HEADER.size] + frame)
        with self.assertRaisesRegex(UpdateCopyError, 'final_invalid'):
            verify(top + records[0] + bad_final)
        # A valid key cannot turn a noncanonical extra DATA after a short chunk
        # into an accepted archive, even if a sender recomputed all tags.
        new_frame = crypto.RECORD.pack(b'D', 2, 1)
        extra = new_frame + aead.encrypt(prefix + (2).to_bytes(8, 'big'), b'x', top[:crypto.HEADER.size] + new_frame)
        with self.assertRaisesRegex(UpdateCopyError, 'size_limit'):
            verify(top + records[0] + extra)

    def test_identity_validation(self):
        for args in (('A' * 32, IDENTITY.database_id, 25), ('a' * 32, 'wrong', 25),
                     ('a' * 32, IDENTITY.database_id, True), ('a' * 32, IDENTITY.database_id, 0)):
            with self.subTest(args=args), self.assertRaises(UpdateCopyError):
                crypto.CopyIdentity(*args)


class ArchiveTests(unittest.TestCase):
    def test_paths_duplicates_and_missing_parent_rejected(self):
        for name in ('/absolute', '../escape', 'a/../escape', 'a//b', './other', 'a\\b', 'x:y', 'a/b', 'PG_VERSION'):
            with self.subTest(name=name), self.assertRaises(UpdateCopyError):
                verify(encrypted(archive(entry(name, b'x'))))

    def test_special_and_link_entries_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
                     tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME):
            with self.subTest(kind=kind), self.assertRaises(UpdateCopyError):
                verify(encrypted(archive(entry('bad', kind=kind, link='elsewhere' if kind in (b'1', b'2') else ''))))

    def test_root_mandatory_and_postgres_version_and_stopped(self):
        cases = (entry('PG_VERSION', b'17\n') + bytes(1024),
                 header('.', directory=True) + entry('PG_VERSION', b'16\n') + bytes(1024),
                 header('.', directory=True) + bytes(1024), archive(entry('postmaster.pid', b'123\n')))
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(UpdateCopyError):
                verify(encrypted(data))

    def test_tar_checksum_padding_eof_and_trailing_garbage(self):
        value = archive()
        checksum = bytearray(value)
        checksum[1] ^= 1
        padding = bytearray(value)
        padding[512 * 2 + 3] = 1
        cases = (bytes(checksum), bytes(padding), value[:-512], value + bytes(512), value + b'trailer')
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(UpdateCopyError):
                verify(encrypted(data))

    def test_privileged_mode_rejected(self):
        info = tarfile.TarInfo('bad')
        info.mode = 0o4755
        with self.assertRaisesRegex(UpdateCopyError, 'entry_invalid'):
            verify(encrypted(archive(info.tobuf(tarfile.USTAR_FORMAT))))


class RestorePassTests(unittest.TestCase):
    """Real stream and authentication on Windows too; filesystem primitives tested natively."""
    def test_wrong_key_never_opens_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'copy.enc'
            path.write_bytes(encrypted(archive()))
            with patch.object(crypto, '_linux_filesystem'), patch.object(crypto, '_safe_cipher', lambda value: open(value, 'rb')), \
                    patch.object(crypto, '_RestoreTarget') as target:
                with self.assertRaisesRegex(UpdateCopyError, 'authentication_failed'):
                    crypto.restore_copy(path, Path(temp) / 'target', b'x' * 32, MARKER, IDENTITY, LIMITS, deadline=deadline())
                target.assert_not_called()

    def test_invalid_authenticated_archive_never_opens_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'copy.enc'
            path.write_bytes(encrypted(archive(entry('../escape', b'secret'))))
            with patch.object(crypto, '_linux_filesystem'), patch.object(crypto, '_safe_cipher', lambda value: open(value, 'rb')), \
                    patch.object(crypto, '_RestoreTarget') as target:
                with self.assertRaisesRegex(UpdateCopyError, 'archive_path_invalid'):
                    crypto.restore_copy(path, 'unused', KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
                target.assert_not_called()

    def test_restore_reuses_same_open_fd_and_verifies_twice(self):
        captures = []
        class Target:
            def __init__(self, path, timer): self.files = []
            def directory(self, parts, info): pass
            def file(self, parts): return io.BytesIO()
            def finish_file(self, output, info): captures.append(output.getvalue())
            def finish(self): captures.append('finished')
            def close(self): pass
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'copy.enc'
            path.write_bytes(encrypted(archive(entry('data', b'hello'))))
            with patch.object(crypto, '_linux_filesystem'), patch.object(crypto, '_safe_cipher', side_effect=lambda value: open(value, 'rb')) as opened, \
                    patch.object(crypto, '_RestoreTarget', Target), patch.object(crypto, '_DecryptReader', wraps=crypto._DecryptReader) as reader:
                proof = crypto.restore_copy(path, 'unused', KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(reader.call_count, 2)
                self.assertIs(reader.call_args_list[0].args[0], reader.call_args_list[1].args[0])
                self.assertEqual(captures, [b'17\n', b'hello', 'finished'])
                self.assertEqual(proof.entry_count, 3)

    def test_mutation_between_passes_is_refused_before_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'copy.enc'
            value = encrypted(archive(entry('data', b'hello')))
            path.write_bytes(value)
            def changed_target(*args):
                # Another descriptor alters ciphertext after the first verification.
                with open(path, 'r+b') as stream:
                    stream.seek(crypto.HEADER.size + 16 + crypto.RECORD.size + 100)
                    byte = stream.read(1)
                    stream.seek(-1, 1)
                    stream.write(bytes([byte[0] ^ 1]))
                return DummyTarget()
            class DummyTarget:
                def close(self): pass
            with patch.object(crypto, '_linux_filesystem'), patch.object(crypto, '_safe_cipher', lambda value: open(value, 'rb', buffering=0)), \
                    patch.object(crypto, '_RestoreTarget', side_effect=changed_target):
                with self.assertRaisesRegex(UpdateCopyError, 'authentication_failed'):
                    crypto.restore_copy(path, 'unused', KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / 'application' / 'ops' / 'update_copy_crypto.py'
        spec = importlib.util.spec_from_file_location('update_copy_cli_test', path)
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    @staticmethod
    def arguments(mode='verify'):
        return [mode, '--ciphertext', str(ROOT / 'copy.enc'), '--key-file', str(ROOT / 'copy.key'),
                '--marker-file', str(ROOT / 'marker.json'), '--copy-id', IDENTITY.copy_id,
                '--database-id', IDENTITY.database_id, '--schema-version', '25',
                '--max-bytes', str(LIMITS.max_bytes), '--max-files', str(LIMITS.max_files)]

    def test_entrypoint_not_shadowed_when_pythonpath_contains_application(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'application'))
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'application' / 'ops' / 'update_copy_crypto.py'), '--help'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--ciphertext', result.stdout)

    def test_non_root_refused_before_control_reads(self):
        with patch.object(self.cli.sys, 'platform', 'linux'), patch.object(self.cli.os, 'geteuid', return_value=10001, create=True), \
                patch.object(self.cli, '_root_file') as opened, patch.object(self.cli.sys, 'stderr', io.StringIO()) as errors:
            self.assertEqual(self.cli.main(self.arguments()), 2)
            opened.assert_not_called()
            self.assertEqual(json.loads(errors.getvalue()), {'error': 'update_copy_root_linux_required'})

    def test_success_receipt_and_errors_contain_no_paths(self):
        proof = verify(encrypted(archive()))
        with patch.object(self.cli.sys, 'platform', 'linux'), patch.object(self.cli.os, 'geteuid', return_value=0, create=True), \
                patch.object(self.cli, '_root_file', side_effect=[KEY, marker_bytes(MARKER)]), \
                patch.object(self.cli, 'verify_copy', return_value=proof) as called, patch.object(self.cli.sys, 'stdout', io.StringIO()) as output:
            self.assertEqual(self.cli.main(self.arguments()), 0)
            self.assertEqual(json.loads(output.getvalue()), proof.as_dict())
            self.assertEqual(called.call_args.args[0], str(ROOT / 'copy.enc'))
        with patch.object(self.cli.sys, 'platform', 'linux'), patch.object(self.cli.os, 'geteuid', return_value=0, create=True), \
                patch.object(self.cli, '_root_file', side_effect=OSError('secret path /key/copy.key')), \
                patch.object(self.cli.sys, 'stderr', io.StringIO()) as errors:
            self.assertEqual(self.cli.main(self.arguments()), 2)
            self.assertEqual(json.loads(errors.getvalue()), {'error': 'update_copy_io_failed'})

    def test_argument_combinations_before_control_reads(self):
        for args in (self.arguments('create'), self.arguments('restore'), self.arguments() + ['--source', '/source']):
            with self.subTest(args=args), patch.object(self.cli.sys, 'platform', 'linux'), \
                    patch.object(self.cli.os, 'geteuid', return_value=0, create=True), patch.object(self.cli, '_root_file') as opened, \
                    patch.object(self.cli.sys, 'stderr', io.StringIO()):
                self.assertEqual(self.cli.main(args), 2)
                opened.assert_not_called()


@unittest.skipUnless(sys.platform == 'linux' and os.geteuid() == 0, 'root Linux filesystem is exercised in disposable native acceptance')
class NativeFilesystemTests(unittest.TestCase):
    def test_real_create_verify_restore_metadata_and_stopped_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / 'source', root / 'target'
            source.mkdir(mode=0o700)
            target.mkdir()
            (source / 'PG_VERSION').write_bytes(b'17\n')
            (source / 'base').mkdir(mode=0o700)
            (source / 'base' / '1').write_bytes(b'postgres synthetic page' * 10000)
            for path in source.rglob('*'):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
                os.chown(path, 999, 999)
                os.utime(path, (100, 100))
            os.chown(source, 999, 999)
            os.utime(source, (100, 100))
            cipher = root / 'copy.partial'
            proof = crypto.create_copy(source, cipher, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
            self.assertEqual(crypto.verify_copy(cipher, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline()), proof)
            os.chmod(target, 0o777)
            with self.assertRaisesRegex(UpdateCopyError, 'not_empty_owned'):
                crypto.restore_copy(cipher, target, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
            self.assertEqual(list(target.iterdir()), [])
            os.chmod(target, 0o700)
            self.assertEqual(crypto.restore_copy(cipher, target, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline()), proof)
            for old in [source, *source.rglob('*')]:
                new = target / old.relative_to(source)
                a, b = old.stat(), new.stat()
                self.assertEqual((a.st_uid, a.st_gid, a.st_mode, int(a.st_mtime)), (b.st_uid, b.st_gid, b.st_mode, int(b.st_mtime)))
                if old.is_file(): self.assertEqual(old.read_bytes(), new.read_bytes())
            with self.assertRaisesRegex(UpdateCopyError, 'not_empty_owned'):
                crypto.restore_copy(cipher, target, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
            (source / 'postmaster.pid').write_text('123')
            with self.assertRaisesRegex(UpdateCopyError, 'database_not_stopped'):
                crypto.create_copy(source, root / 'bad.partial', KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())

    def test_fifo_and_cipher_permissions_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'copy.enc'
            os.mkfifo(path, 0o600)
            with self.assertRaisesRegex(UpdateCopyError, 'cipher_invalid'):
                crypto.verify_copy(path, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
            path.unlink()
            path.write_bytes(encrypted(archive()))
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(UpdateCopyError, 'cipher_invalid'):
                crypto.verify_copy(path, KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())

    def test_symlink_hardlink_and_target_escape_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            (source / 'PG_VERSION').write_bytes(b'17\n')
            (source / 'escape').symlink_to('/etc')
            with self.assertRaisesRegex(UpdateCopyError, 'source_type_invalid'):
                crypto.create_copy(source, root / 'one.partial', KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())
            (source / 'escape').unlink()
            os.link(source / 'PG_VERSION', source / 'hard')
            with self.assertRaisesRegex(UpdateCopyError, 'source_type_invalid'):
                crypto.create_copy(source, root / 'two.partial', KEY, MARKER, IDENTITY, LIMITS, deadline=deadline())


if __name__ == '__main__':
    unittest.main()
