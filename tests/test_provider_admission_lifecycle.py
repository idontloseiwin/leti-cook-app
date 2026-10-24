"""Restart fencing: native flock on Unix, labelled lock simulator on Windows."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from test_provider_admission import ThreadLockSimulator, IDENTITY, BOOT
import provider_admission as pa


class ProviderAdmissionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'admission'
        self.locks = pa._Flock() if os.name != 'nt' else ThreadLockSimulator()

    def start(self, count=2, boot=BOOT, identity=IDENTITY):
        return pa.AdmissionGate.for_backend(self.path, installation_id=identity,
            boot_id=boot, concurrency=count, _locks=self.locks)

    def test_restart_reuses_gate_and_ceiling_with_a_surviving_request(self):
        first = self.start()
        with first.acquire('auto', timeout=.5):
            second = self.start()
            self.assertEqual(first.config, second.config)
            self.assertEqual(second.snapshot()['active'], 1)
            with second.acquire('operator', timeout=.5):
                with self.assertRaises(pa.AdmissionTimeout):
                    first.acquire('interactive', timeout=.02)

    def test_changed_limit_requires_actual_old_lock_release(self):
        first = self.start()
        before = (self.path / 'state.json').read_bytes()
        with first.acquire('auto', timeout=.5):
            active = (self.path / 'state.json').read_bytes()
            with self.assertRaisesRegex(pa.AdmissionUnavailable, 'previous_callers_active'):
                self.start(count=3)
            self.assertEqual((self.path / 'state.json').read_bytes(), active)
        new = self.start(count=3)
        self.assertEqual(new.snapshot()['concurrency'], 3)
        self.assertNotEqual(new.config.generation, first.config.generation)
        with self.assertRaises(pa.AdmissionUnavailable):
            first.acquire('auto', timeout=.5)
        self.assertNotEqual((self.path / 'state.json').read_bytes(), before)

    def test_shrink_then_expand_never_replaces_lock_inodes(self):
        initial = self.start(count=4)
        files = json.loads((self.path / 'state.json').read_text())['files']
        self.start(count=1)
        final = self.start(count=4)
        after = json.loads((self.path / 'state.json').read_text())['files']
        self.assertEqual(files, after)
        self.assertNotEqual(initial.config.generation, final.config.generation)

    def test_boot_change_resets_monotonic_deadlines_only_after_drain(self):
        first = self.start()
        next_boot = '44444444-4444-4444-8444-444444444444'
        with first.acquire('operator', timeout=.5):
            with self.assertRaises(pa.AdmissionUnavailable):
                self.start(boot=next_boot)
        first.set_cooldown(60, effective=1)
        new = self.start(boot=next_boot)
        self.assertEqual(new.snapshot()['effective'], 2)
        self.assertFalse(new.snapshot()['cooling_down'])

    def test_other_installation_missing_or_corrupt_state_is_not_recreated(self):
        self.start()
        with self.assertRaises(pa.AdmissionUnavailable):
            self.start(identity='55555555-5555-4555-8555-555555555555')
        state = self.path / 'state.json'
        state.write_text('{}')
        with self.assertRaises(pa.AdmissionUnavailable):
            self.start()
        self.assertEqual(state.read_text(), '{}')
        state.unlink()
        with self.assertRaises(pa.AdmissionUnavailable):
            self.start()
        self.assertFalse(state.exists())

    def test_live_unregistered_ticket_also_prevents_reconfiguration(self):
        first = self.start()
        handle = first._lock_file('ticket-0.lock')
        self.assertTrue(handle.acquire())
        try:
            with self.assertRaisesRegex(pa.AdmissionUnavailable, 'previous_callers_active'):
                self.start(count=1)
        finally:
            handle.close()
        self.assertEqual(self.start(count=1).snapshot()['concurrency'], 1)


if __name__ == '__main__':
    unittest.main()
