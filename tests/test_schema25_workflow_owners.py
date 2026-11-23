"""Current Python counterparts to explicitly historical source-schema regressions."""
import ast,inspect,re,sys,unittest
from pathlib import Path
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.retention import deleted_channel_retention as deleted,retention_queries as tombs
from db.user_work import user_work_restore as restore,user_work_restore_bind as binding,user_work_read_queries as reads
from db.jobs import operator_job_chunk_settlement as settlement
from db.inventory import inventory_quota_deferral as deferred
from db.inventory import inventory_scan_steps as scans
from db.jobs import operator_job_inventory_failure as failure
from db.base.postgres import DatabaseError

class CurrentWorkflowOwners(unittest.TestCase):
 def test_import_keys_ignore_input_order_and_reject_extra_keys(self):
  allowed=[{'create_fingerprint','created_at'}]
  for value in ({'create_fingerprint':1,'created_at':2},{'created_at':2,'create_fingerprint':1}):
   restore._keys(value,allowed,'user_work_saved_view_invalid')
  for value in ({'created_at':1},{'created_at':1,'create_fingerprint':2,'private':3}):
   with self.assertRaisesRegex(DatabaseError,'user_work_saved_view_invalid'):restore._keys(value,allowed,'user_work_saved_view_invalid')
  with self.assertRaises(DatabaseError) as raised:restore._keys([],allowed,'user_work_saved_view_invalid')
  self.assertEqual(raised.exception.code,'22023')
  self.assertIn('ORDER BY k COLLATE "C"',binding._DATA_SHAPE)
 def test_export_keeps_hidden_channels_and_two_owner_link_types(self):
  source=Path(reads.__file__).read_text(encoding='utf-8')
  self.assertNotIn('is_hidden IS NOT TRUE',source)
  for term in ("'hidden_at',control.hidden_at","'channel_ref',coalesce(m.user_channel_ref,m.restore_owner_ref)","'channel_ref',coalesce(c.user_channel_ref,c.restore_owner_ref)"):
   self.assertEqual(source.count(term),2)
 def test_transfer_validates_native_owners_before_exact_two_writes(self):
  tx=Mock();tx.fetch_one.side_effect=[{'id':'old','owner':'new'},{'present':True}]
  binding._transfer(tx,'old','new',adopt=False)
  self.assertEqual(tx.fetch_one.call_count,2);self.assertEqual(tx.execute.call_count,2)
  for call,table in zip(tx.execute.call_args_list,('saved_collection_members','video_type_review_cases')):
   self.assertIn('UPDATE public.'+table+' SET user_channel_ref=%s::uuid,restore_owner_ref=NULL',call.args[0])
   self.assertEqual(call.args[1],('new','old'))
  tx=Mock();tx.fetch_one.side_effect=[{'id':'old','owner':'new'},{'present':False}]
  with self.assertRaisesRegex(DatabaseError,'user_work_adopt_invalid'):binding._transfer(tx,'old','new',adopt=False)
  tx.execute.assert_not_called()
 def test_bind_cycle_check_precedes_merge_and_preserves_visibility(self):
  source=inspect.getsource(binding._bind)
  self.assertLess(source.index('WITH RECURSIVE up(id)'),source.index('UPDATE public.channels SET folder_id'))
  merge=source[source.index('if channel is not None:'):source.index('data=None if channel_data')]
  self.assertNotIn('is_hidden=',merge)
  self.assertIn('parent_ref',merge);self.assertIn('_transfer(',merge)
 def test_deleted_owner_locks_and_live_lease_checks_are_native(self):
  self.assertIn('FOR UPDATE OF c NOWAIT',deleted._LOCK)
  self.assertIn('FOR UPDATE NOWAIT',deleted._PENDING_LOCK)
  self.assertNotIn('SKIP LOCKED',deleted._PAGE)
  for table in ('video_collection_state','channel_playlist_state','operator_jobs'):
   self.assertIn('FROM public.'+table+' WHERE channel_id=%(channel)s::text AND lease_token IS NOT NULL AND lease_expires_at>%(at)s::timestamptz',deleted._BUSY)
 def test_deleted_final_epoch_fence_is_outside_row_error_handlers(self):
  source=inspect.getsource(deleted.cleanup_hot_deleted_channels);tree=ast.parse(source)
  fence=next(n for n in ast.walk(tree) if isinstance(n,ast.For) and isinstance(n.target,ast.Name) and n.target.id=='scope')
  self.assertFalse(any(n.lineno<=fence.lineno<=n.end_lineno for n in ast.walk(tree) if isinstance(n,ast.Try)))
  self.assertLess(source.index('for scope in scopes:'),source.index('DELETE FROM public.channels'))
  for token in ('FOR SHARE','control_epoch IS NOT DISTINCT FROM','is_hidden AND hidden_at IS NOT NULL','hidden_at<='):
   self.assertIn(token,deleted._FENCE)
 def test_tombstone_rechecks_locked_clock_before_receipts_and_header(self):
  self.assertIn("interval '720 hours'",tombs._CLOCK)
  self.assertIn('FOR UPDATE NOWAIT',tombs._LOCK)
  for token in ('deleted_at IS NOT NULL','deleted_at<%s::timestamptz','deleted_at<=%s::timestamptz'):
   self.assertIn(token,tombs._LOCK)
  source=inspect.getsource(tombs.cleanup_deleted_saved_objects)
  self.assertLess(source.index("locked['expired'] is not True"),source.index('DELETE FROM public.saved_collection_commands'))
  self.assertLess(source.index('DELETE FROM public.saved_collection_commands'),source.index('DELETE FROM public.saved_objects'))
 def test_restore_captures_revision_floors_before_deleting_tombstones(self):
  source=inspect.getsource(restore._restore)
  self.assertLess(source.index('jsonb_object_agg'),source.index('DELETE FROM public.saved_objects'))
  self.assertIn('revision',source);self.assertIn('tombs',source)
  self.assertIn('WHERE deleted_at IS NULL',restore._EMPTY)
 def test_quota_refund_precedes_terminal_exhaustion_and_requires_newer_queue(self):
  source=Path(settlement.__file__).read_text(encoding='utf-8')
  self.assertIn('job_attempts=greatest(target.job_attempts-1,0)',settlement._REFUND)
  self.assertIn('state.updated_at>=target.updated_at',settlement._REFUND)
  self.assertIn("state.last_error_type='youtube_quota_exhausted'",settlement._REFUND)
  self.assertLess(source.index('tx.execute(_REFUND,args)'),source.index('tx.execute(_EXHAUSTED'))
  self.assertIn('youtube_quota_reset_at(',source)
 def test_inventory_quota_dedicated_owner_preserves_attempt_counters(self):
  self.assertEqual(scans._QUOTA_ERRORS,{'youtube_daily_quota_exceeded_error','youtube_quota_exhausted'})
  source=Path(deferred.__file__).read_text(encoding='utf-8')
  self.assertIn('youtube_quota_reset_at(',source)
  self.assertIn("receipt.receipt_scope='quota_defer'",source)
  self.assertNotRegex(source,r'(?i)\b(?:attempt_count|discovery_error_count)\s*=')
  self.assertIn('THEN 0 ELSE 1 END',failure._ERROR_COUNT)

if __name__=='__main__':unittest.main()