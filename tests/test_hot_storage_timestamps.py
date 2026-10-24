"""UTC normalization preserves timezone validation and exact read boundaries."""
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from hot_storage import HOT_DATA_WINDOW, capture_read_lifetime, source_is_current, utc_instant


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


class AmbiguousDatetime(datetime):
    def utcoffset(self):
        return None


class TimestampTests(unittest.TestCase):
    def test_utc_datetime_and_offset_inputs_represent_the_same_instant(self):
        expected = datetime(2026, 10, 11, 12, 3, 4, 123456, tzinfo=timezone.utc)
        self.assertIs(utc_instant(expected), expected)
        for minutes in (-720, -90, 0, 90, 840):
            local = expected.astimezone(timezone(timedelta(minutes=minutes)))
            for value in (local, local.isoformat(), local.isoformat(" ")):
                with self.subTest(value=value):
                    actual = utc_instant(value)
                    self.assertEqual(actual, expected)
                    self.assertIs(actual.tzinfo, timezone.utc)

    def test_postgres_trimmed_fractions_keep_microseconds(self):
        for digits in ("1", "12", "123", "1234", "12345", "123456"):
            for suffix in ("Z", "+00:00", "+02:00"):
                value = f"2026-10-11T12:03:04.{digits}{suffix}"
                expected = datetime(2026, 10, 11, 10 if suffix == "+02:00" else 12,
                                    3, 4, int(digits.ljust(6, "0")), tzinfo=timezone.utc)
                self.assertEqual(utc_instant(value), expected)

    def test_naive_invalid_and_ambiguous_clocks_still_fail_closed(self):
        for value in (None, 0, True, "", "broken", "2026-10-11", "2026-10-11T12:03:04",
                      datetime(2026, 10, 11), datetime(2026, 10, 11, tzinfo=NoOffset()),
                      AmbiguousDatetime(2026, 10, 11, tzinfo=timezone.utc)):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                utc_instant(value)

    def test_source_boundaries_and_captured_deadlines_do_not_widen(self):
        now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        oldest = now - HOT_DATA_WINDOW
        with capture_read_lifetime() as lifetime:
            self.assertTrue(source_is_current(oldest, now=now))
            self.assertFalse(source_is_current(oldest - timedelta(microseconds=1), now=now))
            self.assertFalse(source_is_current(now + timedelta(microseconds=1), now=now))
        self.assertEqual(lifetime.expires_at, now)
        with capture_read_lifetime() as lifetime:
            self.assertFalse(source_is_current(oldest, now=now,
                                               valid_until=now + timedelta(microseconds=1)))
        self.assertIsNone(lifetime.expires_at)


if __name__ == "__main__":
    unittest.main()
