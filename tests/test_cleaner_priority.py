"""Cleaner priority is independent from row-local read protection. No database."""
import unittest

from test_runtime_gates import DAY, Clocks, Repository, iso, step
from hot_runtime import HotRuntimeController, PAUSE_AFTER_PASSES
from hot_retention import HotCleanupCycle
from db.hot.hot_retention import CleanupPage, RetentionError

AS_OF, CUTOFF = '2026-10-06T00:00:00+00:00', '2026-09-07T00:00:00+00:00'  # 29 days


def raw_page(**changes):
    return {'as_of': AS_OF, 'cutoff': CUTOFF, 'scanned': 1, 'deleted': 0, 'scrubbed': 0, 'deferred': 1,
            'complete': True, 'next_cursor': None, **changes}


class Cycle:
    """Each pass has `pages` steps; `passes` lists the findings of each pass (the last repeats)."""
    def __init__(self, clocks, pages, passes):
        self.clocks, self.pages, self.passes, self.done, self.pass_index = clocks, pages, passes, 0, 0

    def findings(self):
        return self.passes[min(self.pass_index, len(self.passes) - 1)]

    def summary(self):
        return {'status': 'in_progress', 'batches': self.done}

    def reset(self):
        self.done = 0
        self.pass_index += 1

    def run_slice(self, **kw):
        self.done += 1
        found = self.findings()
        return {'status': 'scan_exhausted' if self.done >= self.pages else 'in_progress', 'batches': self.done,
                'counts': {'scanned': 1, 'deleted': 0, 'scrubbed': 0, 'deferred': found.get('deferred', 0)},
                'targets_not_visited': [], 'targets_pending': [], 'targets_with_observed_deferrals': [],
                'invalid_clocks_observed': found.get('invalid', 0),
                'unknown_expiry_observed': found.get('unknown', 0),
                'oldest_observed_expired_source_at': found.get('expired') and iso(self.clocks.wall - found['expired']),
                'pages': [{'as_of': iso(self.clocks.wall)}]}


def runtime(*passes, pages=3):
    clocks = Clocks()
    clocks.wall = 1_800_000_000.0  # far enough from 1970 for clocks a month back
    controller = HotRuntimeController(Repository(clocks), local_cleanup=lambda: {'status': 'current'},
                                      clear_caches=lambda: None, clock=lambda: clocks.wall, monotonic=lambda: clocks.mono)
    controller.cycle = Cycle(clocks, pages, list(passes))
    return controller, clocks


def next_pass(controller, clocks):
    """Tick once a second, as the runtime loop does, until the current or next pass ends."""
    start = controller.cycle.pass_index
    for _ in range(200):
        clocks.advance(1)
        controller.step()
        if controller.cycle.pass_index > start:
            return controller.status()
    raise AssertionError('pass did not end')


class CleanerPriorityTests(unittest.TestCase):
    def test_busy_fresh_rows_do_not_withhold_reads(self):
        controller, clocks = runtime({'deferred': 7})
        status = next_pass(controller, clocks)
        self.assertEqual(status['status'], 'ready')
        self.assertFalse(status['collection_pause_requested'])
        self.assertEqual(controller._next_cycle, clocks.mono + 5)  # skipped rows are checked again soon

    def test_skipped_rows_use_row_local_read_protection(self):
        for findings in ({'deferred': 1, 'expired': 29 * DAY + 60}, {'deferred': 1, 'unknown': 1}, {'deferred': 1, 'invalid': 1}):
            with self.subTest(findings=findings):
                controller, clocks = runtime({}, findings)
                self.assertEqual(next_pass(controller, clocks)['status'], 'ready')
                self.assertEqual(next_pass(controller, clocks)['status'], 'ready')
                self.assertEqual(controller.status()['scan']['unknown_expiry_observed'], findings.get('unknown', 0))

    def test_unknown_age_does_not_block_reads(self):
        controller, clocks = runtime({'deferred': 1, 'unknown': 1}, {})
        self.assertEqual(next_pass(controller, clocks)['status'], 'ready')
        self.assertEqual(next_pass(controller, clocks)['status'], 'ready')

    def test_an_expired_row_skipped_pass_after_pass_asks_collection_to_pause(self):
        controller, clocks = runtime({'deferred': 1, 'expired': 29 * DAY + 60}, {'deferred': 1, 'expired': 29 * DAY + 60},
                                     {'deferred': 1, 'expired': 29 * DAY + 60}, {})
        statuses = [next_pass(controller, clocks) for _ in range(PAUSE_AFTER_PASSES + 1)]
        self.assertEqual([s['collection_pause_requested'] for s in statuses], [False, False, True, False])
        self.assertEqual(statuses[-1]['status'], 'ready')

    def test_data_half_a_day_from_youtubes_limit_asks_for_a_pause_at_once(self):
        controller, clocks = runtime({'deferred': 1, 'expired': 29 * DAY + DAY // 2})
        status = step(controller, clocks)
        self.assertEqual(status['status'], 'ready')
        self.assertTrue(status['collection_pause_requested'])


class UnknownAgeReportTests(unittest.TestCase):
    def test_a_page_carries_its_unknown_age_count(self):
        self.assertEqual(CleanupPage.parse('inventory', raw_page(unknown_expiry_remaining=2), 100).unknown_expiry_remaining, 2)
        self.assertEqual(CleanupPage.parse('inventory', raw_page(), 100).unknown_expiry_remaining, 0)
        self.assertEqual(CleanupPage.parse('inventory', raw_page(unknown_expiry_remaining=2), 100).summary()['unknown_expiry_remaining'], 2)
        for bad in (-1, True, '2'):
            with self.subTest(bad=bad), self.assertRaises(RetentionError):
                CleanupPage.parse('inventory', raw_page(unknown_expiry_remaining=bad), 100)

    def test_the_cycle_adds_them_up(self):
        repository = type('Pages', (), {'cleanup_page': lambda self, target, after_id=None, limit=100:
                                        CleanupPage.parse(target, raw_page(unknown_expiry_remaining=2), limit)})()
        cycle = HotCleanupCycle(repository, targets=('inventory', 'deleted_channels'))
        summary = cycle.run_slice(max_batches=2)
        self.assertEqual(summary['unknown_expiry_observed'], 4)
        cycle.reset()
        self.assertEqual(cycle.summary()['unknown_expiry_observed'], 0)


if __name__ == '__main__':
    unittest.main()
