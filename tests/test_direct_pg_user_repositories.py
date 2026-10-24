"""Direct user-work and operational repository contracts; no database or network."""
from contextlib import contextmanager
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from psycopg.types.json import Jsonb
from hot_storage import iso_utc
from db.user_work.channel_labels import ChannelLabelRepository
from db.user_work.channel_organization import ChannelOrganizationRepository
from db.channel.channel_trash import ChannelTrashRepository
from db.base.postgres import DatabaseUnavailable
from db.user_work.saved_collections import SavedCollectionsRepository
from db.user_work.saved_views import SavedViewsRepository
from db.runs.sync_runs import SyncRunRepository
from db.reads.system_health import CHANNEL_HEALTH_COLUMNS, CHANNEL_HEALTH_ORDER, SystemHealthRepository
from db.user_work.user_work_backup import UserWorkBackupRepository
from db.reviews.video_type_reviews import VideoTypeReviewsRepository
from db.quota.youtube_quota import YouTubeQuotaRepository

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


class DirectDatabase:
    """Only the new driver protocol is available; legacy chains fail immediately."""
    def __init__(self, *results):
        self.results = deque(results)
        self.calls = []

    def _result(self):
        if not self.results:
            raise AssertionError("unexpected database call")
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def _sql(self, mode, operation, statement, parameters, maximum, error_codes):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", operation):
            raise AssertionError("unsafe operation identity")
        self.calls.append({"mode": mode, "operation": operation,
                           "sql": statement if isinstance(statement, str) else statement.as_string(),
                           "params": dict(parameters) if isinstance(parameters, Mapping) else tuple(parameters), "max_rows": maximum, "error_codes": error_codes})
        return self._result()

    def read_rows(self, operation, statement, parameters=(), *, max_rows):
        rows = self._sql("read", operation, statement, parameters, max_rows, frozenset())
        if operation == 'analytics_saved_sources':
            rows = [dict(row, source_expires_at=None) for row in rows]
        return rows

    def read_row(self, operation, statement, parameters=()):
        return self._sql("read_one", operation, statement, parameters, 1, frozenset())

    def write_rows(self, operation, statement, parameters=(), *, max_rows, error_codes=frozenset()):
        return self._sql("returning", operation, statement, parameters, max_rows, error_codes)

    def write(self, operation, statement, parameters=(), *, error_codes=frozenset()):
        return self._sql("write", operation, statement, parameters, None, error_codes)

    def call(self, name, parameters=None, *, max_rows=1000):
        self.calls.append({"mode": "function", "operation": name, "params": parameters, "max_rows": max_rows})
        return self._result()


class TransactionDatabase(DirectDatabase):
    def __init__(self, *results):
        super().__init__(*results)
        self.transactions=[]
        self.ends=[]
        self.owner=object()
    @contextmanager
    def transaction(self, operation):
        self.transactions.append(operation)
        try: yield self.owner
        except BaseException:
            self.ends.append('rollback')
            raise
        else: self.ends.append('commit')


