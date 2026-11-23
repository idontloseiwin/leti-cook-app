"""Expired mutation rows cannot leak into HTTP replies; no provider or DB."""
import asyncio
import threading
import types
import unittest
from unittest.mock import patch
from http_test_support import channel_http,endpoint,jobs_stub
from web.models import ChannelRequest
from db.reads import analytics_read_queries as analytics
from db.reviews import video_type_review_queries as reviews
from db.jobs import operator_job_read_queries as jobs


class MutationReadBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_every_channel_add_outcome_uses_the_reread_even_for_top_level_title(self):
        loop_thread=threading.get_ident()
        for status in ('added','exists','restored'):
            ack={'status':status,'title':'expired provider title',
                 'channel':{'channel_id':'UC1','title':'expired provider title','country_code':'GB'},
                 'hydration':{'ready':False,'job':{'summary_json':{'title':'expired provider title'}}}}
            threads=[]
            admitted={'channel_id':'UC1','title':None,'country_code':None}
            current_hydration={'ready':True,'job':None}
            def read_hydration(channel):
                self.assertEqual(channel,admitted)
                threads.append(threading.get_ident())
                return current_hydration
            def read(channel_id):
                self.assertEqual(channel_id,'UC1');threads.append(threading.get_ident())
                return [admitted]
            with patch('web.channels.channel_service.add_channel',return_value=ack) as write, \
                 patch('web.channels.FrontendRepository') as repository:
                repository.return_value.load_channel.side_effect=read
                http=channel_http(jobs=jobs_stub(_load_initial_hydration_state=read_hydration))
                result=await endpoint(http.add_router,'add_channel')(ChannelRequest(url='@test'),None)
            self.assertIsNone(result['title']);self.assertIsNone(result['channel']['title'])
            self.assertIsNone(result['channel']['country_code'])
            self.assertNotIn('expired provider title',repr(result))
            self.assertEqual(result['status'],status)
            self.assertEqual(result['hydration'],current_hydration)
            self.assertNotEqual(result['hydration'],ack['hydration'])
            write.assert_called_once()
            self.assertEqual(len(threads),2)
            self.assertEqual(threads[0],threads[1]);self.assertNotEqual(threads[0],loop_thread)

    async def test_deleted_between_write_and_read_never_falls_back_to_mutation_fields(self):
        with patch('web.channels.channel_service.add_channel',return_value={
            'status':'added','title':'old','channel':{'channel_id':'UC1','title':'old'}}), \
             patch('web.channels.FrontendRepository') as repository:
            repository.return_value.load_channel.return_value=[]
            result=await endpoint(channel_http().add_router,'add_channel')(ChannelRequest(url='@test'),None)
        self.assertIsNone(result['title']);self.assertEqual(result['channel']['channel_id'],'UC1')

    async def test_internal_restore_preparation_does_not_add_a_read_or_lose_fields(self):
        ack={'status':'prepared','channel':{'channel_id':'UC1','internal':'preserved'}}
        with patch('web.channels.channel_service.add_channel',return_value=ack), \
             patch('web.channels.FrontendRepository') as repository:
            result=await channel_http().resolve_user_work_channel('@test',None)
            repository.assert_not_called()
        self.assertIs(result,ack)


class SourceReasonOrdering(unittest.TestCase):
    """Saved collection members are judged by the shared collection scope."""
    def statement(self):
        from db.reads.research_sql import compile_collection_members
        return compile_collection_members('c0000000-0000-4000-8000-000000000001', ['a'*11], '2026-10-29T00:00:00Z')[0]

    def test_saved_member_reasons_follow_their_own_clocks(self):
        query=self.statement()
        # The owner binding, then the member's row under that owner, then identity
        # evidence precede every reason that depends on them.
        order=["THEN 'channel_hidden'","THEN 'inventory_missing' ELSE 'channel_unavailable'","THEN 'source_mismatch' ELSE 'inventory_missing'",
               'h.source_expires_at>a.evaluated_at',"THEN 'source_unpublished' WHEN h.video_id IS NOT NULL","THEN 'type_conflict'",
               "THEN 'source_missing'",'p.availability_checked_at>a.evaluated_at',"THEN 'source_unavailable'",
               "p.published_at<=a.evaluated_at),false) THEN 'source_unpublished'","THEN 'stream_incomplete'"]
        positions=[query.index(text) for text in order]
        self.assertEqual(positions,sorted(positions))
        for name in ('video_inventory','hot_video_inventory','video_inventory_type_resolution','videos','video_search_projection','channel_user_work'):
            self.assertIn('readable.'+name+' ',query)
        for name in ('video_inventory','video_inventory_memberships','videos','channels','channel_collection_state'):
            self.assertNotIn('public.'+name+' ',query)

    def test_member_admission_and_type_proof_are_the_shared_query_ones(self):
        from db.reads.research_sql import _STAGES_NOW, _SOURCE, _TYPE_JOIN, _IDENTITY_JOIN, _LIVE_JOIN
        query=self.statement()
        # The member read uses the shared stages as of now; pages fill {admission} per mode.
        for _,sql in _STAGES_NOW:self.assertIn(sql,query)
        self.assertIn('{admission}',_SOURCE)
        self.assertIn(_TYPE_JOIN,query);self.assertIn(_IDENTITY_JOIN,query)
        # A scope read gives a row the same type and identity proof with one lateral for rows read live
        # The same views and keys; identity only for the type it keeps.
        self.assertIn(_LIVE_JOIN,_SOURCE)
        for text in (_TYPE_JOIN,_IDENTITY_JOIN):self.assertNotIn(text,_SOURCE)
        for text in ('FROM readable.video_inventory x','WHERE NOT p.proof_current AND x.video_id=p.video_id LIMIT 1',
                     'CROSS JOIN LATERAL (SELECT CASE WHEN p.proof_current THEN p.inventory_type ELSE live.video_type END AS video_type) i',
                     'FROM readable.hot_video_inventory h0','h0.video_id=p.video_id AND h0.channel_id=p.channel_id'):
            self.assertIn(text,_TYPE_JOIN+_IDENTITY_JOIN);self.assertIn(text,_LIVE_JOIN)
        # No per-type rule: every format uses its own canonical proof.
        self.assertNotIn("'long'",query);self.assertNotIn('type_unsupported',query)

    def test_user_decision_and_readable_observation_remain_separate(self):
        self.assertIn("case when c.video_id is null then null when c.state='resolved' then c.decision_type else i.video_type end",reviews.CASE_JSON)
        self.assertIn('readable.video_type_review_cases',reviews._GET)
        self.assertIn('readable.video_inventory',reviews._GET)
        self.assertIn('public.video_type_review_history',reviews._HISTORY)
        self.assertIn('readable.operator_jobs',jobs._LATEST)


if __name__=='__main__':unittest.main()
