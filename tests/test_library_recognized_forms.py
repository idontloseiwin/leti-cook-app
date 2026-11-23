"""Check that saved Library filters and UTC instant ranges reopen with the same meaning."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application')]

from research_query.library_v1 import translate

TABLE = json.loads((ROOT / 'tests' / 'fixtures' / 'library_translation_cases.json').read_text(encoding='utf-8'))
FORMS = json.loads((ROOT / 'tests' / 'fixtures' / 'library_recognized_forms.json').read_text(encoding='utf-8'))
ROWS = {row['name']: row for row in TABLE['cases']}


def meaning(conditions, row):
    result = translate(conditions, channel_id=TABLE['channel_id'], time_zone=row['time_zone'],
                       video_type=row['video_type']).payload()
    if result['status'] != 'translated':
        return {'status': result['status'], 'missing': result['missing']}
    query = result['query']
    return {'status': 'translated', 'video_types': query['video_types'], 'where': query.get('where'),
            'order_by': query['order_by']}


class TranslationTableTests(unittest.TestCase):
    def test_every_row_with_an_expectation_is_the_translator_output(self):
        checked = 0
        for row in TABLE['cases']:
            if row['expected'] is None:
                continue
            with self.subTest(row['name']):
                self.assertEqual(meaning(row['conditions'], row), row['expected'])
                checked += 1
        self.assertGreaterEqual(checked, 60)

    def test_each_recognized_form_translates_to_the_same_query(self):
        names = [form['name'] for form in FORMS['cases']]
        self.assertEqual(len(names), len(set(names)))
        for form in FORMS['cases']:
            row = ROWS[form['name']]
            with self.subTest(form['name']):
                self.assertEqual(row['expected']['status'], 'translated')
                self.assertTrue(form['why'])
                if form['recognized'] is None:
                    continue
                self.assertNotEqual(form['recognized'], row['conditions'])
                self.assertEqual(meaning(form['recognized'], row), row['expected'])


UTC_INSTANT_RANGES = json.loads((ROOT / 'tests' / 'fixtures' / 'library_saved_utc_instants.json').read_text(encoding='utf-8'))


def instants(node):
    if isinstance(node, dict):
        if node.get('field') == 'video.published_at' and isinstance(node.get('value'), str):
            yield node['value']
        for key in ('all', 'any'):
            for child in node.get(key, ()):
                yield from instants(child)


class SavedInstantRangeTests(unittest.TestCase):
    """Change to calendar fields: a Library v2 view saved with UTC instants of local midnights
    (before the switch) reopens as Library filters (test_library_saved.mjs) whose
     translation selects exactly the same rows in the saved zone."""

    def test_old_instant_shape_and_calendar_fields_select_the_same_rows(self):
        from datetime import datetime, timedelta, timezone
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_library_v1_translation import cells, evaluate
        self.assertEqual(len(UTC_INSTANT_RANGES['cases']), 11)
        steps = (-90000, -86400, -3601, -3600, -1, 0, 1, 3599, 3600, 86399, 86400, 90000)
        for row in UTC_INSTANT_RANGES['cases']:
            new = meaning(row['conditions'], row)
            self.assertEqual(new['status'], 'translated', row['name'])
            self.assertEqual(new['order_by'], row['order_by'], row['name'])
            fields = json.dumps(new['where'])
            self.assertTrue('video.published_date' in fields or 'video.published_year' in fields, row['name'])
            self.assertNotIn('video.published_at', fields, row['name'])
            moments = [None, '1999-06-01T00:00:00Z', '2040-06-01T00:00:00Z']
            for value in instants(row['where']):
                at = datetime.fromisoformat(value.replace('Z', '+00:00'))
                moments += [(at + timedelta(seconds=step)).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') for step in steps]
            matched = 0
            for moment in moments:
                values = cells({'video_id': 'x', 'title': 't', 'published_at': moment}, row['time_zone'])
                with self.subTest(row=row['name'], moment=moment):
                    old = evaluate(row['where'], values) is True
                    self.assertEqual(evaluate(new['where'], values) is True, old)
                    matched += old
            self.assertTrue(0 < matched < len(moments), row['name'])


if __name__ == '__main__':
    unittest.main()
