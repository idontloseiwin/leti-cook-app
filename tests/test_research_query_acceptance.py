"""The manual acceptance report must not turn missing/failed samples into a pass."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from research_query_acceptance import (COMPARISON_ARMS, DIAGNOSTIC_ARMS, WRITE_MIN_SAMPLES, CountingRepository,
                                       arm_order, assess, stable_answer, verify_generated, write_impact)
from research_query_benchmark import TARGETS, NOW


def write_samples(medians, n):
    return [{'arm': arm, 'write_ms': ms, 'phases': {'commit_release_ms': ms / 3}}
            for arm, ms in medians.items() for _ in range(n)]


class AcceptanceEvidenceTests(unittest.TestCase):
    def report(self, samples, *, medians=None, n=WRITE_MIN_SAMPLES, errors=(), failures=0, diagnostic=None):
        comparison = write_samples(medians or {'background': 2, 'reads': 3}, n)
        return {'size': 'M', 'targets': TARGETS, 'samples': samples,
                'writes': {'comparison': {'shared': write_impact(comparison, COMPARISON_ARMS)},
                           'diagnostic': write_impact(write_samples(diagnostic or {'refresher_off': 2, 'refresher_on': 2}, n),
                                                      DIAGNOSTIC_ARMS),
                           'refresher_errors': list(errors), 'refresher_work': {'failures': failures}}}

    def test_sparse_samples_cannot_claim_latency_pass(self):
        result = assess(self.report([{'case': 'folder_long_warm', 'http_ms': 1, 'status': 200}]))
        self.assertIsNone(result['warm_http_pass'])
        self.assertFalse(result['write_ratio_pass'])

    def test_write_gate_needs_enough_alternated_samples_in_both_arms(self):
        self.assertIsNone(assess(self.report([], n=WRITE_MIN_SAMPLES - 1))['write_ratio_pass'])
        self.assertIsNone(assess(self.report([], medians={'background': 2}))['write_ratio_pass'])
        self.assertTrue(assess(self.report([], medians={'background': 4, 'reads': 4.4}))['write_ratio_pass'])
        self.assertIsNone(assess({'size': 'M', 'targets': TARGETS, 'samples': []})['write_ratio_pass'])

    def test_gate_compares_reads_with_background_and_the_diagnostic_stays_apart(self):
        # A costly refresher in the diagnostic does not touch the gate.
        result = assess(self.report([], medians={'background': 4, 'reads': 4.4},
                                    diagnostic={'refresher_off': 2, 'refresher_on': 4}))
        self.assertEqual(result['write_median_regression_ratio'], 1.1)
        self.assertTrue(result['write_ratio_pass'])
        self.assertEqual(result['refresher_impact_ratio'], 2.0)

    def test_refresher_failures_during_blocks_fail_the_refresher_check(self):
        self.assertTrue(assess(self.report([]))['refresher_pass'])
        self.assertFalse(assess(self.report([], errors=['TimeoutError']))['refresher_pass'])
        self.assertFalse(assess(self.report([], failures=1))['refresher_pass'])

    def test_arms_alternate_and_lead_in_turn(self):
        self.assertEqual([arm_order(COMPARISON_ARMS, r) for r in range(3)],
                         [['background', 'reads'], ['reads', 'background'], ['background', 'reads']])
        self.assertEqual(arm_order(DIAGNOSTIC_ARMS, 1), ['refresher_on', 'refresher_off'])

    def test_counting_repository_records_real_batches_and_failures(self):
        class Repository:
            results = [(3, 4), ValueError('offline'), (0, 1)]
            def refresh_counts(self, limit):
                result = self.results.pop(0)
                if isinstance(result, Exception): raise result
                return result
        counting = CountingRepository(Repository())
        self.assertEqual(counting.refresh_counts(10), (3, 4))
        with self.assertRaises(ValueError): counting.refresh_counts(10)
        counting.refresh_counts(10)
        self.assertEqual(counting.snapshot(), {'batches': 2, 'markers_taken': 3, 'rows_refreshed': 5, 'failures': 1})

    def test_slow_or_failed_requests_cannot_be_counted_as_fast_success(self):
        samples = [{'case': 'folder_long_warm', 'http_ms': 2600, 'status': 200} for _ in range(20)]
        samples.append({'case': 'folder_long_warm', 'http_ms': 1, 'status': 503})
        result = assess(self.report(samples))
        self.assertFalse(result['warm_http_pass'])
        self.assertEqual(result['warm_http']['n'], 20)

    def test_stable_comparison_keeps_cells_clocks_coverage_and_provenance(self):
        original = {'columns': ['video.views'], 'rows': [{'id': 'example', 'cells': {'video.views':
                    {'value': '0', 'state': 'known', 'observed_at': '2026-10-25T00:00:00Z'}}}],
                    'provenance': {'video.views': {'origin': 'youtube'}}, 'coverage': {'status': 'complete'},
                    'page': {'has_more': False}, 'context': {'evaluated_at': 'changes on each read'}}
        for key, value in [('value', None), ('state', 'unknown'), ('observed_at', None)]:
            changed = deepcopy(original)
            changed['rows'][0]['cells']['video.views'][key] = value
            self.assertNotEqual(stable_answer(original), stable_answer(changed))

    def test_independent_validator_rejects_missing_rows(self):
        with self.assertRaisesRegex(AssertionError, 'independent_order_mismatch'):
            verify_generated({'rows': []}, ['z0000000010'], 10, NOW)


if __name__ == '__main__':
    unittest.main()
