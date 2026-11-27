"""Semantic guards for the export and status reads; no PostgreSQL connection."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.user_work import user_work_read_queries as export
from db.base.sql_values import application_value
from psycopg.types.json import JsonbLoader
from test_user_work_read_queries import Database, COUNTS

class ExportEncodingTests(unittest.TestCase):
    def test_document_projects_each_section_once_and_sizes_it_in_sql(self):
        # The main row sections each have a materialized projection.
        sections=[line.split('(WITH projected AS MATERIALIZED (SELECT ',1)[0].strip().split("'")[1]
                  for line in export._EXPORT_DOCUMENT.splitlines() if '(WITH projected AS MATERIALIZED (SELECT ' in line]
        self.assertEqual(sections,['folders','labels','channels','assignments','saved_objects','members','pending','reviews'])
        preferences="'field_preferences',(SELECT coalesce(pending_import,document) FROM readable.research_preferences WHERE workspace_id='installation'),\n"
        self.assertEqual(export._EXPORT_DOCUMENT.count(preferences),1)
        self.assertEqual(export._EXPORT_DOCUMENT.count(export._ALERT_EXPORT),1)
        self.assertEqual(export._EXPORT_ESTIMATE.count(export._ALERT_EXPORT),1)
        self.assertIn('FROM readable.alert_rules',export._ALERT_EXPORT)
        self.assertEqual(export._EXPORT_DOCUMENT.count('sized as not materialized'),1)
        self.assertEqual(export._EXPORT_DOCUMENT.count('value::text'),1)
        self.assertEqual(export._EXPORT_DOCUMENT.count('octet_length('),1)
        self.assertIn('SELECT 1 FROM readable.channel_user_work LIMIT 10001',export._EXPORT_CAPACITY)
        # The status document carries the reconnect state of each channel.
        reconnect=",'reconnect_state',p.reconnect_state,\n      'reconnect_not_before',p.reconnect_not_before,'reconnect_error_code',p.reconnect_error_code"
        self.assertEqual(export._STATUS_DOCUMENT.count(reconnect),1)

    def test_text_decode_matches_installed_native_jsonb_decode_and_normalization(self):
        vectors=['{}','{"integer":9007199254740993123456789}',
                 '{"positive":1e999,"negative":-1e999,"small":1e-999}',
                 '{"text":"Київ / O\\\"Brien", "escaped":"\\u041a"}',
                 '{"stamp":"2026-10-06T12:34:56+00:00","nested":[null,true,{},[]]}',
                 '{"real":1.2500,"negative_zero":-0.0}',
                 '{"folders":[{"name":"  / αβ  ","id":"00000000-0000-0000-0000-000000000000"}]}']
        loader=JsonbLoader(3802)
        for encoded in vectors:
            with self.subTest(encoded=encoded):
                self.assertEqual(export._export_document({'encoded_value':encoded}),
                                 application_value(loader.load(encoded.encode('utf-8'))))

    def test_all_three_guards_reject_before_decoding_and_rollback(self):
        maximum=export._MAX_BYTES
        cases=[(dict(COUNTS,folders=1001),),
               (COUNTS,{'total_bytes':maximum-17407}),
               (COUNTS,{'total_bytes':0},{'total_bytes':maximum+1,'encoded_value':None})]
        for rows in cases:
            database=Database(*rows)
            with patch.object(export.json,'loads',side_effect=AssertionError('decoded_rejected_payload')) as decode:
                with self.assertRaises(export.DatabaseServerError):export.export_snapshot(database)
            decode.assert_not_called()
            self.assertEqual(len(database.queries),len(rows))
            self.assertEqual(database.events,['begin','rollback'])

    def test_malformed_transport_and_nonobject_json_fail_closed(self):
        for value in (None,[],{},b'{}','[1]','null','42','{broken'):
            with self.subTest(value=value),self.assertRaisesRegex(RuntimeError,'user_work_response_invalid'):
                export._export_document({'encoded_value':value})

    def test_exact_byte_limit_decodes_once_and_preserves_operation(self):
        encoded=json.dumps({'folders':[],'note':'  α  '},ensure_ascii=False)
        database=Database(COUNTS,{'total_bytes':export._MAX_BYTES-17408},
                          {'total_bytes':export._MAX_BYTES,'encoded_value':encoded})
        with patch.object(export.json,'loads',wraps=json.loads) as decode:
            self.assertEqual(export.export_snapshot(database),{'folders':[],'note':'  α  '})
        decode.assert_called_once_with(encoded)
        self.assertEqual(database.events,['begin','commit'])
        self.assertEqual(len(database.operations),1)
        self.assertTrue(database.operations[0].read_only and database.operations[0].repeatable_read)
        self.assertEqual([s for s,p in database.queries],[export._EXPORT_CAPACITY,export._EXPORT_ESTIMATE,export._EXPORT_DOCUMENT])

if __name__=='__main__':unittest.main()
