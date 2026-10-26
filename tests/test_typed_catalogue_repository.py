"""Typed catalogue boundaries using local recording connections, never a server."""
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from catalogue_scope import CatalogueReadBudget, CatalogueScopeError, CatalogueScopeRepository
from db.base import postgres as pg
from db.base.read_context import http_method_scope, http_read_scope
from db.reads import analytics_read_queries as queries
from db.reads.channel_explorer import ChannelExplorerRepository
from db.reads.channel_video_growth import ChannelVideoGrowthRepository
from hot_storage import HotSourceReadExpired
from test_inventory_source_stream import SourceConnection, Pool, DSN, NOW, CHANNEL, row
from test_analytics_read_queries import RecordingDatabase, growth_row, VIDEO


class TypedInventoryTests(unittest.TestCase):
    def repository(self, rows, *, clock=None):
        connection = SourceConnection(rows)
        pool = Pool(connection, connection.events)
        database = pg.PostgresDatabase(
            pg.PostgresConfig(DSN), admission=lambda name: None,
            pool_factory=lambda config: pool,
        ).open()
        self.addCleanup(database.close)
        return ChannelExplorerRepository(database, read_clock=clock or (lambda: NOW)), connection

    def test_each_type_uses_one_bound_protected_cursor_and_full_eof(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                repo, connection = self.repository([row(n) for n in range(1001)])
                with http_method_scope("GET"), http_read_scope():
                    rows = CatalogueScopeRepository(repo).load_full_inventory(CHANNEL, video_type=kind)
                self.assertEqual(len(rows), 1001)
                self.assertEqual(len(connection.queries), 1)
                statement, params = connection.queries[0]
                self.assertEqual(params, (CHANNEL, kind))
                self.assertIn("FROM readable.hot_video_inventory", statement)
                self.assertIn("channel_id=%s AND video_type=%s", statement)
                self.assertNotIn(CHANNEL, statement)
                self.assertEqual(connection.source_sizes, [1000, 1000, 1000])
                self.assertEqual((connection.source_closes, connection.commits), (1, 1))

    def test_invalid_type_cannot_open_source_or_fall_back_to_long(self):
        for kind in ("all", "shorts", "", None, [], "short' OR true --"):
            with self.subTest(kind=kind):
                repo, connection = self.repository([])
                with self.assertRaisesRegex(ValueError, "unsupported video type"):
                    list(repo.iter_inventory_pages(CHANNEL, video_type=kind))
                with self.assertRaisesRegex(ValueError, "unsupported video type"):
                    CatalogueScopeRepository(repo).load_full_inventory(CHANNEL, video_type=kind)
                self.assertEqual(connection.queries, [])

    def test_legacy_long_fake_and_alias_need_no_new_method_or_keyword(self):
        class LongOnly:
            def iter_long_inventory_pages(self, channel_id):
                self.channel_id = channel_id
                yield [row(1)]
        legacy = LongOnly()
        scope = CatalogueScopeRepository(legacy)
        self.assertEqual(scope.load_full_inventory(CHANNEL), [row(1)])
        self.assertEqual(scope.load_full_long_inventory(CHANNEL), [row(1)])
        self.assertEqual(legacy.channel_id, CHANNEL)
        repo, connection = self.repository([row(1)])
        self.assertEqual(list(repo.iter_long_inventory_pages(CHANNEL)), [[row(1)]])
        self.assertEqual(connection.queries[0][1], (CHANNEL, "long"))

    def test_typed_read_rechecks_earliest_deadline_after_eof(self):
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                clock = [NOW]
                repo, connection = self.repository([row(0, expiry=NOW + timedelta(seconds=1))],
                                                   clock=lambda: clock[0])
                connection.on_fetch = lambda values: clock.__setitem__(0, NOW + timedelta(seconds=2)) if not values else None
                with self.assertRaises(HotSourceReadExpired):
                    CatalogueScopeRepository(repo).load_full_inventory(CHANNEL, video_type=kind)
                self.assertEqual((connection.source_closes, connection.commits), (1, 1))

    def test_typed_read_budget_abort_closes_source_without_second_query(self):
        repo, connection = self.repository([row(n) for n in range(1001)])
        scope = CatalogueScopeRepository(repo, CatalogueReadBudget(max_bytes=1))
        with self.assertRaisesRegex(CatalogueScopeError, "byte_budget"):
            scope.load_full_inventory(CHANNEL, video_type="short")
        self.assertEqual(len(connection.queries), 1)
        self.assertEqual((connection.source_closes, connection.rollbacks), (1, 1))

    def test_typed_scope_rejects_duplicate_identity_across_pages(self):
        class DuplicateSource:
            def iter_inventory_pages(self, channel_id, *, video_type):
                self.selected_type = video_type
                try:
                    yield [row(1)]
                    yield [row(1)]
                finally:
                    self.closed = True
        source = DuplicateSource()
        with self.assertRaisesRegex(CatalogueScopeError, "identity_invalid"):
            CatalogueScopeRepository(source).load_full_inventory(CHANNEL, video_type="stream")
        self.assertEqual(source.selected_type, "stream")
        self.assertTrue(source.closed)

    def test_resolution_response_carries_canonical_channel_proof(self):
        proof = dict(video_id=VIDEO, canonical_channel_id=CHANNEL,
                     canonical_video_type="short", resolution_status="resolved")
        database = RecordingDatabase([proof])
        rows = ChannelExplorerRepository(database).load_type_resolutions([VIDEO], video_type="short")
        self.assertEqual(rows, [proof])
        self.assertEqual(database.calls[0][2], ("short", [VIDEO]))
        self.assertIn("proof.canonical_channel_id", database.calls[0][1])


class TypedGrowthTests(unittest.TestCase):
    def test_requested_type_is_bound_by_real_query_owner_with_no_type_interpolation(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                database = RecordingDatabase([growth_row()])
                result = ChannelVideoGrowthRepository(database).load_video_view_growth(
                    CHANNEL, [VIDEO], as_of=NOW, video_type=kind,
                )
                self.assertEqual(result, [growth_row()])
                self.assertEqual(len(database.calls), 1)
                operation, statement, params, maximum = database.calls[0]
                self.assertEqual((operation, maximum), ("analytics_video_growth", 1))
                self.assertEqual(params["video_type"], kind)
                self.assertEqual(params["channel_id"], CHANNEL)
                self.assertEqual(params["video_ids"], [VIDEO])
                self.assertIs(statement, queries.GROWTH_SQL)
                self.assertIn("inventory.video_type = %(video_type)s::text", statement)
                self.assertNotIn("inventory.video_type = 'long'", statement)

    def test_invalid_type_is_rejected_even_for_empty_ids_before_dispatch(self):
        for kind in ("all", "shorts", None, [], "stream' OR true --"):
            with self.subTest(kind=kind):
                database = RecordingDatabase([])
                with self.assertRaises(pg.DatabaseError):
                    queries.read_video_view_growth_28d(
                        database, channel_id=CHANNEL, video_ids=[], as_of=NOW, video_type=kind,
                    )
                with self.assertRaises(ValueError):
                    ChannelVideoGrowthRepository(database).load_video_view_growth(
                        CHANNEL, [], as_of=NOW, video_type=kind,
                    )
                with self.assertRaises(ValueError):
                    CatalogueScopeRepository(Mock()).load_video_view_growth(
                        CHANNEL, [], as_of=NOW, video_type=kind,
                    )
                self.assertEqual(database.calls, [])

    def test_scope_preserves_type_across_bounded_growth_batches(self):
        class TypedSource:
            def __init__(self):
                self.calls = []
            def load_video_view_growth(self, channel_id, ids, *, as_of, recorded_from, video_type):
                self.calls.append((channel_id, len(ids), as_of, recorded_from, video_type))
                return [{"video_id": value} for value in ids]
        source = TypedSource()
        ids = [f"v{n:010d}" for n in range(501)]
        rows = CatalogueScopeRepository(source).load_video_view_growth(
            CHANNEL, ids, as_of=NOW, recorded_from=NOW - timedelta(days=5), video_type="short",
        )
        self.assertEqual([item["video_id"] for item in rows], ids)
        self.assertEqual([call[1] for call in source.calls], [500, 1])
        self.assertTrue(all(call[0] == CHANNEL and call[4] == "short" for call in source.calls))

    def test_long_scope_and_repository_omit_new_keyword_for_existing_callers(self):
        class LongOnly:
            def load_video_view_growth(self, channel_id, ids, *, as_of):
                return [{"video_id": value} for value in ids]
        self.assertEqual(CatalogueScopeRepository(LongOnly()).load_video_view_growth(
            CHANNEL, [VIDEO], as_of=NOW,
        ), [{"video_id": VIDEO}])
        with patch("db.reads.channel_video_growth.read_video_view_growth_28d", return_value=[growth_row()]) as read:
            ChannelVideoGrowthRepository(Mock()).load_video_view_growth(CHANNEL, [VIDEO], as_of=NOW)
            self.assertNotIn("video_type", read.call_args.kwargs)

    def test_growth_uses_canonical_fresh_scope_and_completed_stream_snapshots(self):
        statement = queries.GROWTH_SQL
        self.assertIn("from readable.hot_video_inventory as inventory", statement)
        self.assertIn("channel.is_hidden is not true", statement)
        self.assertIn("inventory.source_checked_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds)", statement)
        self.assertIn("video.channel_id = %(channel_id)s::text", statement)
        self.assertIn("video.checked_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds)", statement)
        self.assertIn("video.metadata_checked_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds)", statement)
        self.assertIn("isfinite(video.stream_actual_end_at)", statement)
        self.assertIn("video.stream_actual_end_at <= video.metadata_checked_at", statement)
        self.assertIn("video.stream_actual_end_at <= video.checked_at", statement)
        # Endpoint, full-window start and partial-window start reads.
        self.assertEqual(statement.count("snapshot.recorded_at >= scoped.stream_actual_end_at"), 3)
        self.assertIn("from readable.video_snapshots as snapshot", statement)
        self.assertIn("snapshot.recorded_at = scoped.current_checked_at", statement)
        # The protected inventory view is the proof owner, rather than another
        # raw table join that could admit ambiguous or expired membership.
        schema = (ROOT / "schema/schema.sql").read_text()
        view = schema.rsplit("CREATE OR REPLACE VIEW readable.hot_video_inventory", 1)[1].split(";", 1)[0]
        for proof in ("resolution.canonical_channel_id = inv.channel_id",
                      "resolution.canonical_video_type = inv.video_type",
                      "resolution.resolution_status = 'resolved'",
                      "proof.last_present_at IS NOT NULL",
                      "publication.publication_status IS NOT NULL"):
            self.assertIn(proof, view)


if __name__ == "__main__":
    unittest.main()
