"""Actual runtime binding through provider source, feeder and Coordinator."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import collection_shutdown
from collection.dispatch import Coordinator
from hot_storage import HotSourceReadExpired
from runtime.collection import bind_reconnect_feeder
from test_reconnect_feeder import CandidateRepository, identity
from test_reconnect_dispatch import CHANNEL, IN_TRANSACTION, ID
from test_user_work_reconnect_queue import Database
from db.user_work import user_work_reconnect as queue


class ReconnectRuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        handoff = patch.object(collection_shutdown, '_requested', Event())
        handoff.start()
        self.addCleanup(handoff.stop)
        self.owner = get_ident()
        self.calls = []
        self.repository = CandidateRepository(1, {identity(1)})
        self.repository.claim['user_input'] = CHANNEL

    def request(self, resource, **options):
        self.assertNotEqual(get_ident(), self.owner)
        self.assertFalse(IN_TRANSACTION.get())
        self.calls.append((resource, options))
        payload = ({'items': [{'id': CHANNEL, 'snippet': {'title': 'Synthetic channel'},
                              'statistics': {'videoCount': '1'}}]} if resource == 'channels'
                   else {'pageInfo': {'totalResults': 1}})
        return SimpleNamespace(status_code=200, headers={}, json=lambda: payload)

    def feeder(self):
        from db.user_work import user_work_backup
        with patch.object(user_work_backup, 'UserWorkBackupRepository', return_value=self.repository):
            return bind_reconnect_feeder(object(), request=self.request,
                require_ready=lambda: None)

    def test_real_binding_keeps_database_on_owner_and_source_time_unchanged(self):
        feeder = self.feeder()
        with ThreadPoolExecutor(max_workers=2) as executor:
            coordinator = Coordinator(executor, 2)
            feeder.offer_to(coordinator)
            for _ in range(30):
                turn = coordinator.step()
                if turn.snapshot.held == 0 and turn.snapshot.ready == 0:
                    break
                coordinator.wait(0.05)
            self.assertEqual(coordinator.snapshot().held, 0)
        bindings = [args for name, args, _ in self.repository.events if name == 'bind']
        self.assertEqual(len(bindings), 1)
        row = bindings[0][3]
        self.assertEqual(row['channel_id'], CHANNEL)
        self.assertEqual(row['channel_metadata_checked_at'], row['added_at'])
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(options['quota_bucket'] == 'general' for _, options in self.calls))
        self.assertTrue(all(options['max_response_bytes'] > 0 and options['deadline'] > 0
                            for _, options in self.calls))

    def test_manual_admission_unavailable_is_not_loaded_by_runtime_binding(self):
        from db.user_work.reconnect_admission import ChannelAddAdmissionUnavailable
        with patch('db.user_work.reconnect_admission.configured_channel_add_admission',
                   side_effect=ChannelAddAdmissionUnavailable()) as manual:
            self.test_real_binding_keeps_database_on_owner_and_source_time_unchanged()
        manual.assert_not_called()

    def test_provider_quota_persists_wait_and_new_runtime_resumes_at_pacific_reset(self):
        # Real source, feeder, unit, facade and queue SQL owners; only PostgreSQL
        # execution/provider responses and clocks are doubles. The persisted row
        # survives recreation of every runtime object, never an HTTP retry.
        class QueueDatabase(Database):
            def __init__(self):
                super().__init__()
                self.now = datetime(2026, 10, 11, 6, 59, tzinfo=timezone.utc)
                self.requested = self.now - timedelta(hours=1)
                self.not_before = self.last_attempt = None
                self.state, self.reason, self.attempts = 'queued', None, 0

            def eligible(self):
                return self.state == 'queued' or (self.state == 'waiting' and self.not_before <= self.now)

            def fetch_one(self, statement, parameters=()):
                if statement == queue._PAGE_CLOCK:
                    self.calls.append((statement, parameters))
                    return {'at': self.now}
                return super().fetch_one(statement, parameters)

            def fetch_all(self, statement, parameters=(), *, max_rows):
                assert statement == queue._CANDIDATE_PAGE
                self.calls.append((statement, parameters))
                return [] if parameters['after_id'] else [dict(id=ID,
                    last_attempt_at=self.last_attempt, requested_at=self.requested,
                    eligible=self.eligible())]

            def returning_all(self, statement, parameters=(), *, max_rows):
                assert statement == queue._CLAIM
                self.calls.append((statement, parameters))
                if not self.eligible():
                    return []
                self.attempts += 1
                self.state, self.last_attempt, self.not_before = 'running', self.now, None
                token = str(UUID(int=self.attempts, version=4))
                self.fence.update(reconnect_state='running', lease_token=token)
                return [dict(id=ID, revision=1, lease_token=token, user_input=CHANNEL)]

            def returning_one(self, statement, parameters=()):
                assert statement == queue._RELEASE
                self.calls.append((statement, parameters))
                self.state, delay, repeat_delay, self.reason, *fence = parameters
                assert delay == repeat_delay and fence == [ID, 1, 1, self.fence['lease_token']]
                self.not_before = self.now + timedelta(seconds=delay)
                self.fence.update(reconnect_state=self.state, lease_token=None)
                return {'id': ID}

        database = QueueDatabase()
        outcomes, requests = [], []
        def provider(resource, **options):
            self.assertNotEqual(get_ident(), self.owner)
            requests.append(resource)
            if len(requests) == 1:
                return SimpleNamespace(status_code=403, headers={}, json=lambda: {
                    'error': {'errors': [{'reason': 'quotaExceeded'}]}})
            return self.request(resource, **options)

        def fresh_runtime_pass():
            feeder = bind_reconnect_feeder(database, request=provider,
                require_ready=lambda: None, on_settled=outcomes.append)
            feeder.options['utc_clock'] = lambda: database.now
            with ThreadPoolExecutor(max_workers=1) as executor:
                coordinator = Coordinator(executor, 1)
                feeder.offer_to(coordinator)
                for _ in range(30):
                    status = coordinator.step().snapshot
                    if status.held == 0 and status.ready == 0:
                        break
                    coordinator.wait(0.05)
                self.assertEqual((coordinator.snapshot().held, coordinator.snapshot().ready), (0, 0))

        with patch('db.user_work.reconnect_admission.configured_channel_add_admission',
                   side_effect=AssertionError('backup reconnect must not read manual admission')), \
             patch.object(queue, 'bind_user_channel_restore_item',
                          return_value={'outcome': 'ready', 'merged': True}) as bound:
            fresh_runtime_pass()
            self.assertEqual((database.state, database.reason, database.attempts),
                             ('waiting', 'provider_quota_exceeded', 1))
            self.assertEqual(database.not_before, datetime(2026, 10, 11, 7, tzinfo=timezone.utc))
            release = next(params for sql, params in database.calls if sql == queue._RELEASE)
            self.assertEqual(release[:4], ('waiting', 60, 60, 'provider_quota_exceeded'))
            self.assertIn('commit', database.ends)
            self.assertEqual(outcomes, [dict(outcome='ready', reason='provider_quota_exceeded',
                                            quota_exhausted=True, rate_limited=False)])
            database.now += timedelta(seconds=59)
            fresh_runtime_pass()
            self.assertEqual((requests, database.attempts), (['channels'], 1))
            bound.assert_not_called()
            database.now += timedelta(seconds=1)
            fresh_runtime_pass()
            self.assertEqual(database.attempts, 2)
            self.assertEqual(requests, ['channels', 'channels', 'playlistItems', 'playlistItems', 'playlistItems'])
            bound.assert_called_once()
            self.assertEqual(outcomes[-1], dict(outcome='ready', reason=None,
                                               quota_exhausted=False, rate_limited=False))
            self.assertEqual(sum(sql == queue._RELEASE for sql, _ in database.calls), 1)

    def test_waiting_does_not_renew_expired_or_future_source(self):
        guard = self.feeder().options['require_source']
        now = datetime.now(timezone.utc)
        for stamp in (now - timedelta(days=30), now + timedelta(days=1)):
            row = {'channel_metadata_checked_at': stamp.isoformat()}
            original = dict(row)
            with self.assertRaises(HotSourceReadExpired):
                guard(row)
            self.assertEqual(row, original)


if __name__ == '__main__':
    unittest.main()
