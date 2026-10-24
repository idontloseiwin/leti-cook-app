"""Provider refusal and atomic settlement taxonomy, using only offline doubles."""
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock,patch
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from youtube_errors import (YouTubeRateLimitError,YouTubeDailyQuotaExceededError,
                            youtube_rate_limit_retry_after,youtube_response_error,
                            YouTubeAPIError,YouTubePlaylistNotFoundError,is_playlist_not_found_error)
from youtube_channels import fetch_channel_metadata_batch
from youtube_playlist import fetch_playlist_items_page
from youtube_video_profiles import fetch_video_metadata,fetch_initial_video_profiles
from collection_policy import failure_policy,inventory_source_policy
from collection.inventory import run_typed_inventory_claim,run_typed_inventory_batch
from video_collection_service import run_claimed_common_video_collection,run_claimed_initial_video_collection
from test_batch_stats_quota import Repository,InitialRepository,collection_service,metadata,RATE
from test_shared_inventory_engine import Repository as InventoryRepository,claim,NOW,CHANNEL
from test_error_codes_not_text import sync_functions

class Response:
    def __init__(self,status,payload,retry='7'):
        self.status_code,self.payload,self.headers=status,payload,{'Retry-After':retry}
    def json(self):
        if self.payload is None:raise ValueError('invalid body')
        return self.payload
    def raise_for_status(self):
        if self.status_code>=400:raise AssertionError('classified refusal reached HTTP handler')

class SourceTests(unittest.TestCase):
    def test_all_sources_one_request_and_bounded_rate_delay(self):
        providers=((fetch_channel_metadata_batch,[CHANNEL]),
                   (fetch_playlist_items_page,'UULF'+CHANNEL[2:]),
                   (fetch_video_metadata,['vid00000001']),
                   (fetch_initial_video_profiles,['vid00000001']))
        for provider,ids in providers:
            for status,payload in ((403,RATE),(429,None)):
                with self.subTest(provider=provider.__name__,status=status):
                    transport=Mock(return_value=Response(status,payload))
                    with self.assertRaises(YouTubeRateLimitError) as caught:
                        provider(ids,youtube_api_get=transport,api_key=None,attempt_recorder=None,
                                 timeout_seconds=1,emit_event=Mock(),perf_counter=lambda:0)
                    transport.assert_called_once()
                    self.assertEqual(caught.exception.retry_after_seconds,7)
                    self.assertEqual(str(caught.exception),'provider_rate_limited')

    def test_constructor_bounds_and_daily_evidence_wins(self):
        for given,expected in ((None,60),(True,60),('4',60),(-8,1),(0,1),(4,4),(900,60)):
            self.assertEqual(YouTubeRateLimitError(retry_after_seconds=given).retry_after_seconds,expected)
        self.assertIsNone(youtube_rate_limit_retry_after(
            RuntimeError('reason=rateLimitExceeded | reason=quotaExceeded')))
        error=youtube_response_error(Response(429,{},'0'),
            {'error':{'errors':[{'reason':'dailyLimitExceeded'},{'reason':'rateLimitExceeded'}]}})
        self.assertIsInstance(error,YouTubeDailyQuotaExceededError)

    def test_generic_404_is_not_rate_or_daily(self):
        self.assertIsNone(youtube_response_error(Response(404,{}),{'error':{'message':'untrusted'}}))

    def test_playlist_error_status_without_error_body_is_never_empty_success(self):
        for status in (400,403,404):
            transport=Mock(return_value=Response(status,{}))
            with self.subTest(status=status),self.assertRaises(YouTubeAPIError) as caught:
                fetch_playlist_items_page('UULF'+CHANNEL[2:],youtube_api_get=transport,
                    api_key=None,attempt_recorder=None,timeout_seconds=1,emit_event=Mock(),perf_counter=lambda:0)
            transport.assert_called_once()
            self.assertFalse(is_playlist_not_found_error(caught.exception))

    def test_playlist_message_only_is_unknown_and_exact_reason_is_absent(self):
        for exact in (False,True):
            payload={'error':{'message':'The playlist cannot be found'}}
            if exact:payload['error']['errors']=[{'reason':'playlistNotFound'}]
            transport=Mock(return_value=Response(404,payload))
            expected=YouTubePlaylistNotFoundError if exact else YouTubeAPIError
            with self.subTest(exact=exact),self.assertRaises(expected) as caught:
                fetch_playlist_items_page('UULF'+CHANNEL[2:],youtube_api_get=transport,
                    api_key=None,attempt_recorder=None,timeout_seconds=1,emit_event=Mock(),perf_counter=lambda:0)
            transport.assert_called_once()
            self.assertEqual(is_playlist_not_found_error(caught.exception),exact)

