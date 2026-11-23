"""Deterministic evidence-wait clocks and bounds; no database or provider."""
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from discovery_policy import (
    DiscoveryPolicyError, DiscoverySettings, WaitClock, RepairWindow,
    advance_wait_clock, needs_review, retry_due_at, lifecycle_due_at,
    technical_retry_due_at, unresolved_status,
    repair_window, prepare_repair_window, repair_preparation_deadline,
)

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
SETTINGS = DiscoverySettings()


class SettingsTests(unittest.TestCase):
    def test_distinct_event_full_and_repair_budgets_have_owner_accepted_defaults(self):
        self.assertEqual((SETTINGS.events_general_daily_limit, SETTINGS.events_general_window_limit,
                          SETTINGS.events_stats_daily_limit, SETTINGS.events_stats_window_limit),
                         (2500, 100, 500, 50))
        self.assertEqual((SETTINGS.full_general_daily_limit, SETTINGS.full_general_window_limit), (1000, 25))
        self.assertEqual((SETTINGS.repair_target_limit, SETTINGS.repair_general_limit,
                          SETTINGS.repair_stats_limit, SETTINGS.repair_window_seconds), (300, 600, 50, 604800))
        with self.assertRaises(FrozenInstanceError):
            SETTINGS.full_general_daily_limit = 1

    def test_values_do_not_coerce_boolean_float_text_or_unbounded_input(self):
        for item in fields(SETTINGS):
            for bad in (True, False, 0, -1, 1.0, '1', None, 2 ** 40):
                with self.subTest(field=item.name, bad=bad), self.assertRaises(DiscoveryPolicyError):
                    replace(SETTINGS, **{item.name: bad})

    def test_retry_order_and_budget_window_are_validated_independently(self):
        for changes in ({'retry_second_seconds': 119}, {'retry_sparse_seconds': 1799},
                        {'technical_retry_first_seconds': 21601},
                        {'review_retry_seconds': 21599}, {'unresolved_pause_seconds': 604800},
                        {'events_general_window_limit': 2501}, {'events_stats_window_limit': 501},
                        {'full_general_window_limit': 1001}):
            with self.subTest(changes=changes), self.assertRaises(DiscoveryPolicyError):
                replace(SETTINGS, **changes)
        # Campaigns may span several days; do not invent a per-day campaign cap.
        self.assertEqual(replace(SETTINGS, repair_general_limit=3000).repair_general_limit, 3000)


class RetryTests(unittest.TestCase):
    def test_technical_backoff_is_independent_exponential_bounded_and_configurable(self):
        for failures, delay in enumerate((120,240,480,960,1920,3840,7680,15360,21600,21600), 1):
            self.assertEqual(technical_retry_due_at(NOW, failures, SETTINGS),
                             NOW+timedelta(seconds=delay))
        changed=replace(SETTINGS,technical_retry_first_seconds=30,technical_retry_max_seconds=300)
        self.assertEqual(technical_retry_due_at(NOW,5,changed),NOW+timedelta(seconds=300))
        self.assertEqual(technical_retry_due_at(NOW,2**31-1,SETTINGS),NOW+timedelta(hours=6))
        for bad in (0,-1,True,1.0,'1',None,2**40):
            with self.subTest(bad=bad), self.assertRaises(DiscoveryPolicyError):
                technical_retry_due_at(NOW,bad,SETTINGS)

    def test_retries_are_relative_to_previous_check_not_initial_signal(self):
        current = NOW
        for attempt, minutes_from_start in enumerate((2, 12, 42, 402, 762), start=1):
            current = retry_due_at(current, attempt, SETTINGS)
            self.assertEqual(current, NOW + timedelta(minutes=minutes_from_start))
        self.assertEqual(retry_due_at(NOW + timedelta(days=2), 2, SETTINGS),
                         NOW + timedelta(days=2, minutes=10))

    def test_no_default_settings_or_implicit_attempt_increment(self):
        for bad in (None, {}, True):
            with self.assertRaises(DiscoveryPolicyError):
                retry_due_at(NOW, 1, bad)
        for bad in (0, -1, True, 1.0, '1', None, 2 ** 40):
            with self.assertRaises(DiscoveryPolicyError):
                retry_due_at(NOW, bad, SETTINGS)

    def test_clock_requires_aware_datetime_and_normalizes_offset(self):
        offset = timezone(timedelta(hours=2))
        self.assertEqual(retry_due_at(NOW.astimezone(offset), 1, SETTINGS), NOW + timedelta(minutes=2))
        for bad in (NOW.replace(tzinfo=None), NOW.isoformat(), None, True):
            with self.assertRaises(DiscoveryPolicyError):
                retry_due_at(bad, 1, SETTINGS)
        with self.assertRaisesRegex(DiscoveryPolicyError, '^discovery_clock_invalid$'):
            retry_due_at(datetime.max.replace(tzinfo=timezone.utc), 1, SETTINGS)

    def test_future_start_has_two_hour_ceiling_and_nonfuture_uses_lifecycle_cadence(self):
        self.assertEqual(lifecycle_due_at(NOW, NOW + timedelta(minutes=45), SETTINGS),
                         NOW + timedelta(minutes=45))
        self.assertEqual(lifecycle_due_at(NOW, NOW + timedelta(days=14), SETTINGS),
                         NOW + timedelta(hours=2))
        for scheduled in (None, NOW, NOW - timedelta(seconds=1)):
            self.assertEqual(lifecycle_due_at(NOW, scheduled, SETTINGS), NOW + timedelta(minutes=15))
        with self.assertRaises(DiscoveryPolicyError):
            lifecycle_due_at(NOW, 'not-a-clock', SETTINGS)


