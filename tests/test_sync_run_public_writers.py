"""Public boundary preservation for the accepted Python sync-run writers."""
from pathlib import Path
import sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.runs.sync_runs import SyncRunRepository
from db.base.postgres import DatabaseUnavailable
from test_direct_pg_user_repositories import TransactionDatabase

class PublicWriterTests(unittest.TestCase):
 def setUp(self):self.enterContext(patch('deployment_profile.selected_deployment_profile',return_value='self-hosted'))
 def test_bad_envelope_or_empty_cutoff_never_opens_connection(self):
  db=TransactionDatabase();repo=SyncRunRepository(db)
  for invoke in (lambda:repo.start({}),lambda:repo.finish(None,{}),lambda:repo.update_progress(1,{'bad-name':1}),lambda:repo.recover_stale_running(' ',{})):
   with self.assertRaises(ValueError):invoke()
  self.assertEqual(db.transactions,[])
 def test_absent_identity_keeps_progress_noop_and_finish_insert(self):
  db=TransactionDatabase();repo=SyncRunRepository(db)
  for identity in (None,0,False):self.assertFalse(repo.update_progress(identity,{}))
  with patch('db.runs.sync_runs.finish_insert_sync_run',return_value=1) as insert:
   for identity in (None,0,False):self.assertTrue(repo.finish(identity,{'status':'error'}))
   self.assertEqual(insert.call_count,3)
   insert.assert_called_with(db.owner,{'status':'error'})
  self.assertEqual([op.name for op in db.transactions],['sync_run_finish_insert']*3)
 def test_unknown_ack_propagates_once_for_each_public_writer(self):
  error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
  entries=(('start_sync_run',lambda r:r.start({'status':'running'})),('update_sync_run',lambda r:r.finish(1,{'status':'error'})),('finish_insert_sync_run',lambda r:r.finish(None,{'status':'error'})),('recover_stale_sync_runs',lambda r:r.recover_stale_running('cutoff',{})))
  for name,invoke in entries:
   db=TransactionDatabase();repo=SyncRunRepository(db)
   with patch('db.runs.sync_runs.'+name,side_effect=error) as helper:
    with self.assertRaises(DatabaseUnavailable) as caught:invoke(repo)
    self.assertIs(caught.exception,error);self.assertEqual(helper.call_count,1)
   self.assertEqual(db.ends,['rollback']);self.assertEqual(len(db.transactions),1)
 def test_start_no_row_receipt_remains_none(self):
  db=TransactionDatabase()
  with patch('db.runs.sync_runs.start_sync_run',return_value=None):self.assertIsNone(SyncRunRepository(db).start({'status':'running'}))

if __name__=='__main__':unittest.main()