class PolicyTests(unittest.TestCase):
    def test_video_rate_short_retry_does_not_spend_daily_failure_count(self):
        for typed in (False,True):
            for requested,seconds in ((None,60),(0,1),(8,8),(900,60)):
                result=failure_policy(now=NOW,consecutive_failures=9,error_type='provider_rate_limited',
                                      retry_after_seconds=requested,typed=typed)
                self.assertEqual(result.retry_at,NOW+timedelta(seconds=seconds))
                self.assertEqual(result.consecutive_failures,9)

    def test_inventory_rate_bound_for_all_chains_without_daily_wait(self):
        for kind in ('long','short','stream'):
            for scan in ('initial_inventory','full_reconciliation','discovery'):
                result=inventory_source_policy(kind,scan,'error',0,3,99,99,'provider_rate_limited',NOW)
                self.assertEqual(result.next_due_at,NOW+timedelta(seconds=60))
                self.assertEqual(result.no_new_streak,3)

    def test_daily_failure_waits_pacific_midnight_across_dst(self):
        for now,reset in (('2026-11-01T12:00:00+00:00','2026-11-02T08:00:00+00:00'),
                          ('2026-03-08T12:00:00+00:00','2026-03-09T07:00:00+00:00')):
            for typed in (False,True):
                result=failure_policy(now=datetime.fromisoformat(now),consecutive_failures=9,
                    error_type='youtube_quota_exhausted',retry_after_seconds=30,typed=typed)
                self.assertEqual(result.retry_at,datetime.fromisoformat(reset))
                self.assertEqual(result.consecutive_failures,9)

    def test_metadata_rate_preserves_failure_count_and_code(self):
        updates=[]
        sync_functions(updates)['mark_channel_metadata_error'](
            {'channel_id':CHANNEL,'collection_control_epoch':7,'channel_metadata_error_count':8},
            YouTubeRateLimitError(retry_after_seconds=9),NOW)
        self.assertEqual(updates[0]['channel_metadata_error'],'provider_rate_limited')
        self.assertEqual(updates[0]['channel_metadata_error_count'],8)
        self.assertEqual(updates[0]['channel_metadata_next_after'],(NOW+timedelta(seconds=9)).isoformat())

