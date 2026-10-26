"""Source data is kept 29 days; analytics compare over 28. Every day-scale SQL interval is accounted for. No database."""
from datetime import timedelta
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from collection_schedule import MAX_INTERVAL_SECONDS  # noqa: E402
from db.hot.hot_retention import DEFAULT_RETENTION_WINDOW  # noqa: E402
from hot_storage import ANALYTICS_WINDOW, HOT_DATA_WINDOW, RETENTION_HOURS, STARTUP_CATCHUP_GRACE_HOURS  # noqa: E402

INTERVAL = re.compile(r"interval '(\d+) (hours?|days?)'")
# pg_dump prints view intervals as 'H:MM:SS'::interval.
DUMPED = re.compile(r"'(\d+):00:00'::interval")
WINDOW = f'{RETENTION_HOURS} hours'
# Day-scale intervals that are not the retention window, with their meaning.
OTHERS = {
    ('application/db/reads/analytics_read_queries.py', '672 hours'): 2,  # 28-day growth comparison
    ('application/db/jobs/operator_job_inventory_preparation.py', '28 days'): 1,  # videos published in the last 28 days
    ('application/db/retention/retention_queries.py', '720 hours'): 1,  # deleted views and collections: user work, 30 days
    ('application/db/user_work/user_work_restore_bind.py', '504 hours'): 1,  # restored channel metadata, unchanged
}


def day_scale_intervals():
    found = {}
    for path in sorted([*ROOT.glob('application/**/*.py'), ROOT / 'schema/schema.sql']):
        if 'node_modules' in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding='utf-8')
        literals = [(int(number) * (24 if unit.startswith('day') else 1), f'{number} {unit}')
                    for number, unit in INTERVAL.findall(text)]
        literals += [(int(hours), f'{hours} hours') for hours in DUMPED.findall(text)]
        for hours, literal in literals:
            if hours >= 7 * 24:
                key = (relative, literal)
                found[key] = found.get(key, 0) + 1
    return found


class RetentionWindowTests(unittest.TestCase):
    def test_source_data_is_kept_29_days_inside_youtubes_30(self):
        self.assertEqual(HOT_DATA_WINDOW, timedelta(days=29))
        self.assertEqual(HOT_DATA_WINDOW, timedelta(hours=RETENTION_HOURS))
        self.assertEqual(DEFAULT_RETENTION_WINDOW, HOT_DATA_WINDOW)
        self.assertLess(HOT_DATA_WINDOW, timedelta(days=30))

    def test_startup_catchup_grace_is_one_hour_without_extending_source_lifetime(self):
        self.assertIs(type(STARTUP_CATCHUP_GRACE_HOURS), int)
        self.assertEqual(STARTUP_CATCHUP_GRACE_HOURS, 1)
        self.assertEqual(HOT_DATA_WINDOW, timedelta(hours=RETENTION_HOURS))
        self.assertLess(HOT_DATA_WINDOW + timedelta(hours=STARTUP_CATCHUP_GRACE_HOURS), timedelta(days=30))

    def test_analytics_stay_28_days_inside_the_window(self):
        self.assertEqual(ANALYTICS_WINDOW, timedelta(days=28))
        self.assertLess(ANALYTICS_WINDOW, HOT_DATA_WINDOW)

    def test_maximum_refresh_leaves_one_day_before_source_expiry(self):
        self.assertEqual(timedelta(seconds=MAX_INTERVAL_SECONDS), timedelta(days=28))
        self.assertEqual(HOT_DATA_WINDOW - timedelta(seconds=MAX_INTERVAL_SECONDS), timedelta(days=1))

    def test_every_day_scale_sql_interval_is_the_window_or_named(self):
        found = day_scale_intervals()
        window = {key: count for key, count in found.items() if key[1] == WINDOW}
        others = {key: count for key, count in found.items() if key[1] != WINDOW}
        self.assertEqual(others, OTHERS)
        # Copy admission has strict/inclusive 29-day comparisons, plus the
        # upper deadline bound and conversion back to acquisition time.
        copy_count = window.pop(('application/db/hot/update_copy_sources.py', WINDOW))
        self.assertEqual(copy_count, 4)
        # Accepted source-clock, RR03 and inventory-chain deltas: account for
        # each location explicitly, retaining the prior 139-expression guard.
        additions = {
            'application/db/jobs/operator_job_inventory_chain_proof.py': (1, 1),
            'application/db/jobs/operator_job_lifecycle.py': (8, 2),
            'application/db/reads/channel_explorer.py': (1, 1),
            'application/db/retention/inventory_chain_retention.py': (1, 1),
            'application/db/videos/video_metadata_completion.py': (2, 1),
            # Acquisition/deadline contract: each new literal is named and
            # counted; the original 139-expression baseline stays unchanged.
            'application/db/hot/legacy_source_provenance.py': (3, 3),
            'application/db/hot/source_clocks.py': (1, 1),
            'application/db/inventory/manual_decision_inventory_effects.py': (1, 1),
            'application/db/retention/evidence_retention.py': (6, 5),
            'application/db/reviews/video_review_evidence.py': (3, 1),
            'application/db/reviews/video_type_review_decisions.py': (1, 1),
            # Schema29: automatic proof (4), public hot inventory (2), readable
            # hot inventory (4), readable type resolution (2), review proof (5).
            # These17 plus schema27's5 leave the original77 schema literals.
            # Schema31 replaces4 reader scopes: videos(4), two hot inventories(6),
            # and hydration proof(4). All14 retain the same29-day bounds.
            # Schema37 restates type resolution with the membership view's
            # admission inline (8) and hot inventory (4): the same bounds.
            'schema/schema.sql': (125, 48),
            'application/db/inventory/automatic_type_policy.py': (4, 4),
            'application/db/inventory/automatic_type_reconciliation.py': (2, 2),
            'application/db/videos/video_queue_initialization.py': (2, 2),
        }
        for path, (current, added) in additions.items():
            self.assertEqual(window[(path, WINDOW)], current, path)
            window[(path, WINDOW)] -= added
        self.assertEqual(sum(window.values()), 139)
        self.assertEqual(window[('schema/schema.sql', WINDOW)], 77)  # Retained predicates plus reader-view bounds and deadlines.

    def test_other_copies_of_the_window_follow_it(self):
        from channel_inventory_estimate import COUNT_FRESH_HOURS
        self.assertEqual(COUNT_FRESH_HOURS, RETENTION_HOURS)
        collections = (ROOT / 'application/frontend/src/explorer/savedCollections.ts').read_text(encoding='utf-8')
        self.assertIn('export const SOURCE_RETENTION_MS = 29 * 86_400_000;', collections)
        self.assertEqual(HOT_DATA_WINDOW, timedelta(milliseconds=29 * 86_400_000))
        self.assertNotRegex(collections, r'28 \* 86_?400_?000')


if __name__ == '__main__':
    unittest.main()
