"""Offline snapshot, error-order and borrowed proof tests for F8 shared scope."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_scope as scope
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database
AT='2026-10-06T12:00:00+00:00'

def header(**changes):
    result=dict(job='{}',version=1,expired=False,requested_at=AT,require_frozen=False,sources=[],unresolved=False)
    result.update(changes);return result
def entry(**changes):
    result=dict(key='long',value='{}',terminal=None,scan_error=None,receipt_error=None,scan='{}',receipt='{}')
    result.update(changes);return result
def scan(**changes):
    result=dict(value='{}',invalid=False,started_at=AT,boundary=AT);result.update(changes);return result


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(scope,'_hot');self.hot=self.patch.start();self.addCleanup(self.patch.stop)
    def test_legacy_scope_returns_after_native_composite_boolean_cast(self):
        for version in (0,2,-1):
            tx=Database(header(version=version,expired=True));self.assertIsNone(scope.assert_full_initial_scope(tx,{},True))
            self.assertEqual(len(tx.calls),1);self.assertEqual(tx.events,['borrow']);self.hot.assert_not_called()
    def test_expired_marker_precedes_source_clock_and_scope_shape(self):
        tx=Database(header(expired=True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_source_scope_expired'):scope.assert_full_initial_scope(tx,{})
        self.assertEqual(len(tx.calls),1);self.assertEqual(tx.events,['borrow','poison']);self.hot.assert_not_called()
    def test_requested_hot_clock_precedes_shape_and_deferred_scan_cast(self):
        tx=Database(header(sources=[entry(scan_error='22P02')]))
        self.hot.side_effect=DatabaseServerError('hot_source_clock_out_of_window',code='22023')
        with self.assertRaises(DatabaseError) as error:scope.assert_full_initial_scope(tx,{})
        self.assertEqual(error.exception.code,'22023');self.assertEqual(len(tx.calls),1)
    def test_scope_shape_precedes_deferred_uuid_and_native_lookup(self):
        tx=Database(header(sources=[entry(scan_error='22P02')]),dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_scope_invalid'):scope.assert_full_initial_scope(tx,{})
        self.assertEqual(len(tx.calls),2)
    def test_scan_uuid_error_precedes_missing_identity_and_native_timestamp_casts(self):
        for code in ('22P02',None):
            tx=Database(header(sources=[entry(scan_error=code,scan=None)]),dict(invalid=False))
            with self.assertRaises(DatabaseError) as caught:scope.assert_full_initial_scope(tx,{})
            self.assertEqual(caught.exception.code,code or 'P0001');self.assertEqual(len(tx.calls),2)
    def test_identity_rejects_before_timestamp_projection_and_hot_scan_clocks(self):
        tx=Database(header(sources=[entry()]),dict(invalid=False),scan(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_source_identity_invalid'):scope.assert_full_initial_scope(tx,{})
        self.hot.assert_called_once_with(tx,AT);self.assertEqual(len(tx.calls),3)
    def test_unfinished_source_checks_order_then_optional_frozen_error(self):
        for frozen in (False,True,None):
            tx=Database(header(require_frozen=frozen,sources=[entry()]),dict(invalid=False),scan(),dict(invalid=False))
            if frozen:
                with self.assertRaisesRegex(DatabaseError,'full_initial_targets_not_frozen'):scope.assert_full_initial_scope(tx,{},frozen)
            else:self.assertIsNone(scope.assert_full_initial_scope(tx,{},frozen))
            self.assertEqual(self.hot.call_args_list[-3:],[__import__('unittest.mock',fromlist=['call']).call(tx,AT)]*3)
    def test_terminal_clock_checked_before_boundary_and_coverage(self):
        tx=Database(header(sources=[entry(terminal='completed')]),dict(invalid=False),scan(),dict(invalid=False),dict(at=AT),dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_source_clock_invalid'):scope.assert_full_initial_scope(tx,{})
        self.assertEqual(self.hot.call_count,4);self.assertEqual(tx.calls[-1][2],(AT,AT))
    def test_counter_coverage_invalid_precedes_deferred_receipt_uuid_error(self):
        tx=Database(header(sources=[entry(terminal='completed',receipt_error='22P02')]),dict(invalid=False),scan(),dict(invalid=False),dict(at=AT),dict(invalid=False),dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_source_coverage_invalid'):scope.assert_full_initial_scope(tx,{})
        self.assertEqual(len(tx.calls),7)
    def test_native_counter_overflow_propagates_and_poisons_borrowed_owner(self):
        native=DatabaseServerError('database_operation_failed',code='22003')
        tx=Database(header(sources=[entry(terminal='completed')]),dict(invalid=False),scan(),dict(invalid=False),dict(at=AT),dict(invalid=False),native)
        with self.assertRaises(DatabaseError) as caught:scope.assert_full_initial_scope(tx,{})
        self.assertIs(caught.exception,native);self.assertEqual(tx.events,['borrow','poison'])
    def test_receipt_error_is_deferred_until_its_native_validation_position(self):
        tx=Database(header(sources=[entry(terminal='completed',receipt_error='22P02')]),dict(invalid=False),scan(),dict(invalid=False),dict(at=AT),dict(invalid=False),dict(invalid=False))
        with self.assertRaises(DatabaseError) as error:scope.assert_full_initial_scope(tx,{})
        self.assertEqual(error.exception.code,'22P02');self.assertEqual(len(tx.calls),7)
    def test_full_success_keeps_jsonb_entry_order_and_snapshot_target_flag(self):
        entries=[entry(key=kind,terminal='completed') for kind in ('long','short','stream')]
        answers=[header(require_frozen=True,sources=entries,unresolved=True),dict(invalid=False)]
        for item in entries:answers.extend([scan(),dict(invalid=False),dict(at=AT),dict(invalid=False),dict(invalid=False),dict(valid=True)])
        answers.append(dict(invalid=True));tx=Database(*answers)
        with self.assertRaisesRegex(DatabaseError,'full_initial_targets_not_frozen'):scope.assert_full_initial_scope(tx,{},True)
        self.assertEqual([p['kind'] for kind,sql,p in tx.calls if sql==scope._SCAN],['long','short','stream'])
        self.assertEqual(tx.calls[-1][2],(3,True,'{}'));self.assertEqual(self.hot.call_count,10)
        self.assertTrue(all('FROM public.' not in sql for kind,sql,p in tx.calls[1:]))
    def test_empty_sources_allowed_before_freeze(self):
        tx=Database(header(),dict(invalid=False));self.assertIsNone(scope.assert_full_initial_scope(tx,{}))
        self.assertEqual(len(tx.calls),2)
    def test_target_type_is_native_exact_identity_with_no_time_filter(self):
        for value in (None,{'video_type':'short'}):
            tx=Database(value);self.assertEqual(scope.full_initial_target_type(tx,' channel ','video'),None if value is None else 'short')
            self.assertEqual(tx.calls[0][2],dict(channel_id=' channel ',video_id='video'))
        self.assertNotIn('clock_timestamp',scope._TYPE);self.assertIn("r.blocked_reason IS NULL",scope._TYPE)
    def test_all_source_tables_captured_once_without_locks_or_stored_functions(self):
        p=dict(job='{}',entry='{}',scan='{}',receipt='{}',kind='long',channel_id='channel',video_id='video')
        statements=[(scope._INPUT,(False,Jsonb({}))),(scope._SHAPE,('{}',)),(scope._SCAN,p),(scope._IDENTITY,p),
            (scope._SOURCE,('{}',)),(scope._COVERAGE,p),(scope._RECEIPT,p),(scope._FROZEN,(3,False,'{}')),(scope._TYPE,p)]
        for sql,params in statements:_reviewed_statement(sql,read=True,max_rows=1);PostgresQuery(Transformer()).convert(sql,params)
        self.assertIn('ORDER BY e.ordinality',scope._INPUT);self.assertIn('pg_input_error_info',scope._INPUT)
        for sql in (scope._SCAN,scope._IDENTITY,scope._COVERAGE,scope._RECEIPT,scope._FROZEN):self.assertNotIn('FROM public.',sql)
        source=(ROOT/'application/db/jobs/operator_job_scope.py').read_text();self.assertNotIn('database.call(',source);self.assertNotIn('FOR UPDATE',source)


if __name__=='__main__':unittest.main()
