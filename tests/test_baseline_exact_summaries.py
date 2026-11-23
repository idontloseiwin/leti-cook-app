"""Numeric summary examples covering fractional values, large integers and missing values."""
from fractions import Fraction
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from research_query.operation_kinds.summary import round_metric

FIRST = 2 ** 53
KEYS = ('known_count', 'missing_count', 'total', 'mean', 'median', 'q1', 'q3', 'min', 'max')
# name -> (values, sample count, _value_summary of them). Fractions enter SQL as 40-digit numerics.
CASES = {
    'fractional_sample': ([Fraction(5, 3), Fraction(1, 3), Fraction(4, 3), Fraction(2, 3)], 6, {
        'known_count': 4, 'missing_count': 2, 'total': 4, 'mean': 1,
        'median': 1, 'q1': 0.58, 'q3': 1.42, 'min': 0.33, 'max': 1.67}),
    'counters_above_json_integer_precision': ([FIRST + 12, FIRST, FIRST + 8, FIRST + 4], 4, {
        'known_count': 4, 'missing_count': 0,
        'total': str(4 * FIRST + 24), 'mean': str(FIRST + 6),
        'median': str(FIRST + 6), 'q1': str(FIRST + 3),
        'q3': str(FIRST + 9), 'min': str(FIRST), 'max': str(FIRST + 12)}),
    'fractional_result_below_safe_integer_limit': ([4503599627370507, 4503599627370508], 2, {
        'known_count': 2, 'missing_count': 0, 'total': '9007199254741015', 'mean': '4503599627370507.5',
        'median': '4503599627370507.5', 'q1': '4503599627370507.25', 'q3': '4503599627370507.75',
        'min': 4503599627370507, 'max': 4503599627370508}),
    'empty': ([], 3, {'known_count': 0, 'missing_count': 3, 'total': None, 'mean': None,
                      'median': None, 'q1': None, 'q3': None, 'min': None, 'max': None}),
    'single': ([7], 1, {'known_count': 1, 'missing_count': 0, 'total': 7, 'mean': 7,
                        'median': 7, 'q1': 7, 'q3': 7, 'min': 7, 'max': 7}),
}
# The statistics PostgreSQL 17 returns for CASES (EXACT_SUMMARY_SQL of research_aggregate_pg test_07).
SQL_TEXTS = {
    'fractional_sample': {
        'total': '4.0000000000000000000000000000000000000000', 'mean': '1.0000000000000000000000000000000000000000',
        'median': '0.99999999999999999999999999999999999999985', 'q1': '0.583333333333333333333333333333333333333350',
        'q3': '1.41666666666666666666666666666666666666650', 'min': '0.3333333333333333333333333333333333333333',
        'max': '1.666666666666666666666666666666666666667'},
    'counters_above_json_integer_precision': {
        'total': '36028797018963992', 'mean': '9007199254740998.0000', 'median': '9007199254740998.0',
        'q1': '9007199254740995.00', 'q3': '9007199254741001.00', 'min': '9007199254740992', 'max': '9007199254741004'},
    'fractional_result_below_safe_integer_limit': {
        'total': '9007199254741015', 'mean': '4503599627370507.5000', 'median': '4503599627370507.5',
        'q1': '4503599627370507.25', 'q3': '4503599627370507.75', 'min': '4503599627370507', 'max': '4503599627370508'},
    'empty': dict.fromkeys(('total', 'mean', 'median', 'q1', 'q3', 'min', 'max')),
    'single': {'total': '7', 'mean': '7.0000000000000000', 'median': '7.0', 'q1': '7.00', 'q3': '7.00', 'min': '7', 'max': '7'},
}


def presented(expected):
    """The shared answer's form of an expected value: counts as numbers, the rest as text."""
    return {key: expected[key] if key.endswith('_count') or expected[key] is None else str(expected[key]) for key in KEYS}


class ExactBaselineSummaries(unittest.TestCase):
    def test_presentation_of_sql_statistics_matches_expected_values(self):
        self.assertEqual(set(SQL_TEXTS), set(CASES))
        for name, (values, sample_count, expected) in CASES.items():
            with self.subTest(case=name):
                known = len(values)
                metric = round_metric(dict(SQL_TEXTS[name], known_count=known, missing_count=sample_count - known))
                self.assertEqual({key: metric[key] for key in KEYS}, presented(expected))

    def test_counters_above_json_integer_precision_stay_exact_text(self):
        result = round_metric(dict(SQL_TEXTS['counters_above_json_integer_precision']))
        self.assertTrue(all(isinstance(result[key], str) and '.' not in result[key] for key in ('total', 'mean', 'median', 'q1', 'q3')))
        self.assertEqual(int(result['median']) - int(result['min']), 6)

    def test_half_even_hundredths_and_unknowns(self):
        self.assertEqual(round_metric(dict.fromkeys(('total', 'mean', 'median', 'q1', 'q3', 'min', 'max'))),
                         dict.fromkeys(('total', 'mean', 'median', 'q1', 'q3', 'min', 'max')))
        self.assertEqual(round_metric({'total': '0.125', 'mean': '0.135', 'median': '2.50', 'q1': '-0.005',
                                       'q3': '4503599627370507.255', 'min': '7.000', 'max': '1'}),
                         {'total': '0.12', 'mean': '0.14', 'median': '2.5', 'q1': '0', 'q3': '4503599627370507.26',
                          'min': '7', 'max': '1'})


if __name__ == '__main__':
    unittest.main()
