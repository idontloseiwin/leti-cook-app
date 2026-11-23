"""A restore item for a tracked channel merges into it; parent links cannot form a cycle. No database or provider."""
from pathlib import Path
import types
import unittest

from http_test_support import channel_http, cache_stub, jobs_stub
from unittest.mock import patch
from test_route_inventory import routes

ROOT=Path(__file__).resolve().parents[1]
SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')

class TrackedChannelMergeTests(unittest.TestCase):
    def test_current_schema_has_no_already_tracked_refusal(self):
        self.assertNotIn('channel_already_tracked',SCHEMA)

class TrackedChannelQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_tracked_channel_is_prepared_without_youtube(self):
        channel_id='UC'+'b'*22
        calls=[]
        existing={'channel_id':channel_id,'title':'Tracked','collection_permission':'manual_required',
                  'collection_control_epoch':7}
        before=dict(existing)
        def forbidden_work(*args, **kwargs):
            raise AssertionError('Tracked restore must not estimate, write, or start hydration')
        class Repository:
            def __init__(self,db):pass
            def load_channel_record(self,value):return existing if value==channel_id else None
            def insert_channel(self,row,*,permission):forbidden_work()
            def preserve_user_input(self,value,locator):forbidden_work()
        cache = cache_stub()
        cache.clear_frontend_read_cache = forbidden_work
        http = channel_http(cache=cache, jobs=jobs_stub(_prepare_channel_add_hydration=forbidden_work))
        url='https://www.youtube.com/channel/'+channel_id
        with patch('web.channels.ChannelCommandRepository', Repository), \
             patch('web.channels.ChannelTrashRepository', return_value=types.SimpleNamespace()), \
             patch('web.channels.youtube_api_get', side_effect=lambda *a, **k: calls.append(k) or None), \
             patch('web.channels.channel_sources.estimate_initial_inventory', side_effect=forbidden_work), \
             patch('web.channels.collection_api.channel_metadata_update_values', side_effect=forbidden_work):
            result = await http.resolve_user_work_channel(url, None)
        self.assertEqual(result,{'status':'prepared','channel':{'channel_id':channel_id}})
        self.assertEqual(calls,[])
        self.assertEqual(existing,before)

class ParentRouteTests(unittest.TestCase):
    def test_the_unused_parent_route_is_removed(self):
        self.assertNotIn(('POST','/channels/{channel_id}/parent'),routes())
        self.assertNotIn('assign_parent',(ROOT/'application/db/user_work/channel_organization.py').read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
