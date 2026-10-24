"""A running process and its children keep one explicit immutable schedule."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from collection_schedule import CollectionSchedules, ScheduleError, serialize_snapshot
from runtime.collection_schedules import (
    SNAPSHOT_ENV, DIGEST_ENV, schedule_environment,
    backend_schedule_environment, load_process_schedules,
)
from self_hosted_collection import CollectionSupervisor
from self_hosted_settings import SETTINGS_ENV, preset
from db.base.postgres import PostgresConfig, PostgresDatabase, Transaction, DatabaseError


class ScheduleRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.default = CollectionSchedules()
        self.changed = replace(self.default,
            long_counters=replace(self.default.long_counters, age_0_1d_seconds=1800),
            short_inventory=replace(self.default.short_inventory, found_new_seconds=1800),
            stream_inventory=replace(self.default.stream_inventory, reconciliation_seconds=86400),
            rechecks=replace(self.default.rechecks, unavailable_long_max_seconds=21600, identity_seconds=86400))

    def test_backend_captures_file_once_and_child_never_rereads(self):
        with patch('runtime.collection_schedules.read_schedule_file', return_value=self.default) as read:
            captured = backend_schedule_environment({SETTINGS_ENV: '/etc/leti-cook/config.ini'})
        read.assert_called_once_with(Path('/etc/leti-cook/collection-schedules.ini'))
        with patch('runtime.collection_schedules.read_schedule_file', side_effect=AssertionError('child file read')):
            self.assertEqual(load_process_schedules(captured), self.default)
            self.assertEqual(load_process_schedules({**captured, SETTINGS_ENV: '/different/config.ini'}), self.default)
        with patch('runtime.collection_schedules.read_schedule_file', return_value=self.changed):
            restarted = backend_schedule_environment({SETTINGS_ENV: '/etc/leti-cook/config.ini', **captured})
        self.assertEqual(load_process_schedules(restarted), self.changed)
        self.assertEqual(load_process_schedules(captured), self.default)

    def test_invalid_restart_has_no_fallback_to_inherited_snapshot(self):
        captured = schedule_environment(self.default)
        with patch('runtime.collection_schedules.read_schedule_file', side_effect=ScheduleError('schedule_fields_invalid')):
            with self.assertRaisesRegex(ScheduleError, 'schedule_fields_invalid'):
                backend_schedule_environment({SETTINGS_ENV: '/etc/config.ini', **captured})
        self.assertEqual(load_process_schedules(captured), self.default)

    def test_missing_transfer_fails_even_with_readable_path(self):
        good = schedule_environment(self.default)
        with patch('runtime.collection_schedules.read_schedule_file', side_effect=AssertionError('fallback')):
            for env in ({}, {SNAPSHOT_ENV: good[SNAPSHOT_ENV]}, {DIGEST_ENV: good[DIGEST_ENV]},
                        {SETTINGS_ENV: '/etc/config.ini', 'LIC_COLLECTION_SUPERVISED': '1'}):
                with self.subTest(env_keys=sorted(env)), self.assertRaisesRegex(ScheduleError, 'schedule_snapshot_required'):
                    load_process_schedules(env)

    def test_nonscheduling_owner_can_omit_only_complete_snapshot(self):
        self.assertIsNone(load_process_schedules({}, required=False))
        good = schedule_environment(self.default)
        self.assertEqual(load_process_schedules(good, required=False), self.default)
        for env in ({SNAPSHOT_ENV: good[SNAPSHOT_ENV]}, {DIGEST_ENV: good[DIGEST_ENV]},
                    {**good, DIGEST_ENV: '0' * 64}):
            with self.subTest(keys=sorted(env)), self.assertRaises(ScheduleError):
                load_process_schedules(env, required=False)

    def test_collector_composition_rejects_optional_or_wrong_model(self):
        from runtime.collection_schedules import require_schedule_model
        self.assertIs(require_schedule_model(self.default), self.default)
        for value in (None, {}, 'default', Mock()):
            with self.subTest(type=type(value).__name__), self.assertRaises(ScheduleError):
                require_schedule_model(value)

    def test_changed_or_non_ascii_transfer_is_rejected(self):
        good = schedule_environment(self.default)
        with self.assertRaises(ScheduleError):
            load_process_schedules({**good, SNAPSHOT_ENV: serialize_snapshot(self.changed).decode('ascii')})
        with self.assertRaises(ScheduleError):
            load_process_schedules({**good, SNAPSHOT_ENV: 'é'})

    def test_backend_requires_explicit_settings_path(self):
        with self.assertRaisesRegex(ScheduleError, 'schedule_path_required'):
            backend_schedule_environment({})

    def test_supervisor_captured_snapshot_wins_over_later_environment(self):
        settings = dict(preset='standard', limits=preset('standard'), automatic_collection=True,
                        interval_minutes=30, manual_stats_quota_units=200, handoff_grace_seconds=120)
        value = dict(prepared=True, phase='ready', settings=settings)
        manager = SimpleNamespace(lock=threading.RLock(), store=SimpleNamespace(read=lambda: value))
        process = SimpleNamespace(stdin=None, poll=lambda: None)
        spawn = Mock(return_value=process)
        supervisor = CollectionSupervisor(manager, ready=lambda: None, schedules=self.default,
                                          spawn=spawn, clock=lambda: 1000, wall_clock=lambda: 1000)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                **schedule_environment(self.changed), 'OPERATOR_JOB_WAKE_PATH': str(Path(folder)/'wake')}):
            supervisor.tick()
        env = spawn.call_args.kwargs['env']
        self.assertEqual(load_process_schedules(env), self.default)
        self.assertEqual(env['LIC_COLLECTION_SUPERVISED'], '1')
        self.assertNotIn('YOUTUBE_API_KEY', env)
        self.assertEqual(supervisor.deadline, 2800)

    def test_rechecks_survive_child_handoff_and_database_transaction_capture(self):
        captured = schedule_environment(self.changed)
        with patch('runtime.collection_schedules.read_schedule_file', side_effect=AssertionError('config reread')):
            child = load_process_schedules(captured)
            snapshot = child.cadence_snapshot()
            database = PostgresDatabase(PostgresConfig('postgresql://authenticator:fixture@localhost:5432/db'),
                pool_factory=Mock(side_effect=AssertionError('pool opened')), cadence_snapshot=snapshot)
            tx = Transaction(Mock(), Mock(), cadence_snapshot=database.cadence_snapshot)
            self.assertIs(tx.cadence_snapshot.rechecks, child.rechecks)
            self.assertEqual(tx.cadence_snapshot.rechecks.unavailable_long_max_seconds, 21600)
            self.assertEqual(tx.cadence_snapshot.rechecks.identity_seconds, 86400)
            self.assertIs(tx.cadence_snapshot.inventory_by_type.short, child.short_inventory)
            self.assertEqual(tx.cadence_snapshot.inventory_by_type.short.found_new_seconds, 1800)
            self.assertEqual(tx.cadence_snapshot.inventory_by_type.stream.reconciliation_seconds, 86400)
            database.close()

    def test_database_holds_same_snapshot_without_opening_pool(self):
        snapshot = self.default.cadence_snapshot()
        factory = Mock(side_effect=AssertionError('pool opened'))
        database = PostgresDatabase(PostgresConfig('postgresql://authenticator:fixture@localhost:5432/db'),
                                    pool_factory=factory, cadence_snapshot=snapshot)
        self.assertIs(database.cadence_snapshot, snapshot)
        with self.assertRaises(AttributeError):
            database.cadence_snapshot = self.changed.cadence_snapshot()
        factory.assert_not_called()
        database.close()
        with self.assertRaises(DatabaseError):
            _ = database.cadence_snapshot

    def test_transaction_borrows_snapshot_and_enforces_lifetime(self):
        snapshot = self.default.cadence_snapshot()
        tx = Transaction(Mock(), Mock(), cadence_snapshot=snapshot)
        self.assertIs(tx.cadence_snapshot, snapshot)
        with self.assertRaises(AttributeError):
            tx.cadence_snapshot = self.changed.cadence_snapshot()
        tx._active = False
        with self.assertRaises(DatabaseError):
            _ = tx.cadence_snapshot


if __name__ == '__main__':
    unittest.main()
