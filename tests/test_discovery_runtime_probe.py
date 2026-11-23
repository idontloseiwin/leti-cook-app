"""Read-only admission advice cannot activate or refill discovery work."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.discovery_runtime import runtime_status, runtime_enabled, _source_due
from discovery_policy import DiscoverySettings
from collection_schedule import CollectionSchedules


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.clock = dict(now=self.now, day=self.now.date(), reset=self.now + timedelta(hours=12))
        self.tx = Mock()
        self.operations = []
        @contextmanager
        def transaction(operation):
            self.operations.append(operation)
            yield self.tx
        self.database = Mock(transaction=transaction)
        self.database.cadence_snapshot = CollectionSchedules(discovery=DiscoverySettings()).cadence_snapshot()
        self.options = dict(project_id='test', settings=DiscoverySettings(), interval_minutes=30)

    def probe(self, *, events=None, owner=None, source=None, hydration=False, budget=None):
        self.tx.fetch_one.side_effect = [dict(enabled=True), self.clock, budget]
        with patch('db.discovery_runtime._event_probe', side_effect=[(events, owner), (None, None)]), \
             patch('db.discovery_runtime._hydration_due', return_value=hydration), \
             patch('db.discovery_runtime._source_due', return_value=source):
            result = runtime_status(self.database, **self.options)
        self.tx.execute.assert_not_called()
        self.assertTrue(all(operation.read_only for operation in self.operations))
        return result

    def budget(self, **changes):
        return dict(blocked_until=None, event_used=0, event_limit=2500,
            full_used=0, full_limit=1000, window_end=self.now + timedelta(minutes=15),
            window_event_used=0, window_event_limit=100, window_full_used=0, window_full_limit=25,
            **changes)

    def test_old_profile_never_touches_database(self):
        result = runtime_status(self.database, **{**self.options, 'settings': None})
        self.assertEqual(result, dict(enabled=False, runnable=False, next_retry=None, budget_wait=False))
        self.assertEqual(self.operations, [])

    def test_disabled_database_stops_before_demand_probe(self):
        self.tx.fetch_one.return_value = dict(enabled=False)
        result = runtime_status(self.database, **self.options)
        self.assertFalse(result['runnable'])
        self.tx.fetch_one.assert_called_once()

    def test_active_probe_is_read_only(self):
        self.tx.fetch_one.return_value = dict(enabled=True)
        self.assertTrue(runtime_enabled(self.database))
        self.tx.execute.assert_not_called()
        self.assertTrue(self.operations[0].read_only)

    def test_fresh_event_or_durable_tail_can_launch_without_wake(self):
        self.assertTrue(self.probe(events=dict(next_try_at=self.now))['runnable'])
        self.assertTrue(self.probe(hydration=True)['runnable'])

    def test_spent_window_waits_until_persisted_reset_and_does_not_debit(self):
        budget = self.budget()
        budget['window_event_used'] = budget['window_event_limit']
        result = self.probe(events=dict(next_try_at=self.now), budget=budget)
        self.assertEqual(result, dict(enabled=True, runnable=False,
            next_retry=budget['window_end'], budget_wait=True))

    def test_writer_receipt_recovery_remains_runnable_without_provider_allowance(self):
        budget = self.budget()
        budget['event_used'] = budget['event_limit']
        result = self.probe(owner=dict(next_try_at=self.now), budget=budget)
        self.assertTrue(result['runnable'])
        self.assertFalse(result['budget_wait'])

    def test_full_budget_blocks_full_source_but_not_a_fresh_event(self):
        budget = self.budget()
        budget['window_full_used'] = budget['window_full_limit']
        self.assertFalse(self.probe(source=dict(full_required=True,event_due=True), budget=budget)['runnable'])
        self.assertTrue(self.probe(source=dict(full_required=True,event_due=True), budget=budget,
                                  events=dict(next_try_at=self.now))['runnable'])

    def test_manual_full_walk_needs_only_full_allowance_without_an_event(self):
        budget = self.budget()
        budget['event_used'] = budget['event_limit']
        self.assertTrue(self.probe(source=dict(full_required=True,event_due=False),budget=budget)['runnable'])
        self.assertFalse(self.probe(source=dict(full_required=True,event_due=True),budget=budget)['runnable'])

    def test_sleeping_event_does_not_suppress_new_wake_until_its_retry(self):
        result = self.probe(events=dict(next_try_at=self.now + timedelta(hours=6)))
        self.assertFalse(result['runnable'])
        self.assertFalse(result['budget_wait'])

    def test_due_count_head_remains_runnable_when_full_capacity_is_spent(self):
        budget=self.budget()
        budget['full_used']=budget['full_limit']
        source=dict(full_required=True,head_due=True,head_signal=True)
        self.assertTrue(self.probe(source=source,budget=budget)['runnable'])
        budget['event_used']=budget['event_limit']
        self.assertFalse(self.probe(source=source,budget=budget)['runnable'])

    def test_scheduled_head_does_not_inherit_event_debit_from_full_debt(self):
        budget=self.budget()
        budget['full_used']=budget['full_limit']
        budget['event_used']=budget['event_limit']
        self.assertTrue(self.probe(source=dict(full_required=True,head_due=True,head_signal=False),budget=budget)['runnable'])

    def test_advisory_source_order_prefers_a_head_needing_fewer_allowances(self):
        _source_due(self.tx,('long','short'),self.database.cadence_snapshot.inventory_by_type)
        statement=self.tx.fetch_one.call_args.args[0]
        self.assertIn('ORDER BY head_due DESC,head_signal,full_required,',statement)


if __name__ == '__main__':
    unittest.main()