class SavedWorkTests(unittest.TestCase):
    def test_object_and_member_cursors_are_bound_with_explicit_tie_breakers(self):
        db = DirectDatabase([], [])
        repository = SavedCollectionsRepository(db)
        injection = "v'); DROP TABLE saved_objects; --"
        repository.list_objects(101, (NOW.isoformat(), injection))
        repository.list_members(injection, 51, (NOW.isoformat(), injection))
        for call in db.calls:
            self.assertNotIn(injection, call["sql"])
            self.assertIn(injection, call["params"])
            self.assertIn(" DESC", call["sql"])
            self.assertEqual(call["max_rows"], call["params"][-1])
        self.assertIn("(created_at,id)<(%s::timestamptz,%s::uuid)", db.calls[0]["sql"])
        self.assertIn("(added_at,video_id)<(%s::timestamptz,%s)", db.calls[1]["sql"])

    def test_source_batch_order_coverage_and_trusted_clock(self):
        ids = [f"v{i:010d}" for i in range(501)]
        def source(video_id):
            return {"video_id": video_id, "channel_id": None, "source_status": "unavailable",
                    "source_reason": "inventory_missing", "video_row": None}
        db = DirectDatabase([source(v) for v in reversed(ids[:500])], [source(ids[-1])])
        repository = SavedCollectionsRepository(db, read_clock=lambda: NOW)
        rows = repository.load_sources(ids, as_of=NOW + timedelta(days=1))
        self.assertEqual([row["video_id"] for row in rows], ids)
        self.assertEqual([call["max_rows"] for call in db.calls], [500, 1])
        self.assertTrue(all(call["params"]["as_of"] == iso_utc(NOW) for call in db.calls))
        self.assertTrue(all(call["operation"] == "analytics_saved_sources" for call in db.calls))

    def test_source_incomplete_or_duplicate_identity_is_rejected(self):
        for result in ([], [{"video_id": "other"}], [{"video_id": "a"}, {"video_id": "a"}]):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                SavedCollectionsRepository(DirectDatabase(result), read_clock=lambda: NOW).load_sources(["a"], as_of=NOW)

    def test_view_channel_batch_is_bounded_and_hidden_channels_excluded(self):
        db = DirectDatabase([{"channel_id": "a", "is_hidden": False}, {"channel_id": "b", "is_hidden": True}])
        repository = SavedViewsRepository(db)
        self.assertEqual(repository.load_available_channels(["b", "a", "a"]), {"a"})
        self.assertEqual(db.calls[0]["params"], (["a", "b"], 2))
        self.assertIn("ANY(%s::text[])", db.calls[0]["sql"])
        self.assertEqual(repository.load_available_channels([]), set())
        with self.assertRaises(ValueError):
            repository.load_available_channels([str(i) for i in range(101)])

    def test_mutations_preserve_explicit_null_and_do_not_retry(self):
        failure = DatabaseUnavailable("database_unavailable", outcome_unknown=True)
        db = DirectDatabase()
        repository = SavedCollectionsRepository(db)
        with patch('db.user_work.saved_mutations.create_saved_object',return_value={'outcome':'ready'}) as create:
            repository.create_object("id", "name", "fingerprint")
            create.assert_called_once_with(db,kind='collection',object_id='id',name='name',fingerprint='fingerprint',channel_id=None,conditions=None)
        with patch('db.user_work.saved_collections.change_saved_collection_members',side_effect=failure) as mutate:
            with self.assertRaises(DatabaseUnavailable) as caught:
                repository.change_members("id", "request", "add", ["a"], 1, "hash")
            self.assertIs(caught.exception, failure)
            mutate.assert_called_once_with(db,collection_id='id',request_id='request',action='add',video_ids=['a'],expected_revision=1,fingerprint='hash')
        self.assertEqual(db.calls, [])

    def test_saved_rename_delete_preserve_kind_revision_and_result_shape(self):
        db = DirectDatabase()
        repository = SavedViewsRepository(db)
        with patch('db.user_work.saved_mutations.rename_saved_object',return_value={'outcome':'conflict'}) as rename:
            self.assertEqual(repository.rename_object('id','name',7), {'outcome':'conflict'})
            rename.assert_called_once_with(db,kind='view',object_id='id',name='name',expected_revision=7)
        with patch('db.user_work.saved_mutations.delete_saved_object',return_value=[]) as delete:
            with self.assertRaisesRegex(RuntimeError,'Invalid Saved mutation response'):repository.delete_object('id',7)
            delete.assert_called_once_with(db,kind='view',object_id='id',expected_revision=7)
        self.assertEqual(db.calls, [])

    def test_page_limits_and_plain_response_validation(self):
        repository = SavedCollectionsRepository(DirectDatabase())
        for value in (0, 102, True):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                repository.list_objects(value)
        with self.assertRaises(RuntimeError):
            SavedCollectionsRepository(DirectDatabase({"id": "x"})).get_object("x")


