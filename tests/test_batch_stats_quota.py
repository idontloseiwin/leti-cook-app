"""Batch-statistics refusals stop collection. No server, database or provider."""
from pathlib import Path
from functools import partial
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import youtube_batch_stats
import youtube_errors
from youtube_batch_stats import YouTubeBatchStatsError, fetch_video_batch_stats, is_batch_stats_quota_error
from youtube_video_profiles import fetch_initial_video_profiles
from collection_outcome import copy_collection_stop, sync_completion_fields
from db import VideoCollectionClaim
from operator_jobs import operator_collection_stop_reason
from video_collection_service import (VideoCollectionService, run_claimed_common_video_collection,
                                      run_claimed_initial_video_collection)

SECRET='AIza'+'s'*35
QUOTA={'error':{'code':403,'message':'Quota exceeded for key='+SECRET,'errors':[{'reason':'quotaExceeded','message':SECRET}]}}
RATE={'error':{'code':403,'message':'Rate limited','errors':[{'reason':'userRateLimitExceeded'}]}}
EXHAUSTED={'error':{'code':429,'message':'Resource has been exhausted','status':'RESOURCE_EXHAUSTED'}}
ACCESS={'error':{'code':403,'message':'accessNotConfigured '+SECRET,'errors':[{'reason':'accessNotConfigured'}]}}

class Response:
    def __init__(self,status,payload):
        self.status_code,self.payload=status,payload
    def json(self):
        if self.payload is None:raise ValueError('not JSON')
        return self.payload
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError(f'HTTP {self.status_code}')

def batch(status,payload,ids=('vid00000001',)):
    with patch.object(youtube_batch_stats,'youtube_api_get',lambda *a,**k:Response(status,payload)):
        return fetch_video_batch_stats(list(ids),api_key=None,attempt_recorder=None)

def sync_classifier():
    return partial(youtube_errors.is_youtube_daily_quota_exhausted,
                   is_batch_stats_quota_error=is_batch_stats_quota_error)

class ProviderRefusalTests(unittest.TestCase):
    def test_quota_and_rate_limit_refusals_are_classified(self):
        for status,payload in ((403,QUOTA),):
            with self.assertRaises(YouTubeBatchStatsError) as caught:batch(status,payload)
            self.assertEqual(caught.exception.code,'quota_exhausted',(status,payload))
            self.assertTrue(is_batch_stats_quota_error(caught.exception))
            self.assertTrue(sync_classifier()(caught.exception))
            self.assertNotIn(SECRET,str(caught.exception))

    def test_rate_refusals_are_temporary_typed_errors(self):
        for status,payload in ((403,RATE),(429,EXHAUSTED),(429,None)):
            with self.assertRaises(youtube_errors.YouTubeRateLimitError) as caught:batch(status,payload)
            self.assertEqual(caught.exception.code,'provider_rate_limited')
            self.assertEqual(caught.exception.retry_after_seconds,60)
            self.assertFalse(is_batch_stats_quota_error(caught.exception))
            self.assertFalse(sync_classifier()(caught.exception))
            self.assertNotIn(SECRET,str(caught.exception))

    def test_other_refusals_are_not_quota(self):
        with self.assertRaises(YouTubeBatchStatsError) as caught:batch(403,ACCESS)
        self.assertEqual(caught.exception.code,'api_error_response')
        self.assertFalse(sync_classifier()(caught.exception))
        self.assertNotIn(SECRET,str(caught.exception))

    def test_successful_response_is_unchanged(self):
        result=batch(200,{'items':[]},ids=('vid00000001','vid00000002'))
        self.assertEqual(result['summary']['failed_ids'],['vid00000001','vid00000002'])

