"""HTTP collection commands cannot borrow the local user-work admission gate."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from starlette.responses import Response
from db.base.read_context import http_read_scope, is_http_read
from hot_runtime import (HotRuntimeUnavailable, require_hot_runtime_ready,
                         runtime_request_access, runtime_collection_access)
from http_test_support import channel_http, endpoint, jobs_stub
from web.channels import _require_collection_ready
from web.models import ChannelRequest
from self_hosted_onboarding import SetupManager
from self_hosted_configuration import STARTER_CHANNEL_URLS
from self_hosted_settings import SettingsError

class CollectionHttpGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_collection_rejects_add_resolve_hydration_and_backfill_before_any_work(self):
        gate=Mock(side_effect=HotRuntimeUnavailable())
        http=channel_http(require_collection_ready=gate)
        with patch('web.channels.channel_service.add_channel') as command:
            with self.assertRaises(HotRuntimeUnavailable):
                await endpoint(http.add_router,'add_channel')(ChannelRequest(url='@test'),None)
            with self.assertRaises(HotRuntimeUnavailable):
                await endpoint(http.add_router,'add_starter_channel')(ChannelRequest(url=next(iter(STARTER_CHANNEL_URLS))),None)
            with self.assertRaises(HotRuntimeUnavailable):
                await http.resolve_user_work_channel('@test',None)
            with self.assertRaises(HotRuntimeUnavailable):
                endpoint(http.hydration_router,'enqueue_channel_hydration')('channel',None,None,Response())
            with self.assertRaises(HotRuntimeUnavailable):
                endpoint(http.backfill_router,'start_channel_backfill_current_stats')('channel',None)
            with self.assertRaises(HotRuntimeUnavailable):
                http.activate_restored_user_channel('channel',None)
            command.assert_not_called()
        self.assertEqual(gate.call_count,6)

    async def test_real_collection_gate_and_inner_mutation_scope_keep_reader_role_and_restore_api_read(self):
        events=[]
        owner=SimpleNamespace(require_collection_ready=lambda:events.append('collection'),
            require_ready=lambda:events.append('api_read'),require_database_ready=lambda:events.append('user_work'))
        identity='UC'+'a'*22
        def write(*args,**kwargs):
            self.assertTrue(is_http_read())
            require_hot_runtime_ready(operation='database')
            return {'status':'added','channel':{'channel_id':identity}}
        def read(*args,**kwargs):
            self.assertTrue(is_http_read())
            require_hot_runtime_ready(operation='database')
            return [{'channel_id':identity,'title':None}]
        with patch('hot_runtime.selected_deployment_profile',return_value='self-hosted'), \
             patch('hot_runtime.load_hot_admission_config',return_value=object()), \
             patch('hot_runtime._local_controller',owner), \
             patch('web.channels.channel_service.add_channel',side_effect=write), \
             patch('web.channels.FrontendRepository') as repository:
            repository.return_value.load_channel.side_effect=read
            http=channel_http(require_collection_ready=_require_collection_ready,
                              jobs=jobs_stub(_load_initial_hydration_state=lambda row:{}))
            with http_read_scope(),runtime_request_access('POST','/add-channel'):
                result=await endpoint(http.add_router,'add_channel')(ChannelRequest(url='@test'),None)
                require_hot_runtime_ready(operation='database')
            self.assertEqual(result['status'],'added')
            self.assertEqual(events,['collection','collection','api_read','api_read'])

    def test_setup_prepare_cannot_write_configuration_or_start_thread_when_paused(self):
        store=Mock();store.read.return_value={'phase':'ready','settings':{}}
        manager=SetupManager(store,apply=lambda value:None)
        with patch('hot_runtime.require_hot_runtime_ready',side_effect=HotRuntimeUnavailable()) as gate, \
             patch('self_hosted_onboarding.threading.Thread') as thread:
            with self.assertRaisesRegex(SettingsError,'maintenance_not_ready'):
                manager.prepare(1)
        gate.assert_called_once_with(operation='collection')
        thread.assert_not_called();store.update.assert_not_called()

    def test_nested_background_scope_restores_http_category_after_error(self):
        events=[]
        owner=SimpleNamespace(require_collection_ready=lambda:events.append('collection'),
            require_ready=lambda:events.append('api_read'),require_database_ready=lambda:events.append('user_work'))
        with patch('hot_runtime.selected_deployment_profile',return_value='self-hosted'), \
             patch('hot_runtime.load_hot_admission_config',return_value=object()), \
             patch('hot_runtime._local_controller',owner):
            with runtime_request_access('POST','/user-work/pending/local/resolve'):
                require_hot_runtime_ready(operation='database')
                with self.assertRaises(ValueError),runtime_collection_access():
                    require_hot_runtime_ready(operation='database')
                    raise ValueError('synthetic')
                require_hot_runtime_ready(operation='database')
            require_hot_runtime_ready(operation='database')
        self.assertEqual(events,['user_work','collection','user_work','collection'])

if __name__=='__main__':unittest.main()
