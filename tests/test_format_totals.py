"""Views by format add up public videos of the whole stored list (RR-02)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from channel_explorer import FORMAT_TOTAL_SCAN_LIMIT, SCAN_LIMIT, build_channel_format_totals  # noqa: E402

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def video(key, *, views=10, status="public", availability_age=timedelta(0), video_type_channel="c"):
    return dict(video_id=key, channel_id=video_type_channel, title=key, published_at=iso(NOW - timedelta(days=20)),
                checked_at=iso(NOW), metadata_checked_at=iso(NOW),
                availability_checked_at=iso(NOW - availability_age), availability_status=status,
                duration=600, views=views, like_count=1, comment_count=1, category_id="10", tags=[],
                stream_actual_end_at=iso(NOW - timedelta(days=1)))


class Repository:
    """One channel: the long list and, for typed totals, the Shorts list."""

    def __init__(self, long_videos, short_videos=()):
        self.lists = {"long": list(long_videos), "short": list(short_videos), "stream": []}
        self.videos = {row["video_id"]: row for row in [*long_videos, *short_videos]}

    def load_channel(self, *args, **kwargs):
        return dict(channel_id="c", title="Channel", channel_metadata_status="ok",
                    channel_metadata_checked_at=iso(NOW))

    def load_inventory(self, channel_id, *, video_type, limit):
        rows = [dict(video_id=row["video_id"], source_checked_at=iso(NOW),
                     published_at=iso(NOW - timedelta(days=20))) for row in self.lists[video_type]]
        return deepcopy(rows[:limit])

    def load_format_inventory(self, channel_id, *, video_type, limit):
        return self.load_inventory(channel_id, video_type=video_type, limit=limit)

    def load_long_inventory(self, channel_id, *, limit):
        return self.load_inventory(channel_id, video_type="long", limit=limit)

    def count_canonical_inventory(self, channel_id, *, video_type, exclude_waiting=False):
        return len(self.lists[video_type])

    def load_videos_by_ids(self, ids, **kwargs):
        return deepcopy([self.videos[key] for key in ids if key in self.videos])

    def load_list_inventory(self, channel_id, *, limit):
        return dict(complete=False,unique_video_count=None,source_expires_at=None,
                    formats={kind:dict(status="not_collected",listed_video_count=None,source_expires_at=None)
                             for kind in ("long","short","stream")})

    def load_inventory_state(self, channel_id, *, video_type):
        if not self.lists[video_type]:
            return None
        return dict(inventory_status="completed", source_checked_at=iso(NOW), product_exposure_ready=True)

    def load_type_resolutions(self, video_ids, *, video_type):
        return [dict(video_id=key, resolution_status="resolved", canonical_video_type=video_type,
                     manual_valid=False, present_membership_types=[video_type],
                     present_membership_channels=["c"]) for key in video_ids]


def counts_add_up(fmt, coverage):
    return (fmt["known_view_count"] + fmt["missing_view_count"] + coverage.get("unavailable_excluded_count", 0)
            == fmt["scanned_inventory_count"])


class FormatTotalTests(unittest.TestCase):
    def test_a_channel_above_the_catalogue_scan_limit_gets_its_total(self):
        self.assertGreaterEqual(FORMAT_TOTAL_SCAN_LIMIT, 1_000_000)
        videos = [video(f"v{index:05d}") for index in range(SCAN_LIMIT + 1402)]
        result = build_channel_format_totals(Repository(videos), "c", now=NOW)
        long = result["formats"]["long"]
        self.assertEqual(long["status"], "ready")
        self.assertEqual(long["views"], 10 * len(videos))
        self.assertEqual(long["scanned_inventory_count"], len(videos))
        self.assertTrue(result["coverage"]["inventory_complete"])

    def test_waiting_video_does_not_make_other_published_totals_incomplete(self):
        class WaitingRepository(Repository):
            def count_canonical_inventory(self, channel_id, *, video_type, exclude_waiting=False):
                # The catalogue already excludes the single waiting publication.
                ordinary = len(self.lists[video_type])
                return ordinary if exclude_waiting else ordinary + 1
        result = build_channel_format_totals(WaitingRepository([video("a",views=12)],
            [video("s",views=34)]),"c",now=NOW,include_typed=True)
        self.assertEqual((result["formats"]["long"]["status"],result["formats"]["long"]["views"]),("ready",12))
        self.assertEqual((result["formats"]["short"]["status"],result["formats"]["short"]["views"]),("ready",34))

    def test_a_video_no_longer_public_leaves_the_sum_without_hiding_it(self):
        videos = [video("a", views=100), video("b", views=50), video("c", views=7, status="private")]
        result = build_channel_format_totals(Repository(videos), "c", now=NOW)
        long, coverage = result["formats"]["long"], result["coverage"]
        self.assertEqual((long["status"], long["views"]), ("ready", 150))
        self.assertEqual(coverage["unavailable_excluded_count"], 1)
        self.assertEqual(long["missing_view_count"], 0)
        self.assertTrue(counts_add_up(long, coverage))

    def test_an_unconfirmed_availability_still_withholds_the_total(self):
        videos = [video("a"), video("b", availability_age=timedelta(days=30))]
        result = build_channel_format_totals(Repository(videos), "c", now=NOW)
        long, coverage = result["formats"]["long"], result["coverage"]
        self.assertEqual((long["status"], long["views"]), ("incomplete", None))
        self.assertEqual(coverage["availability_withheld_count"], 1)
        self.assertEqual(coverage["unavailable_excluded_count"], 0)
        self.assertTrue(counts_add_up(long, coverage))

    def test_automatic_long_short_resolution_counts_only_the_selected_format(self):
        class AutomaticRepository(Repository):
            def load_type_resolutions(self, video_ids, *, video_type):
                return [dict(video_id=key,resolution_status="resolved",canonical_video_type=video_type,
                             manual_valid=False,automatic_valid=True,
                             present_membership_types=["long","short"],present_membership_channels=["c"])
                        for key in video_ids]
        repo=AutomaticRepository([video("long",views=12)],[video("short",views=34)])
        result=build_channel_format_totals(repo,"c",now=NOW,include_typed=True)
        self.assertEqual((result["formats"]["long"]["views"],result["formats"]["short"]["views"]),(12,34))
        for kind in ("long","short"):
            self.assertEqual(result["formats"][kind]["status"],"ready")

    def test_shorts_follow_the_same_public_rule(self):
        shorts = [video("s1", views=30), video("s2", views=5, status="private")]
        result = build_channel_format_totals(Repository([video("a")], shorts), "c", now=NOW, include_typed=True)
        short = result["formats"]["short"]
        self.assertEqual((short["status"], short["views"]), ("ready", 30))
        self.assertEqual(short["coverage"]["unavailable_excluded_count"], 1)
        self.assertEqual(short["coverage"]["scan_limit"], FORMAT_TOTAL_SCAN_LIMIT)
        self.assertTrue(counts_add_up(short, short["coverage"]))


if __name__ == "__main__":
    unittest.main()
