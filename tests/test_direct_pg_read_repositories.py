
"""Direct read-repository contracts using recorded SQL, never a live database."""
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from catalogue_scope import CatalogueScopeError
from db.reads import channel_baseline, channel_derived_insights, channel_explorer
from db.reads.channel_baseline import ChannelBaselineRepository
from db.reads.channel_counter_dynamics import ChannelCounterDynamicsRepository
from db.reads.channel_derived_insights import ChannelDerivedInsightsRepository
from db.reads.channel_explorer import ChannelExplorerRepository
from db.reads.channel_timeline import ChannelTimelineRepository
from db.reads.channel_video_growth import ChannelVideoGrowthRepository, VideoGrowthResponseError
from db.reads.frontend import FrontendRepository, SNAPSHOT_READ_LIMIT
from db.base.postgres import DatabaseError, _INVENTORY_SOURCE
from db.user_work.saved_collections import SavedCollectionsRepository
from db.reads.system_health import SystemHealthRepository
from channel_timeline import build_channel_timeline
from hot_storage import HOT_DATA_WINDOW, HotSourceReadExpired, iso_utc

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
CURRENT = iso_utc(NOW - timedelta(hours=1))
EXPIRED = iso_utc(NOW - HOT_DATA_WINDOW - timedelta(days=1))
DEADLINE = iso_utc(datetime.now(timezone.utc)+timedelta(days=1))


def timeline_payload(**changes):
    return {"bucket_at": CURRENT, "recorded_at": CURRENT, "views": 1,
            "subscribers": None, "correction_within_bucket": False,
            "scan_truncated": False, "snapshot_count": 1, "source_expires_at": DEADLINE, **changes}


def growth_payload(video_id, **changes):
    return {"video_id": video_id, "growth_status": "unavailable", "delta_views": None,
            "tracked_days": None, "observed_start_at": None, "observed_end_at": None,
            "observed_current_views": None, "actual_interval_seconds": None,
            "boundary_distance_seconds": None, "reason": "video_not_in_scope", "source_expires_at": DEADLINE, **changes}


def saved_source_payload(video_id):
    return {"video_id": video_id, "channel_id": None, "source_status": "unavailable",
            "source_reason": "inventory_missing", "video_row": None, "source_expires_at": DEADLINE}


class RecordingDatabase:
    """The direct protocol only: a legacy chain fails immediately."""
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def _read(self, kind, operation, statement, params, maximum):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", operation):
            raise AssertionError("invalid direct operation name")
        rendered = statement if isinstance(statement, str) else statement.as_string()
        self.calls.append((kind, operation, rendered, dict(params) if isinstance(params, Mapping) else tuple(params), maximum))
        if not self.results:
            raise AssertionError("unexpected database read")
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        if operation in {'analytics_channel_timeline', 'analytics_video_growth', 'analytics_saved_sources'} and isinstance(result, list):
            result = [dict(row, source_expires_at=DEADLINE) for row in result]
            if operation == "analytics_channel_timeline":
                result = [dict(row, response_source_expires_at=DEADLINE) for row in result]
        return result

    @contextmanager
    def transaction(self, operation):
        assert operation.name in ('analytics_video_growth', 'channel_explorer_inventory_page') and operation.read_only
        database = self
        class ReadTransaction:
            @contextmanager
            def inventory_source_rows(self, channel_id, video_type):
                assert operation.name == 'channel_explorer_inventory_page'
                assert operation.total_timeout_ms == 60_000
                database.stream_declarations = getattr(database, 'stream_declarations', [])
                database.stream_declarations.append((_INVENTORY_SOURCE, (channel_id, video_type)))
                class SourceRows:
                    def fetch(self, size):
                        return database.read_rows(operation.name, _INVENTORY_SOURCE,
                                                  (channel_id, video_type), max_rows=size)
                try:
                    yield SourceRows()
                finally:
                    database.stream_closes = getattr(database, 'stream_closes', 0) + 1
            def fetch_one(self, statement):
                assert statement == "SELECT set_config('jit','off',true) AS value"
                return {'value': 'off'}
            def fetch_all(self, statement, params, *, max_rows):
                return database.read_rows(operation.name, statement, params, max_rows=max_rows)
        yield ReadTransaction()

    def read_rows(self, operation, statement, params=(), *, max_rows):
        result = self._read("rows", operation, statement, params, max_rows)
        if isinstance(result, list) and len(result) > max_rows:
            raise DatabaseError('database_result_limit_exceeded')
        return result

    def read_row(self, operation, statement, params=()):
        return self._read("row", operation, statement, params, 1)

    def call(self, name, params=None, *, max_rows=1000):
        self.calls.append(("function", name, dict(params or {}), max_rows))
        if not self.results:
            raise AssertionError("unexpected function call")
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        if isinstance(result, list) and len(result) > max_rows:
            raise DatabaseError('database_result_limit_exceeded')
        return result


