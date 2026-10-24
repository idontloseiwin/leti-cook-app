"""Offline boundary vectors for policies prepared for later writer cutovers."""
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
import collection_policy as p
from db.videos.collection_policy import load_cadence_snapshot,_decode_cadence_rows
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


def seed_snapshot():
    """Read the current schema seed to prove format2 default parity."""
    schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")
    values = re.search(r"INSERT INTO public\.video_collection_cadence\([^;]+? VALUES\s*(.*?);", schema, re.S).group(1)
    rows = []
    for row in re.findall(r"\(([^()]+)\)", values):
        rows.append(p.CadenceRule(*json.loads("[" + row.replace("'", '"').replace("NULL", "null") + "]")))
    return p.CadenceSnapshot(tuple(rows), NOW)


def rows(snapshot=None):
    return [dict(asdict(rule), captured_at=NOW.isoformat(), clocks_present=True)
            for rule in (snapshot or seed_snapshot()).rules]


class Database:
    def __init__(self, rows):
        self.rows, self.calls = rows, []
        self.cadence_snapshot = CollectionSchedules().cadence_snapshot()

    def read_rows(self, operation, statement, parameters, *, max_rows):
        self.calls.append((operation, statement, parameters, max_rows))
        return self.rows


class CadenceSnapshotTests(unittest.TestCase):
    def test_injected_immutable_snapshot_has_no_database_read(self):
        db = Database(rows())
        snapshot = load_cadence_snapshot(db)
        self.assertIs(snapshot, db.cadence_snapshot)
        self.assertEqual(set(snapshot.rules[:16]), set(seed_snapshot().rules))
        self.assertEqual(len(snapshot.rules), 30)
        self.assertIsNone(snapshot.captured_at)
        self.assertEqual(db.calls, [])
        with self.assertRaises(FrozenInstanceError):
            snapshot.rules[0].cadence_seconds = 42
        with self.assertRaises(FrozenInstanceError):
            snapshot.rules = ()

    def test_cadence_maximum_and_recheck_snapshot_are_validated_independently(self):
        rule = seed_snapshot().rules[0]
        for seconds in (1, p.MAX_INTERVAL_SECONDS):
            self.assertEqual(replace(rule, cadence_seconds=seconds).cadence_seconds, seconds)
        for seconds in (0, p.MAX_INTERVAL_SECONDS + 1, True, 1.0):
            with self.assertRaises(p.PolicyError):
                replace(rule, cadence_seconds=seconds)
        rechecks = p.RecheckSchedules(unavailable_long_max_seconds=21600, identity_seconds=86400)
        snapshot = p.CadenceSnapshot((rule,), NOW, rechecks)
        self.assertIs(snapshot.rechecks, rechecks)
        self.assertEqual(snapshot.captured_at, NOW)
        with self.assertRaises(FrozenInstanceError):
            snapshot.rechecks.identity_seconds = 1
        for invalid in (None, {}, True):
            with self.assertRaises(p.PolicyError):
                p.CadenceSnapshot((rule,), rechecks=invalid)

    def test_database_rows_cannot_change_process_snapshot(self):
        db = Database(rows())
        original = load_cadence_snapshot(db)
        db.rows[0]["cadence_seconds"] = 42
        self.assertIs(load_cadence_snapshot(db), original)
        self.assertEqual(original.rules[0].cadence_seconds, 3600)
        self.assertEqual(db.calls, [])
        db.cadence_snapshot = None
        with self.assertRaisesRegex(DatabaseError, 'snapshot_required'):
            load_cadence_snapshot(db)

    def test_empty_oversized_duplicate_and_malformed_snapshots_fail_closed(self):
        valid = rows()
        bad_row = dict(valid[0], cadence_seconds=True)
        bad_clock = dict(valid[0], captured_at="infinity")
        no_typed_clock = dict(valid[-1], clocks_present=False)
        missing_column = dict(valid[0]); missing_column.pop("enabled")
        for result in ([], None, tuple(valid), valid * 63, [valid[0]] * 2,
                       [bad_row], [bad_clock], [no_typed_clock], [missing_column],
                       [dict(valid[0], enabled="true")], [dict(valid[0], collection_kind={})],
                       [dict(valid[0], video_type=[])],
                       [valid[0], dict(valid[1], captured_at=(NOW + timedelta(seconds=1)).isoformat())]):
            with self.subTest(result_type=type(result).__name__):
                with self.assertRaises(p.PolicyError):
                    _decode_cadence_rows(result)

    def test_versions_enabled_and_equal_version_highest_minimum(self):
        base = seed_snapshot().rules[0]
        rules = (base, replace(base, policy_version=2, cadence_seconds=43),
                 replace(base, policy_version=3, cadence_seconds=44, enabled=False),
                 replace(base, min_age_seconds=10, policy_version=2, cadence_seconds=45))
        snapshot = p.CadenceSnapshot(rules)
        rule = p.select_policy(snapshot, "long", "counters", "public", NOW - timedelta(seconds=20), NOW, NOW)
        self.assertEqual((rule.cadence_seconds, rule.policy_version, rule.min_age_seconds), (45, 2, 10))


class CollectionPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = seed_snapshot()

    def error(self, code, call, *, sqlstate="P0001"):
        with self.assertRaises(p.PolicyError) as caught:
            call()
        self.assertEqual((caught.exception.code, caught.exception.sqlstate), (code, sqlstate))

    def test_availability_vocabulary_is_exact(self):
        for status, expected in ((None, "public"), ("public", "public"), ("unlisted", "unlisted"),
                                 ("PUBLIC", None), (" public ", None), ("private", None), ("", None)):
            self.assertEqual(p.availability_class(status), expected)

    def test_long_all_bucket_boundaries_round_half_away_from_zero(self):
        for kind in ("counters", "metadata"):
            rules = [r for r in self.snapshot.rules if (r.video_type, r.collection_kind, r.availability_class)
                     == ("long", kind, "public")]
            for edge in [r.min_age_seconds for r in rules] + [-1, 1]:
                for fraction in ("-0.500001", "-0.5", "-0.499999", "0", "0.499999", "0.5", "0.500001"):
                    age = Decimal(edge) + Decimal(fraction)
                    rounded = max(int(age.quantize(Decimal(1), rounding=ROUND_HALF_UP)), 0)
                    expected = next(r for r in rules if r.min_age_seconds <= rounded and
                                    (r.max_age_seconds is None or rounded < r.max_age_seconds))
                    anchor = NOW - timedelta(microseconds=int(age * 1_000_000))
                    with self.subTest(kind=kind, age=age):
                        self.assertEqual(p.select_policy(self.snapshot, "long", kind, "public", anchor, None, NOW), expected)

    def test_null_future_and_unlisted_long_ages(self):
        for anchor in (None, NOW, NOW + timedelta(days=1)):
            self.assertEqual(p.policy_value(self.snapshot, "counters", "public", anchor, NOW, "cadence_seconds"), 3600)
        self.assertEqual(p.policy_value(self.snapshot, "counters", "public", NOW, None, "cadence_seconds"), 3600)
        self.assertEqual(p.policy_value(self.snapshot, "metadata", "unlisted", NOW - timedelta(days=900), NOW, "cadence_seconds"), 2419200)
        self.assertEqual(p.policy_value(self.snapshot, "metadata", None, NOW, NOW, "priority"), 0)
        for kind, status, value in (("other", "public", "cadence_seconds"), ("metadata", "private", "cadence_seconds"),
                                    ("metadata", "public", "unknown")):
            self.assertIsNone(p.policy_value(self.snapshot, kind, status, NOW, NOW, value))

    def test_typed_calendar_day_cutoff_is_open_lower_closed_upper(self):
        for kind, offset in product(("short", "stream"), (-1, 0, 1)):
            anchor = NOW - timedelta(days=90, microseconds=offset)
            rule = p.select_policy(self.snapshot, kind, "metadata", "ignored", anchor, NOW, NOW)
            self.assertEqual(rule.age_bucket, "age_91d_plus" if offset > 0 else "age_0_90d")
        for anchor in (None, NOW + timedelta(days=1), "infinity"):
            rule = p.select_policy(self.snapshot, "short", "metadata", None, anchor, NOW, NOW)
            self.assertEqual(rule.age_bucket, "age_0_90d")
        self.assertEqual(p.select_policy(self.snapshot, "stream", "metadata", None, "-infinity", NOW, NOW).age_bucket,
                         "age_91d_plus")
        self.assertIsNone(p.select_policy(self.snapshot, "short", "metadata", None, None, None, NOW))

    def test_long_due_guard_order_future_source_and_explicit_db_clock(self):
        for published in (None, "infinity", "-infinity"):
            self.assertEqual(p.long_due_at(self.snapshot, "counters", "private", published, NOW, NOW, db_now=NOW), NOW)
        self.assertIsNone(p.long_due_at(self.snapshot, "counters", "private", NOW, None, NOW, db_now=NOW))
        for checked in (None, "infinity", "-infinity", NOW + timedelta(microseconds=1)):
            self.assertEqual(p.long_due_at(self.snapshot, "counters", "public", NOW, checked, NOW, db_now=NOW), NOW)
        checked = NOW - timedelta(hours=2)
        self.assertEqual(p.long_due_at(self.snapshot, "counters", "public", NOW, checked, NOW, db_now=NOW), checked + timedelta(hours=1))
        self.assertIsNone(p.long_due_at(self.snapshot, "unsupported", "public", NOW, checked, NOW, db_now=NOW))

    def test_typed_jitter_unsigned_hash_is_early_and_repeatable(self):
        # MD5('abc') begins 90015098, above signed int32; independently pinned.
        expected_offset = 604800 - 21600 + 0x90015098 % 21601
        for kind in ("short", "stream"):
            for _ in range(2):
                result = p.typed_due_at(self.snapshot, kind, "abc", NOW, NOW, db_now=NOW)
                self.assertEqual(result, NOW + timedelta(seconds=expected_offset))
                self.assertLessEqual(result, NOW + timedelta(days=7))
                self.assertGreaterEqual(result, NOW + timedelta(days=7, hours=-6))
        custom = replace(self.snapshot.rules[-2], jitter_window_seconds=0)
        self.assertEqual(p.typed_due_at(p.CadenceSnapshot((custom,)), "stream", "abc", NOW, NOW, db_now=NOW),
                         NOW + timedelta(days=7))

    def test_typed_null_type_and_space_only_id_match_sql_guard(self):
        for kind, identity, checked in (("long", "abc", NOW), ("short", None, NOW),
                                        ("short", "   ", NOW), ("short", "abc", None)):
            self.error("typed_video_collection_due_input_invalid",
                       lambda: p.typed_due_at(self.snapshot, kind, identity, NOW, checked, db_now=NOW))
        self.error("typed_video_collection_cadence_missing",
                   lambda: p.typed_due_at(self.snapshot, None, "abc", NOW, NOW, db_now=NOW))
        self.assertIsInstance(p.typed_due_at(self.snapshot, "short", "\t", NOW, NOW, db_now=NOW), datetime)
        for checked in ("infinity", "-infinity", NOW + timedelta(microseconds=1)):
            self.assertEqual(p.typed_due_at(self.snapshot, None, "abc", None, checked, db_now=NOW), NOW)

    def test_hot_metadata_ceiling_and_clock_rejection(self):
        for at in (None, "infinity", "-infinity", NOW + timedelta(microseconds=1)):
            self.error("hot_video_refresh_as_of_invalid", lambda: p.metadata_due_at(None, NOW, at, db_now=NOW))
        for available in (None, "infinity", "-infinity", NOW + timedelta(microseconds=1)):
            self.assertEqual(p.metadata_due_at(NOW + timedelta(days=30), available, NOW, db_now=NOW), NOW)
        available = NOW - timedelta(days=20)
        self.assertEqual(p.metadata_due_at(NOW + timedelta(days=30), available, NOW, db_now=NOW), NOW + timedelta(days=8))
        self.assertEqual(p.metadata_due_at(None, available, NOW, db_now=NOW), NOW)
        self.assertEqual(p.metadata_due_at("-infinity", available, NOW, db_now=NOW), p.Infinity.NEGATIVE)

    def test_quota_requires_both_deadlines_but_preserves_null_and_infinity_sql(self):
        clocks = (None, NOW - timedelta(microseconds=1), NOW, NOW + timedelta(microseconds=1), p.Infinity.POSITIVE)
        for error in ("youtube_daily_quota_exceeded_error", "youtube_quota_exhausted"):
            for due, source in product(clocks, repeat=2):
                expected = all(isinstance(v, datetime) and v <= NOW for v in (due, source))
                self.assertEqual(p.quota_claim_allowed(error, due, source, NOW), expected)
            self.assertFalse(p.quota_claim_allowed(error, NOW, NOW, None))
            self.assertTrue(p.quota_claim_allowed(error, None, None, "infinity"))
        for error in (None, "", "unknown", "YouTube_quota_exhausted"):
            self.assertTrue(p.quota_claim_allowed(error, None, None, None))

    def test_pacific_reset_reuses_existing_helper_and_crosses_dst(self):
        for before, expected in (("2026-03-08T08:00:00+00:00", "2026-03-09T07:00:00+00:00"),
                                 ("2026-11-01T07:00:00+00:00", "2026-11-02T08:00:00+00:00"),
                                 ("2026-10-05T06:59:59+00:00", "2026-10-05T07:00:00+00:00")):
            self.assertEqual(p.youtube_quota_reset_at(datetime.fromisoformat(before)), datetime.fromisoformat(expected))
        with patch("collection_policy.next_pacific_midnight", return_value=NOW) as helper:
            self.assertEqual(p.youtube_quota_reset_at(NOW), NOW)
            helper.assert_called_once_with(NOW)
        self.assertIsNone(p.youtube_quota_reset_at(None))
        self.assertEqual(p.youtube_quota_reset_at("infinity"), p.Infinity.POSITIVE)

    def test_clock_timezone_normalization_and_finite_range_fail_closed(self):
        local = NOW.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(p.clock(local), NOW)
        self.error("collection_policy_clock_invalid", lambda: p.clock(NOW.replace(tzinfo=None)))
        self.error("collection_policy_clock_out_of_range",
                   lambda: p.inventory_source_policy("long", "discovery", "complete", 1, 0, 0, 0, None,
                                                     datetime.max.replace(tzinfo=timezone.utc)), sqlstate="22008")
        for now in (None, "infinity", "-infinity"):
            self.error("collection_policy_db_clock_invalid",
                       lambda: p.typed_due_at(self.snapshot, "short", "abc", NOW, NOW, db_now=now))


