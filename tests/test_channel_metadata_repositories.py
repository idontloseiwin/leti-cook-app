"""Named metadata repository cutover: direct writes, stable shapes, no replay."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import psycopg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.channel.channel_sync import ChannelSyncRepository
from db.base.postgres import DatabaseServerError,DatabaseUnavailable,PostgresConfig,PostgresDatabase
from test_channel_metadata_batch_transaction import A,B,SavepointConnection,Pool,DSN,Result,one,changed,row,ordered,held
from channel_workflow_fixtures import stage_reads, workflow_results, metadata_results


class MetadataRepositoryBoundaryTests(unittest.TestCase):
    def test_error_writers_keep_repository_normalization_and_named_shapes(self):
        database=Mock();database.call.side_effect=AssertionError('RPC write forbidden')
        repository=ChannelSyncRepository(database)
        recorded={'channel_id':A,'error_code':'invalid_response','error_at':'2026-10-06T01:00:00+00:00','error_count':2}
        with patch('db.channel.channel_sync.channel_metadata_batch.record_channel_initial_inventory_error',return_value=recorded) as record, \
             patch('db.channel.channel_sync.channel_metadata_batch.clear_channel_initial_inventory_error',return_value={'channel_id':A,'cleared':True}) as clear:
            self.assertEqual(repository.record_initial_inventory_error(' '+A+' ',' invalid_response ',expected_epoch=7),recorded)
            self.assertEqual(repository.clear_initial_inventory_error(' '+A+' ',expected_epoch=7),{'channel_id':A,'cleared':True})
        record.assert_called_once_with(database,A,'invalid_response',7);clear.assert_called_once_with(database,A,7)
        database.call.assert_not_called()

    def test_repository_rejects_missing_epoch_and_invalid_action_before_direct_dispatch(self):
        repository=ChannelSyncRepository(object())
        with patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch') as settle, \
             patch('db.channel.channel_sync.channel_metadata_batch.record_channel_initial_inventory_error') as record, \
             patch('db.channel.channel_sync.channel_metadata_batch.clear_channel_initial_inventory_error') as clear:
            for epoch in (None,0,True,'7'):
                with self.assertRaises(ValueError):repository.record_initial_inventory_error(A,'error',expected_epoch=epoch)
                with self.assertRaises(ValueError):repository.clear_initial_inventory_error(A,expected_epoch=epoch)
                with self.assertRaises(ValueError):repository.update_metadata_batch([row(epoch=epoch)])
            for action in (None,'accepted','',True):
                with self.assertRaises(ValueError):repository.update_metadata_batch([{**row(),'control_action':action}])
            for rows in ([row(),row()],[row()]*51):
                with self.assertRaises(ValueError):repository.update_metadata_batch(rows)
            settle.assert_not_called();record.assert_not_called();clear.assert_not_called()

    def test_batch_maps_plain_per_row_receipts_without_exposing_native_detail(self):
        database=Mock();database.call.side_effect=AssertionError('RPC write forbidden')
        inputs=[row(B,8),row()]
        with patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch',return_value=[
                {'channel_id':A,'status':'error','error_code':'23514'},{'channel_id':B,'status':'updated'}]) as settle:
            self.assertEqual(ChannelSyncRepository(database).update_metadata_batch(inputs),{A:'error',B:'updated'})
        settle.assert_called_once_with(database,inputs);database.call.assert_not_called()

    def test_batch_response_shape_checks_remain_strict(self):
        repository=ChannelSyncRepository(object())
        for response in (None,{},[],[{'channel_id':B,'status':'updated'}],[{'channel_id':A,'status':'unknown'}],
                         [{'channel_id':A,'status':'updated'},{'channel_id':A,'status':'updated'}]):
            with patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch',return_value=response),self.assertRaises(TypeError):
                repository.update_metadata_batch([row()])

    def test_empty_batch_skips_writes_but_readiness_still_calls_direct_helper_once(self):
        database=Mock();database.call.side_effect=AssertionError('retained RPC forbidden')
        repository=ChannelSyncRepository(database)
        with patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch') as settle, \
             patch('db.readiness.collection_preflight.assert_metadata_batch_ready',return_value=None) as ready:
            self.assertEqual(repository.update_metadata_batch([]),{})
            self.assertIsNone(repository.assert_metadata_batch_ready())
            ready.assert_called_once_with(database);settle.assert_not_called()
            denied=DatabaseServerError('database_operation_failed',code='42501')
            ready.reset_mock();ready.side_effect=denied
            with self.assertRaises(DatabaseServerError) as caught:repository.assert_metadata_batch_ready()
            self.assertIs(caught.exception,denied)
            ready.assert_called_once_with(database);settle.assert_not_called()
        database.call.assert_not_called()

    def test_running_mark_requires_every_captured_scope_to_update(self):
        repository=ChannelSyncRepository(object())
        with patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch',return_value=[{'channel_id':A,'status':'error'}]) as settle:
            with self.assertRaisesRegex(RuntimeError,'channel_metadata_mark_failed'):
                repository.mark_metadata_running([{'channel_id':A,'collection_control_epoch':7}],'2026-10-06T01:00:00Z')
        self.assertEqual(settle.call_args.args[1],[row(channel_metadata_status='running',channel_metadata_started_at='2026-10-06T01:00:00Z')])

    def test_readiness_failure_prevents_marks_and_provider_dispatch(self):
        from collection import channel_metadata
        database=Mock();database.call.side_effect=AssertionError('retained RPC forbidden')
        denied=DatabaseServerError('database_operation_failed',code='42501')
        provider=Mock();context=SimpleNamespace(database=database,sources=SimpleNamespace(fetch_channel_metadata_batch=provider))
        with patch.object(channel_metadata,'load_due_channel_metadata_channels',return_value=[{'channel_id':A,'collection_control_epoch':7}]), \
             patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch') as settle, \
             patch('db.readiness.collection_preflight.assert_metadata_batch_ready',side_effect=denied) as ready:
            with self.assertRaises(DatabaseServerError) as caught:channel_metadata.run_channel_metadata_sync_batch(context=context)
        self.assertIs(caught.exception,denied)
        ready.assert_called_once_with(database)
        settle.assert_not_called();provider.assert_not_called();database.call.assert_not_called()


class MetadataRepositoryTransactionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=SavepointConnection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        self.assertFalse(hasattr(self.database,'call'))
        self.repository=ChannelSyncRepository(self.database)

    def test_real_mark_uses_one_transaction_with_original_epoch_and_final_share(self):
        value=row(channel_metadata_status='running',channel_metadata_started_at='2026-10-06T01:00:00Z')
        self.connection.results.extend((ordered(value),*metadata_results(A),Result((held(),))))
        self.repository.mark_metadata_running([{'channel_id':A,'collection_control_epoch':7}],'2026-10-06T01:00:00Z')
        self.assertEqual(self.connection.queries[0][1][0].obj,[value])
        self.assertIn('FOR SHARE',self.connection.queries[-1][0])
        self.assertEqual((self.connection.commits,self.pool.borrowed,self.connection.savepoint_releases),(1,1,1));self.assertFalse(hasattr(self.database,'call'))

    def test_real_record_and_clear_return_only_after_their_commit(self):
        value={'channel_id':A,'error_code':'inventory_error','error_at':'2026-10-06T01:00:00+00:00','error_count':1}
        self.connection.results.extend((one(channel_id=A,error_code='inventory_error',epoch=7),*workflow_results('record_error',value=value),Result((held(),))))
        self.assertEqual(self.repository.record_initial_inventory_error(A,'inventory_error',expected_epoch=7),value)
        self.connection.results.extend((one(channel_id=A,epoch=7),one(channel_id=A),Result((held(),))))
        self.assertEqual(self.repository.clear_initial_inventory_error(A,expected_epoch=7),{'channel_id':A,'cleared':True})
        self.assertEqual((self.connection.commits,self.pool.borrowed),(2,2));self.assertFalse(hasattr(self.database,'call'))

    def test_stale_final_scope_propagates_and_rolls_back_without_rpc_fallback(self):
        self.connection.results.extend((ordered(row()),changed(),Result((held(epoch=8),))))
        with self.assertRaises(DatabaseServerError) as caught:self.repository.update_metadata_batch([row()])
        self.assertEqual((caught.exception.code,caught.exception.message),('P0001','channel_collection_scope_lost'))
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(0,1,1));self.assertFalse(hasattr(self.database,'call'))

    def test_unknown_commit_propagates_once_without_batch_replay(self):
        self.connection.results.extend((ordered(row()),changed(),Result((held(),))))
        self.connection.commit_error=psycopg.OperationalError('private commit detail')
        with self.assertRaises(DatabaseUnavailable) as caught:self.repository.update_metadata_batch([row()])
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual((len(self.connection.queries),self.pool.borrowed),(3,1));self.assertFalse(hasattr(self.database,'call'))


if __name__=='__main__':unittest.main()
