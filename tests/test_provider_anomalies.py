"""Provider details the collector used to handle coarsely (audit S7-14). No server, database or provider."""
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
import sys
import time
import ast
import unittest
from collections.abc import Mapping

from test_error_codes_not_text import module_parts

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from youtube_errors import YouTubeAPIError, is_youtube_daily_quota_exhausted
from youtube_batch_stats import is_batch_stats_quota_error
from youtube_video_profiles import fetch_video_metadata
from db.videos.video_collection import VideoCollectionClaim  # noqa: E402
from operator_jobs import _is_daily_quota_error  # noqa: E402
from scripts.export_youtube_category_reference import (ReferenceResponse, entity_tag, export_reference,  # noqa: E402
                                                       response_etag)
from video_collection_service import VideoCollectionService, _default_is_quota_exhausted  # noqa: E402

NOW=datetime(2026,9,26,12,tzinfo=timezone.utc)

class Result:
    def __init__(self,headers=None):self.headers=headers or {}

class EntityTagTests(unittest.TestCase):
    def test_tags_are_sent_bare(self):
        # YouTube answers 304 only to the bare body tag; quoted, it always gets 200.
        self.assertEqual(entity_tag('XI7nbFXulYBIpL0ayR_gDh3eu1k'),'XI7nbFXulYBIpL0ayR_gDh3eu1k')
        self.assertEqual(entity_tag('"quoted"'),'quoted')
        for value in (None,'','""',' spaced','in"side','W/"weak"','a'*1100,'a,b','"a", "b"'):
            self.assertIsNone(entity_tag(value),value)

    def test_the_header_wins_over_the_body(self):
        self.assertEqual(response_etag(Result({'ETag':'"header"'}),{'etag':'body'}),'header')
        self.assertEqual(response_etag(Result(),{'etag':'body'}),'body')
        self.assertEqual(response_etag(object(),{'etag':'body'}),'body')
        self.assertIsNone(response_etag(Result(),{}))

    def test_a_refresh_sends_the_stored_tag_and_reuses_the_cache(self):
        def first(endpoint,params,*,if_none_match=None):
            self.assertIsNone(if_none_match)
            if endpoint=='i18nLanguages':
                return ReferenceResponse({'items':[{'snippet':{'hl':'en','name':'English'}}]},'lang-tag')
            return ReferenceResponse({'items':[{'id':'10','snippet':{'title':'Music'}}]},'cat-tag')
        reference=export_reference(first,['10'],now=NOW)
        self.assertEqual(reference['validators']['languages'],'lang-tag')
        self.assertEqual(reference['validators']['categories'],{'en':'cat-tag'})

        sent=[]
        def second(endpoint,params,*,if_none_match=None):
            sent.append((endpoint,if_none_match))
            return ReferenceResponse(None)          # 304 Not Modified
        later=NOW+timedelta(hours=1)
        refreshed=export_reference(second,['10'],now=later,previous=reference,clock=lambda:later)
        self.assertEqual(sent,[('i18nLanguages','lang-tag'),('videoCategories','cat-tag')])
        self.assertEqual(refreshed['categories'],{'en':{'10':'Music'}})

    def test_a_reference_stored_with_bare_tags_still_sends_them(self):
        def first(endpoint,params,*,if_none_match=None):
            if endpoint=='i18nLanguages':
                return ReferenceResponse({'items':[{'snippet':{'hl':'en','name':'English'}}]})
            return ReferenceResponse({'items':[{'id':'10','snippet':{'title':'Music'}}]})
        reference=export_reference(first,['10'],now=NOW)
        reference['validators']={'category_ids':['10'],'languages':'old-lang','categories':{'en':'old-cat'}}
        sent=[]
        def second(endpoint,params,*,if_none_match=None):
            sent.append(if_none_match)
            return ReferenceResponse(None)
        later=NOW+timedelta(hours=1)
        export_reference(second,['10'],now=later,previous=reference,clock=lambda:later)
        self.assertEqual(sent,['old-lang','old-cat'])

    def test_a_reference_stored_with_quoted_tags_sends_them_bare(self):
        def first(endpoint,params,*,if_none_match=None):
            if endpoint=='i18nLanguages':
                return ReferenceResponse({'items':[{'snippet':{'hl':'en','name':'English'}}]})
            return ReferenceResponse({'items':[{'id':'10','snippet':{'title':'Music'}}]})
        reference=export_reference(first,['10'],now=NOW)
        reference['validators']={'category_ids':['10'],'languages':'"old-lang"','categories':{'en':'"old-cat"'}}
        sent=[]
        def second(endpoint,params,*,if_none_match=None):
            sent.append(if_none_match)
            return ReferenceResponse(None)
        later=NOW+timedelta(hours=1)
        export_reference(second,['10'],now=later,previous=reference,clock=lambda:later)
        self.assertEqual(sent,['old-lang','old-cat'])

class RateLimitTests(unittest.TestCase):
    REFUSALS=('Too many | reason=rateLimitExceeded','Too many | status=x | reason=userRateLimitExceeded')

    def test_daily_predicates_reject_rate_refusals(self):
        classifiers=(partial(is_youtube_daily_quota_exhausted,is_batch_stats_quota_error=is_batch_stats_quota_error),
                     _default_is_quota_exhausted,_is_daily_quota_error)
        for classify in classifiers:
            for text in self.REFUSALS:
                self.assertFalse(classify(YouTubeAPIError(text)),(classify,text))
            self.assertTrue(classify(YouTubeAPIError('Quota | reason=quotaExceeded')),classify)
            self.assertFalse(classify(YouTubeAPIError('Denied | reason=accessNotConfigured')),classify)

    def test_onboarding_reports_rate_as_temporary(self):
        from self_hosted_onboarding import provider_error_code
        from types import SimpleNamespace
        for status,reason,expected in ((403,'rateLimitExceeded','provider_unavailable'),
                                      (403,'userRateLimitExceeded','provider_unavailable'),
                                      (429,None,'provider_unavailable'),
                                      (403,'quotaExceeded','provider_quota_exceeded')):
            payload={'error':{'errors':[{'reason':reason}]}}
            self.assertEqual(provider_error_code(SimpleNamespace(status_code=status),payload),expected)


