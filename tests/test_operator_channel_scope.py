"""Offline command admission/unknown-ACK regression tests, no external calls."""
from pathlib import Path
from datetime import datetime, timezone
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from collection import commands
from db.jobs.operator_jobs import OperatorJobRepository

class OperatorChannelScopeTests(unittest.TestCase):
    def setUp(self):
        self.network=patch('socket.socket.connect',side_effect=AssertionError('network forbidden'))
        self.network.start();self.addCleanup(self.network.stop)
        self.row={'id':'job-a','channel_id':'channel-a','status':'error','phase':'verify','accepted_epoch':3,'requested_at':datetime.now(timezone.utc).isoformat()}
        self.repo=Mock()

    def retry(self,**changes):
        options=dict(retry_request_id='00000000-0000-0000-0000-000000000001',expected_epoch=5,max_items=10,max_pages=10,max_quota_units=10)
        options.update(changes)
        return commands.retry_initial_hydration_job(self.repo,self.row,**options)

    def test_restored_completed_failure_skips_old_generation_cas(self):
        self.row.update(status='completed',error_count=1)
        self.repo.retry_initial_inventory.return_value={**self.row,'status':'queued','accepted_epoch':5}
        result=self.retry()
        self.repo.mark_completed_initial_hydration_failed.assert_not_called()
        self.assertEqual(self.repo.retry_initial_inventory.call_args.kwargs['expected_epoch'],5)
        self.assertEqual(result['accepted_epoch'],5)

    def test_unknown_ack_only_reconciles_same_accepted_epoch(self):
        self.repo.retry_initial_inventory.side_effect=TimeoutError('synthetic ack loss')
        self.repo.get_initial_inventory_job.return_value={**self.row,'status':'queued','accepted_epoch':5}
        self.assertEqual(self.retry()['accepted_epoch'],5)

    def test_unknown_ack_cannot_adopt_later_restore_generation(self):
        self.repo.retry_initial_inventory.side_effect=TimeoutError('synthetic ack loss')
        self.repo.get_initial_inventory_job.return_value={**self.row,'status':'queued','accepted_epoch':7}
        with self.assertRaises(TimeoutError):self.retry()

    def test_old_running_snapshot_is_not_an_ack_for_new_generation(self):
        self.row['status']='running'
        with self.assertRaisesRegex(RuntimeError,'channel_collection_scope_lost'):self.retry()
        self.repo.retry_initial_inventory.assert_not_called()

    def test_invalid_epoch_stops_before_repository(self):
        with self.assertRaises(ValueError):self.retry(expected_epoch=True)
        self.assertEqual(self.repo.mock_calls,[])

    def test_failure_normalization_uses_job_scope_not_current_channel(self):
        self.row.update(status='completed',error_count=2)
        self.repo.get_initial_inventory_job.return_value={**self.row,'status':'error'}
        commands.normalize_completed_initial_hydration_failure(self.repo,self.row)
        self.repo.mark_completed_initial_hydration_failed.assert_called_once_with('job-a','channel-a',error_count=2,expected_epoch=3)

    def test_repository_no_unscoped_failure_table_update(self):
        repository=OperatorJobRepository.__new__(OperatorJobRepository)
        repository._rpc=Mock(return_value=1)
        repository.mark_completed_initial_hydration_failed('job-a','channel-a',error_count=2,expected_epoch=3)
        repository._rpc.assert_called_once_with('mark_completed_initial_hydration_failed',{'p_job_id':'job-a','p_channel_id':'channel-a','p_error_count':2,'p_expected_epoch':3})

if __name__=='__main__':unittest.main()
