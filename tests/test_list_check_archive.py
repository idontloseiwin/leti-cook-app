from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from ops.list_check_report import archive_week, previous_week_end, timestamp


class WeeklyAggregate(unittest.TestCase):
    def test_window_and_atomic_retry(self):
        end = previous_week_end(datetime(2026, 11, 12, 15, tzinfo=timezone.utc))
        self.assertEqual(end, timestamp('2026-11-09T00:00:00Z'))
        start = end-timedelta(days=7)
        report = dict(version=1, start=start.isoformat(), end_exclusive=end.isoformat(), groups=[])
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = archive_week(root, report)
            archive_week(root, dict(report, groups=[{'later': 1}]))
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), report)
            self.assertEqual(list(root.iterdir()), [path])
            path.write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'conflict'):
                archive_week(root, report)
            with self.assertRaisesRegex(ValueError, 'invalid'):
                archive_week(root, dict(report, start=end.isoformat()))
