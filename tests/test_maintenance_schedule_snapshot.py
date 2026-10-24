"""Maintenance captures policy; deleting incident history cannot reschedule video work."""
from dataclasses import replace
from unittest.mock import Mock, patch
import unittest
from collection_schedule import CollectionSchedules
from db.hot import hot_runtime as factory
from db.retention import deleted_channel_retention as cleanup
from db.videos import video_queue_initialization as queue
from db.base.postgres import PostgresConfig
from test_direct_pg_transactions import Connection, Pool, Result, DSN

CHANNEL = 'UC' + 'a'*22
VIDEO = 'aaaaaaaaaaa'
OWNER = 'e0000000-0000-4000-8000-000000000001'
AT = '2026-10-07T00:00:00+00:00'
FLOOR = '2026-09-09T00:00:00+00:00'


class MaintenanceScheduleTests(unittest.TestCase):
    def test_missing_or_invalid_model_cannot_open_pool(self):
        for snapshot in (None, object()):
            with self.subTest(snapshot=type(snapshot).__name__), patch('db.base.postgres._new_pool') as pool:
                with self.assertRaisesRegex(factory.RetentionError, 'maintenance_client_unavailable'):
                    factory.maintenance_repository(PostgresConfig(DSN), cadence_snapshot=snapshot)
                pool.assert_not_called()
        with patch('db.base.postgres._new_pool') as pool:
            with self.assertRaises(TypeError):
                factory.maintenance_repository(PostgresConfig(DSN))
            pool.assert_not_called()

    def test_open_orphan_incident_cleanup_retains_snapshot_without_rebuilding_queue(self):
        model = CollectionSchedules()
        model = replace(model, long_counters=replace(model.long_counters, age_0_1d_seconds=43))
        snapshot = model.cadence_snapshot()
        events=[];connection=Connection(events);pool=Pool(connection,events)
        def row(**value): return Result(rows=(value,))
        write=lambda count=0:Result(has_columns=False,rowcount=count)
        connection.results.extend([
            row(roles_valid=True,relations_valid=True,privileges_valid=True),
            row(target='deleted_channels',after_id='',limit=100,at=AT,cutoff=FLOOR),
            row(channel_id=CHANNEL),
            row(channel_id=CHANNEL,owner=OWNER,control_epoch=7,has_payload=False),
            row(busy=False), Result(),
            *[write() for _ in cleanup._CHILD_DELETES[:7]],
            row(old_text='{"video_id":"aaaaaaaaaaa","restored_at":null}',was_open=True),
            row(old_video_id=VIDEO,new_video_id=None,old_closed=False,new_closed=False,withdrawal=False),
            *[write() for _ in cleanup._CHILD_DELETES[8:]],
            row(remaining=False),write(),write(),row(remaining=False),
            row(channel_id=CHANNEL,valid=True),write(1),
        ])
        with patch('db.base.postgres._new_pool',return_value=pool), \
             patch('db.base.postgres._runtime_admission',side_effect=lambda name:None), \
             patch.object(cleanup,'drain_channel_memberships') as unrelated_cascade:
            repository=factory.maintenance_repository(PostgresConfig(DSN),cadence_snapshot=snapshot)
            try:
                self.assertIs(repository.database.cadence_snapshot,snapshot)
                result=repository.database.cleanup_hot_deleted_channels(target='deleted_channels',after_id='',limit=100)
            finally:repository.close()
        self.assertEqual((result['scanned'],result['deleted'],result['deferred'],result['complete']),(1,2,0,True))
        unrelated_cascade.assert_called_once()
        self.assertEqual(unrelated_cascade.call_args.args[1],[CHANNEL])
        self.assertFalse(any(queue._LONG_INPUT in sql or queue._LONG_UPSERT in sql
                             or queue._TYPED_INSERT in sql for sql,_ in connection.queries))
        # Retained availability episodes belong to video_collection_state;
        # deleting their historical incident must not reset their deadline.
        self.assertFalse(any('UPDATE public.video_collection_state' in sql for sql,_ in connection.queries))
        self.assertTrue(any(cleanup._INCIDENT_DELETE in sql for sql,_ in connection.queries))
        self.assertFalse(connection.results)
        self.assertEqual((pool.borrowed,len(pool.opens),len(pool.closes)),(1,1,1))
        self.assertEqual(connection.rollbacks,0)
