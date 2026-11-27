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
    ('application/db/jobs/operator_job_inventory_preparation.py', '28 days'): 1,  # videos published in the last 28 days
    ('application/db/retention/retention_queries.py', '720 hours'): 1,  # deleted views and collections: user work, 30 days
    ('application/db/user_work/user_work_restore_bind.py', '504 hours'): 1,  # restored channel metadata, unchanged
}
# Every use of the 29-day source window, per file. A literal added or dropped
# anywhere must be counted here, so each one is a deliberate use of the window.
WINDOW_USES = {
    'application/db/category_reference.py': 1,
    'application/db/channel/channel_control.py': 1,
    'application/db/channel/channel_inventory_estimate_normalization.py': 1,
    'application/db/hot/hot_collection_termination.py': 1,
    'application/db/hot/source_clocks.py': 1,
    # Copy admission has strict/inclusive 29-day comparisons, plus the
    # upper deadline bound and conversion back to acquisition time.
    'application/db/hot/update_copy_sources.py': 4,
    'application/db/inventory/automatic_type_policy.py': 4,
    'application/db/inventory/automatic_type_reconciliation.py': 2,
    'application/db/inventory/inventory_claims.py': 3,
    'application/db/inventory/inventory_review_events.py': 4,
    'application/db/inventory/inventory_runtime_queries.py': 2,
    'application/db/inventory/inventory_scan_clock.py': 3,
    'application/db/inventory/inventory_scan_pages.py': 2,
    'application/db/inventory/inventory_scan_steps.py': 1,
    'application/db/inventory/inventory_scans.py': 2,
    'application/db/inventory/manual_decision_inventory_effects.py': 1,
    'application/db/jobs/operator_job_enqueuing.py': 3,
    'application/db/jobs/operator_job_inventory_chain_proof.py': 1,
    'application/db/jobs/operator_job_inventory_evidence.py': 5,
    'application/db/jobs/operator_job_lifecycle.py': 8,
    'application/db/jobs/operator_job_retry.py': 1,
    'application/db/reads/channel_explorer.py': 1,
    # The removal date of a channel YouTube no longer returns (docs/DATA.md).
    'application/db/reads/frontend.py': 1,
    'application/db/retention/category_reference_retention.py': 1,
    'application/db/retention/current_source_retention.py': 1,
    'application/db/retention/deleted_channel_retention.py': 1,
    'application/db/retention/evidence_retention.py': 7,
    'application/db/retention/inventory_chain_retention.py': 1,
    'application/db/retention/inventory_copies_retention.py': 1,
    'application/db/retention/inventory_retention.py': 1,
    'application/db/retention/operational_retention.py': 1,
    'application/db/reviews/video_review_evidence.py': 3,
    'application/db/reviews/video_type_review_decisions.py': 1,
    'application/db/user_work/user_work_restore_bind.py': 1,
    # Each optional counter and the engagement report expire on the source window.
    'application/db/videos/engagement_observations.py': 6,
    # Addressed membership evidence expires on the same source window.
    'application/db/videos/typed_video_read_policy.py': 1,
    'application/db/videos/video_counter_completion.py': 1,
    'application/db/videos/video_identity_recovery.py': 2,
    'application/db/videos/video_initial_claims.py': 1,
    'application/db/videos/video_metadata_completion.py': 2,
    'application/db/videos/video_profile_completion.py': 1,
    'application/db/videos/video_queue_initialization.py': 2,
    # Reader views, retained predicates and deadlines of the schema.
    'application/db/user_work/alerts.py': 3,
    # Alert capture clamps an old start clock before subtracting the fallback
    # comparison window when the channel has no added_at.
    'schema/schema.sql': 151,
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
        window = {path: count for (path, literal), count in found.items() if literal == WINDOW}
        others = {key: count for key, count in found.items() if key[1] != WINDOW}
        self.assertEqual(others, OTHERS)
        self.assertEqual(window, WINDOW_USES)

    def test_other_copies_of_the_window_follow_it(self):
        from channel_inventory_estimate import COUNT_FRESH_HOURS
        self.assertEqual(COUNT_FRESH_HOURS, RETENTION_HOURS)
        collections = (ROOT / 'application/frontend/src/explorer/savedCollections.ts').read_text(encoding='utf-8')
        self.assertIn('export const SOURCE_RETENTION_MS = 29 * 86_400_000;', collections)
        self.assertEqual(HOT_DATA_WINDOW, timedelta(milliseconds=29 * 86_400_000))
        self.assertNotRegex(collections, r'28 \* 86_?400_?000')


if __name__ == '__main__':
    unittest.main()
