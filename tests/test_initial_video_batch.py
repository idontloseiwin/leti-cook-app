"""Initial response transaction boundaries; no database or provider requests."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.videos import initial_video_batch as helper
from db.hot.hot_collection_control import VIDEO_RPCS
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, PostgresConfig, PostgresDatabase
from db.videos.video_collection import VideoCollectionRepository
from test_direct_pg_transactions import Connection, Pool, Result, DSN


def command(operation="complete_metadata", video="aaaaaaaaaaa", kind="long"):
    return {"operation": operation, "rows": [{"video_id": video, "video_type": kind,
        "channel_id": "UC0000000000000000000001", "lease_token": "ac000000-0000-4000-8000-000000000001",
        "collection_attempt_id": "ac000000-0000-4000-8000-000000000002"}]}


class InitialBatchTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.connection = Connection(self.events)
        self.pool = Pool(self.connection, self.events)
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=lambda _: None,
            pool_factory=lambda _: self.pool).open()
        self.addCleanup(self.database.close)
        self.repository = VideoCollectionRepository(self.database)

    def test_all_five_writers_borrow_one_transaction_and_sorted_locks(self):
        commands = [command("complete_metadata", "eeeeeeeeeee"),
            command("complete_typed_metadata", "ddddddddddd", "short"),
            command("complete_stream_lifecycle", "ccccccccccc", "stream"),
            command("complete_missing", "bbbbbbbbbbb", "short"),
            command("fail_batch", "aaaaaaaaaaa", "long")]
        self.connection.results.append(Result(tuple({"video_id": str(i)} for i in range(5))))
        seen = []
        def writer(database, rows, *, transaction):
            self.assertIsNone(database)
            seen.append((transaction, rows[0]["video_id"]))
            with transaction.borrow():
                return len(rows)
        with patch.dict(helper._WRITERS, {key: writer for key in helper._WRITERS}):
            self.assertEqual(self.repository.complete_initial_batch(commands), 5)
        self.assertEqual(len({id(tx) for tx, _ in seen}), 1)
        self.assertEqual([video for _, video in seen], [c["rows"][0]["video_id"] for c in commands])
        self.assertEqual(self.connection.queries[0][1], (["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd", "eeeeeeeeeee"],))
        self.assertEqual((self.pool.borrowed, self.connection.commits), (1, 1))
        self.assertIn("complete_initial_video_batch", VIDEO_RPCS)

    def test_count_mismatch_rolls_back_previous_writes_and_never_replays(self):
        self.connection.results.append(Result((dict(video_id="aaaaaaaaaaa"), dict(video_id="bbbbbbbbbbb"))))
        calls = []
        def writer(database, rows, *, transaction):
            calls.append(rows[0]["video_id"])
            return 1 if len(calls) == 1 else 0
        with patch.dict(helper._WRITERS, {"complete_metadata": writer}):
            with self.assertRaisesRegex(DatabaseServerError, "initial_video_batch_incomplete"):
                self.repository.complete_initial_batch([command(), command(video="bbbbbbbbbbb")])
        self.assertEqual(calls, ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertEqual((self.pool.borrowed, self.connection.commits, self.connection.rollbacks), (1, 0, 1))

    def test_child_error_rolls_back_whole_batch(self):
        self.connection.results.append(Result((dict(video_id="aaaaaaaaaaa"),)))
        def writer(database, rows, *, transaction):
            with transaction.borrow():
                raise DatabaseServerError("channel_collection_scope_lost", code="P0001")
        with patch.dict(helper._WRITERS, {"complete_metadata": writer}):
            with self.assertRaisesRegex(DatabaseServerError, "channel_collection_scope_lost"):
                self.repository.complete_initial_batch([command()])
        self.assertEqual((self.connection.commits, self.connection.rollbacks, self.pool.borrowed), (0, 1, 1))

    def test_unknown_commit_never_replays_even_receipt_only_bundle(self):
        for operation in ("complete_metadata", "fail_batch"):
            self.connection.results.append(Result((dict(video_id="aaaaaaaaaaa"),)))
            self.connection.commit_error = psycopg.OperationalError("private")
            before = self.pool.borrowed
            calls = []
            def writer(database, rows, *, transaction):
                calls.append(operation)
                return len(rows)
            with patch.dict(helper._WRITERS, {operation: writer}):
                with self.assertRaises(DatabaseUnavailable) as caught:
                    self.repository.complete_initial_batch([command(operation)])
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(calls, [operation])
            self.assertEqual(self.pool.borrowed - before, 1)

    def test_all_shape_and_identity_validation_precedes_connection(self):
        cases = [None, {}, [{"operation": "rpc", "rows": []}], [{"operation": [], "rows": []}],
            [{"operation": "complete_metadata", "rows": "bad"}], [command(), command()],
            [command("complete_metadata", kind="short")], [command("complete_typed_metadata", kind="long")],
            [command("complete_stream_lifecycle", kind="invalid")], [command("fail_batch", kind="invalid")],
            [command(video=" spaced ")], [command(video="")]]
        mixed = command("complete_typed_metadata", kind="short")
        mixed["rows"].append(command("complete_typed_metadata", "bbbbbbbbbbb", "stream")["rows"][0])
        cases.append([mixed])
        for field in ("lease_token", "collection_attempt_id", "channel_id"):
            missing = command()
            del missing["rows"][0][field]
            cases.append([missing])
        many = command()
        many["rows"] = [command(video=f"{i:011d}")["rows"][0] for i in range(51)]
        cases.append([many])
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.repository.complete_initial_batch(value)
        self.assertEqual(self.pool.borrowed, 0)
        self.assertEqual(self.repository.complete_initial_batch([]), 0)
        self.assertEqual(self.pool.borrowed, 0)

    def test_fifty_distinct_rows_accept_and_input_is_not_mutated(self):
        commands = [command(video=f"{i:011d}") for i in range(50)]
        before = deepcopy(commands)
        self.connection.results.append(Result(tuple(dict(video_id=f"{i:011d}") for i in range(50))))
        with patch.dict(helper._WRITERS, {"complete_metadata": lambda database, rows, transaction: len(rows)}):
            self.assertEqual(self.repository.complete_initial_batch(commands), 50)
        self.assertEqual(commands, before)


if __name__ == "__main__":
    unittest.main()
