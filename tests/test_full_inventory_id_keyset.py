"""Full traversal order is internal; public ordering and source admission survive."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from catalogue_browse import RecordingCatalogueRepository, FrozenCatalogueRepository
from catalogue_scope import CatalogueReadBudget, CatalogueScopeError, CatalogueScopeRepository
from channel_baseline import build_channel_baseline
from channel_explorer import build_channel_explorer
from channel_video_growth import build_channel_video_growth
from catalogue_filters import build_channel_filter_facets
from db.reads import channel_explorer as owner
from db.reads.channel_explorer import ChannelExplorerRepository
from db.base.postgres import DatabaseUnavailable
from hot_storage import HotSourceReadExpired, capture_read_lifetime
from test_direct_pg_read_repositories import RecordingDatabase

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
CHECKED = (NOW - timedelta(hours=1)).isoformat()
EXPIRY = NOW + timedelta(days=1)
CHANNEL = "UC" + "x" * 22


def inventory(key, published=CHECKED, expiry=EXPIRY):
    return dict(video_id=key, published_at=published, source_checked_at=CHECKED,
                source_expires_at=expiry.isoformat())


class FullInventoryKeysetTests(unittest.TestCase):
    def repository(self, *pages):
        database = RecordingDatabase(*pages)
        return ChannelExplorerRepository(database, read_clock=lambda: NOW), database

    def test_full_scan_uses_one_protected_stream_and_requires_empty_eof(self):
        pages = [[inventory("A", None), inventory("b")], [inventory("z", None)], []]
        repository, database = self.repository(*pages)
        with patch.object(owner, "INVENTORY_PAGE_SIZE", 2):
            self.assertEqual(list(repository.iter_long_inventory_pages("channel'quoted")), pages[:-1])
        self.assertEqual(len(database.calls), 3)  # A short nonempty page is not EOF.
        for call in database.calls:
            self.assertEqual((call[1], call[4]), ("channel_explorer_inventory_page", 2))
            self.assertIn("FROM readable.hot_video_inventory", call[2])
            self.assertIn("ORDER BY video_id ASC", call[2])
            self.assertNotIn("public.", call[2])
            self.assertNotIn("published_at <", call[2])
            self.assertNotIn("channel'quoted", call[2])
        self.assertEqual(len(database.stream_declarations), 1)
        self.assertTrue(all(call[3] == ("channel'quoted", "long") for call in database.calls))
        self.assertEqual(database.stream_closes, 1)
        self.assertNotIn("video_id >", database.stream_declarations[0][0])

    def test_empty_first_page_is_known_empty(self):
        repository, database = self.repository([])
        self.assertEqual(list(repository.iter_long_inventory_pages(CHANNEL)), [])
        self.assertEqual(len(database.calls), 1)

    def test_invalid_rows_or_identity_fail_before_yield(self):
        for page in (None, {}, [None], [inventory(None)], [inventory(7)],
                     [inventory("")], [inventory("a b")], [inventory("a' OR true")],
                     [inventory("a"), inventory("a")]):
            with self.subTest(page=page):
                repository, database = self.repository(page)
                with self.assertRaises(CatalogueScopeError):
                    next(repository.iter_long_inventory_pages(CHANNEL))
                self.assertEqual(len(database.calls), 1)

    def test_repeated_boundary_anywhere_in_page_is_not_progress(self):
        for page in ([inventory("b")], [inventory("b"), inventory("c")]):
            with self.subTest(page=page):
                repository, database = self.repository([inventory("a"), inventory("b")], page)
                iterator = repository.iter_long_inventory_pages(CHANNEL)
                next(iterator)
                with self.assertRaisesRegex(CatalogueScopeError, "did_not_advance"):
                    next(iterator)
                self.assertEqual(len(database.calls), 2)

    def test_full_consumer_rejects_cross_page_duplicate_not_equal_to_cursor(self):
        repository, database = self.repository([inventory("a"), inventory("b")],
                                              [inventory("a"), inventory("c")])
        with self.assertRaisesRegex(CatalogueScopeError, "identity_invalid"):
            CatalogueScopeRepository(repository).load_full_long_inventory(CHANNEL)
        self.assertEqual(len(database.calls), 2)

    def test_text_order_belongs_to_database_collation(self):
        repository, _ = self.repository([inventory("z")], [inventory("A")], [])
        self.assertEqual([r["video_id"] for page in repository.iter_long_inventory_pages(CHANNEL)
                          for r in page], ["z", "A"])

    def test_source_deadline_registered_and_later_expiry_refuses_full_result(self):
        repository, database = self.repository([inventory("a")], [inventory("b", expiry=NOW)])
        with capture_read_lifetime() as lifetime:
            with self.assertRaises(HotSourceReadExpired):
                CatalogueScopeRepository(repository).load_full_long_inventory(CHANNEL)
        self.assertEqual(lifetime.expires_at, EXPIRY)
        self.assertEqual(len(database.calls), 2)

    def test_unknown_read_is_not_retried_or_declared_eof(self):
        error = DatabaseUnavailable("database_connection_failed", outcome_unknown=True)
        repository, database = self.repository([inventory("a")], error)
        iterator = repository.iter_long_inventory_pages(CHANNEL)
        next(iterator)
        with self.assertRaises(DatabaseUnavailable) as raised:
            next(iterator)
        self.assertIs(raised.exception, error)
        self.assertEqual(len(database.calls), 2)

    def test_existing_byte_budget_stops_before_another_page(self):
        repository, database = self.repository([inventory("a")], [])
        with self.assertRaisesRegex(CatalogueScopeError, "byte_budget"):
            CatalogueScopeRepository(repository, CatalogueReadBudget(max_bytes=1)).load_full_long_inventory(CHANNEL)
        self.assertEqual(len(database.calls), 1)

    def test_existing_time_budget_stops_before_another_page(self):
        clock = [0.0]
        budget = CatalogueReadBudget(timeout_seconds=1, clock=lambda: clock[0])
        repository, database = self.repository([inventory("a")], [])
        original = database.read_rows
        def delayed(*args, **kwargs):
            value = original(*args, **kwargs)
            clock[0] = 2.0
            return value
        database.read_rows = delayed
        with self.assertRaisesRegex(CatalogueScopeError, "deadline"):
            CatalogueScopeRepository(repository, budget).load_full_long_inventory(CHANNEL)
        self.assertEqual(len(database.calls), 1)


class StoredRows:
    def __init__(self):
        self.ids = [f"v{i:010d}" for i in range(24)]
        self.inventory = {key: inventory(key, None if i == 0 else
            (NOW - timedelta(days=3 + i % 5)).isoformat()) for i, key in enumerate(self.ids)}
        self.videos = {key: dict(video_id=key, title=f"title {i % 4}",
            published_at=self.inventory[key]["published_at"], checked_at=CHECKED,
            metadata_checked_at=CHECKED, availability_checked_at=CHECKED,
            availability_status="private" if i % 7 == 0 else "public",
            views=1000 + i % 3 * 40, duration="PT12M" if i % 2 else "PT5M",
            like_count=20 + i, comment_count=i, category_id="", tags=["even" if i % 2 else "odd"])
            for i, key in enumerate(self.ids)}
        self.videos[self.ids[-2]]["published_at"] = (NOW + timedelta(days=1)).isoformat()
        del self.videos[self.ids[-1]]  # Missing rows stay missing in every ordering.

    def load_channel(self, channel_id, *, columns):
        return dict(channel_id=CHANNEL, title="Channel", subscribers=2000, views=120000,
                    video_count=24, is_hidden=False, channel_metadata_status="ok",
                    channel_metadata_checked_at=CHECKED, channel_created_at="2020-01-01T00:00:00Z")

    def load_videos_by_ids(self, video_ids, *, columns):
        return [deepcopy(self.videos[key]) for key in video_ids if key in self.videos]

    def load_video_view_growth(self, channel_id, video_ids, *, as_of, recorded_from=None):
        return [dict(video_id=key, growth_status="ready", delta_views=int(key[1:]) % 3 * 20,
                     tracked_days=None, observed_start_at=(NOW-timedelta(hours=1, days=28)).isoformat(),
                     observed_end_at=CHECKED, observed_current_views=self.videos[key]["views"],
                     actual_interval_seconds=28*86400, boundary_distance_seconds=0, reason=None)
                for key in video_ids]


class FullRepository(StoredRows, ChannelExplorerRepository):
    def __init__(self):
        StoredRows.__init__(self)
        self.calls = []
        ChannelExplorerRepository.__init__(self, self, read_clock=lambda: NOW)

    @contextmanager
    def transaction(self, operation):
        repository = self
        assert operation.name == 'channel_explorer_inventory_page' and operation.read_only
        class ReadTransaction:
            @contextmanager
            def inventory_source_rows(self, channel_id, video_type):
                keys = sorted(repository.inventory)
                class SourceRows:
                    offset = 0
                    def fetch(self, size):
                        repository.calls.append((channel_id, video_type, size))
                        selected = keys[self.offset:self.offset+size]
                        self.offset += len(selected)
                        return [deepcopy(repository.inventory[key]) for key in selected]
                yield SourceRows()
        yield ReadTransaction()


class PermutedRepository(StoredRows):
    def __init__(self, order):
        super().__init__()
        self.order = order(self.ids)

    def iter_long_inventory_pages(self, channel_id):
        for start in range(0, len(self.order), 5):
            yield [deepcopy(self.inventory[key]) for key in self.order[start:start + 5]]


class FullConsumerEquivalenceTests(unittest.TestCase):
    def check_builder(self, builder, **kwargs):
        # Actual new DB iterator versus arbitrary traversal, including reversed
        # publication ties/NULL. Compare whole responses, not just counts.
        with patch.object(owner, "INVENTORY_PAGE_SIZE", 5):
            current = FullRepository()
            expected = builder(current, CHANNEL, now=NOW, full_scope=True, **kwargs)
        self.assertEqual(len(current.calls), 6)  # 24 identities + explicit EOF.
        for order in (lambda ids: list(reversed(ids)), lambda ids: ids[::2] + ids[1::2]):
            with self.subTest(order=order):
                actual = builder(PermutedRepository(order), CHANNEL, now=NOW, full_scope=True, **kwargs)
                self.assertEqual(actual, expected)
        return expected

    def test_catalogue_order_ties_and_publication_nulls_are_preserved(self):
        for sort_by in ("published_at", "views", "title"):
            with self.subTest(sort_by=sort_by):
                result = self.check_builder(build_channel_explorer, all_rows=True, sort_by=sort_by,
                                            catalogue_filters='{"version":1}')
                self.assertGreater(len(result["videos"]), 10)

    def test_baseline_aggregates_are_permutation_equivalent(self):
        self.check_builder(build_channel_baseline, catalogue_filters='{"version":1}')

    def test_growth_order_and_observations_are_permutation_equivalent(self):
        for sort_by in ("published_at", "views_gained"):
            with self.subTest(sort_by=sort_by):
                result = self.check_builder(build_channel_video_growth, all_rows=True, sort_by=sort_by,
                                            catalogue_filters='{"version":1}')
                self.assertTrue(any(v["view_growth"]["status"] == "ready" for v in result["videos"]))

    def test_full_filter_facets_are_permutation_equivalent(self):
        self.check_builder(build_channel_filter_facets, catalogue_filters='{"version":1}')

    def test_recorded_id_order_replays_same_frozen_catalogue(self):
        source = FullRepository()
        # Recording intentionally excludes withheld rows; use public rows for
        # this snapshot round-trip, while permutation cases retain withheld rows.
        for row in source.videos.values():
            row["availability_status"] = "public"
        repository = RecordingCatalogueRepository(source, now=NOW,
            source_valid_until=NOW+timedelta(minutes=10))
        options = dict(now=NOW, source_valid_until=NOW+timedelta(minutes=10),
                       full_scope=True, all_rows=True, catalogue_filters='{"version":1}')
        live = build_channel_explorer(repository, CHANNEL, **options)
        frozen = build_channel_explorer(FrozenCatalogueRepository(repository.snapshot), CHANNEL, **options)
        self.assertEqual(frozen, live)


if __name__ == "__main__":
    unittest.main()