class InventorySourcePolicyTests(unittest.TestCase):
    def decide(self, kind="long", scan="discovery", outcome="complete", new=0,
               streak=0, attempt=1, errors=0, code=None):
        return p.inventory_source_policy(kind, scan, outcome, new, streak, attempt, errors, code, NOW)

    def test_empty_backoff_and_long_full_reconciliation_streak_retention(self):
        for kind in ("long", "short", "stream"):
            expected = (2, 4, 8, 8, 8, 24) if kind == "long" else (2, 4, 8, 24, 24, 24)
            for streak, hours in enumerate(expected):
                result = self.decide(kind=kind, streak=streak)
                self.assertEqual((result.next_due_at, result.no_new_streak, result.retain_scan),
                                 (NOW + timedelta(hours=hours), streak + 1, False))
        for outcome in ("complete", "empty", "boundary"):
            result = self.decide(scan="full_reconciliation", outcome=outcome, streak=8, new=1)
            self.assertEqual((result.next_due_at, result.no_new_streak), (NOW + timedelta(hours=1), 8))

    def test_continuation_resets_only_long_discovery_with_new_rows(self):
        for kind, scan, new in product(("long", "short", "stream"),
                                       ("initial_inventory", "full_reconciliation", "discovery"), (0, 1)):
            result = self.decide(kind=kind, scan=scan, outcome="continuation", streak=9, new=new)
            discovery = kind == "long" and scan == "discovery"
            self.assertEqual(result.next_due_at, NOW + timedelta(minutes=30 if discovery else 0))
            self.assertEqual(result.no_new_streak, 0 if discovery and new else 9)

    def test_new_rows_reset_streak_for_other_terminal_branches(self):
        for kind, scan, outcome in product(("long", "short", "stream"),
                                           ("initial_inventory", "discovery"), ("complete", "empty", "boundary")):
            result = self.decide(kind=kind, scan=scan, outcome=outcome, streak=9, new=1)
            self.assertEqual((result.next_due_at, result.no_new_streak), (NOW + timedelta(hours=1), 0))

    def test_error_backoff_uses_scan_specific_counter_and_caps(self):
        for count, long_hours, typed_hours in ((None, 2, 1), (-3, 1, 1), (0, 1, 1),
                                              (1, 2, 1), (2, 4, 2), (3, 8, 4), (4, 16, 8), (5, 24, 24), (99, 24, 24)):
            full = self.decide(scan="full_reconciliation", outcome="error", attempt=count, errors=99)
            self.assertEqual(full.next_due_at, NOW + timedelta(hours=long_hours))
            for kind in ("short", "stream"):
                typed = self.decide(kind=kind, outcome="error", attempt=count, errors=99)
                self.assertEqual(typed.next_due_at, NOW + timedelta(hours=typed_hours))
        for count, hours in ((None, 2), (-1, 1), (0, 2), (1, 4), (2, 8), (3, 16), (4, 24), (99, 24)):
            self.assertEqual(self.decide(outcome="error", errors=count, attempt=99).next_due_at, NOW + timedelta(hours=hours))

    def test_not_found_and_exact_invalid_scan_error_set(self):
        invalid = ("repeated_page_token", "inventory_token_cycle", "inventory_token_history_invalid",
                   "invalid_next_page_token", "empty_page_with_continuation", "invalid_playlist_item",
                   "duplicate_playlist_item", "invalid_items", "invalid_response")
        for outcome, code in product(("error", "playlist_not_found"), (*invalid, None, "transport_error")):
            result = self.decide(scan="full_reconciliation", outcome=outcome, code=code)
            self.assertEqual(result.retain_scan, code not in invalid)
        for kind in ("long", "short", "stream"):
            result = self.decide(kind=kind, outcome="playlist_not_found")
            self.assertEqual(result.next_due_at, NOW + timedelta(days=1 if kind == "long" else 21))
            self.assertFalse(result.retain_scan)

    def test_null_defaults_invalid_input_and_int32_overflow(self):
        result = self.decide(streak=None, new=None, attempt=None, errors=None)
        self.assertEqual((result.next_due_at, result.no_new_streak), (NOW + timedelta(hours=2), 1))
        for kind, scan, outcome, at in ((None, "discovery", "complete", NOW), ("bad", "discovery", "complete", NOW),
                                       ("long", None, "complete", NOW), ("long", "discovery", None, NOW),
                                       ("long", "discovery", "complete", None)):
            with self.assertRaises(p.PolicyError) as caught:
                p.inventory_source_policy(kind, scan, outcome, 0, 0, 0, 0, None, at)
            self.assertEqual(caught.exception.code, "inventory_policy_input_invalid")

    def test_explicit_type_schedule_controls_intervals_without_changing_branches(self):
        defaults=CollectionSchedules().cadence_snapshot().inventory_by_type
        for kind in ('long','short','stream'):
            configured=replace(getattr(defaults,kind),missing_playlist_seconds=88,found_new_seconds=77,
                discovery_continuation_seconds=99,quiet_1_seconds=101,quiet_2_seconds=202,
                quiet_3_seconds=303,quiet_4_seconds=404,quiet_5_seconds=505,quiet_6_plus_seconds=606,
                error_0_seconds=11,error_1_seconds=22,error_2_seconds=33,error_3_seconds=44,
                error_4_seconds=55,error_5_plus_seconds=66)
            def decide(scan='discovery',outcome='complete',new=0,streak=0,attempt=1,errors=0,code=None):
                return p.inventory_source_policy(kind,scan,outcome,new,streak,attempt,errors,code,NOW,schedule=configured)
            self.assertEqual(decide(outcome='playlist_not_found').next_due_at,NOW+timedelta(seconds=88))
            self.assertEqual(decide(new=1).next_due_at,NOW+timedelta(seconds=77))
            self.assertEqual(decide(outcome='continuation').next_due_at,NOW+timedelta(seconds=99))
            self.assertEqual(decide(scan='initial_inventory',outcome='continuation').next_due_at,NOW)
            for streak,seconds in enumerate((101,202,303,404,505,606,606)):
                self.assertEqual(decide(streak=streak).next_due_at,NOW+timedelta(seconds=seconds))
            for count,seconds in ((-1,11),(0,11),(1,22),(2,33),(3,44),(4,55),(5,66),(999,66)):
                self.assertEqual(decide(scan='full_reconciliation',outcome='error',attempt=count).next_due_at,
                    NOW+timedelta(seconds=seconds))
            # Long discovery retains its error counter; typed uses attempts.
            expected=44 if kind=='long' else 22
            self.assertEqual(decide(outcome='error',attempt=1,errors=2).next_due_at,NOW+timedelta(seconds=expected))
            rate=decide(outcome='error',code='provider_rate_limited')
            self.assertEqual((rate.next_due_at,rate.retain_scan),(NOW+timedelta(seconds=60),True))
            full=decide(scan='full_reconciliation',streak=4)
            self.assertEqual((full.next_due_at,full.no_new_streak),
                (NOW+timedelta(seconds=77),4) if kind=='long' else (NOW+timedelta(seconds=505),5))

    def test_explicit_inventory_schedule_must_be_finite_model(self):
        with self.assertRaises(p.PolicyError) as caught:
            p.inventory_source_policy('short','discovery','complete',1,0,1,0,None,NOW,schedule={})
        self.assertEqual(caught.exception.code,'inventory_schedule_invalid')
        for options in (dict(streak=2147483647), dict(outcome="error", errors=2147483647)):
            with self.assertRaises(p.PolicyError) as caught:
                self.decide(**options)
            self.assertEqual(caught.exception.sqlstate, "22003")


if __name__ == "__main__":
    unittest.main()
