"""Offline quota delivery contracts using the real transaction driver.

Native PostgreSQL casts/constraints and actual concurrency are covered by the
separately opt-in differential probe, not simulated by these recording fakes.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.quota import quota_events as events
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, PostgresConfig, PostgresDatabase
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN


FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"
STAMP = "2026-10-05T12:00:00+00:00"
FINISHED = "2026-10-05T12:00:01+00:00"


def start(identity=FIRST, *, project="primary", retry=None):
    values = {"request_attempt_id": identity, "project_id": project,
              "quota_bucket": "general", "quota_day": "2026-10-05", "phase": "initial_inventory",
              "endpoint": "playlistItems", "quota_units": 1, "status": "started", "started_at": STAMP}
    if retry is not None:
        values["retry_of_request_attempt_id"] = retry
    return {"kind": "start", "request_attempt_id": identity, "project_id": project, "values": values}


def finish(identity=FIRST, *, duration=1000, project="primary"):
    return {"kind": "finish", "request_attempt_id": identity, "project_id": project,
            "values": {"status": "success", "finished_at": FINISHED,
                       "duration_ms": duration, "http_status": 200, "error_type": None}}


def native(event, index=1, *, size=1000, day_matches=True):
    # Script the native rows a PostgreSQL cast returns; this does not validate.
    row = dict.fromkeys(events._IMMUTABLE + events._TERMINAL)
    row.update(event["values"])
    for field in ("request_attempt_id", "retry_of_request_attempt_id"):
        if row[field] is not None:
            row[field] = UUID(row[field])
    for field in ("started_at", "finished_at"):
        if row[field] is not None:
            row[field] = datetime.fromisoformat(row[field].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    row.update(event_index=index, event_kind=event["kind"], event_id=UUID(event["request_attempt_id"]),
               event_project=event["project_id"], payload_bytes=size, day_matches=day_matches)
    return row


def ledger(start_event=None, terminal=None):
    row = native(start_event or start())
    row = {field: row[field] for field in events._IMMUTABLE + events._TERMINAL}
    if terminal:
        row.update({field: native(terminal)[field] for field in events._TERMINAL})
    return row


class EventCopyTests(unittest.TestCase):
    def test_labels_canonicalize_without_mutating_or_dropping_invalid_fields(self):
        event = start()
        event["values"].update(phase="raw / provider payload", endpoint="unlisted", quota_bucket="",
                               extra="retained to reject")
        before = deepcopy(event)
        copied = events.quota_event_copy(event)
        self.assertEqual(event, before)
        self.assertEqual([copied["values"][key] for key in ("phase", "endpoint", "quota_bucket")],
                         ["other"] * 3)
        self.assertEqual(copied["values"]["extra"], "retained to reject")

    def test_wrong_label_types_are_not_sanitized_into_valid_strings(self):
        for value in (None, 12, True, [], {}):
            event = start()
            event["values"]["phase"] = value
            self.assertEqual(events.quota_event_copy(event)["values"]["phase"], value)
        self.assertIsNone(events.quota_event_copy(finish())["values"]["error_type"])
        event = finish()
        event["values"]["error_type"] = "private / error text"
        self.assertEqual(events.quota_event_copy(event)["values"]["error_type"], "OtherError")


class QuotaTransactionTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.connection = Connection(self.trace)
        self.pool = Pool(self.connection, self.trace)
        self.gates = []
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=self.gates.append,
                                         pool_factory=lambda config: self.pool).open()
        self.addCleanup(self.database.close)

    def queue(self, *results):
        self.connection.results.extend(results)

    def initial(self, batch, locked, *, size=1000, day_matches=True):
        self.queue(Result(tuple(native(events.quota_event_copy(event), index, size=size, day_matches=day_matches)
                                for index, event in enumerate(batch, 1))),
                   Result(has_columns=False, rowcount=sum(event["kind"] == "start" for event in batch)),
                   Result(tuple(locked)))

    def fail_code(self, batch, code):
        with self.assertRaises(DatabaseServerError) as caught:
            events.persist_events(self.database, batch)
        self.assertEqual(caught.exception.message, code)
        self.assertEqual(caught.exception.code, "P0001")
        self.assertIsNone(caught.exception.details)

    def test_start_finishes_once_and_acknowledges_only_committed_batch(self):
        batch = [start(), finish()]
        self.initial(batch, [ledger()])
        self.queue(Result(has_columns=False, rowcount=1), Result((ledger(terminal=finish()),)))
        self.assertEqual(events.persist_events(self.database, batch), 2)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 0))
        self.assertEqual((self.pool.borrowed, self.pool.returned), (1, 1))
        self.assertEqual(len(self.connection.queries), 5)
        self.assertTrue(all(gate is None for gate in self.gates))
        setting = next(params for statement, params in self.connection.setup if "statement_timeout" in statement)
        self.assertEqual(setting, ("15000ms", "2000ms"))
        self.assertIn("ledger.status = 'started'", self.connection.queries[3][0])

    def test_public_repository_ack_uses_the_real_transaction_helper(self):
        from db.quota.youtube_quota import YouTubeQuotaRepository
        self.initial([start()], [ledger()])
        self.assertIs(YouTubeQuotaRepository(self.database).persist_events([start()]), True)
        self.assertEqual(self.connection.commits, 1)
        self.assertFalse(any("public\".\"persist_youtube_quota_events" in statement
                             for statement, _ in self.connection.queries))

    def test_child_before_parent_and_reversed_ids_use_one_insert_and_ordered_locks(self):
        batch = [start(SECOND, retry=FIRST), start()]
        self.initial(batch, [ledger(), ledger(batch[0])])
        self.queue(Result(({"request_attempt_id": UUID(FIRST), "project_id": "primary"},)))
        self.assertEqual(events.persist_events(self.database, batch), 2)
        self.assertEqual(len(self.connection.queries), 4)
        insert, lock = self.connection.queries[1:3]
        self.assertIn("ORDER BY proposed.request_attempt_id", insert[0])
        self.assertIn("ON CONFLICT (request_attempt_id) DO NOTHING", insert[0])
        self.assertIn("ORDER BY ledger.request_attempt_id FOR UPDATE OF ledger", lock[0])
        self.assertEqual(lock[1], ([UUID(FIRST), UUID(SECOND)],))
        self.assertIn("SELECT request_attempt_id, project_id", self.connection.queries[3][0])

    def test_finish_before_start_is_supported_and_duplicate_terminal_is_one_update(self):
        batch = [finish(), start(), finish()]
        self.initial(batch, [ledger()])
        self.queue(Result(has_columns=False, rowcount=1), Result((ledger(terminal=finish()),)))
        self.assertEqual(events.persist_events(self.database, batch), 3)
        self.assertEqual(len(json.loads(self.connection.queries[3][1][0])), 1)

    def test_terminal_replay_does_not_lower_row_or_write_terminal_again(self):
        batch = [start(), finish()]
        self.initial(batch, [ledger(terminal=finish())])
        self.assertEqual(events.persist_events(self.database, batch), 2)
        self.assertEqual(len(self.connection.queries), 3)
        self.assertFalse(any(query.lstrip().startswith("UPDATE") for query, _ in self.connection.queries))

    def test_unknown_labels_compare_after_canonicalization_including_old_rows(self):
        event = start()
        event["values"].update(phase="private old label", endpoint="not/a/label")
        old = ledger()
        old.update(phase="different old label", endpoint="another private label")
        self.initial([event], [old])
        self.assertEqual(events.persist_events(self.database, [event]), 1)
        raw, canonical = map(json.loads, self.connection.queries[0][1])
        self.assertEqual(raw[0]["values"]["phase"], "private old label")
        self.assertEqual(canonical[0]["values"]["phase"], "other")

    def test_actual_finish_normalizes_inherited_labels_without_changing_event(self):
        event = finish()
        original = deepcopy(event)
        old = ledger()
        old.update(phase="old_private_label", endpoint="old_endpoint", quota_bucket="old_bucket")
        self.initial([event], [old])
        self.queue(Result(has_columns=False, rowcount=1), Result((ledger(terminal=event),)))
        self.assertEqual(events.persist_events(self.database, [event]), 1)
        self.assertEqual(event, original)
        statement, parameters = self.connection.queries[3]
        values = json.loads(parameters[0])[0]
        for field in ("phase", "endpoint", "quota_bucket"):
            self.assertEqual(values[field], "other")
            self.assertIn(f"{field} = proposed.{field}", statement)
        self.assertEqual(values["error_type"], None)
        self.assertEqual(len(self.connection.queries), 5)

    def test_terminal_replay_does_not_scrub_inherited_noncanonical_labels(self):
        event = finish()
        old = ledger(terminal=event)
        old.update(phase="old_private_label", endpoint="old_endpoint", quota_bucket="old_bucket")
        original = deepcopy(old)
        self.initial([event], [old])
        self.assertEqual(events.persist_events(self.database, [event]), 1)
        self.assertEqual(old, original)
        self.assertEqual(len(self.connection.queries), 3)
        self.assertFalse(any(query.lstrip().startswith("UPDATE") for query, _ in self.connection.queries))

    def test_changed_immutable_replay_rolls_back_inserted_batch(self):
        changed = start()
        changed["values"]["quota_units"] = 2
        self.initial([start(SECOND), changed], [ledger(), ledger(start(SECOND))])
        self.fail_code([start(SECOND), changed], "quota_events_conflict")
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(len(self.connection.queries), 3)

    def test_conflicting_second_finish_rolls_back_whole_batch(self):
        batch = [start(), finish(), finish(duration=999)]
        self.initial(batch, [ledger()])
        self.fail_code(batch, "quota_events_conflict")
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(len(self.connection.queries), 3)

    def test_missing_start_and_project_conflict_remain_distinct(self):
        self.initial([finish()], [])
        self.fail_code([finish()], "quota_events_missing_start")
        self.initial([finish(project="different")], [ledger()])
        self.fail_code([finish(project="different")], "quota_events_conflict")

    def test_retry_parent_must_have_same_project(self):
        child = start(SECOND, retry=FIRST)
        self.initial([child], [ledger(child)])
        self.queue(Result(({"request_attempt_id": UUID(FIRST), "project_id": "different"},)))
        self.fail_code([child], "quota_events_conflict")

    def test_batch_at_limit_has_constant_statement_count_and_bounded_results(self):
        batch = [start(str(UUID(int=index))) for index in range(1, 101)]
        self.initial(batch, [ledger(event) for event in batch])
        self.assertEqual(events.persist_events(self.database, batch), 100)
        self.assertEqual(len(self.connection.queries), 3)
        self.assertEqual(self.connection.fetch_sizes, [101, 101])

    def test_raw_jsonb_byte_limit_is_checked_before_insert(self):
        self.queue(Result((native(start(), size=events.MAX_BATCH_BYTES + 1),)))
        self.fail_code([start()], "quota_events_invalid")
        self.assertEqual(len(self.connection.queries), 1)
        self.assertEqual(self.connection.rollbacks, 1)
        self.assertIn("octet_length", self.connection.queries[0][0])

    def test_byte_boundary_and_large_unknown_label_do_not_inherit_journal_8k_limit(self):
        event = start()
        event["values"]["phase"] = "x" * 9000
        current = ledger()
        current["phase"] = "other"
        self.initial([event], [current], size=events.MAX_BATCH_BYTES)
        self.assertEqual(events.persist_events(self.database, [event]), 1)

    def test_pacific_day_check_occurs_before_writes(self):
        self.queue(Result((native(start(), day_matches=False),)))
        self.fail_code([start()], "quota_events_invalid")
        self.assertEqual(len(self.connection.queries), 1)

    def test_native_cast_errors_and_constraint_errors_have_old_public_code(self):
        for state in ("22007", "22008", "22003", "23514", "23502"):
            with self.subTest(state=state):
                self.queue(Result(execute_error=ServerFailure(state)))
                self.fail_code([start()], "quota_events_invalid")
        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 5)

    def test_foreign_key_error_maps_missing_start_and_never_retries(self):
        self.queue(Result((native(start(SECOND, retry=FIRST)),)),
                   Result(execute_error=ServerFailure("23503")))
        self.fail_code([start(SECOND, retry=FIRST)], "quota_events_missing_start")
        self.assertEqual(len(self.connection.queries), 2)

    def test_known_lock_failure_propagates_without_ack_or_retry(self):
        self.queue(Result(execute_error=ServerFailure("55P03")))
        with self.assertRaises(DatabaseServerError) as caught:
            events.persist_events(self.database, [start()])
        self.assertEqual(caught.exception.code, "55P03")
        self.assertEqual(len(self.connection.queries), 1)
        self.assertEqual(self.connection.commits, 0)

    def test_unknown_commit_propagates_and_identical_input_is_retained(self):
        batch = [start()]
        original = deepcopy(batch)
        self.initial(batch, [ledger()])
        self.connection.commit_error = ServerFailure("08006")
        with self.assertRaises(DatabaseUnavailable) as caught:
            events.persist_events(self.database, batch)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(batch, original)
        self.assertEqual(len(self.connection.queries), 3)
        self.assertEqual(self.pool.returned, 1)

    def test_actual_post_trigger_terminal_values_are_verified(self):
        self.initial([finish()], [ledger()])
        self.queue(Result(has_columns=False, rowcount=1), Result((ledger(terminal=finish(duration=999)),)))
        self.fail_code([finish()], "quota_events_conflict")
        self.assertEqual(self.connection.rollbacks, 1)

    def test_bad_batch_is_fully_rejected_before_dispatch(self):
        variants = []
        for field, value in (("quota_units", True), ("quota_units", "1"), ("quota_units", 1.0),
                             ("phase", 5), ("quota_day", "2026/10/05"),
                             ("retry_of_request_attempt_id", FIRST)):
            event = start()
            event["values"][field] = value
            variants.append([start(SECOND), event])
        extra = start()
        extra["values"]["raw_payload"] = "private"
        variants.extend(([extra], [], [start()] * 101, None, {"events": [start()]}))
        for batch in variants:
            with self.subTest(batch_type=type(batch).__name__):
                self.fail_code(batch, "quota_events_invalid")
        self.assertEqual(self.connection.queries, [])
        self.assertEqual(self.pool.borrowed, 0)


if __name__ == "__main__":
    unittest.main()
