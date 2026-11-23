"""Initial loading/cleanup admission regressions; no database or YouTube."""
import json
import unittest
import threading
from unittest.mock import Mock, patch

import test_priority_retry as fixture
import test_hot_admission_diagnostics as diagnostic_fixture
from hot_runtime import HotRuntimeUnavailable, supervised_collection_handoff


class CleanupAdmissionTests(unittest.TestCase):
    setUp = diagnostic_fixture.HotAdmissionDiagnosticTests.setUp
    signed = diagnostic_fixture.HotAdmissionDiagnosticTests.signed
    remote = diagnostic_fixture.HotAdmissionDiagnosticTests.remote

    def test_signed_priority_finishes_only_supervised_unit_and_keeps_switch_closed(self):
        proof = self.signed(collection={'allowed': False, 'reason': 'collection_priority'})
        remote = self.remote(lambda: proof)
        with self.assertRaises(HotRuntimeUnavailable):
            remote.require_collection_ready()
        handoff = Mock()
        with supervised_collection_handoff(handoff):
            remote.require_collection_ready()
            remote.require_collection_ready()  # Same rule applies to cached proof.
        self.assertEqual(handoff.call_count, 2)
        self.assertFalse(proof['collection']['allowed'])
        with self.assertRaises(HotRuntimeUnavailable):
            remote.require_collection_ready()

    def test_hard_refusals_never_allow_supervised_completion(self):
        for reason in ('clock_changed', 'time_mismatch', 'database_unavailable',
                       'schema_invalid', 'update_hold_invalid', 'stopped'):
            with self.subTest(reason=reason):
                proof = self.signed(collection={'allowed': False, 'reason': reason})
                handoff = Mock()
                with supervised_collection_handoff(handoff):
                    with self.assertRaises(HotRuntimeUnavailable):
                        self.remote(lambda: proof).require_collection_ready()
                handoff.assert_not_called()


class CleanupPriorityTests(unittest.TestCase):
    def test_status_waits_for_atomic_priority_observation(self):
        controller, repository = fixture.runtime()
        started, finished = threading.Event(), threading.Event()
        observed = controller._priority.observe
        readers = []
        def read_status():
            started.set()
            controller.status()
            finished.set()
        def observe(page, now):
            reader = threading.Thread(target=read_status)
            readers.append(reader)
            reader.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.05), 'Status must not read a half-updated cause registry.')
            observed(page, now)
        controller._priority.observe = observe
        try:
            fixture.tick(controller, repository)
        finally:
            for reader in readers:
                reader.join(2)
        self.assertTrue(finished.is_set())
        self.assertFalse(controller.status()['maintenance_failed'])

    def test_unknown_age_by_itself_never_becomes_a_pause_cause(self):
        pending = fixture.PriorityRetrySet()
        pending.observe(fixture.page('video_metadata', 1_800_000_000,
            (fixture.ref('video_metadata', 'fresh-busy'),), unknown=True), 0)
        self.assertFalse(pending.unresolved)

    def test_targeted_retries_do_not_count_as_complete_passes(self):
        controller, repository = fixture.runtime()
        # A confirmed expired source with more than the 12-hour safety margin.
        page = fixture.page('video_metadata', repository.wall, (repository.a,))
        from dataclasses import replace
        page = replace(page, oldest_remaining_expired_source_at=fixture.iso(repository.wall - 29.1 * fixture.DAY))
        controller._priority.observe(page, repository.mono)
        controller._held_passes = 1
        for _ in range(8):
            repository.wall += 5
            repository.mono += 5
            controller._retry_priority()
        self.assertEqual(controller._held_passes, 1)
        self.assertFalse(controller.status()['collection_pause_requested'])

    def test_transition_records_reason_targets_counts_once_and_recovers(self):
        controller, repository = fixture.runtime()
        controller.status()
        with patch('telemetry.emit_event') as emit:
            for _ in range(3):
                fixture.tick(controller, repository)
            state = controller.status()
            self.assertFalse(state['collection']['allowed'])
            transitions = [call for call in emit.call_args_list if call.args[3] == 'hot_runtime_switch']
            self.assertEqual(len(transitions), 1)
            self.assertEqual(transitions[0].kwargs['phase'], 'collection_priority')
            self.assertEqual(json.loads(transitions[0].kwargs['detail'])['cause'], 'storage_deadline_near')
            causes = [call for call in emit.call_args_list if call.args[3] == 'hot_runtime_priority']
            self.assertTrue(causes)
            self.assertTrue(all(call.kwargs['count'] == 1 for call in causes))
            self.assertNotIn('video-A', str(emit.call_args_list))
            previous = emit.call_count
            controller.status()
            self.assertEqual(emit.call_count, previous)
            repository.held.clear()
            fixture.tick(controller, repository, 5)
            self.assertTrue(controller.status()['collection']['allowed'])
            self.assertEqual(emit.call_args.kwargs['phase'], 'ready')

    def test_advancing_inventory_pages_do_not_count_as_full_cleanup_passes(self):
        controller, repository = fixture.runtime()
        original = repository.cleanup_page
        from dataclasses import replace
        def ordinary_expiry(*args, **kwargs):
            page = original(*args, **kwargs)
            return replace(page, oldest_remaining_expired_source_at=
                fixture.iso(repository.wall - 29.1 * fixture.DAY)) if page.deferred else page
        repository.cleanup_page = ordinary_expiry
        for _ in range(20):
            fixture.tick(controller, repository, 1)
        self.assertGreater(len(repository.calls), 3)
        self.assertEqual(controller._held_passes, 0)
        self.assertFalse(controller.status()['collection_pause_requested'])


if __name__ == '__main__':
    unittest.main()
