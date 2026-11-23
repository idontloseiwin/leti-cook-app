from pathlib import Path
from dataclasses import replace
import sys
import unittest
from unittest.mock import Mock, patch
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from discovery_service import DiscoveryService
from discovery_policy import DiscoverySettings
from youtube_errors import YouTubePlaylistNotFoundError

CHANNEL='UC'+'x'*22
VIDEO='fxvideo0001'


class DiscoveryServiceTests(unittest.TestCase):
    def make(self, duration='PT1M', rows=1):
        self.repo=Mock();self.provider=Mock();self.budget=Mock();self.membership=Mock()
        self.rows=[dict(video_id=VIDEO,channel_id=CHANNEL,work_id=str(i),kind='entry',work_phase='metadata_wait',discovery_attempts=0) for i in range(rows)]
        self.repo.claim_event_batch.return_value=self.rows
        self.repo.resolve_collected.return_value=[]
        self.repo.target_state.return_value={row['work_id']:dict(queue_exists=False,video_type=None) for row in self.rows}
        self.repo.terminal_observation.side_effect=lambda rows,*a,**k:[row['work_id'] for row in rows]
        self.repo.record_membership.side_effect=lambda rows,**k:dict(accepted_work_ids=[r['work_id'] for r in rows],rejected_work_ids=[])
        self.budget.reserve.return_value=dict(allowed=True);self.budget.begin_dispatch.return_value=True
        self.provider.fetch_metadata.return_value=[dict(id=VIDEO,snippet=dict(channelId=CHANNEL),status=dict(privacyStatus='public'),contentDetails=dict(duration=duration))]
        self.membership.return_value={'items':[]}
        service=DiscoveryService(self.repo,self.provider,self.budget,settings=DiscoverySettings(),fetch_membership=self.membership,
            worker_id='fixture',run_id=str(uuid4()))
        self.hydrate=Mock(side_effect=lambda *a,**k:iter(()));service._hydrate=self.hydrate
        return service

    @staticmethod
    def absent(playlist):
        raise YouTubePlaylistNotFoundError(confirmed_playlist_id=playlist)

    def without_views(self):
        service = self.make(duration='PT10M')
        self.rows[0]['origin'] = 'websub'
        item = self.provider.fetch_metadata.return_value[0]
        item['snippet']['liveBroadcastContent'] = 'none'
        item['status']['uploadStatus'] = 'processed'
        return service, item

    def test_public_processed_without_views_checks_every_enabled_format_then_settles(self):
        service, _ = self.without_views()
        result = service.run_once()
        self.assertEqual(result['settled'], 1)
        self.assertEqual([c.args for c in self.membership.call_args_list],
            [(prefix + CHANNEL[2:], identity) for prefix in ('UUSH','UULF') for identity in (None, VIDEO)])
        self.assertEqual(self.repo.terminal_observation.call_args.args[1], 'no_public_stats_or_membership')
        self.repo.defer.assert_not_called()

    def test_partial_proofs_never_close_a_notification_without_views(self):
        for change in ('zero','null','malformed','lifecycle','stream','unprocessed','other_origin','unscoped_404','failure'):
            with self.subTest(change=change):
                service, item = self.without_views()
                if change in ('zero','null','malformed'):
                    item['statistics'] = {'viewCount': {'zero':'0','null':None,'malformed':'?' }[change]}
                elif change=='lifecycle': item['snippet'].pop('liveBroadcastContent')
                elif change=='stream': item['liveStreamingDetails']={}
                elif change=='unprocessed': item['status']['uploadStatus']='uploaded'
                elif change=='other_origin': self.rows[0]['origin']='repair'
                elif change=='unscoped_404': self.membership.side_effect=YouTubePlaylistNotFoundError()
                elif change=='failure': self.membership.side_effect=[self.page(), self.page(), RuntimeError('unavailable')]
                service.run_once()
                self.repo.terminal_observation.assert_not_called()
                self.repo.defer.assert_called()

    def test_matching_short_circuits_the_negative_outcome(self):
        service, _ = self.without_views()
        self.membership.return_value=self.page(VIDEO)
        service.run_once()
        self.repo.terminal_observation.assert_not_called()
        self.hydrate.assert_called_once()

    def test_missing_metadata_does_not_become_identity_or_type(self):
        service=self.make();self.provider.fetch_metadata.return_value=[]
        service.run_once()
        self.membership.assert_not_called();self.repo.record_membership.assert_not_called();self.hydrate.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[1],'metadata_not_returned')

    def test_budget_denial_does_not_lease_ordinary_fill(self):
        service=self.make()
        service.claim_fill=Mock(return_value=[])
        self.budget.reserve.return_value={'allowed':False,'reason':'window_budget'}
        service.run_once()
        service.claim_fill.assert_not_called()
        self.provider.fetch_metadata.assert_not_called()
        self.budget.reserve.return_value={'allowed':True}
        service.run_once()
        self.assertEqual(service.claim_fill.call_args.kwargs['excluded'],(VIDEO,))
        self.assertEqual(service.claim_fill.call_args.kwargs['limit'],49)

    def test_completed_publication_reaches_membership_without_changing_format(self):
        service = self.make()
        item = self.provider.fetch_metadata.return_value[0]
        item['snippet']['liveBroadcastContent'] = 'completed'
        item['liveStreamingDetails'] = {'actualStartTime':'2020-01-01T01:00:00Z'}
        self.membership.return_value = self.page(VIDEO)
        service.run_once()
        self.membership.assert_called_once()
        self.hydrate.assert_called_once()

    def test_after_wait_public_none_still_requires_typed_membership(self):
        service = self.make()
        self.rows[0]['wait_reason'] = 'lifecycle_wait'
        item = self.provider.fetch_metadata.return_value[0]
        item['snippet']['liveBroadcastContent'] = 'none'
        item['status']['uploadStatus'] = 'processed'
        self.membership.return_value = self.page(VIDEO)
        service.run_once()
        self.membership.assert_called_once()
        self.hydrate.assert_called_once()

    def test_known_held_and_failed_notifications_use_configured_review_interval(self):
        from db.discovery import DiscoveryRepository
        claim = dict(origin='websub', work_id='1', claim_token=str(uuid4()), claim_channel_epoch=1)
        row = dict(work_phase='metadata_wait', kind='entry', membership_receipt_id=None, video_id=VIDEO,
                   channel_id=CHANNEL, received_at=None, metadata_source_checked_at=None)
        custom = replace(DiscoverySettings(), review_retry_seconds=90000)
        cases = [('manual_hold', None, {}, 86400), (None, 'youtube_error', {'settings': custom}, 90000)]
        for paused, error, policy, seconds in cases:
            with self.subTest(paused=paused, error=error):
                tx = Mock()
                tx.fetch_one.side_effect = [dict(paused_reason=paused, last_error_type=error),
                                            dict(fresh=False, unavailable=False)]
                database = Mock()
                database.transaction.return_value = nullcontext(tx)
                with patch('db.discovery.locked_claim', return_value=row):
                    result = DiscoveryRepository(database).resolve_collected(
                        [claim], enabled_types=('short','long'), queue_if_stale=True, **policy)
                self.assertEqual(result, [])
                tx.execute.assert_called_once()
                statement, values = tx.execute.call_args.args
                self.assertIn("wait_reason='needs_review'", statement)
                self.assertEqual(values, (seconds, '1'))

    def shared_response(self):
        from types import SimpleNamespace
        service = self.make()
        del service._hydrate
        self.membership.return_value = self.page(VIDEO)
        self.provider.repository.claim_targeted_batch.return_value = {'rows': [], 'deferred': []}
        self.repo.resolve_collected.return_value = ['0']
        filler = SimpleNamespace(video_id='fxfill00001', channel_id=CHANNEL, collection_mode='counters')
        service.claim_fill = Mock(return_value=[filler])
        self.provider.fetch_metadata.return_value.append(dict(id=filler.video_id,
            snippet=dict(channelId=CHANNEL), status=dict(privacyStatus='public'), statistics=dict(viewCount='3')))
        self.provider._validate_common_metadata.return_value = (None, None)
        return service, filler

    def test_failed_head_write_preserves_membership_and_does_not_repeat_http(self):
        service, _ = self.shared_response()
        self.repo.observe_head.side_effect = RuntimeError('head write refused')
        with self.assertLogs('discovery_service', level='WARNING') as logs:
            result = service.run_once()
        self.assertEqual((result['claimed'], result['settled'], result['deferred']), (1, 1, 0))
        self.assertIn('discovery_head_write_failed', logs.output[0])
        self.membership.assert_called_once()
        self.repo.record_membership.assert_called_once()
        self.provider.complete_observed_counters.assert_called_once()
        self.repo.defer.assert_not_called()

    def test_optional_counter_write_cannot_delay_primary_notification(self):
        service, filler = self.shared_response()
        def fail_write(*args, **kwargs):
            self.repo.resolve_collected.assert_called_once()
            raise RuntimeError('lost counter acknowledgement')
        self.provider.complete_observed_counters.side_effect = fail_write
        with self.assertLogs('discovery_service', level='WARNING') as logs:
            result = service.run_once()
        self.assertEqual((result['settled'], result['deferred']), (1, 0))
        self.assertTrue(any('discovery_fill_write_unknown' in line for line in logs.output))
        self.provider.repository.release_discovery_admission.assert_called_once_with([filler])
        self.provider.fetch_metadata.assert_called_once()
        self.membership.assert_called_once()

    def test_unknown_metadata_fill_outcome_releases_exact_claim_after_primary(self):
        service, filler = self.shared_response()
        filler.collection_mode = 'metadata'
        def finish(*args, **kwargs):
            self.repo.resolve_collected.assert_called_once()
            self.assertEqual(args[1], [filler])
            yield from ()
            return {'settlement_unknown': True}
        service._common_steps = finish
        with self.assertLogs('discovery_service', level='WARNING') as logs:
            result = service.run_once()
        self.assertEqual(result['settled'], 1)
        self.assertIn('discovery_fill_write_unknown', logs.output[-1])
        self.provider.repository.release_discovery_admission.assert_called_once_with([filler])
        self.provider.fetch_metadata.assert_called_once()

    def test_failure_to_claim_spare_slots_still_sends_notification(self):
        service, _ = self.shared_response()
        service.claim_fill.side_effect = RuntimeError('fill claim failed')
        self.provider.fetch_metadata.return_value.pop()
        with self.assertLogs('discovery_service', level='WARNING') as logs:
            result = service.run_once()
        self.assertEqual(result['settled'], 1)
        self.assertIn('discovery_fill_claim_failed', logs.output[-1])
        self.assertEqual(self.provider.fetch_metadata.call_args.args[0], [VIDEO])
        self.provider.repository.release_discovery_admission.assert_not_called()

    def test_unknown_admission_outcome_remains_fatal_during_fill(self):
        from discovery_admission import DiscoveryAdmissionUnsafe
        service, filler = self.shared_response()
        def unsafe(claims, admission, *args):
            admission.failure = DiscoveryAdmissionUnsafe('grant state lost')
            yield from ()
            raise RuntimeError('optional writer failed')
        service._settle_fill = unsafe
        with self.assertLogs('discovery_service', level='WARNING'):
            with self.assertRaises(DiscoveryAdmissionUnsafe):
                service.run_once()
        self.repo.resolve_collected.assert_called_once()
        self.provider.repository.release_discovery_admission.assert_called_once_with([filler])

    def test_failed_cleanup_does_not_reopen_delivered_notification(self):
        service, _ = self.shared_response()
        self.provider.complete_observed_counters.side_effect = RuntimeError('ack lost')
        self.provider.repository.release_discovery_admission.side_effect = RuntimeError('database unavailable')
        with self.assertLogs('discovery_service', level='WARNING') as logs:
            result = service.run_once()
        self.assertEqual((result['settled'], result['deferred']), (1, 0))
        self.assertTrue(any('discovery_fill_release_unconfirmed' in line for line in logs.output))
        self.provider.fetch_metadata.assert_called_once()

    def test_failure_of_required_membership_keeps_terminal_fence(self):
        service, filler = self.shared_response()
        self.repo.record_membership.side_effect = RuntimeError('required receipt failed')
        with self.assertRaisesRegex(RuntimeError, 'required receipt failed'):
            service.run_once()
        self.repo.resolve_collected.assert_not_called()
        self.provider.complete_observed_counters.assert_not_called()
        self.provider.repository.release_discovery_admission.assert_called_once_with([filler])

    def test_only_future_date_defers_unknown_video_and_creates_no_membership(self):
        service = self.make()
        item = self.provider.fetch_metadata.return_value[0]
        item['liveStreamingDetails'] = {'scheduledStartTime':'2099-01-01T01:00:00Z'}
        service.run_once()
        self.membership.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[1], 'lifecycle_wait')

    def test_long_duration_does_not_prove_long_membership(self):
        service=self.make('PT4M');service.run_once()
        self.assertEqual(self.membership.call_count,3)
        self.assertEqual([c.args[0][:4] for c in self.membership.call_args_list],['UULF','UULF','UULV'])
        self.hydrate.assert_not_called();self.repo.record_membership.assert_not_called()

    def test_short_positive_stops_other_playlist_and_coalesces_events(self):
        service=self.make(rows=2)
        self.membership.return_value={'items':[{'contentDetails':{'videoId':VIDEO}}]}
        service.run_once()
        self.assertEqual(self.membership.call_count,1)
        self.assertEqual(self.provider.fetch_metadata.call_args.args[0],[VIDEO])
        self.assertEqual(len(self.repo.record_membership.call_args.args[0]),2)
        self.assertEqual(len(self.hydrate.call_args.args[0]),2)

    def test_membership_404_does_not_mark_list_or_video_missing(self):
        service=self.make();self.membership.side_effect=lambda playlist, identity: self.absent(playlist)
        service.run_once()
        self.assertEqual(self.membership.call_count,3)
        self.repo.record_membership.assert_not_called();self.hydrate.assert_not_called()

    def test_foreign_channel_never_enters_inventory(self):
        service=self.make();self.provider.fetch_metadata.return_value[0]['snippet']['channelId']='UC'+'y'*22
        service.run_once()
        self.membership.assert_not_called();self.hydrate.assert_not_called()
        self.assertEqual(self.repo.terminal_observation.call_args.args[1],'channel_mismatch')
        self.repo.defer.assert_not_called()

    def test_budget_denial_never_calls_provider(self):
        service=self.make();self.budget.reserve.return_value=dict(allowed=False,reason='window_budget')
        result=service.run_once()
        self.provider.fetch_metadata.assert_not_called();self.membership.assert_not_called()
        self.assertEqual((result['claimed'],result['settled'],result['deferred']),(1,0,1))

    def test_notification_batch_reuses_profiles_collected_during_membership_checks(self):
        service = self.make(rows=12)
        del service._hydrate
        for index, row in enumerate(self.rows):
            row['video_id'] = f'fx{index:09d}'
        ids = [row['video_id'] for row in self.rows]
        self.provider.fetch_metadata.return_value = [
            dict(id=identity, snippet=dict(channelId=CHANNEL), status=dict(privacyStatus='public'),
                 contentDetails=dict(duration='PT1M')) for identity in ids]
        collected = set()

        def membership(playlist, identity):
            collected.update(ids[:4])
            return self.page(*ids)

        self.membership.side_effect = membership
        self.repo.resolve_collected.side_effect = lambda rows, **kw: [
            row['work_id'] for row in rows if row['video_id'] in collected]
        from types import SimpleNamespace
        claims = [SimpleNamespace(video_id=i, lease_token=uuid4(), collection_mode='hydrate',
                                  channel_id=CHANNEL) for i in ids[4:]]
        self.provider.repository.claim_targeted_batch.return_value = dict(
            rows=claims, deferred=[dict(video_id=i, reason='already_attempted_in_run') for i in ids[:4]])
        self.repo.target_state.side_effect = lambda rows: {
            row['work_id']: dict(queue_exists=row['video_id'] in collected) for row in rows}
        self.repo.acknowledge.side_effect = lambda rows: [row['work_id'] for row in rows]
        def finish(*args, **kwargs):
            self.assertEqual([c.video_id for c in args[1]], ids[4:])
            yield from ()
            return {}
        service._common_steps = finish
        result = service.run_once()
        self.assertEqual((result['claimed'], result['settled'], result['deferred']), (12, 12, 0))
        self.repo.defer.assert_not_called()

    def test_existing_video_passes_to_video_owner_before_any_metadata(self):
        service=self.make();self.repo.target_state.return_value={'0':dict(queue_exists=True,video_type='long')}
        service.run_once()
        self.provider.fetch_metadata.assert_not_called();self.membership.assert_not_called();self.hydrate.assert_not_called()
        self.repo.resolve_collected.assert_called_once_with(self.rows, enabled_types=service.enabled, queue_if_stale=True, settings=service.settings)

    def test_confirmed_deletion_skips_availability_backoff_and_provider(self):
        service=self.make()
        self.rows[0]['kind']='deleted'
        self.repo.target_state.return_value={'0':dict(queue_exists=True,video_type='short',
            paused_reason='availability_retry_expired')}
        self.repo.resolve_collected.return_value=['0']
        self.assertEqual(service.run_once()['settled'],1)
        self.repo.resolve_collected.assert_called_once_with(self.rows, enabled_types=service.enabled)
        self.hydrate.assert_not_called()
        self.provider.fetch_metadata.assert_not_called()

    def test_bad_duplicate_response_does_not_count_as_two_videos(self):
        service=self.make();self.provider.fetch_metadata.return_value*=2
        service.run_once()
        self.membership.assert_not_called();self.hydrate.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.kwargs['technical_phase'],'metadata')

    def test_future_date_is_observation_but_does_not_guess_stream_type(self):
        service=self.make()
        future=datetime.now(timezone.utc)+timedelta(hours=4)
        self.provider.fetch_metadata.return_value[0]['liveStreamingDetails']={'scheduledStartTime':future.isoformat()}
        service.run_once()
        self.assertEqual(self.repo.defer.call_args.args[1],'lifecycle_wait')
        self.assertTrue(self.repo.defer.call_args.kwargs['lifecycle_observed'])
        self.assertEqual(self.repo.defer.call_args.kwargs['future_until'],future)
        self.membership.assert_not_called()

    def test_confirmed_disabled_stream_settles_without_provider_work(self):
        service=self.make();self.repo.target_state.return_value={'0':dict(queue_exists=True,video_type='stream')}
        self.repo.settle_disabled.return_value=['0']
        self.assertEqual(service.run_once()['settled'],1)
        self.provider.fetch_metadata.assert_not_called();self.hydrate.assert_not_called();self.repo.defer.assert_not_called()

    def test_unknown_identity_with_existing_pause_never_calls_provider(self):
        service=self.make();self.repo.target_state.return_value={'0':dict(queue_exists=True,video_type=None,paused_reason='held')}
        service.run_once()
        self.provider.fetch_metadata.assert_not_called();self.hydrate.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[1],'held')

    def many(self, *, duration='PT1M'):
        service=self.make(duration)
        self.ids=['fxshared001','fxshared002']
        self.rows=[dict(video_id=identity,channel_id=CHANNEL,work_id=str(index),
                        kind='entry',work_phase='metadata_wait',discovery_attempts=0) for index,identity in enumerate(self.ids)]
        self.repo.claim_event_batch.return_value=self.rows
        self.repo.target_state.return_value={row['work_id']:dict(queue_exists=False,video_type=None) for row in self.rows}
        self.provider.fetch_metadata.return_value=[dict(id=identity,snippet=dict(channelId=CHANNEL),
            status=dict(privacyStatus='public'),contentDetails=dict(duration=duration)) for identity in self.ids]
        return service

    @staticmethod
    def page(*identities):
        return {'items':[{'contentDetails':{'videoId':identity}} for identity in identities]}

    def test_distinct_ids_share_one_frozen_head_and_one_positive_receipt(self):
        service=self.many()
        def provider(playlist,identity):
            self.assertIsNone(identity)
            self.assertEqual(self.repo.start_request.call_args.args[0],self.rows)
            return self.page(*self.ids)
        self.membership.side_effect=provider
        result=service.run_once()
        self.assertEqual(result['discovered'],2)
        self.membership.assert_called_once_with('UUSH'+CHANNEL[2:],None)
        self.assertEqual(self.repo.record_membership.call_args.args[0],self.rows)
        self.assertEqual(len(self.repo.record_membership.call_args.kwargs['items']),2)
        self.assertEqual(self.hydrate.call_args.args[0],self.rows)

    def test_head_filters_unrequested_ids_and_checks_deep_remainder_exactly(self):
        service=self.many()
        self.membership.side_effect=[self.page(self.ids[0],'fxunasked01'),self.page(self.ids[1])]
        service.run_once()
        self.assertEqual([call.args[1] for call in self.membership.call_args_list],[None,self.ids[1]])
        saved=[item['video_id'] for call in self.repo.record_membership.call_args_list for item in call.kwargs['items']]
        self.assertEqual(saved,self.ids)
        self.assertEqual({row['video_id'] for row in self.hydrate.call_args.args[0]},set(self.ids))

    def test_empty_head_does_not_mean_deep_ids_are_absent(self):
        service=self.many()
        self.membership.side_effect=[self.page(),self.page(self.ids[0]),self.page(self.ids[1])]
        service.run_once()
        self.assertEqual([call.args[1] for call in self.membership.call_args_list],[None,*self.ids])
        self.assertEqual(self.repo.record_membership.call_count,2)
        self.repo.defer.assert_not_called()

    def test_shared_request_duration_prunes_short_but_does_not_guess_type(self):
        service=self.many(duration='PT4M')
        self.membership.return_value=self.page(*self.ids)
        service.run_once()
        self.membership.assert_called_once_with('UULF'+CHANNEL[2:],None)
        self.assertEqual(self.repo.record_membership.call_args.kwargs['video_type'],'long')

    def test_shared_404_skips_only_that_playlist_without_exact_retries(self):
        service=self.many()
        self.membership.side_effect=[YouTubePlaylistNotFoundError(confirmed_playlist_id="UUSH"+CHANNEL[2:]),self.page(*self.ids)]
        service.run_once()
        self.assertEqual([call.args for call in self.membership.call_args_list],
                         [('UUSH'+CHANNEL[2:],None),('UULF'+CHANNEL[2:],None)])
        self.repo.record_membership.assert_called_once()
        self.assertEqual(self.repo.record_membership.call_args.kwargs['video_type'],'long')

    def test_unknown_head_outcome_defers_exact_group_without_fallthrough(self):
        service=self.many()
        self.membership.side_effect=TimeoutError('fixture')
        result=service.run_once()
        self.membership.assert_called_once()
        self.repo.record_membership.assert_not_called();self.hydrate.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[:2],(self.rows,'retry'))
        self.assertEqual((result['claimed'],result['settled'],result['deferred']),(2,0,2))

    def test_released_failed_owner_is_pending_in_waiting_only_report(self):
        service=self.make();self.rows[0]['work_phase']='owner_wait'
        self.repo.acknowledge.return_value=[]
        self.repo.release_failed_owner.return_value=['0']
        result=service.run_once()
        self.assertEqual((result['claimed'],result['settled'],result['deferred']),(1,0,1))
        self.provider.fetch_metadata.assert_not_called()

    def test_malformed_head_is_not_an_empty_success(self):
        service=self.many();self.membership.return_value={}
        service.run_once()
        self.membership.assert_called_once();self.repo.record_membership.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[1],'retry')

    def test_duplicate_or_oversized_head_is_rejected_before_positive_write(self):
        for page in (self.page('fxshared001','fxshared001'),self.page(*[f'fxlarge{i:04}' for i in range(51)])):
            with self.subTest(size=len(page['items'])):
                service=self.many();self.membership.return_value=page
                service.run_once()
                self.membership.assert_called_once();self.repo.record_membership.assert_not_called()
                self.assertEqual(self.repo.defer.call_args.args[1],'retry')

    def test_distinct_channels_never_share_one_playlist_request(self):
        service=self.many();other='UC'+'y'*22
        self.rows[1]['channel_id']=other
        self.provider.fetch_metadata.return_value[1]['snippet']['channelId']=other
        self.membership.side_effect=lambda playlist,identity:self.page(self.ids[0] if playlist.endswith(CHANNEL[2:]) else self.ids[1])
        service.run_once()
        self.assertEqual([call.args for call in self.membership.call_args_list],
                         [('UUSH'+CHANNEL[2:],None),('UUSH'+other[2:],None)])

    def test_known_foreign_channel_rejects_only_its_event(self):
        service=self.many()
        self.repo.target_state.return_value['0']=dict(channel_mismatch=True,queue_exists=True,video_type='long')
        self.provider.fetch_metadata.return_value=self.provider.fetch_metadata.return_value[1:]
        self.membership.return_value=self.page(self.ids[1])
        result=service.run_once()
        self.assertEqual(self.repo.terminal_observation.call_args.args[0],[self.rows[0]])
        self.assertEqual(self.provider.fetch_metadata.call_args.args[0],[self.ids[1]])
        self.assertEqual(self.hydrate.call_args.args[0],[self.rows[1]])
        self.assertEqual(result['settled'],1)

    def test_same_id_under_two_channels_keeps_correct_metadata_target(self):
        service=self.make(rows=2)
        self.rows[0]['channel_id']='UC'+'y'*22
        self.membership.return_value=self.page(VIDEO)
        result=service.run_once()
        self.provider.fetch_metadata.assert_called_once_with([VIDEO],'discovery_metadata')
        self.assertEqual(self.repo.terminal_observation.call_args.args[:2],([self.rows[0]],'channel_mismatch'))
        self.assertEqual(self.hydrate.call_args.args[0],[self.rows[1]])
        self.assertEqual(result['settled'],1)

    def test_malformed_provider_channel_does_not_poison_healthy_batch_row(self):
        service=self.many()
        self.provider.fetch_metadata.return_value[0]['snippet']['channelId']='UC-invalid'
        self.membership.return_value=self.page(self.ids[1])
        service.run_once()
        self.repo.terminal_observation.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[0],[self.rows[0]])
        self.assertEqual(self.repo.defer.call_args.kwargs['technical_phase'],'metadata')
        self.assertEqual(self.hydrate.call_args.args[0],[self.rows[1]])

    def test_missing_deleted_event_does_not_close_entry_for_same_id(self):
        service=self.make(rows=2);self.rows[0]['kind']='deleted'
        self.provider.fetch_metadata.return_value=[]
        result=service.run_once()
        self.assertEqual(self.repo.terminal_observation.call_args.args[:2],([self.rows[0]],'unknown_deletion_checked'))
        self.assertEqual(self.repo.terminal_observation.call_args.kwargs['items'],[])
        self.assertEqual(self.repo.defer.call_args.args[:2],([self.rows[1]],'metadata_not_returned'))
        self.assertEqual((result['settled'],result['deferred']),(1,1))
        self.repo.acknowledge.assert_not_called()

    def test_terminal_proof_race_defers_only_unsettled_and_keeps_healthy_batch(self):
        service=self.many();self.rows[0]['kind']='deleted'
        self.provider.fetch_metadata.return_value=self.provider.fetch_metadata.return_value[1:]
        self.repo.terminal_observation.side_effect=None
        self.repo.terminal_observation.return_value=[]
        self.membership.return_value=self.page(self.ids[1])
        result=service.run_once()
        self.assertEqual(self.repo.defer.call_args.args[:2],([self.rows[0]],'owner_busy'))
        self.assertEqual(self.hydrate.call_args.args[0],[self.rows[1]])
        self.assertEqual(result['settled'],0)
        self.repo.acknowledge.assert_not_called()

    def test_terminal_result_cannot_settle_an_unclaimed_event(self):
        service=self.make();self.rows[0]['kind']='deleted'
        self.provider.fetch_metadata.return_value=[]
        self.repo.terminal_observation.side_effect=None
        self.repo.terminal_observation.return_value=['unclaimed']
        with self.assertRaisesRegex(RuntimeError,'^discovery_terminal_outcome_unknown$'):
            service.run_once()
        self.repo.defer.assert_not_called();self.hydrate.assert_not_called()

    def test_unknown_completed_stream_has_one_positive_diagnostic_and_no_inventory(self):
        service=self.make('PT1H')
        now=datetime.now(timezone.utc)
        self.provider.fetch_metadata.return_value[0]['liveStreamingDetails']={
            'actualStartTime':(now-timedelta(hours=2)).isoformat(),
            'actualEndTime':(now-timedelta(hours=1)).isoformat()}
        publication=(now-timedelta(days=2)).isoformat()
        self.provider.fetch_metadata.return_value[0]['snippet']['publishedAt']=now.isoformat()
        positive=self.page(VIDEO)
        positive['items'][0]['contentDetails']['videoPublishedAt']=publication
        self.membership.side_effect=[self.page(),self.page(),positive]
        result=service.run_once()
        self.assertEqual([c.args[0][:4] for c in self.membership.call_args_list],['UULF','UULF','UULV'])
        self.assertEqual(result['settled'],1)
        self.assertEqual(self.repo.terminal_observation.call_args.args[1],'format_off')
        self.assertEqual(self.repo.terminal_observation.call_args.kwargs['items'],[dict(video_id=VIDEO,published_at=publication)])
        self.repo.record_membership.assert_not_called();self.hydrate.assert_not_called()
        self.repo.acknowledge.assert_not_called()

    def test_negative_disabled_probe_is_not_repeated_on_sparse_retry(self):
        service=self.make('PT4M')
        self.rows[0]['disabled_stream_probe_at']=datetime.now(timezone.utc)-timedelta(hours=6)
        service.run_once()
        self.assertEqual([call.args[1] for call in self.membership.call_args_list],[None,VIDEO])
        self.repo.mark_disabled_probe.assert_not_called();self.repo.terminal_observation.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.args[1],'membership_not_observed')

    def test_new_event_allows_one_diagnostic_for_coalesced_id(self):
        service=self.make('PT4M',rows=2)
        self.rows[0]['disabled_stream_probe_at']=datetime.now(timezone.utc)-timedelta(days=1)
        service.run_once()
        self.assertEqual(self.membership.call_count,3)
        self.assertEqual(self.repo.mark_disabled_probe.call_args.args[0],self.rows)

    def test_live_or_unfinished_observation_defers_metadata_without_membership(self):
        for marker in ('live','upcoming','actual_start'):
            with self.subTest(marker=marker):
                service=self.make()
                item=self.provider.fetch_metadata.return_value[0]
                if marker=='actual_start':
                    item['liveStreamingDetails']={'actualStartTime':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}
                else:item['snippet']['liveBroadcastContent']=marker
                service.run_once()
                self.membership.assert_not_called();self.hydrate.assert_not_called()
                self.assertEqual(self.repo.defer.call_args.args[1],'lifecycle_wait')
                self.assertIsNone(self.repo.defer.call_args.kwargs['future_until'])

    def test_invalid_lifecycle_does_not_pause_problem_age(self):
        service=self.make()
        item=self.provider.fetch_metadata.return_value[0]
        item['snippet']['liveBroadcastContent']='live'
        item['liveStreamingDetails']={'actualEndTime':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}
        service.run_once()
        self.repo.complete_attempt.assert_not_called();self.membership.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.kwargs['technical_phase'],'metadata')
        self.assertFalse(self.repo.defer.call_args.kwargs['lifecycle_observed'])

    def test_repeated_metadata_errors_use_durable_phase_count_without_evidence(self):
        now=datetime(2026,11,9,tzinfo=timezone.utc)
        for failed in range(10):
            service=self.make();service.clock=lambda:now
            self.rows[0]['technical_metadata_failures']=failed
            self.provider.fetch_metadata.side_effect=TimeoutError('fixture')
            service.run_once()
            self.assertEqual(self.repo.defer.call_args.args[2],now+timedelta(seconds=min(120*2**failed,21600)))
            self.assertEqual(self.repo.defer.call_args.kwargs['technical_phase'],'metadata')
            self.repo.complete_attempt.assert_not_called()

    def test_shared_metadata_error_preserves_individual_technical_delays(self):
        service=self.many();now=datetime(2026,11,9,tzinfo=timezone.utc);service.clock=lambda:now
        self.rows[0]['technical_metadata_failures']=9
        self.provider.fetch_metadata.side_effect=TimeoutError('fixture')
        service.run_once()
        self.provider.fetch_metadata.assert_called_once()
        deadlines={row['work_id']:call.args[2] for call in self.repo.defer.call_args_list for row in call.args[0]}
        self.assertEqual(deadlines,{'0':now+timedelta(hours=6),'1':now+timedelta(minutes=2)})
        self.repo.complete_attempt.assert_not_called()

    def test_shared_negative_metadata_preserves_individual_evidence_delays(self):
        service=self.many();now=datetime(2026,11,9,tzinfo=timezone.utc);service.clock=lambda:now
        self.rows[0]['discovery_attempts']=9
        self.provider.fetch_metadata.return_value=[]
        service.run_once()
        self.provider.fetch_metadata.assert_called_once()
        deadlines={row['work_id']:call.args[2] for call in self.repo.defer.call_args_list for row in call.args[0]}
        self.assertEqual(deadlines,{'0':now+timedelta(hours=6),'1':now+timedelta(minutes=2)})

    def test_custom_short_policy_reaches_db_intact_on_claim_defer_and_owner_recovery(self):
        service=self.make();now=datetime(2026,11,9,tzinfo=timezone.utc);service.clock=lambda:now
        settings=replace(service.settings,retry_first_seconds=30,retry_second_seconds=60,
            retry_third_seconds=120,retry_sparse_seconds=600,needs_review_seconds=1800,
            review_retry_seconds=3600,unresolved_pause_seconds=7200,
            technical_retry_first_seconds=30,technical_retry_max_seconds=600)
        service.settings=settings
        self.provider.fetch_metadata.return_value=[]
        service.run_once()
        self.assertIs(self.repo.claim_event_batch.call_args.kwargs['settings'],settings)
        self.assertIs(self.repo.defer.call_args.kwargs['settings'],settings)
        self.assertEqual(self.repo.defer.call_args.args[2],now+timedelta(seconds=30))
        self.repo.defer.reset_mock()
        self.rows[0]['work_phase']='owner_wait'
        self.repo.acknowledge.return_value=[];self.repo.release_failed_owner.return_value=[]
        service.run_once()
        self.assertIs(self.repo.release_failed_owner.call_args.kwargs['settings'],settings)
        self.assertIs(self.repo.defer.call_args.kwargs['settings'],settings)

    def test_metadata_and_short_success_do_not_reset_failing_long_stage(self):
        service=self.make();now=datetime(2026,11,9,tzinfo=timezone.utc);service.clock=lambda:now
        self.rows[0]['technical_membership_failures']=4
        self.membership.side_effect=[self.page(),TimeoutError('fixture')]
        service.run_once()
        self.repo.record_membership_check.assert_not_called()
        self.assertEqual(self.repo.defer.call_args.kwargs['technical_phase'],'membership')
        self.assertEqual(self.repo.defer.call_args.args[2],now+timedelta(seconds=1920))

    def test_readiness_refusal_before_http_does_not_bind_nonexistent_request(self):
        service=self.make();service.require_ready=Mock(side_effect=[None,RuntimeError('not_ready')])
        service.run_once()
        self.provider.fetch_metadata.assert_not_called();self.repo.start_request.assert_not_called()
        self.assertIsNone(self.repo.defer.call_args.kwargs['request_id'])

    def test_unknown_request_commit_stops_before_http_without_false_retry(self):
        from discovery_admission import DiscoveryAdmissionUnsafe
        service=self.make();self.repo.start_request.side_effect=RuntimeError('commit_unknown')
        with self.assertRaises(DiscoveryAdmissionUnsafe):service.run_once()
        self.provider.fetch_metadata.assert_not_called();self.repo.defer.assert_not_called()

    def test_expired_availability_keeps_exact_pause_reason_without_provider(self):
        service=self.make()
        self.repo.target_state.return_value={'0':dict(queue_exists=True,video_type=None,
                                                     paused_reason='availability_retry_expired')}
        result=service.run_once()
        self.repo.pause.assert_called_once_with(self.rows,'availability_retry_expired')
        self.repo.defer.assert_not_called();self.provider.fetch_metadata.assert_not_called()
        self.assertEqual(result['deferred'],1)

    def test_membership_race_rejection_does_not_hydrate_rejected_row(self):
        service=self.many();self.membership.return_value=self.page(*self.ids)
        self.repo.record_membership.side_effect=None
        self.repo.record_membership.return_value=dict(accepted_work_ids=['1'],rejected_work_ids=['0'])
        result=service.run_once()
        self.assertEqual(result['settled'],1)
        self.assertEqual(self.hydrate.call_args.args[0],[self.rows[1]])


if __name__=='__main__':unittest.main()
