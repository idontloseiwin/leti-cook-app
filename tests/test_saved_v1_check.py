"""Saved v1 conditions round-trip through portable backup."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]

from test_library_v1_translation import TRANSLATIONS, v1
from research_query.library_v1 import translate  # noqa: E402
from saved_work import SavedWorkError, normalize_conditions  # noqa: E402
from user_work_backup import UserWorkError, normalize_backup  # noqa: E402

CHANNEL = 'UC' + 'A' * 22


def answer(value):
    try:
        return {'accepted': normalize_conditions(copy.deepcopy(value))}
    except SavedWorkError as error:
        return {'refused': [error.status_code, error.code, error.message]}




def backup(conditions):
    view = {'id': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'name': 'View', 'revision': 1,
            'created_at': '2026-09-01T00:00:00+00:00', 'updated_at': '2026-09-01T00:00:00+00:00',
            'conditions': conditions}
    channel = {'id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'user_input': 'https://www.youtube.com/@one',
               'folder_id': None, 'parent_ref': None, 'is_hidden': False, 'added_at': '2026-09-01T00:00:00+00:00',
               'label_keys': [], 'saved_views': [view]}
    return {'format': 'leti-cook-user-work', 'version': 1, 'created_at': '2026-09-20T00:00:00+00:00',
            'user_work': {'folders': [], 'labels': [], 'channels': [channel], 'collections': [], 'reviews': []},
            'browser_preferences': {'catalogue_density': 'large', 'bookmarked_channel_refs': [], 'collapsed_folder_ids': []}}


class SavedConditionsTests(unittest.TestCase):
    def test_all_supported_filters_survive_normalization_and_backup(self):
        for name, filters, facets, zone, _ in TRANSLATIONS:
            with self.subTest(case=name):
                raw = v1(filters, facets)
                normalized = normalize_conditions(copy.deepcopy(raw))
                stored = normalize_backup(backup(raw))['user_work']['channels'][0]['saved_views'][0]['conditions']
                self.assertEqual(stored, normalized)
                self.assertEqual(translate(stored, channel_id=CHANNEL, time_zone=zone).payload(),
                                 translate(raw, channel_id=CHANNEL, time_zone=zone).payload())

    def test_filter_combinations_keep_their_values_in_backup_and_queries(self):
        from itertools import product
        empty_facets = {'version': 1, 'view_buckets': [], 'duration_buckets': [], 'published_years': [],
                        'published_older_than': None, 'cohort': None}
        samples = product(('idea', 'two words', 'café', '42', 'x'*100), ('1', '10', '22', '27'),
                          ('0', '1', '100', '100000'), ('asc', 'desc'), ('long', 'short', 'stream'),
                          ('UTC', 'America/New_York'))
        for title, category, views, direction, kind, zone in samples:
            raw = v1({'q': ' '+title+' ', 'min_views': '00'+views, 'category_id': '0'+category,
                      'sort_order': direction, 'sort_by': 'views'})
            normalized = dict(raw['filters'], q=title, min_views=views, category_id=category)
            expected = dict(version=1, filters=normalized, facets=empty_facets)
            with self.subTest(title=title[:12], category=category, views=views, direction=direction, kind=kind, zone=zone):
                stored = normalize_backup(backup(raw))['user_work']['channels'][0]['saved_views'][0]['conditions']
                self.assertEqual(stored, expected)
                translated = translate(stored, channel_id=CHANNEL, time_zone=zone, video_type=kind).query
                self.assertEqual(translated['where'], {'all': [
                    {'field':'video.title','op':'contains_casefold_v1','value':title},
                    {'field':'video.category_id','op':'eq','value':category},
                    {'field':'video.views','op':'gte','value':views}]})
                self.assertEqual(translated['video_types'], [kind])
                self.assertEqual(translated['order_by'][0], {'field':'video.views','direction':direction,'nulls':'last'})

    def test_refusals_identify_the_invalid_part(self):
        cases = [(None, 'Saved view request is invalid.'),
                 (dict(v1(),version=77), 'Unsupported catalogue condition version.'),
                 (v1({'q':'x'*101}), 'Search is limited to 100 characters.'),
                 (v1(facets={'time_zone':'America/Argentina'}), 'Saved catalogue conditions are invalid.')]
        for raw, message in cases:
            with self.subTest(message=message):
                self.assertEqual(answer(raw), {'refused': [422, 'invalid_request', message]})
                with self.assertRaises(UserWorkError):
                    normalize_backup(backup(raw))

    def test_invalid_conditions_are_rejected_in_backup_and_directly(self):
        for raw in ({'version':77}, v1({'q':'x'*101}), {'version':1,'filters':[]}):
            with self.subTest(raw=raw):
                with self.assertRaises(SavedWorkError):
                    normalize_conditions(raw)
                with self.assertRaises(UserWorkError):
                    normalize_backup(backup(raw))


class TimeZoneTests(unittest.TestCase):

    def test_a_zone_directory_is_refused_not_an_error(self):
        value = {'version': 1, 'filters': {'q': '', 'category_id': '', 'published_from': '', 'published_before': '',
                                           'min_views': '', 'max_views': '', 'min_duration_seconds': '',
                                           'max_duration_seconds': '', 'sort_by': 'published_at', 'sort_order': 'desc'},
                 'facets': {'version': 1, 'time_zone': 'America/Argentina'}}
        self.assertEqual(answer(value), {'refused': [422, 'invalid_request', 'Saved catalogue conditions are invalid.']})
        stored = normalize_conditions({**value, 'facets': {'version': 1}})
        with self.assertRaises(ValueError):
            translate(stored, channel_id=CHANNEL, time_zone='America/Argentina')


if __name__ == '__main__':
    unittest.main()
