"""Snapshot consistency and bounded export contracts without network access."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.user_work import user_work_read_queries as queries
from db.base.postgres import DatabaseServerError, _reviewed_statement

COUNTS = dict(channels=2, pending=1, objects=1, pending_views=1, folders=1,
              labels=2, assignments=2, pending_labels=2, members=3, reviews=1, history=2)

class Database:
    def __init__(self, *rows):
        self.rows=deque(rows); self.queries=[]; self.operations=[]; self.events=[]
    @contextmanager
    def transaction(self, operation):
        self.operations.append(operation); self.events.append("begin")
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
    def fetch_all(self, statement, parameters=(), *, max_rows):
        self.queries.append((statement, parameters))
        return []
    def fetch_one(self, statement, parameters=()):
        self.queries.append((statement, parameters))
        result=self.rows.popleft()
        if isinstance(result, BaseException): raise result
        return result

class SnapshotTests(unittest.TestCase):
    def test_export_uses_single_repeatable_read_and_acknowledges_after_commit(self):
        expected={"folders":[], "pending":[], "reviews":[]}
        db=Database(COUNTS, {"total_bytes":10}, {"total_bytes":100,"encoded_value":json.dumps(expected)})
        self.assertEqual(queries.export_snapshot(db), expected)
        self.assertEqual(db.events, ["begin","commit"])
        self.assertEqual(len(db.operations),1)
        self.assertTrue(db.operations[0].read_only)
        self.assertTrue(db.operations[0].repeatable_read)
        self.assertEqual(db.operations[0].total_timeout_ms,60000)
        self.assertEqual(len(db.queries),3)
        self.assertEqual(db.queries[-1][1], {"max_bytes":16*1024*1024})

    def test_capacity_boundaries_preserve_user_work_limits(self):
        limits={"channels":9999,"pending":9998,"objects":999,"pending_views":999,
                "folders":1000,"labels":1001,"assignments":99998,"pending_labels":99998,
                "members":100000,"reviews":1000,"history":10000}
        for key, allowed in limits.items():
            # Account for the other half of shared capacity in COUNTS.
            with self.subTest(key=key):
                row=dict(COUNTS,**{key:allowed})
                queries._capacity(row,export=True)
                db=Database(dict(row,**{key:allowed+1}))
                with self.assertRaises(DatabaseServerError) as caught:queries.export_snapshot(db)
                self.assertEqual((caught.exception.code,caught.exception.message),("P0001","user_work_export_limit"))
                self.assertEqual(len(db.queries),1)
                self.assertEqual(db.events[-1],"rollback")

    def test_estimate_rejects_before_aggregating_document(self):
        db=Database(COUNTS,{"total_bytes":16*1024*1024-1023})
        with self.assertRaises(DatabaseServerError):queries.export_snapshot(db)
        self.assertEqual(len(db.queries),2)

    def test_final_byte_guard_rejects_without_transferring_document(self):
        db=Database(COUNTS,{"total_bytes":0},{"total_bytes":16*1024*1024+1,"encoded_value":None})
        with self.assertRaises(DatabaseServerError):queries.export_snapshot(db)
        self.assertIn("case when total_bytes <=",db.queries[-1][0].lower())
        self.assertIn("else null",db.queries[-1][0].lower())

    def test_inclusive_byte_boundary(self):
        db=Database(COUNTS,{"total_bytes":16*1024*1024-1024},{"total_bytes":16*1024*1024,"encoded_value":"{}"})
        self.assertEqual(queries.export_snapshot(db),{})

    def test_status_cast_then_validate_before_accessing_user_rows(self):
        for value in (None,-1,10001):
            with self.subTest(value=value):
                db=Database({"offset":value})
                with self.assertRaisesRegex(DatabaseServerError,"user_work_offset_invalid"):queries.restore_status(db,value)
                self.assertEqual(len(db.queries),1)
        error=DatabaseServerError("database_operation_failed",code="22P02")
        db=Database(error)
        with self.assertRaises(DatabaseServerError) as caught:queries.restore_status(db,"not an integer")
        self.assertIs(caught.exception,error)

    def test_status_pagination_is_bound_and_snapshot_consistent(self):
        expected={"can_restore":False,"pending":[],"pending_more":False}
        db=Database({"offset":10000},COUNTS,{"value":expected})
        result=queries.restore_status(db,10000)
        self.assertEqual({k:v for k,v in result.items() if k!="reconnect"},expected)
        self.assertEqual(db.queries[-2][1],{"offset":10000})
        self.assertTrue(db.operations[0].read_only and db.operations[0].repeatable_read)
        self.assertEqual(result["reconnect"]["excluded_hidden"],0)
        self.assertEqual(sum(result["reconnect"]["counts"].values()),0)
        self.assertIn("LIMIT 100",db.queries[-2][0])
        self.assertIn("LIMIT 101",db.queries[-2][0])

    def test_all_queries_use_driver_read_grammar_and_native_projection(self):
        for statement in (queries._EXPORT_CAPACITY,queries._EXPORT_ESTIMATE,
                          queries._EXPORT_DOCUMENT,queries._STATUS_CAPACITY,queries._STATUS_DOCUMENT):
            _reviewed_statement(statement,read=True,max_rows=1)
            self.assertNotIn("export_user_work_backup(",statement)
            self.assertNotIn("get_user_work_restore_status(",statement)
        self.assertIn("hidden_at",queries._EXPORT_DOCUMENT)
        self.assertIn("restore_owner_ref",queries._EXPORT_DOCUMENT)
        self.assertIn("deleted_at IS NOT NULL",queries._STATUS_DOCUMENT)

    def test_invalid_shape_never_acknowledges_success(self):
        for result in (None,[],42):
            db=Database(COUNTS,{"total_bytes":0},{"total_bytes":1,"encoded_value":json.dumps(result)})
            with self.assertRaisesRegex(RuntimeError,"user_work_response_invalid"):queries.export_snapshot(db)
            self.assertEqual(db.events[-1],"rollback")

class RepositoryReadTests(unittest.TestCase):
    def test_real_repository_snapshot_and_status_use_transactions_without_rpc(self):
        from db.user_work.user_work_backup import UserWorkBackupRepository
        expected={"folders":[],"labels":[],"channels":[],"assignments":[],"saved_objects":[],"members":[],"pending":[],"reviews":[]}
        status={"can_restore":False,"pending":[],"pending_more":False}
        db=Database(COUNTS,{"total_bytes":0},{"total_bytes":2,"encoded_value":json.dumps(expected)},
                    {"offset":1},COUNTS,{"value":status})
        repository=UserWorkBackupRepository(db)
        self.assertEqual(repository.snapshot(),expected)
        result=repository.status(1)
        self.assertEqual({k:v for k,v in result.items() if k!="reconnect"},status)
        self.assertEqual(result["reconnect"]["excluded_hidden"],0)
        self.assertEqual(len(db.operations),2)
        self.assertTrue(all(operation.read_only and operation.repeatable_read for operation in db.operations))
        self.assertEqual(db.queries[-2][1],{"offset":1})

if __name__ == "__main__": unittest.main()
