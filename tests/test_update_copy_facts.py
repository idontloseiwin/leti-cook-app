"""Versioned source facts stay diagnostic until the installed copy policy accepts them."""
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.hot.hot_runtime import MaintenanceRepository
from db.hot.hot_retention import RetentionError
from db.hot.update_copy_sources import (
    UPDATE_COPY_WINDOW_SQL, UPDATE_COPY_SOURCE_ROWS, _PROFILE_OTHER_INVALID,
)
from update_copy_protocol import marker_from_window, window_facts_version, UpdateCopyError

V1 = {'as_of': '2026-10-18T12:00:00+00:00', 'oldest_source_at': '2026-10-01T00:00:00+00:00',
      'expired': False, 'invalid_source_clock': False}
V2 = {**V1, 'version': 2, 'retained_profile_only': False}
RETAINED = {**V2, 'invalid_source_clock': True, 'retained_profile_only': True}
NOW = int(datetime.fromisoformat(V1['as_of']).timestamp())


class VersionedCopyFactsTests(unittest.TestCase):
    def test_repository_preserves_both_versions_and_actual_clocks(self):
        for value in (V1, V2, RETAINED, {**RETAINED, 'oldest_source_at': None},
                      {**RETAINED, 'expired': True, 'oldest_source_at': '2025-01-01T00:00:00+00:00'},
                      {**V2, 'invalid_source_clock': True}):
            with self.subTest(value=value):
                db = Mock(); db.read_update_copy_window.return_value = dict(value)
                self.assertEqual(MaintenanceRepository(db).update_copy_window(), value)
                db.read_update_copy_window.assert_called_once_with()

    def test_shape_cannot_claim_a_partial_or_inconsistent_v2_proof(self):
        bad = (
            {**V1, 'version': 2}, {**V1, 'retained_profile_only': True},
            {**V2, 'version': True}, {**V2, 'version': 1}, {**V2, 'version': 3},
            {**V2, 'version': '2'}, {**V2, 'retained_profile_only': 1},
            {**V2, 'retained_profile_only': None}, {**V2, 'retained_profile_only': True},
            {**RETAINED, 'extra': 'ignored?'},
        )
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(UpdateCopyError): window_facts_version(value)
                db = Mock(); db.read_update_copy_window.return_value = value
                with self.assertRaisesRegex(RetentionError, 'invalid_update_copy_window'):
                    MaintenanceRepository(db).update_copy_window()

    def test_retained_claim_cannot_normalize_future_or_infinite_clock(self):
        future = (datetime.fromisoformat(V1['as_of']) + timedelta(microseconds=1)).isoformat()
        for oldest in ('infinity', '-infinity', 'not-a-date', future):
            value = {**RETAINED, 'oldest_source_at': oldest}
            with self.subTest(oldest=oldest):
                db = Mock(); db.read_update_copy_window.return_value = value
                with self.assertRaisesRegex(RetentionError, 'invalid_update_copy_window'):
                    MaintenanceRepository(db).update_copy_window()

    def test_standard_policy_never_accepts_unconfirmed_v1_or_v2(self):
        for value in ({**V1, 'invalid_source_clock': True},
                      {**V2, 'invalid_source_clock': True}, RETAINED):
            with self.subTest(value=value), self.assertRaisesRegex(UpdateCopyError, 'unconfirmed'):
                marker_from_window(value, created_at=NOW)

    def test_valid_v2_does_not_change_standard_deadline(self):
        oldest = (datetime.fromisoformat(V1['as_of']) - timedelta(days=29)).isoformat()
        self.assertEqual(marker_from_window({**V2, 'oldest_source_at': oldest}, created_at=NOW).expires_at,
                         NOW + 23 * 3600)


