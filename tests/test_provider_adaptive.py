"""Shared slowdown through actual bounded files; portable lock simulator only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import provider_admission as pa
from test_provider_admission import ThreadLockSimulator, Clock, IDENTITY, GENERATION, BOOT


class AdaptiveAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock, self.locks = Clock(), ThreadLockSimulator()
        self.directory = Path(self.temp.name) / 'gate'
        self.config = pa.AdmissionConfig(IDENTITY, GENERATION, BOOT, 8)
        self.gate = pa.AdmissionGate.initialize(self.directory, self.config,
            _locks=self.locks, _clock=self.clock)
        self.other = pa.AdmissionGate(self.directory, self.config,
            _locks=self.locks, _clock=self.clock)
        self.permits = []
        self.addCleanup(self.close_permits)
        randomness = patch.object(pa.random, 'random', return_value=0.25)
        randomness.start()
        self.addCleanup(randomness.stop)

    def close_permits(self):
        for permit in self.permits:
            permit.close()

    def take(self, gate=None):
        permit = (gate or self.gate).acquire('auto')
        self.permits.append(permit)
        return permit

    def state(self):
        return json.loads((self.directory / 'state.json').read_text())

    def blocked(self, gate=None):
        gate = gate or self.gate
        with patch.object(gate, '_wait', side_effect=pa.AdmissionTimeout('test wait reached')):
            with self.assertRaises(pa.AdmissionTimeout):
                gate.acquire('operator')

    def reduce(self, *, delay=2):
        permit = self.take()
        permit.observe(rate_seconds=delay)
        permit.close()

    def after_cooldown(self):
        self.clock.value = self.state()['cooldown_until'] + 0.01

    def test_one_reduction_for_parallel_responses_across_gate_instances(self):
        permits = [self.take(self.gate if index % 2 else self.other) for index in range(8)]
        for permit in permits:
            permit.observe(rate_seconds=3)
        state = self.state()
        self.assertEqual((state['effective'], state['adaptive']['epoch']), (4, 1))
        self.assertEqual(state['cooldown_until'], 103.25)
        self.assertEqual(self.gate.snapshot()['active'], 8)
        self.blocked(self.other)

    def test_late_parallel_retry_after_extends_pause_without_second_halve(self):
        first, longer, shorter = self.take(), self.take(self.other), self.take()
        first.observe(rate_seconds=1)
        first.close()
        self.clock.value += 0.1
        longer.observe(rate_seconds=60)
        longer.close()
        self.clock.value += 1
        shorter.observe(rate_seconds=1)
        shorter.close()
        state = self.state()
        self.assertEqual((state['effective'], state['adaptive']['epoch']), (4, 1))
        self.assertAlmostEqual(state['cooldown_until'], 160.1)
        self.clock.value = 102
        self.blocked(self.other)
        self.after_cooldown()
        probe = self.take(self.other)
        self.blocked()
        probe.observe(success=True)
        probe.close()
        self.assertEqual(self.state()['adaptive']['healthy_since'], self.clock.value)

    def test_shorter_parallel_retry_after_cannot_shorten_long_pause(self):
        first, shorter = self.take(self.other), self.take()
        first.observe(rate_seconds=60)
        first.close()
        self.clock.value += 1
        shorter.observe(rate_seconds=1)
        shorter.close()
        state = self.state()
        self.assertEqual((state['effective'], state['adaptive']['epoch']), (4, 1))
        self.assertEqual(state['cooldown_until'], 160)
        self.blocked()

    def test_late_rate_during_probe_preserves_single_probe_and_extended_pause(self):
        late, first = self.take(), self.take(self.other)
        first.observe(rate_seconds=1)
        first.close()
        self.after_cooldown()
        probe = self.take(self.other)
        probe_identity = self.state()['adaptive']['probe']
        late.observe(rate_seconds=60)
        late.close()
        state = self.state()
        self.assertEqual(state['adaptive']['probe'], probe_identity)
        self.assertEqual((state['effective'], state['adaptive']['epoch']), (4, 1))
        self.blocked()
        probe.observe(success=True)
        probe.close()
        state = self.state()
        self.assertTrue(state['adaptive']['probe_needed'])
        self.assertIsNone(state['adaptive']['healthy_since'])
        self.blocked(self.other)
        self.after_cooldown()
        with self.take() as replacement:
            replacement.observe(success=True)
        self.assertEqual(self.state()['effective'], 4)
        self.assertEqual(self.state()['adaptive']['healthy_since'], self.clock.value)

    def test_late_rate_after_recovery_discards_prior_healthy_period(self):
        late, first = self.take(), self.take(self.other)
        first.observe(rate_seconds=1)
        first.close()
        self.after_cooldown()
        with self.take(self.other) as probe:
            probe.observe(success=True)
        outstanding = self.take()
        self.clock.value += 31
        late.observe(rate_seconds=3)
        late.close()
        outstanding.observe(success=True)
        outstanding.close()
        state = self.state()
        self.assertEqual((state['effective'], state['adaptive']['epoch']), (4, 1))
        self.assertTrue(state['adaptive']['probe_needed'])
        self.assertIsNone(state['adaptive']['healthy_since'])
        self.blocked()
        self.after_cooldown()
        with self.take() as probe:
            probe.observe(success=True)
        self.clock.value += 29.9
        with self.take(self.other) as healthy:
            healthy.observe(success=True)
        self.assertEqual(self.state()['effective'], 4)
        self.clock.value += 0.11
        with self.take() as healthy:
            healthy.observe(success=True)
        self.assertEqual(self.state()['effective'], 5)

    def test_live_probe_still_blocks_after_late_retry_after_expires(self):
        late, first = self.take(), self.take()
        first.observe(rate_seconds=1)
        first.close()
        self.after_cooldown()
        probe = self.take(self.other)
        late.observe(rate_seconds=2)
        late.close()
        self.after_cooldown()
        self.blocked()  # Pause expiry is not evidence that this probe finished.
        probe.observe(success=True)
        probe.close()
        self.assertFalse(self.state()['adaptive']['probe_needed'])
        self.assertEqual(self.state()['effective'], 4)

    def test_only_one_probe_then_gradual_recovery(self):
        self.reduce()
        self.blocked()
        self.after_cooldown()
        probe = self.take(self.other)
        self.blocked()
        probe.observe(success=True)
        probe.close()
        self.assertFalse(self.state()['adaptive']['probe_needed'])
        self.assertEqual(self.state()['effective'], 4)
        self.clock.value += 29.9
        with self.take() as permit:
            permit.observe(success=True)
        self.assertEqual(self.state()['effective'], 4)
        self.clock.value += 0.11
        with self.take() as permit:
            permit.observe(success=True)
        self.assertEqual(self.state()['effective'], 5)
        for _ in range(6):
            self.clock.value += 30
            with self.take() as permit:
                permit.observe(success=True)
        self.assertEqual(self.state()['effective'], 8)

    def test_rejected_probe_starts_new_epoch_once(self):
        self.reduce()
        self.after_cooldown()
        with self.take() as probe:
            probe.observe(rate_seconds=60)
        state = self.state()
        self.assertEqual((state['effective'], state['adaptive']['epoch']), (2, 2))
        self.assertAlmostEqual(state['cooldown_until'] - self.clock.value, 60)
        self.blocked()

    def test_old_success_cannot_satisfy_new_probe(self):
        old_success, rate = self.take(), self.take()
        rate.observe(rate_seconds=2)
        rate.close()
        self.after_cooldown()
        old_success.observe(success=True)
        old_success.close()
        self.assertTrue(self.state()['adaptive']['probe_needed'])
        self.assertIsNone(self.state()['adaptive']['healthy_since'])
        probe = self.take()
        self.blocked(self.other)
        probe.close()

    def test_reduced_limit_counts_busy_slots_above_it(self):
        permits = [self.take() for _ in range(8)]
        permits[0].observe(rate_seconds=1)
        permits[0].close()
        self.after_cooldown()
        self.blocked()  # Seven live requests are still above the new limit4.
        for permit in permits[1:5]:
            permit.close()
        probe = self.take()
        self.assertEqual(self.gate.snapshot()['active'], 4)
        probe.close()

    def test_unobserved_probe_can_be_replaced_but_does_not_restore_limit(self):
        self.reduce()
        self.after_cooldown()
        first = self.take()
        first.close()
        self.assertTrue(self.state()['adaptive']['probe_needed'])
        with self.take(self.other) as replacement:
            replacement.observe(success=True)
        self.assertEqual(self.state()['effective'], 4)

    def test_dead_probe_metadata_reaped_from_kernel_lock_evidence(self):
        self.reduce()
        self.after_cooldown()
        probe = self.take()
        # Simulate process loss in the labelled lock double, not a native kill.
        probe._slot.close()
        probe._ticket.close()
        probe._slot = probe._ticket = None
        self.assertEqual(self.other.snapshot()['active'], 0)
        self.assertTrue(self.state()['adaptive']['probe_needed'])
        with self.take(self.other) as replacement:
            replacement.observe(success=True)

    def test_neutral_failed_probe_does_not_recover_or_spin(self):
        self.reduce()
        self.after_cooldown()
        with self.take() as probe:
            probe.observe(success=False)
        state = self.state()
        self.assertTrue(state['adaptive']['probe_needed'])
        self.assertEqual(state['cooldown_until'], self.clock.value + 1)
        self.assertEqual(state['effective'], 4)
        self.blocked()

    def test_non_rate_error_resets_success_period_without_reducing(self):
        self.reduce()
        self.after_cooldown()
        with self.take() as permit:
            permit.observe(success=True)
        self.clock.value += 31
        with self.take() as permit:
            permit.observe(success=False)
        with self.take() as permit:
            permit.observe(success=True)
        self.assertEqual(self.state()['effective'], 4)
        self.assertEqual(self.state()['adaptive']['healthy_since'], self.clock.value)

    def test_duplicate_or_closed_feedback_is_refused_without_second_mutation(self):
        permit = self.take()
        permit.observe(rate_seconds=1)
        before = self.state()
        with self.assertRaises(pa.AdmissionUnavailable):
            permit.observe(rate_seconds=1)
        self.assertEqual(self.state(), before)
        permit.close()
        with self.assertRaises(pa.AdmissionUnavailable):
            permit.observe(success=True)

    def test_unknown_feedback_commit_is_not_repeated(self):
        permit = self.take()
        with patch.object(self.gate, '_save', side_effect=OSError('unknown')) as save:
            with self.assertRaises(pa.AdmissionUnavailable):
                permit.observe(rate_seconds=2)
            with self.assertRaises(pa.AdmissionUnavailable):
                permit.observe(rate_seconds=2)
            self.assertEqual(save.call_count, 1)
        permit.close()

    def test_corrupt_adaptive_state_has_no_private_fallback(self):
        initial = self.state()
        for change in ({'epoch': True}, {'epoch': -1}, {'healthy_since': float('nan')},
                       {'probe': GENERATION}, {'probe_needed': 1}, {'extra': 1}):
            state = {**initial, 'adaptive': {**initial['adaptive'], **change}}
            (self.directory / 'state.json').write_text(json.dumps(state))
            with self.subTest(change=change), self.assertRaises(pa.AdmissionUnavailable):
                self.gate.acquire('auto')
        (self.directory / 'state.json').write_text(json.dumps(initial))


if __name__ == '__main__':
    unittest.main()
