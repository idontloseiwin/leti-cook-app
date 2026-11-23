"""Channels YouTube no longer returns: listed last, with since when and the date their stored data is removed.

Offline: the notice SQL itself runs as lic_reader in tests/reader_role_http_pg.py, and the writer of
the first missing day runs on PostgreSQL in tests/channel_profile_status_pg.py.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from collection.channel_metadata import CHANNEL_METADATA_STATUS_NOT_FOUND
from db.channel import channel_workflow_updates as channel_writer
from db.reads.frontend import GONE_CHANNEL_NOTICES_SQL, FrontendRepository
from hot_storage import RETENTION_HOURS
from http_test_support import cache_stub, endpoint
import ui_read_trace
from web.constants import CHANNEL_GONE_FROM_YOUTUBE_STATUS
from web.library import channel_list_order, create_library_http

NOW = datetime.now(timezone.utc)
CHECKED = (NOW - timedelta(hours=1)).isoformat()
GONE = CHANNEL_METADATA_STATUS_NOT_FOUND
DEADLINE_READ = "frontend_load_gone_channel_notices"


def row(channel_id, title=None, status="ok"):
    return {"channel_id": channel_id, "title": title, "channel_metadata_status": status,
            "channel_metadata_checked_at": CHECKED, "is_hidden": False, "source_expires_at": None}


class Database:
    def __init__(self, rows, deadlines=()):
        self.rows, self.deadlines, self.calls = rows, list(deadlines), []

    def read_rows(self, operation, sql, params=None, **kwargs):
        self.calls.append((operation, sql, params, kwargs))
        if operation == "frontend_load_channel_rows":
            return [dict(item) for item in self.rows]
        if operation == DEADLINE_READ:
            return [dict(item) for item in self.deadlines]
        if operation in ("frontend_load_folder_rows", "channel_labels_list", "channel_labels_assignments"):
            return []
        raise AssertionError("Unexpected query: " + operation)


def sidebar(database):
    library = create_library_http(cache=cache_stub(), database=database)
    return endpoint(library.router, "sidebar_data")()


class GoneChannelOrderTests(unittest.TestCase):
    def test_status_is_the_collector_status(self):
        self.assertEqual(CHANNEL_GONE_FROM_YOUTUBE_STATUS, CHANNEL_METADATA_STATUS_NOT_FOUND)

    def test_named_then_loading_then_gone_whatever_the_ids(self):
        rows = [row("UC0gone", status=GONE), row("UC1loading", status=None), row("UC2beta", "Beta"),
                row("UC3alpha", "alpha"), row("UC4error", status="error")]
        database = Database(rows, [{"channel_id": "UC0gone", "data_expires_at": None}])
        channels = sidebar(database)["channels"]
        # The stored title of a gone channel is withdrawn on read, so it can never decide its place.
        self.assertEqual([item["channel_id"] for item in channels],
                         ["UC3alpha", "UC2beta", "UC1loading", "UC4error", "UC0gone"])
        self.assertIsNone(channels[-1]["title"])

    def test_order_key_ignores_a_title_left_on_a_gone_row(self):
        named_gone = row("UCa", "Aardvark", status=GONE)
        self.assertGreater(channel_list_order(named_gone), channel_list_order(row("UCz", None, status="running")))


class GoneChannelDeadlineTests(unittest.TestCase):
    def test_only_gone_channels_carry_the_removal_date_and_first_missing_day(self):
        deadline, since = "2026-12-01T10:00:00Z", datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
        database = Database([row("UCok", "Fine"), row("UCgone", status=GONE), row("UCempty", status=GONE)],
                            [{"channel_id": "UCgone", "data_expires_at": deadline, "not_found_since": since},
                             {"channel_id": "UCempty", "data_expires_at": None, "not_found_since": None}])
        channels = {item["channel_id"]: item for item in sidebar(database)["channels"]}
        for field in ("youtube_data_expires_at", "channel_metadata_not_found_since"):
            self.assertNotIn(field, channels["UCok"])
            self.assertIn(field, channels["UCempty"])
            self.assertIsNone(channels["UCempty"][field])       # expired / unknown, not absent
        self.assertEqual(channels["UCgone"]["youtube_data_expires_at"], deadline)
        self.assertEqual(channels["UCgone"]["channel_metadata_not_found_since"], "2026-11-02T06:00:00Z")
        (_, _, params, options), = [call for call in database.calls if call[0] == DEADLINE_READ]
        self.assertEqual(params, {"ids": ["UCempty", "UCgone"]})
        self.assertEqual(options, {"max_rows": 2})

    def test_a_channel_the_deadline_read_did_not_see_as_gone_has_no_date(self):
        database = Database([row("UCraced", status=GONE)], [])
        channel, = sidebar(database)["channels"]
        self.assertNotIn("youtube_data_expires_at", channel)
        self.assertNotIn("channel_metadata_not_found_since", channel)

    def test_no_deadline_read_without_gone_channels(self):
        database = Database([row("UCok", "Fine"), row("UCloading", status=None)])
        sidebar(database)
        self.assertNotIn(DEADLINE_READ, [call[0] for call in database.calls])

    def test_repository_returns_utc_instants_and_skips_an_empty_request(self):
        database = Database([], [{"channel_id": "UCgone", "data_expires_at": datetime(2026, 12, 1, 12, tzinfo=timezone(timedelta(hours=2)))},
                                 {"channel_id": "UCempty", "data_expires_at": None}])
        database.deadlines[0]["not_found_since"] = datetime(2026, 11, 2, 8, tzinfo=timezone(timedelta(hours=2)))
        repository = FrontendRepository(database)
        self.assertEqual(repository.load_gone_channel_notices([]), {})
        self.assertEqual(database.calls, [])
        result = repository.load_gone_channel_notices(["UCgone", "UCempty", "UCgone", ""])
        self.assertEqual(result, {
            "UCgone": {"youtube_data_expires_at": "2026-12-01T10:00:00Z",
                       "channel_metadata_not_found_since": "2026-11-02T06:00:00Z"},
            "UCempty": {"youtube_data_expires_at": None, "channel_metadata_not_found_since": None}})
        self.assertEqual(database.calls[0][2], {"ids": ["UCempty", "UCgone"]})

    def test_sql_reads_only_reader_views_with_the_retention_window(self):
        relations = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+\.[a-z_]+)", GONE_CHANNEL_NOTICES_SQL))
        self.assertEqual(relations, {
            "readable.channels", "readable.videos", "readable.video_snapshots", "readable.video_optional_metadata",
            "readable.video_inventory_memberships", "readable.retained_video_identities", "readable.channel_snapshots"})
        self.assertEqual(re.findall(r"interval '(\d+) hours'", GONE_CHANNEL_NOTICES_SQL), [str(RETENTION_HOURS)])
        self.assertIn("channel_metadata_status='channel_not_found'", GONE_CHANNEL_NOTICES_SQL)
        # Leti Cook's own "not found" marker for a video is not YouTube data and never extends the date.
        self.assertIn("availability_status<>'unknown_unavailable'", GONE_CHANNEL_NOTICES_SQL)
        # The first missing day is read from readable.channels.
        self.assertIn("gone.channel_metadata_not_found_since AS not_found_since", GONE_CHANNEL_NOTICES_SQL)

    def test_the_first_missing_day_is_derived_on_every_metadata_status_write(self):
        def rendered(fields):
            return channel_writer._final_statement(fields).as_string(None)
        column = '"channel_metadata_not_found_since"='
        derived = rendered(("channel_metadata_checked_at", "channel_metadata_status"))
        self.assertEqual(derived.count(column), 1)
        self.assertIn(column + "CASE proposed.channel_metadata_status WHEN 'ok' THEN NULL", derived)
        # A caller cannot set it next to a status: the derived value wins.
        self.assertEqual(rendered(("channel_metadata_not_found_since", "channel_metadata_status")),
                         rendered(("channel_metadata_status",)))
        self.assertNotIn(column, rendered(("title", "views")))
        # Only the row before the write, the proposed status and the check time decide it.
        term = channel_writer._NOT_FOUND_SINCE_TERM.as_string(None)
        self.assertEqual(sorted(set(re.findall(r"\b(channel|proposed)\.(\w+)", term))),
                         [("channel", "channel_metadata_error"), ("channel", "channel_metadata_not_found_since"),
                          ("channel", "channel_metadata_status"), ("proposed", "channel_metadata_checked_at"),
                          ("proposed", "channel_metadata_status")])

    def test_the_read_is_observed_by_name(self):
        self.assertIn(DEADLINE_READ, ui_read_trace.READ_DATABASE_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
