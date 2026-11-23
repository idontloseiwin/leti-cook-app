"""Native JSONB summary projection never converts counters to binary floats."""
from pathlib import Path
import json,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.runs.sync_run_summary import persisted_summary_text,persisted_summary_copy

class NativeSummaryTests(unittest.TestCase):
    def test_numeric_tokens_survive_at_extreme_range_and_fractional_scale(self):
        for token in ('0.'+'0'*399+'1','1'+'0'*5000,'-0.0000','123.4500','1e1000'):
            raw='{"processed":'+token+',"title":"provider title"}'
            self.assertEqual(persisted_summary_text(raw),'{"processed":'+token+'}')
    def test_native_number_is_not_a_label_and_numeric_strings_stay_filtered(self):
        raw='{"processed":"123","duration_ms":123,"mode":"running","errors":false,"requested":null}'
        self.assertEqual(json.loads(persisted_summary_text(raw)),dict(duration_ms=123,mode='running',errors=False,requested=None))
    def test_existing_storage_policy_is_shared_for_ordinary_wire_values(self):
        for value in (None,[],True,5,{},dict(processed=8,yield_requested=3,mode='provider_text'),dict(reported_values={'processed':3.5},video_ids=['secret'],timings={'duration_ms':12})):
            self.assertEqual(json.loads(persisted_summary_text(json.dumps(value))),persisted_summary_copy(value))
    def test_depth_and_unknown_sections_match_retained_projection(self):
        raw='{"timings":{"timings":{"timings":{"processed":1,"timings":{"processed":2}}}},"provider":{"processed":9}}'
        self.assertEqual(json.loads(persisted_summary_text(raw)),dict(timings=dict(timings=dict(timings=dict(processed=1)))))
    def test_sql_null_json_null_empty_objects_and_invalid_json(self):
        for raw in (None,'null','{}','[]','1','"running"'):self.assertEqual(persisted_summary_text(raw),'{}')
        for raw in ('{"processed":NaN}','{"processed":Infinity}','broken'):
            with self.assertRaises(ValueError):persisted_summary_text(raw)
        with self.assertRaises(ValueError):persisted_summary_text({})

if __name__=='__main__':unittest.main()