class WaitClockTests(unittest.TestCase):
    def test_review_and_pause_boundaries_keep_original_age(self):
        clock=WaitClock(NOW,NOW)
        for elapsed,status in ((timedelta(days=7,microseconds=-1),'active'),
                               (timedelta(days=7),'review'),
                               (timedelta(days=30,microseconds=-1),'review'),
                               (timedelta(days=30),'paused'),
                               (timedelta(days=90),'paused')):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(unresolved_status(clock,NOW+elapsed,SETTINGS),status)
        self.assertEqual(clock,WaitClock(NOW,NOW))

    def test_future_pause_does_not_reset_preexisting_problem_age(self):
        opening=NOW-timedelta(days=29)
        clock=advance_wait_clock(None,observed_at=NOW,opened_at=opening,
                                 scheduled_start_at=NOW+timedelta(days=40))
        self.assertEqual(unresolved_status(clock,NOW+timedelta(days=40),SETTINGS),'review')
        self.assertEqual(unresolved_status(clock,NOW+timedelta(days=41),SETTINGS),'paused')
        self.assertEqual(clock.opened_at,opening)

    def test_duplicate_checks_and_events_keep_opening_and_review_age(self):
        clock = advance_wait_clock(None, observed_at=NOW, opened_at=NOW - timedelta(days=6))
        again = advance_wait_clock(clock, observed_at=NOW + timedelta(hours=23))
        self.assertEqual(again, clock)
        self.assertFalse(needs_review(again, NOW + timedelta(days=1, microseconds=-1), SETTINGS))
        self.assertTrue(needs_review(again, NOW + timedelta(days=1), SETTINGS))
        with self.assertRaises(DiscoveryPolicyError):
            advance_wait_clock(clock, observed_at=NOW, opened_at=NOW)

    def test_future_pause_preserves_prior_wait_and_does_not_age_a_future_premiere(self):
        opening = NOW - timedelta(days=6)
        future = NOW + timedelta(days=14)
        clock = advance_wait_clock(None, observed_at=NOW, opened_at=opening, scheduled_start_at=future)
        self.assertEqual(clock.opened_at, opening)
        self.assertEqual(clock.unresolved_since, opening)
        self.assertFalse(needs_review(clock, future, SETTINGS))
        resumed = advance_wait_clock(clock, observed_at=future)
        self.assertEqual(resumed.unresolved_since, opening + timedelta(days=14))
        self.assertIsNone(resumed.future_wait_started_at)
        self.assertTrue(needs_review(resumed, future + timedelta(days=1), SETTINGS))

    def test_repeated_future_observation_preserves_start_and_accumulated_age(self):
        opening = NOW - timedelta(days=6)
        clock = advance_wait_clock(None, observed_at=NOW, opened_at=opening,
                                   scheduled_start_at=NOW + timedelta(days=14))
        changed = advance_wait_clock(clock, observed_at=NOW + timedelta(days=1),
                                     scheduled_start_at=NOW + timedelta(days=20))
        self.assertEqual(changed.future_wait_started_at, NOW)
        self.assertEqual(changed.unresolved_since, opening)
        self.assertEqual(changed.future_wait_until, NOW + timedelta(days=20))
        resumed = advance_wait_clock(changed, observed_at=NOW + timedelta(days=20))
        self.assertTrue(needs_review(resumed, NOW + timedelta(days=21), SETTINGS))

    def test_downtime_after_known_start_counts_as_problem_wait_not_future(self):
        clock = advance_wait_clock(None, observed_at=NOW, opened_at=NOW - timedelta(days=6),
                                   scheduled_start_at=NOW + timedelta(hours=2))
        resumed_at = NOW + timedelta(days=2)
        self.assertTrue(needs_review(clock, resumed_at, SETTINGS))
        resumed = advance_wait_clock(clock, observed_at=resumed_at)
        self.assertEqual(resumed.unresolved_since, NOW - timedelta(days=6) + timedelta(hours=2))
        self.assertTrue(needs_review(resumed, resumed_at, SETTINGS))

    def test_new_future_after_expiry_preserves_gap_and_repeated_cycles(self):
        opening = NOW - timedelta(days=6)
        clock = advance_wait_clock(None, observed_at=NOW, opened_at=opening,
                                   scheduled_start_at=NOW + timedelta(hours=2))
        # Two unconfirmed hours after the old start cannot be paused retroactively.
        renewed_at = NOW + timedelta(hours=4)
        renewed = advance_wait_clock(clock, observed_at=renewed_at,
                                     scheduled_start_at=renewed_at + timedelta(hours=2))
        self.assertEqual(renewed.unresolved_since, opening + timedelta(hours=2))
        self.assertEqual(renewed.future_wait_started_at, renewed_at)
        resumed = advance_wait_clock(renewed, observed_at=NOW + timedelta(hours=6))
        self.assertEqual(resumed.unresolved_since, opening + timedelta(hours=4))
        self.assertTrue(needs_review(resumed, NOW + timedelta(hours=28), SETTINGS))

    def test_shortened_future_and_early_publication_settle_only_actual_pause(self):
        clock = advance_wait_clock(None, observed_at=NOW, opened_at=NOW - timedelta(hours=1),
                                   scheduled_start_at=NOW + timedelta(days=14))
        earlier = advance_wait_clock(clock, observed_at=NOW + timedelta(minutes=30),
                                     scheduled_start_at=NOW + timedelta(hours=1))
        self.assertEqual(earlier.future_wait_started_at, NOW)
        resumed = advance_wait_clock(earlier, observed_at=NOW + timedelta(minutes=45))
        self.assertEqual(resumed.unresolved_since, NOW - timedelta(minutes=15))

    def test_invalid_pairs_order_and_nonfinite_clock_types_fail_closed(self):
        for args in ((NOW, NOW - timedelta(seconds=1)),
                     (NOW, NOW, NOW, None), (NOW, NOW, None, NOW),
                     (NOW, NOW, NOW, NOW), (NOW, NOW, NOW - timedelta(seconds=1), NOW),
                     (None, NOW), (NOW, None)):
            with self.subTest(args=args), self.assertRaises(DiscoveryPolicyError):
                WaitClock(*args)
        clock = WaitClock(NOW, NOW, NOW + timedelta(seconds=1), NOW + timedelta(hours=1))
        with self.assertRaises(DiscoveryPolicyError):
            advance_wait_clock(clock, observed_at=NOW)
        with self.assertRaises(DiscoveryPolicyError):
            needs_review(clock, NOW, SETTINGS)