class Response:
    def __init__(self,items):self.items=items
    def json(self):return {'items':self.items}
    def raise_for_status(self):pass

def metadata_fetcher(items):
    namespace={'fetch_video_metadata':fetch_video_metadata,'Mapping':Mapping,'time':time,'YouTubeAPIError':YouTubeAPIError,'emit_event':lambda *a,**k:None,
               'youtube_api_get':lambda *a,**k:Response(items),'youtube_error_message':lambda *a:'error',
               'YOUTUBE_QUOTA_ATTEMPT_RECORDER':None,'API_KEY':None,'YOUTUBE_API_TIMEOUT_SECONDS':20}
    return module_parts(ROOT/'application/core.py',{'get_video_metadata'},namespace)['get_video_metadata']

class MetadataIdentityTests(unittest.TestCase):
    ITEMS=[{'id':'vid00000001','statistics':{'viewCount':'1'}},{'id':'vid00000001'},{'id':'unrequested1'},'junk']

    def test_other_callers_still_reject_the_whole_response(self):
        with self.assertRaises(YouTubeAPIError):
            metadata_fetcher(self.ITEMS)(['vid00000001','vid00000002'])

    def test_the_common_queue_gets_every_item(self):
        videos=metadata_fetcher(self.ITEMS)(['vid00000001','vid00000002'],reject_identity_anomalies=False)
        self.assertEqual([video if isinstance(video,str) else video['id'] for video in videos],
                         ['vid00000001','vid00000001','unrequested1','junk'])
        self.assertNotIn('statistics',videos[0])

    def test_the_common_queue_asks_for_per_item_handling(self):
        tree=ast.parse((ROOT/'application/runtime/collection.py').read_text(encoding='utf-8'))
        adapter=next(node for node in tree.body if isinstance(node,ast.FunctionDef)
                     and node.name=='video_collection_dependencies')
        calls=[node for node in ast.walk(adapter) if isinstance(node,ast.Call)
               and isinstance(node.func,ast.Name) and node.func.id=='get_video_metadata']
        self.assertEqual(len(calls),1)
        flags=[keyword.value for keyword in calls[0].keywords
               if keyword.arg=='reject_identity_anomalies']
        self.assertEqual(len(flags),1)
        self.assertIsInstance(flags[0],ast.Constant)
        self.assertIs(flags[0].value,False)

        # Actual process composition binds the raw owner to the same policy adapter.
        process_tree=ast.parse((ROOT/'application/runtime/process_collection.py').read_text(encoding='utf-8'))
        binding=next(node for node in process_tree.body if isinstance(node,ast.FunctionDef)
                     and node.name=='bind_process_collection')
        composition=[node for node in ast.walk(binding) if isinstance(node,ast.Call)
                     and isinstance(node.func,ast.Name) and node.func.id=='video_collection_dependencies']
        self.assertEqual(len(composition),1)
        providers=[keyword.value for keyword in composition[0].keywords if keyword.arg=='get_video_metadata']
        self.assertEqual(len(providers),1)
        self.assertIsInstance(providers[0],ast.Call)
        self.assertEqual(providers[0].func.id,'partial')
        self.assertEqual(providers[0].args[0].id,'fetch_video_metadata')

class Repository:
    def __init__(self):self.failed,self.missing=[],[]
    def fail_batch(self,rows):self.failed.extend(rows)
    def complete_missing(self,rows):self.missing.extend(rows)

def claims(*ids):
    return [VideoCollectionClaim(video_id=video_id,channel_id='UC'+'c'*22,collection_mode='metadata',
                                 lease_token='token-'+video_id,lease_expires_at='2026-09-26T13:00:00Z',
                                 next_due_at='2026-09-26T12:00:00Z') for video_id in ids]

def process(items,batch):
    repository=Repository()
    unused=lambda *a,**k:None
    service=VideoCollectionService(repository,fetch_full=unused,fetch_metadata=lambda ids,reason:items,
                                   fetch_counters=unused,build_full_payload=unused,build_counter_payload=unused)
    service._process_common_claims(batch,reason='probe')
    return {row['video_id']:row['error_type'] for row in repository.failed},repository

class PerItemSettlementTests(unittest.TestCase):
    def test_a_repeated_item_fails_only_its_video(self):
        failed,_=process([{'id':'vid00000001'},{'id':'vid00000001'},{'id':'vid00000002'}],
                         claims('vid00000001','vid00000002'))
        self.assertEqual(failed['vid00000001'],'duplicate_video_metadata_item')
        self.assertNotIn(failed.get('vid00000002'),{'duplicate_video_metadata_item','youtube_metadata_request_error'})

    def test_an_unrequested_item_never_turns_a_video_unavailable(self):
        failed,repository=process([{'id':'vid00000001'},{'id':'unrequested1'}],claims('vid00000001','vid00000002'))
        self.assertEqual(failed['vid00000002'],'malformed_youtube_metadata_response')
        self.assertEqual(repository.missing,[])

if __name__=='__main__':unittest.main()
