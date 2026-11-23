"""Offline owner/order guards; native cast/lock parity remains a separate gate."""
import inspect
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.runs import sync_run_write_normalization as writer
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, _reviewed_statement
from test_operator_job_lifecycle import Database as BaseDatabase


class Database(BaseDatabase):
    def validate_operational_assignments(self,family,values,*,identity=None):
        self.assignment_validation=(family,dict(values),identity)

    def fetch_all(self, statement, parameters=(), *, max_rows):
        self.limit = max_rows
        return self.answer('all', statement, parameters)


def row(summary='{}', **columns):
    return {'summary_text':summary, 'columns_text':json.dumps({'id':1, **columns})}


def rendered(statement):
    return statement.as_string() if hasattr(statement, 'as_string') else statement


class AddressedUpdateTests(unittest.TestCase):
    def test_full_native_composite_order_matches_schema23(self):
        source = (ROOT/'schema/schema.sql').read_text()
        body = re.search(r'CREATE TABLE public.sync_runs \((.*?)\n\);', source, re.S).group(1)
        columns = re.findall(r'^    (\w+) ', body, re.M)
        self.assertEqual(columns, [name for name, _ in writer.COLUMN_TYPES])
        self.assertEqual(len(columns), 20)

    def test_empty_assignments_do_not_open_or_query_transaction(self):
        tx = Database()
        self.assertEqual(writer.update_sync_run(tx, 1, {}), 0)
        self.assertEqual((tx.calls, tx.events, tx.operations), ([], [], []))

    def test_assignment_envelope_rejects_257_before_identifier_or_sql(self):
        tx = Database()
        values = {('invalid name' if i == 0 else 'field_' + str(i)): i for i in range(257)}
        with self.assertRaisesRegex(ValueError, '^database_assignments_invalid$'):
            writer.update_sync_run(tx, 1, values)
        self.assertEqual((tx.calls, tx.events, tx.operations), ([], [], []))

    def test_counter_only_normalizes_against_complete_proposed_native_row(self):
        tx = Database(row('{"errors":2}', errors=3), 1)
        self.assertEqual(writer.update_sync_run(tx, 1, {'errors':3}), 1)
        query = rendered(tx.calls[0][1])
        self.assertIn('%s::integer AS "errors"', query)
        self.assertIn('FOR NO KEY UPDATE OF run', query)
        self.assertIn('ROW((locked.current)."id",(locked.current)."run_type",(locked.current)."status"', query)
        self.assertNotIn('CROSS JOIN supplied', query)
        self.assertEqual(tx.calls[0][2], (3,1))
        update = rendered(tx.calls[1][1])
        self.assertIn('"errors"=proposed."errors"', update)
        self.assertIn('"summary_json"=%s::jsonb', update)
        self.assertNotIn('"started_at"=', update)
        self.assertEqual(json.loads(tx.calls[1][2][0]),
            {'reported_values':{'errors':2}, 'summary_schema_version':3, 'column_fields':1})
        self.assertEqual(tx.events, ['borrow'])

    def test_input_order_and_original_json_adapter_boundary_are_preserved(self):
        values = {'finished_at':'epoch', 'summary_json':{'errors':1}, 'duration_seconds':'3.5', 'errors':2}
        tx = Database(row('{"errors":1}', errors=2), 1)
        writer.update_sync_run(tx, 9, values)
        statement = rendered(tx.calls[0][1])
        self.assertLess(statement.index('%s::timestamptz'), statement.index('%s::jsonb'))
        self.assertLess(statement.index('%s::jsonb'), statement.index('%s::double precision'))
        parameters = tx.calls[0][2]
        self.assertEqual((parameters[0],parameters[2:]), ('epoch', ('3.5',2,9)))
        self.assertIsInstance(parameters[1], Jsonb)
        self.assertEqual(parameters[1].obj, {'errors':1})
        self.assertEqual(values['summary_json'], {'errors':1})

    def test_explicit_sql_null_summary_is_not_json_null(self):
        tx = Database(row(None, summary_json=None), 1)
        writer.update_sync_run(tx, 1, {'summary_json':None})
        self.assertEqual(tx.calls[0][2], (None,1))
        self.assertEqual(json.loads(tx.calls[1][2][0]), {'summary_schema_version':3,'column_fields':0})
        tx = Database(row('null', errors=1))
        with self.assertRaisesRegex(DatabaseError, '^sync_run_summary_shape_unsupported$'):
            writer.update_sync_run(tx, 1, {'errors':1})
        self.assertEqual(len(tx.calls), 1)
        self.assertEqual(tx.events, ['borrow','poison'])

    def test_missing_identity_returns_zero_without_normalizing_or_writing(self):
        tx = Database(None)
        with patch.object(writer,'normalize_sync_run_summary_text',side_effect=AssertionError('unexpected codec')):
            self.assertEqual(writer.update_sync_run(tx, '1', {'errors':1}), 0)
        self.assertEqual(len(tx.calls), 1)
        self.assertEqual(tx.calls[0][2], (1,'1'))

    def test_native_cast_and_unknown_column_failures_poison_without_retry(self):
        for values,code in (({'errors':'bad'},'22P02'), ({'errors':2**40},'22003'), ({'not_a_column':1},'42703')):
            error = DatabaseServerError('database_operation_failed',code=code)
            tx = Database(error)
            with self.assertRaises(DatabaseServerError) as caught:
                writer.update_sync_run(tx, 999, values)
            self.assertIs(caught.exception,error)
            self.assertEqual((len(tx.calls),tx.events), (1,['borrow','poison']))
            if code == '42703':
                self.assertIn('run."not_a_column"',rendered(tx.calls[0][1]))

    def test_invalid_identifier_fails_before_query(self):
        tx = Database()
        with self.assertRaisesRegex(ValueError,'database_identifier_invalid'):
            writer.update_sync_run(tx,1,{'errors; DROP TABLE':1})
        self.assertFalse(tx.calls)
        self.assertEqual(tx.events,['borrow','poison'])

    def test_timestamp_comparisons_borrow_same_transaction_once(self):
        tx = Database(row('{"started_at":"epoch","finished_at":"epoch"}',
            started_at='1970-01-01',finished_at='1970-01-01'),
            [{'position':1,'equal':True},{'position':2,'equal':True}],1)
        writer.update_sync_run(tx,1,{'errors':1})
        self.assertEqual([call[0] for call in tx.calls],['one','all','execute'])
        self.assertEqual(tx.limit,2)
        self.assertEqual(json.loads(tx.calls[-1][2][0]),{'summary_schema_version':3,'column_fields':6})
        self.assertEqual(tx.operations,[])

    def test_stored_huge_number_never_decodes_as_int_or_float(self):
        huge='9'*5001
        tx=Database({'summary_text':'{"requested":'+huge+',"unknown":"private"}',
            'columns_text':'{"errors":1,"summary_json":{"requested":'+huge+'}}'},1)
        writer.update_sync_run(tx,1,{'errors':1})
        self.assertIn('"requested":'+huge,tx.calls[-1][2][0])
        self.assertNotIn('private',tx.calls[-1][2][0])
        self.assertEqual(tx.calls[-1][2][1],'{"errors":1,"summary_json":{"requested":'+huge+'}}')

    def test_malformed_proposed_row_and_write_failure_never_retry(self):
        for proposed in ({}, {'columns_text':{},'summary_text':'{}'}):
            tx=Database(proposed)
            with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):
                writer.update_sync_run(tx,1,{'errors':1})
            self.assertEqual(len(tx.calls),1)
        error=DatabaseUnavailable('database_operation_failed',outcome_unknown=True)
        tx=Database(row(),error)
        with self.assertRaises(DatabaseUnavailable) as caught:
            writer.update_sync_run(tx,1,{'errors':1})
        self.assertIs(caught.exception,error)
        self.assertEqual(len(tx.calls),2)
        self.assertEqual(tx.events,['borrow','poison'])


