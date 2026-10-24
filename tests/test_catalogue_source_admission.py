"""Independent clocks must survive catalogue assembly and its read caches."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from channel_explorer import build_channel_profile
from catalogue_filters import build_channel_filter_facets
from db.base.repository import DatabaseRepository
from hot_storage import HOT_DATA_WINDOW, HotSourceReadExpired, capture_read_lifetime
from hot_storage_fields import channel_source_fields

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=1)).isoformat()
EXPIRED = (NOW - HOT_DATA_WINDOW - timedelta(seconds=1)).isoformat()


class Stored:
    def __init__(self, profile=FRESH, country=FRESH, availability=FRESH):
        self.channel = dict(channel_id='channel',title='profile',country_code='GB',
            channel_metadata_status='ok',channel_metadata_checked_at=profile,
            country_collected_at=country,is_hidden=False)
        self.video = dict(video_id='video',title='video',views=10,duration='PT30S',
            published_at=FRESH,checked_at=FRESH,metadata_checked_at=FRESH,
            availability_status='public',availability_checked_at=availability)

    def load_channel(self, channel_id, *, columns):
        return dict(self.channel)

    def load_recent_public_video_counts(self, channel_id, *, recorded_through):
        return []

    def load_long_inventory(self, channel_id, *, limit):
        return [dict(video_id='video',source_checked_at=FRESH)]

    def load_videos_by_ids(self, video_ids, *, columns):
        return [dict(self.video)]


class CatalogueSourceAdmissionTests(unittest.TestCase):
    def test_country_does_not_borrow_a_fresh_profile_clock(self):
        for clock in (None,EXPIRED,'infinity','-infinity',(NOW+timedelta(seconds=1)).isoformat()):
            with self.subTest(clock=clock):
                result=build_channel_profile(Stored(country=clock),'channel',now=NOW)
                self.assertEqual(result['channel']['title'],'profile')
                self.assertIsNone(result['channel']['country_code'])

    def test_country_survives_an_expired_profile_and_sets_its_own_deadline(self):
        repository=Stored(profile=EXPIRED)
        with capture_read_lifetime() as lifetime:
            result=build_channel_profile(repository,'channel',now=NOW)
        self.assertIsNone(result['channel']['title'])
        self.assertEqual(result['channel']['country_code'],'GB')
        self.assertFalse(result['freshness']['channel_data_fresh'])
        self.assertEqual(lifetime.expires_at,datetime.fromisoformat(FRESH)+HOT_DATA_WINDOW)

    def test_country_cache_horizon_cannot_extend_its_deadline(self):
        row=Stored(country=(NOW-HOT_DATA_WINDOW+timedelta(seconds=10)).isoformat()).channel
        masked=channel_source_fields(row,now=NOW,valid_until=NOW+timedelta(seconds=20))
        self.assertEqual(masked['title'],'profile')
        self.assertIsNone(masked['country_code'])

    def test_facets_withhold_stale_or_unknown_availability(self):
        for clock in (None,EXPIRED,'infinity','-infinity',(NOW+timedelta(seconds=1)).isoformat()):
            with self.subTest(clock=clock):
                result=build_channel_filter_facets(Stored(availability=clock),'channel',now=NOW)
                self.assertEqual(result['matched_count'],0)
                self.assertEqual(result['coverage']['video_rows_withheld_stale'],1)
                self.assertFalse(result['coverage']['complete'])
        result=build_channel_filter_facets(Stored(),'channel',now=NOW)
        self.assertEqual(result['matched_count'],1)
        self.assertTrue(result['coverage']['complete'])

    def test_repository_keeps_earlier_join_witness_deadline(self):
        repository=DatabaseRepository(None,read_clock=lambda:NOW)
        earlier=NOW+timedelta(seconds=10)
        rows=[dict(source_expires_at=(NOW+timedelta(days=1)).isoformat()),
              dict(source_expires_at=earlier.isoformat())]
        with capture_read_lifetime() as lifetime:
            self.assertEqual(repository.admit_source_deadlines(rows),rows)
        self.assertEqual(lifetime.expires_at,earlier)
        for deadline in (NOW,NOW-timedelta(seconds=1)):
            with self.assertRaises(HotSourceReadExpired):
                repository.admit_source_deadlines([dict(source_expires_at=deadline.isoformat())])


if __name__=='__main__':
    unittest.main()
