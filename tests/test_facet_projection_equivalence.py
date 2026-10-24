"""Output contracts for contextual facets, independent of evaluation strategy."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from catalogue_filters import build_channel_filter_facets
from channel_explorer import ChannelExplorerQueryError

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
FRESH = NOW - timedelta(hours=1)

def iso(value):
    return value.isoformat().replace("+00:00", "Z")

def row(key, views, duration, likes, comments, tags, published, hours):
    return dict(video_id=key, title="alpha " + key if key != "b" else "beta", views=views,
        duration=duration, like_count=likes, comment_count=comments, tags=tags,
        category_id="22" if key != "b" else "25", published_at=published,
        checked_at=iso(FRESH), metadata_checked_at=iso(FRESH), availability_checked_at=iso(FRESH),
        availability_status="public", source_expires_at=iso(NOW + timedelta(hours=hours)))

def example_rows():
    return [row("a", 500, "PT2M", 5, 1, [], "2026-01-01T00:30:00Z", 1),
            row("b", 5000, "PT7M", 500, 50, ["one"], "2025-12-31T23:30:00Z", 2),
            row("c", 50000, "PT12M", 5000, 500, ["tag"] * 6, "2024-06-01T12:00:00Z", 3)]

class StoredFacets:
    def __init__(self, rows=None, growth=None, extra_inventory=()):
        self.rows = deepcopy(example_rows() if rows is None else rows)
        self.growth = deepcopy(growth or [])
        self.inventory = [dict(video_id=r["video_id"], source_checked_at=iso(FRESH),
                               source_expires_at=r["source_expires_at"]) for r in self.rows]
        self.inventory.extend(deepcopy(list(extra_inventory)))
        self.growth_reads = 0
    def load_channel(self, channel_id, *, columns):
        return dict(channel_id=channel_id, is_hidden=False, channel_metadata_status="ok",
                    channel_metadata_checked_at=iso(FRESH))
    def load_long_inventory(self, channel_id, *, limit):
        return deepcopy(self.inventory[:limit])
    def iter_long_inventory_pages(self, channel_id):
        for start in range(0, len(self.inventory), 2):
            yield deepcopy(self.inventory[start:start + 2])
    def load_videos_by_ids(self, ids, *, columns):
        return deepcopy([r for r in self.rows if r["video_id"] in ids])
    def load_frozen_video_growth(self, ids):
        self.growth_reads += 1
        return deepcopy([r for r in self.growth if r["video_id"] in ids])

def growth_row(video, *, partial=False, delta=50):
    days = 7 if partial else 28
    return dict(video_id=video["video_id"], growth_status="partial" if partial else "ready",
        observed_start_at=iso(FRESH-timedelta(days=days)), observed_end_at=iso(FRESH),
        observed_current_views=video["views"], actual_interval_seconds=days*86400,
        boundary_distance_seconds=None if partial else 0, tracked_days=days if partial else None,
        reason="full_window_history_unavailable" if partial else None, delta_views=delta,
        source_expires_at=iso(NOW+timedelta(minutes=30)))

def build(repository=None, *, filters=None, **custom):
    return build_channel_filter_facets(repository or StoredFacets(), "channel", now=NOW,
        catalogue_filters=json.dumps(dict(version=1, **(filters or {}))), **custom)

def counts(result, field):
    return {r["key"]: r["count"] for r in result["facets"][field]}

class FacetProjectionContracts(unittest.TestCase):
    def test_two_active_fields_exclude_only_their_own_predicate_and_expiry(self):
        value=build(filters={"view_buckets":["under_1k"], "duration_buckets":["5m_10m"]})
        self.assertEqual(value["matched_count"],0)
        self.assertEqual(counts(value,"views")["1k_10k"],1)
        self.assertEqual(sum(counts(value,"views").values()),1)
        self.assertEqual(counts(value,"duration")["under_5m"],1)
        self.assertEqual(sum(counts(value,"duration").values()),1)
        self.assertEqual(value["facets"]["views"][0]["source_expires_at"],iso(NOW+timedelta(hours=2)))
        self.assertEqual(value["facets"]["duration"][0]["source_expires_at"],iso(NOW+timedelta(hours=1)))
        self.assertEqual(sum(counts(value,"likes").values()),0)
        self.assertIsNone(value["facets"]["likes"][0]["source_expires_at"])
    def test_static_preset_and_custom_bound_keep_distinct_contexts(self):
        value=build(filters={"like_buckets":["under_100"],"bounds":{"min_comments":"10"}})
        self.assertEqual(value["matched_count"],0)
        self.assertEqual(counts(value,"likes")["100_1k"],1)
        self.assertEqual(counts(value,"likes")["1k_10k"],1)
        self.assertEqual(counts(value,"comments")["under_10"],1)
        self.assertEqual(sum(counts(value,"comments").values()),1)
    def test_publication_years_use_selected_zone_and_unfiltered_options(self):
        utc=build(filters={"published_years":[2025]})
        local=build(filters={"published_years":[2025],"time_zone":"America/New_York"})
        self.assertEqual((utc["matched_count"],local["matched_count"]),(1,2))
        self.assertEqual([(r["year"],r["count"]) for r in utc["facets"]["published"]["years"]],[(2026,1),(2025,1),(2024,1)])
        self.assertEqual([(r["year"],r["count"]) for r in local["facets"]["published"]["years"]],[(2025,2),(2024,1)])
    def test_unknown_metric_rows_remain_eligible_but_not_numeric_buckets(self):
        rows=example_rows()+[row("unknown","bad",{},None,False,[None],"not-a-date",4)]
        value=build(StoredFacets(rows))
        self.assertEqual(value["matched_count"],4)
        self.assertEqual(value["coverage"]["eligible_count"],4)
        self.assertEqual(sum(counts(value,"views").values()),3)
        self.assertEqual(sum(counts(value,"duration").values()),3)
        filtered=build(StoredFacets(rows),min_views=0)
        self.assertEqual(filtered["matched_count"],3)
        self.assertEqual(sum(counts(filtered,"views").values()),3)
    def test_growth_uses_frozen_evidence_for_all_self_excluding_facets(self):
        rows=example_rows();repo=StoredFacets(rows,[growth_row(rows[0]),growth_row(rows[1],partial=True,delta=5)])
        value=build(repo,filters={"growth":{"min_views_gained":"0"},"view_buckets":["under_1k"]},frozen_growth=True)
        self.assertEqual(value["matched_count"],1)
        self.assertEqual(sum(counts(value,"views").values()),2)
        self.assertEqual(counts(value,"views")["1k_10k"],1)
        self.assertEqual(value["facets"]["views"][0]["source_expires_at"],iso(NOW+timedelta(minutes=30)))
        self.assertEqual(repo.growth_reads,1)
        self.assertEqual(value["source"]["growth_evidence"],"same_browse_generation")
        with self.assertRaises(ChannelExplorerQueryError):
            build(repo,filters={"growth":{"min_views_gained":"0"}})
    def test_full_history_filter_excludes_partial_and_endpoint_mismatched_growth(self):
        rows=example_rows()
        evidence=[growth_row(rows[0]),growth_row(rows[1],partial=True),growth_row(rows[2])]
        evidence[2]["observed_current_views"]+=1
        value=build(StoredFacets(rows,evidence),filters={"growth":{"full_history":True}},
                    frozen_growth=True,full_scope=True)
        self.assertEqual(value["matched_count"],1)
        self.assertEqual(counts(value,"views")["under_1k"],1)
        self.assertEqual(sum(counts(value,"views").values()),1)
        self.assertTrue(value["coverage"]["complete"])
        self.assertEqual(value["coverage"]["eligible_count"],3)
        self.assertEqual(value["source"]["growth_evidence"],"same_browse_generation")
    def test_source_coverage_and_facets_agree_across_full_and_bounded_small_scope(self):
        rows=example_rows();rows[1]["availability_status"]="private";rows[2]["metadata_checked_at"]=iso(NOW-timedelta(days=30))
        missing=[dict(video_id="missing",source_checked_at=iso(FRESH),source_expires_at=iso(NOW+timedelta(hours=4)))]
        limited=build(StoredFacets(rows,extra_inventory=missing));full=build(StoredFacets(rows,extra_inventory=missing),full_scope=True)
        self.assertEqual(full["facets"],limited["facets"])
        self.assertEqual(full["matched_count"],1)
        self.assertEqual((full["coverage"]["missing_video_row_count"],full["coverage"]["video_rows_withheld_stale"],full["coverage"]["availability_excluded_count"]),(1,1,1))
        self.assertFalse(full["coverage"]["complete"])

if __name__ == "__main__":
    unittest.main()