class SettlementTests(unittest.TestCase):
    def test_general_rate_stops_before_counter_request_and_missing(self):
        repository,calls=Repository(),{'metadata':0,'counters':0}
        service=collection_service(repository,200,{},calls)
        service.fetch_metadata=Mock(side_effect=YouTubeRateLimitError(retry_after_seconds=4))
        result=run_claimed_common_video_collection(repository,service,limit=150,worker_id='worker')
        self.assertEqual(repository.claim_calls,1)
        service.fetch_metadata.assert_called_once()
        self.assertEqual(calls['counters'],0)
        self.assertEqual(len(repository.failed),50)
        self.assertEqual({row['retry_after_seconds'] for row in repository.failed},{4})
        self.assertEqual({row['error_type'] for row in repository.failed},{'provider_rate_limited'})
        self.assertTrue(result['rate_limited'])
        self.assertFalse(result['quota_exhausted'])
        self.assertEqual(result['unavailable_marked'],0)

    def test_general_unknown_receipt_prevents_next_write_or_provider(self):
        repository,calls=Repository(),{'metadata':0,'counters':0}
        repository.fail_batch=Mock(side_effect=TimeoutError('unknown commit'))
        service=collection_service(repository,200,{},calls)
        service.fetch_metadata=Mock(side_effect=YouTubeRateLimitError())
        result=run_claimed_common_video_collection(repository,service,limit=150,worker_id='worker')
        repository.fail_batch.assert_called_once()
        service.fetch_metadata.assert_called_once()
        self.assertEqual(calls['counters'],0)
        self.assertTrue(result['settlement_unknown'])

    def test_initial_general_rate_fails_but_counter_rate_preserves_general_profile(self):
        for at_counter in (False,True):
            repository,calls=InitialRepository(),{'metadata':0,'counters':0}
            service=collection_service(repository,429,None,calls)
            service.fetch_initial_profiles=Mock(
                side_effect=metadata if at_counter else YouTubeRateLimitError(retry_after_seconds=11))
            result=run_claimed_initial_video_collection(service,job_id='job',channel_id=CHANNEL,
                video_ids=[f'vid{i:08d}' for i in range(50)],worker_id='worker',run_id='run',concurrency=1)
            self.assertEqual(repository.claim_calls,1)
            service.fetch_initial_profiles.assert_called_once()
            self.assertEqual(calls,{'metadata':0,'counters':int(at_counter)})
            self.assertEqual(len(repository.commits),1)
            if at_counter:
                self.assertEqual(repository.failed,[])
                self.assertEqual(len(repository.identities),50)
                self.assertEqual({row['video_id'] for row in repository.identities},
                                 {f'vid{i:08d}' for i in range(50)})
                for row in repository.identities:
                    self.assertTrue(row['counterless_profile'])
                    self.assertFalse(row['missing'])
                    self.assertEqual(row['title'],'Synthetic profile')
                    self.assertFalse({'views','like_count','comment_count','error_type','retry_after_seconds'} & row.keys())
            else:
                self.assertEqual(repository.identities,[])
                self.assertEqual(len(repository.failed),50)
                self.assertEqual({row['error_type'] for row in repository.failed},{'provider_rate_limited'})
                self.assertEqual({row['retry_after_seconds'] for row in repository.failed},{11})
            self.assertEqual(result['metadata_deferred'],50 if at_counter else 0)
            self.assertEqual(result['batch_requests'],int(at_counter))
            self.assertEqual(result['stopped_reason'],'provider_rate_limited')
            self.assertTrue(result['rate_limited'])
            for key in ('quota_exhausted','batch_unavailable','infrastructure_error'):
                self.assertFalse(result[key],key)
            self.assertEqual((result['unavailable_marked'],result['fallback_requests']),(0,0))

    def test_initial_unknown_atomic_commit_is_not_ack_or_retried(self):
        repository,calls=InitialRepository(),{'metadata':0,'counters':0}
        repository.complete_initial_batch=Mock(side_effect=TimeoutError('unknown commit'))
        service=collection_service(repository,429,None,calls)
        service.fetch_initial_profiles=Mock(side_effect=YouTubeRateLimitError())
        result=run_claimed_initial_video_collection(service,job_id='job',channel_id=CHANNEL,
            video_ids=['vid00000001'],worker_id='worker',run_id='run',concurrency=1)
        repository.complete_initial_batch.assert_called_once()
        service.fetch_initial_profiles.assert_called_once()
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual((result['updated'],result['unavailable_marked']),(0,0))
        self.assertFalse(repository.failed)

    def test_playlist_rate_error_keeps_original_cursor_not_absence(self):
        for kind in ('long','short','stream'):
            repository=InventoryRepository()
            owned=claim(kind,active_scan_id='scan',resume_page_token='cursor')
            provider=Mock(side_effect=YouTubeRateLimitError())
            result=run_typed_inventory_claim(repository=repository,claim=owned,
                fetch_page=provider,now=lambda:NOW)
            provider.assert_called_once()
            failure=[event[1] for event in repository.events if isinstance(event,tuple)][0]
            self.assertEqual(failure['error_code'],'provider_rate_limited')
            self.assertEqual(failure['expected_page_token'],'cursor')
            self.assertEqual(failure['outcome'],'error')
            self.assertTrue(result['rate_limited'])
            self.assertEqual((result['pages_processed'],result['provider_attempts']),(0,1))

    def test_inventory_rate_stops_pending_formats_without_callback(self):
        repository=InventoryRepository()
        owned=[claim("long"),claim("short"),claim("stream")]
        repository.claim_pass=Mock(return_value=owned)
        provider=Mock(side_effect=YouTubeRateLimitError())
        with patch("db.hot.hot_collection_control.abort_pending_playlists") as release:
            result=run_typed_inventory_batch(repository=repository,fetch_page=provider,
                limit=3,worker_id="offline",prepare=False,now=lambda:NOW)
        provider.assert_called_once()
        repository.claim_pass.assert_called_once()
        release.assert_called_once_with(repository.database,owned[1:])
        self.assertTrue(result["rate_limited"])
        self.assertFalse(result["quota_exhausted"])
        self.assertEqual(result["provider_attempts"],1)
        self.assertEqual(result["playlist_not_found"],0)

    def test_playlist_unknown_receipt_never_returns_success(self):
        repository=InventoryRepository()
        repository.fail_scan=Mock(side_effect=TimeoutError('unknown commit'))
        provider=Mock(side_effect=YouTubeRateLimitError())
        with self.assertRaises(RuntimeError) as caught:
            run_typed_inventory_claim(repository=repository,claim=claim(),fetch_page=provider,now=lambda:NOW)
        self.assertEqual(caught.exception.provider_error_type,'provider_rate_limited')
        provider.assert_called_once()
        repository.fail_scan.assert_called_once()

if __name__=='__main__':unittest.main()
