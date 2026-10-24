"""Shared counter/availability policy boundaries without provider or DB calls."""
import configparser
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import collection_policy as policy
import collection_schedule as schedule
from collection_schedule_model import UnavailableSchedule, UnavailableSchedules

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
TEMPLATE = (ROOT / 'config/collection-schedules.ini').read_bytes()


def v2_file():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(TEMPLATE.decode())
    parser['format']['version'] = '2'
    for section in tuple(parser.sections()):
        if section.endswith(('.unavailable', '.inventory')) or section in (
                'schedule.short.counters', 'schedule.stream.counters'):
            parser.remove_section(section)
    parser['schedule.short.metadata']['age_through_90d_seconds'] = '345600'
    parser['schedule.stream.metadata']['jitter_seconds'] = '0'
    parser['schedule.long.counters']['age_0_1d_seconds'] = '1800'
    parser['schedule.rechecks']['identity_seconds'] = '86400'
    output = io.StringIO()
    parser.write(output)
    return output.getvalue().encode()


class VersionThreeScheduleTests(unittest.TestCase):
    def test_installed_v2_keeps_every_existing_setting_and_adds_only_new_defaults(self):
        value = schedule.parse_schedule_file(v2_file())
        self.assertEqual(value.short_metadata.age_through_90d_seconds, 345600)
        self.assertEqual(value.stream_metadata.jitter_seconds, 0)
        self.assertEqual(value.long_counters.age_0_1d_seconds, 1800)
        self.assertEqual(value.rechecks.identity_seconds, 86400)
        self.assertEqual(value.short_counters, schedule.LongCounters())
        self.assertEqual(value.stream_counters, schedule.LongCounters())
        self.assertEqual(value.long_unavailable, UnavailableSchedule())
        raw = schedule.serialize_snapshot(value)
        self.assertEqual(schedule.deserialize_snapshot(raw, schedule.snapshot_digest(value)), value)
        self.assertIn(b'"format":4', raw)

    def test_legacy_version_is_not_permission_to_ignore_unknown_or_incomplete_values(self):
        raw = v2_file()
        for changed in (raw + b'\n[schedule.short.counters]\nage_0_1d_seconds = 1\n',
                        raw.replace(b'identity_seconds = 86400\n', b''),
                        raw.replace(b'age_0_1d_seconds = 1800', b'unknown = 1800')):
            with self.subTest(changed=changed[:32]), self.assertRaises(schedule.ScheduleError):
                schedule.parse_schedule_file(changed)

    def test_new_counter_sections_have_independent_same_long_defaults(self):
        value = schedule.parse_schedule_file(TEMPLATE)
        self.assertEqual(value.long_counters, value.short_counters)
        self.assertEqual(value.long_counters, value.stream_counters)
        changed = replace(value, short_counters=replace(value.short_counters, age_0_1d_seconds=1800))
        for kind, seconds in (('long', 3600), ('short', 1800), ('stream', 3600)):
            rule = policy.select_counter_policy_at_age(changed.cadence_snapshot(), kind, 'public', 0)
            self.assertEqual(rule.cadence_seconds, seconds)

    def test_recheck_sections_are_independent_and_roundtrip_to_worker(self):
        value = schedule.CollectionSchedules()
        changed = replace(value, short_unavailable=UnavailableSchedule(43200, 259200, 172800, 864000))
        raw = schedule.serialize_snapshot(changed)
        copied = schedule.deserialize_snapshot(raw, schedule.snapshot_digest(changed))
        self.assertEqual(copied, changed)
        snapshot = copied.cadence_snapshot()
        self.assertEqual(snapshot.unavailable_by_type.short.daily_seconds, 43200)
        self.assertEqual(snapshot.unavailable_by_type.long, UnavailableSchedule())
        self.assertEqual(snapshot.unavailable_by_type.stream, UnavailableSchedule())

    def test_episode_setting_bounds_do_not_change_metadata_interval_bounds(self):
        self.assertEqual(UnavailableSchedule().stop_after_seconds, 29 * 86400)
        for changed in ({'stop_after_seconds': 29 * 86400 + 1},
                        {'stop_after_seconds': True}, {'daily_seconds': 0},
                        {'weekly_seconds': 28 * 86400 + 1},
                        {'daily_window_seconds': 86400, 'stop_after_seconds': 86399}):
            with self.subTest(changed=changed), self.assertRaises(schedule.ScheduleError):
                UnavailableSchedule(**changed)
        with self.assertRaises(schedule.ScheduleError):
            schedule.TypedMetadata(age_over_90d_seconds=29 * 86400)


class SharedCounterPolicyTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = schedule.CollectionSchedules().cadence_snapshot()

    def test_all_types_choose_exact_same_default_boundaries(self):
        for seconds, expected in ((0, 3600), (86399, 3600), (86400, 7200),
                                  (604799, 7200), (604800, 21600),
                                  (2419200, 43200), (7776000, 86400),
                                  (31536000, 604800)):
            for kind in ('long', 'short', 'stream'):
                with self.subTest(kind=kind, age=seconds):
                    selected = policy.select_counter_policy_at_age(self.snapshot, kind, 'public', seconds)
                    self.assertEqual(selected.cadence_seconds, expected)
                    due = policy.counters_due_at(self.snapshot, kind, 'public',
                        NOW - timedelta(seconds=seconds), NOW, NOW, db_now=NOW)
                    self.assertEqual(due, NOW + timedelta(seconds=expected))

    def test_unlisted_is_available_and_private_not_scheduled_as_counter_work(self):
        for kind in ('long', 'short', 'stream'):
            self.assertEqual(policy.counters_due_at(self.snapshot, kind, 'unlisted', NOW, NOW,
                NOW, db_now=NOW), NOW + timedelta(days=28))
            self.assertIsNone(policy.counters_due_at(self.snapshot, kind, 'private', NOW, NOW,
                NOW, db_now=NOW))

    def test_unknown_type_is_never_inferred_as_long(self):
        self.assertIsNone(policy.select_counter_policy_at_age(self.snapshot, None, 'public', 0))
        with self.assertRaises(policy.PolicyError):
            policy.counters_due_at(self.snapshot, None, 'public', NOW, NOW, NOW, db_now=NOW)

    def test_short_and_stream_metadata_rules_do_not_change(self):
        value = schedule.CollectionSchedules()
        changed = replace(value, short_counters=replace(value.short_counters, age_0_1d_seconds=1),
                          stream_counters=replace(value.stream_counters, age_365d_plus_seconds=2))
        for kind in ('short', 'stream'):
            for age in (1, 90, 91, 400):
                anchor = NOW - timedelta(days=age)
                self.assertEqual(policy.typed_due_at(value.cadence_snapshot(), kind, 'same-video', anchor,
                    NOW, db_now=NOW), policy.typed_due_at(changed.cadence_snapshot(), kind,
                    'same-video', anchor, NOW, db_now=NOW))


class UnavailableRetryTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = schedule.CollectionSchedules().cadence_snapshot()

    def test_daily_then_weekly_then_local_stop_without_29th_day_provider_retry(self):
        for kind in ('long', 'short', 'stream', None):
            for day, next_day in ((0, 1), (1, 2), (6, 7), (7, 14), (14, 21),
                                  (21, 28), (28, 29)):
                with self.subTest(kind=kind, day=day):
                    result = policy.unavailable_retry_policy(self.snapshot, kind, NOW, NOW + timedelta(days=day))
                    self.assertEqual(result.next_due_at, NOW + timedelta(days=next_day))
                    self.assertEqual(result.stop_at, NOW + timedelta(days=29))
                    self.assertFalse(result.stopped)
            for day in (29, 30, 300):
                result = policy.unavailable_retry_policy(self.snapshot, kind, NOW, NOW + timedelta(days=day))
                self.assertTrue(result.stopped)
                self.assertIsNone(result.next_due_at)

    def test_late_execution_does_not_reset_episode_or_request_catchup_burst(self):
        result = policy.unavailable_retry_policy(self.snapshot, 'short', NOW,
                                                NOW + timedelta(days=20, hours=3))
        self.assertEqual(result.next_due_at, NOW + timedelta(days=27, hours=3))
        self.assertEqual(result.stop_at, NOW + timedelta(days=29))
        result = policy.unavailable_retry_policy(self.snapshot, 'stream', NOW,
                                                NOW + timedelta(days=28, hours=23))
        self.assertEqual(result.next_due_at, NOW + timedelta(days=29))

    def test_one_type_override_does_not_affect_other_types_or_unknown_identity(self):
        changed = replace(self.snapshot, unavailable_by_type=UnavailableSchedules(
            short=UnavailableSchedule(43200, 86400, 172800, 864000)))
        for kind, expected in (('long', 7), ('short', 2), ('stream', 7), (None, 7)):
            at = NOW + timedelta(days=7)
            result = policy.unavailable_retry_policy(changed, kind, NOW, at)
            self.assertEqual(result.next_due_at, at + timedelta(days=expected))

    def test_stop_boundary_is_exact_and_aware_clocks_are_normalized(self):
        first = NOW.astimezone(timezone(timedelta(hours=3)))
        before = NOW + timedelta(days=29, microseconds=-1)
        result = policy.unavailable_retry_policy(self.snapshot, 'long', first, before)
        self.assertFalse(result.stopped)
        self.assertEqual(result.next_due_at, NOW + timedelta(days=29))
        self.assertTrue(policy.unavailable_retry_policy(self.snapshot, 'long', first,
            NOW + timedelta(days=29)).stopped)

    def test_unknown_dates_or_future_first_miss_fail_closed(self):
        for first in (None, 'infinity', NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)):
            with self.subTest(first=first), self.assertRaises(policy.PolicyError):
                policy.unavailable_retry_policy(self.snapshot, 'long', first, NOW)
        with self.assertRaises(policy.PolicyError):
            policy.unavailable_retry_policy(self.snapshot, 'other', NOW, NOW)


if __name__ == '__main__':
    unittest.main()
