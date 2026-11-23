"""Shared provider ban on a disposable PostgreSQL 17; fake HTTP only."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'application'), str(Path(__file__).resolve().parent)]
import research_query_pg as native
import quota_buckets as quota
import youtube_client
from db.discovery_budget import DiscoveryBudgetRepository
from db.quota.provider_quota_control import ProviderQuotaControlRepository
from discovery_policy import DiscoverySettings
from youtube_errors import YouTubeDailyQuotaExceededError


class _UncertainControl:
    """Lose a control read or ACK, never a transaction's committed contents."""
    def __init__(self, database):
        self.database = database
        self.read_error = self.lose_write_ack = False

    @contextmanager
    def transaction(self, operation):
        if operation.name == 'provider_quota_control_read' and self.read_error:
            raise OSError('synthetic control read outage')
        with self.database.transaction(operation) as tx:
            yield tx
        if operation.name == 'provider_quota_control_exhausted' and self.lose_write_ack:
            raise OSError('synthetic lost control write acknowledgement')


class CommonProviderControlPostgres(unittest.TestCase):
    setUpClass = classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass = classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh = classmethod(native.NativeQueryTests.refresh.__func__)

    def setUp(self):
        self.project = 'common-control-'+str(uuid4())
        self.settings = DiscoverySettings()
        self.database = _UncertainControl(self.db)
        self.budget = DiscoveryBudgetRepository(self.db, project_id=self.project,
            settings=self.settings, interval_minutes=10)
        self.control = ProviderQuotaControlRepository(self.db, project_id=self.project)
        quota.end_pass()
        self.addCleanup(quota.end_pass)

    def scope(self):
        return quota.controlled(self.database, project_id=self.project,
                                settings=self.settings, interval_minutes=10)

    def test_real_refusal_precedes_journal_and_stops_restart_and_preissued_grant(self):
        token = str(uuid4())
        self.assertTrue(self.budget.reserve(token, bucket='general')['allowed'])
        response = Mock(status_code=403, is_redirect=False, headers={})
        response.json.return_value = {'error': {'errors': [{'reason': 'quotaExceeded'}]}}
        recorder = Mock()
        recorder.start_attempt.return_value = str(uuid4())
        observed = []
        def finish(*args, **kwargs):
            observed.append(self.control.snapshot()['exhausted'])
            return False  # optional journal failure does not undo the ban
        recorder.finish_attempt.side_effect = finish
        with self.scope(), patch('hot_runtime.require_hot_runtime_ready'), \
             patch('provider_runtime.admit_request', side_effect=lambda **kw: nullcontext(None)), \
             patch('youtube_client._read_bounded_response'), \
             patch('youtube_client.requests.get', return_value=response) as http:
            result = youtube_client.youtube_api_get('videos', phase='test', quota_bucket='general',
                attempt_recorder=recorder, params={'part':'snippet','id':'video000001'})
            self.assertEqual(result.status_code, 403)
            self.assertEqual(http.call_count, 1)
        self.assertEqual(observed, [('general',)])
        self.assertFalse(self.budget.begin_dispatch(token))
        with self.scope():
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('general')
            quota.before_request('stats')
        row = self.admin.execute('SELECT event_used,full_used FROM public.discovery_budget_days WHERE project_id=%s',
                                 (self.project,)).fetchone()
        self.assertEqual(row, (1, 0))

    def test_unknown_control_recovers_without_creating_any_admission_or_ban(self):
        self.database.read_error = True
        with self.scope():
            with self.assertRaises(quota.QuotaStateUnavailable): quota.before_request('general')
            self.database.read_error = False
            quota.before_request('general')
        self.assertEqual(self.control.snapshot()['exhausted'], ())
        self.assertEqual(self.admin.execute('SELECT count(*) FROM public.discovery_budget_days WHERE project_id=%s',
                                           (self.project,)).fetchone()[0], 0)

    def test_unknown_ban_ack_is_idempotently_reconciled_without_budget_refill(self):
        self.database.lose_write_ack = True
        with self.scope():
            quota.note_provider_refusal('stats')
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('stats')
            self.database.lose_write_ack = False
            quota.before_request('general')  # retries the pending ban
        with self.scope():
            with self.assertRaises(YouTubeDailyQuotaExceededError): quota.before_request('stats')
        row = self.admin.execute('SELECT event_used,full_used,window_event_used,window_full_used FROM public.discovery_budget_days WHERE project_id=%s',
                                 (self.project,)).fetchone()
        self.assertEqual(row, (0, 0, 0, 0))

    def test_project_and_bucket_bans_are_isolated(self):
        self.budget.note_exhausted('general')
        other = ProviderQuotaControlRepository(self.db, project_id='different-'+str(uuid4()))
        self.assertEqual(other.snapshot()['exhausted'], ())
        self.assertEqual(self.control.snapshot()['exhausted'], ('general',))

    def test_exact_started_grant_is_rechecked_after_its_window_expires(self):
        token=str(uuid4())
        self.assertTrue(self.budget.reserve(token,bucket='general')['allowed'])
        self.assertEqual(self.budget.validate_dispatch(token,bucket='general')['reason'],'grant_not_dispatchable')
        self.assertTrue(self.budget.begin_dispatch(token))
        self.assertTrue(self.budget.validate_dispatch(token,bucket='general')['allowed'])
        self.assertEqual(self.budget.validate_dispatch(token,bucket='stats')['reason'],'grant_mismatch')
        self.admin.execute("UPDATE public.discovery_budget_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE grant_id=%s::uuid",(token,))
        self.assertEqual(self.budget.validate_dispatch(token,bucket='general')['reason'],'grant_expired')
        row=self.admin.execute('SELECT event_used FROM public.discovery_budget_days WHERE project_id=%s',
                               (self.project,)).fetchone()
        self.assertEqual(row,(1,))

    def test_health_control_reads_as_http_reader_without_raw_table_access(self):
        from collection_schedule import discovery_schedules
        from db.base.postgres import DatabaseError
        from db.base.read_context import http_method_scope, http_read_scope
        from runtime.collection_schedules import schedule_environment
        from youtube_pipeline_health import read_provider_quota_control, quota_control_schedule_mode
        self.budget.note_exhausted('general')
        expected=self.control.snapshot()
        # The backend launcher installs this immutable snapshot before importing
        # server/core. Health reads the inherited environment, not a collector's
        # process-global quota scope (the web process intentionally has none).
        environment=dict(schedule_environment(discovery_schedules()),YOUTUBE_QUOTA_PROJECT_ID=self.project)
        with patch.dict(os.environ,environment), http_method_scope('GET'), http_read_scope():
            self.assertFalse(quota.control_active())
            self.assertEqual(quota_control_schedule_mode(),'durable')
            self.assertEqual(self.db.read_row('quota_control_reader_role','SELECT current_user AS role')['role'],
                             'lic_reader')
            result=self.control.snapshot()
            health=read_provider_quota_control(self.db)
            with self.assertRaises(DatabaseError) as refused:
                self.db.read_row('quota_control_reader_probe',
                    'SELECT count(*) AS n FROM public.discovery_budget_days')
        self.assertEqual(getattr(refused.exception,'code',None),'42501')
        self.assertEqual(result,expected)
        self.assertEqual(health['status'],'ok')
        self.assertIsNone(health['reason'])
        self.assertEqual(health['exhausted_buckets'],{'general':True,'video_batch_get_stats':False})


    def test_retry_reads_current_ban_and_grant_expiry_without_repeating_mutation(self):
        from db.base.postgres import DatabaseUnavailable
        attempts=[]
        database=self.db
        after_first=[lambda:self.budget.note_exhausted('general')]
        class LoseOneRead:
            @contextmanager
            def transaction(_,operation):
                attempts.append(operation)
                with database.transaction(operation) as tx:
                    yield tx
                if after_first:
                    after_first.pop()()
                    raise DatabaseUnavailable('database_connection_failed',reason='connection_failed')
        proxy=LoseOneRead()
        snapshot=ProviderQuotaControlRepository(proxy,project_id=self.project).snapshot()
        self.assertEqual(snapshot['exhausted'],('general',))
        self.assertEqual(len(attempts),2)
        self.assertEqual(attempts[0].deadline_monotonic,attempts[1].deadline_monotonic)
        self.assertTrue(all(operation.read_only for operation in attempts))
        token=str(uuid4())
        self.assertTrue(self.budget.reserve(token,bucket='stats')['allowed'])
        self.assertTrue(self.budget.begin_dispatch(token))
        after_first.append(lambda:self.admin.execute(
            "UPDATE public.discovery_budget_grants SET expires_at=clock_timestamp()-interval '1 second' WHERE grant_id=%s::uuid",(token,)))
        attempts.clear()
        decision=DiscoveryBudgetRepository(proxy,project_id=self.project,settings=self.settings,
            interval_minutes=10).validate_dispatch(token,bucket='stats')
        self.assertEqual(decision['reason'],'grant_expired')
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.admin.execute('SELECT event_used FROM public.discovery_budget_days WHERE project_id=%s AND bucket=%s',
            (self.project,'stats')).fetchone(),(1,))


if __name__ == '__main__': unittest.main()