def inventory(video_id, published=CURRENT, *, current=True):
    return {"video_id": video_id, "published_at": published,
            "source_checked_at": CURRENT if current else EXPIRED}


def video(video_id, *, current=True, public=True):
    return {"video_id": video_id, "title": video_id, "views": 2**60,
            "checked_at": CURRENT if current else EXPIRED,
            "metadata_checked_at": CURRENT,
            "availability_checked_at": CURRENT,
            "availability_status": "public" if public else "private"}


class InventoryReadTests(unittest.TestCase):
    def test_long_inventory_is_one_statement_with_the_requested_limit(self):
        # The protected view cannot be read in pages by its order key, so the
        # requested prefix is one statement (and one snapshot).
        for repository_type in (ChannelExplorerRepository, ChannelBaselineRepository, ChannelDerivedInsightsRepository):
            with self.subTest(repository=repository_type.__name__):
                rows = [inventory("a"), inventory("b"), inventory("c", None)]
                db = RecordingDatabase(rows)
                self.assertEqual(repository_type(db).load_long_inventory("channel'quoted", limit=5001), rows)
                self.assertEqual(len(db.calls), 1)
                self.assertIn("ORDER BY published_at DESC NULLS LAST, video_id ASC LIMIT %s", db.calls[0][2])
                self.assertNotIn("channel'quoted", db.calls[0][2])
                self.assertEqual(db.calls[0][3], ("channel'quoted", "long", 5001))
                self.assertEqual(db.calls[0][4], 5001)
                self.assertEqual(repository_type(RecordingDatabase()).load_long_inventory("channel", limit=0), [])

    def test_scope_scan_requires_empty_page_and_rejects_repeated_cursor(self):
        page = [inventory("a")]
        db = RecordingDatabase(page, page)
        with patch.object(channel_explorer, "INVENTORY_PAGE_SIZE", 1):
            iterator = ChannelExplorerRepository(db).iter_long_inventory_pages("channel")
            self.assertEqual(next(iterator), page)
            with self.assertRaisesRegex(CatalogueScopeError, "did_not_advance"):
                next(iterator)
        db = RecordingDatabase([inventory("a")], [])
        pages = list(ChannelExplorerRepository(db).iter_long_inventory_pages("channel"))
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(db.calls), 2)

    def test_strict_scope_rejects_invalid_response_and_cursor(self):
        for response in ({}, [inventory("invalid id")]):
            db = RecordingDatabase(response)
            with self.assertRaises(CatalogueScopeError):
                list(ChannelExplorerRepository(db).iter_long_inventory_pages("channel"))

    def test_canonical_count_keeps_bigints_and_rejects_missing_count(self):
        db = RecordingDatabase({"count": 2**60}, None)
        repository = ChannelExplorerRepository(db)
        self.assertEqual(repository.count_canonical_inventory("channel", video_type="long"), 2**60)
        self.assertIn("SELECT canonical_count FROM readable.canonical_inventory_counts", db.calls[0][2])
        self.assertNotIn("public.video_inventory", db.calls[0][2])
        self.assertEqual(db.calls[0][3], ("channel", "long"))
        with self.assertRaisesRegex(RuntimeError, "count is unavailable"):
            repository.count_canonical_inventory("channel", video_type="long")

    def test_published_completeness_scope_is_explicit_and_separate(self):
        db = RecordingDatabase({"count": 7})
        repository = ChannelExplorerRepository(db)
        self.assertEqual(repository.count_canonical_inventory("channel",video_type="long",exclude_waiting=True),7)
        self.assertIn("readable.published_inventory_counts",db.calls[0][2])
        self.assertNotIn("public.videos",db.calls[0][2])
        with self.assertRaises(ValueError):
            repository.count_canonical_inventory("channel",video_type="long",exclude_waiting="yes")

    def test_video_identity_reads_are_batched_and_values_are_parameters(self):
        ids = [f"v{i}" for i in range(501)]
        for cls in (ChannelExplorerRepository, ChannelBaselineRepository, ChannelDerivedInsightsRepository, FrontendRepository):
            with self.subTest(repository=cls.__name__):
                db = RecordingDatabase([video(v) for v in ids[:500]], [video(ids[-1])])
                rows = cls(db, read_clock=lambda: NOW).load_videos_by_ids(ids + [ids[0]], columns="video_id,title,views")
                self.assertEqual(len(rows), 501)
                self.assertEqual([call[4] for call in db.calls], [500, 1])
                self.assertEqual(db.calls[0][3], (ids[:500],))
                self.assertIn("video_id = ANY(%s::text[])", db.calls[0][2])
                self.assertEqual(rows[0]["views"], 2**60)

    def test_empty_identity_reads_do_not_dispatch(self):
        for cls in (ChannelExplorerRepository, ChannelBaselineRepository, ChannelDerivedInsightsRepository, FrontendRepository):
            db = RecordingDatabase()
            self.assertEqual(cls(db).load_videos_by_ids([], columns="video_id"), [])
            self.assertEqual(db.calls, [])

    def test_projection_is_quoted_and_not_a_sql_expression(self):
        db = RecordingDatabase({"channel_id": "channel"})
        self.assertEqual(ChannelExplorerRepository(db).load_channel("channel", columns="channel_id,is_hidden"),
                         {"channel_id": "channel"})
        self.assertIn('SELECT "channel_id", "is_hidden"', db.calls[0][2])
        with self.assertRaises(ValueError):
            ChannelExplorerRepository(RecordingDatabase()).load_channel("channel", columns="channel_id;SELECT 1")

    def test_type_resolution_batches_preserve_canonical_type_filter(self):
        ids = [f"v{i}" for i in range(501)]
        deadline = iso_utc(NOW + timedelta(seconds=1))
        nullable = {"video_id": "v3", "canonical_video_type": "short",
                    "resolution_status": None, "present_membership_types": [],
                    "present_membership_channels": None, "manual_valid": None,
                    "source_expires_at": deadline}
        first = [nullable, {"video_id": "v1", "manual_valid": False}, nullable]
        last = [{"video_id": ids[-1], "manual_valid": True}]
        db = RecordingDatabase(first, last)
        rows = ChannelExplorerRepository(db, read_clock=lambda: NOW).load_type_resolutions(
            [None, "", *ids, ids[0], 0], video_type="short")
        # Keep returned row order/multiplicity/NULLs; only requested IDs are deduplicated.
        self.assertEqual(rows, first + last)
        self.assertEqual([call[3] for call in db.calls], [("short", ids[:500]), ("short", ids[500:])])
        self.assertEqual([call[4] for call in db.calls], [500, 1])
        self.assertIn("readable.video_inventory_type_resolution", db.calls[0][2])
        self.assertIn("proof.canonical_video_type = requested_type.video_type", db.calls[0][2])
        self.assertIn("proof.automatic_valid,proof.source_expires_at", db.calls[0][2])
        self.assertNotIn("public.", db.calls[0][2])
        empty = RecordingDatabase()
        repository = ChannelExplorerRepository(empty, read_clock=lambda: NOW)
        self.assertEqual(repository.load_type_resolutions([None, "", None], video_type="short"), [])
        with self.assertRaisesRegex(ValueError, "unsupported video type"):
            repository.load_type_resolutions([], video_type="other")
        self.assertEqual(empty.calls, [])
        expired = RecordingDatabase(first, last)
        with self.assertRaises(HotSourceReadExpired):
            ChannelExplorerRepository(expired, read_clock=lambda: NOW + timedelta(seconds=2)).load_type_resolutions(
                ids, video_type="short")
        self.assertEqual(len(expired.calls), 2)  # Re-admit the whole returned bag after the final batch.
        over_limit = RecordingDatabase([nullable, nullable])
        with self.assertRaisesRegex(DatabaseError, "database_result_limit_exceeded"):
            ChannelExplorerRepository(over_limit).load_type_resolutions(["v3", "v3"], video_type="short")
        self.assertEqual(len(over_limit.calls), 1)