class HotAndScrubTests(unittest.TestCase):
    def test_hot_zero_match_does_not_sample_clock_or_project(self):
        tx=Database(None)
        with patch.object(writer,'persisted_summary_text',side_effect=AssertionError('unexpected projection')):
            self.assertEqual(writer.interrupt_sync_run(tx,run_id=1,started_at='epoch',at='captured'),0)
        self.assertEqual(tx.calls,[('one',writer._HOT_LOCK,(1,'epoch'))])

    def test_hot_exact_predicate_clock_and_preprojection_precede_finalizer(self):
        huge='1'+'0'*5000
        current={'summary_text':'{"requested":'+huge+',"private":"drop"}',
                 'columns_text':'{"id":1,"started_at":"1970-01-01T00:00:00Z"}'}
        proposed=row('{"requested":'+huge+','+writer._HOT_FLAGS[1:],errors=1)
        tx=Database(current,proposed,1)
        self.assertEqual(writer.interrupt_sync_run(tx,run_id=1,started_at='epoch',at='captured'),1)
        self.assertEqual(tx.calls[1][2],('captured','{"requested":'+huge+'}',current['columns_text']))
        query=rendered(tx.calls[1][1])
        self.assertIn('greatest(0,extract(epoch FROM clock.at-run.started_at))',query)
        self.assertIn('greatest(run.errors,1)',query)
        self.assertNotIn('clock_timestamp',query)
        self.assertTrue(rendered(tx.calls[-1][1]).endswith(writer._RUNNING))
        self.assertEqual(tx.calls[-1][2][-2:],(1,'epoch'))
        self.assertIn('"requested":'+huge,tx.calls[-1][2][0])
        self.assertNotIn('private',tx.calls[-1][2][0])
        self.assertEqual(tx.events,['borrow'])

    def test_hot_late_native_failure_propagates_and_never_updates(self):
        error=DatabaseServerError('database_operation_failed',code='22003')
        tx=Database(row(),error)
        with self.assertRaises(DatabaseError) as caught:
            writer.interrupt_sync_run(tx,run_id=1,started_at='epoch',at='captured')
        self.assertIs(caught.exception,error)
        self.assertEqual(len(tx.calls),2)
        self.assertEqual(tx.events,['borrow','poison'])

    def test_scrub_missing_and_unchanged_rows_do_not_normalize(self):
        for answers in ((None,), (row(),{'columns_text':'{}','summary_text':'{}','changed':False})):
            tx=Database(*answers)
            with patch.object(writer,'normalize_sync_run_summary_text',side_effect=AssertionError('unexpected codec')):
                self.assertEqual(writer.scrub_sync_run(tx,run_id=1),0)
            self.assertEqual(tx.calls[0],('one',writer._SCRUB_LOCK,(1,)))
        self.assertIn('FOR UPDATE OF run NOWAIT',writer._SCRUB_LOCK)

    def test_scrub_keeps_preprojection_predicate_after_final_normalization(self):
        current=row('{"errors":5,"unknown":"private"}',errors=5)
        proposed=row('{"errors":5}',errors=5)
        tx=Database(current,{**proposed,'changed':True},1)
        self.assertEqual(writer.scrub_sync_run(tx,run_id=1),1)
        normalized,identity,compared=tx.calls[-1][2]
        self.assertEqual(identity,1)
        self.assertEqual(json.loads(normalized),{'summary_schema_version':3,'column_fields':1})
        self.assertEqual(compared,'{"errors":5}')
        self.assertEqual(tx.calls[-1][1],writer._SCRUB_UPDATE)
        self.assertNotEqual(normalized,compared)
        self.assertEqual(tx.events,['borrow'])

    def test_scrub_nowait_failure_has_no_retry_or_new_candidate(self):
        error=DatabaseServerError('database_operation_failed',code='55P03')
        tx=Database(error)
        with self.assertRaises(DatabaseServerError) as caught:
            writer.scrub_sync_run(tx,run_id=1)
        self.assertIs(caught.exception,error)
        self.assertEqual(len(tx.calls),1)
        self.assertEqual(tx.events,['borrow','poison'])

    def test_no_expiry_clock_pool_callback_or_retained_function_dispatch(self):
        source=inspect.getsource(writer)
        for token in ('clock_timestamp(', 'now()', '.transaction(', '.call(', 'CREATE ', 'GRANT ', 'LIMIT ', 'OFFSET ', 'json.loads('):
            self.assertNotIn(token,source)
        self.assertNotIn('cutoff',writer._SCRUB_LOCK+writer._SCRUB_UPDATE)

    def test_all_statement_families_bind_and_pass_driver_statement_guard(self):
        tx=Database(row(),1);writer.update_sync_run(tx,1,{'errors':1,'summary_json':None})
        tx2=Database(row(),row(),1);writer.interrupt_sync_run(tx2,run_id=1,started_at='epoch',at='epoch')
        tx3=Database(row(),dict(row(),changed=True),1);writer.scrub_sync_run(tx3,run_id=1)
        for tx in (tx,tx2,tx3):
            for kind,statement,parameters in tx.calls:
                mode='read' if kind=='one' else 'write'
                guarded=_reviewed_statement(statement,read=mode=='read',max_rows=1 if mode=='read' else None)
                query=PostgresQuery(Transformer());query.convert(guarded,parameters)
                self.assertTrue(query.query)


if __name__=='__main__':
    unittest.main()