class Repository:
    """Supplies three batches of claims: 25 long metadata claims and 25 long counter claims each."""
    def __init__(self):
        self.claim_calls,self.failed,self.identities,self.commits=0,[],[],[]
    def reconcile_typed(self,video_type,limit):return {'affected_count':0}
    def claim_due_batch(self,limit,worker_id,run_id,lease_seconds):
        self.claim_calls+=1
        base=(self.claim_calls-1)*50
        rows=[VideoCollectionClaim(video_id=f'vid{base+i:08d}',channel_id='UC'+'c'*22,
                                   collection_mode='metadata' if i<25 else 'counters',lease_token=f't{base+i}',
                                   lease_expires_at='2026-09-24T00:15:00+00:00',next_due_at='2026-09-24T00:00:00+00:00')
              for i in range(50)] if self.claim_calls<=3 else []
        return {'rows':rows,'inspected_count':len(rows),'paused_count':0,'scan_exhausted':self.claim_calls>=3}
    def load_counter_presence(self,video_ids):return {video_id:True for video_id in video_ids}
    def fail_batch(self,rows):
        self.failed.extend(rows)
        return len(rows)
    def complete_identity_refresh(self,rows):
        self.identities.extend(rows)
        return len(rows)

def metadata(video_ids,phase):
    return [{'id':video_id,'snippet':{'channelId':'UC'+'c'*22,'publishedAt':'2026-01-01T00:00:00Z','title':'Synthetic profile'},
             'contentDetails':{'duration':'PT10M'},'status':{'privacyStatus':'public','uploadStatus':'processed'}}
            for video_id in video_ids]

class InitialRepository(Repository):
    """Operator hydration claims: fixed targets in chunks of 50."""
    def claim_initial_video_ids(self,video_ids,**lease):
        self.claim_calls+=1
        return [VideoCollectionClaim(video_id=video_id,channel_id='UC'+'c'*22,
                                     collection_mode='metadata',lease_token=f't{video_id}',
                                     lease_expires_at='2026-09-24T00:15:00+00:00',next_due_at='2026-09-24T00:00:00+00:00')
                for i,video_id in enumerate(video_ids)]
    def complete_initial_batch(self,commands):
        self.commits.append(commands)
        for command in commands:
            assert command['operation'] in {'fail_batch','complete_identity_refresh'}
            getattr(self,command['operation'])(command['rows'])
        return sum(len(command['rows']) for command in commands)


def collection_service(repository,status,payload,calls):
    def fetch_metadata(ids,phase):
        calls['metadata']+=1;return metadata(ids,phase)
    def fetch_counters(ids,phase):
        calls['counters']+=1;return batch(status,payload,ids=ids)
    return VideoCollectionService(repository,fetch_full=fetch_metadata,fetch_metadata=fetch_metadata,
                                  fetch_counters=fetch_counters,build_full_payload=lambda *a:{},
                                  build_counter_payload=lambda *a:{},is_quota_exhausted=sync_classifier())

def run_collection(status,payload):
    repository,calls=Repository(),{'metadata':0,'counters':0}
    service=collection_service(repository,status,payload,calls)
    summary=run_claimed_common_video_collection(repository,service,limit=150,worker_id='worker')
    return summary,repository,calls

