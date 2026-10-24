"""Guard the shared proof used by legacy copy admission and schema migration."""
from pathlib import Path
import sys
import sqlite3
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.hot.legacy_source_provenance import LEGACY_PROVENANCE_CTE, legacy_update_copy_window_sql
from db.hot.update_copy_sources import UPDATE_COPY_SOURCE_ROWS, _UNPROVED, _source_facts
from db.base.postgres import _reviewed_statement

class LegacyProofContractTests(unittest.TestCase):
    def test_migration_embeds_the_same_proof_as_legacy_admission(self):
        step = (ROOT / "schema/steps/0027-data-source-provenance.sql").read_text(encoding="utf-8")
        embedded = step.split("-- BEGIN LEGACY_PROVENANCE_CTE\n", 1)[1].split("\n-- END LEGACY_PROVENANCE_CTE", 1)[0]
        self.assertEqual(embedded, LEGACY_PROVENANCE_CTE)
        guard = step.index("RAISE EXCEPTION 'legacy_source_provenance_unconfirmed'")
        self.assertLess(guard, step.index("UPDATE public.video_inventory"))

    def test_legacy_unavailable_details_are_removed_without_refreshing_other_data(self):
        # Execute the exact migration's portable UPDATE; PostgreSQL upgrade and
        # rollback acceptance is independently exercised by the native fixture.
        step = (ROOT / "schema/steps/0027-data-source-provenance.sql").read_text(encoding="utf-8")
        statement = step.split("-- BEGIN LEGACY_AVAILABILITY_REPAIR\n", 1)[1].split(
            "\n-- END LEGACY_AVAILABILITY_REPAIR", 1)[0]
        rows = [
            (1, 'unknown_unavailable', 'public', 'processed'),
            (2, 'unknown_unavailable', 'private', None),
            (3, 'unknown_unavailable', None, 'failed'),
            (4, 'unknown_unavailable', None, None),
            (5, 'public', 'public', 'processed'),
            (6, 'unlisted', 'unlisted', 'processed'),
            (7, 'private', 'private', 'processed'),
            (8, None, 'public', 'processed'),
            (9, 'unrecognized_legacy_status', 'public', 'processed'),
        ]
        with sqlite3.connect(':memory:') as database:
            database.execute("ATTACH DATABASE ':memory:' AS public")
            database.execute("CREATE TABLE public.videos(video_id INTEGER, availability_status TEXT, "
                             "privacy_status TEXT, upload_status TEXT, availability_checked_at TEXT, "
                             "metadata_checked_at TEXT, checked_at TEXT, title TEXT, views INTEGER)")
            source_facts = ('2026-10-14', '2026-09-30', '2026-10-13', 'retained metadata', 123)
            database.executemany('INSERT INTO public.videos VALUES(?,?,?,?,?,?,?,?,?)',
                                 [row + source_facts for row in rows])
            before = database.execute('SELECT * FROM public.videos ORDER BY video_id').fetchall()
            result = database.execute(statement)
            self.assertEqual(result.rowcount, 3)
            after = database.execute('SELECT * FROM public.videos ORDER BY video_id').fetchall()
            for old, actual in zip(before, after):
                expected = old[:2] + (None, None) + old[4:] if old[0] in (1, 2, 3) else old
                self.assertEqual(actual, expected)
            self.assertEqual(database.execute(statement).rowcount, 0)

    def test_schema26_reader_has_no_schema27_columns_and_preserves_other_guards(self):
        query = legacy_update_copy_window_sql()
        for column in ('published_acquired_at','typed_source_published_acquired_at',
                       'typed_source_stream_end_acquired_at','evidence_expires_at'):
            self.assertNotIn(column, query)
        self.assertIn(LEGACY_PROVENANCE_CTE, query)
        self.assertIn(_UNPROVED, query)
        replaced = {'inventory_publication','queue_publication','queue_stream_end','review_evidence'}
        for name, rows in UPDATE_COPY_SOURCE_ROWS.items():
            if name not in replaced:
                with self.subTest(source=name):
                    self.assertIn(_source_facts(name, rows), query)
        _reviewed_statement(query, read=True, max_rows=1)

    def test_legacy_review_deadline_is_compensated_once_and_keeps_expiry_boundary(self):
        rows = 'SELECT expires_at AS source_at,true AS inclusive FROM legacy_review_proof'
        facts = _source_facts('review_evidence', rows)
        self.assertIn(facts, legacy_update_copy_window_sql())
        self.assertIn("- interval '696 hours' AS oldest_source_at,true AS inclusive", facts)
        self.assertIn("(clock.at + interval '696 hours')", facts)
        self.assertNotIn('typed_collection_checked_at', LEGACY_PROVENANCE_CTE)

if __name__ == "__main__":
    unittest.main()