class WindowReadTests(unittest.TestCase):
    def test_counter_windows_are_clamped_and_null_baselines_excluded(self):
        db = RecordingDatabase({"recorded_at": CURRENT, "views": 2**60}, {"recorded_at": CURRENT})
        repository = ChannelCounterDynamicsRepository(db, read_clock=lambda: NOW)
        self.assertEqual(repository.load_latest_snapshot("channel", recorded_through=iso_utc(NOW + timedelta(days=2)))["views"], 2**60)
        self.assertEqual(db.calls[0][3], ("channel", iso_utc(NOW - HOT_DATA_WINDOW), iso_utc(NOW)))
        repository.load_metric_baseline("channel", metric="subscribers",
                                       recorded_from=iso_utc(NOW - timedelta(days=50)),
                                       recorded_through=iso_utc(NOW))
        self.assertIn('"subscribers" IS NOT NULL ORDER BY recorded_at ASC LIMIT 1', db.calls[1][2])
        with self.assertRaises(ValueError):
            repository.load_metric_baseline("channel", metric="views;DROP TABLE videos",
                                            recorded_from=CURRENT, recorded_through=CURRENT)

    def test_expired_window_never_dispatches(self):
        db = RecordingDatabase()
        repository = ChannelCounterDynamicsRepository(db, read_clock=lambda: NOW)
        self.assertIsNone(repository.load_latest_snapshot("channel", recorded_through=EXPIRED))
        self.assertEqual(db.calls, [])

    def test_snapshot_keyset_keeps_global_order_and_bigint_tiebreaker(self):
        first = [{"id": 2**60, "video_id": "a", "recorded_at": CURRENT},
                 {"id": 2**60-1, "video_id": "b", "recorded_at": CURRENT}]
        second = [{"id": 2**60-2, "video_id": "a", "recorded_at": CURRENT}]
        db = RecordingDatabase(first, second)
        repository = ChannelDerivedInsightsRepository(db, read_clock=lambda: NOW)
        with patch.object(channel_derived_insights, "SNAPSHOT_PAGE_SIZE", 2):
            rows = repository.load_snapshots_by_video_ids(
                ["a", "b"], recorded_from=NOW-timedelta(days=40),
                recorded_through=NOW+timedelta(days=1), limit=3, include_engagement=False)
        self.assertEqual(rows, first + second)
        self.assertNotIn("like_count", db.calls[0][2])
        self.assertEqual(db.calls[0][3][0], ["a", "b"])
        self.assertIn("recorded_at DESC, id DESC", db.calls[1][2])
        self.assertIn("id < %s", db.calls[1][2])
        self.assertEqual(db.calls[1][3][-3:], (CURRENT, 2**60-1, 1))

    def test_timeline_function_preserves_coverage_and_requested_arguments(self):
        payload = [timeline_payload(views=2**60, scan_truncated=True, snapshot_count=123),
                   timeline_payload(recorded_at=EXPIRED, views=1, snapshot_count=999)]
        db = RecordingDatabase(payload)
        repository = ChannelTimelineRepository(db, read_clock=lambda: NOW)
        rows, truncated, count = repository.load_snapshot_window(
            "channel", metric="views", recorded_from=EXPIRED, recorded_through=iso_utc(NOW),
            bucket_seconds=1, scan_limit=20000, limit=900)
        expected = {key: value for key, value in payload[0].items() if key not in {"scan_truncated", "snapshot_count"}}
        self.assertEqual(rows, [expected])
        self.assertTrue(truncated)
        self.assertEqual(count, 123)
        call = db.calls[0]
        self.assertEqual(call[1], "analytics_channel_timeline")
        self.assertEqual(call[3]["bucket_seconds"], 300)
        self.assertEqual(call[3]["return_limit"], 500)
        self.assertEqual(call[4], 501)
        self.assertEqual(payload[0]["snapshot_count"], 123)

    def test_timeline_allows_exact_sql_bucket_sentinel_and_rejects_overrun(self):
        for requested, expected in ((0, 2), (1, 2), (499, 500), (500, 501), (900, 501)):
            with self.subTest(limit=requested):
                payload = [timeline_payload(views=index, snapshot_count=expected)
                           for index in range(expected)]
                db = RecordingDatabase(payload, payload + [dict(payload[-1])])
                repository = ChannelTimelineRepository(db, read_clock=lambda: NOW)
                options = dict(metric="views", recorded_from=None, recorded_through=iso_utc(NOW),
                               bucket_seconds=300, scan_limit=20000, limit=requested)
                rows, truncated, count = repository.load_snapshot_window("channel", **options)
                self.assertEqual(len(rows), expected)
                self.assertFalse(truncated)
                self.assertEqual(count, expected)
                self.assertEqual(db.calls[0][3]["return_limit"], min(500, max(1, requested)))
                self.assertEqual(db.calls[0][4], expected)
                with self.assertRaisesRegex(DatabaseError, "database_result_limit_exceeded"):
                    repository.load_snapshot_window("channel", **options)

    def test_timeline_caller_keeps_latest_500_points_and_discloses_sentinel(self):
        payload = [timeline_payload(bucket_at=iso_utc(NOW - timedelta(minutes=5 * (500-index))),
                    recorded_at=iso_utc(NOW - timedelta(minutes=5 * (500-index))),
                    views=index, snapshot_count=501) for index in range(501)]
        db = RecordingDatabase({"channel_id": "channel", "is_hidden": False}, [], payload, [])
        repository = ChannelTimelineRepository(db, read_clock=lambda: NOW)
        result = build_channel_timeline(repository, "channel", metric="views", period="28d", now=NOW)
        self.assertEqual(len(result["points"]), 500)
        self.assertEqual(result["points"][0]["value"], 1)
        self.assertEqual(result["points"][-1]["value"], 500)
        self.assertTrue(result["coverage"]["series_truncated"])
        self.assertFalse(result["coverage"]["scan_truncated"])
        self.assertEqual(result["coverage"]["snapshot_count"], 501)
        self.assertEqual([call[1] for call in db.calls].count("analytics_channel_timeline"), 1)

    def test_timeline_edges_and_publications_keep_order_and_freshness(self):
        db = RecordingDatabase([{"recorded_at": CURRENT}], [inventory("new"), inventory("old", EXPIRED), inventory("expired", current=False)])
        repository = ChannelTimelineRepository(db, read_clock=lambda: NOW)
        self.assertEqual(repository.load_snapshot_edge_candidates("channel", metric="views", at_or_before=iso_utc(NOW), descending=False, limit=0),
                         [{"recorded_at": CURRENT}])
        self.assertIn('"views" IS NOT NULL ORDER BY recorded_at ASC LIMIT %s', db.calls[0][2])
        self.assertEqual(db.calls[0][3][-1], 1)
        rows = repository.load_publication_events("channel", published_from=EXPIRED, published_through=CURRENT, limit=5)
        self.assertEqual([row["video_id"] for row in rows], ["old", "new"])
        self.assertIn("published_at <= %s::timestamptz AND published_at >= %s::timestamptz", db.calls[1][2])
        self.assertEqual(db.calls[1][3], ("channel", "long", CURRENT, EXPIRED, 5))


