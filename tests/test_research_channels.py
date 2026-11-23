"""Channel grain reuses the same protected query and Saved contracts."""
import unittest,json
from dataclasses import replace
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from research_query.registry import CHANNEL_SOURCE_ID, source_field
from research_query.validation import validate,canonical,AccessContext,QueryRejected,capabilities
from research_query.saved import from_query,bound_query
from db.reads.research_sql import compile_query


def channel_query(**patch):
    return dict(query_version=1,source={'id':CHANNEL_SOURCE_ID},scope={'kind':'channel','id':'UC'+'A'*22},
        select=['channel.id','channel.title','channel.subscribers'],order_by=[{'field':'channel.subscribers','direction':'desc'}],**patch)

class ChannelContractTests(unittest.TestCase):
    def setUp(self):self.context=AccessContext('synthetic','owner',enabled=True)
    def test_grain_and_saved_roundtrip(self):
        q=channel_query();plan=validate(canonical(q),self.context)
        self.assertNotIn('video_types',json.loads(plan.encoded));self.assertNotIn('observations',json.loads(plan.encoded))
        self.assertEqual(plan.request.source.id,CHANNEL_SOURCE_ID)
        owner,definition=from_query(q);self.assertEqual(bound_query(definition,owner)['source'],q['source'])
        caps=capabilities(self.context,CHANNEL_SOURCE_ID)
        self.assertEqual(caps['grain'],'channel');self.assertEqual(caps['video_types'],[])
        self.assertEqual({f['id'] for f in caps['fields']},{'channel.id','channel.title','channel.avatar_url','channel.subscribers','channel.views','channel.video_count','channel.country_code','channel.created_at'})
        self.assertNotIn('channel.id',{f['id'] for f in capabilities(self.context)['fields']})
    def test_video_options_and_cross_grain_fields_refused(self):
        q=channel_query()
        for patch in ({'video_types':None},{'video_types':['long']},{'observations':{}},{'select':['video.views']},
                      {'scope':{'kind':'collection','id':'11111111-1111-4111-8111-111111111111'}}):
            with self.subTest(patch=patch),self.assertRaises(QueryRejected):validate(canonical({**q,**patch}),self.context)
    def test_source_dependencies_do_not_require_video_scan(self):
        context=replace(self.context,disabled_dependencies=frozenset({'membership','availability','publication','stream_completion'}))
        plan=validate(canonical(channel_query()),context);sql,params=compile_query(plan)
        self.assertNotIn('video_search_projection',sql);self.assertNotIn('video_inventory',sql)
        self.assertIn('readable.channels',sql);self.assertIn('channel_id COLLATE "C" ASC NULLS LAST',sql)
        self.assertEqual(source_field('channel.views',CHANNEL_SOURCE_ID).grain,'channel')
        with self.assertRaises(QueryRejected):validate(canonical(channel_query()),replace(context,disabled_dependencies=frozenset({'lifecycle'})))

if __name__=='__main__':unittest.main()
