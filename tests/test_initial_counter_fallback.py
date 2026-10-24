"""Initial General/Batch handoff with actual service and atomic planner."""
from datetime import datetime, timezone
from threading import Barrier, Lock, get_ident
import copy
import unittest
from unittest.mock import Mock
from test_initial_video_parallel import (AtomicRepository, service, run,
    video, IN_TRANSACTION)
from youtube_errors import YouTubeDailyQuotaExceededError

class InitialCounterFallbackTests(unittest.TestCase):
    def rows(self, repository):
        return {row['video_id']: (command['operation'], row) for batch in repository.commits
                for command in batch for row in command['rows']}

    def test_only_missing_required_counts_fall_back_and_whole_batch_commits_once(self):
        ids=['complete','optional','missing','stream','live','absent']
        items=[video(v,ended=v=='stream') for v in ids if v!='absent']
        for item in items:
            if item['id'] in ('missing','stream','live'):item['statistics'].pop('viewCount')
            if item['id']=='optional':item['statistics'].pop('likeCount')
        original=copy.deepcopy(items)
        repository=AtomicRepository({'stream':('stream','stream_lifecycle'),'live':('stream','stream_lifecycle')})
        collector=service(repository,lambda ids,phase:items)
        def counters(ids,phase):
            self.assertNotEqual(get_ident(),repository.owner)
            self.assertFalse(IN_TRANSACTION.get())
            self.assertEqual(ids,['missing','stream'])
            return {'items':[{'id':v,'views':456,'like_count':None,'comment_count':2} for v in ids],
                    'summary':{'failed_ids':[]}}
        collector.fetch_counters=Mock(side_effect=counters)
        result=run(collector,ids)
        rows=self.rows(repository)
        self.assertEqual(items,original)
        self.assertEqual((result['updated'],result['batch_requests'],result['metadata_requests']),(4,1,1))
        self.assertEqual(len(repository.commits),1)
        self.assertEqual(rows['missing'][1]['views'],456)
        self.assertIsNone(rows['optional'][1]['like_count'])
        self.assertEqual(rows['live'][0],'complete_stream_lifecycle')
        self.assertEqual(rows['absent'][0],'complete_missing')
        collector.fetch_counters.assert_called_once()

    def assert_counterless_profile(self,operation,row,source,kind):
        self.assertEqual(operation,'complete_identity_refresh')
        self.assertTrue(row['counterless_profile'])
        self.assertFalse(row['missing'])
        self.assertEqual(row['video_type'],kind)
        self.assertEqual(row['video_id'],source['id'])
        self.assertEqual(row['channel_id'],source['snippet']['channelId'])
        self.assertEqual(row['title'],source['snippet']['title'])
        self.assertEqual(row['published_at'],source['snippet']['publishedAt'])
        self.assertEqual(row['duration'],source['contentDetails']['duration'])
        self.assertFalse({'views','like_count','comment_count','error_type','retry_after_seconds'} & row.keys())

    def test_both_sources_without_views_preserve_all_three_profiles_without_inventing_counts(self):
        ids=['long','short','stream']
        items=[video(v,ended=v=='stream') for v in ids]
        for item in items:item['statistics'].pop('viewCount')
        repository=AtomicRepository({'short':('short','hydrate'),'stream':('stream','hydrate')})
        collector=service(repository,lambda ids,phase:items)
        collector.fetch_counters=Mock(return_value={'items':[], 'summary':{'failed_ids':ids}})
        result=run(collector,ids)
        self.assertEqual(result['updated'],0)
        self.assertEqual(result['source_view_count_absent'],3)
        self.assertEqual((result['metadata_deferred'],result['unavailable_marked'],result['errors']),(3,0,0))
        rows=self.rows(repository)
        self.assertEqual(set(rows),set(ids))
        for item in items:
            self.assert_counterless_profile(*rows[item['id']],item,item['id'])
        self.assertEqual(len(repository.commits),1)

    def test_batch_failure_is_not_general_failure_and_preserves_complete_profiles(self):
        for exception,code,quota in [(YouTubeDailyQuotaExceededError('quota'),'youtube_quota_exhausted',True),
                                     (TimeoutError('private text'),'youtube_batch_request_error',False)]:
            with self.subTest(code=code):
                items=[video('good'),video('missing')];items[1]['statistics'].pop('viewCount')
                repository=AtomicRepository();collector=service(repository,lambda ids,phase:items)
                collector.fetch_counters=Mock(side_effect=exception)
                result=run(collector,['good','missing'])
                rows=self.rows(repository)
                self.assertEqual(repository.successful_ids(),{'good'})
                self.assert_counterless_profile(*rows['missing'],items[1],'long')
                self.assertEqual((result['metadata_deferred'],result['source_view_count_absent'],
                                  result['unavailable_marked']),(1,1,0))
                self.assertEqual(result['batch_unavailable'],not quota)
                self.assertEqual(result['stopped_reason'],'quota_exhausted' if quota else 'batch_unavailable')
                self.assertEqual(result['batch_quota_exhausted'],quota)
                self.assertFalse(result['general_quota_exhausted'])
                self.assertEqual(result['batch_requests'],1)
                self.assertNotIn('private text',str(result))
                self.assertEqual(len(repository.commits),1)

    def test_invalid_batch_identity_preserves_general_profile_without_false_unavailability(self):
        item=video('missing');item['statistics'].pop('viewCount')
        for response in ({'items':[{'id':'foreign','views':1}],'summary':{'failed_ids':[]}},
                         {'items':[],'summary':{'failed_ids':[]}},
                         {'items':[{'id':'missing','views':1}],'summary':{'failed_ids':['missing']}}):
            with self.subTest(response=response):
                repository=AtomicRepository();collector=service(repository,lambda ids,phase:[item])
                collector.fetch_counters=Mock(return_value=response)
                result=run(collector,['missing'])
                self.assert_counterless_profile(*self.rows(repository)['missing'],item,'long')
                self.assertEqual((result['updated'],result['metadata_deferred'],result['unavailable_marked']),(0,1,0))
                self.assertTrue(result['batch_unavailable'])
                self.assertEqual(result['stopped_reason'],'batch_unavailable')
                self.assertEqual((result['metadata_requests'],result['batch_requests']),(1,1))
                self.assertEqual(len(repository.commits),1)

    def test_fallback_stays_within_eight_threads_and_one_transaction_per_fifty(self):
        ids=[f'video{i:06d}' for i in range(400)]
        repository=AtomicRepository();barrier=Barrier(8);lock=Lock();active=peak=0
        def general(ids,phase):
            result=[video(v) for v in ids]
            for item in result:item['statistics'].pop('viewCount')
            return result
        def counters(ids,phase):
            nonlocal active,peak
            self.assertFalse(IN_TRANSACTION.get());self.assertEqual(len(ids),50)
            with lock:active+=1;peak=max(peak,active)
            barrier.wait(10)
            with lock:active-=1
            return {'items':[{'id':v,'views':1} for v in ids],'summary':{'failed_ids':[]}}
        collector=service(repository,general);collector.fetch_counters=counters
        result=run(collector,ids)
        self.assertEqual((peak,result['updated'],result['metadata_requests'],result['batch_requests']),(8,400,8,8))
        self.assertEqual(len(repository.commits),8)

if __name__=='__main__':unittest.main()