class FrontendReadTests(unittest.TestCase):
    def test_inventory_counts_use_one_snapshot_for_all_channels_and_types(self):
        db = RecordingDatabase([
            {"video_type": "long", "count": 2**60, "source_checked_at": CURRENT},
            {"video_type": "short", "count": 4, "source_checked_at": CURRENT},
        ])
        repository = FrontendRepository(db, read_clock=lambda: NOW)
        channels = [f"channel-{i}" for i in range(830)]
        counts = repository.count_inventory_by_type(channels + channels, ["long", "short", "stream", "long"])
        self.assertEqual(counts, {"long": 2**60, "short": 4, "stream": 0})
        self.assertEqual(len(db.calls), 1)
        self.assertEqual(db.calls[0][3], (sorted(channels), ["long", "short", "stream"]))
        self.assertEqual(db.calls[0][4], 3)
        self.assertIn("FROM readable.hot_video_inventory", db.calls[0][2])
        self.assertIn("GROUP BY video_type", db.calls[0][2])
        self.assertIn("min(source_checked_at)", db.calls[0][2])
        self.assertIn("min(source_expires_at)", db.calls[0][2])

    def test_inventory_counts_keep_earliest_deadline_and_reject_expired_group(self):
        from hot_storage import capture_read_lifetime
        early = NOW + timedelta(hours=1)
        rows = [
            {"video_type": "long", "count": 2, "source_checked_at": CURRENT,
             "source_expires_at": iso_utc(NOW + timedelta(days=1))},
            {"video_type": "short", "count": 1, "source_checked_at": CURRENT,
             "source_expires_at": iso_utc(early)},
        ]
        with capture_read_lifetime() as lifetime:
            FrontendRepository(RecordingDatabase(rows), read_clock=lambda: NOW).count_inventory_by_type(
                ["a"], ["long", "short"])
        self.assertEqual(lifetime.expires_at, early)
        for invalid in ({"source_checked_at": EXPIRED}, {"source_expires_at": iso_utc(NOW)}):
            with self.subTest(invalid=invalid), self.assertRaises(HotSourceReadExpired):
                db = RecordingDatabase([rows[0], dict(rows[1], **invalid)])
                FrontendRepository(db, read_clock=lambda: NOW).count_inventory_by_type(["a"], ["long", "short"])

    def test_inventory_counts_empty_scope_and_empty_result(self):
        db = RecordingDatabase([])
        repository = FrontendRepository(db, read_clock=lambda: NOW)
        self.assertEqual(repository.count_inventory_by_type([], ["long"]), {"long": 0})
        self.assertEqual(repository.count_inventory_by_type(["a"], []), {})
        self.assertEqual(db.calls, [])
        self.assertEqual(repository.count_inventory_by_type(["a"], ["long", "stream"]), {"long": 0, "stream": 0})
        with self.assertRaises(RuntimeError):
            FrontendRepository(RecordingDatabase(None)).count_inventory_by_type(["a"], ["long"])

    def test_channel_and_folder_reads_fail_loud_at_verified_schema_bounds(self):
        db = RecordingDatabase([{"channel_id": "c", "title": "Expired", "channel_metadata_status": "ok",
                                 "channel_metadata_checked_at": EXPIRED}], [{"id": "folder", "name": "F"}])
        repository = FrontendRepository(db, read_clock=lambda: NOW)
        self.assertIsNone(repository.load_channel_rows("channel_id,title")[0]["title"])
        self.assertEqual(db.calls[0][4], 10_000)
        self.assertIn('"channel_metadata_checked_at"', db.calls[0][2])
        self.assertNotIn("LIMIT", db.calls[0][2])
        self.assertEqual(repository.load_folder_rows(), [{"id": "folder", "name": "F"}])
        self.assertEqual(db.calls[1][4], 1000)
        self.assertNotIn("LIMIT", db.calls[1][2])

    def test_offset_pages_return_raw_count_after_source_expires(self):
        raw = [inventory("current"), inventory("expired", current=False)]
        db = RecordingDatabase(raw, raw, [])
        repository = FrontendRepository(db, read_clock=lambda: NOW)
        rows, fetched = repository.load_channel_inventory_page("channel", video_type=" LONG ", offset=1000, page_size=1000)
        self.assertEqual([row["video_id"] for row in rows], ["current"])
        self.assertEqual(fetched, 2)
        self.assertEqual(db.calls[0][3], ("channel", "long", 1000, 1000))
        self.assertIn("published_at DESC NULLS FIRST, video_id DESC LIMIT %s OFFSET %s", db.calls[0][2])
        rows, fetched, last = repository.load_recent_long_inventory_page(
            published_from=EXPIRED, channel_ids=["UC1", "UC2"], before=(CURRENT, "zz"), page_size=2)
        self.assertEqual(fetched, 2)
        self.assertEqual(len(rows), 1)
        # The next page starts after the last row read, current or not.
        self.assertEqual(last, (raw[-1]["published_at"], "expired"))
        self.assertEqual(db.calls[1][3], ("long", EXPIRED, ["UC1", "UC2"], CURRENT, "zz", 2))
        self.assertIn('channel_id = ANY(%s::text[])', db.calls[1][2])
        self.assertIn('(published_at, video_id COLLATE "C") < (%s::timestamptz, %s COLLATE "C")', db.calls[1][2])
        self.assertIn('ORDER BY published_at DESC, video_id COLLATE "C" DESC LIMIT %s', db.calls[1][2])
        self.assertNotIn("OFFSET", db.calls[1][2])
        repository.load_channel_backfill_page("channel", video_type="stream", offset=7, page_size=3)
        self.assertIn("FROM public.video_inventory", db.calls[2][2])
        self.assertEqual(db.calls[2][3], ("channel", "stream", 3, 7))

    def test_video_source_contract_filters_stale_and_nonpublic_rows(self):
        db = RecordingDatabase([video("current"), video("stale", current=False), video("private", public=False)])
        rows = FrontendRepository(db, read_clock=lambda: NOW).load_videos_by_ids(
            ["current", "stale", "private"], columns="video_id,title,views")
        self.assertEqual([row["video_id"] for row in rows], ["current"])
        self.assertIn('"metadata_checked_at"', db.calls[0][2])
        self.assertIn('"availability_checked_at"', db.calls[0][2])

    def test_snapshot_limits_keep_inclusive_range_behavior_and_new_safety_bound(self):
        db = RecordingDatabase([{"recorded_at": CURRENT, "views": 2**60}], [])
        repository = FrontendRepository(db, read_clock=lambda: NOW)
        self.assertEqual(repository.load_video_snapshots("v", limit=0)[0]["views"], 2**60)
        self.assertEqual(db.calls[0][4], 1)
        self.assertEqual(db.calls[0][3][-2:], ("v", 1))
        self.assertIn("ORDER BY recorded_at ASC", db.calls[0][2])
        repository.load_video_snapshots("v", order_desc=True)
        self.assertEqual(db.calls[1][4], SNAPSHOT_READ_LIMIT)
        self.assertNotIn("LIMIT", db.calls[1][2])
        self.assertIn("ORDER BY recorded_at DESC", db.calls[1][2])


