"""Regular provider steps with real service/threads and guarded DB doubles."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from collection.dispatch import Ack, Completion, Coordinator, DispatchBlocked, Prepared
from collection.common_video_dispatch import CommonBatchUnit
from collection import common_video_dispatch as common
from db import VideoCollectionClaim
from hot_runtime import HotRuntimeUnavailable
import collection_shutdown
from test_initial_video_parallel import service, video, CHANNEL, STAMP


class Repository:
    def __init__(self):
        self.owner, self.database = get_ident(), object()
        self.writes, self.claimed, self.releases = [], 0, []
        self.fail_commit = False
        self.mode = 'metadata'

    def claim(self):
        assert get_ident() == self.owner
        self.claimed += 1
        return {'rows': [VideoCollectionClaim(video_id='abcdefghijk', channel_id=CHANNEL,
            collection_mode=self.mode, lease_token='original-token',
            lease_expires_at=STAMP, next_due_at=STAMP)], 'inspected_count': 1,
            'paused_count': 0, 'scan_exhausted': True}

    def record(self, operation, rows):
        assert get_ident() == self.owner
        if self.fail_commit:
            raise OSError('synthetic unknown commit')
        self.writes.append((operation, list(rows)))
        return len(rows)

    def complete_metadata(self, rows):
        return self.record('metadata', rows)

    def complete_counters(self, rows):
        return self.record('counters', rows)

    def complete_missing(self, rows):
        return self.record('missing', rows)

    def complete_identity_refresh(self, rows):
        return self.record('identity', rows)

    def fail_batch(self, rows):
        return self.record('failure', rows)

    def load_counter_presence(self, identities):
        assert get_ident() == self.owner
        return dict.fromkeys(identities, True)


class CommonDispatchTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(collection_shutdown, '_requested', Event())
        gate.start()
        self.addCleanup(gate.stop)
        self.repo, self.network = Repository(), []
        self.collector = service(self.repo, Mock())
        def metadata(ids, reason):
            self.assertNotEqual(get_ident(), self.repo.owner)
            self.network.append(('general', ids))
            items = [video(value) for value in ids]
            for item in items:
                item["statistics"].pop("viewCount")  # Exercise the required-counter continuation.
            return items
        def counters(ids, reason):
            self.assertNotEqual(get_ident(), self.repo.owner)
            self.network.append(('stats', ids))
            return {'items': [{'id': value, 'views': 123, 'like_count': 7,
                               'comment_count': 2} for value in ids],
                    'summary': {'failed_ids': []}}
        self.collector.fetch_metadata = metadata
        self.collector.fetch_counters = counters
        self.progress, self.claimed = Mock(), Mock()
        self.unit = CommonBatchUnit(self.collector, key='regular', lane='auto',
            channel=CHANNEL, video_type='long', claim_batch=self.repo.claim,
            require_ready=Mock(), on_claimed=self.claimed, on_settled=self.progress)

    def fetch(self, prepared):
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(prepared.fetch, Event()).result(timeout=5)

    def test_stats_miss_requests_general_instead_of_declaring_video_missing(self):
        for response in ({'items': [], 'summary': {'failed_ids': []}},
                         {'items': [{'id': 'abcdefghijk', 'views': None}], 'summary': {}},
                         {'items': [], 'summary': {'failed_ids': ['abcdefghijk']}}):
            with self.subTest(response=response):
                self.repo.mode = 'counters'
                self.repo.writes.clear()
                self.collector.fetch_counters = lambda ids, reason: response
                unit = CommonBatchUnit(self.collector, key='regular', lane='auto',
                    channel=CHANNEL, video_type='long', claim_batch=self.repo.claim,
                    require_ready=Mock(), on_claimed=self.claimed, on_settled=self.progress)
                prepared = unit.prepare()
                self.assertIsInstance(unit.settle(Completion('response',
                    value=self.fetch(prepared), fetch_started=True)), Ack)
                self.assertEqual([kind for kind, _ in self.repo.writes], ['failure'])
                row = self.repo.writes[0][1][0]
                self.assertEqual((row['error_type'], row['retry_after_seconds'], row['lease_token']),
                    ('youtube_stats_counters_unavailable', 30, 'original-token'))

    def test_general_fallback_miss_preserves_positive_availability_without_counters(self):
        self.collector.fetch_counters = lambda ids, reason: {'items': [], 'summary': {}}
        prepared = self.unit.prepare()
        continuation = self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True))
        self.assertIsInstance(continuation, Prepared)
        self.assertIsInstance(self.unit.settle(Completion('response',
            value=self.fetch(continuation), fetch_started=True)), Ack)
        self.assertEqual(self.repo.writes[0][0],'identity')
        self.assertTrue(self.repo.writes[0][1][0]['counterless_profile'])
        self.assertFalse(self.repo.writes[0][1][0]['missing'])
        self.assertNotIn('views',self.repo.writes[0][1][0])

    def test_stats_transport_error_does_not_promote_availability(self):
        self.repo.mode = 'counters'
        self.collector.fetch_counters = Mock(side_effect=TimeoutError('synthetic'))
        prepared = self.unit.prepare()
        self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True))
        self.assertEqual(self.repo.writes[0][1][0]['error_type'], 'youtube_batch_request_error')

    def test_general_then_stats_same_claim_and_owner_thread_writes(self):
        self.assertEqual(self.repo.claimed, 0)
        prepared = self.unit.prepare()
        self.assertEqual(self.repo.claimed, 1)
        first = self.fetch(prepared)
        self.assertEqual(self.repo.writes, [])
        continuation = self.unit.settle(Completion('response', value=first, fetch_started=True))
        self.assertIsInstance(continuation, Prepared)
        self.assertEqual(continuation.deadline, prepared.deadline)
        self.progress.assert_not_called()
        self.assertEqual(self.repo.writes, [])
        final = self.fetch(continuation)
        self.assertIsInstance(self.unit.settle(Completion('response', value=final, fetch_started=True)), Ack)
        self.assertEqual([value[0] for value in self.network], ['general', 'stats'])
        self.assertEqual([value[0] for value in self.repo.writes], ['metadata'])
        self.assertEqual(self.unit.summary['updated'], 1)
        self.assertEqual(self.repo.claimed, 1)
        self.claimed.assert_called_once()
        self.progress.assert_called_once()

    def test_explicit_shared_stop_after_general_drains_without_stats_or_false_hot(self):
        stopped = Event()
        self.unit.should_stop = stopped.is_set
        prepared = self.unit.prepare()
        response = self.fetch(prepared)
        stopped.set()
        with patch.object(common, '_drain_unsent') as release:
            self.assertIsInstance(self.unit.settle(Completion('response', value=response, fetch_started=True)), Ack)
        release.assert_called_once_with(self.repo, self.unit.claims)
        self.assertEqual([value[0] for value in self.network], ['general'])
        self.assertFalse(self.unit.summary.get('hot_runtime_interrupted'))
        self.progress.assert_called_once()

    def test_expired_counter_continuation_releases_original_tokens_without_stats(self):
        prepared = self.unit.prepare()
        continuation = self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True))
        self.assertIsInstance(continuation, Prepared)
        with patch.object(common, '_drain_unsent') as release:
            self.assertIsInstance(self.unit.settle(Completion('expired')), Ack)
        release.assert_called_once_with(self.repo, self.unit.claims)
        self.assertEqual(self.network, [('general', ['abcdefghijk'])])
        self.assertEqual(self.repo.writes, [])
        self.progress.assert_called_once()
        self.assertEqual(self.unit.summary['metadata_requests'], 1)
        self.assertEqual(self.unit.summary['batch_requests'], 0)
        self.assertTrue(self.unit.summary['incomplete'])

    def test_cancel_reports_already_committed_missing_and_zero_unsent_stats(self):
        def claim():
            batch = self.repo.claim()
            batch['rows'].append(replace(batch['rows'][0], video_id='secondvideo', lease_token='token-2'))
            return batch
        self.unit.claim_batch = claim
        original = self.collector.fetch_metadata
        self.collector.fetch_metadata = lambda ids, reason: original(ids, reason)[1:]
        prepared = self.unit.prepare()
        continuation = self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True))
        self.assertIsInstance(continuation, Prepared)
        self.assertEqual([value[0] for value in self.repo.writes], ['missing'])
        with patch.object(common, '_drain_unsent') as release:
            self.assertIsInstance(self.unit.settle(Completion('cancelled')), Ack)
        release.assert_called_once_with(self.repo, self.unit.claims)
        self.assertEqual(self.unit.summary['unavailable_marked'], 1)
        self.assertEqual(self.unit.summary['batch_requests'], 0)
        self.assertEqual(self.unit.summary['unresolved'], 1)
        self.progress.assert_called_once()

    def test_known_hot_denial_after_claim_or_general_drains_exact_tokens(self):
        for after_general in (False, True):
            with self.subTest(after_general=after_general):
                self.setUp()
                self.unit.require_ready.side_effect = ([None, None, HotRuntimeUnavailable()]
                    if after_general else [None, HotRuntimeUnavailable()])
                with patch.object(common, '_drain_unsent') as release:
                    prepared = self.unit.prepare()
                    if after_general:
                        self.assertIsInstance(self.unit.settle(Completion('response',
                            value=self.fetch(prepared), fetch_started=True)), Ack)
                    release.assert_called_once_with(self.repo, self.unit.claims)
                self.assertEqual(self.unit.phase, 'released')
                self.assertTrue(self.unit.summary['hot_runtime_interrupted'])
                self.assertEqual(self.unit.summary['batch_requests'], 0)
                self.progress.assert_called_once()

    def test_unknown_abort_keeps_credit_and_does_not_report_progress(self):
        self.unit.require_ready.side_effect = [None, HotRuntimeUnavailable()]
        with patch.object(common, '_drain_unsent', side_effect=OSError('abort ack lost')) as release:
            with ThreadPoolExecutor(max_workers=1) as executor:
                coordinator = Coordinator(executor, 1)
                coordinator.offer(self.unit.work)
                with self.assertRaises(DispatchBlocked):
                    coordinator.step()
                with self.assertRaises(DispatchBlocked):
                    coordinator.step()
                self.assertEqual(coordinator.snapshot().held, 1)
            release.assert_called_once()
        self.progress.assert_not_called()

    def test_provider_error_is_settled_once_through_existing_failure_owner(self):
        self.collector.fetch_metadata = Mock(side_effect=RuntimeError('synthetic refusal'))
        prepared = self.unit.prepare()
        self.assertIsInstance(self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True)), Ack)
        self.assertEqual([value[0] for value in self.repo.writes], ['failure'])
        self.assertFalse(self.unit.summary['settlement_unknown'])
        self.assertEqual(self.network, [])

    def test_unknown_commit_blocks_actual_coordinator_credit_without_retry(self):
        self.repo.fail_commit = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            coordinator = Coordinator(executor, 2)
            coordinator.offer(self.unit.work)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    coordinator.step()
                except DispatchBlocked:
                    break
                coordinator.wait(.01)
            else:
                self.fail('unknown commit was not blocked')
            self.assertEqual(coordinator.snapshot().held, 1)
            self.assertEqual(self.unit.phase, 'settlement_unknown')
            for _ in range(2):
                with self.assertRaises(DispatchBlocked):
                    coordinator.step()
            self.assertEqual(self.repo.claimed, 1)
            self.assertEqual(len(self.network), 2)
            self.progress.assert_not_called()

    def test_unknown_claim_never_replays(self):
        self.unit.claim_batch = Mock(side_effect=OSError('claim ack lost'))
        with self.assertRaises(OSError):
            self.unit.prepare()
        with self.assertRaisesRegex(RuntimeError, 'prepare_repeated'):
            self.unit.prepare()
        self.unit.claim_batch.assert_called_once()
        self.assertEqual(self.network, [])

    def test_parent_progress_failure_does_not_repeat_confirmed_video_write(self):
        self.progress.side_effect = OSError('parent progress acknowledgement lost')
        prepared = self.unit.prepare()
        continuation = self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True))
        response = Completion('response', value=self.fetch(continuation), fetch_started=True)
        with self.assertRaises(OSError):
            self.unit.settle(response)
        self.assertEqual(self.unit.phase, 'progress')
        self.assertEqual(len(self.repo.writes), 1)
        with self.assertRaisesRegex(RuntimeError, 'settle_repeated'):
            self.unit.settle(response)
        self.assertEqual(len(self.repo.writes), 1)
        self.progress.assert_called_once()

    def test_counter_only_uses_stats_and_presence_read_stays_on_owner(self):
        self.repo.mode = 'counters'
        def build(item, recorded_at):
            self.assertEqual(get_ident(), self.repo.owner)
            return {'snapshot': {**item, 'recorded_at': recorded_at}}
        self.collector.build_counter_payload = build
        prepared = self.unit.prepare()
        result = self.unit.settle(Completion('response', value=self.fetch(prepared), fetch_started=True))
        self.assertIsInstance(result, Ack)
        self.assertEqual([value[0] for value in self.network], ['stats'])
        self.assertEqual([value[0] for value in self.repo.writes], ['counters'])
        self.assertEqual(self.unit.summary['updated'], 1)

    def test_empty_claim_reports_pass_budget_without_provider(self):
        self.unit.claim_batch = Mock(return_value={'rows': [], 'inspected_count': 50})
        self.unit.prepare()
        self.claimed.assert_called_once_with({'rows': [], 'inspected_count': 50})
        self.progress.assert_not_called()
        self.assertEqual(self.network, [])


if __name__ == '__main__':
    unittest.main()
