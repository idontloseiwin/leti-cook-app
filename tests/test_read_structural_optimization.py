"""Semantic guards for reviewed read optimizations; no PostgreSQL connection."""
from hashlib import sha256
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.reads import analytics_read_queries as analytics
from db.user_work import user_work_read_queries as export
from db.base.sql_values import application_value
from psycopg.types.json import JsonbLoader
from test_user_work_read_queries import Database, COUNTS
from test_user_work_export_projection import before_reader_views

class TimelineBaselineTests(unittest.TestCase):
    def test_admission_and_materialization_preserve_original_timeline_algorithm(self):
        # Undoing the accepted materialization must give the reviewed pre-56 query (its SHA-256).
        self.assertEqual(analytics.TIMELINE_SQL.count('scan_meta as materialized ('),1)
        # R10 adds per-bucket clocks and separates the whole-scan clock. Undo
        # only these additive lifetime fields; the numeric algorithm is pinned.
        timeline=analytics.TIMELINE_SQL
        additions=(
            ("""            ) as previous_metric_value,
            lag(bucket_assigned.recorded_at) over (
                order by bucket_assigned.recorded_at asc
            ) as previous_recorded_at
""", """            ) as previous_metric_value
"""),
            ("""            ) as correction_within_bucket,
            min(least(annotated.recorded_at, annotated.previous_recorded_at))
                + interval '696 hours' AS source_expires_at
""", """            ) as correction_within_bucket
"""),
            ("""            bucketed.correction_within_bucket,
            bucketed.source_expires_at
""", """            bucketed.correction_within_bucket
"""),
            ("""        scan_meta.snapshot_count,
        limited.source_expires_at,
        least((SELECT min(recorded_at) FROM selected),
              (SELECT min(recorded_at) FROM predecessor))+interval '696 hours' AS response_source_expires_at
""", """        scan_meta.snapshot_count
"""),
        )
        self.assertEqual(timeline.count("interval '696 hours'"),4)
        for added,original in additions:
            self.assertEqual(timeline.count(added),1)
            timeline=timeline.replace(added,original,1)
        self.assertEqual(timeline.count('readable.channel_snapshots'),4)
        timeline=timeline.replace('readable.channel_snapshots','public.channel_snapshots')
        self.assertEqual(timeline.count("interval '696 hours'"),2)
        timeline=timeline.replace("interval '696 hours'","interval '672 hours'")
        before=timeline.replace('scan_meta as materialized (','scan_meta as (',1)
        self.assertEqual(before.count('scan_meta as ('),1)
        self.assertNotIn('scan_meta as materialized (',before)
        self.assertEqual(sha256(before.encode()).hexdigest(),'446b79e68657a19b3eaff9ebbbef339de5adea7be515d24fed4000abe264277b')
        # The old clock/scan/predecessor/lag prefix is still exact after undoing
        # just the accepted executor-materialization directive. Whole-query
        # equality above also guards the aggregation and result suffix.
        prefix=timeline.replace('scan_meta as materialized (','scan_meta as (',1).split('    bucketed as (',1)[0]
        self.assertEqual(sha256(prefix.encode()).hexdigest(),'405bce5c35f664eee0ef02bc3c4e992895ad25ee24b9675f04a53d99bf3b4286')
        schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
        self.assertIn('ADD CONSTRAINT channel_snapshots_pkey PRIMARY KEY (channel_id, recorded_at)',schema)

    def test_rejected_latest_row_candidate_is_not_activated(self):
        self.assertEqual(analytics.TIMELINE_SQL.count('array_agg('),2)
        self.assertNotIn('join selected as latest',analytics.TIMELINE_SQL)

class ExportEncodingTests(unittest.TestCase):
    def test_capacity_estimate_status_and_document_projection_are_unchanged(self):
        expected={'_EXPORT_CAPACITY':'2a2a87c7cd7c5f81c970445d7c17a8da5b0db74029905457cd95b732c3ba881a',
                  '_EXPORT_ESTIMATE':'3fe34793c2657437bc97f4603d16b37e15f4e8df4700ea80b20730048414ced4',
                  '_STATUS_DOCUMENT':'723382ed2f91b9fc093d29ae3f32c37ece212d03682bfc63246d92300142dda0'}
        for name,digest in expected.items():
            statement=getattr(export,name)
            if name=='_STATUS_DOCUMENT':
                # F4 adds only these three local reconnect fields. Keep the
                # original projection/ordering digest after undoing that delta.
                addition=",'reconnect_state',p.reconnect_state,\n      'reconnect_not_before',p.reconnect_not_before,'reconnect_error_code',p.reconnect_error_code"
                self.assertEqual(statement.count(addition),1)
                statement=statement.replace(addition,'',1)
            self.assertEqual(sha256(before_reader_views(statement).encode()).hexdigest(),digest)
        # Accepted59 materializes each original JSONB projection, preserving
        # constructors and original sort expressions. Undo only those wrappers.
        reverse=[];sections=[]
        for line in export._EXPORT_DOCUMENT.splitlines(keepends=True):
            if '(WITH projected AS MATERIALIZED (SELECT ' not in line:
                reverse.append(line);continue
            prefix,tail=line.split('(WITH projected AS MATERIALIZED (SELECT ',1)
            projection,rest=tail.rsplit(') SELECT ',1)
            aggregate,suffix=rest.rsplit(' FROM projected)',1)
            reverse.append(prefix+'(SELECT '+aggregate+' FROM (SELECT '+projection+') projected)'+suffix)
            sections.append(prefix.strip().split("'")[1])
        self.assertEqual(sections,['folders','labels','channels','assignments','saved_objects','members','pending','reviews'])
        original=before_reader_views(''.join(reverse))
        # The reviewed pre-59 document, pinned by its SHA-256.
        self.assertEqual(sha256(original.encode()).hexdigest(),'3a653a2274a19af8ae45a224444ef2bb7227d8c3e8f4069668c59f6092966eea')
        body=original.split(') AS value),',1)[0]
        self.assertEqual(sha256(body.encode()).hexdigest(),'290f5ccda41cc9f90f5fbf4ef7cf6cd7bc5ba78ce7c9d9122e95ff216a178d70')
        self.assertEqual(export._EXPORT_DOCUMENT.count('value::text'),1)
        self.assertEqual(export._EXPORT_DOCUMENT.count('octet_length('),1)

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
               (COUNTS,{'total_bytes':maximum-1023}),
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
        database=Database(COUNTS,{'total_bytes':export._MAX_BYTES-1024},
                          {'total_bytes':export._MAX_BYTES,'encoded_value':encoded})
        with patch.object(export.json,'loads',wraps=json.loads) as decode:
            self.assertEqual(export.export_snapshot(database),{'folders':[],'note':'  α  '})
        decode.assert_called_once_with(encoded)
        self.assertEqual(database.events,['begin','commit'])
        self.assertEqual(len(database.operations),1)
        self.assertTrue(database.operations[0].read_only and database.operations[0].repeatable_read)
        self.assertEqual([s for s,p in database.queries],[export._EXPORT_CAPACITY,export._EXPORT_ESTIMATE,export._EXPORT_DOCUMENT])

if __name__=='__main__':unittest.main()
