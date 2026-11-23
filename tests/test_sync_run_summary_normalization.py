"""Prepared summary codec tests; no database, server or provider work."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.base.postgres import DatabaseError, DatabaseServerError
from db.runs.sync_run_summary import (COLUMN_FIELDS, SQL_NULL, compare_summary_timestamps,
    normalize_sync_run_summary, persisted_summary_copy, project_sync_run_summary)
from operational_copy_policy import safe_summary_copy


class NormalizerTests(unittest.TestCase):
    def test_sql_null_is_empty_but_json_null_is_unsupported(self):
        self.assertEqual(normalize_sync_run_summary(SQL_NULL, {}),
                         {'summary_schema_version':3, 'column_fields':0})
        for value in (None, [], '', 2, True):
            with self.subTest(value=value), self.assertRaises(DatabaseError) as caught:
                normalize_sync_run_summary(value, {})
            self.assertEqual(caught.exception.message, 'sync_run_summary_shape_unsupported')
            self.assertEqual(caught.exception.code, 'P0001')

    def test_wire_boundary_rejects_native_values_and_non_json_objects(self):
        cycle = []; cycle.append(cycle)
        for value in (datetime.now(timezone.utc), Decimal('1'), float('nan'), float('inf'),
                      {1:'value'}, {'errors': SQL_NULL}, cycle):
            with self.subTest(kind=type(value)), self.assertRaisesRegex(ValueError, 'sync_run_summary_wire_value_invalid'):
                normalize_sync_run_summary({'errors':value}, {'errors':0})

    def test_each_bit_sparse_projection_round_trip_and_input_immutability(self):
        self.assertEqual(len(COLUMN_FIELDS), 10)
        for bit, (path, column) in enumerate(COLUMN_FIELDS):
            original = {}; target = original
            for key in path[:-1]: target = target.setdefault(key, {})
            target[path[-1]] = '2026-10-05T00:00:00+00:00' if column.endswith('_at') else bit+1
            before = deepcopy(original)
            encoded = normalize_sync_run_summary(original, {column:target[path[-1]]})
            self.assertEqual(encoded['column_fields'], 1 << bit)
            self.assertEqual(project_sync_run_summary({'summary_json':encoded, column:target[path[-1]]}),
                             {**original,'summary_schema_version':3})
            self.assertEqual(original, before)

    def test_full_mask_and_idempotence(self):
        summary, columns = {}, {}
        for i,(path,column) in enumerate(COLUMN_FIELDS):
            target=summary
            for key in path[:-1]: target=target.setdefault(key,{})
            target[path[-1]]=None if column.endswith('_at') else i
            columns[column]=target[path[-1]]
        encoded=normalize_sync_run_summary(summary, columns)
        self.assertEqual(encoded['column_fields'],1023)
        self.assertEqual(normalize_sync_run_summary(encoded,{}),encoded)
        self.assertEqual(persisted_summary_copy(persisted_summary_copy(encoded)),persisted_summary_copy(encoded))

    def test_supported_version_text_matches_jsonb_not_python_numeric_coercion(self):
        for version in (None, 2, '2'):
            self.assertEqual(normalize_sync_run_summary({'summary_schema_version':version},{})['column_fields'],0)
        for version in (3,'3'):
            self.assertEqual(normalize_sync_run_summary({'summary_schema_version':version,'column_fields':4},{})['column_fields'],4)
        for version in (2.0,3.0,True,False,'02',{},[]):
            with self.subTest(version=version), self.assertRaises(DatabaseError) as caught:
                normalize_sync_run_summary({'summary_schema_version':version,'column_fields':0},{})
            self.assertEqual(caught.exception.message,'sync_run_summary_shape_unsupported')

    def test_mask_validation_and_integer_overflow_precedence(self):
        for mask in (None,True,False,1.0,-1,1024,'1',[],{}):
            with self.subTest(mask=mask),self.assertRaises(DatabaseError) as caught:
                normalize_sync_run_summary({'column_fields':mask},{})
            self.assertEqual(caught.exception.message,'sync_run_summary_invalid_column_mask')
        for mask in (2147483648, 10**30, 1e20):
            with self.subTest(mask=mask),self.assertRaises(DatabaseError) as caught:
                normalize_sync_run_summary({'column_fields':mask},{})
            self.assertEqual((caught.exception.message,caught.exception.code),('database_operation_failed','22003'))
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary({'summary_schema_version':'3'}, {})
        self.assertEqual(caught.exception.message,'sync_run_summary_missing_column_mask')

    def test_reported_values_validation_conflict_and_private_error(self):
        for reported in (None,[],True,0,''):
            with self.subTest(reported=reported),self.assertRaises(DatabaseError) as caught:
                normalize_sync_run_summary({'reported_values':reported},{})
            self.assertEqual(caught.exception.message,'sync_run_summary_invalid_reported_values')
        self.assertEqual(normalize_sync_run_summary({'errors':1,'reported_values':{'errors':1.0}}, {'errors':2})['reported_values'],{'errors':1})
        for summary,columns in (({'errors':1},{}),({'errors':1,'reported_values':{'errors':True}},{'errors':2})):
            with self.assertRaises(DatabaseError) as caught: normalize_sync_run_summary(summary,columns)
            self.assertEqual((caught.exception.message,caught.exception.code),('database_operation_failed','P0001'))

    def test_jsonb_boolean_number_null_and_recursive_numeric_equality(self):
        for original, actual, differs in ((True,1,True),(False,0,True),(1,1.0,False),
              ([True], [1],True),({'x':1}, {'x':1.0},False),
              (9007199254740993,9007199254740992.0,True),(None,None,False),(None,0,True)):
            result=normalize_sync_run_summary({'errors':original},{'errors':actual})
            self.assertEqual('reported_values' in result,differs)
            self.assertEqual(result['column_fields'],1)

    def test_sql_null_columns_and_json_exists_scalar_behavior(self):
        for columns in (SQL_NULL,['errors'],'errors'):
            self.assertEqual(normalize_sync_run_summary({'errors':None},columns)['reported_values'],{'errors':None})
        with self.assertRaises(DatabaseError): normalize_sync_run_summary({'errors':1},None)

    def test_nested_missing_non_object_and_empty_sections_are_preserved_by_codec(self):
        summary={'channel_metadata':None,'regular_video_stats':[1], 'errors':0}
        result=normalize_sync_run_summary(summary, {'errors':0})
        self.assertIsNone(result['channel_metadata']); self.assertEqual(result['regular_video_stats'],[1])
        result=normalize_sync_run_summary({'channel_metadata':{'processed':2}},{'metadata_processed':2})
        self.assertEqual(result['channel_metadata'],{})
        self.assertNotIn('channel_metadata',persisted_summary_copy(result))

    def test_timestamps_are_one_batch_and_matching_wire_values_need_no_cast(self):
        compare=Mock(return_value=[True,False])
        summary={'started_at':'2026-10-05T01:00:00+01:00','finished_at':'bad timestamp'}
        columns={'started_at':'2026-10-05T00:00:00Z','finished_at':None}
        result=normalize_sync_run_summary(summary,columns,compare_timestamps=compare)
        compare.assert_called_once_with([(summary['started_at'],columns['started_at']),('bad timestamp',None)])
        self.assertEqual(result['reported_values'],{'finished_at':'bad timestamp'})
        compare.reset_mock()
        normalize_sync_run_summary(summary,summary,compare_timestamps=compare)
        compare.assert_not_called()

    def test_earlier_reported_conflict_precedes_later_timestamp_without_comparing(self):
        compare=Mock(side_effect=DatabaseServerError('database_operation_failed',code='22023'))
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary({'errors':1,'reported_values':{'errors':2},'started_at':'bad zone'},
                {'errors':3,'started_at':'epoch'},compare_timestamps=compare)
        self.assertEqual(caught.exception.code,'P0001');compare.assert_not_called()

    def test_earlier_timestamp_error_precedes_later_missing_column(self):
        error=DatabaseServerError('database_operation_failed',code='22023')
        compare=Mock(return_value=[error])
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary({'started_at':'bad zone','finished_at':'epoch'},
                {'started_at':'epoch'},compare_timestamps=compare)
        self.assertIs(caught.exception,error);compare.assert_called_once()

    def test_first_timestamp_conflict_precedes_second_deferred_native_error(self):
        compare=Mock(return_value=[False,DatabaseServerError('database_operation_failed',code='22009')])
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary({'started_at':'later','finished_at':'bad zone',
                'reported_values':{'started_at':'earlier'}},{'started_at':'epoch','finished_at':'epoch'},compare_timestamps=compare)
        self.assertEqual(caught.exception.code,'P0001');compare.assert_called_once()

    def test_missing_and_malformed_comparator_results_fail_closed(self):
        for comparator in (None,lambda pairs:None,lambda pairs:[None],lambda pairs:[],lambda pairs:[1]):
            with self.assertRaises(ValueError):
                normalize_sync_run_summary({'started_at':'epoch'}, {'started_at':'1970-01-01'},compare_timestamps=comparator)


class NativeComparisonTests(unittest.TestCase):
    def test_reuses_given_transaction_and_binds_at_most_two_pairs(self):
        transaction=Mock()
        transaction.fetch_all.return_value=[{'position':1,'equal':True},{'position':2,'equal':False}]
        pairs=[('epoch','1970-01-01T00:00:00Z'),('secret literal', None)]
        self.assertEqual(compare_summary_timestamps(transaction,pairs),[True,False])
        statement,parameters=transaction.fetch_all.call_args.args
        self.assertEqual(parameters[0].obj,pairs)
        self.assertEqual(transaction.fetch_all.call_args.kwargs,{'max_rows':2})
        self.assertNotIn('secret literal',statement)
        self.assertIn('pg_input_error_info',statement); self.assertIn("('22007', '22008')",statement)
        self.assertIn('::timestamptz',statement);self.assertIn('LIMIT 3',statement)
        transaction.transaction.assert_not_called()

    def test_empty_or_invalid_batch_does_not_dispatch(self):
        transaction=Mock()
        self.assertEqual(compare_summary_timestamps(transaction,[]),[])
        for value in ([('a','b')]*3,[('a',)],None,[(datetime.now(timezone.utc),'a')]):
            with self.assertRaises(ValueError): compare_summary_timestamps(transaction,value)
        transaction.fetch_all.assert_not_called()

    def test_native_errors_are_not_retried_or_masked(self):
        transaction=Mock(); error=DatabaseError('database_operation_failed',code='22023')
        transaction.fetch_all.side_effect=error
        with self.assertRaises(DatabaseError) as caught:compare_summary_timestamps(transaction,[('a','b')])
        self.assertIs(caught.exception,error);self.assertEqual(transaction.fetch_all.call_count,1)

    def test_native_input_error_is_deferred_without_raw_message(self):
        transaction=Mock();transaction.fetch_all.return_value=[{'position':1,'equal':False,'error_code':'22023'}]
        values=compare_summary_timestamps(transaction,[('secret-invalid-zone','epoch')])
        self.assertEqual((values[0].code,values[0].message),('22023','database_operation_failed'))
        self.assertNotIn('secret',str(values[0]))

    def test_native_result_shape_is_checked(self):
        for rows in ([],[{'position':2,'equal':True}],[{'position':1,'equal':None}]):
            transaction=Mock();transaction.fetch_all.return_value=rows
            with self.assertRaises(ValueError): compare_summary_timestamps(transaction,[('a','b')])


class PersistedProjectionTests(unittest.TestCase):
    def test_storage_excludes_yield_but_api_policy_keeps_it(self):
        value={'yield_requested':True,'errors':0,'video_collection':{'yield_requested':True,'requested':2}}
        self.assertTrue(safe_summary_copy(value)['yield_requested'])
        self.assertEqual(persisted_summary_copy(value),{'errors':0,'video_collection':{'requested':2}})
        self.assertTrue(value['video_collection']['yield_requested'])

    def test_finite_names_scalars_labels_and_depth(self):
        value={'errors':None,'requested':1.25,'mode':'ready','unknown':'secret','updated':'secret',
               'video_collection':{'errors':True},'new_video_stats':[], 'reported_values':{'errors':2,'started_at':'epoch','channel_metadata.processed':3}}
        expected={'errors':None,'requested':1.25,'mode':'ready','video_collection':{'errors':True},'reported_values':{'errors':2}}
        self.assertEqual(persisted_summary_copy(value),expected)
        self.assertEqual(persisted_summary_copy(value,3),{k:v for k,v in expected.items() if k not in {'video_collection','reported_values'}})
        self.assertEqual(persisted_summary_copy(value,4),{})
        self.assertEqual(persisted_summary_copy(SQL_NULL),{})
        self.assertEqual(persisted_summary_copy(None),{})

    def test_sql_null_depth_recurses_without_default_cap(self):
        value={'errors':1}
        for _ in range(5):value={'video_collection':value}
        self.assertEqual(persisted_summary_copy(value),{})
        self.assertEqual(persisted_summary_copy(value,None),value)



if __name__ == '__main__':
    unittest.main()

