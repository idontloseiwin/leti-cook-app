"""All-type Health read boundary: unavailable never means zero backlog."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from queue_health import VIDEO_COUNTS, DISCOVERY_COUNTS, validate_queue_health, validate_metadata_queue_health
from db.base.postgres import DatabaseError
from db.reads.queue_health_queries import (get_all_video_queue_health, get_all_discovery_queue_health,
    get_channel_metadata_queue_health, get_system_queue_health, SYSTEM_NO_DISCOVERY_SQL)


def envelope(unit="videos"):
    if unit == "channels":
        return dict(schema_version=1, scope="channel_profiles", unit=unit,
                    total=0, eligible_channels=0, policy_paused_channels=0,
                    due=0, failed=0, excluded_failed=0, running=0, null_schedule=0,
                    oldest_overdue_at=None, oldest_running_at=None, status_counts={})
    counts = VIDEO_COUNTS if unit == "videos" else DISCOVERY_COUNTS
    kinds = ("long", "short", "stream", "unknown") if unit == "videos" else ("long", "short", "stream")
    zero = dict.fromkeys(counts, 0)
    zero["oldest_overdue_at"] = None
    result = dict(zero, schema_version=2, scope="all_types", unit=unit,
                  channel_eligibility_checked=True, operational_eligibility_checked=False,
                  by_type={kind: dict(zero) for kind in kinds})
    if unit == "playlists":
        result["total_channels"] = 0
    return result


class Database:
    def __init__(self, row):
        self.row, self.calls = row, []

    @contextmanager
    def transaction(self, operation):
        self.calls.append(operation)
        yield self

    def fetch_one(self, statement):
        self.calls.append(statement)
        return self.row


class QueueHealthBoundaryTests(unittest.TestCase):
    def test_empty_queue_is_explicit_and_not_unknown(self):
        for unit in ("videos", "playlists"):
            value = envelope(unit)
            self.assertEqual(validate_queue_health(value, unit=unit), value)

    def test_one_short_without_long_has_exact_totals_and_no_readiness_inference(self):
        value = envelope()
        for row in (value, value["by_type"]["short"]):
            row.update(total=2, due=1, metadata_due=1, failed=1,
                       oldest_overdue_at="2026-10-22T12:00:00+00:00")
        result = validate_queue_health(value, unit="videos")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["by_type"]["long"]["total"], 0)
        self.assertFalse(result["operational_eligibility_checked"])

    def test_missing_schema33_addition_and_malformed_counts_are_unavailable(self):
        broken = [None, {}, {"schema_version": 1}]
        for field, bad in (("due", True), ("failed", -1), ("total", "5")):
            value = envelope()
            value[field] = bad
            broken.append(value)
        missing_type = envelope()
        del missing_type["by_type"]["stream"]
        broken.append(missing_type)
        broken.append(dict(envelope(), operational_eligibility_checked=True))
        for value in broken:
            with self.subTest(value=value), self.assertRaises(DatabaseError):
                get_all_video_queue_health(Database({"value": value}))

    def test_totals_cannot_hide_disagreement_with_type_buckets(self):
        value = envelope()
        value["total"] = 1
        with self.assertRaises(ValueError):
            validate_queue_health(value, unit="videos")

    def test_channel_and_playlist_counts_are_distinct(self):
        value = envelope("playlists")
        value.update(total=3, total_channels=1)
        for row in value["by_type"].values():
            row["total"] = 1
        self.assertEqual(validate_queue_health(value, unit="playlists")["total_channels"], 1)

    def test_metadata_failures_are_not_multiplied_by_video_types(self):
        value = envelope("channels")
        value.update(total=3, eligible_channels=2, policy_paused_channels=1,
                     due=1, failed=1, excluded_failed=1, running=1,
                     status_counts={"error": 1, "running": 1},
                     oldest_overdue_at="2026-10-22T12:00:00+00:00",
                     oldest_running_at="2026-10-22T12:01:00+00:00")
        self.assertEqual(get_channel_metadata_queue_health(Database({"value": value})), value)

    def test_metadata_unknown_or_partial_observations_never_become_zero(self):
        for value in (None, {}, dict(envelope("channels"), total=1),
                      dict(envelope("channels"), status_counts={"error": 1})):
            with self.subTest(value=value), self.assertRaises(DatabaseError):
                get_channel_metadata_queue_health(Database({"value": value}))
        self.assertEqual(validate_metadata_queue_health(envelope("channels")), envelope("channels"))

    def test_home_uses_one_transaction_for_all_current_queues(self):
        value = {"videos": envelope(), "channel_discovery": envelope("playlists"),
                 "channel_metadata": envelope("channels")}
        db = Database({"value": value})
        self.assertEqual(get_system_queue_health(db), value)
        self.assertEqual(len(db.calls), 2)
        self.assertTrue(db.calls[0].read_only)
        self.assertEqual(db.calls[0].statement_timeout_ms, 5000)
        self.assertEqual(db.calls[0].total_timeout_ms, 8000)
        value["channel_metadata"] = None
        with self.assertRaises(DatabaseError):
            get_system_queue_health(Database({"value": value}))

    def test_home_disabled_discovery_is_omitted_not_reported_empty(self):
        value = {"videos": envelope(), "channel_discovery": None, "channel_metadata": envelope("channels")}
        db = Database({"value": value})
        self.assertEqual(get_system_queue_health(db, discovery_enabled=False),
                         dict(value, channel_discovery={"mode": "disabled", "due": None, "failed": None}))
        self.assertEqual(db.calls[1], SYSTEM_NO_DISCOVERY_SQL)
        self.assertNotIn("readable.channel_playlist_state", db.calls[1])
        with self.assertRaises(DatabaseError):
            get_system_queue_health(Database({"value": value}))

    def test_scoped_reader_has_short_readonly_budget_and_never_retries(self):
        for reader, unit in ((get_all_video_queue_health, "videos"),
                             (get_all_discovery_queue_health, "playlists"),
                             (get_channel_metadata_queue_health, "channels")):
            db = Database({"value": envelope(unit)})
            reader(db)
            operation = db.calls[0]
            self.assertTrue(operation.read_only)
            self.assertEqual(operation.statement_timeout_ms, 5000)
            self.assertEqual(operation.lock_timeout_ms, 250)
            self.assertEqual(operation.total_timeout_ms, 8000)
            self.assertEqual(len(db.calls), 2)
        invalid = Database(None)
        with self.assertRaises(DatabaseError):
            get_all_video_queue_health(invalid)
        self.assertEqual(len(invalid.calls), 2)

    def test_schema_change_has_only_view_ddl_and_preserves_prior_steps(self):
        text = (ROOT / "schema/steps/0034-all-type-queue-health.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE OR REPLACE VIEW readable.typed_video_collection_health", text)
        for effect in ("UPDATE public.", "DELETE FROM", "INSERT INTO", "ALTER TABLE", "GRANT ", "REVOKE "):
            self.assertNotIn(effect, text)
        from db.videos.video_initial_policy import VIDEO_HANDOFF_STATE_SQL
        self.assertIn(VIDEO_HANDOFF_STATE_SQL, text)


if __name__ == "__main__":
    unittest.main()
