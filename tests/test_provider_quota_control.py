"""Collector control: real transport boundary, synthetic storage/provider only."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
import os
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
import quota_buckets as quota
import youtube_client
from discovery_policy import DiscoverySettings
from discovery_admission import DiscoveryAdmissionDenied
from youtube_errors import YouTubeDailyQuotaExceededError
from youtube_pipeline_health import classify_health, read_provider_quota_control
from db.quota.provider_quota_control import ProviderQuotaControlRepository


class Storage:
    def __init__(self):
        self.project = 'primary'
        self.bans = set()
        self.read_error = self.write_error = False
        self.operations = []

    def snapshot(self):
        self.operations.append('read')
        if self.read_error:
            raise OSError('synthetic control outage')
        return dict(quota_day=quota.quota_day(), next_reset_at=datetime.now(timezone.utc)+timedelta(hours=1),
                    exhausted=tuple(self.bans))

    def note_exhausted(self, bucket, *, quota_day=None):
        self.operations.append('ban:'+bucket)
        if self.write_error:
            raise OSError('synthetic unknown write ACK')
        self.bans.add(bucket)


class ControlTests(unittest.TestCase):
    def setUp(self):
        quota.end_pass()
        self.addCleanup(quota.end_pass)
        self.storage = Storage()
        p = patch('db.quota.provider_quota_control.ProviderQuotaControlRepository', return_value=self.storage)
        p.start(); self.addCleanup(p.stop)
        mode = patch('youtube_pipeline_health.quota_control_schedule_mode', return_value='durable')
        mode.start(); self.addCleanup(mode.stop)

    def scope(self):
        return quota.controlled(object(), settings=DiscoverySettings(), interval_minutes=10)

    def test_persisted_ban_survives_new_scope_and_nested_passes(self):
        with self.scope():
            quota.before_request('general')
            quota.note_provider_refusal('general')
            quota.start_pass(); quota.end_pass()
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('general')
            quota.before_request('video_batch_get_stats')
        with self.scope():
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('general')
        self.assertEqual(self.storage.operations.count('ban:general'), 1)

    def test_unknown_read_has_no_http_or_quota_refusal_and_recovers(self):
        self.storage.read_error = True
        with patch('pipeline_trace.event') as events, self.scope():
            for _ in range(2):
                with self.assertRaises(quota.QuotaStateUnavailable) as caught: quota.before_request('general')
                self.assertFalse(caught.exception.lic_http_attempted)
                self.assertEqual(caught.exception.reason, 'quota_state_unavailable')
                self.assertEqual(quota.exhausted(), ())
            self.storage.read_error = False
            quota.before_request('general')
        self.assertEqual([call.args[1] for call in events.call_args_list], ['unknown', 'recovered'])
        self.assertGreaterEqual(events.call_args.kwargs['duration_ms'], 0)
        self.assertEqual(self.storage.bans, set())

    def test_write_failure_latches_only_affected_bucket_and_retries(self):
        self.storage.write_error = True
        with self.scope():
            quota.note_provider_refusal('general')
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('general')
            quota.before_request('stats')
            self.assertEqual(quota.exhausted(), ('general',))
            self.storage.write_error = False
            quota.before_request('stats')
        with self.scope():
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('general')

    def test_no_pre_http_ban_or_unbound_web_control(self):
        with self.scope(): quota.before_request('general')
        self.assertEqual(self.storage.bans, set())
        self.storage.read_error = True
        quota.before_request('general')  # explicit scope ended; web is out of scope

    def test_v4_explicit_legacy_read_does_not_create_new_control_or_budget(self):
        with patch('db.quota.youtube_quota.YouTubeQuotaRepository') as legacy:
            legacy.return_value.exhausted_buckets.return_value = ('stats',)
            self.assertFalse(quota.control_active())
            self.assertEqual(quota.read_remembered(object(), project_id='legacy-project'), ('stats',))
            self.assertEqual(legacy.call_args.kwargs['project_id'], 'legacy-project')
            self.assertEqual(self.storage.operations, [])

    def test_v5_never_falls_back_to_legacy_ledger_on_control_error(self):
        with patch('db.quota.youtube_quota.YouTubeQuotaRepository') as legacy, self.scope():
            self.assertTrue(quota.control_active())
            self.storage.bans.add('general')
            self.assertEqual(quota.read_remembered(object()), ('general',))
            self.storage.read_error = True
            with self.assertRaises(OSError): quota.read_remembered(object())
            legacy.assert_not_called()

    def test_bound_summary_observation_cannot_ban_a_new_day(self):
        today = quota.quota_day()
        with self.scope():
            quota.note_provider_refusal('general')
            self.storage.bans.clear()
            with patch('quota_buckets.quota_day', return_value=today+timedelta(days=1)):
                quota.start_pass()
                quota.note_exhausted('general')  # delayed old-day response/summary
                quota.before_request('general')
        self.assertEqual(self.storage.operations.count('ban:general'), 1)

    def test_refusal_latches_while_another_thread_is_waiting_for_storage(self):
        reading, release, latched = Event(), Event(), Event()
        original = self.storage.snapshot
        def slow_read():
            reading.set()
            if not release.wait(2): raise AssertionError('test did not release storage')
            return original()
        class NotifyingSet(set):
            def add(self, item):
                super().add(item)
                latched.set()
        results = []
        def read():
            try: quota.before_request('general')
            except Exception as error: results.append(error)
        with self.scope() as control:
            control.banned = NotifyingSet()
            self.storage.snapshot = slow_read
            reader = Thread(target=read)
            writer = Thread(target=lambda: quota.note_provider_refusal('general'))
            reader.start()
            self.assertTrue(reading.wait(1))
            writer.start()
            try:
                self.assertTrue(latched.wait(1))
                self.assertTrue(quota.is_exhausted('general'))
            finally:
                release.set()
                reader.join(2); writer.join(2)
            self.assertFalse(reader.is_alive() or writer.is_alive())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], YouTubeDailyQuotaExceededError)

    def test_day_rollover_drops_old_local_latch_without_new_day_write(self):
        today = quota.quota_day()
        with self.scope():
            quota.note_provider_refusal('general')
            self.storage.bans.clear()  # durable reader now selects the next day
            with patch('quota_buckets.quota_day', return_value=today+timedelta(days=1)):
                quota.before_request('general')
        self.assertEqual(self.storage.operations.count('ban:general'), 1)

    def request(self, *, status=200, reason=None, on_admit=None):
        response = Mock(status_code=status, is_redirect=False, headers={})
        response.json.return_value = ({'error': {'errors': [{'reason': reason}]}} if reason else {'items': []})
        def admitted(**kwargs):
            if on_admit: on_admit()
            return nullcontext(None)
        recorder = Mock()
        recorder.start_attempt.return_value = 'synthetic'
        recorder.finish_attempt.side_effect = lambda *a, **kw: self.storage.operations.append('journal') or True
        with patch('hot_runtime.require_hot_runtime_ready'), \
             patch('provider_runtime.admit_request', side_effect=admitted), \
             patch('youtube_client._read_bounded_response'), \
             patch('youtube_client.requests.get', return_value=response) as http:
            self.last_http = http
            result = youtube_client.youtube_api_get('videos', phase='test', quota_bucket='general',
                attempt_recorder=recorder, params={'part':'snippet','id':'video000001'})
        return result, http

    def test_observation_counts_wire_refusal_once_and_skips_remembered_ban(self):
        from list_observation import wire_cost
        with self.scope(), wire_cost() as first:
            result, http = self.request(status=403, reason='quotaExceeded')
            self.assertEqual((result.status_code, http.call_count), (403, 1))
            with wire_cost() as second:
                with self.assertRaises(YouTubeDailyQuotaExceededError):
                    self.request()
            self.assertEqual(second, dict(attempts=0, general=0, retries=0))
        self.assertEqual(first, dict(attempts=1, general=1, retries=0))

    def test_actual_refusal_is_durable_before_optional_journal(self):
        with self.scope():
            result, http = self.request(status=403, reason='quotaExceeded')
        self.assertEqual(result.status_code, 403)
        self.assertEqual(http.call_count, 1)
        self.assertLess(self.storage.operations.index('ban:general'), self.storage.operations.index('journal'))

    def test_gate_rechecks_ban_that_arrives_while_waiting_for_permit(self):
        with self.scope():
            with self.assertRaises(YouTubeDailyQuotaExceededError):
                self.request(on_admit=lambda: self.storage.bans.add('general'))
            self.last_http.assert_not_called()

    def test_grant_is_rechecked_after_permit_wait_without_another_debit(self):
        budget=Mock()
        budget.validate_dispatch.return_value=dict(allowed=True,reason='dispatchable')
        token=str(uuid4())
        def expire():
            budget.validate_dispatch.return_value=dict(allowed=False,reason='grant_expired')
        with self.scope(), quota.discovery_grant(budget,token):
            with self.assertRaises(DiscoveryAdmissionDenied) as caught:
                self.request(on_admit=expire)
            self.assertEqual(caught.exception.reason,'grant_expired')
            self.last_http.assert_not_called()
        budget.validate_dispatch.assert_called_once_with(token,bucket='general')
        budget.reserve.assert_not_called()
        budget.begin_dispatch.assert_not_called()

    def test_one_grant_cannot_send_second_http_in_one_callback(self):
        budget=Mock()
        budget.validate_dispatch.return_value=dict(allowed=True,reason='dispatchable')
        with self.scope(), quota.discovery_grant(budget,str(uuid4())):
            _,first=self.request()
            self.assertEqual(first.call_count,1)
            with self.assertRaises(DiscoveryAdmissionDenied) as caught: self.request()
            self.assertEqual(caught.exception.reason,'grant_already_dispatched')
            self.last_http.assert_not_called()
        self.assertEqual(budget.validate_dispatch.call_count,1)

    def test_unknown_grant_read_loses_its_allowance_and_new_grant_can_recover(self):
        budget=Mock()
        budget.validate_dispatch.side_effect=OSError('synthetic grant read outage')
        with self.scope():
            with quota.discovery_grant(budget,str(uuid4())):
                with self.assertRaises(quota.QuotaStateUnavailable): self.request()
                self.last_http.assert_not_called()
                budget.validate_dispatch.side_effect=None
                budget.validate_dispatch.return_value=dict(allowed=True,reason='dispatchable')
                with self.assertRaises(DiscoveryAdmissionDenied): self.request()
                self.last_http.assert_not_called()
            with quota.discovery_grant(budget,str(uuid4())):
                _,http=self.request()
                self.assertEqual(http.call_count,1)

    def test_429_5xx_and_access_refusal_do_not_create_daily_bans(self):
        for status, reason in ((429,'rateLimitExceeded'),(503,'backendError'),(403,'forbidden')):
            with self.subTest(status=status), self.scope(): self.request(status=status, reason=reason)
        self.assertEqual(self.storage.bans, set())

    def test_crash_before_confirmed_write_has_explicit_limited_guarantee(self):
        self.storage.write_error = True
        with self.scope(): quota.note_provider_refusal('general')
        self.storage.write_error = False
        with self.scope(): quota.before_request('general')
        self.assertEqual(self.storage.bans, set())

    def test_health_unknown_is_not_green_or_exhausted(self):
        self.storage.read_error = True
        control = read_provider_quota_control(object())
        self.assertEqual(control['reason'], 'quota_state_unavailable')
        self.assertEqual(control['exhausted_buckets'], {'general':None,'video_batch_get_stats':None})
        status, critical, warnings = classify_health({'status':'completed','errors':0}, {}, {}, {}, {}, {}, [],
            False, 90, 120, last_sync_without_errors={'status':'completed'}, quota_control=control)
        self.assertEqual(status,'warning')
        self.assertFalse(critical)
        self.assertIn('Quota state is unavailable', warnings[0])
        self.storage.read_error = False
        self.assertEqual(read_provider_quota_control(object())['status'], 'ok')


class RepositoryTests(unittest.TestCase):
    def database(self):
        self.tx = Mock()
        self.now = datetime(2026, 11, 1, 7, tzinfo=timezone.utc)
        self.clock = dict(now=self.now, day=self.now.date(), day_start=self.now,
                          reset=self.now+timedelta(hours=25))
        self.tx.fetch_one.return_value = self.clock
        self.database_mock = Mock()
        self.database_mock.transaction.side_effect = lambda operation: nullcontext(self.tx)
        return self.database_mock

    def test_transient_snapshot_read_retries_once_with_remaining_deadline(self):
        from db.base.postgres import DatabaseUnavailable
        database=self.database()
        self.tx.fetch_all.return_value=[]
        database.transaction.side_effect=[DatabaseUnavailable('database_connection_failed',reason='connection_failed'),
                                          nullcontext(self.tx)]
        with patch('db.quota.provider_quota_control.time.monotonic',side_effect=[100,102]):
            result=ProviderQuotaControlRepository(database).snapshot()
        self.assertEqual(result['exhausted'],())
        self.assertEqual([call.args[0].total_timeout_ms for call in database.transaction.call_args_list],[5000,3000])
        self.assertEqual([call.args[0].deadline_monotonic for call in database.transaction.call_args_list],[105,105])
        self.assertTrue(all(call.args[0].read_only for call in database.transaction.call_args_list))
        self.tx.execute.assert_not_called()

    def test_only_one_transient_read_retry_and_no_retry_after_deadline(self):
        from db.base.postgres import DatabaseUnavailable
        for elapsed,calls in ((0.1,2),(5,1)):
            database=self.database()
            database.transaction.side_effect=DatabaseUnavailable('database_connection_failed')
            with self.subTest(elapsed=elapsed), patch('db.quota.provider_quota_control.time.monotonic',side_effect=[100,100+elapsed]):
                with self.assertRaises(DatabaseUnavailable):ProviderQuotaControlRepository(database).snapshot()
            self.assertEqual(database.transaction.call_count,calls)

    def test_nontransient_control_errors_and_ban_writes_are_never_retried(self):
        from db.base.postgres import DatabaseServerError,DatabaseUnavailable
        for error in (DatabaseServerError('database_operation_failed',code='42501'),
                      DatabaseUnavailable('database_pool_unavailable',reason='pool_timeout'),
                      DatabaseUnavailable('database_runtime_role_invalid'),
                      DatabaseUnavailable('database_connection_failed',outcome_unknown=True),
                      ValueError('quota_control_clock_mismatch')):
            database=self.database();database.transaction.side_effect=error
            with self.subTest(error=error),self.assertRaises(type(error)):
                ProviderQuotaControlRepository(database).snapshot()
            self.assertEqual(database.transaction.call_count,1)
        database=self.database()
        database.transaction.side_effect=DatabaseUnavailable('database_connection_failed')
        with self.assertRaises(DatabaseUnavailable):
            ProviderQuotaControlRepository(database,settings=DiscoverySettings(),interval_minutes=10).note_exhausted('general')
        self.assertEqual(database.transaction.call_count,1)

    def test_final_grant_retry_rechecks_expiry_without_new_debit(self):
        from db.base.postgres import DatabaseUnavailable
        from db.discovery_budget import DiscoveryBudgetRepository
        database=self.database()
        database.transaction.side_effect=[DatabaseUnavailable('database_connection_failed'),nullcontext(self.tx)]
        grant=dict(project_id='project',bucket='general',quota_day=self.clock['day'],
                   dispatch_started_at=self.now,expires_at=self.now,campaign_id=None)
        self.tx.fetch_one.side_effect=[grant,self.clock]
        decision=DiscoveryBudgetRepository(database,project_id='project',settings=DiscoverySettings(),interval_minutes=10).validate_dispatch(str(uuid4()),bucket='general')
        self.assertEqual(decision,dict(allowed=False,reason='grant_expired'))
        self.assertEqual(database.transaction.call_count,2)
        self.tx.execute.assert_not_called()

    def test_ban_does_not_debit_or_replace_existing_policy_snapshot(self):
        from db.discovery_budget import DiscoveryBudgetRepository
        database = self.database()
        repo = DiscoveryBudgetRepository(database, project_id='same-project',
            settings=DiscoverySettings(), interval_minutes=10)
        self.assertEqual(repo.note_exhausted('stats'), self.clock['reset'])
        statements = [call.args[0] for call in self.tx.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn('ON CONFLICT DO NOTHING', statements[0])
        self.assertIn('SET blocked_until=', statements[1])
        self.assertNotIn('event_used=', statements[1])
        self.assertNotIn('discovery_budget_grants', ' '.join(statements))
        args = self.tx.execute.call_args.args[1]
        self.assertEqual((args['project'], args['bucket'], args['day']),
                         ('same-project','stats',self.clock['day']))
        self.assertEqual(args['reset']-args['now'], timedelta(hours=25))
        self.assertEqual(database.transaction.call_args.args[0].total_timeout_ms, 5000)

    def test_snapshot_is_bounded_project_day_read_without_journal_dependency(self):
        database = self.database()
        self.tx.fetch_all.return_value = [dict(bucket='general',blocked_until=self.clock['reset']),
                                         dict(bucket='stats',blocked_until=None)]
        result = ProviderQuotaControlRepository(database, project_id='project-two').snapshot()
        self.assertEqual(result['exhausted'], ('general',))
        call = self.tx.fetch_all.call_args
        self.assertEqual(call.args[1], ('project-two', self.clock['day']))
        self.assertEqual(call.kwargs['max_rows'], 2)
        self.assertIn('LIMIT 2', call.args[0])
        self.assertIn('FROM readable.provider_quota_control', call.args[0])
        self.assertNotIn('public.discovery_budget_days', call.args[0])
        self.assertNotIn('journal', call.args[0])
        self.tx.execute.assert_not_called()
        self.assertTrue(database.transaction.call_args.args[0].read_only)

    def test_old_day_pending_refusal_never_blocks_the_new_day(self):
        database = self.database()
        repo = ProviderQuotaControlRepository(database, project_id='same-project',
            settings=DiscoverySettings(), interval_minutes=10)
        repo.note_exhausted('general', quota_day=self.clock['day']-timedelta(days=1))
        self.tx.execute.assert_not_called()

    def test_dispatch_revalidation_rejects_expiry_new_day_unstarted_and_other_bucket(self):
        from db.discovery_budget import DiscoveryBudgetRepository
        for change,bucket,reason in (
                ({'expires_at':datetime(2026,11,1,7,tzinfo=timezone.utc)},'general','grant_expired'),
                ({'quota_day':datetime(2026,10,31,tzinfo=timezone.utc).date()},'general','grant_expired'),
                ({'dispatch_started_at':None},'general','grant_not_dispatchable'),
                ({},'stats','grant_mismatch'),
                ({},'general','dispatchable')):
            with self.subTest(reason=reason,bucket=bucket):
                database=self.database()
                grant=dict(project_id='project',bucket='general',quota_day=self.clock['day'],
                           dispatch_started_at=self.now,expires_at=self.now+timedelta(minutes=10),campaign_id=None)
                grant.update(change)
                self.tx.fetch_one.side_effect=[grant,self.clock,dict(blocked_until=None)]
                repo=DiscoveryBudgetRepository(database,project_id='project',settings=DiscoverySettings(),interval_minutes=10)
                decision=repo.validate_dispatch(str(uuid4()),bucket=bucket)
                self.assertEqual(decision['reason'],reason)
                self.assertEqual(decision['allowed'],reason=='dispatchable')
                self.tx.execute.assert_not_called()
                self.assertTrue(database.transaction.call_args.args[0].read_only)

    def test_dispatch_revalidation_observes_campaign_close_and_new_ban(self):
        from db.discovery_budget import DiscoveryBudgetRepository
        for campaign,tail,reason in ((str(uuid4()),{'state':'completed'},'campaign_closed'),
                                     (None,{'blocked_until':datetime(2026,11,2,8,tzinfo=timezone.utc)},'provider_quota')):
            with self.subTest(reason=reason):
                database=self.database()
                grant=dict(project_id='project',bucket='general',quota_day=self.clock['day'],
                           dispatch_started_at=self.now,expires_at=self.now+timedelta(minutes=10),campaign_id=campaign)
                self.tx.fetch_one.side_effect=[grant,self.clock,tail]
                repo=DiscoveryBudgetRepository(database,project_id='project',settings=DiscoverySettings(),interval_minutes=10)
                self.assertEqual(repo.validate_dispatch(str(uuid4()),bucket='general')['reason'],reason)
                self.tx.execute.assert_not_called()


class HealthScheduleModeTests(unittest.TestCase):
    def test_v4_uses_legacy_ledger_and_v5_uses_only_durable_state(self):
        from collection_schedule import CollectionSchedules, discovery_schedules
        from runtime.collection_schedules import schedule_environment
        for schedules,scope in ((CollectionSchedules(),'legacy_collection'),(discovery_schedules(),'collector')):
            with self.subTest(scope=scope), patch.dict(os.environ,schedule_environment(schedules),clear=True), \
                 patch('youtube_pipeline_health.YouTubeQuotaRepository') as legacy, \
                 patch('db.quota.provider_quota_control.ProviderQuotaControlRepository') as control:
                legacy.return_value.exhausted_buckets.return_value=('stats',)
                control.return_value.snapshot.return_value=Storage().snapshot()
                result=read_provider_quota_control(object())
                self.assertEqual(result['scope'],scope)
                if scope=='legacy_collection':
                    control.assert_not_called()
                    self.assertTrue(result['exhausted_buckets']['video_batch_get_stats'])
                else:
                    legacy.assert_not_called()
                    self.assertFalse(result['exhausted_buckets']['video_batch_get_stats'])
                    control.return_value.snapshot.side_effect=OSError('control unavailable')
                    self.assertEqual(read_provider_quota_control(object())['reason'],'quota_state_unavailable')
                    legacy.assert_not_called()

    def test_missing_or_invalid_schedule_never_assumes_legacy_health(self):
        for environment in ({},{'LIC_COLLECTION_SCHEDULE_SNAPSHOT':'invalid','LIC_COLLECTION_SCHEDULE_SHA256':'invalid'}):
            with self.subTest(environment=bool(environment)), patch.dict(os.environ,environment,clear=True), \
                 patch('youtube_pipeline_health.YouTubeQuotaRepository') as legacy:
                result=read_provider_quota_control(object())
                self.assertEqual(result['reason'],'quota_state_unavailable')
                self.assertEqual(result['exhausted_buckets'],{'general':None,'video_batch_get_stats':None})
                legacy.assert_not_called()


if __name__ == '__main__': unittest.main()
