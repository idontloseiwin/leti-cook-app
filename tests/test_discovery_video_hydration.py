"""Exact-attempt and original-clock adapter checks; no provider or database."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from collection.common_video_dispatch import CommonBatchUnit
from collection import common_video_dispatch
from collection.discovery_video_hydration import REQUIRED_METADATA_PARTS, reusable_metadata_response
from collection.dispatch import Skipped
from db import VideoCollectionClaim
from test_common_video_dispatch import Repository
from test_initial_video_parallel import service, video, CHANNEL, STAMP

ATTEMPT = 'ab700000-0000-4000-8000-000000000001'
SOURCE = 'ab700000-0000-4000-8000-000000000002'


class DiscoveryHydrationTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository()
        self.repo.complete_typed_metadata = lambda rows: self.repo.record(rows[0]['video_type'], rows)
        self.repo.complete_stream_lifecycle = lambda rows: self.repo.record('lifecycle', rows)
        self.collector = service(self.repo, Mock(side_effect=AssertionError('provider forbidden')))
        self.claim = self.repo.claim()['rows'][0]
        self.observed = datetime.now(timezone.utc)-timedelta(minutes=2)
        self.received = self.observed+timedelta(seconds=1)
        self.args = dict(requested_ids=[self.claim.video_id], items=[video(self.claim.video_id)],
            observed_at=self.observed, received_at=self.received, source_request_id=SOURCE,
            requested_parts=REQUIRED_METADATA_PARTS)

    def response(self, claims=None, **changes):
        return reusable_metadata_response(self.collector, claims or [self.claim], **dict(self.args, **changes))

    def run_response(self, claims=None, **changes):
        selected = claims or [self.claim]
        return self.collector.process_common_claims(selected, attempt_id=ATTEMPT,
            initial_response=self.response(selected, **changes))

    def test_prefetch_is_copied_and_original_source_clocks_are_preserved(self):
        response = self.response()
        self.args['items'][0]['snippet']['title'] = 'changed after capture'
        result = self.collector.process_common_claims([self.claim], attempt_id=ATTEMPT, initial_response=response)
        self.assertEqual(result['updated'], 1)
        kind, rows = self.repo.writes[0]
        self.assertEqual(kind, 'metadata')
        self.assertEqual(rows[0]['collection_attempt_id'], ATTEMPT)
        self.assertEqual(rows[0]['recorded_at'], self.observed.isoformat())
        self.assertEqual(rows[0]['metadata_observed_at'], self.observed.isoformat())
        self.assertNotEqual(rows[0]['title'], 'changed after capture')
        self.assertEqual(response.source_request_id, SOURCE)
        self.assertEqual(response.metadata_received_at, self.received)
        self.collector.forbidden.assert_not_called()

    def test_long_and_short_can_share_exact_owner_attempt(self):
        short = replace(self.claim, video_id='secondvideo', video_type='short', collection_mode='hydrate')
        short_item = video(short.video_id)
        short_item['contentDetails']['duration'] = 'PT1M'
        self.run_response([self.claim, short], requested_ids=[self.claim.video_id, short.video_id],
                          items=[video(self.claim.video_id), short_item])
        self.assertEqual([kind for kind, _ in self.repo.writes], ['metadata', 'short'])
        self.assertEqual({row['collection_attempt_id'] for _, rows in self.repo.writes for row in rows}, {ATTEMPT})

    def test_confirmed_omission_uses_preallocated_attempt_and_original_clock(self):
        self.run_response([replace(self.claim, prior_omission=True)], items=[])
        self.assertEqual([kind for kind, _ in self.repo.writes], ['missing'])
        row = self.repo.writes[0][1][0]
        self.assertEqual(row['collection_attempt_id'], ATTEMPT)
        self.assertEqual(row['recorded_at'], self.observed.isoformat())

    def test_first_omission_is_exact_failure_not_success(self):
        result = self.run_response(items=[])
        self.assertEqual(result['updated'], 0)
        self.assertEqual([kind for kind, _ in self.repo.writes], ['failure'])
        self.assertEqual(self.repo.writes[0][1][0]['collection_attempt_id'], ATTEMPT)
        self.assertEqual(self.repo.writes[0][1][0]['error_type'], 'youtube_video_missing_unconfirmed')

    def test_identity_refresh_is_same_attempt_not_hydration(self):
        self.run_response([replace(self.claim, video_type='unknown', collection_mode='identity_refresh')])
        self.assertEqual([kind for kind, _ in self.repo.writes], ['identity'])
        self.assertEqual(self.repo.writes[0][1][0]['collection_attempt_id'], ATTEMPT)

    def test_lifecycle_uses_same_attempt(self):
        item = video(self.claim.video_id)
        item['snippet']['liveBroadcastContent'] = 'upcoming'
        item['contentDetails']['duration'] = 'PT0S'
        item['liveStreamingDetails'] = {'scheduledStartTime': (self.received+timedelta(days=1)).isoformat()}
        self.run_response([replace(self.claim, video_type='stream', collection_mode='stream_lifecycle')], items=[item])
        self.assertEqual([kind for kind, _ in self.repo.writes], ['lifecycle'])
        self.assertEqual(self.repo.writes[0][1][0]['collection_attempt_id'], ATTEMPT)

    def test_incomplete_parts_and_missing_required_counter_are_not_reused(self):
        self.assertIsNone(self.response(requested_parts=REQUIRED_METADATA_PARTS-{'statistics'}))
        item = copy.deepcopy(self.args['items'][0]); item['statistics'].pop('viewCount')
        self.assertIsNone(self.response(items=[item]))
        item = copy.deepcopy(self.args['items'][0]); item.pop('status')
        self.assertIsNone(self.response(items=[item]))
        self.assertEqual(self.repo.writes, [])

    def test_unknown_optional_sections_are_allowed_when_requested(self):
        item = copy.deepcopy(self.args['items'][0]); item.pop('topicDetails')
        self.assertIsNotNone(self.response(items=[item]))

    def test_foreign_duplicate_and_wrong_channel_profiles_reject(self):
        for items in ([video('wrongvideo1')], self.args['items']*2,
                      [dict(video(self.claim.video_id), snippet={'channelId': 'foreign'})]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.response(items=items)
        self.assertEqual(self.repo.writes, [])

    def test_claim_subset_of_one_physical_response_excludes_unclaimed_items(self):
        response = self.response(requested_ids=[self.claim.video_id, 'secondvideo'],
                                 items=[video(self.claim.video_id), video('secondvideo')])
        self.assertEqual([row['id'] for row in response.items], [self.claim.video_id])
        with self.assertRaises(ValueError):
            self.response([replace(self.claim, video_id='unrequested')])

    def test_invalid_source_clocks_and_counter_only_claim_reject_reuse(self):
        for changes in (dict(observed_at=self.received+timedelta(seconds=1)),
                        dict(observed_at=self.observed.replace(tzinfo=None)),
                        dict(received_at=datetime.now(timezone.utc)+timedelta(days=1)),
                        dict(source_request_id='bad')):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.response(**changes)
        self.assertIsNone(self.response([replace(self.claim, collection_mode='counters')]))

    def test_binding_failure_drains_unsent_exact_tokens_and_does_not_fetch(self):
        unit = CommonBatchUnit(self.collector, key='discovery', lane='auto', channel=CHANNEL,
            video_type='long', claim_batch=self.repo.claim, require_ready=lambda: None,
            on_claimed=Mock(side_effect=RuntimeError('binding failed')), on_settled=Mock(), attempt_id=ATTEMPT)
        with patch.object(common_video_dispatch, '_drain_unsent') as drain:
            with self.assertRaisesRegex(RuntimeError, 'binding failed'): unit.prepare()
        drain.assert_called_once_with(self.repo, unit.claims)
        self.collector.forbidden.assert_not_called()
        unit.on_settled.assert_not_called()

    def test_binding_precedes_prefetch_write(self):
        order=[]
        old=self.repo.record
        self.repo.record=lambda kind, rows: (order.append('write'), old(kind, rows))[1]
        unit=CommonBatchUnit(self.collector, key='discovery', lane='auto', channel=CHANNEL,
            video_type='long', claim_batch=self.repo.claim, require_ready=lambda: None,
            on_claimed=lambda info: order.append('bind'), on_settled=lambda summary: order.append('settled'),
            attempt_id=ATTEMPT, initial_response=self.response())
        self.assertIsInstance(unit.prepare(), Skipped)
        self.assertEqual(order, ['bind','write','settled'])

    def test_general_admission_denial_releases_without_provider_failure(self):
        from discovery_admission import DiscoveryAdmissionDenied
        due=self.received+timedelta(days=1)
        self.repo.release_discovery_admission=Mock()
        self.collector.fetch_metadata=Mock(side_effect=DiscoveryAdmissionDenied(
            dict(reason='window_budget',next_try_at=due)))
        result=self.collector.process_common_claims([self.claim],attempt_id=ATTEMPT)
        self.repo.release_discovery_admission.assert_called_once_with([self.claim])
        self.assertEqual(self.repo.writes,[])
        self.assertEqual((result['errors'],result['metadata_requests']),(0,0))
        self.assertEqual(result['admission_reason'],'window_budget')
        self.assertEqual(result['admission_retry_at'],due.isoformat())

    def test_stats_admission_denial_keeps_general_evidence_without_hydration_ack(self):
        from discovery_admission import DiscoveryAdmissionDenied
        item=copy.deepcopy(self.args['items'][0]);item['statistics'].pop('viewCount')
        self.collector.fetch_metadata=lambda ids,reason:[item]
        self.collector.fetch_counters=Mock(side_effect=DiscoveryAdmissionDenied(dict(reason='daily_budget')))
        self.repo.release_discovery_admission=Mock()
        result=self.collector.process_common_claims([self.claim],attempt_id=ATTEMPT)
        self.assertEqual([kind for kind,_ in self.repo.writes],['identity'])
        self.assertTrue(self.repo.writes[0][1][0]['counterless_profile'])
        self.assertEqual(self.repo.writes[0][1][0]['collection_attempt_id'],ATTEMPT)
        self.assertEqual((result['errors'],result['batch_requests'],result['updated']),(0,0,0))
        self.assertTrue(result['admission_denied'])
        self.repo.release_discovery_admission.assert_called_once_with([self.claim])

    def test_unsafe_admission_releases_then_propagates_without_failure_write(self):
        from discovery_admission import DiscoveryAdmissionUnsafe
        self.repo.release_discovery_admission=Mock()
        self.collector.fetch_metadata=Mock(side_effect=DiscoveryAdmissionUnsafe('uncertain'))
        with self.assertRaises(DiscoveryAdmissionUnsafe):
            self.collector.process_common_claims([self.claim],attempt_id=ATTEMPT)
        self.repo.release_discovery_admission.assert_called_once_with([self.claim])
        self.assertEqual(self.repo.writes,[])

    def test_unsafe_after_real_quota_response_keeps_request_count(self):
        from discovery_admission import DiscoveryAdmissionUnsafe
        from collection.provider_steps import run_provider_steps
        for attempted in (False,True):
            with self.subTest(http_attempted=attempted):
                self.repo.release_discovery_admission=Mock()
                self.collector.fetch_metadata=Mock(side_effect=DiscoveryAdmissionUnsafe(
                    'quota ban persistence failed',http_attempted=attempted))
                summary={}
                with self.assertRaises(DiscoveryAdmissionUnsafe):
                    run_provider_steps(self.collector.common_claim_steps([self.claim],
                        attempt_id=ATTEMPT,summary=summary))
                self.assertEqual(summary['metadata_requests'],int(attempted))
                self.assertEqual(summary['errors'],0)
                self.assertEqual(self.repo.writes,[])

    def test_general_control_unknown_releases_without_provider_failure(self):
        from quota_buckets import QuotaStateUnavailable
        self.repo.release_discovery_admission=Mock()
        self.collector.fetch_metadata=Mock(side_effect=QuotaStateUnavailable())
        result=self.collector.process_common_claims([self.claim],attempt_id=ATTEMPT)
        self.repo.release_discovery_admission.assert_called_once_with([self.claim])
        self.assertEqual(self.repo.writes,[])
        self.assertEqual((result['metadata_requests'],result['batch_requests']),(0,0))
        self.assertTrue(result['quota_control_unknown'])
        self.assertEqual(result['stopped_reason'],'quota_state_unavailable')
        self.assertEqual(result['admission_reason'],'quota_state_unavailable')
        self.assertEqual(result['errors'],1)
        self.assertFalse(result['settlement_unknown'])
        self.assertFalse(result['quota_exhausted'])

    def test_stats_control_unknown_keeps_only_received_general_evidence(self):
        from quota_buckets import QuotaStateUnavailable
        item=copy.deepcopy(self.args['items'][0]);item['statistics'].pop('viewCount')
        self.collector.fetch_metadata=lambda ids,reason:[item]
        self.collector.fetch_counters=Mock(side_effect=QuotaStateUnavailable())
        self.repo.release_discovery_admission=Mock()
        result=self.collector.process_common_claims([self.claim],attempt_id=ATTEMPT)
        self.assertEqual([kind for kind,_ in self.repo.writes],['identity'])
        self.assertTrue(self.repo.writes[0][1][0]['counterless_profile'])
        self.assertEqual((result['metadata_requests'],result['batch_requests'],result['updated']),(1,0,0))
        self.assertEqual(result['degraded_batch_requests'],0)
        self.assertTrue(result['quota_control_unknown'])
        self.assertFalse(result['batch_unavailable'])
        self.assertFalse(result['quota_exhausted'])
        self.repo.release_discovery_admission.assert_called_once_with([self.claim])

    def test_counter_only_control_unknown_has_no_source_or_failure_write(self):
        from quota_buckets import QuotaStateUnavailable
        claim=replace(self.claim,collection_mode='counters')
        self.repo.release_discovery_admission=Mock()
        self.collector.fetch_counters=Mock(side_effect=QuotaStateUnavailable())
        result=self.collector.process_common_claims([claim],attempt_id=ATTEMPT)
        self.assertEqual(self.repo.writes,[])
        self.assertEqual((result['batch_requests'],result['degraded_batch_requests']),(0,0))
        self.assertTrue(result['quota_control_unknown'])
        self.repo.release_discovery_admission.assert_called_once_with([claim])

    def test_control_unknown_release_failure_keeps_settlement_unknown(self):
        from quota_buckets import QuotaStateUnavailable
        self.repo.release_discovery_admission=Mock(side_effect=RuntimeError('lost release acknowledgement'))
        self.collector.fetch_metadata=Mock(side_effect=QuotaStateUnavailable())
        result=self.collector.process_common_claims([self.claim],attempt_id=ATTEMPT)
        self.assertTrue(result['quota_control_unknown'])
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(self.repo.writes,[])
        self.assertEqual(result['metadata_requests'],0)

    def test_common_batch_wrapper_preserves_unknown_control_and_stops_next_batch(self):
        from quota_buckets import QuotaStateUnavailable
        from collection_outcome import collection_stop_reason
        self.repo.release_discovery_admission=Mock()
        self.collector.fetch_metadata=Mock(side_effect=QuotaStateUnavailable())
        claims=[replace(self.claim,video_id=f'v{index:010d}') for index in range(51)]
        result=self.collector.process_claims(claims)
        self.assertTrue(result['quota_control_unknown'])
        self.assertEqual(collection_stop_reason(result,'pass'),'quota_control_unknown')
        self.assertEqual((result['claimed'],result['errors'],result['metadata_requests']),(50,1,0))
        self.collector.fetch_metadata.assert_called_once()
        self.repo.release_discovery_admission.assert_called_once_with(claims[:50])
        self.assertEqual(self.repo.writes,[])

    def test_typed_entrypoint_preserves_unknown_control_without_provider_failure(self):
        from quota_buckets import QuotaStateUnavailable
        from collection_outcome import collection_stop_reason
        from video_collection_service import run_claimed_typed_video_collection
        claim=replace(self.claim,video_type='short',collection_mode='hydrate')
        self.repo.reconcile_typed=Mock(return_value={'enabled':True})
        self.repo.claim_due_typed_batch=Mock(return_value=dict(rows=[claim],inspected_count=1,scan_exhausted=True))
        self.repo.release_discovery_admission=Mock()
        self.collector.fetch_full=Mock(side_effect=QuotaStateUnavailable())
        result=run_claimed_typed_video_collection(self.repo,self.collector,
            video_type='short',limit=2,worker_id='control-regression')
        self.assertTrue(result['quota_control_unknown'])
        self.assertEqual(collection_stop_reason(result,'pass'),'quota_control_unknown')
        self.assertEqual(result['errors'],1)
        self.repo.release_discovery_admission.assert_called_once_with([claim])
        self.repo.claim_due_typed_batch.assert_called_once()
        self.assertEqual(self.repo.writes,[])


if __name__ == '__main__': unittest.main()
