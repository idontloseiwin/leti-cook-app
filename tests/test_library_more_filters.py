"""Library's further filters: Captions, Description contains,
declared paid promotion and a stream's actual end; and the duration presets of
each format in the translator.

Optional v1 filter keys, absent when empty, so every older Saved view, address and
create fingerprint keeps its exact form. The one translator (research_query/library_v1.py)
gives them meaning; the browser codec (catalogueQuery.ts) accepts the same keys and
choices. Offline: no database, network or YouTube.
"""
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application')]

from research_query.library_v1 import (DURATION_SETS, MORE_FILTER_KEYS, MORE_CHOICES, more_conditions,  # noqa: E402
                                       normalize_more_filters, translate)
from saved_work import SavedWorkError, normalize_conditions, translate_library_conditions  # noqa: E402

CHANNEL = 'UC' + 'A' * 22
EMPTY = {'q': '', 'category_id': '', 'published_from': '', 'published_before': '', 'min_views': '', 'max_views': '',
         'min_duration_seconds': '', 'max_duration_seconds': '', 'sort_by': 'published_at', 'sort_order': 'desc'}


def v1(**filters):
    return {'version': 1, 'filters': {**EMPTY, **filters}, 'facets': {'version': 1}}


def where(zone='UTC', video_type='long', **filters):
    result = translate(v1(**filters), channel_id=CHANNEL, time_zone=zone, video_type=video_type)
    assert result.status == 'translated', result
    return result.query.get('where')


class MoreFiltersTests(unittest.TestCase):
    def test_older_objects_keep_their_exact_form(self):
        before = normalize_conditions(v1())
        self.assertEqual(set(before['filters']), set(EMPTY))
        # Empty further filters are absent, not empty strings: the create fingerprint does not change.
        self.assertEqual(normalize_conditions(v1(captions='', description_q='  ', stream_ended_from='')), before)
        self.assertIsNone(where())

    def test_normalized_values(self):
        filters = normalize_conditions(v1(captions='with', description_q='  Cooking  pasta ', paid_promotion='not_declared',
                                          stream_ended_from='2026-03-29', stream_ended_before='2026-04-01'))['filters']
        self.assertEqual({key: filters[key] for key in MORE_FILTER_KEYS},
                         {'captions': 'with', 'description_q': 'Cooking  pasta', 'paid_promotion': 'not_declared',
                          'stream_ended_from': '2026-03-29', 'stream_ended_before': '2026-04-01'})

    def test_invalid_values_are_refused(self):
        for key, value in (('captions', 'maybe'), ('paid_promotion', 'yes'), ('description_q', 'x' * 101), ('description_q', 'a\x00b'),
                           ('stream_ended_from', '2026-02-30'), ('stream_ended_from', '0000-01-01'), ('stream_ended_before', '0001-01-01'),
                           ('captions', 1), ('unknown_filter', 'x')):
            with self.subTest(key=key, value=value), self.assertRaises(SavedWorkError):
                normalize_conditions(v1(**{key: value}))
        # 100 UTF-16 units are allowed, as in the browser.
        self.assertEqual(normalize_more_filters({'description_q': '\U0001F600' * 50}), {'description_q': '\U0001F600' * 50})
        with self.assertRaises(ValueError):
            normalize_more_filters({'description_q': '\U0001F600' * 51})

    def test_translation(self):
        self.assertEqual(where(captions='with'), {'field': 'video.caption_available', 'op': 'eq', 'value': True})
        self.assertEqual(where(captions='without'), {'field': 'video.caption_available', 'op': 'eq', 'value': False})
        self.assertEqual(where(paid_promotion='declared'), {'field': 'video.paid_promotion', 'op': 'eq', 'value': True})
        self.assertEqual(where(paid_promotion='not_declared'), {'field': 'video.paid_promotion', 'op': 'eq', 'value': False})
        # The Library search literal rules (trim, collapse whitespace; casefold on the server).
        self.assertEqual(where(description_q='  Cooking \t pasta '), {'field': 'video.description', 'op': 'contains_casefold_v1', 'value': 'Cooking pasta'})
        # Local midnights of the viewer's zone across the DST change (Berlin, 29 March 2026).
        self.assertEqual(where(zone='Europe/Berlin', video_type='stream', stream_ended_from='2026-03-29', stream_ended_before='2026-04-01'),
                         {'all': [{'field': 'video.actual_end_at', 'op': 'gte', 'value': '2026-03-28T23:00:00Z'},
                                  {'field': 'video.actual_end_at', 'op': 'lt', 'value': '2026-03-31T22:00:00Z'}]})
        self.assertEqual(where(zone='Pacific/Kiritimati', stream_ended_from='2026-01-01'),
                         {'field': 'video.actual_end_at', 'op': 'gte', 'value': '2025-12-31T10:00:00Z'})
        # Beyond the instant range an end is open: year 1 in a zone east of UTC starts before 0001-01-01Z (Tokyo local mean time +9:18:59).
        self.assertEqual(where(zone='Asia/Tokyo', stream_ended_from='0001-01-01', stream_ended_before='0001-01-03'),
                         {'field': 'video.actual_end_at', 'op': 'lt', 'value': '0001-01-02T14:41:01Z'})

    def test_order_after_every_older_filter(self):
        conditions = v1(q='guide', min_views='10', captions='with', paid_promotion='declared', description_q='pasta',
                        stream_ended_from='2026-01-01')
        nodes = translate(conditions, channel_id=CHANNEL, time_zone='UTC', video_type='stream').query['where']['all']
        self.assertEqual([node.get('field') for node in nodes[:2]], ['video.title', 'video.views'])
        self.assertEqual([node.get('field') for node in nodes[2:]], ['video.caption_available', 'video.paid_promotion', 'video.description',
                                                                      'video.actual_end_at'])
        self.assertEqual(more_conditions(conditions['filters'], 'UTC'), nodes[2:])

    def test_route_translates_them(self):
        body = {'channel_id': CHANNEL, 'conditions': v1(captions='with'), 'time_zone': 'UTC', 'video_type': 'short'}
        reply = translate_library_conditions(body)['translation']
        self.assertEqual(reply['status'], 'translated')
        self.assertEqual(reply['query']['where'], {'field': 'video.caption_available', 'op': 'eq', 'value': True})

    def test_the_browser_codec_knows_the_same_keys_and_choices(self):
        explorer = ROOT / 'application/frontend/src/explorer'
        filters = (explorer / 'filters.ts').read_text(encoding='utf-8')
        keys = re.search(r"MORE_CATALOGUE_FILTER_KEYS = \[([^\]]+)\]", filters).group(1)
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", keys)), MORE_FILTER_KEYS)
        codec = (explorer / 'catalogueQuery.ts').read_text(encoding='utf-8')
        for key, name in (('captions', 'CATALOGUE_CAPTION_CHOICES'), ('paid_promotion', 'CATALOGUE_PAID_PROMOTION_CHOICES')):
            choices = re.search(name + r" = \{([^}]+)\}", codec).group(1)
            self.assertEqual(set(re.findall(r"(\w+): '", choices)), set(MORE_CHOICES[key][1]), key)