class RetainedProfilePredicateTests(unittest.TestCase):
    """Execute the owner predicate, with only PostgreSQL cast syntax removed.

    SQLite exercises nullable boolean/status logic; the separate <=1000-row
    PostgreSQL recipe verifies native timestamps and the complete source query.
    """
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute("ATTACH DATABASE ':memory:' AS public")
        self.db.execute('CREATE TABLE clock (at TEXT)')
        self.db.execute('INSERT INTO clock VALUES (?)', (V1['as_of'],))
        self.db.execute('''CREATE TABLE public.channels (
            channel_metadata_status TEXT, channel_metadata_checked_at TEXT,
            title TEXT, avatar_url TEXT, subscribers INTEGER, views INTEGER,
            video_count INTEGER, channel_created_at TEXT, uploads_playlist_id TEXT)''')
        self.db.create_function('isfinite', 1,
                                lambda value: None if value is None else value not in ('infinity', '-infinity'))
        self.predicate = _PROFILE_OTHER_INVALID.replace('::timestamptz', '')

    def other_invalid(self, rows):
        self.db.execute('DELETE FROM public.channels')
        self.db.executemany('INSERT INTO public.channels VALUES (?,?,?,NULL,NULL,NULL,NULL,NULL,NULL)', rows)
        return bool(self.db.execute('SELECT ' + self.predicate).fetchone()[0])

    def test_all_known_retained_states_with_finite_past_observation(self):
        for status in ('channel_not_found', 'error', 'running', None):
            for stamp in (V1['oldest_source_at'], V1['as_of']):
                with self.subTest(status=status, stamp=stamp):
                    self.assertFalse(self.other_invalid([(status, stamp, 'old profile')]))

    def test_physical_missing_infinite_and_future_observations_still_block(self):
        for status, stamp in product(('channel_not_found', 'error', 'running', None, 'ok'),
                                    (None, 'infinity', '-infinity', '2027-01-01T00:00:00+00:00')):
            with self.subTest(status=status, stamp=stamp):
                self.assertTrue(self.other_invalid([(status, stamp, 'profile')]))

    def test_unknown_status_is_not_whitelisted_by_finite_time(self):
        for status in ('pending', 'missing', '', 'OK', 'failed'):
            with self.subTest(status=status):
                self.assertTrue(self.other_invalid([(status, V1['oldest_source_at'], 'profile')]))

    def test_mixed_rows_cannot_hide_an_unsupported_profile(self):
        good = ('channel_not_found', V1['oldest_source_at'], 'old profile')
        for bad in (('unexpected', V1['oldest_source_at'], 'other'), ('error', None, 'other')):
            self.assertTrue(self.other_invalid([good, bad]))
        self.assertFalse(self.other_invalid([good, ('ok', V1['as_of'], 'fresh')]))

    def test_no_payload_and_valid_ok_profile_are_not_violations(self):
        self.assertFalse(self.other_invalid([('unexpected', None, None)]))
        self.assertFalse(self.other_invalid([('ok', V1['oldest_source_at'], 'valid')]))
        self.assertFalse(self.other_invalid([]))

    def test_final_sql_requires_exhaustive_proof_not_just_a_retained_row(self):
        expression = UPDATE_COPY_WINDOW_SQL.split('2 AS version,', 1)[1].split(' AS retained_profile_only', 1)[0]
        self.db.execute('CREATE TABLE facts (invalid_source_clock INTEGER, other_invalid_clock INTEGER)')
        self.db.execute('CREATE TABLE proof (unproved INTEGER, profile_other_invalid INTEGER)')
        for invalid, other, unproved, profile_bad in product((False, True), repeat=4):
            self.db.execute('DELETE FROM facts'); self.db.execute('DELETE FROM proof')
            self.db.execute('INSERT INTO facts VALUES (?,?)', (invalid, other))
            self.db.execute('INSERT INTO proof VALUES (?,?)', (unproved, profile_bad))
            actual = bool(self.db.execute('SELECT ' + expression + ' FROM facts CROSS JOIN proof').fetchone()[0])
            self.assertEqual(actual, invalid and not (other or unproved or profile_bad))

    def test_all_sources_keep_their_original_projection_and_nonprofile_gate(self):
        for name in UPDATE_COPY_SOURCE_ROWS:
            self.assertIn("SELECT '" + name + "'::text AS source_name", UPDATE_COPY_WINDOW_SQL)
        self.assertIn("FILTER(WHERE source_name<>'channel_profile')", UPDATE_COPY_WINDOW_SQL)
        self.assertIn('decision.decision_type=i.video_type', UPDATE_COPY_WINDOW_SQL)
        self.assertIn('s.scan_id=p.active_scan_id', UPDATE_COPY_WINDOW_SQL)
        self.assertEqual(UPDATE_COPY_WINDOW_SQL.count('clock_timestamp()'), 1)


if __name__ == '__main__':
    unittest.main()
