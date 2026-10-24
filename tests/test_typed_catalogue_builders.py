"""Typed catalogue parity and independent server admission, entirely offline."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from channel_explorer import build_channel_explorer
from channel_video_growth import build_channel_video_growth
from catalogue_filters import build_channel_filter_facets

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
FRESH = NOW - timedelta(hours=1)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class TypedCatalogueRepository:
    """Small independent per-type store, also reusable for route/cache tests."""
    def __init__(self, count=6):
        self.rows = {}
        self.inventories = {}
        self.states = {}
        self.proofs = {}
        self.growth_reads = []
        self.inventory_reads = []
        for video_type, prefix in (("long", "l"), ("short", "h"), ("stream", "s")):
            self.states[video_type] = dict(channel_id="channel", video_type=video_type,
                inventory_status="completed", product_exposure_ready=False,
                source_checked_at=iso(FRESH), inventory_completed_at=iso(FRESH),
                source_expires_at=iso(NOW + timedelta(days=2)))
            inventory = self.inventories[video_type] = []
            for index in range(count):
                video_id = prefix + str(index).zfill(10)
                self.rows[video_id] = dict(video_id=video_id, channel_id="channel", title=f"Video {index:04}",
                    views=(index + 1) * 100, like_count=index * 10, comment_count=index, category_id="22",
                    published_at=iso(NOW - timedelta(days=10, minutes=index)), duration=f"PT{index + 1}M",
                    tags=["tag"] * (index % 3), checked_at=iso(FRESH), metadata_checked_at=iso(FRESH),
                    availability_status="public", availability_checked_at=iso(FRESH), publication_status=None,
                    stream_actual_end_at=iso(NOW-timedelta(days=9)) if video_type == "stream" else None,
                    source_expires_at=iso(NOW+timedelta(days=2)))
                inventory.append(dict(video_id=video_id, channel_id="channel", video_type=video_type,
                    published_at=self.rows[video_id]["published_at"], source_checked_at=iso(FRESH),
                    source_expires_at=iso(NOW+timedelta(days=2))))
                self.proofs[video_id] = dict(video_id=video_id, canonical_channel_id="channel",
                    canonical_video_type=video_type, resolution_status="resolved",
                    source_expires_at=iso(NOW+timedelta(days=2)))

    def load_channel(self, channel_id, *, columns):
        return dict(channel_id=channel_id, title="Channel", is_hidden=False,
            channel_metadata_status="ok", channel_metadata_checked_at=iso(FRESH))

    def load_inventory_state(self, channel_id, *, video_type):
        return deepcopy(self.states[video_type])

    def load_inventory(self, channel_id, *, video_type, limit):
        self.inventory_reads.append(video_type)
        return deepcopy(self.inventories[video_type][:limit])

    def load_long_inventory(self, channel_id, *, limit):
        return self.load_inventory(channel_id, video_type="long", limit=limit)

    def iter_inventory_pages(self, channel_id, *, video_type="long"):
        self.inventory_reads.append(video_type)
        rows = self.inventories[video_type]
        for offset in range(0, len(rows), 4):
            yield deepcopy(rows[offset:offset+4])

    def iter_long_inventory_pages(self, channel_id):
        return self.iter_inventory_pages(channel_id)

    def load_type_resolutions(self, ids, *, video_type):
        return deepcopy([self.proofs[key] for key in ids if key in self.proofs])

    def load_videos_by_ids(self, ids, *, columns):
        fields = set(columns.split(",")) | {"source_expires_at"}
        return deepcopy([{field: value for field, value in self.rows[key].items() if field in fields}
                         for key in ids if key in self.rows])

    def load_video_view_growth(self, channel_id, ids, *, as_of, video_type="long", recorded_from=None):
        self.growth_reads.append((video_type, list(ids)))
        return self.load_frozen_video_growth(ids)

    def load_frozen_video_growth(self, ids):
        rows = [dict(video_id=key, growth_status="partial", observed_start_at=iso(FRESH-timedelta(days=7)),
            observed_end_at=iso(FRESH), observed_current_views=self.rows[key]["views"],
            actual_interval_seconds=7*86400, tracked_days=7, boundary_distance_seconds=None,
            reason="full_window_history_unavailable", delta_views=(self.rows[key]["views"] or 0)//2,
            source_expires_at=iso(NOW+timedelta(days=2))) for key in ids]
        for row in rows:
            if row["observed_current_views"] is None:
                row.update(growth_status="unavailable", observed_start_at=None, actual_interval_seconds=None,
                           tracked_days=None, delta_views=None, reason="current_views_missing")
        return rows


BUILDERS = (build_channel_explorer, build_channel_video_growth, build_channel_filter_facets)


def build(builder, repository, video_type, **changes):
    return builder(repository, "channel", video_type=video_type, typed_catalogue_read=True,
        full_scope=True, now=NOW, **changes)


class TypedCatalogueBuildersTests(unittest.TestCase):
    def test_filter_and_contextual_facet_matrix_matches_long_for_each_type(self):
        static = [dict(q="0002"), dict(category_id="25"), dict(min_views=200, max_views=400),
            dict(min_duration_seconds=120, max_duration_seconds=240),
            dict(published_from="2026-10-09", published_before="2026-10-10")]
        extra = [dict(bounds={"min_likes": "10", "max_likes": "30"}),
            dict(bounds={"min_comments": "1", "max_comments": "3"}),
            dict(bounds={"min_title_length": "20"}), dict(bounds={"min_tag_count": "2"}),
            dict(bounds={"min_like_rate": "5"}), dict(bounds={"min_comment_rate": "0.5"}),
            dict(view_buckets=["under_1k"]), dict(duration_buckets=["under_5m"]),
            dict(like_buckets=["under_100"]), dict(comment_buckets=["under_10"]),
            dict(title_length_buckets=["40_60"]), dict(tag_count_buckets=["zero"]),
            dict(published_years=[2025]), dict(published_older_than=2026),
            dict(cohort={"kind": "overview", "published_from": "2026-01-01T00:00:00Z",
                         "published_through": iso(NOW)})]
        cases = [(dict(**custom, catalogue_filters='{"version":1}'), False) for custom in static]
        cases += [(dict(catalogue_filters=json.dumps(dict(version=1, **filters))), False) for filters in extra]
        cases += [(dict(catalogue_filters=json.dumps({"version": 1, "growth": filters})), True)
                  for filters in ({"min_views_gained": "200"}, {"max_views_gained": "100"},
                                  {"min_tracked_days": "7"}, {"full_history": True})]
        for changes, dynamic in cases:
            expected = None
            for video_type in ("long", "short", "stream"):
                with self.subTest(video_type=video_type, changes=changes):
                    repository = TypedCatalogueRepository()
                    for prefix in ("l", "h", "s"):
                        repository.rows[prefix+"0000000001"]["category_id"] = "25"
                        repository.rows[prefix+"0000000002"]["title"] = "A" * 50
                        repository.rows[prefix+"0000000003"]["published_at"] = "2025-10-01T00:00:00Z"
                        repository.rows[prefix+"0000000005"].update(views=None, like_count=None, comment_count=None,
                            duration=None, title=None, tags=None)
                    builder = build_channel_video_growth if dynamic else build_channel_explorer
                    catalogue = build(builder, repository, video_type, **changes)
                    facets = build(build_channel_filter_facets, repository, video_type,
                                   frozen_growth=dynamic, **changes)
                    indices = [row["video_id"][1:] for row in catalogue["videos"]]
                    actual = (indices, facets["matched_count"], facets["facets"])
                    if expected is None:
                        expected = actual
                    self.assertEqual(actual, expected)
                    self.assertEqual(facets["matched_count"], len(indices))

    def test_all_sort_fields_keep_unknown_values_last_in_both_directions(self):
        for field in ("published_at", "views", "like_count", "comment_count", "duration", "title", "views_gained"):
            for order in ("asc", "desc"):
                expected = None
                for video_type in ("long", "short", "stream"):
                    with self.subTest(field=field, order=order, video_type=video_type):
                        repository = TypedCatalogueRepository()
                        key = repository.inventories[video_type][-1]["video_id"]
                        repository.rows[key]["views" if field == "views_gained" else field] = None
                        builder = build_channel_video_growth if field == "views_gained" else build_channel_explorer
                        result = build(builder, repository, video_type, sort_by=field, sort_order=order)
                        indices = [row["video_id"][1:] for row in result["videos"]]
                        self.assertEqual(indices[-1], key[1:])
                        if expected is None:
                            expected = indices
                        self.assertEqual(indices, expected)

    def test_full_catalogue_filters_and_sort_before_page_have_three_type_parity(self):
        for video_type in ("long", "short", "stream"):
            for builder in BUILDERS[:2]:
                with self.subTest(video_type=video_type, builder=builder.__name__):
                    repository = TypedCatalogueRepository()
                    result = build(builder, repository, video_type, catalogue_filters=json.dumps({
                        "version": 1, "bounds": {"min_likes": "20"}}), sort_by="views", sort_order="desc", limit=2, offset=1)
                    self.assertEqual([r["views"] for r in result["videos"]], [500, 400])
                    self.assertEqual(result["pagination"]["matched_count"], 4)
                    self.assertTrue(result["coverage"]["complete"])
                    self.assertEqual(result["video_type"], video_type)
                    self.assertEqual(repository.inventory_reads, [video_type])
                    self.assertEqual(result["coverage"]["product_exposure_ready"], video_type == "long")
                    self.assertTrue(result["coverage"]["catalogue_read_ready"])
                    if builder is build_channel_video_growth:
                        self.assertEqual(repository.growth_reads[0][0], video_type)

    def test_existing_nonlong_gate_stays_closed_without_explicit_catalogue_acceptance(self):
        for builder in BUILDERS:
            for video_type in ("short", "stream"):
                repository = TypedCatalogueRepository()
                result = builder(repository, "channel", video_type=video_type, now=NOW)
                self.assertFalse(result["coverage"]["catalogue_read_ready"])
                self.assertEqual(repository.inventory_reads, [])

    def test_missing_stale_wrong_identity_or_incomplete_scan_never_opens_typed_read(self):
        changes = ({"inventory_status": "partial"}, {"inventory_completed_at": None},
            {"source_checked_at": iso(NOW-timedelta(days=30))}, {"channel_id": "other"},
            {"video_type": "long"}, {"source_expires_at": iso(NOW)},
            {"source_expires_at": iso(NOW+timedelta(seconds=1))})
        for builder in BUILDERS:
            for fields in changes:
                with self.subTest(builder=builder.__name__, fields=fields):
                    repository = TypedCatalogueRepository()
                    repository.states["short"].update(fields)
                    result = build(builder, repository, "short", source_valid_until=NOW+timedelta(seconds=5))
                    self.assertFalse(result["coverage"]["complete"])
                    self.assertFalse(result["coverage"]["catalogue_read_ready"])
                    self.assertEqual(repository.inventory_reads, [])

    def test_canonical_type_channel_and_expiry_are_required(self):
        for builder in BUILDERS:
            repository = TypedCatalogueRepository()
            ids = [row["video_id"] for row in repository.inventories["short"]]
            repository.proofs[ids[0]]["canonical_video_type"] = "long"
            repository.proofs[ids[1]]["canonical_channel_id"] = "other"
            repository.proofs[ids[2]]["source_expires_at"] = iso(NOW)
            result = build(builder, repository, "short")
            self.assertEqual(result["coverage"]["type_resolution_withheld"], 3)
            self.assertFalse(result["coverage"]["complete"])
            if "videos" in result:
                self.assertEqual({row["video_id"] for row in result["videos"]}, set(ids[3:]))
            else:
                self.assertEqual(result["matched_count"], 3)

    def test_waiting_and_unfinished_streams_are_withheld(self):
        for builder in BUILDERS:
            repository = TypedCatalogueRepository()
            ids = [row["video_id"] for row in repository.inventories["stream"]]
            repository.rows[ids[0]]["stream_actual_end_at"] = None
            repository.rows[ids[1]]["stream_actual_end_at"] = iso(NOW)
            repository.rows[ids[2]]["publication_status"] = "awaiting_completion"
            result = build(builder, repository, "stream")
            self.assertEqual(result["coverage"]["video_rows_withheld_lifecycle"], 3)
            self.assertFalse(result["coverage"]["complete"])

    def test_unknown_private_and_stale_availability_never_become_public(self):
        for builder in BUILDERS:
            for video_type in ("long", "short", "stream"):
                repository = TypedCatalogueRepository()
                ids = [row["video_id"] for row in repository.inventories[video_type]]
                repository.rows[ids[0]]["availability_status"] = "private"
                repository.rows[ids[1]]["availability_checked_at"] = None
                repository.rows[ids[2]]["availability_checked_at"] = iso(NOW-timedelta(days=30))
                repository.rows[ids[3]]["availability_status"] = None
                result = build(builder, repository, video_type)
                actual = {row["video_id"] for row in result.get("videos", [])}
                if "videos" in result:
                    self.assertEqual(actual, set(ids[4:]))
                else:
                    self.assertEqual(result["matched_count"], 2)
                self.assertFalse(result["coverage"]["complete"])

    def test_wrong_current_row_channel_is_withheld_even_with_valid_type_proof(self):
        for builder in BUILDERS:
            repository = TypedCatalogueRepository()
            repository.rows["h0000000000"]["channel_id"] = "other"
            result = build(builder, repository, "short")
            self.assertEqual(result["coverage"]["video_rows_withheld_identity"], 1)
            self.assertFalse(result["coverage"]["complete"])

    def test_contextual_facets_and_frozen_growth_use_same_typed_scope(self):
        for video_type in ("long", "short", "stream"):
            filters = json.dumps({"version": 1, "bounds": {"min_likes": "20"},
                "growth": {"min_views_gained": "200"}})
            repository = TypedCatalogueRepository()
            result = build(build_channel_filter_facets, repository, video_type,
                catalogue_filters=filters, frozen_growth=True)
            self.assertEqual(result["matched_count"], 3)
            self.assertEqual(sum(row["count"] for row in result["facets"]["views"]), 3)
            self.assertEqual(repository.inventory_reads, [video_type])
            self.assertTrue(result["coverage"]["complete"])


if __name__ == "__main__":
    unittest.main()
