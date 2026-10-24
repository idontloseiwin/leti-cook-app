"""Response item lifetimes preserve exact independent and shared source proofs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from hot_storage import HOT_DATA_WINDOW, capture_read_lifetime, register_read_deadline
from source_expiry import minimum_expiry, source_expiry, captured_source_call
from channel_explorer import build_channel_explorer, build_channel_profile, build_channel_format_totals
from channel_baseline import build_channel_baseline
from channel_timeline import _timeline_points, _publication_events
from catalogue_filters import build_channel_filter_facets
from video_history import build_video_history
from video_launch import build_video_launch
from channel_derived_insights import build_channel_derived_insights
from channel_video_growth import normalize_growth_for_video
from channel_counter_dynamics import _metric_result

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
def iso(value):
    return value.isoformat().replace("+00:00", "Z")

class Catalogue:
    def __init__(self):
        self.inventory = [dict(video_id=key, source_checked_at=iso(NOW-timedelta(days=days)),
                               published_at=iso(NOW-timedelta(days=20))) for key,days in (("a",28),("b",1))]
        self.videos = [dict(video_id=key, title=key, published_at=iso(NOW-timedelta(days=20)),
                            checked_at=iso(NOW), metadata_checked_at=iso(NOW), availability_checked_at=iso(NOW),
                            availability_status="public", duration=600, views=20,like_count=2,comment_count=1,
                            category_id="10",tags=[]) for key in ("a","b")]
    def load_channel(self, *args, **kwargs):
        return dict(channel_id="c",title="Channel",channel_metadata_status="ok",channel_metadata_checked_at=iso(NOW))
    def load_recent_public_video_counts(self, *args, **kwargs):return []
    def load_inventory(self,*args,**kwargs):return deepcopy(self.inventory)
    load_long_inventory=load_inventory
    load_format_inventory=load_inventory
    def count_canonical_inventory(self,*args,**kwargs):return len(self.inventory)
    def load_videos_by_ids(self, ids, **kwargs):return deepcopy([row for row in self.videos if row["video_id"] in ids])
    def load_snapshots_by_video_ids(self, ids, **kwargs):
        return [dict(id=i*2+j,video_id=key,recorded_at=iso(NOW-timedelta(hours=2-j)),views=10+j*10,
                     like_count=j+1,comment_count=1) for i,key in enumerate(ids,1) for j in (0,1)]

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

    def test_catalogue_membership_and_labels_have_individual_deadlines(self):
        repo=Catalogue()
        with patch("channel_explorer.youtube_video_category_name",return_value="Music") as labels:
            result=build_channel_explorer(repo,"c",now=NOW)
        by_id={row["video_id"]:row for row in result["videos"]}
        self.assertEqual(by_id["a"]["source_expires_at"],iso(NOW+timedelta(days=1)))
        self.assertEqual(by_id["b"]["source_expires_at"],iso(NOW+timedelta(days=28)))
        labels.assert_called_once()
        self.assertEqual(result["options"]["categories"][0]["source_expires_at"],by_id["a"]["source_expires_at"])
        self.assertEqual(result["channel"]["source_expires_at"],iso(NOW+HOT_DATA_WINDOW))
        self.assertEqual(build_channel_profile(repo,"c",now=NOW)["channel"]["source_expires_at"],iso(NOW+HOT_DATA_WINDOW))

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

    def test_launch_real_conflict_proof_and_independent_unreached_horizon(self):
        video=dict(video_id="v",checked_at=iso(NOW),published_at=iso(NOW-timedelta(days=3)),views=20)
        rows=[dict(id=1,video_id="v",recorded_at=iso(NOW-timedelta(days=27)),views=1),
              dict(id=2,video_id="v",recorded_at=iso(NOW-timedelta(days=2)),views=10)]
        class Repository:
            def load_snapshots_by_video_ids(self,*args,**kwargs):return deepcopy(rows)
        context=dict(channel_id="c",video_id="v",browse_session="x",browse_created_at=iso(NOW),
            expires_at=iso(NOW+timedelta(seconds=30)),catalogue=dict(video_type="long",channel=dict(channel_id="c"),videos=[video],
            coverage=dict(scan_mode="full_catalogue",truncated=False,inventory_complete=True,scanned_count=1),
            pagination=dict(offset=0,returned_count=1,matched_count=1,has_more_in_scan=False)))
        result=build_video_launch(Repository(),context,now=NOW)
        first,later,_=result["horizons"]
        # All observations participate in duplicate/conflict proof for reached milestone.
        self.assertEqual(first["source_expires_at"],iso(NOW+timedelta(days=2)))
        self.assertEqual(first["target"]["observed_at"],rows[1]["recorded_at"])
        self.assertEqual(later["target"]["status"],"not_reached")
        self.assertEqual(later["source_expires_at"],iso(NOW+HOT_DATA_WINDOW))
        self.assertEqual(later["target"]["source_expires_at"],later["source_expires_at"])

    def test_catalogue_label_earlier_than_video_is_preserved(self):
        expiry=NOW+timedelta(seconds=6)
        def label(_):
            register_read_deadline(expiry)
            return "Music"
        with patch("channel_explorer.youtube_video_category_name",side_effect=label):
            result=build_channel_explorer(Catalogue(),"c",now=NOW)
        self.assertEqual({row["source_expires_at"] for row in result["videos"]},{iso(expiry)})

    def test_baseline_points_independent_but_sample_aggregates_atomic(self):
        with patch("channel_baseline.youtube_video_category_name",return_value="Music"):
            result=build_channel_baseline(Catalogue(),"c",now=NOW)
        points={row["video_id"]:row for row in result["publication_points"]}
        self.assertLess(points["a"]["source_expires_at"],points["b"]["source_expires_at"])
        expected=points["a"]["source_expires_at"]
        self.assertEqual(result["statistics"]["views"]["source_expires_at"],expected)
        self.assertEqual(result["publication_calendar"]["years"][0]["days"][0]["source_expires_at"],expected)
        self.assertEqual(result["distributions"]["views"][0]["source_expires_at"],expected)

    def test_facets_keep_real_membership_witness(self):
        result=build_channel_filter_facets(Catalogue(),"c",now=NOW)
        self.assertEqual(result["facets"]["views"][0]["source_expires_at"],iso(NOW+timedelta(days=1)))
        self.assertEqual(result["facets"]["published"]["years"][0]["source_expires_at"],iso(NOW+timedelta(days=1)))

    def test_history_intervals_use_two_points_without_recursive_expiry(self):
        stamps=[NOW-timedelta(days=27),NOW-timedelta(days=2),NOW]
        rows=[dict(id=i+1,video_id="v",recorded_at=iso(stamp),views=i*10,like_count=i,comment_count=i)
              for i,stamp in enumerate(stamps)]
        class Repository:
            def load_snapshots_by_video_ids(self,*args,**kwargs):return deepcopy(rows)
        context=dict(channel_id="c",video_id="v",browse_session="x",browse_created_at=iso(NOW),
                     expires_at=iso(NOW+timedelta(seconds=30)),video=dict(checked_at=iso(NOW),views=20,like_count=2,comment_count=2))
        result=build_video_history(Repository(),context,now=NOW)
        self.assertEqual(result["intervals"][0]["source_expires_at"],iso(stamps[0]+HOT_DATA_WINDOW))
        self.assertEqual(result["intervals"][1]["source_expires_at"],iso(stamps[1]+HOT_DATA_WINDOW))
        self.assertEqual(result["points"][2]["source_expires_at"],iso(stamps[2]+HOT_DATA_WINDOW))
        self.assertEqual(result["interaction_metrics"]["likes"]["intervals"][1]["source_expires_at"],iso(stamps[1]+HOT_DATA_WINDOW))
        # The browse cursor's 30-second validity is not the item's source age.
        self.assertGreater(result["points"][2]["source_expires_at"],context["expires_at"])

    def test_timeline_correction_dependency_does_not_cascade(self):
        stamps=[NOW-timedelta(days=2),NOW-timedelta(days=1),NOW]
        early=NOW+timedelta(seconds=5)
        rows=_timeline_points([(stamp,i*10,stamp) for i,stamp in enumerate(stamps)],
            expected_interval_seconds=86400,correction_times={stamps[0]},
            source_expiries={stamps[0]:iso(early),stamps[1]:iso(stamps[1]+HOT_DATA_WINDOW),stamps[2]:iso(stamps[2]+HOT_DATA_WINDOW)})
        self.assertEqual(rows[0]["source_expires_at"],iso(early))
        self.assertEqual(rows[1]["source_expires_at"],iso(stamps[0]+HOT_DATA_WINDOW))
        self.assertEqual(rows[2]["source_expires_at"],iso(stamps[1]+HOT_DATA_WINDOW))

    def test_publication_event_combines_title_and_membership_evidence(self):
        class Repository:
            def load_publication_events(self,*args,**kwargs):
                return [dict(video_id="v",published_at=iso(NOW),source_expires_at=iso(NOW+timedelta(hours=1)))]
            def load_video_titles(self,*args,**kwargs):
                return {"v":dict(title="Name",source_expires_at=iso(NOW+timedelta(minutes=1)))}
        rows,_=_publication_events(Repository(),"c",requested_start=None,requested_end=NOW)
        self.assertEqual(rows[0]["title"],"Name")
        self.assertEqual(rows[0]["source_expires_at"],iso(NOW+timedelta(minutes=1)))

    def test_derived_change_preserves_old_membership(self):
        result=build_channel_derived_insights(Catalogue(),"c",now=NOW)
        by_id={row["video_id"]:row for row in result["changes"]}
        self.assertEqual(by_id["a"]["source_expires_at"],iso(NOW+timedelta(days=1)))
        self.assertGreater(by_id["b"]["source_expires_at"],by_id["a"]["source_expires_at"])

    def test_format_total_uses_inventory_not_only_fresh_counters(self):
        result=build_channel_format_totals(Catalogue(),"c",now=NOW)
        self.assertEqual(result["formats"]["long"]["status"],"ready")
        self.assertEqual(result["formats"]["long"]["views"],40)
        self.assertEqual(result["formats"]["long"]["source_expires_at"],iso(NOW+timedelta(days=1)))

    def test_growth_keeps_earlier_database_witness(self):
        early=NOW+timedelta(hours=1)
        raw=dict(growth_status="partial",observed_start_at=iso(NOW-timedelta(days=2)),observed_end_at=iso(NOW),
                 observed_current_views=20,delta_views=10,actual_interval_seconds=172800,tracked_days=2,
                 reason="full_window_history_unavailable",source_expires_at=iso(early))
        result=normalize_growth_for_video(dict(checked_at=iso(NOW),views=20),raw,now=NOW)
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["delta_views"],10)
        self.assertEqual(result["source_expires_at"],iso(early))

    def test_counter_result_uses_baseline_witness_and_drops_rejected_old_row(self):
        baseline=dict(recorded_at=iso(NOW-timedelta(days=7)),views=10,source_expires_at=iso(NOW+timedelta(hours=1)))
        latest=dict(recorded_at=iso(NOW),views=20)
        result=_metric_result("views",latest=latest,baseline=baseline,generated_at=NOW,boundary_at=NOW-timedelta(days=7))
        self.assertEqual(result["source_expires_at"],baseline["source_expires_at"])
        baseline["recorded_at"]=iso(NOW-HOT_DATA_WINDOW-timedelta(days=1))
        result=_metric_result("views",latest=latest,baseline=baseline,generated_at=NOW,boundary_at=NOW-timedelta(days=7))
        self.assertEqual(result["source_expires_at"],iso(NOW+HOT_DATA_WINDOW))

if __name__ == "__main__":unittest.main()