class GrowthReadTests(unittest.TestCase):
    def test_growth_keeps_exact_id_coverage_order_and_omitted_default(self):
        db = RecordingDatabase([growth_payload("b", delta_views=2**60), growth_payload("a")])
        rows = ChannelVideoGrowthRepository(db).load_video_view_growth("channel", ["a", "b", "a"], as_of=NOW)
        self.assertEqual([row["video_id"] for row in rows], ["a", "b"])
        self.assertEqual(rows[1]["delta_views"], 2**60)
        self.assertIsNone(db.calls[0][3]["recorded_from"])
        self.assertEqual(db.calls[0][4], 2)

    def test_growth_missing_or_duplicate_rows_never_return_partial_result(self):
        for result in ([growth_payload("a")], [growth_payload("a"), growth_payload("a")], {"video_id": "a"}):
            with self.subTest(result=result):
                with self.assertRaises(VideoGrowthResponseError):
                    ChannelVideoGrowthRepository(RecordingDatabase(result)).load_video_view_growth(
                        "channel", ["a", "b"], as_of=NOW)

    def test_growth_batches_and_forwards_explicit_window(self):
        ids = [f"v{i}" for i in range(501)]
        db = RecordingDatabase([growth_payload(v) for v in ids[:500]], [growth_payload(ids[-1])])
        rows = ChannelVideoGrowthRepository(db).load_video_view_growth(
            "channel", ids, as_of=NOW, recorded_from=NOW-timedelta(days=7))
        self.assertEqual(len(rows), 501)
        self.assertEqual([call[4] for call in db.calls], [500, 1])
        self.assertEqual(db.calls[1][3]["video_ids"], [ids[-1]])
        self.assertEqual(db.calls[0][3]["recorded_from"], iso_utc(NOW-timedelta(days=7)))


