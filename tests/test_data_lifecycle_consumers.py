"""Source-pair consumers: pending copies, deadlines and fully scrubbed chains."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.hot.source_clocks import SOURCE_CLOCKS, STARTUP_EXPIRY_SOURCES
from db.hot import update_copy_sources as copies
from db.retention import evidence_retention as evidence
from db.retention import inventory_retention as inventory
from db.retention import inventory_chain_retention as chains


class SourcePairConsumers(unittest.TestCase):
    def test_pending_queue_copies_have_separate_acquisition_clocks(self):
        for name, value, clock in (
            ('inventory_publication', 'published_at', 'published_acquired_at'),
            ('queue_publication', 'typed_source_published_at', 'typed_source_published_acquired_at'),
            ('queue_stream_end', 'typed_stream_actual_end_at', 'typed_source_stream_end_acquired_at'),
        ):
            with self.subTest(source=name):
                spec = SOURCE_CLOCKS[name]
                self.assertEqual(spec.expression, clock)
                self.assertEqual(spec.payload, value + ' IS NOT NULL')
                self.assertIn(clock, copies.UPDATE_COPY_SOURCE_ROWS[name])
                self.assertNotIn('typed_collection_checked_at', copies.UPDATE_COPY_SOURCE_ROWS[name])
        self.assertNotIn('queue_api_copies', SOURCE_CLOCKS)
        self.assertNotIn('i.published_at IS NOT NULL', copies._UNPROVED)

    def test_review_deadline_is_not_local_last_seen(self):
        spec = SOURCE_CLOCKS['review_evidence']
        self.assertTrue(spec.expires)
        self.assertEqual(spec.expression, 'evidence_expires_at')
        query = STARTUP_EXPIRY_SOURCES['review_evidence']
        self.assertIn("clock.cutoff + interval '696 hours'", query)
        self.assertNotIn('last_seen_at', query)
        facts = copies._source_facts('review_evidence', copies.UPDATE_COPY_SOURCE_ROWS['review_evidence'])
        self.assertIn('ORDER BY source_at LIMIT 1', facts)
        self.assertIn("- interval '696 hours'", facts)
        self.assertIn("clock.at + interval '696 hours'", facts)
        spec = evidence._TARGETS['review_evidence']
        self.assertEqual(spec.clock, 'evidence_expires_at')
        self.assertNotIn('last_seen_at', spec.predicate)
        self.assertIn('<=%(now)s::timestamptz', spec.predicate)

    def test_cleanup_clears_the_pair_and_preserves_other_valid_queue_copy(self):
        self.assertIn('published_acquired_at', inventory._PUBLICATION)
        self.assertNotIn('video_inventory_memberships', inventory._PUBLICATION)
        self.assertIn('typed_source_published_acquired_at', inventory._QUEUE_NOOP)
        self.assertIn('typed_source_stream_end_acquired_at', inventory._QUEUE_NOOP)
        self.assertNotIn('typed_collection_checked_at', inventory._QUEUE_NOOP)
        sql = inventory._QUEUE_SCRUB
        self.assertIn('typed_source_published_at=CASE', sql)
        self.assertIn('typed_stream_actual_end_at=CASE', sql)
        self.assertIn('typed_source_published_acquired_at=CASE', sql)
        self.assertIn('typed_source_stream_end_acquired_at=CASE', sql)
        self.assertIn('published_acquired_at=NULL', inventory._WITHDRAW_TYPE_EFFECT)
        self.assertIn('typed_source_published_acquired_at=NULL', inventory._RECOVERY_QUEUE)
        self.assertIn('typed_source_stream_end_acquired_at=NULL', inventory._RECOVERY_QUEUE)

    def test_review_scrub_keeps_local_timestamps_but_removes_deadline(self):
        tx = Mock()
        evidence._mutate(tx, evidence._TARGETS['review_evidence'], 'review_evidence',
                         {'case_id': '00000000-0000-4000-8000-000000000001'}, '2026-10-01', '2026-10-30')
        sql = tx.execute.call_args.args[0]
        self.assertIn('evidence_expires_at=NULL', sql)
        self.assertNotIn('last_seen_at=', sql)
        self.assertNotIn('first_seen_at=', sql)

    def test_marker_alone_never_exempts_parent_children_or_targets(self):
        contract = chains.SOURCE_FREE_JOB_SQL
        for required in ('source.resume_page_token IS NULL', 'source.inventory_scan_id IS NULL',
                         "source.inventory_checkpoint='{}'::jsonb", 'operator_job_targets',
                         'operator_job_inventory_chains', "child.checkpoint='{}'::jsonb",
                         "child.last_ack='{}'::jsonb", 'child.terminal_outcome IS NULL',
                         'child.channel_id=source.channel_id', 'count(*)=3'):
            self.assertIn(required, contract)
        self.assertIn(contract, SOURCE_CLOCKS['operator_jobs_and_targets'].payload)
        self.assertIn('operator_chain_linked_scans', SOURCE_CLOCKS)
        self.assertIn('operator_chain_ack_receipts', SOURCE_CLOCKS)
        self.assertIn('LEFT JOIN', copies.UPDATE_COPY_SOURCE_ROWS['operator_chain_linked_scans'])
        self.assertIn('LEFT JOIN', copies.UPDATE_COPY_SOURCE_ROWS['operator_chain_ack_receipts'])


if __name__ == '__main__':
    unittest.main()
