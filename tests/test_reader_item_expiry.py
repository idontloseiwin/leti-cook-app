"""Per-item database clocks survive projection without borrowing a response clock."""
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.reads import operational_read_queries as operational
from db.reads.system_health import SystemHealthRepository, CHANNEL_HEALTH_COLUMNS, CHANNEL_HEALTH_ORDER
from db.runs.sync_runs import SyncRunRepository
from db.base.postgres import DatabaseError
from db.jobs import operator_job_read_queries as jobs
from db.inventory import inventory_runtime_queries as hydration
from db.reviews import video_type_review_queries as reviews
from hot_storage import HotSourceReadExpired, capture_read_lifetime, iso_utc

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
EARLY = iso_utc(NOW + timedelta(seconds=5))
LATE = iso_utc(NOW + timedelta(seconds=30))


class Database:
    def __init__(self, *values):
        self.values = deque(deepcopy(values))
        self.operations = []
    def read_rows(self, *args, **kwargs):
        return self.values.popleft()
    def fetch_one(self, *args, **kwargs):
        return self.values.popleft()
    def fetch_all(self, *args, **kwargs):
        return self.values.popleft()
    @contextmanager
    def transaction(self, operation):
        self.operations.append(operation)
        yield self


class ItemLifetimeTests(unittest.TestCase):
    def setUp(self):
        for target in ('db.base.repository.utc_now', 'db.reads.analytics_read_queries.utc_now',
                       'db.inventory.inventory_runtime_queries.utc_now'):
            clock = patch(target, return_value=NOW)
            clock.start()
            self.addCleanup(clock.stop)

    def test_operational_and_sync_rows_keep_their_own_source_clock(self):
        rows = [dict(channel_id='c', source_expires_at=EARLY), dict(channel_id='d', source_expires_at=LATE)]
        self.assertEqual(operational.get_channel_coverage_summary(Database(rows)), rows)
        actual, truncated = SystemHealthRepository(Database(rows), read_clock=lambda: NOW).fetch_paginated(
            'channels', CHANNEL_HEALTH_COLUMNS, operations=CHANNEL_HEALTH_ORDER)
        self.assertEqual(actual, rows)
        self.assertFalse(truncated)
        runs = [dict(id=1, summary_json={}, source_expires_at=EARLY),
                dict(id=2, summary_json={}, source_expires_at=LATE)]
        self.assertEqual(SyncRunRepository(Database(runs), read_clock=lambda: NOW).list_recent(limit=2), runs)

    def test_latest_active_and_suspended_jobs_preserve_receipt_expiry(self):
        job = dict(channel_id='c', accepted_epoch=1, id='id', job_type='channel_current_stats_backfill',
                   status='queued', source_expires_at=EARLY)
        for suspended in (False, True):
            with self.subTest(suspended=suspended):
                database = Database(dict(value=job), dict(suspended=suspended))
                value = jobs.get_operator_job_status(database, 'c')
                self.assertEqual(value['source_expires_at'], EARLY)
                self.assertEqual(value['status'], 'suspended' if suspended else 'queued')
                self.assertTrue(database.operations[0].read_only)
        self.assertEqual(jobs.list_active_operator_jobs(Database([job]))[0]['source_expires_at'], EARLY)
        self.assertEqual(jobs.get_operator_job_by_id(Database(dict(value=job), dict(suspended=False)),
            'id', channel_id='c', job_type=job['job_type'])['source_expires_at'], EARLY)

    def test_hydration_preserves_its_aggregate_clock_after_admission(self):
        document = dict(status='ok', formats={}, source_expires_at=LATE)
        with capture_read_lifetime() as lifetime:
            actual = hydration.get_channel_initial_typed_hydration(
                Database(dict(invalid=False), dict(value='off'), dict(value=document)), 'c')
        self.assertEqual(actual, document)
        self.assertEqual(lifetime.expires_at, NOW+timedelta(seconds=30))

    def test_review_case_native_datetime_is_json_serializable_utc(self):
        from fastapi.responses import JSONResponse
        import json
        case = dict.fromkeys(reviews.CASE_FIELDS)
        native = NOW+timedelta(seconds=5)
        value = reviews._value(dict(value=case, source_expires_at=native), reviews.CASE_FIELDS)
        response = JSONResponse(value)
        self.assertEqual(json.loads(response.body)['source_expires_at'], EARLY)

    def test_review_case_retains_clock_but_pure_user_history_does_not_invent_one(self):
        case = dict.fromkeys(reviews.CASE_FIELDS)
        for expiry in (EARLY, None):
            with self.subTest(expiry=expiry):
                self.assertEqual(reviews._value(dict(value=case, source_expires_at=expiry), reviews.CASE_FIELDS),
                                 dict(case, source_expires_at=expiry))
        history = dict.fromkeys(reviews.HISTORY_FIELDS)
        self.assertEqual(reviews._value(dict(value=history), reviews.HISTORY_FIELDS), history)
        with self.assertRaisesRegex(DatabaseError, 'database_result_shape_invalid'):
            reviews._value(dict(value=dict(case, unknown='extra'), source_expires_at=EARLY), reviews.CASE_FIELDS)


if __name__ == '__main__':
    unittest.main()