class OtherReaderCapTests(unittest.TestCase):
    def test_saved_sources_preserve_exact_batched_id_coverage_without_sentinel(self):
        ids = [f"v{index:010d}" for index in range(501)]
        payload = [saved_source_payload(identity) for identity in ids]
        db = RecordingDatabase(list(reversed(payload[:500])), payload[500:])
        rows = SavedCollectionsRepository(db, read_clock=lambda: NOW).load_sources(ids, as_of=NOW)
        self.assertEqual([row["video_id"] for row in rows], ids)
        self.assertEqual([call[4] for call in db.calls], [500, 1])
        self.assertEqual([call[1] for call in db.calls], ["analytics_saved_sources"] * 2)

    def test_health_coverage_accepts_all_channels_up_to_schema_limit(self):
        payload = [{"channel_id": str(index)} for index in range(10_000)]
        db = RecordingDatabase(payload, payload + [{"channel_id": "over-limit"}])
        repository = SystemHealthRepository(db)
        self.assertEqual(repository.fetch_rpc_rows("get_channel_coverage_summary"), payload)
        self.assertEqual(db.calls[0][4], 10_000)
        with self.assertRaisesRegex(DatabaseError, "database_result_limit_exceeded"):
            repository.fetch_rpc_rows("get_channel_coverage_summary")

    def test_health_queue_is_one_aggregate_row_and_unknown_read_never_dispatches(self):
        db = RecordingDatabase([{"total": 0}], [{"total": 0}, {"total": 1}])
        repository = SystemHealthRepository(db)
        self.assertEqual(repository.fetch_rpc_rows("get_video_collection_health"), [{"total": 0}])
        self.assertEqual(db.calls[0][4], 1)
        with self.assertRaisesRegex(DatabaseError, "database_result_limit_exceeded"):
            repository.fetch_rpc_rows("get_video_collection_health")
        with self.assertRaises(ValueError):
            repository.fetch_rpc_rows("unknown_health_function")
        self.assertEqual(len(db.calls), 2)


if __name__ == "__main__":
    unittest.main()
