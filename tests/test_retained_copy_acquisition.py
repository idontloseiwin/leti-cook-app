"""Acquisition clocks follow copied API values, never queue result time."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
from db.inventory import inventory_scan_pages as pages, inventory_review_events as restoration
from db.videos import video_queue_initialization as initialization
from db.videos import video_initial_claims as initial, video_queue_maintenance as priority
from db.videos import typed_video_read_policy as candidate, typed_reconciliation_schedule as reconciliation
from db.videos import video_typed_metadata_completion as metadata, video_missing_completion as missing
from db.videos import video_stream_completion as lifecycle
from db.reviews import video_type_review_decisions as manual
from db.base.postgres import _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer


class RetainedAcquisitionTests(unittest.TestCase):
    def test_pending_insert_carries_origin_without_claiming_hydration(self):
        self.assertIn("published_acquired_at", pages._INSERT_CANONICAL)
        self.assertIn("typed_source_published_acquired_at", initialization._TYPED_INSERT)
        self.assertIn("candidate.source_published_acquired_at", initialization._TYPED_INSERT)
        self.assertNotIn("typed_collection_checked_at", initialization._TYPED_INSERT)
        self.assertIn("typed_source_published_acquired_at", initial._INSERT)
        self.assertNotIn("typed_collection_checked_at", initial._INSERT)

    def test_candidate_clock_uses_the_source_of_selected_publication(self):
        self.assertIn("m.last_present_at AS member_published_acquired_at", candidate._NATIVE_FACTS)
        self.assertIn("i.published_acquired_at AS inventory_published_acquired_at", candidate._NATIVE_FACTS)
        self.assertIn("source_published_acquired_at", candidate.TYPED_CANDIDATE_INPUT_ID_SQL)
        self.assertIn("facts.member_published_at IS NOT NULL", candidate.TYPED_CANDIDATE_INPUT_ID_SQL)

    def test_manual_sources_have_independent_ages(self):
        self.assertIn("i.published_acquired_at", manual._QUEUE_SOURCE)
        self.assertIn("v.metadata_checked_at", manual._QUEUE_SOURCE)
        self.assertIn("published_acquired_at", manual._QUEUE_UPDATE)
        self.assertIn("stream_end_acquired_at", manual._QUEUE_UPDATE)
        self.assertNotIn("decided_at", manual._QUEUE_SOURCE)

    def test_metadata_sets_both_origins_but_missing_and_live_do_not(self):
        for name in ("typed_source_published_acquired_at", "typed_source_stream_end_acquired_at"):
            self.assertIn(name, metadata._COMPLETE)
            self.assertNotIn(name, missing._COMPLETE)
            self.assertNotIn(name, lifecycle._COMPLETE)
        self.assertIn("coalesce(input.metadata_observed_at,input.recorded_at)", metadata._COMPLETE)

    def test_missing_response_clears_unconfirmed_status_values_for_all_types(self):
        self.assertIn("privacy_status=NULL", missing._VIDEO)
        self.assertIn("upload_status=NULL", missing._VIDEO)
        self.assertNotIn("ELSE video.privacy_status", missing._VIDEO)
        self.assertNotIn("ELSE video.upload_status", missing._VIDEO)

    def test_restore_reconcile_and_priority_carry_origins(self):
        self.assertIn("published_acquired_at", restoration._RESTORE)
        self.assertIn("incoming.last_present_at", restoration._RESTORE)
        self.assertIn("typed_source_published_acquired_at", reconciliation._FINALIZE)
        self.assertIn("typed_source_published_acquired_at", priority._PRIORITIZE_TYPED)

    def test_changed_statements_still_pass_driver_and_parameter_binding(self):
        statements = (pages._INSERT_CANONICAL, restoration._RESTORE,
            initialization._TYPED_INSERT, initial._INSERT, priority._PRIORITIZE_TYPED,
            manual._QUEUE_UPDATE, metadata._COMPLETE, missing._VIDEO, reconciliation._FINALIZE)
        for statement in statements:
            with self.subTest(statement=statement[:65]):
                named = re.findall(r"%\(([^)]+)\)s", statement)
                params = {name: None for name in named} if named else (None,) * statement.count("%s")
                returning = "RETURNING" in statement.upper()
                checked = _reviewed_statement(statement, read=returning,
                    returning=returning, max_rows=50 if returning else 0)
                PostgresQuery(Transformer()).convert(checked.as_string(), params)


if __name__ == "__main__":
    unittest.main()