class CollectionRunTests(unittest.TestCase):
    def assert_preserved_general_profiles(self,summary,repository):
        # The first half already has a valid General observation; only the
        # counter-only half retains the provider refusal's retry state.
        self.assertEqual({row['video_id'] for row in repository.identities},
                         {f'vid{i:08d}' for i in range(25)})
        self.assertEqual({row['video_id'] for row in repository.failed},
                         {f'vid{i:08d}' for i in range(25,50)})
        self.assertEqual((summary['metadata_deferred'],summary['source_view_count_absent'],
                          summary['updated'],summary['unavailable_marked'],summary['errors']),(25,25,0,0,1))
        for row in repository.identities:
            self.assertTrue(row['counterless_profile'])
            self.assertFalse(row['missing'])
            self.assertEqual(row['title'],'Synthetic profile')
            self.assertEqual(row['lease_token'],'t'+str(int(row['video_id'][3:])))
            self.assertFalse({'views','like_count','comment_count','error_type','retry_after_seconds'} & row.keys())

    def test_batch_quota_refusal_stops_the_run(self):
        summary,repository,calls=run_collection(403,QUOTA)
        self.assertEqual((repository.claim_calls,calls),(1,{'metadata':1,'counters':1}))
        self.assertEqual((summary['stopped_reason'],summary['batch_quota_exhausted'],summary['quota_exhausted']),
                         ('quota_exhausted',True,True))
        self.assertEqual({row['error_type'] for row in repository.failed},{'youtube_quota_exhausted'})
        self.assertEqual({row['retry_after_seconds'] for row in repository.failed},{3600})
        self.assert_preserved_general_profiles(summary,repository)

    def test_rate_stops_after_one_batch_without_daily_or_fallback(self):
        summary,repository,calls=run_collection(429,None)
        self.assertEqual((repository.claim_calls,calls),(1,{'metadata':1,'counters':1}))
        self.assertEqual((summary['stopped_reason'],summary['rate_limited'],
                          summary['batch_unavailable'],summary['quota_exhausted']),
                         ('provider_rate_limited',True,False,False))
        self.assertEqual({row['error_type'] for row in repository.failed},{'provider_rate_limited'})
        self.assertEqual({row['retry_after_seconds'] for row in repository.failed},{60})
        self.assert_preserved_general_profiles(summary,repository)
        self.assertEqual(summary['unavailable_marked'],0)
        self.assertEqual(summary['fallback_requests'],0)
        target={}
        self.assertTrue(copy_collection_stop(target,summary))
        self.assertTrue(target['rate_limited'])
        self.assertFalse(target['quota_exhausted'])
        self.assertEqual(operator_collection_stop_reason(summary),'rate_limited')

    def test_other_batch_refusal_stops_the_run_without_quota_flags(self):
        summary,repository,calls=run_collection(403,ACCESS)
        self.assertEqual((repository.claim_calls,calls),(1,{'metadata':1,'counters':1}))
        self.assertEqual((summary['stopped_reason'],summary['batch_unavailable'],
                          summary['batch_quota_exhausted'],summary['quota_exhausted']),
                         ('batch_unavailable',True,False,False))
        self.assertEqual({row['error_type'] for row in repository.failed},{'youtube_batch_request_error'})
        self.assert_preserved_general_profiles(summary,repository)

class OuterHandlerTests(unittest.TestCase):
    """The stop reaches every wrapper that decides whether to send more requests."""
    def summaries(self):
        return {name:run_collection(*response)[0] for name,response in (('quota',(403,QUOTA)),('other',(403,ACCESS)))}

    def test_sync_wrappers_stop(self):
        copy_stop=copy_collection_stop
        for name,summary in self.summaries().items():
            target={}
            self.assertTrue(copy_stop(target,summary),name)
            self.assertEqual(target['stopped_reason'],summary['stopped_reason'])
        target={}
        self.assertTrue(copy_stop(target,{'batch_unavailable':True}))
        self.assertEqual((target['stopped_reason'],target['quota_exhausted']),('batch_unavailable',False))

    def test_operator_drain_stops(self):
        summaries=self.summaries()
        self.assertEqual(operator_collection_stop_reason(summaries['quota']),'quota_exhausted')
        self.assertEqual(operator_collection_stop_reason(summaries['other']),'batch_unavailable')

    def test_outcome_is_an_error(self):
        self.assertEqual(sync_completion_fields({'batch_unavailable':True})['completion_outcome'],'error')

    def test_operator_hydration_chunk_reports_the_stop_to_the_drain(self):
        # A chunk holds at most 50 targets; the drain decides on the next chunk from these flags.
        repository,calls=InitialRepository(),{'metadata':0,'counters':0,'initial':0}
        service=collection_service(repository,403,ACCESS,calls)
        def fetch_initial(ids,phase):
            calls['initial']+=1
            return fetch_initial_video_profiles(ids,phase,
                youtube_api_get=lambda *a,**kw:Response(403,ACCESS),api_key=None,
                attempt_recorder=None,timeout_seconds=17,emit_event=lambda *a,**kw:None,
                perf_counter=lambda:0.0)
        service.fetch_initial_profiles=fetch_initial
        combined=run_claimed_initial_video_collection(service,job_id='job',channel_id='UC'+'c'*22,
                                                      video_ids=[f'vid{i:08d}' for i in range(50)],
                                                      worker_id='worker',run_id='run')
        self.assertEqual(calls,{'metadata':0,'counters':0,'initial':1})
        self.assertEqual((combined['stopped_reason'],combined['batch_unavailable'],operator_collection_stop_reason(combined)),
                         ('initial_provider_failed',False,'infrastructure_error'))
        self.assertEqual(len(repository.failed),50)
        self.assertEqual(combined['metadata_requests'],1)
        self.assertEqual(combined['batch_requests'],0)

if __name__=='__main__':unittest.main()
