"""Installed binding with real phase feeders/Coordinator; repository/provider doubles."""
from dataclasses import replace
from threading import Event,get_ident
from types import SimpleNamespace
from unittest.mock import Mock,patch
import os
import unittest

import test_channel_metadata_steps as metadata_fixture
from test_channel_metadata_steps import CHANNEL,row
from test_common_operator_phase import DueRepository
from test_initial_video_parallel import service,video
from test_inventory_feeder import Repository as InventoryRepository
from test_shared_inventory_engine import page
from test_operator_shared_dispatch import Jobs
from operator_jobs import OperatorJobSettings
from collection import channel_metadata as metadata
from collection import metadata_dispatch
from collection.cycle import AutomaticDispatchWindow
from runtime import process_collection as runtime
from db.channel.channel_sync import MetadataCandidateCursor
from collection_shutdown import CURSOR_SNAPSHOT_ENV
import collection_shutdown


class AllPhaseBindingTests(unittest.TestCase):
    db=metadata_fixture.MetadataProviderStepsTests.db
    fetch=metadata_fixture.MetadataProviderStepsTests.fetch
    persist=metadata_fixture.MetadataProviderStepsTests.persist

    def setUp(self):
        metadata_fixture.MetadataProviderStepsTests.setUp(self)
        self.stack.enter_context(patch.object(collection_shutdown,'_requested',Event()))
        self.stack.enter_context(patch.dict(os.environ,{'LIC_YOUTUBE_REQUEST_CONCURRENCY':'2'}))
        self.stack.enter_context(patch.dict(os.environ,{CURSOR_SNAPSHOT_ENV:''}))
        os.environ.pop(CURSOR_SNAPSHOT_ENV)
        self.context.sources.parse_dt=lambda value:metadata_fixture.datetime.fromisoformat(value) if value else None
        self.context.settings.typed_playlist_inventory_enabled=True
        self.context.video_worker_id=lambda:'all-phase'
        self.context.enqueue_initial_inventory=Mock()
        self.video_repo=DueRepository(1)
        self.video_service=service(self.video_repo,Mock())
        def general(ids,reason):
            self.assertNotEqual(get_ident(),self.owner)
            self.events.append(('video',reason))
            return [video(value) for value in ids]
        self.video_service.fetch_metadata=general
        self.video_service.fetch_counters=lambda ids,reason:dict(items=[dict(id=value,views=2,like_count=1,comment_count=1) for value in ids],summary=dict(failed_ids=[]))
        self.context.video=SimpleNamespace(build_service=Mock(return_value=(self.video_repo,self.video_service)),run_batch=Mock(side_effect=AssertionError('inline provider')))
        self.jobs=Jobs([])
        self.jobs.schema_status=Mock(return_value=dict(ready=True,schema_version=7,initial_quota_limit_enforced=False))
        self.inventory=InventoryRepository([(CHANNEL,kind) for kind in ('long','short','stream')])
        self.inventory.prepare_runtime=Mock(return_value=dict(selected_channels=1))
        def playlist(*args,**options):
            self.assertNotEqual(get_ident(),self.owner)
            self.events.append(('playlist',args[0]))
            return page()
        self.context.sources.fetch_playlist_items_page=playlist
        self.context.sources.get_longform_playlist_id=lambda channel:"UULF"+channel[2:]
        ceiling='2026-10-10T00:00:00.000000+00:00'
        self.repository.metadata_candidate_page.side_effect=lambda **kw:dict(rows=[dict(channel_id=CHANNEL,control_epoch=7)] if kw['after'] is None else [],
            next_after=MetadataCandidateCursor(None,CHANNEL),due_ceiling=ceiling,
            inspected_count=int(kw['after'] is None),scan_exhausted=int(kw['after'] is None)<kw['limit'])
        self.repository.capture_metadata_candidates.return_value=[row()]
        self.stack.enter_context(patch.object(runtime,'ChannelSyncRepository',return_value=self.repository))
        self.stack.enter_context(patch.object(metadata_dispatch,'ChannelSyncRepository',return_value=self.repository))
        self.stack.enter_context(patch.object(runtime,'OperatorJobRepository',return_value=self.jobs))
        self.stack.enter_context(patch('db.TypedInventoryRepository',return_value=self.inventory))
        self.stack.enter_context(patch.object(runtime,'load_operator_job_settings',return_value=OperatorJobSettings(None,3,50,1200)))
        self.stack.enter_context(patch('hot_runtime.require_hot_runtime_ready'))
        self.stack.enter_context(patch('db.user_work.reconnect_admission.configured_channel_add_admission',side_effect=AssertionError('manual limiter used by reconnect')))
        self.stack.enter_context(patch('runtime.collection.bind_reconnect_feeder',return_value=SimpleNamespace(exhausted=True,offer_to=lambda *a,**k:0,begin_sweep=lambda:None)))
        self.stack.enter_context(patch('provider_runtime.request_scope',side_effect=lambda *a,**k:__import__('contextlib').nullcontext()))
        self.stack.enter_context(patch.dict('sys.modules',{'core':SimpleNamespace(API_KEY='synthetic',YOUTUBE_QUOTA_ATTEMPT_RECORDER=None)}))

    def test_all_ordered_phases_share_one_window_service_and_budget(self):
        bound=runtime.bind_process_common_dispatch(self.context,whole_cycle=True)
        with patch('collection.cycle.AutomaticDispatchWindow',wraps=AutomaticDispatchWindow) as windows:
            common=bound(limit=1,common_preflight_checked=True)
            first=bound.checkpoint()
            channels=bound.metadata(limit=1)
            observed=[]
            def on_page(result):
                observed.append(result)
            inventory=bound.inventory(limit=3,run_id=42,on_page=on_page)
            summary={'errors':0};bound.finish(summary)
            self.assertIsNotNone(bound.checkpoint())
            self.assertIsNone(first)
            self.assertEqual(windows.call_count,1)
        self.context.video.build_service.assert_called_once()
        self.assertEqual((common['updated'],channels['updated'],inventory['provider_attempts']),(1,1,3))
        self.assertEqual(inventory['prepared'],1)
        self.assertEqual({value['run_id'] for value in self.inventory.claims},{'42'})
        self.assertEqual({result['video_type'] for result in observed},{'long','short','stream'})
        self.assertFalse(hasattr(bound,'recheck'))
        self.assertFalse(hasattr(bound,'loss'))
        self.assertEqual(summary['operator']['chunks_processed'],0)
        self.assertEqual(len(self.jobs.pages),1)  # no per-phase operator reset

    def test_unknown_metadata_blocks_later_phase_dispatch_and_checkpoint(self):
        bound=runtime.bind_process_common_dispatch(self.context,whole_cycle=True)
        bound(limit=1,common_preflight_checked=True)
        self.repository.update_metadata_batch.side_effect=OSError('unknown')
        result=bound.metadata(limit=1)
        self.assertTrue(result['settlement_unknown'])
        summary={'errors':1,**result};bound.finish(summary)
        self.assertIsNone(bound.checkpoint())
        self.assertFalse(self.inventory.claims)
        self.repository.update_metadata_batch.assert_called_once()
