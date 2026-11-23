"""Prepared stored-row normalization: exact JSONB text, no DB/server requests."""
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.base.postgres import DatabaseError, DatabaseServerError
from db.runs.sync_run_summary import (
    COLUMN_FIELDS, SQL_NULL, _NativeJsonNumber, compare_summary_timestamps,
    normalize_sync_run_summary, normalize_sync_run_summary_text, persisted_summary_text,
)


def exact_json(text):
    return json.loads(text, parse_int=Decimal, parse_float=Decimal)


class NativeNormalizationTests(unittest.TestCase):
    def assert_error(self, summary, columns, message, code="P0001", **kwargs):
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary_text(summary, columns, **kwargs)
        self.assertEqual((caught.exception.message, caught.exception.code), (message, code))

    def test_extreme_originals_and_unrelated_nested_values_remain_numbers(self):
        huge = "1" + "0" * 5000
        tiny = "0." + "0" * 399 + "1"
        raw = ('{"errors":' + huge + ',"requested":' + tiny
               + ',"unrelated":[{"scaled":123.4500,"negative_zero":-0.0000},' + huge + ']}' )
        with localcontext() as context:
            context.prec = 2
            result = normalize_sync_run_summary_text(raw, '{"errors":0}')
        values = exact_json(result)
        self.assertEqual(values["reported_values"]["errors"], Decimal(huge))
        self.assertEqual(values["unrelated"][1], Decimal(huge))
        for token in (huge, tiny, "123.4500", "-0.0000"):
            self.assertIn(token, result)
            self.assertNotIn('"' + token + '"', result)
        projected = persisted_summary_text(result)
        self.assertIn(huge, projected)
        self.assertIn(tiny, projected)
        self.assertNotIn("unrelated", projected)
        self.assertEqual(normalize_sync_run_summary_text(result, "{}"), result)

    def test_numeric_equality_is_exact_recursive_and_type_sensitive(self):
        huge = "9" * 5001
        vectors = (
            (huge, huge + ".0000", False),
            ("9007199254740993.0000", "9007199254740992", True),
            ("0.000000000000000000001", "0", True),
            ("-0.0000", "0.0", False),
            ('{"a":[1.00,{"b":2.000}]}', '{"a":[1,{"b":2.0}]}', False),
            ("true", "1", True), ("false", "0", True),
            ('"123"', "123", True), ("null", "null", False),
            ('[true,"2"]', "[1,2]", True),
        )
        with localcontext() as context:
            context.prec = 1
            for original, actual, reported in vectors:
                with self.subTest(original=original[:40]):
                    result = exact_json(normalize_sync_run_summary_text(
                        '{"errors":' + original + '}', '{"errors":' + actual + '}'))
                    self.assertEqual("reported_values" in result, reported)
                    self.assertEqual(result["column_fields"], 1)

    def test_version_text_mask_syntax_and_error_precedence(self):
        for version in ("null", "2", '\"2\"'):
            self.assertEqual(exact_json(normalize_sync_run_summary_text(
                '{"summary_schema_version":' + version + '}', "{}"))["column_fields"], 0)
        for version in ("3", '\"3\"'):
            self.assert_error('{"summary_schema_version":' + version + '}', "{}",
                              "sync_run_summary_missing_column_mask")
        for version in ("2.0", "3.00", "true", "[]", "{}", '\"02\"'):
            self.assert_error('{"summary_schema_version":' + version
                              + ',"column_fields":null}', "{}", "sync_run_summary_shape_unsupported")
        for mask in ("null", "true", "false", "1.00", "-1", "1024", '\"1\"', "[]", "{}"):
            self.assert_error('{"column_fields":' + mask + ',"reported_values":null}',
                              "{}", "sync_run_summary_invalid_column_mask")
        for mask in ("2147483648", "9" * 5001):
            self.assert_error('{"column_fields":' + mask + ',"reported_values":null}',
                              "{}", "database_operation_failed", "22003")
        self.assert_error('{"column_fields":1023,"reported_values":null}', "{}",
                          "sync_run_summary_invalid_reported_values")

    def test_sql_null_json_null_and_json_exists_column_semantics(self):
        self.assertEqual(exact_json(normalize_sync_run_summary_text(None, "{}")),
                         {"summary_schema_version":3, "column_fields":0})
        for raw in ("null", "[]", "true", "2", '\"\"'):
            self.assert_error(raw, "{}", "sync_run_summary_shape_unsupported")
        for columns in (None, '["errors"]', '\"errors\"'):
            self.assertEqual(exact_json(normalize_sync_run_summary_text(
                '{"errors":null}', columns))["reported_values"], {"errors":None})
        for columns in ("null", "{}", "true", "2", "[1]"):
            self.assert_error('{"errors":null}', columns, "database_operation_failed")
        self.assertEqual(exact_json(normalize_sync_run_summary_text(
            '{"started_at":"epoch"}', None))["reported_values"], {"started_at":"epoch"})

    def test_all_ten_fields_masks_and_ordinary_wire_results_agree(self):
        summary, columns = {}, {}
        for i, (path, column) in enumerate(COLUMN_FIELDS):
            target = summary
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = None if column.endswith("_at") else i
            columns[column] = target[path[-1]]
        for original, actual in ((summary, columns), ({"errors":True}, {"errors":1}),
                ({"channel_metadata":None, "regular_video_stats":[1]}, {}),
                ({"reported_values":{}, "column_fields":12}, {}),
                ({"errors":1.5, "reported_values":{"errors":1.5}}, {"errors":2})):
            with self.subTest(original=original):
                text = normalize_sync_run_summary_text(json.dumps(original), json.dumps(actual))
                self.assertEqual(json.loads(text), normalize_sync_run_summary(original, actual))
        result = exact_json(normalize_sync_run_summary_text(json.dumps(summary), json.dumps(columns)))
        self.assertEqual(result["column_fields"], 1023)
        self.assertEqual(result["channel_metadata"], {})
        self.assertEqual(result["regular_video_stats"], {})

    def test_native_reported_equality_and_private_conflict(self):
        huge = "1" + "0" * 5000
        original = '{"errors":' + huge + ',"reported_values":{"errors":' + huge + '.00}}'
        result = normalize_sync_run_summary_text(original, '{"errors":0}')
        self.assertIn('"errors":' + huge + '}', result)
        self.assert_error('{"errors":123.00,"reported_values":{"errors":"123"}}',
                          '{"errors":0}', "database_operation_failed")

    def test_storage_projection_is_separate_and_wire_rejects_native_tokens(self):
        result = normalize_sync_run_summary_text(
            '{"errors":3,"yield_requested":true,"unknown":{"keep":[1.20]}}', '{"errors":3}')
        self.assertEqual(exact_json(result)["unknown"], {"keep":[Decimal("1.20")]})
        self.assertTrue(exact_json(result)["yield_requested"])
        self.assertNotIn("yield_requested", persisted_summary_text(result))
        for value in (_NativeJsonNumber("1"), Decimal("1")):
            with self.assertRaisesRegex(ValueError, "sync_run_summary_wire_value_invalid"):
                normalize_sync_run_summary({"errors":value}, {"errors":0})
            with self.assertRaisesRegex(ValueError, "sync_run_summary_wire_value_invalid"):
                compare_summary_timestamps(Mock(), [(value, None)])
        self.assertEqual(normalize_sync_run_summary(SQL_NULL, {}),
                         {"summary_schema_version":3, "column_fields":0})

    def test_invalid_transport_fails_before_any_transaction_call(self):
        transaction = Mock()
        for raw in (1, {}, '{"errors":NaN}', '{"errors":Infinity}', '{"secret":broken}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "^sync_run_summary_native_text_invalid$"):
                normalize_sync_run_summary_text(raw, "{}", transaction=transaction)
        transaction.fetch_all.assert_not_called()


class NativeTimestampTests(unittest.TestCase):
    def test_two_pairs_use_one_supplied_transaction_with_unquoted_native_numbers(self):
        huge = "8" * 5001
        transaction = Mock()
        transaction.fetch_all.return_value = [
            {"position":1, "equal":False}, {"position":2, "equal":True}]
        result = normalize_sync_run_summary_text(
            '{"started_at":' + huge + ',"finished_at":"epoch"}',
            '{"started_at":0,"finished_at":"1970-01-01T00:00:00Z"}', transaction=transaction)
        statement, parameters = transaction.fetch_all.call_args.args
        self.assertEqual(parameters, ('[[' + huge + ',0],["epoch","1970-01-01T00:00:00Z"]]',))
        self.assertEqual(transaction.fetch_all.call_args.kwargs, {"max_rows":2})
        self.assertIn("::timestamptz", statement)
        self.assertNotIn(huge, statement)
        self.assertEqual(transaction.fetch_all.call_count, 1)
        transaction.transaction.assert_not_called()
        self.assertEqual(exact_json(result)["reported_values"], {"started_at":Decimal(huge)})
        self.assertEqual(exact_json(result)["column_fields"], 6)

    def test_equal_values_need_no_transaction_but_native_grammar_does(self):
        transaction = Mock()
        normalize_sync_run_summary_text('{"started_at":1.00}', '{"started_at":1}', transaction=transaction)
        transaction.fetch_all.assert_not_called()
        with self.assertRaisesRegex(ValueError, "sync_run_summary_timestamp_comparator_required"):
            normalize_sync_run_summary_text('{"started_at":"epoch"}', '{"started_at":"1970-01-01"}')
        transaction.fetch_all.return_value = [{"position":1, "equal":True}]
        result = normalize_sync_run_summary_text('{"started_at":"tomorrow"}',
            '{"started_at":"2026-10-07"}', transaction=transaction)
        self.assertNotIn("reported_values", exact_json(result))
        self.assertEqual(transaction.fetch_all.call_args.args[1], ('[["tomorrow","2026-10-07"]]',))

    def test_first_field_conflict_never_dispatches_later_timestamp(self):
        transaction = Mock()
        with self.assertRaises(DatabaseError) as caught:
            normalize_sync_run_summary_text(
                '{"errors":1.0,"started_at":"private invalid zone","reported_values":{"errors":2}}',
                '{"errors":3,"started_at":"epoch"}', transaction=transaction)
        self.assertEqual((caught.exception.message, caught.exception.code), ("database_operation_failed", "P0001"))
        transaction.fetch_all.assert_not_called()

    def test_deferred_native_error_order_and_no_raw_messages(self):
        vectors = (
            ('{"started_at":"private zone","finished_at":"epoch"}', '{"started_at":"epoch"}',
             [{"position":1, "equal":False, "error_code":"22023"}], "22023"),
            ('{"started_at":"later","finished_at":"private zone","reported_values":{"started_at":"earlier"}}',
             '{"started_at":"epoch","finished_at":"epoch"}',
             [{"position":1,"equal":False},{"position":2,"equal":False,"error_code":"22009"}], "P0001"),
        )
        for summary, columns, rows, code in vectors:
            transaction = Mock()
            transaction.fetch_all.return_value = rows
            with self.assertRaises(DatabaseError) as caught:
                normalize_sync_run_summary_text(summary, columns, transaction=transaction)
            self.assertEqual((caught.exception.message, caught.exception.code), ("database_operation_failed", code))
            self.assertEqual(transaction.fetch_all.call_count, 1)
            self.assertNotIn("private", str(caught.exception))

    def test_driver_failure_is_not_retried_and_malformed_rows_fail_closed(self):
        transaction = Mock()
        error = DatabaseServerError("database_operation_failed", code="57014")
        transaction.fetch_all.side_effect = error
        with self.assertRaises(DatabaseServerError) as caught:
            normalize_sync_run_summary_text('{"started_at":"a"}', '{"started_at":"b"}', transaction=transaction)
        self.assertIs(caught.exception, error)
        self.assertEqual(transaction.fetch_all.call_count, 1)
        for rows in ([], [{"position":2,"equal":True}], [{"position":1,"equal":None}],
                     [{"position":1,"equal":False,"error_code":"private"}]):
            transaction = Mock()
            transaction.fetch_all.return_value = rows
            with self.assertRaisesRegex(ValueError, "sync_run_summary_timestamp_comparison_invalid"):
                normalize_sync_run_summary_text('{"started_at":"a"}', '{"started_at":"b"}', transaction=transaction)
            self.assertEqual(transaction.fetch_all.call_count, 1)


if __name__ == "__main__":
    unittest.main()
