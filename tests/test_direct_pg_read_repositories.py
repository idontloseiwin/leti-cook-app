"""PostgreSQL read repository contracts."""
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from db.reads.channel_explorer import ChannelExplorerRepository
from db.reads.frontend import FrontendRepository, SNAPSHOT_READ_LIMIT
from db.base.postgres import DatabaseError
from db.user_work.saved_collections import SavedCollectionsRepository
from db.reads.system_health import SystemHealthRepository
from hot_storage import HOT_DATA_WINDOW, HotSourceReadExpired, iso_utc

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
CURRENT = iso_utc(NOW - timedelta(hours=1))
EXPIRED = iso_utc(NOW - HOT_DATA_WINDOW - timedelta(days=1))
DEADLINE = iso_utc(datetime.now(timezone.utc)+timedelta(days=1))


def saved_source_payload(video_id):
    return {"video_id": video_id, "channel_id": None, "source_status": "unavailable",
            "source_reason": "inventory_missing", "video_row": None, "source_expires_at": DEADLINE}


class RecordingDatabase:
    """The direct protocol only: any other call chain fails immediately."""
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
        if operation == 'analytics_saved_sources' and isinstance(result, list):
            result = [dict(row, source_expires_at=DEADLINE) for row in result]
        return result

    def read_rows(self, operation, statement, params=(), *, max_rows, disable_jit=False):
        self.disable_jit = disable_jit
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