class OrganizationTests(unittest.TestCase):
    def test_label_caps_and_safe_limit_error_allowlist(self):
        db = DirectDatabase([], [], [{"label_key": "key"}], 1)
        repository = ChannelLabelRepository(db)
        repository.load_labels()
        repository.load_assignments()
        self.assertEqual(repository.create_label({"label_key": "key", "name": "O'Reilly", "label_type": "custom"}), {"label_key": "key"})
        repository.assign_label("channel", "key")
        self.assertEqual([c["max_rows"] for c in db.calls[:3]], [1001, 100000, 1])
        self.assertNotIn("O'Reilly", db.calls[2]["sql"])
        self.assertTrue(all(c["error_codes"] == frozenset({"user_work_limit_reached"}) for c in db.calls[2:]))
        self.assertIn("ON CONFLICT (channel_id,label_key) DO UPDATE", db.calls[3]["sql"])
        self.assertNotIn("created_at", db.calls[3]["sql"])

    def test_folder_null_assignment_and_insert_defaults_are_bound(self):
        db = DirectDatabase(1, 1, 1, 1)
        repository = ChannelOrganizationRepository(db)
        repository.create_folder({"id": "identity", "name": "Folder"})
        repository.assign_folder("channel", None)
        repository.rename_folder("identity", "New")
        repository.delete_folder("identity")
        self.assertNotIn("created_at", db.calls[0]["sql"])
        self.assertEqual(db.calls[0]["error_codes"], frozenset({"user_work_limit_reached"}))
        self.assertEqual(db.calls[1]["params"], (None, "channel"))
        self.assertIn("folder_id=%s::uuid", db.calls[1]["sql"])

    def test_trash_preserves_omitted_default_arguments_and_page_cap(self):
        db = DirectDatabase([])
        repository = ChannelTrashRepository(db)
        repository.list_removed("id")
        with patch('db.channel.channel_trash.set_channel_trash_state', return_value={}) as mutate:
            repository.remove("channel")
            mutate.assert_called_once_with(db, channel_id="channel", restore=False)
            repository.restore("identity", "clock")
            mutate.assert_called_with(db, user_work_id="identity", expected_hidden_at="clock", restore=True)
            repository.restore_channel("channel", "clock")
            mutate.assert_called_with(db, channel_id="channel", expected_hidden_at="clock", restore=True)
        self.assertEqual(db.calls[0]["max_rows"], 101)
        self.assertEqual(len(db.calls), 1)

    def test_backup_readers_use_snapshot_helper_without_rpc_fallback(self):
        db = DirectDatabase()
        repository = UserWorkBackupRepository(db)
        with patch('db.user_work.user_work_backup.export_snapshot', return_value={'schema_version':1}) as export:
            self.assertEqual(repository.snapshot(), {'schema_version':1})
            export.assert_called_once_with(db)
        failure = DatabaseUnavailable('database_connection_failed')
        with patch('db.user_work.user_work_backup.restore_status', side_effect=failure) as status:
            with self.assertRaises(DatabaseUnavailable) as caught: repository.status(100)
            self.assertIs(caught.exception, failure)
            status.assert_called_once_with(db, 100)
        self.assertEqual(db.calls, [])

    def test_import_and_pending_update_use_single_helper_without_rpc_retry(self):
        db = DirectDatabase()
        repository = UserWorkBackupRepository(db)
        document = {'schema_version':2}
        with patch('db.user_work.user_work_backup.import_user_work_backup',return_value={'outcome':'busy'}) as restore:
            self.assertEqual(repository.import_document(document),{'outcome':'busy'})
            restore.assert_called_once_with(db,document)
        with patch('db.user_work.user_work_backup.update_user_channel_restore_item',return_value={'outcome':'conflict'}) as update:
            self.assertEqual(repository.update_pending('id',7,None,None),{'outcome':'conflict'})
            update.assert_called_once_with(db,identity='id',expected_revision=7,user_input=None,parent_ref=None)
        failure=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        with patch('db.user_work.user_work_backup.import_user_work_backup',side_effect=failure) as restore:
            with self.assertRaises(DatabaseUnavailable) as caught:repository.import_document(document)
            self.assertIs(caught.exception,failure);self.assertEqual(restore.call_count,1)
        self.assertEqual(db.calls,[])

    def test_bind_pending_unknown_ack_is_not_retried_or_sent_to_rpc(self):
        db = DirectDatabase()
        failure = DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        with patch('db.user_work.user_work_backup.bind_user_channel_restore_item',side_effect=failure) as bind:
            with self.assertRaises(DatabaseUnavailable) as caught:
                UserWorkBackupRepository(db).bind_pending('id',7,'channel',{'title':'synthetic'})
            self.assertIs(caught.exception,failure)
            bind.assert_called_once_with(db,identity='id',expected_revision=7,channel_id='channel',channel_data={'title':'synthetic'})
        self.assertEqual(db.calls,[])

    def test_pending_backup_and_review_shapes(self):
        db = DirectDatabase([])
        backup = UserWorkBackupRepository(db)
        self.assertIsNone(backup.pending_item("identity"))
        with patch("db.user_work.user_work_backup.bind_user_channel_restore_item",return_value={"outcome":"ready"}) as bind:
            backup.bind_pending("identity", 1, "channel")
            bind.assert_called_once_with(db,identity="identity",expected_revision=1,channel_id="channel",channel_data=None)
        reviews = VideoTypeReviewsRepository(db)
        with patch("db.reviews.video_type_reviews.queries.has_open_video_type_reviews", return_value=False) as has_open, patch(
            "db.reviews.video_type_reviews.queries.list_video_type_review_cases", return_value=[{"case_id": "a"}],
        ) as list_cases:
            self.assertFalse(reviews.has_open())
            self.assertEqual(reviews.list_cases("open", 51), [{"case_id": "a"}])
            has_open.assert_called_once_with(db)
            list_cases.assert_called_once_with(db, state="open", limit=51, after_case_id=None)
        self.assertEqual(len(db.calls), 1)
        self.assertEqual(db.calls[0]["max_rows"], 1)


