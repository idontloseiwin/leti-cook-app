"""Baseline quantiles preserve exact counters and fractional values."""
from fractions import Fraction
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from channel_baseline import _value_summary


class ExactBaselineSummaries(unittest.TestCase):
    def test_fractional_sample_keeps_rounding_coverage_and_input_order(self):
        values = [Fraction(5, 3), Fraction(1, 3), Fraction(4, 3), Fraction(2, 3)]
        before = values.copy()
        self.assertEqual(_value_summary(values, sample_count=6), {
            'known_count': 4, 'missing_count': 2, 'total': 4, 'mean': 1,
            'median': 1, 'q1': 0.58, 'q3': 1.42, 'min': 0.33, 'max': 1.67,
        })
        self.assertEqual(values, before)

    def test_counters_above_json_integer_precision_are_exact(self):
        first = 2 ** 53
        result = _value_summary([first + 12, first, first + 8, first + 4], sample_count=4)
        self.assertEqual(result, {
            'known_count': 4, 'missing_count': 0,
            'total': str(4 * first + 24), 'mean': str(first + 6),
            'median': str(first + 6), 'q1': str(first + 3),
            'q3': str(first + 9), 'min': str(first), 'max': str(first + 12),
        })

    def test_empty_and_single_samples_keep_unknowns_and_quantiles(self):
        empty = _value_summary([], sample_count=3)
        self.assertEqual((empty['known_count'], empty['missing_count']), (0, 3))
        self.assertTrue(all(empty[key] is None for key in ('total', 'mean', 'median', 'q1', 'q3', 'min', 'max')))
        single = _value_summary([7], sample_count=1)
        self.assertTrue(all(single[key] == 7 for key in ('total', 'mean', 'median', 'q1', 'q3', 'min', 'max')))


if __name__ == '__main__':
    unittest.main()
