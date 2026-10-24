"""The public HOT control facade uses only finite closed driver methods."""
from pathlib import Path
from unittest.mock import Mock
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.hot.hot_collection_control import HotCollectionControlRepository
from db.base.postgres import DatabaseUnavailable
import hot_runtime
TOKEN='f9900000-0000-4000-8000-000000000001'
class HotConsumers(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['abort_hot_collection_claims','interrupt_hot_sync_run'])
  self.repo=HotCollectionControlRepository(self.db)
  self.video=dict(video_id='a'*11,video_type='long',lease_token=TOKEN)
 def receipt(self,count=1):
  return dict(status='aborted',video_requested=count,video_released=count,playlist_requested=0,playlist_released=0,operator_requested=0,operator_released=0)
 def test_abort_validates_then_dispatches_under_control_scope(self):
  def action(**kwargs):
   self.assertTrue(hot_runtime._COLLECTION_CONTROL.get());return self.receipt()
  self.db.abort_hot_collection_claims.side_effect=action
  self.assertEqual(self.repo.abort(videos=[self.video]),self.receipt())
  self.db.abort_hot_collection_claims.assert_called_once_with(video_claims=[self.video],playlist_claims=[],operator_claims=[])
  self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())
 def test_empty_or_invalid_input_never_dispatches(self):
  self.assertEqual(self.repo.abort(),self.receipt(0))
  for rows in ([self.video,self.video],[{**self.video,'lease_token':'bad'}],[{**self.video,'extra':1}], [self.video]*51):
   with self.assertRaises(ValueError):self.repo.abort(videos=rows)
  self.db.abort_hot_collection_claims.assert_not_called()
 def test_receipt_counts_are_strict(self):
  for changed in (dict(video_requested=True),dict(video_released=2),dict(status='completed')):
   self.db.abort_hot_collection_claims.return_value={**self.receipt(),**changed}
   with self.assertRaises(RuntimeError):self.repo.abort(videos=[self.video])
 def test_interrupt_validates_and_preserves_identity(self):
  def action(**kwargs):
   self.assertTrue(hot_runtime._COLLECTION_CONTROL.get());return dict(status='interrupted',matched=1)
  self.db.interrupt_hot_sync_run.side_effect=action
  stamp='2026-10-06T10:00:00Z'
  self.assertEqual(self.repo.interrupt_run(7,stamp),dict(status='interrupted',matched=1))
  self.db.interrupt_hot_sync_run.assert_called_once_with(run_id=7,started_at=stamp)
  self.db.interrupt_hot_sync_run.reset_mock()
  for args in ((True,stamp),(0,stamp),(7,'2026-10-06T10:00:00')):
   with self.assertRaises(ValueError):self.repo.interrupt_run(*args)
  self.db.interrupt_hot_sync_run.assert_not_called()
 def test_unknown_ack_is_single_dispatch_and_resets_scope(self):
  for name,call in (('abort_hot_collection_claims',lambda:self.repo.abort(videos=[self.video])),('interrupt_hot_sync_run',lambda:self.repo.interrupt_run(7,'2026-10-06T10:00:00Z'))):
   error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
   action=getattr(self.db,name);action.side_effect=error
   with self.assertRaises(DatabaseUnavailable) as caught:call()
   self.assertIs(caught.exception,error);action.assert_called_once()
   self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())
if __name__=='__main__':unittest.main()
