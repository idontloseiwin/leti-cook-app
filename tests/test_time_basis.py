"""Publication years, days and date filters follow the chosen time zone (owner decision: local by default). No database."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from catalogue_filters import catalogue_time_zone, matches_catalogue_filters, normalize_catalogue_filters  # noqa: E402
from channel_baseline import _publication_calendar  # noqa: E402
from channel_explorer import ChannelExplorerQueryError, _normalized_query  # noqa: E402

LATE = {'published_at': '2026-12-31T23:30:00Z'}  # already 2027 in Kyiv


def filters(**extra):
    return normalize_catalogue_filters(json.dumps({'version': 1, **extra}), {})


def query(**extra):
    return _normalized_query(q='', category_id='', published_from=extra.pop('published_from', ''),
                             published_before=extra.pop('published_before', ''), min_views=None, max_views=None,
                             min_duration_seconds=None, max_duration_seconds=None, sort_by='published_at',
                             sort_order='desc', limit=1, offset=0, catalogue_filters=json.dumps({'version': 1, **extra}))


class TimeZoneFilterTests(unittest.TestCase):
    def test_a_zone_is_validated_and_utc_travels_as_absent(self):
        self.assertEqual(filters(time_zone='Europe/Kyiv')['time_zone'], 'Europe/Kyiv')
        self.assertNotIn('time_zone', filters(time_zone='UTC'))
        self.assertNotIn('time_zone', filters())
        for bad in ('Mars/Base', '../etc/passwd', 5, 'Europe/Kyiv/../x', ''):
            with self.assertRaises(ValueError, msg=repr(bad)):
                filters(time_zone=bad)
        self.assertEqual(catalogue_time_zone(None).key, 'UTC')

    def test_publication_years_are_counted_in_the_zone(self):
        self.assertTrue(matches_catalogue_filters(LATE, filters(time_zone='Europe/Kyiv', published_years=[2027])))
        self.assertFalse(matches_catalogue_filters(LATE, filters(published_years=[2027])))
        self.assertTrue(matches_catalogue_filters(LATE, filters(published_years=[2026])))

    def test_calendar_dates_start_at_midnight_in_the_zone(self):
        kyiv = query(time_zone='Europe/Kyiv', published_from='2026-10-01', published_before='2026-11-01')
        self.assertEqual(kyiv['published_from'].astimezone(timezone.utc).isoformat(), '2026-09-30T21:00:00+00:00')
        # Kyiv leaves summer time on 25 October: November starts at UTC+2.
        self.assertEqual(kyiv['published_before'].astimezone(timezone.utc).isoformat(), '2026-10-31T22:00:00+00:00')
        utc = query(published_from='2026-10-01')
        self.assertEqual(utc['published_from'].isoformat(), '2026-10-01T00:00:00+00:00')
        with self.assertRaises(ChannelExplorerQueryError):
            query(time_zone='Nowhere/Here')


class CalendarTests(unittest.TestCase):
    def test_calendar_days_and_default_year_follow_the_zone(self):
        now = datetime(2026, 12, 31, 23, 45, tzinfo=timezone.utc)
        rows = [LATE, {'published_at': '2026-06-01T12:00:00Z'}]
        utc = _publication_calendar(rows, now=now)
        kyiv = _publication_calendar(rows, now=now, zone=catalogue_time_zone({'time_zone': 'Europe/Kyiv'}))
        self.assertEqual((utc['timezone'], utc['default_year']), ('UTC', 2026))
        self.assertEqual((kyiv['timezone'], kyiv['default_year']), ('Europe/Kyiv', 2027))
        days = lambda calendar: sorted(day['date'] for year in calendar['years'] for day in year['days'])  # noqa: E731
        self.assertEqual(days(utc), ['2026-06-01', '2026-12-31'])
        self.assertEqual(days(kyiv), ['2026-06-01', '2027-01-01'])


if __name__ == '__main__':
    unittest.main()