class QuotaTests(unittest.TestCase):
    def test_recording_is_project_scoped_and_finish_keeps_no_match_ack(self):
        db = TransactionDatabase()
        repository = YouTubeQuotaRepository(db, project_id="owned")
        payload = {"request_attempt_id": "id", "project_id": "wrong", "status": "started"}
        with patch("db.quota.youtube_quota.start_quota_attempt", return_value=1) as start:
            self.assertTrue(repository.start_attempt(payload))
            start.assert_called_once_with(db.owner, {**payload, "project_id": "owned"})
        self.assertEqual(payload["project_id"], "wrong")
        with patch("db.quota.youtube_quota.finish_quota_attempt", return_value=0) as finish:
            self.assertTrue(repository.finish_attempt(" id ", {"status": "success", "http_status": None}))
            finish.assert_called_once_with(db.owner, "id", {"status": "success", "http_status": None})
            self.assertFalse(repository.finish_attempt("", {"status": "success"}))
        self.assertEqual([op.name for op in db.transactions], ["youtube_quota_start", "youtube_quota_finish"])
        self.assertEqual(db.ends, ["commit", "commit"])

    def test_journal_ack_requires_exact_integer_and_never_replays(self):
        event = {"project_id": "owned", "kind": "start"}
        db = DirectDatabase()
        repository = YouTubeQuotaRepository(db, project_id="owned")
        with patch("db.quota.youtube_quota.persist_quota_events", side_effect=[True, 1]) as persist:
            self.assertFalse(repository.persist_events([event]))
            self.assertTrue(repository.persist_events([event]))
            with self.assertRaises(ValueError):
                repository.persist_events([{**event, "project_id": "other"}])
            self.assertEqual(persist.call_count, 2)
            persist.assert_called_with(db, [event])
        self.assertEqual(db.calls, [])

    def test_attempt_pagination_preserves_offsets_and_truncation_at_exact_cap(self):
        db = DirectDatabase([{"request_attempt_id": "a"}, {"request_attempt_id": "b"}], [{"request_attempt_id": "c"}])
        rows, truncated = YouTubeQuotaRepository(db).list_attempts_for_day("2026-10-05", page_size=2, max_rows=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(truncated)
        self.assertEqual([c["params"][-2:] for c in db.calls], [(2, 0), (1, 2)])
        self.assertEqual([c["max_rows"] for c in db.calls], [2, 1])
        self.assertIn("ORDER BY started_at ASC,request_attempt_id ASC", db.calls[0]["sql"])

    def test_summary_omits_server_clock_unless_explicit(self):
        summary = {"schema_version": 1, "attempts": 0, "quota_units": 0, "truncated": False,
                   "duration_ms": {"count": 0, "min": None, "avg": None, "median": None, "max": None},
                   "unfinished": {"count": 0, "stale_count": 0, "oldest_age_minutes": None}}
        for key in ("bucket_attempt_counts", "bucket_units", "phase_attempt_counts", "phase_units", "endpoint_attempt_counts", "status_counts", "http_status_counts", "error_type_counts"):
            summary[key] = {}
        db = DirectDatabase({"value": summary}, {"value": summary})
        repository = YouTubeQuotaRepository(db)
        repository.summarize_day("2026-10-05")
        repository.summarize_day("2026-10-05", now=NOW)
        self.assertTrue(db.calls[0]["params"]["use_default_now"])
        self.assertIsNone(db.calls[0]["params"]["p_now"])
        self.assertFalse(db.calls[1]["params"]["use_default_now"])
        self.assertEqual(db.calls[1]["params"]["p_now"], NOW.isoformat())


class SyncAndHealthTests(unittest.TestCase):
    def test_sync_write_representation_and_original_clock_are_preserved(self):
        db = TransactionDatabase()
        repository = SyncRunRepository(db)
        supplied = {"status": "running", "summary_json": {"value": None}}
        with patch("db.runs.sync_runs.start_sync_run",return_value=2**54) as start:
            self.assertEqual(repository.start(supplied),2**54)
            start.assert_called_once_with(db.owner,supplied)
        values = {"started_at": "original", "finished_at": "finished"}
        with patch("deployment_profile.selected_deployment_profile", return_value="self-hosted"), patch("db.runs.sync_runs.update_sync_run",return_value=1) as update:
            self.assertTrue(repository.finish(2**54, values))
            self.assertTrue(repository.update_progress(2**54, {"started_at": "never refresh"}))
            update.assert_called_once_with(db.owner,2**54,{"finished_at":"finished"})
        self.assertEqual(values["started_at"], "original")
        self.assertEqual([op.name for op in db.transactions],["sync_run_start","sync_run_finish"])
        self.assertTrue(all(op.total_timeout_ms==60_000 for op in db.transactions))
        self.assertEqual(db.ends,['commit','commit']);self.assertEqual(db.calls,[])

    def test_sync_paging_uses_bound_cutoff_and_inclusive_old_range_size(self):
        db = DirectDatabase([{"id": 3}, {"id": 2}], [{"id": 1}])
        with patch("db.runs.sync_runs.SYNC_RUN_PAGE_SIZE", 2):
            result = SyncRunRepository(db).list_since("2026-01-01", limit=3)
        self.assertEqual([r["id"] for r in result], [3, 2, 1])
        self.assertEqual([c["params"] for c in db.calls], [("2026-01-01", 2, 0), ("2026-01-01", 1, 2)])
        self.assertIn("ORDER BY started_at DESC,id DESC", db.calls[0]["sql"])

    def test_latest_changed_and_stale_recovery_keep_existing_filters_and_null(self):
        db = TransactionDatabase([{"id": 1}])
        repository = SyncRunRepository(db)
        self.assertEqual(repository.latest_completed_with_changes(), {"id": 1})
        self.assertIn("status=ANY(%s::text[]) AND errors=0", db.calls[0]["sql"])
        self.assertEqual(db.calls[0]["params"], (["completed", "partial"], 1, 0))
        with patch("db.runs.sync_runs.recover_stale_sync_runs",return_value=3) as recover:
            self.assertEqual(repository.recover_stale_running(" old ", {}),3)
            recover.assert_called_once_with(db.owner,before="old",finished_at=None)
        self.assertEqual(db.transactions[0].name,"recover_stale_sync_runs")
        self.assertEqual(db.transactions[0].total_timeout_ms,60_000)

    def test_health_dispatch_accepts_only_the_reviewed_query(self):
        db = DirectDatabase([{"channel_id": "a"}, {"channel_id": "b"}], [])
        repository = SystemHealthRepository(db)
        result, truncated = repository.fetch_paginated("channels", CHANNEL_HEALTH_COLUMNS,
            operations=CHANNEL_HEALTH_ORDER, page_size=2, max_rows=3)
        self.assertEqual(len(result), 2)
        self.assertFalse(truncated)
        self.assertEqual([c["params"] for c in db.calls], [(2, 0), (1, 2)])
        self.assertIn("readable.channel_playlist_state", db.calls[0]["sql"])
        self.assertNotIn("public.channels_with_discovery", db.calls[0]["sql"])
        for operations in ((), (("delete", (), {}),), (("order", ("channel_id",), {"desc": True}),)):
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                repository.fetch_paginated("channels", CHANNEL_HEALTH_COLUMNS, operations=operations)
        self.assertEqual(len(db.calls), 2)

    def test_health_clock_and_result_caps_use_ordinary_bounded_select(self):
        db = DirectDatabase([])
        self.assertEqual(SystemHealthRepository(db).fetch_rpc_rows("get_video_collection_health"), [])
        self.assertEqual(db.calls[0]["params"], ())
        self.assertEqual(db.calls[0]["mode"], "read")
        self.assertEqual(db.calls[0]["max_rows"], 1)


if __name__ == "__main__":
    unittest.main()
