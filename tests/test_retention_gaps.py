"""Deleted saved-object tombstones expire after 30 days and never block a restore. Source only: no database."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.hot.hot_retention import CleanupPage, RetentionError, TARGET_FUNCTIONS  # noqa: E402

SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')

def page(days, **extra):
    now=datetime(2026,9,25,12,tzinfo=timezone.utc)
    value={'as_of':now.isoformat(),'cutoff':(now-timedelta(days=days)).isoformat(),'scanned':1,'deleted':1,'scrubbed':0,
           'deferred':0,'complete':True,'next_cursor':None,'oldest_remaining_expired_source_at':None,'invalid_clock_remaining':0}
    value.update(extra)
    return value

class TombstoneCleanupTests(unittest.TestCase):
    def test_cleanup_is_a_registered_maintenance_target(self):
        self.assertEqual(TARGET_FUNCTIONS['deleted_saved_objects'],'cleanup_deleted_saved_objects')
        self.assertIn("'cleanup_deleted_saved_objects',",(ROOT/'application/hot_runtime.py').read_text(encoding='utf-8'))
        self.assertNotIn('CREATE FUNCTION public.cleanup_deleted_saved_objects(',SCHEMA)
        from db.retention import retention_queries
        self.assertTrue(callable(retention_queries.cleanup_deleted_saved_objects))

    def test_window_is_thirty_days_for_tombstones_only(self):
        self.assertEqual(CleanupPage.parse('deleted_saved_objects',page(30),100).deleted,1)
        with self.assertRaises(RetentionError):
            CleanupPage.parse('deleted_saved_objects',page(28),100)
        with self.assertRaises(RetentionError):
            CleanupPage.parse('sync_runs',page(30),100)

if __name__=='__main__':unittest.main()