class RepairTests(unittest.TestCase):
    def test_window_is_cutover_anchored_half_open_and_uses_our_receipt_clock(self):
        window = repair_window(NOW, SETTINGS)
        self.assertEqual(window, RepairWindow(NOW - timedelta(days=7), NOW))
        self.assertFalse(window.contains(window.start_at - timedelta(microseconds=1)))
        self.assertTrue(window.contains(window.start_at))
        self.assertTrue(window.contains(NOW - timedelta(microseconds=1)))
        self.assertFalse(window.contains(NOW))
        for prepared in (NOW, NOW + timedelta(days=30)):
            self.assertEqual(prepare_repair_window(NOW, prepared, SETTINGS,
                journal_retention_seconds=90 * 86400), window)

    def test_late_preparation_refuses_instead_of_moving_or_truncating_window(self):
        window = repair_window(NOW, SETTINGS)
        deadline = repair_preparation_deadline(window, 90 * 86400)
        self.assertEqual(deadline, NOW + timedelta(days=83))
        self.assertEqual(prepare_repair_window(NOW, deadline - timedelta(microseconds=1), SETTINGS,
            journal_retention_seconds=90 * 86400), window)
        for prepared in (deadline, deadline + timedelta(days=1)):
            with self.assertRaisesRegex(DiscoveryPolicyError, '^discovery_repair_history_expired$'):
                prepare_repair_window(NOW, prepared, SETTINGS, journal_retention_seconds=90 * 86400)
        with self.assertRaisesRegex(DiscoveryPolicyError, '^discovery_repair_before_cutover$'):
            prepare_repair_window(NOW, NOW - timedelta(seconds=1), SETTINGS,
                                  journal_retention_seconds=90 * 86400)

    def test_journal_retention_is_explicit_and_must_cover_approved_window(self):
        for bad in (True, 0, 7 * 86400, 1.0, '7776000', None):
            with self.assertRaises(DiscoveryPolicyError):
                repair_preparation_deadline(repair_window(NOW, SETTINGS), bad)


if __name__ == '__main__':
    unittest.main()
