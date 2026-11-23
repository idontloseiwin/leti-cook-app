"""Item expiry in catalogue responses."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from hot_storage import HOT_DATA_WINDOW, capture_read_lifetime, register_read_deadline
from source_expiry import source_expiry, captured_source_call
from channel_explorer import build_channel_profile

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
def iso(value):
    return value.isoformat().replace("+00:00", "Z")

class ItemExpiryTests(unittest.TestCase):
    def test_named_clock_never_loses_earlier_view_witness(self):
        row=dict(checked_at=iso(NOW),source_expires_at=iso(NOW+timedelta(seconds=3)))
        self.assertEqual(source_expiry(row,"checked_at"),row["source_expires_at"])
        self.assertIsNone(source_expiry({}))
        with self.assertRaises(ValueError):source_expiry(dict(source_expires_at="broken"))

    def test_category_capture_merges_only_successful_used_read(self):
        early=NOW+timedelta(seconds=5)
        def label():
            register_read_deadline(early)
            return "Music"
        with capture_read_lifetime() as outer:
            value,expiry=captured_source_call(label)
        self.assertEqual((value,expiry),("Music",iso(early)))
        self.assertEqual(outer.expires_at,early)
        def failure():
            register_read_deadline(NOW)
            raise RuntimeError()
        with capture_read_lifetime() as outer:
            with self.assertRaises(RuntimeError):captured_source_call(failure)
        self.assertIsNone(outer.expires_at)
        with capture_read_lifetime() as outer:
            value, expiry = captured_source_call(lambda: "Category 10")
        self.assertEqual(value, "Category 10")
        self.assertIsNone(expiry)
        self.assertIsNone(outer.expires_at)

    def test_masked_profile_clock_cannot_expire_fresh_country(self):
        class Repository:
            def load_channel(self, *args, **kwargs):
                return dict(channel_id="c", title=None, channel_metadata_status="ok",
                    channel_metadata_checked_at=iso(NOW-HOT_DATA_WINDOW-timedelta(days=1)),
                    country_code="GB",country_collected_at=iso(NOW),source_expires_at=iso(NOW+HOT_DATA_WINDOW))
        result=build_channel_profile(Repository(),"c",now=NOW)
        self.assertIsNone(result["channel"]["title"])
        self.assertEqual(result["channel"]["country_code"],"GB")
        self.assertEqual(result["channel"]["source_expires_at"],iso(NOW+HOT_DATA_WINDOW))

    def test_profile_channel_keeps_its_own_clock(self):
        class Repository:
            def load_channel(self, *args, **kwargs):
                return dict(channel_id="c",title="Channel",channel_metadata_status="ok",channel_metadata_checked_at=iso(NOW))
            def load_recent_public_video_counts(self, *args, **kwargs):return []
        self.assertEqual(build_channel_profile(Repository(),"c",now=NOW)["channel"]["source_expires_at"],iso(NOW+HOT_DATA_WINDOW))

if __name__ == "__main__":unittest.main()