class TypedDurationTests(unittest.TestCase):
    """Shorts and Streams have their own duration presets (the shared presets file)."""

    def duration(self, keys, video_type, zone='UTC'):
        conditions = {'version': 1, 'filters': dict(EMPTY), 'facets': {'version': 1, 'duration_buckets': keys}}
        result = translate(conditions, channel_id=CHANNEL, time_zone=zone, video_type=video_type)
        self.assertEqual(result.status, 'translated')
        return result.query['where']

    def test_each_format_translates_its_own_presets(self):
        self.assertEqual(self.duration(['1m_3m'], 'short'), {'all': [{'field': 'video.duration_seconds', 'op': 'gte', 'value': '60'},
                                                                     {'field': 'video.duration_seconds', 'op': 'lt', 'value': '181'}]})
        self.assertEqual(self.duration(['under_15s', '15s_30s'], 'short'), {'field': 'video.duration_seconds', 'op': 'lt', 'value': '30'})
        self.assertEqual(self.duration(['4h_plus'], 'stream'), {'field': 'video.duration_seconds', 'op': 'gte', 'value': '14400'})
        # Long is unchanged.
        self.assertEqual(self.duration(['under_5m'], 'long'), {'field': 'video.duration_seconds', 'op': 'lt', 'value': '300'})

    def test_presets_of_another_format_are_refused(self):
        for keys, video_type in ((['under_5m'], 'short'), (['1m_3m'], 'long'), (['under_15s'], 'stream')):
            with self.subTest(keys=keys, video_type=video_type), self.assertRaises(SavedWorkError):
                translate_library_conditions({'channel_id': CHANNEL, 'conditions': {'version': 1, 'filters': dict(EMPTY),
                                              'facets': {'version': 1, 'duration_buckets': keys}}, 'time_zone': 'UTC', 'video_type': video_type})

    def test_v1_objects_keep_any_format_presets_in_one_order(self):
        keys = [item['key'] for item in DURATION_SETS['short']]
        stored = normalize_conditions({'version': 1, 'filters': dict(EMPTY), 'facets': {'version': 1, 'duration_buckets': list(reversed(keys))}})
        self.assertEqual(stored['facets']['duration_buckets'], keys)
        # Long objects keep their exact form.
        long_object = {'version': 1, 'filters': dict(EMPTY), 'facets': {'version': 1, 'duration_buckets': ['60m_plus', 'under_5m']}}
        self.assertEqual(normalize_conditions(long_object)['facets']['duration_buckets'], ['under_5m', '60m_plus'])
        for bad in (['unknown'], ['1m_3m']):
            facets = {'version': 1, 'duration_buckets': bad}
            filters = dict(EMPTY, min_duration_seconds='10') if bad == ['1m_3m'] else dict(EMPTY)
            with self.subTest(bad=bad), self.assertRaises(SavedWorkError):
                normalize_conditions({'version': 1, 'filters': filters, 'facets': facets})

    def test_the_browser_draws_the_same_sets(self):
        shared = json.loads((ROOT / 'application/shared/catalogue-filter-presets.json').read_text(encoding='utf-8'))
        self.assertEqual(DURATION_SETS['short'], shared['by_video_type']['short']['duration'])
        self.assertEqual(DURATION_SETS['stream'], shared['by_video_type']['stream']['duration'])
        self.assertEqual(DURATION_SETS['long'], shared['duration'])


if __name__ == '__main__':
    unittest.main()
