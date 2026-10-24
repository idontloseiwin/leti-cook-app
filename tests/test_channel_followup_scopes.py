"""Deterministic source-followup scope regressions; no network/database execution."""
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from collection import availability as availability_owner
from collection import channel_metadata,cycle
from db.inventory.typed_inventory import TypedInventoryRepository
from collection import inventory as inventory_service

CHANNEL='UC'+'a'*22
OTHER='UC'+'b'*22
NOW=datetime(2026,9,27,12,tzinfo=timezone.utc)

class ChannelFollowupScopeTests(unittest.TestCase):
 def setUp(self):
  self.addCleanup(patch.stopall)
  patch('socket.socket.connect',side_effect=AssertionError('network forbidden')).start()
  patch('sys.stdout',io.StringIO()).start()
  patch.dict(os.environ,{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}).start()

 def test_each_metadata_reactivation_uses_its_prepared_channel_epoch(self):
  channels=[{'channel_id':CHANNEL,'collection_control_epoch':7},{'channel_id':OTHER,'collection_control_epoch':19}]
  context=SimpleNamespace(database=object(),settings=SimpleNamespace(typed_playlist_inventory_enabled=False),
   enqueue_initial_inventory=Mock(return_value={"status":"queued"}),
   sources=SimpleNamespace(fetch_channel_metadata_batch=Mock(return_value=[{'id':CHANNEL},{'id':OTHER}]),
                           is_youtube_daily_quota_exhausted=lambda e:False))
  def prepared(channel,item,observed_at,*,context):
   yield from ()
   return dict(channel=channel,values={},video_count_changed=False,initial_inventory_completed=False,
               change_direction=None,discovery_prioritized=False,recheck_summary_status=None,
               recheck_error_message=None,run_initial_inventory=True)
  with patch.object(channel_metadata,'load_due_channel_metadata_channels',return_value=channels), \
       patch.object(channel_metadata,'ChannelSyncRepository',return_value=SimpleNamespace(assert_metadata_batch_ready=lambda:None,mark_metadata_running=Mock())), \
       patch.object(channel_metadata,'channel_metadata_update_steps',side_effect=prepared), \
       patch.object(channel_metadata,'_persist_channel_metadata_batch',side_effect=lambda rows,*a,**k:[None]*len(rows)), \
       patch.object(context,'enqueue_initial_inventory',return_value={'status':'queued'}) as initial, \
       patch.object(channel_metadata,'stop_at_boundary',return_value=False):
   result=channel_metadata.run_channel_metadata_sync_batch(context=context)
  self.assertEqual(result['errors'],0)
  self.assertEqual([(c.args[0],c.kwargs['expected_epoch']) for c in initial.call_args_list],[(CHANNEL,7),(OTHER,19)])
  self.assertTrue(all(set(c.kwargs)=={'expected_epoch'} for c in initial.call_args_list))

 def test_discovery_availability_child_reuses_original_claim_scope(self):
  runner=Mock(return_value={'video_ids':[],'missing':0,'errors':0})
  context=SimpleNamespace(video=SimpleNamespace(run_batch=runner))
  with patch.object(availability_owner,'should_trigger_availability_recheck',return_value=True), \
       patch.object(availability_owner,'load_availability_recheck_candidate_ids',return_value=['video']):
   availability_owner.run_channel_availability_recheck(CHANNEL,{},context=context,expected_epoch=7)
  self.assertEqual(runner.call_args.kwargs['channel_scopes'],[{'channel_id':CHANNEL,'control_epoch':7}])
  self.assertEqual(runner.call_args.kwargs['explicit_video_ids'],['video'])

 def test_invalid_discovery_scope_cannot_dispatch_followup(self):
  runner=Mock();context=SimpleNamespace(video=SimpleNamespace(run_batch=runner))
  with self.assertRaises(ValueError):
   availability_owner.run_channel_availability_recheck(CHANNEL,{},context=context,expected_epoch=True)
  runner.assert_not_called()

 def test_continuation_cannot_replace_original_epoch_in_returned_claim(self):
  common=dict(channel_id=CHANNEL,video_type='short',playlist_id='UUSH'+CHANNEL[2:],scan_kind='initial_inventory')
  first=SimpleNamespace(**common,lease_channel_epoch=7,lease_token='first',active_scan_id=None,resume_page_token=None)
  second=SimpleNamespace(**common,lease_channel_epoch=9,lease_token='second',active_scan_id='scan',resume_page_token='next')
  repository=Mock();repository.claim_pass.side_effect=[[first],[second]]
  provider=Mock()
  with patch.object(inventory_service,'run_typed_inventory_claim',return_value={'status':'running','scan_id':'scan','resume_page_token':'next','transition_id':'page-ack'}) as page, patch('db.hot.hot_collection_control.abort_pending_playlists') as release:
   result=inventory_service.run_typed_inventory_batch(worker_id="test-worker", repository=repository,fetch_page=provider,limit=2,prepare=False,now=lambda:NOW)
  self.assertEqual(page.call_count,1)
  self.assertEqual(result['errors'],1)
  release.assert_called_once_with(repository.database,[second])
  self.assertEqual(repository.claim_pass.call_args.kwargs['continuations'],[{'channel_id':CHANNEL,'video_type':'short','scan_id':'scan','control_epoch':7,'last_transition_id':'page-ack'}])
  provider.assert_not_called()

 def test_only_confirmed_count_increases_prioritize_their_original_scopes(self):
  for direction in ('increased','decreased'):
   with self.subTest(direction=direction):
    channels=[{'channel_id':CHANNEL,'collection_control_epoch':7},{'channel_id':OTHER,'collection_control_epoch':19}]
    context=SimpleNamespace(database=object(),settings=SimpleNamespace(typed_playlist_inventory_enabled=True),
     sources=SimpleNamespace(fetch_channel_metadata_batch=Mock(return_value=[{'id':CHANNEL},{'id':OTHER}]),
                             is_youtube_daily_quota_exhausted=lambda e:False))
    def prepared(channel,item,observed_at,*,context):
     yield from ()
     return dict(channel=channel,values={},video_count_changed=True,initial_inventory_completed=True,
                 change_direction=direction,discovery_prioritized=direction=='increased',recheck_summary_status=None,
                 recheck_error_message=None,run_initial_inventory=False)
    priority=Mock();priority.prioritize_channels.return_value=1
    with patch.object(channel_metadata,'load_due_channel_metadata_channels',return_value=channels), \
         patch.object(channel_metadata,'ChannelSyncRepository',return_value=SimpleNamespace(assert_metadata_batch_ready=lambda:None,mark_metadata_running=Mock())), \
         patch.object(channel_metadata,'TypedInventoryRepository',return_value=priority), \
         patch.object(channel_metadata,'channel_metadata_update_steps',side_effect=prepared), \
         patch.object(channel_metadata,'_persist_channel_metadata_batch',return_value=[None,'RuntimeError']), \
         patch.object(channel_metadata,'stop_at_boundary',return_value=False):
     result=channel_metadata.run_channel_metadata_sync_batch(context=context)
    expected=[{'channel_id':CHANNEL,'control_epoch':7}]
    self.assertEqual(result['errors'],1)
    self.assertEqual(result['video_count_changed_channel_scopes'],expected)
    self.assertEqual(result['video_count_'+direction+'_channel_scopes'],expected)
    if direction=='increased':
     priority.prioritize_channels.assert_called_once_with([CHANNEL],expected_channel_scopes=expected)
    else:
     priority.prioritize_channels.assert_not_called()
     self.assertEqual(result['typed_inventory_prioritized'],0)

 def test_priority_repository_passes_captured_epoch_without_reload(self):
  database=Mock(spec_set=['call','read_rows','read_row','write','write_rows'])
  scopes=[{'channel_id':CHANNEL,'control_epoch':7}]
  with patch('db.inventory.inventory_priority.prioritize_typed_inventory_channels',return_value=1) as prioritize:
   self.assertEqual(TypedInventoryRepository(database).prioritize_channels([CHANNEL],expected_channel_scopes=scopes),1)
   prioritize.assert_called_once_with(database,channel_ids=[CHANNEL],expected_channel_scopes=scopes)
   prioritize.reset_mock()
   with self.assertRaises(ValueError):TypedInventoryRepository(database).prioritize_channels([CHANNEL],expected_channel_scopes=[])
   prioritize.assert_not_called()
  database.call.assert_not_called()
  database.read_rows.assert_not_called();database.read_row.assert_not_called()
  database.write.assert_not_called();database.write_rows.assert_not_called()

 def test_cycle_count_decrease_does_not_dispatch_an_extra_availability_batch(self):
  scopes=[{'channel_id':CHANNEL,'control_epoch':7}]
  context=SimpleNamespace(policy=SimpleNamespace(short_video_collection_enabled=False,completed_stream_collection_enabled=False))
  with ExitStack() as stack:
   for name in ('validate_required_auto_sync_rpcs','recover_stale_running_statuses','run_video_stats_sync_batch','run_typed_playlist_inventory_batch'):
    stack.enter_context(patch.object(cycle,name,return_value={'errors':0,'results':[]}))
   stack.enter_context(patch.object(cycle,'run_channel_metadata_sync_batch',return_value={'errors':0,'video_count_decreased_channel_ids':[CHANNEL],'video_count_decreased_channel_scopes':scopes}))
   stack.enter_context(patch.object(cycle,'stop_at_boundary',return_value=False))
   stack.enter_context(patch.object(cycle,'update_sync_run_progress'))
   stack.enter_context(patch.object(cycle,'emit_phase'))
   loss=stack.enter_context(patch.object(availability_owner,'run_channel_long_availability_loss_batch',side_effect=AssertionError('count loss must not request videos')))
   discovery_recheck=stack.enter_context(patch.object(availability_owner,'run_channel_availability_recheck',side_effect=AssertionError('inventory must not request availability')))
   result=cycle.run_auto_sync_cycle(context=context,inventory_page_budget=12)
  self.assertEqual(result['errors'],0)
  self.assertEqual(result['long_availability_loss'],dict(triggered=0,processed=0,updated=0,unavailable_marked=0,errors=0,results=[]))
  loss.assert_not_called();discovery_recheck.assert_not_called()

 def test_loss_batch_and_video_dispatch_keep_supplied_scope(self):
  runner=Mock(return_value={'video_ids':[],'errors':0})
  context=SimpleNamespace(video=SimpleNamespace(run_batch=runner))
  scopes=[{'channel_id':CHANNEL,'control_epoch':7}]
  with patch.object(availability_owner,'load_initial_long_inventory_status',return_value='completed'), \
       patch.object(availability_owner,'load_long_availability_loss_candidate_ids',return_value=['video']), \
       patch.object(availability_owner,'load_video_availability_status_counts',return_value={}), \
       patch.object(availability_owner,'stop_at_boundary',return_value=False):
   result=availability_owner.run_channel_long_availability_loss_batch([CHANNEL],context=context,channel_scopes=scopes)
  self.assertEqual(result['errors'],0)
  self.assertEqual(runner.call_args.kwargs['channel_scopes'],scopes)
  runner.reset_mock()
  with self.assertRaises(ValueError):availability_owner.run_channel_long_availability_loss_batch([CHANNEL],context=context,channel_scopes=[])
  runner.assert_not_called()

 def test_supplied_parent_scope_cannot_be_ignored_by_due_mode(self):
  repository=Mock();provider=Mock()
  with self.assertRaises(ValueError):
   inventory_service.run_typed_inventory_batch(worker_id="test-worker", repository=repository,fetch_page=provider,limit=1,
    expected_channel_scopes=[{'channel_id':CHANNEL,'control_epoch':7}])
  repository.prepare_runtime.assert_not_called();repository.claim_pass.assert_not_called();provider.assert_not_called()

 def test_cycle_never_reenqueues_committed_inventory_after_captured_scope(self):
  first_ids=['first'+str(i) for i in range(1000)]
  scope_a={'channel_id':CHANNEL,'control_epoch':7};scope_b={'channel_id':OTHER,'control_epoch':19}
  discovery={'errors':0,'results':[{'channel_scope':scope_a,'new_video_ids':first_ids},
                                  {'channel_scope':scope_b,'new_video_ids':['last']}]}
  context=SimpleNamespace(database=object(),settings=SimpleNamespace(database_video_collection_enabled=True),
   policy=SimpleNamespace(short_video_collection_enabled=False,completed_stream_collection_enabled=False))
  repository=Mock();repository.enqueue_video_ids.side_effect=[1000,1]
  with ExitStack() as stack:
   for name in ('validate_required_auto_sync_rpcs','recover_stale_running_statuses','run_video_stats_sync_batch','run_channel_metadata_sync_batch'):
    stack.enter_context(patch.object(cycle,name,return_value={'errors':0,'results':[]}))
   def inventory(**kw):
    for row in discovery['results']:
     kw['on_page'](dict(row,channel_id=row['channel_scope']['channel_id'],video_type='long',status='running'))
    return {'errors':0}
   stack.enter_context(patch.object(cycle,'run_typed_playlist_inventory_batch',side_effect=inventory))
   stack.enter_context(patch.object(cycle,'VideoCollectionRepository',return_value=repository))
   stack.enter_context(patch.object(cycle,'stop_at_boundary',return_value=False))
   stack.enter_context(patch.object(cycle,'update_sync_run_progress'))
   stack.enter_context(patch.object(cycle,'emit_phase'))
   result=cycle.run_auto_sync_cycle(context=context,inventory_page_budget=12,new_video_stats_limit=1001)
  self.assertEqual(result['errors'],0)
  self.assertEqual(result['new_video_stats']['enqueued'],0)
  self.assertEqual(result['new_video_ids'],first_ids+['last'])
  self.assertEqual(result['new_video_stats_video_ids'],[])
  repository.enqueue_video_ids.assert_not_called()

if __name__=='__main__':unittest.main()
