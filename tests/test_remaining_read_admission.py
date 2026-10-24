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
    def test_saved_type_and_video_reasons_follow_their_own_clocks(self):
        query=analytics.SAVED_SOURCES_SQL
        self.assertLess(query.index('inventory.source_expires_at is null'),query.index("then 'type_unsupported'"))
        self.assertLess(query.index('pg_catalog.isfinite(current_row.checked_at)'),query.index("then 'source_unavailable'"))
        self.assertLess(query.index('pg_catalog.isfinite(current_row.checked_at)'),query.index("then 'source_unpublished'"))
        for name in ('video_inventory','video_inventory_memberships','video_inventory_type_resolution','channels_with_control','videos'):
            self.assertIn('readable.'+name+' ',query)
            self.assertNotIn('public.'+name+' ',query)

    def test_automatic_long_resolution_uses_expiring_proof_and_exact_long_membership(self):
        query=analytics.SAVED_SOURCES_SQL
        self.assertIn('resolution.manual_valid or resolution.automatic_valid or (',query)
        self.assertIn('least(inventory.source_expires_at,resolution.source_expires_at,',query)
        # Automatic policy has its own proof; it never becomes a manual override
        # permitting another membership type to stand in for the chosen Long.
        self.assertIn("and (resolution.manual_valid or membership.video_type = 'long')",query)
        self.assertIn("resolution.canonical_video_type = 'long'",query)
        self.assertIn('resolution.canonical_channel_id = inventory.channel_id',query)

    def test_user_decision_and_readable_observation_remain_separate(self):
        self.assertIn("case when c.video_id is null then null when c.state='resolved' then c.decision_type else i.video_type end",reviews.CASE_JSON)
        self.assertIn('readable.video_type_review_cases',reviews._GET)
        self.assertIn('readable.video_inventory',reviews._GET)
        self.assertIn('public.video_type_review_history',reviews._HISTORY)
        self.assertIn('readable.operator_jobs',jobs._LATEST)


if __name__=='__main__':unittest.main()
