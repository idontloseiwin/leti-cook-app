"""Persistent root hold: no timers, no writes, collection-only admission."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from update_hold import UpdateHoldGuard
from update_runtime_protocol import HOLD_FILENAME, hold_bytes, parse_hold
from test_runtime_gates import runtime
from hot_runtime import HotRuntimeUnavailable
from self_hosted_onboarding import SetupManager
from self_hosted_settings import SettingsError

OP = 'a' * 32


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory/HOLD_FILENAME
        self.guard = UpdateHoldGuard(self.directory, strict_owner=False)
        if os.name == 'nt':
            # Windows cannot open a directory with Python's os.open. Only this
            # test adapter replaces that FD; hold files/JSON are still real.
            adapter = patch.object(self.guard, '_directory', side_effect=lambda: os.open(__file__, os.O_RDONLY))
            adapter.start()
            self.addCleanup(adapter.stop)

    def test_exact_hold_survives_new_guard_and_never_expires_or_changes_file(self):
        self.path.write_bytes(hold_bytes(OP))
        before = self.path.read_bytes()
        for _ in range(3):
            self.assertEqual(self.guard.check(), {'state':'held','operation_id':OP})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        self.path.unlink()
        self.assertEqual(self.guard.check()['state'], 'absent')

    def test_invalid_files_fail_closed_and_are_not_deleted(self):
        for data in (b'{}', b'{"operation_id":"a","extra":1}',
                     b'{"operation_id":"' + OP.encode() + b'","operation_id":"' + OP.encode() + b'"}',
                     b'x'*513, b'{"operation_id":null}', b'{"operation_id":NaN}'):
            with self.subTest(data_size=len(data)):
                self.path.write_bytes(data)
                self.assertEqual(self.guard.check(), {'state':'invalid','operation_id':None})
                self.assertEqual(self.path.read_bytes(), data)

    def test_only_absent_environment_disables_and_arbitrary_paths_never_open(self):
        self.assertEqual(UpdateHoldGuard.from_environment({}).check()['state'], 'disabled')
        for env in ({'LIC_UPDATE_MARKER_DIRECTORY': str(self.directory)},
                    {'LIC_UPDATE_CIPHER_DIRECTORY':'/run/leti-cook-update/cipher'}):
            self.assertEqual(UpdateHoldGuard.from_environment(env).check()['state'], 'invalid')
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_root_directory_and_file_types_are_enforced(self):
        guard = UpdateHoldGuard(self.directory)
        bad = SimpleNamespace(st_mode=stat.S_IFDIR|0o775, st_uid=0)
        with patch('update_hold.os.open',return_value=12), patch('update_hold.os.fstat',return_value=bad), patch('update_hold.os.close') as close:
            with self.assertRaises(ValueError): guard._directory()
            close.assert_called_once_with(12)
        bad = SimpleNamespace(st_mode=stat.S_IFDIR|0o750, st_uid=10001)
        with patch('update_hold.os.open',return_value=12), patch('update_hold.os.fstat',return_value=bad), patch('update_hold.os.close'):
            with self.assertRaises(ValueError): guard._directory()

    def test_fifo_hardlink_or_backend_owned_file_is_not_accepted(self):
        self.path.write_bytes(hold_bytes(OP))
        for mode, owner, links in ((stat.S_IFIFO|0o640,0,1), (stat.S_IFREG|0o640,0,2),
                                   (stat.S_IFREG|0o640,10001,1), (stat.S_IFREG|0o666,0,1)):
            with self.subTest(mode=mode,owner=owner,links=links):
                self.guard.strict_owner = True
                info=SimpleNamespace(st_mode=mode,st_uid=owner,st_nlink=links,st_size=80)
                with patch.object(self.guard,'_directory',return_value=99), patch('update_hold.os.open',return_value=98), patch('update_hold.os.fstat',return_value=info), patch('update_hold.os.close'):
                    self.assertEqual(self.guard.check()['state'],'invalid')

    def test_real_runtime_holds_only_collection_and_preserves_cleaner_priority(self):
        controller, clocks = runtime()
        controller.update_hold_guard = self.guard
        self.path.write_bytes(hold_bytes(OP))
        controller.initialize()
        state=controller.status()
        self.assertTrue(state['api_read']['allowed'])
        self.assertTrue(state['user_work']['allowed'])
        self.assertEqual(state['collection']['reason'],'update_hold')
        with self.assertRaises(HotRuntimeUnavailable): controller.require_collection_ready()
        controller._pause_requested = True
        self.path.unlink()
        self.assertEqual(controller.status()['collection']['reason'],'collection_priority')
        self.assertTrue(controller._pause_requested)

    def test_malformed_hold_stays_effective_during_cleanup_backoff(self):
        controller, _ = runtime()
        controller.initialize()
        controller.update_hold_guard = self.guard
        controller._retry_at = float('inf')
        controller._failed = True
        self.path.write_bytes(b'broken')
        controller.step()
        state=controller.status()
        self.assertEqual(state['collection']['reason'],'update_hold_invalid')
        self.assertTrue(state['api_read']['allowed'])
        self.assertTrue(state['user_work']['allowed'])

    def test_hold_appearing_while_setup_waits_for_lock_prevents_thread_start(self):
        manager=SetupManager.__new__(SetupManager)
        manager.lock=threading.RLock()
        with patch('hot_runtime.require_hot_runtime_ready',side_effect=[None,HotRuntimeUnavailable()]) as gate:
            with self.assertRaisesRegex(SettingsError,'maintenance_not_ready'):
                manager.prepare(1)
        self.assertEqual(gate.call_count,2)


if __name__=='__main__': unittest.main()
