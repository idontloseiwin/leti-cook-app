"""Formats uses an unordered complete bag, retaining the old truncated prefix."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
import channel_explorer as builder
from catalogue_scope import CatalogueScopeError
from db.base.postgres import DatabaseUnavailable, _INVENTORY_SOURCE
from db.reads import channel_explorer as owner
from db.reads.channel_explorer import ChannelExplorerRepository
from hot_storage import HotSourceReadExpired, capture_read_lifetime
from test_direct_pg_read_repositories import RecordingDatabase
from test_format_totals import Repository, NOW, iso, video

EXPIRY = NOW + timedelta(days=1)


def inventory(key, *, published=None, expiry=EXPIRY):
    return dict(video_id=key, published_at=published, source_checked_at=iso(NOW),
                source_expires_at=iso(expiry))


class TypedAggregateLoaderTests(unittest.TestCase):
    def repository(self, *pages, clock=None):
        database = RecordingDatabase(*pages)
        return ChannelExplorerRepository(database, read_clock=clock or (lambda: NOW)), database

    def test_each_type_uses_one_protected_id_stream_and_explicit_empty_eof(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                first = [inventory("a"), inventory("b", published=iso(NOW))]
                second = [inventory("c")]
                repo, db = self.repository(first, second, [])
                with patch.object(owner, "INVENTORY_PAGE_SIZE", 2):
                    result = repo.load_format_inventory("channel'quoted", video_type=kind, limit=7)
                self.assertEqual(result, first + second)
                self.assertEqual(len(db.calls), 3)
                for call in db.calls:
                    self.assertEqual(call[1], "channel_explorer_inventory_page")
                    self.assertIn("FROM readable.hot_video_inventory", call[2])
                    self.assertIn("ORDER BY video_id ASC", call[2])
                    self.assertNotIn("published_at <", call[2])
                    self.assertNotIn("channel'quoted", call[2])
                self.assertTrue(all(call[3] == ("channel'quoted", kind) for call in db.calls))
                self.assertEqual(len(db.stream_declarations), 1)
                self.assertEqual(db.stream_closes, 1)

    def test_zero_limit_and_invalid_type_do_not_read(self):
        repo, db = self.repository()
        self.assertEqual([], repo.load_format_inventory("c", video_type="long", limit=0))
        with self.assertRaises(ValueError):
            repo.load_format_inventory("c", video_type="uploads", limit=3)
        self.assertEqual([], db.calls)

    def test_empty_bag_is_known_empty(self):
        repo, db = self.repository([])
        self.assertEqual([], repo.load_format_inventory("c", video_type="short", limit=2))
        self.assertEqual(1, len(db.calls))

    def test_exact_cap_without_lookahead_row_remains_full_and_no_fallback(self):
        rows = [inventory("a"), inventory("b")]
        repo, db = self.repository(rows, [])
        with patch.object(owner, "INVENTORY_PAGE_SIZE", 2):
            self.assertEqual(rows, repo.load_format_inventory("c", video_type="long", limit=3))
        self.assertEqual(db.calls[-1][3:], (("c", "long"), 1))
        self.assertTrue(all("ORDER BY video_id ASC" in call[2] for call in db.calls))

    def test_overflow_returns_exact_publication_prefix_with_its_own_deadline(self):
        early = NOW + timedelta(seconds=1)
        late = NOW + timedelta(hours=2)
        discarded = [inventory("a", expiry=early), inventory("b", expiry=early)]
        prefix = [inventory("z", published=iso(NOW), expiry=late),
                  inventory("y", published=iso(NOW-timedelta(days=1)), expiry=late),
                  inventory("x", expiry=late)]
        # The overflow falls back to the publication prefix, read in one statement.
        repo, db = self.repository(discarded, [inventory("c", expiry=early)], prefix)
        with patch.object(owner, "INVENTORY_PAGE_SIZE", 2), capture_read_lifetime() as lifetime:
            self.assertEqual(prefix, repo.load_format_inventory("c", video_type="stream", limit=3))
        self.assertEqual(late, lifetime.expires_at)
        self.assertEqual(3, len(db.calls))
        self.assertEqual(db.calls[2][3], ("c", "stream", 3))
        self.assertIn("ORDER BY published_at DESC NULLS LAST, video_id ASC", db.calls[2][2])
        self.assertEqual(len(discarded), 2)  # Do not mutate a supplied DB result.

    def test_complete_bag_registers_the_earliest_returned_source(self):
        late = NOW + timedelta(hours=2)
        repo, _ = self.repository([inventory("a", expiry=late)], [inventory("b")], [])
        with capture_read_lifetime() as lifetime:
            repo.load_format_inventory("c", video_type="long", limit=5)
        self.assertEqual(late, lifetime.expires_at)

    def test_source_expiring_during_walk_fails_closed_after_eof(self):
        deadline = NOW + timedelta(seconds=2)
        clock = [NOW]
        repo, db = self.repository([inventory("a", expiry=deadline)], [], clock=lambda: clock[0])
        original = db.read_rows
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if not result:
                clock[0] = deadline
            return result
        db.read_rows = read
        with self.assertRaises(HotSourceReadExpired):
            repo.load_format_inventory("c", video_type="long", limit=3)
        self.assertEqual(2, len(db.calls))

    def test_invalid_shape_identity_and_cross_page_duplicates_refuse(self):
        cases = [(None,), ({},), ([None],), ([inventory(None)],),
                 ([inventory("bad id")],), ([inventory("a"), inventory("a")],),
                 ([inventory("a"), inventory("b")], [inventory("a"), inventory("c")])]
        for pages in cases:
            with self.subTest(pages=pages):
                repo, _ = self.repository(*pages)
                with self.assertRaises(CatalogueScopeError):
                    repo.load_format_inventory("c", video_type="long", limit=6)

    def test_database_collation_owns_cursor_order(self):
        repo, _ = self.repository([inventory("z")], [inventory("A")], [])
        self.assertEqual(["z", "A"], [r["video_id"] for r in repo.load_format_inventory("c", video_type="long", limit=4)])

    def test_unknown_read_never_falls_back_retries_or_claims_eof(self):
        error = DatabaseUnavailable("database_connection_failed", outcome_unknown=True)
        repo, db = self.repository([inventory("a")], error)
        with self.assertRaises(DatabaseUnavailable) as got:
            repo.load_format_inventory("c", video_type="long", limit=4)
        self.assertIs(error, got.exception)
        self.assertEqual(2, len(db.calls))


class TotalsRepository(Repository, ChannelExplorerRepository):
    """Execute the real page loaders; other established format inputs are fixed."""
    load_format_inventory = ChannelExplorerRepository.load_format_inventory
    load_inventory = ChannelExplorerRepository.load_inventory
    load_long_inventory = ChannelExplorerRepository.load_long_inventory

    def __init__(self, scenario):
        rows = [video(f"v{i}", views=2**53+i if i == 0 else i+2) for i in range(12)]
        Repository.__init__(self, rows[:4], rows[4:8])
        self.lists["stream"] = rows[8:]
        self.videos.update({r["video_id"]:r for r in rows[8:]})
        self.items = {kind:[inventory(r["video_id"], published=None if i == 0 else iso(NOW-timedelta(days=i % 2+1)),
                                     expiry=NOW+timedelta(hours=i+1)) for i,r in enumerate(values)]
                      for kind,values in self.lists.items()}
        self.calls = []
        self.resolutions = {}
        if scenario == "withheld":
            del self.videos["v1"]
            self.videos["v2"]["availability_status"] = "private"
            self.videos["v4"]["checked_at"] = iso(NOW-timedelta(days=30))
            self.videos["v5"]["availability_checked_at"] = iso(NOW-timedelta(days=30))
            self.resolutions["v6"] = dict(resolution_status="conflict")
            self.resolutions["v7"] = dict(manual_valid=True, present_membership_types=[])
            self.videos["v8"]["stream_actual_end_at"] = None
            self.videos["v9"]["metadata_checked_at"] = iso(NOW-timedelta(days=30))
            self.videos["v10"]["views"] = None
        ChannelExplorerRepository.__init__(self, self, read_clock=lambda: NOW)

    @contextmanager
    def transaction(self, operation):
        repository = self
        assert operation.name == 'channel_explorer_inventory_page' and operation.read_only
        class ReadTransaction:
            @contextmanager
            def inventory_source_rows(self, channel_id, video_type):
                rows = sorted(deepcopy(repository.items[video_type]), key=lambda row: row['video_id'])
                class SourceRows:
                    offset = 0
                    def fetch(self, size):
                        repository.calls.append((operation.name, _INVENTORY_SOURCE,
                                                 (channel_id, video_type), size))
                        page = rows[self.offset:self.offset+size]
                        self.offset += len(page)
                        return page
                yield SourceRows()
        yield ReadTransaction()

    def read_rows(self, operation, statement, params, *, max_rows):
        self.calls.append((operation, statement, tuple(params), max_rows))
        channel, kind, *cursor, size = params
        rows = deepcopy(self.items[kind])
        if "ORDER BY video_id ASC" in statement:
            rows.sort(key=lambda row:row["video_id"])
            if cursor:rows=[r for r in rows if r["video_id"]>cursor[0]]
        else:
            rows.sort(key=lambda row:row["video_id"])
            rows.sort(key=lambda row:row["published_at"] or "", reverse=True)
            if cursor:
                if len(cursor)==1:rows=[r for r in rows if r["published_at"] is None and r["video_id"]>cursor[0]]
                else:
                    pub, _, key = cursor
                    rows=[r for r in rows if r["published_at"] is None or r["published_at"]<pub or r["published_at"]==pub and r["video_id"]>key]
        return rows[:size]

    def load_type_resolutions(self, video_ids, *, video_type):
        return [dict(row, **self.resolutions.get(row["video_id"], {}))
                for row in Repository.load_type_resolutions(self, video_ids, video_type=video_type)]


def scenario_result(build, scenario):
    repo=TotalsRepository(scenario)
    cap=2 if scenario=="overflow" else 100
    with patch.object(owner,"INVENTORY_PAGE_SIZE",2), patch.object(builder,"FORMAT_TOTAL_SCAN_LIMIT",cap):
        result=build(repo,"c",include_typed=True,now=NOW)
    return result,repo


class FullJsonEquivalenceTests(unittest.TestCase):
    def test_complete_withheld_and_overflow_match_saved82_entire_json(self):
        baseline=json.loads((ROOT/'tests/fixtures/format_totals_id_baseline.json').read_text(encoding='utf8'))
        for scenario in ("complete","withheld","overflow"):
            with self.subTest(scenario=scenario):
                result,repo=scenario_result(builder.build_channel_format_totals,scenario)
                self.assertEqual(baseline[scenario],result)
                id_calls=[c for c in repo.calls if "ORDER BY video_id ASC" in c[1]]
                old_calls=[c for c in repo.calls if "ORDER BY published_at DESC" in c[1]]
                self.assertTrue(id_calls)
                self.assertEqual({c[2][1] for c in id_calls},{"long","short","stream"})
                self.assertEqual(bool(old_calls),scenario=="overflow")

    def test_public_bounded_order_stays_publication_order(self):
        repo=TotalsRepository("complete")
        rows=repo.load_inventory("c",video_type="long",limit=3)
        self.assertEqual([r["video_id"] for r in rows],["v2","v1","v3"])
        self.assertTrue(all("ORDER BY published_at DESC" in c[1] for c in repo.calls))

if __name__ == '__main__':unittest.main()
