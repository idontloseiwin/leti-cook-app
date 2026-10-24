"""Application naming keeps compatibility at user-controlled import boundaries."""
import unittest
from unittest.mock import patch

from test_response_item_expiry import Catalogue, NOW
from channel_baseline import build_channel_baseline


class ApplicationNameTests(unittest.TestCase):
    def test_live_baseline_emits_the_current_marker_with_unchanged_source_clocks(self):
        with patch("channel_baseline.youtube_video_category_name", return_value="Music"):
            result = build_channel_baseline(Catalogue(), "c", now=NOW)
        age = result["content_breakdown"]["catalogue_age"]
        self.assertEqual(age["source"], "leti_cook.video_age_from_published_at")
        self.assertEqual(age["sample_count"], 2)
        self.assertEqual(age["source_expires_at"], "2026-10-10T12:00:00Z")
        self.assertFalse(result["source"]["youtube_requests"])
        self.assertFalse(result["source"]["database_writes"])


if __name__ == "__main__":
    unittest.main()