class HeaderReadTests(unittest.TestCase):
    def test_projection_is_quoted_and_not_a_sql_expression(self):
        db = RecordingDatabase({"channel_id": "channel"})
        self.assertEqual(ChannelExplorerRepository(db).load_channel("channel", columns="channel_id,is_hidden"),
                         {"channel_id": "channel"})
        self.assertIn('SELECT "channel_id", "is_hidden"', db.calls[0][2])
        with self.assertRaises(ValueError):
            ChannelExplorerRepository(RecordingDatabase()).load_channel("channel", columns="channel_id;SELECT 1")


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
        self.assertEqual(db.calls[0][3], {"channels": sorted(channels), "types": ["long", "short", "stream"]})
        self.assertEqual(db.calls[0][4], 3)
        # The read model's current proof, the protected view live only for the other rows.
        # Its own columns come from the narrow view, without readable.videos.
        self.assertIn("FROM readable.video_search_projection_types p WHERE p.channel_id=ANY(%(channels)s::text[])", db.calls[0][2])
        self.assertNotIn("readable.video_search_projection p", db.calls[0][2])
        self.assertNotIn("readable.videos", db.calls[0][2])
        self.assertIn("WHERE p.proof_current AND p.hot_present AND p.inventory_type=ANY(%(types)s::text[])", db.calls[0][2])
        self.assertIn("FROM readable.hot_video_inventory h0", db.calls[0][2])
        self.assertIn("WHERE p.proof_current IS NOT TRUE AND h.video_type=ANY(%(types)s::text[])", db.calls[0][2])
        self.assertIn("GROUP BY video_type", db.calls[0][2])
        self.assertIn("min(source_checked_at)", db.calls[0][2])
        self.assertIn("min(source_expires_at)", db.calls[0][2])
        self.assertTrue(db.disable_jit)

    def test_the_typed_view_restates_the_projection_view_without_its_join(self):
        # The narrow view is the projection view's first columns (ids, proof_current, stored
        # type proof) over the same tables and queue join, without readable.videos; same options, owner, grant.
        schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")
        options = " WITH (security_barrier=true, security_invoker=false) AS\n"
        for head in ("CREATE OR REPLACE VIEW readable.video_search_projection", "CREATE VIEW readable.video_search_projection_types"):
            self.assertEqual(schema.count(head + options), 1, head)
        full = schema.split("CREATE OR REPLACE VIEW readable.video_search_projection" + options, 1)[1].split(";", 1)[0]
        narrow = schema.split("CREATE VIEW readable.video_search_projection_types" + options, 1)[1].split(";", 1)[0]
        tables = ("\nFROM public.video_search_projection p"
                  "\nLEFT JOIN public.video_search_projection_queue q ON q.video_id=p.video_id")
        self.assertIn(tables + "\nLEFT JOIN readable.videos v ON v.video_id=p.video_id", full)
        own = full.split("p.identity_clock,p.identity_expiry,", 1)[0] + "p.identity_clock,p.identity_expiry"
        self.assertIn("AND q.video_id IS NULL) AS proof_current,", own)
        self.assertEqual(narrow, own + tables)
        for view in ("video_search_projection", "video_search_projection_types"):
            self.assertEqual(re.findall(r"^(?:GRANT|REVOKE|ALTER VIEW)\b[^\n;]*\breadable\." + view + r"\b[^\n]*", schema, re.M),
                             [f"ALTER VIEW readable.{view} OWNER TO postgres;", f"GRANT SELECT ON readable.{view} TO lic_reader;"])

    def test_the_projection_view_appends_the_videos_payload_after_its_own_columns(self):
        # The payload a catalogue read may also select comes from the projection view's own
        # readable.videos row, appended after the columns it had: each is that view's column itself, masked
        # there, and together they supply the catalogue payload in one lookup.
        from db.reads.research_sql import _EXTRA_COLUMNS
        schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")
        options = " WITH (security_barrier=true, security_invoker=false) AS\n"
        self.assertEqual(schema.count("CREATE OR REPLACE VIEW readable.video_search_projection" + options), 1)
        full = schema.split("CREATE OR REPLACE VIEW readable.video_search_projection" + options, 1)[1].split(";", 1)[0]
        before, appended = full.split(" v.publication_status,v.stream_actual_end_at,\n", 1)
        appended, tables = appended.split("\nFROM public.video_search_projection p", 1)
        columns = [column.strip() for column in appended.replace("\n", "").split(",")]
        self.assertTrue(all(re.fullmatch(r"v\.[a-z_]+", column) for column in columns), columns)
        self.assertEqual({column[2:] for column in columns} | {"stream_actual_end_at"}, set(_EXTRA_COLUMNS.values()))
        self.assertEqual(len(columns), len(set(columns)))
        self.assertNotIn(" AS ", appended)
        self.assertTrue(tables.endswith("\nLEFT JOIN readable.videos v ON v.video_id=p.video_id"))

    def test_the_publication_times_view_shows_only_what_readable_videos_shows(self):
        # Readable.video_publication_times holds no data of its own. A row is one public.videos row
        # whose published_at readable.videos shows unmasked: the same metadata admission expression, as text.
        schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")
        videos = schema.rsplit("CREATE OR REPLACE VIEW readable.videos WITH (security_barrier=true, security_invoker=false) AS\n", 1)[1]
        videos = videos.split(";", 1)[0]
        self.assertIn("CASE WHEN admitted.metadata THEN s.published_at END AS published_at,", videos)
        admission = re.search(r"\n(\(isfinite\(s\.metadata_checked_at\) [^\n]*\)) AS metadata,\n", videos).group(1)
        options = " WITH (security_barrier=true, security_invoker=false) AS\n"
        view = schema.split("CREATE VIEW readable.video_publication_times" + options, 1)[1].split(";", 1)[0]
        self.assertEqual(view, "SELECT s.video_id,s.published_at FROM public.videos s\nWHERE s.published_at IS NOT NULL AND " + admission)
        self.assertEqual(schema.count("readable.video_publication_times"), 3)
        self.assertEqual(re.findall(r"^(?:GRANT|REVOKE|ALTER VIEW)\b[^\n;]*\breadable\.video_publication_times\b[^\n]*", schema, re.M),
                         ["ALTER VIEW readable.video_publication_times OWNER TO postgres;",
                          "GRANT SELECT ON readable.video_publication_times TO lic_reader;"])

    def test_inventory_counts_read_as_the_http_reader_from_any_caller(self):
        # The read model is granted to lic_reader only; the gateway picks it for HTTP reads.
        from db.base.read_context import is_http_read
        seen = []

        class Database(RecordingDatabase):
            def read_rows(self, *args, **kwargs):
                seen.append(is_http_read())
                return super().read_rows(*args, **kwargs)

        FrontendRepository(Database([]), read_clock=lambda: NOW).count_inventory_by_type(["a"], ["long"])
        self.assertEqual(seen, [True])
        self.assertFalse(is_http_read())

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
        db = RecordingDatabase(raw, [])
        repository = FrontendRepository(db, read_clock=lambda: NOW)
        rows, fetched = repository.load_channel_inventory_page("channel", video_type=" LONG ", offset=1000, page_size=1000)
        self.assertEqual([row["video_id"] for row in rows], ["current"])
        self.assertEqual(fetched, 2)
        self.assertEqual(db.calls[0][3], ("channel", "long", 1000, 1000))
        self.assertIn("published_at DESC NULLS FIRST, video_id DESC LIMIT %s OFFSET %s", db.calls[0][2])
        repository.load_channel_backfill_page("channel", video_type="stream", offset=7, page_size=3)
        self.assertIn("FROM public.video_inventory", db.calls[1][2])
        self.assertEqual(db.calls[1][3], ("channel", "stream", 3, 7))

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


class OtherReaderCapTests(unittest.TestCase):
    def test_saved_sources_preserve_exact_batched_id_coverage_without_sentinel(self):
        ids = [f"v{index:010d}" for index in range(501)]
        payload = [saved_source_payload(identity) for identity in ids]
        db = RecordingDatabase(list(reversed(payload[:500])), payload[500:])
        rows = SavedCollectionsRepository(db, read_clock=lambda: NOW).load_sources("c0000000-0000-4000-8000-000000000001", ids, as_of=NOW)
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
