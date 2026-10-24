"""Offline selection, report sanitization and cleanup guards; never connects."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lic_direct_pg_repositories", ROOT / "tools/check_direct_pg_repositories.py")
probe_tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe_tool
spec.loader.exec_module(probe_tool)


class SelectionTests(unittest.TestCase):
    def test_list_does_not_connect_or_import_application(self):
        with patch.object(probe_tool.safety, "connect") as connect, patch.object(probe_tool, "open_database") as open_db:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(probe_tool.main(["--list-cases"]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual({row["case_id"] for row in result["cases"]}, {"user_work", "quota_sync", "reads", "timeline", "stale_sync", "transaction_steps"})
            self.assertEqual(set(probe_tool.CASES), set(probe_tool.RUNNERS))
            connect.assert_not_called()
            open_db.assert_not_called()

    def test_explicit_distinct_selection_required(self):
        for args in ([], ["--case", "reads", "--case", "reads"], ["--case", "all"]):
            with self.subTest(args=args), patch.object(probe_tool.safety, "connect") as connect:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    probe_tool.main(args)
                self.assertEqual(error.exception.code, 2)
                connect.assert_not_called()

    def test_existing_report_refused_before_connection(self):
        with patch.object(probe_tool.safety, "connect") as connect:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe_tool.main(["--case", "reads", "--report", str(Path(__file__))])
            connect.assert_not_called()

    def test_reuses_identical_safety_module(self):
        self.assertEqual(probe_tool.safety.MAX_ROWS, 1000)
        self.assertEqual(probe_tool.safety.validate_configuration.__module__, "check_direct_pg_baseline")
        self.assertEqual(probe_tool.safety.LOCK_ID, 962106004)

    def test_runtime_is_the_shipped_driver_and_seeds_use_current_owners(self):
        source = (ROOT / "tools/check_direct_pg_repositories.py").read_text(encoding="utf-8")
        self.assertIn("from db.base.postgres import PostgresConfig, PostgresDatabase", source)
        self.assertIn("from db.channel.channel_control import create_channel_for_collection", source)
        for retired in ("legacy_pg_transport", "LegacyOracleDatabase", "public.create_channel_for_collection",
                        "recover_stale_sync_runs", "database.call"):
            self.assertNotIn(retired, source)


class SanitizerTests(unittest.TestCase):
    def test_driver_sqlstate_is_retained_but_payload_and_class_name_are_not(self):
        private_type = type("PrivatePayloadInType", (RuntimeError,), {})
        error = private_type("postgresql://private:password@example.test SQL secret")
        error.code = "22023"
        error.message = "untrusted value"
        error.diag = SimpleNamespace(message_primary="private user payload")
        self.assertEqual(probe_tool.safe_failure(error), {"failure_type": "operation", "sqlstate": "22023"})

    def test_raw_sqlstate_and_only_owned_tokens(self):
        error = probe_tool.ProbeFailure("repository_probe_expected_count")
        error.sqlstate = "P0001"
        self.assertEqual(probe_tool.safe_failure(error), {"failure_type": "probe", "failure_code": "repository_probe_expected_count", "sqlstate": "P0001"})
        for token in ("raw private payload", "repository_probe_dsn://password", "repository_probe_" + "a" * 91):
            self.assertEqual(probe_tool.safe_failure(probe_tool.ProbeFailure(token)), {"failure_type": "probe"})

    def test_preflight_codes_and_fixed_address_kind_survive(self):
        error = probe_tool.safety.BaselineFailure("baseline_actual_server_not_loopback")
        error.server_address_kind = "ipv4_not_allowed"
        self.assertEqual(probe_tool.safe_failure(error), {"failure_type": "preflight", "failure_code": "baseline_actual_server_not_loopback", "server_address_kind": "ipv4_not_allowed"})

    def test_fixed_stages_and_exception_categories_only(self):
        self.assertEqual(probe_tool.safe_failure(KeyError("private key"), stage="backup_build"),
                         {"failure_type": "operation", "failure_category": "key_error", "failure_stage": "backup_build"})
        self.assertNotIn("failure_stage", probe_tool.safe_failure(RuntimeError("private"), stage="private stage"))
        error = ValueError("private")
        error.code = "invalid_user_work_backup"
        self.assertEqual(probe_tool.safe_failure(error)["failure_code"], "invalid_user_work_backup")
        error.code = "private_payload"
        self.assertNotIn("failure_code", probe_tool.safe_failure(error))


class BackupFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_path = sys.path[:]
        sys.path.insert(0, str(ROOT / "application"))

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.original_path

    def test_saved_condition_fixture_survives_real_backup_projection(self):
        from saved_work import normalize_conditions, SavedWorkError
        from user_work_backup import build_backup, import_payload
        with self.assertRaises(SavedWorkError):
            normalize_conditions({"version": 1, "filters": {}, "facets": {}})
        conditions = probe_tool.saved_conditions()
        self.assertEqual(normalize_conditions(conditions), conditions)
        stamp = "2026-10-05T00:00:00+00:00"
        identity = "a0000000-0000-4000-8000-000000000002"
        snapshot = {
            "folders": [{"id": probe_tool.FOLDER, "name": probe_tool.TITLE, "created_at": stamp}],
            "labels": [{"label_key": probe_tool.LABEL, "name": probe_tool.TITLE,
                        "label_type": "custom", "created_at": stamp}],
            "channels": [{"channel_id": probe_tool.CHANNEL, "user_work_id": identity,
                          "user_input": "https://www.youtube.com/channel/" + probe_tool.CHANNEL,
                          "folder_id": probe_tool.FOLDER, "parent_channel_id": None,
                          "is_hidden": False, "hidden_at": None, "added_at": stamp}],
            "assignments": [{"channel_id": probe_tool.CHANNEL, "label_key": probe_tool.LABEL}],
            "saved_objects": [{"id": probe_tool.VIEW, "kind": "view", "name": probe_tool.TITLE,
                               "channel_id": probe_tool.CHANNEL, "conditions": conditions,
                               "revision": 1, "created_at": stamp, "updated_at": stamp}],
            "members": [], "pending": [], "reviews": [],
        }
        document = import_payload(build_backup(snapshot, now=stamp)["backup"], normalized=True)
        self.assertEqual(document["channels"][0]["id"], identity)
        self.assertEqual(document["channels"][0]["saved_views"][0]["conditions"], conditions)
        self.assertIn("create_fingerprint", document["channels"][0]["saved_views"][0])
        self.assertEqual(document["channels"][0]["label_keys"], [probe_tool.LABEL])


class ProbeTests(unittest.TestCase):
    def make_probe(self):
        admin = MagicMock()
        admin.scalar.return_value = datetime(2026, 10, 5, tzinfo=timezone.utc)
        return probe_tool.Probe(admin, object(), {}, {"public.channels": 0})

    def test_failed_check_does_not_publish_values_or_mark_passed(self):
        probe = self.make_probe()
        probe.check("one_check", True)
        with self.assertRaisesRegex(probe_tool.ProbeFailure, "repository_probe_failed_check"):
            probe.check("failed_check", False)
        with self.assertRaisesRegex(probe_tool.ProbeFailure, "repository_probe_duplicate_check_name"):
            probe.check("one_check", True)
        self.assertEqual(probe.report()["checks"], [{"check_id": "one_check", "status": "passed"}])

    def test_method_calls_count_failures_without_retaining_arguments(self):
        probe = self.make_probe()
        def failure(secret):
            raise RuntimeError(secret)
        with self.assertRaises(RuntimeError):
            probe.call(failure, "private secret")
        self.assertEqual(probe.report()["repository_method_calls"], 1)
        self.assertNotIn("private", json.dumps(probe.report()))

    def test_row_budget_failure_does_not_prevent_fixed_cleanup(self):
        probe = self.make_probe()
        with patch.object(probe_tool.safety, "bounded_counts", return_value={"public.channels": 1001}), patch.object(probe_tool.safety, "reset_fixture") as cleanup:
            with self.assertRaisesRegex(probe_tool.ProbeFailure, "repository_probe_synthetic_row_budget"):
                probe.observe()
            probe.reset()
            cleanup.assert_called_once_with(probe.admin, probe.snapshots, probe.before_counts)
            self.assertEqual(probe.report()["completed_resets"], 1)
        with patch.object(probe_tool.safety, "bounded_counts", return_value={"public.channels": 2}):
            probe.observe()
            self.assertEqual(probe.report()["peak_observed_total_rows"], 2)

    def test_profile_restored_after_failure(self):
        for original in ({}, {"LIC_DEPLOYMENT_PROFILE": "previous-local-test"}):
            with patch.dict(probe_tool.os.environ, original, clear=True):
                with self.assertRaises(RuntimeError):
                    with probe_tool.self_hosted_profile():
                        self.assertEqual(probe_tool.os.environ["LIC_DEPLOYMENT_PROFILE"], "self-hosted")
                        raise RuntimeError("expected")
                self.assertEqual(dict(probe_tool.os.environ), original)


class StaleRecoveryProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_path = sys.path[:]
        sys.path.insert(0, str(ROOT / "application"))

    @classmethod
    def tearDownClass(cls):
        sys.path[:] = cls.original_path

    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    transaction_now = datetime(2026, 10, 7, 12, 0, 2, tzinfo=timezone.utc)

    def expected(self, finish):
        # Independently specified stored results, not obtained from the probe or codec.
        return [
            dict(status='completed', started_at=self.now-timedelta(hours=4),
                 finished_at=self.now-timedelta(minutes=1), errors=0, summary_json={'errors': 0}),
            dict(status='error', started_at=self.now-timedelta(hours=3), finished_at=finish, errors=1,
                 summary_json={'summary_schema_version': 3, 'column_fields': 9,
                               'reported_values': {'errors': 0}, 'stale_run_recovered': True}),
            dict(status='error', started_at=self.now-timedelta(hours=2),
                 finished_at=self.now-timedelta(minutes=2), errors=2,
                 summary_json={'summary_schema_version': 3, 'column_fields': 5,
                               'routing': {'enabled': False}, 'stale_run_recovered': True}),
            dict(status='error', started_at=self.now-timedelta(hours=1, seconds=1),
                 finished_at=finish, errors=1,
                 summary_json={'summary_schema_version': 3, 'column_fields': 5,
                               'reported_values': {'errors': 0}, 'stale_run_recovered': True}),
            dict(status='running', started_at=self.now-timedelta(hours=1),
                 finished_at=None, errors=0, summary_json={}),
            dict(status='running', started_at=self.now-timedelta(hours=1)+timedelta(seconds=1),
                 finished_at=None, errors=0, summary_json={}),
        ]

    def run_stale_case(self, mutate=None):
        from db.base.postgres import DatabaseError
        first = self.expected(self.now-timedelta(minutes=1))
        snapshots = [deepcopy(first), deepcopy(first), self.expected(self.transaction_now),
                     [{'before_conflict': True}], [{'before_conflict': True}]]
        if mutate is not None:
            mutate(snapshots)
        admin = MagicMock()
        admin.scalar.return_value = self.now
        def execute(statement, parameters=()):
            if statement.startswith('SELECT status,started_at,finished_at,errors,summary_json'):
                return snapshots.pop(0)
            return []
        admin.execute.side_effect = execute
        operations, owners = [], []
        @contextlib.contextmanager
        def transaction(operation):
            operations.append(operation)
            tx = MagicMock()
            tx.fetch_one.return_value = {'value': self.transaction_now}
            owners.append(tx)
            yield tx
        database = SimpleNamespace(transaction=transaction)
        probe = probe_tool.Probe(admin, database, {}, {})
        probe.observe = MagicMock()
        probe.reset = MagicMock()
        with patch('db.runs.sync_runs.recover_stale_sync_runs',
                   side_effect=[3, 0, 3, DatabaseError('database_operation_failed', code='P0001')]) as recover:
            probe_tool.stale_sync(probe)
            calls = recover.call_args_list
        self.assertEqual(snapshots, [])
        return probe, admin, operations, owners, calls

    def test_full_case_uses_repository_transactions_and_exact_rows(self):
        probe, _, operations, owners, calls = self.run_stale_case()
        self.assertEqual([op.name for op in operations], ['recover_stale_sync_runs'] * 4)
        self.assertEqual([op.total_timeout_ms for op in operations], [60000] * 4)
        self.assertEqual([call.args[0] for call in calls], owners)
        self.assertEqual([owner.fetch_one.call_count for owner in owners], [0, 0, 1, 0])
        owners[2].fetch_one.assert_called_once_with('SELECT now() AS value')
        self.assertIsNone(calls[2].kwargs['finished_at'])
        self.assertIn('stale_recovery_exact_rows', probe.checks)
        self.assertIn('stale_recovery_null_clock', probe.checks)
        self.assertIn('stale_report_conflict_atomic', probe.checks)

    def test_expected_stored_summaries_match_current_codec_projection(self):
        from db.runs.sync_run_summary import normalize_sync_run_summary, persisted_summary_copy
        expected = self.expected(self.now-timedelta(minutes=1))
        inputs = (
            (1, {'errors': 0, 'channel_metadata': {'processed': 9}}),
            (2, {'errors': 2, 'finished_at': (self.now-timedelta(minutes=2)).isoformat(),
                 'routing': {'enabled': False}}),
            (3, {'errors': 0, 'finished_at': None}),
        )
        for index, summary in inputs:
            row = expected[index]
            columns = {'errors': row['errors'], 'finished_at': row['finished_at'].isoformat(),
                       'metadata_processed': 0}
            proposed = {**summary, 'stale_run_recovered': True, 'reason': 'process_exit_without_finish'}
            # These synthetic strings use one canonical ISO representation; only
            # NULL versus a timestamp needs the controlled unequal comparison.
            normalized = normalize_sync_run_summary(
                proposed, columns, compare_timestamps=lambda pairs: [a == b for a, b in pairs])
            self.assertEqual(persisted_summary_copy(normalized), row['summary_json'])

    def test_full_result_rejects_timestamp_errors_summary_and_completed_corruption(self):
        mutations = (
            lambda rows: rows[2].update(finished_at=self.now),
            lambda rows: rows[1].update(errors=2),
            lambda rows: rows[1]['summary_json'].update(column_fields=1),
            lambda rows: rows[2]['summary_json']['routing'].update(enabled=True),
            lambda rows: rows[0].update(status='error'),
            lambda rows: rows[4].update(started_at=self.now),
            lambda rows: rows.pop(),
        )
        for corrupt in mutations:
            with self.subTest(corrupt=corrupt), self.assertRaisesRegex(
                    probe_tool.ProbeFailure, '^repository_probe_stale_recovery_exact_rows$'):
                self.run_stale_case(lambda snapshots: corrupt(snapshots[0]))

    def test_replay_cannot_change_a_completed_row(self):
        with self.assertRaisesRegex(probe_tool.ProbeFailure, '^repository_probe_stale_recovery_replay_no_changes$'):
            self.run_stale_case(lambda snapshots: snapshots[1][0].update(errors=1))

    def test_null_finish_preserves_existing_finish_and_summary(self):
        for mutate in (
            lambda rows: rows[2].update(finished_at=self.transaction_now),
            lambda rows: rows[1]['summary_json'].update(stale_run_recovered=False),
        ):
            with self.subTest(mutate=mutate), self.assertRaisesRegex(
                    probe_tool.ProbeFailure, '^repository_probe_stale_recovery_null_clock$'):
                self.run_stale_case(lambda snapshots: mutate(snapshots[2]))

    def test_conflict_seed_violates_current_reported_value_invariant_without_trigger(self):
        from db.base.postgres import DatabaseError
        from db.runs.sync_run_summary import normalize_sync_run_summary
        _, admin, _, _, _ = self.run_stale_case()
        updates = [call for call in admin.execute.call_args_list
                   if call.args[0].startswith('UPDATE public.sync_runs SET summary_json=')]
        self.assertEqual(len(updates), 1)
        document = updates[0].args[1][0].obj
        self.assertEqual(document, {'summary_schema_version': 3, 'column_fields': 1,
                                    'reported_values': {'errors': 7}})
        # Recovery proposes original errors=0 against the new column value=1.
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary({**document, 'errors': 0}, {'errors': 1})
        self.assertEqual(caught.exception.code, 'P0001')
        # A raw unnormalized errors=7 document alone is not a conflict anymore.
        self.assertEqual(normalize_sync_run_summary({'errors': 7}, {'errors': 1})['reported_values'],
                         {'errors': 7})

    def test_clock_adapter_preserves_real_recovery_cursor_and_owner_budget(self):
        from db.base.postgres import PostgresConfig, PostgresDatabase
        from db.runs.sync_runs import SyncRunRepository
        from test_direct_pg_transactions import Connection, Pool, Result, DSN
        from test_video_scheduled_cursor import Cursor
        events = []
        connection = Connection(events)
        connection.results.append(Result(({'value': self.transaction_now},)))
        ordinary_cursor = connection.cursor
        stream = Cursor([])
        cursor_options = []
        def cursor(**options):
            if options:
                cursor_options.append(options)
                return stream
            return ordinary_cursor()
        connection.cursor = cursor
        pool = Pool(connection, events)
        with PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
                              pool_factory=lambda config: pool) as database:
            with patch.object(database, 'transaction', wraps=database.transaction) as opened:
                clocked = probe_tool._RecoveryClockDatabase(database)
                self.assertEqual(SyncRunRepository(clocked).recover_stale_running('2026-10-07', {}), 0)
                opened.assert_called_once()
                operation = opened.call_args.args[0]
                self.assertEqual(operation.name, 'recover_stale_sync_runs')
                self.assertEqual(operation.total_timeout_ms, 60000)
                self.assertFalse(operation.read_only)
        self.assertEqual(clocked.now, self.transaction_now)
        self.assertEqual((pool.borrowed, pool.returned, connection.commits, connection.rollbacks), (1, 1, 1, 0))
        self.assertEqual(cursor_options[0]['name'], 'lic_sync_run_recovery')
        self.assertFalse(cursor_options[0]['withhold'])
        self.assertEqual(len(stream.calls), 1)
        self.assertEqual(stream.sizes, [1])
        self.assertTrue(stream.closed)


class MainLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.admin = MagicMock()
        self.admin.scalar.return_value = datetime(2026, 10, 5, tzinfo=timezone.utc)
        self.database = MagicMock()
        self.database.__enter__.return_value = self.database
        self.database.__exit__.return_value = False
        self.database.read_row.return_value = {"login": "authenticator", "role": "service_role"}
        self.patches = [
            patch.object(probe_tool.safety, "validate_configuration", return_value=("ADMIN", "RUNTIME")),
            patch.object(probe_tool.safety, "connect", return_value=self.admin),
            patch.object(probe_tool.safety, "preflight", return_value=({}, {"public.channels": 0})),
            patch.object(probe_tool.safety, "bounded_counts", return_value={"public.channels": 0}),
            patch.object(probe_tool.safety, "reset_fixture"),
            patch.object(probe_tool, "open_database", return_value=self.database),
        ]
        self.mocks = [p.start() for p in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def run_main(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = probe_tool.main(["--case", "reads"])
        return status, json.loads(output.getvalue())

    def test_selected_case_runs_and_cleans_once(self):
        def runner(probe):
            probe.check("fixed_result", True)
        with patch.dict(probe_tool.RUNNERS, {"reads": runner}):
            status, result = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(result["passed_checks"], 1)
        self.assertEqual([r["case_id"] for r in result["cases"]], ["reads"])
        self.assertFalse(result["fixture_cleanup_incomplete"])
        self.mocks[4].assert_called_once()
        self.admin.connection.close.assert_called_once()

    def test_operation_failure_still_cleans_and_sanitizes(self):
        def runner(probe):
            probe.check("before_failure", True)
            raise RuntimeError("private runtime payload")
        with patch.dict(probe_tool.RUNNERS, {"reads": runner}):
            status, result = self.run_main()
        self.assertEqual(status, 1)
        self.assertEqual(result["cases"][0]["status"], "failed")
        self.assertEqual(result["passed_checks"], 1)
        self.assertFalse(result["fixture_cleanup_incomplete"])
        self.assertNotIn("private", json.dumps(result))
        self.mocks[4].assert_called_once()

    def test_cleanup_failure_never_reports_success(self):
        self.mocks[4].side_effect = RuntimeError("private cleanup payload")
        with patch.dict(probe_tool.RUNNERS, {"reads": lambda p: p.check("fixed_result", True)}):
            status, result = self.run_main()
        self.assertEqual(status, 1)
        self.assertTrue(result["fixture_cleanup_incomplete"])
        self.assertEqual(result["cases"][0]["status"], "failed")
        self.assertEqual(result["cases"][0]["cleanup_failure"], {"failure_type": "operation"})
        self.assertNotIn("private", json.dumps(result))

    def test_preflight_failure_never_opens_runtime_or_cleans_existing_data(self):
        self.mocks[2].side_effect = probe_tool.safety.BaselineFailure("baseline_nonempty_domain")
        status, result = self.run_main()
        self.assertEqual(status, 1)
        self.assertEqual(result["failure_code"], "baseline_nonempty_domain")
        self.assertEqual(result["cases"], [])
        self.mocks[4].assert_not_called()
        self.mocks[5].assert_not_called()

    def test_identity_mismatch_never_starts_a_case(self):
        self.database.read_row.return_value = {"login": "postgres", "role": "postgres"}
        status, result = self.run_main()
        self.assertEqual(status, 1)
        self.assertEqual(result["failure_code"], "repository_probe_runtime_identity")
        self.assertEqual(result["cases"], [])
        self.mocks[4].assert_not_called()


if __name__ == "__main__":
    unittest.main()
