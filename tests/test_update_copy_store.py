"""Offline ownership/expiry tests; systemd and privileged identity are fakes."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from application.update_copy_protocol import Marker, marker_bytes
from ops import update_copy_store as store

PROJECT = 'leti-cook-0123456789ab'
DATABASE_ID = '9a2fc4bc-9d4a-4bc4-b4be-69c7d37f2d23'
COPY_ID = 'a' * 32
NOW = 2000000000
MARKER = Marker(NOW, NOW + 3600)


@contextmanager
def unlocked(_layout):
    yield


def ancestors_without_privileged_identity(path):
    for entry in (path, *path.parents):
        if not stat.S_ISDIR(entry.lstat().st_mode) or entry.is_symlink():
            raise store.UpdateCopyError('update_copy_path_invalid')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='leti-copy-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.installation = self.directory / 'installation'
        self.units = self.directory / 'systemd'
        self.units.mkdir()
        self.calls = []
        self.modes = []
        self.owners = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, replacement in (
            ('_require_root', lambda: None),
            ('_safe_ancestors', ancestors_without_privileged_identity),
            ('_owner_mode', lambda info, mode, gid=0: None),
            ('_chown', lambda path, uid, gid: self.owners.append((Path(path), uid, gid))),
            ('_fsync_directory', lambda path: None),
            ('_metadata_lock', unlocked),
        ):
            self.stack.enter_context(patch.object(store, target, replacement))
        self.stack.enter_context(patch.object(store.os, 'chmod',
            lambda path, mode, **kwargs: self.modes.append((Path(path), mode))))

    def prepare(self):
        return store.prepare_layout(self.installation, PROJECT, DATABASE_ID,
                                    _systemd_directory=self.units)

    def systemctl(self, args):
        self.calls.append(list(args))

    def arm(self, layout=None, **kwargs):
        return store.arm_copy(layout or self.prepare(), copy_id=COPY_ID, marker=MARKER,
                              schema_version=25, run_systemctl=self.systemctl, now=NOW,
                              **kwargs)

    def cipher(self, layout):
        layout.partial_path.write_bytes(b'synthetic ciphertext')
        reservation = store.CopyReservation(layout, COPY_ID, 25, MARKER)
        store.publish_copy(reservation, now=NOW)
        return reservation

    def test_layout_is_separate_and_idempotent_with_exact_scope(self):
        layout = self.prepare()
        self.assertFalse(self.installation.exists())
        self.assertEqual(layout.root, self.directory / 'installation.update')
        self.assertEqual(layout.keys, self.directory / 'installation.update-keys')
        self.assertEqual(self.prepare(), layout)
        self.assertIn((layout.marker_directory, 0o750), self.modes)
        self.assertIn((layout.cipher_directory, 0o730), self.modes)
        self.assertIn((layout.marker_directory, 0, 10001), self.owners)
        receipt = json.loads((layout.owner / 'scope.json').read_text())
        self.assertEqual(receipt['project'], PROJECT)
        self.assertEqual(receipt['database_id'], DATABASE_ID)
        self.assertNotIn('systemd_directory', receipt)

    def test_preflight_and_prepare_reject_foreign_siblings_without_mutation(self):
        foreign = self.directory / 'installation.update-keys'
        foreign.mkdir()
        (foreign / 'mine').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'scope_exists'):
            store.validate_new_layout(self.installation)
        with self.assertRaises((ValueError, OSError)):
            self.prepare()
        self.assertFalse((self.directory / 'installation.update').exists())
        self.assertEqual((foreign / 'mine').read_text(), 'keep')

    def test_existing_scope_project_database_and_directory_identity_cannot_change(self):
        layout = self.prepare()
        for project, database in [('leti-cook-ffffffffffff', DATABASE_ID),
                                  (PROJECT, '19e56d8d-7a4e-49b2-9f99-9fd3c2ac1e6d')]:
            with self.subTest(project=project, database=database):
                with self.assertRaisesRegex(ValueError, 'scope_invalid'):
                    store.prepare_layout(self.installation, project, database,
                                         _systemd_directory=self.units)
        original = layout.cipher_directory.rename(layout.root / 'original-cipher')
        layout.cipher_directory.mkdir()
        with self.assertRaisesRegex(ValueError, 'scope_invalid'):
            store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW)
        self.assertTrue(original.is_dir())

    def test_bad_paths_and_identity_are_rejected(self):
        for path in ['relative', self.directory / 'has space', self.directory / 'percent%p']:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, 'path_invalid'):
                    store.validate_new_layout(path)
        for project, database in [('lic-old', DATABASE_ID), (PROJECT, 'not-a-uuid')]:
            with self.assertRaises(ValueError):
                store.prepare_layout(self.installation, project, database)
        self.assertFalse((self.directory / 'installation.update').exists())

    def test_symlinked_sibling_never_follows_or_deletes_outside_target(self):
        outside = self.directory / 'foreign'
        outside.mkdir()
        witness = outside / 'copy.key'
        witness.write_bytes(b'keep')
        link = self.directory / 'installation.update-keys'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('OS does not permit unprivileged symlink creation')
        with self.assertRaises((ValueError, OSError)):
            self.prepare()
        self.assertEqual(witness.read_bytes(), b'keep')

    def test_timer_is_active_before_key_exists_and_is_persistent_utc(self):
        layout = self.prepare()
        def systemctl(args):
            self.assertFalse(layout.key_path.exists())
            self.assertFalse(layout.cipher_path.exists())
            self.assertTrue(layout.marker_path.exists())
            self.systemctl(args)
        reservation = store.arm_copy(layout, copy_id=COPY_ID, marker=MARKER,
                                     schema_version=25, run_systemctl=systemctl, now=NOW)
        self.assertEqual(len(layout.key_path.read_bytes()), 32)
        self.assertEqual(self.calls[-1], ['is-active', '--quiet', 'leti-cook-copy-' + COPY_ID + '.timer'])
        units = store._unit_data(reservation)
        timer = units['leti-cook-copy-' + COPY_ID + '.timer'].decode()
        service = units['leti-cook-copy-' + COPY_ID + '.service'].decode()
        for required in ('Persistent=true', 'AccuracySec=1s', 'RandomizedDelaySec=0', ' UTC'):
            self.assertIn(required, timer)
        self.assertIn('Restart=on-failure', service)
        self.assertIn('StartLimitIntervalSec=0', service)
        self.assertNotIn('manage.py', service)
        self.assertIn('/owner/purge-helper.py', service)
        self.assertEqual(json.loads(layout.marker_path.read_bytes()),
                         {'created_at': NOW, 'expires_at': NOW + 3600})

    def test_arm_failure_never_creates_key_and_cannot_be_resumed_with_new_key(self):
        layout = self.prepare()
        def fail(_args):
            raise RuntimeError('synthetic systemd refusal')
        with self.assertRaises(RuntimeError):
            store.arm_copy(layout, copy_id=COPY_ID, marker=MARKER, schema_version=25,
                           run_systemctl=fail, now=NOW)
        self.assertFalse(layout.key_path.exists())
        self.assertFalse(layout.cipher_path.exists())
        with self.assertRaisesRegex(ValueError, 'operation_exists'):
            self.arm(layout)
        self.assertTrue(store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW))

    def test_foreign_units_are_not_overwritten(self):
        layout = self.prepare()
        path = self.units / ('leti-cook-copy-' + COPY_ID + '.service')
        path.write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'unit_exists'):
            self.arm(layout)
        self.assertEqual(path.read_text(), 'foreign')
        self.assertFalse(layout.key_path.exists())
        self.assertFalse((layout.owner / store.OPERATION_FILENAME).exists())

    def test_purge_key_first_then_cipher_and_only_then_disarms(self):
        layout = self.arm().layout
        self.cipher(layout)
        layout.partial_path.write_bytes(b'partial')
        original = store._fixed_unlink
        deleted = []
        def unlink(directory, name):
            deleted.append(name)
            original(directory, name)
        def systemctl(args):
            self.assertFalse(layout.key_path.exists())
            self.assertFalse(layout.cipher_path.exists())
            self.assertFalse(layout.partial_path.exists())
            self.systemctl(args)
        with patch.object(store, '_fixed_unlink', unlink):
            self.assertTrue(store.purge_copy(layout, force=True, run_systemctl=systemctl, now=NOW))
        self.assertEqual(deleted[:3], ['copy.key', 'copy.enc', 'copy.partial'])
        self.assertFalse(layout.marker_path.exists())
        self.assertFalse((layout.owner / store.OPERATION_FILENAME).exists())
        self.assertTrue(layout.root.is_dir())
        self.assertFalse(store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW))

    def test_failed_cipher_cleanup_keeps_timer_and_root_journal(self):
        layout = self.arm().layout
        self.cipher(layout)
        original = store._fixed_unlink
        def unlink(directory, name):
            if name == 'copy.enc':
                raise PermissionError('synthetic denied unlink')
            original(directory, name)
        self.calls.clear()
        with patch.object(store, '_fixed_unlink', unlink):
            with self.assertRaises(PermissionError):
                store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW)
        self.assertFalse(layout.key_path.exists())
        self.assertTrue(layout.cipher_path.exists())
        self.assertTrue(layout.marker_path.exists())
        self.assertTrue((layout.owner / store.OPERATION_FILENAME).exists())
        self.assertEqual(self.calls, [])
        self.assertEqual(len(list(self.units.iterdir())), 2)

    def test_live_copy_is_kept_default_expired_copy_purged(self):
        layout = self.arm().layout
        self.cipher(layout)
        self.assertFalse(store.purge_copy(layout, run_systemctl=self.systemctl, now=NOW + 3599))
        self.assertTrue(layout.key_path.exists())
        self.assertTrue(store.purge_copy(layout, run_systemctl=self.systemctl, now=NOW + 3600))

    def test_invalid_or_missing_marker_cannot_extend_lifetime(self):
        layout = self.arm().layout
        layout.marker_path.write_bytes(b'{"created_at":true}')
        self.assertTrue(store.purge_copy(layout, run_systemctl=self.systemctl, now=NOW))
        layout = self.arm(layout).layout
        layout.marker_path.unlink()
        self.assertTrue(store.purge_copy(layout, run_systemctl=self.systemctl, now=NOW))

    def test_stale_timer_does_not_remove_another_copy(self):
        layout = self.arm().layout
        key = layout.key_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
            store.purge_copy(layout, expected_copy_id='b' * 32, force=True,
                             run_systemctl=self.systemctl, now=NOW)
        self.assertEqual(layout.key_path.read_bytes(), key)

    def test_helper_survives_installation_removal_and_does_not_import_it(self):
        layout = self.arm().layout
        self.installation.mkdir()
        self.installation.rmdir()
        helper = (layout.owner / store.HELPER_FILENAME).read_bytes()
        protocol = (layout.owner / store.PROTOCOL_FILENAME).read_bytes()
        self.assertEqual(helper, Path(store.__file__).read_bytes())
        self.assertTrue(protocol.startswith(b'"""Bounded'))
        self.assertTrue(store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW))

    def test_restore_requires_matching_identity_clock_live_marker_and_completed_cipher(self):
        layout = self.arm().layout
        self.cipher(layout)
        args = dict(expected_copy_id=COPY_ID, expected_schema_version=25,
                    expected_marker=MARKER, time_trusted=True, now=NOW)
        self.assertEqual(store.validate_restore(layout, **args).copy_id, COPY_ID)
        for changes, code in [({'expected_copy_id': 'b' * 32}, 'identity_mismatch'),
                              ({'expected_schema_version': 26}, 'identity_mismatch'),
                              ({'expected_marker': Marker(NOW, NOW + 100)}, 'identity_mismatch'),
                              ({'time_trusted': False}, 'clock_untrusted'),
                              ({'now': NOW - 1}, 'clock_invalid'),
                              ({'now': NOW + 3600}, 'expired')]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ValueError, code):
                    store.validate_restore(layout, **(args | changes))
        layout.partial_path.write_bytes(b'unfinished')
        with self.assertRaisesRegex(ValueError, 'partial_present'):
            store.validate_restore(layout, **args)

    def test_publish_never_overwrites_cipher_or_accepts_hardlinked_partial(self):
        reservation = self.arm()
        reservation.partial_path.write_bytes(b'partial')
        reservation.cipher_path.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'operation_exists'):
            store.publish_copy(reservation, now=NOW)
        self.assertEqual(reservation.cipher_path.read_bytes(), b'keep')
        reservation.cipher_path.unlink()
        os.link(reservation.partial_path, self.directory / 'external-link')
        with self.assertRaisesRegex(ValueError, 'file_invalid'):
            store.publish_copy(reservation, now=NOW)

    def test_secret_partial_creation_failure_stays_at_fixed_timer_covered_name(self):
        layout = self.prepare()
        real = store._fsync_directory
        def fail(path):
            if path == layout.keys:
                raise OSError('synthetic fsync failure')
            real(path)
        with patch.object(store, '_fsync_directory', fail):
            with self.assertRaises(OSError):
                self.arm(layout)
        self.assertEqual([p.name for p in layout.keys.iterdir()], ['copy.key'])
        self.assertEqual(len(list(self.units.iterdir())), 2)
        self.assertTrue(store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW))

    def test_clock_change_while_arming_creates_no_unwatched_key(self):
        layout = self.prepare()
        with patch.object(store.time, 'time', side_effect=[NOW, NOW + 3600]):
            with self.assertRaisesRegex(ValueError, 'expired'):
                store.arm_copy(layout, copy_id=COPY_ID, marker=MARKER, schema_version=25,
                               run_systemctl=self.systemctl)
        self.assertFalse(layout.key_path.exists())
        self.assertEqual(len(list(self.units.iterdir())), 2)

    def test_corrupt_or_missing_root_journal_deletes_fixed_secrets_without_guessing_units(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                # Separate owned installation per scenario.
                self.installation = self.directory / ('corrupt' if corrupt else 'missing')
                layout = self.arm().layout
                self.cipher(layout)
                operation = layout.owner / store.OPERATION_FILENAME
                if corrupt:
                    operation.write_bytes(b'{"copy_id":"../../foreign"}')
                else:
                    operation.unlink()
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, 'receipt_invalid'):
                    store.purge_copy(layout, expected_copy_id=COPY_ID, force=True,
                                     run_systemctl=self.systemctl, now=NOW)
                self.assertFalse(layout.key_path.exists())
                self.assertFalse(layout.cipher_path.exists())
                self.assertTrue(layout.marker_path.exists())
                self.assertEqual(self.calls, [])
                # Remove only synthetic test unit files before second scenario.
                for unit in self.units.iterdir():
                    unit.unlink()

    def test_unit_change_refuses_disarm_but_private_material_is_already_gone(self):
        layout = self.arm().layout
        self.cipher(layout)
        unit = self.units / ('leti-cook-copy-' + COPY_ID + '.service')
        unit.write_bytes(b'foreign replacement')
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'unit_changed'):
            store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW)
        self.assertFalse(layout.key_path.exists())
        self.assertFalse(layout.cipher_path.exists())
        self.assertEqual(unit.read_bytes(), b'foreign replacement')
        self.assertEqual(self.calls, [])

    def test_scope_duplicate_fields_and_boolean_version_are_rejected(self):
        layout = self.prepare()
        path = layout.owner / store.SCOPE_FILENAME
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"version":1', b'"version":true'))
        with self.assertRaisesRegex(ValueError, 'scope_invalid'):
            store.load_layout(self.installation, PROJECT, DATABASE_ID)
        path.write_bytes(original.replace(b'{', b'{"version":1,', 1))
        with self.assertRaisesRegex(ValueError, 'receipt_invalid'):
            store.load_layout(self.installation, PROJECT, DATABASE_ID)

    def test_invalid_wall_clock_does_not_keep_an_owned_copy_indefinitely(self):
        layout = self.arm().layout
        self.assertTrue(store.purge_copy(layout, run_systemctl=self.systemctl, now=float('nan')))

    def test_metadata_lock_contention_leaves_watcher_and_key(self):
        layout = self.arm().layout
        @contextmanager
        def busy(_layout):
            raise store.UpdateCopyError('update_copy_store_busy')
            yield
        self.calls.clear()
        with patch.object(store, '_metadata_lock', busy):
            with self.assertRaisesRegex(ValueError, 'store_busy'):
                store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW)
        self.assertTrue(layout.key_path.exists())
        self.assertEqual(self.calls, [])

    def test_read_rejects_hardlinks_and_cleanup_does_not_recurse(self):
        layout = self.arm().layout
        os.link(layout.key_path, self.directory / 'key-link')
        with self.assertRaisesRegex(ValueError, 'file_invalid'):
            store._read(layout.key_path, limit=32)
        layout.cipher_path.mkdir()
        (layout.cipher_path / 'keep').write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'cleanup_blocked'):
            store.purge_copy(layout, force=True, run_systemctl=self.systemctl, now=NOW)
        self.assertEqual((layout.cipher_path / 'keep').read_bytes(), b'keep')
        self.assertTrue(layout.marker_path.exists())


class IdentityPrimitiveTests(unittest.TestCase):
    def test_only_exact_root_owner_group_and_mode_are_admitted(self):
        store._owner_mode(SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o600), 0o600)
        for uid, gid, mode in ((10001, 0, 0o600), (0, 10001, 0o600),
                               (0, 0, 0o640), (0, 0, 0o1600), (0, 0, 0o700)):
            with self.subTest(uid=uid, gid=gid, mode=mode):
                with self.assertRaisesRegex(ValueError, 'permissions_invalid'):
                    store._owner_mode(SimpleNamespace(st_uid=uid, st_gid=gid,
                                                       st_mode=stat.S_IFREG | mode), 0o600)

    def test_non_root_and_non_posix_entrypoints_fail_before_filesystem_mutation(self):
        with patch.object(store.os, 'name', 'nt'):
            with self.assertRaisesRegex(ValueError, 'root_required'):
                store._require_root()
        with patch.object(store.os, 'name', 'posix'), \
                patch.object(store.os, 'geteuid', return_value=1000, create=True):
            with self.assertRaisesRegex(ValueError, 'root_required'):
                store._require_root()


if __name__ == '__main__':
    unittest.main()
