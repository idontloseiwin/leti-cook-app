"""API-derived copies stay inside the 28-day window without scrubbing fresh evidence. Source only: no database."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]
SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')

class ReviewEvidenceTests(unittest.TestCase):
    def test_only_evidence_outside_the_window_is_scrubbed(self):
        from db.retention.evidence_retention import _TARGETS
        spec=_TARGETS['review_evidence']
        self.assertEqual(spec.clock,'evidence_expires_at')
        self.assertTrue(spec.expires)
        self.assertEqual(spec.predicate,
            "(t.evidence_expires_at IS NULL OR NOT isfinite(t.evidence_expires_at) "
            "OR t.evidence_expires_at<=%(now)s::timestamptz "
            "OR t.evidence_expires_at>%(now)s::timestamptz+interval '696 hours') AND "
            "(t.observed_canonical_type IS NOT NULL OR cardinality(t.observed_types)>0 "
            "OR cardinality(t.observed_channels)>0 OR t.blocked_reason IS NOT NULL)")

class PauseReasonTests(unittest.TestCase):
    def test_a_paused_video_keeps_no_copy_of_its_status(self):
        from db.videos import video_queue_initialization
        source=(ROOT/'application/db/videos/video_queue_initialization.py').read_text(encoding='utf-8')
        self.assertNotIn("'availability:'",source)
        self.assertIn('availability_unsupported',source)

class RetiredPageTokenTests(unittest.TestCase):
    def test_the_repair_cursor_can_no_longer_be_stored(self):
        self.assertNotIn('    deep_inventory_page_token text,',SCHEMA)
        self.assertNotIn('CONSTRAINT channels_deep_inventory_page_token_retired_check',SCHEMA)
        self.assertIn('    NULL::text AS deep_inventory_page_token,',SCHEMA)
        for path in (ROOT/'application').rglob('*.py'):
            source=path.read_text(encoding='utf-8')
            if path==ROOT/'application/db/channel/channel_discovery_projection.py':
                # The retired public read field is always literal SQL NULL;
                # this exact projection is not a persisted provider token.
                null_projection="'deep_inventory_page_token': 'NULL::text',"
                self.assertEqual(source.count(null_projection),1)
                source=source.replace(null_projection,'')
            self.assertIsNone(re.search(r'["\']deep_inventory_page_token["\']\s*:',source),path.name)

if __name__=='__main__':unittest.main()
