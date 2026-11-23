"""Library conditions, ordering and saved query format."""
from datetime import datetime, timezone
from fractions import Fraction
from functools import cmp_to_key
import json
from pathlib import Path
import re
import sys
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]

from research_query.library_v1 import (CAPABILITIES, FACET_KEYS, GROWTH_FILTER_KEYS, LIBRARY_SELECT, PUBLISHED_DATE,
                                       PUBLISHED_YEAR, SORT_FIELDS, STATIC_BOUND_KEYS, STATIC_PRESETS,
                                       publication_date_conditions, leaf_count, search_literal, translate)
from research_query.saved import translate_legacy
from saved_work import FILTER_KEYS, SavedWorkError
from research_query import growth
from youtube_source_fields import youtube_duration_seconds

CHANNEL = 'UC' + 'A' * 22
EMPTY = {'q': '', 'category_id': '', 'published_from': '', 'published_before': '', 'min_views': '', 'max_views': '',
         'min_duration_seconds': '', 'max_duration_seconds': '', 'sort_by': 'published_at', 'sort_order': 'desc'}


def v1(filters=None, facets=None):
    return {'version': 1, 'filters': {**EMPTY, **(filters or {})}, 'facets': {'version': 1, **(facets or {})}}


def where(filters=None, facets=None, zone='UTC'):
    result = translate(v1(filters, facets), channel_id=CHANNEL, time_zone=zone)
    assert result.status == 'translated', result
    return result.query.get('where')


def leaf(field, op, value=None):
    return {'field': field, 'op': op} if value is None else {'field': field, 'op': op, 'value': value}


def span(field, low, high):
    return {'all': [leaf(field, 'gte', low), leaf(field, 'lt', high)]}


GAINED, INTERVAL, STATUS = growth.GAINED, growth.INTERVAL, growth.STATUS


def grown(*leaves):
    """Growth.legacy_conditions: any growth condition admits only ready and partial rows."""
    return {'all': [leaf(GAINED, 'is_known'), *leaves]}


# (v1 key, filters, facets, viewer zone, exact where). One row per condition key.
TRANSLATIONS = [
    ('filters.q', {'q': ' Straße   Guide\t'}, None, 'UTC', leaf('video.title', 'contains_casefold_v1', 'Straße Guide')),
    ('filters.category_id', {'category_id': '022'}, None, 'UTC', leaf('video.category_id', 'eq', '22')),
    ('filters.published_from', {'published_from': '2024-01-01'}, None, 'UTC', leaf(PUBLISHED_DATE, 'gte', '2024-01-01')),
    ('filters.published_from', {'published_from': '2024-01-01'}, None, 'Europe/Berlin', leaf(PUBLISHED_DATE, 'gte', '2024-01-01')),
    ('filters.published_before', {'published_before': '2024-03-31'}, None, 'America/New_York', leaf(PUBLISHED_DATE, 'lt', '2024-03-31')),
    ('filters.min_views', {'min_views': '0010'}, None, 'UTC', leaf('video.views', 'gte', '10')),
    ('filters.max_views', {'max_views': '1000000000000000'}, None, 'UTC', leaf('video.views', 'lte', '1000000000000000')),
    ('filters.min_duration_seconds', {'min_duration_seconds': '60'}, None, 'UTC', leaf('video.duration_seconds', 'gte', '60')),
    ('filters.max_duration_seconds', {'max_duration_seconds': '0'}, None, 'UTC', leaf('video.duration_seconds', 'lte', '0')),
    ('filters.min_likes', {'min_likes': '5'}, None, 'UTC', leaf('video.likes', 'gte', '5')),
    ('filters.max_likes', {'max_likes': '007'}, None, 'UTC', leaf('video.likes', 'lte', '7')),
    ('filters.min_comments', {'min_comments': '0'}, None, 'UTC', leaf('video.comments', 'gte', '0')),
    ('filters.max_comments', {'max_comments': '99'}, None, 'UTC', leaf('video.comments', 'lte', '99')),
    ('facets.view_buckets', None, {'view_buckets': ['10k_100k']}, 'UTC', span('video.views', '10000', '100000')),
    ('facets.view_buckets', None, {'view_buckets': ['10m_plus', 'under_1k', '1k_10k']}, 'UTC',
     {'any': [leaf('video.views', 'lt', '10000'), leaf('video.views', 'gte', '10000000')]}),
    ('facets.view_buckets', None, {'view_buckets': ['under_1k', '1k_10k', '10k_100k', '100k_1m', '1m_10m', '10m_plus']}, 'UTC',
     leaf('video.views', 'is_known')),
    ('facets.duration_buckets', None, {'duration_buckets': ['under_5m', '20m_40m']}, 'UTC',
     {'any': [leaf('video.duration_seconds', 'lt', '300'), span('video.duration_seconds', '1200', '2400')]}),
    ('facets.like_buckets', None, {'like_buckets': ['100_1k', '1k_10k']}, 'UTC', span('video.likes', '100', '10000')),
    ('facets.comment_buckets', None, {'comment_buckets': ['10k_plus']}, 'UTC', leaf('video.comments', 'gte', '10000')),
    ('facets.published_years', None, {'published_years': [2024]}, 'UTC', leaf(PUBLISHED_YEAR, 'in', ['2024'])),
    ('facets.published_years', None, {'published_years': [2022, 2024]}, 'Asia/Tokyo', leaf(PUBLISHED_YEAR, 'in', ['2022', '2024'])),
    ('facets.published_older_than', None, {'published_older_than': 2023}, 'UTC', leaf(PUBLISHED_YEAR, 'lt', '2023')),
    ('facets.published_older_than', None, {'published_years': [2025, 2024, 2023], 'published_older_than': 2023}, 'America/New_York',
     {'any': [leaf(PUBLISHED_YEAR, 'in', ['2023', '2024', '2025']), leaf(PUBLISHED_YEAR, 'lt', '2023')]}),
    # The Library's calculated values (computed registry fields).
    ('filters.min_title_length', {'min_title_length': '3'}, None, 'UTC', leaf('video.title_length', 'gte', '3')),
    ('filters.max_title_length', {'max_title_length': '80'}, None, 'UTC', leaf('video.title_length', 'lte', '80')),
    ('filters.min_tag_count', {'min_tag_count': '1'}, None, 'UTC', leaf('video.tag_count', 'gte', '1')),
    ('filters.max_tag_count', {'max_tag_count': '15'}, None, 'UTC', leaf('video.tag_count', 'lte', '15')),
    ('filters.min_like_rate', {'min_like_rate': '2.5'}, None, 'UTC', leaf('video.like_rate', 'gte', '2.5')),
    ('filters.max_like_rate', {'max_like_rate': '33.333333'}, None, 'UTC', leaf('video.like_rate', 'lte', '33.333333')),
    ('filters.min_comment_rate', {'min_comment_rate': '0.000001'}, None, 'UTC', leaf('video.comment_rate', 'gte', '0.000001')),
    ('filters.max_comment_rate', {'max_comment_rate': '1'}, None, 'UTC', leaf('video.comment_rate', 'lte', '1')),
    ('facets.title_length_buckets', None, {'title_length_buckets': ['under_40', '60_80']}, 'UTC',
     {'any': [leaf('video.title_length', 'lt', '40'), span('video.title_length', '60', '80')]}),
    ('facets.tag_count_buckets', None, {'tag_count_buckets': ['zero', '16_plus']}, 'UTC',
     {'any': [span('video.tag_count', '0', '1'), leaf('video.tag_count', 'gte', '16')]}),
    ('facets.cohort', None, {'cohort': {'kind': 'overview', 'published_from': '2026-09-01T00:00:00Z', 'published_through': '2026-10-01T12:30:00.5Z'}}, 'UTC',
     {'all': [leaf('video.published_at', 'gte', '2026-09-01T00:00:00Z'), leaf('video.published_at', 'lte', '2026-10-01T12:30:00.500000Z')]}),
    ('facets.cohort', None, {'cohort': {'kind': 'overview', 'published_from': None, 'published_through': '2026-10-01T00:00:00+02:00'}}, 'UTC',
     {'any': [leaf('video.published_at', 'is_unknown'), leaf('video.published_at', 'lte', '2026-09-30T22:00:00Z')]}),
    # 28-day growth: fields through growth.legacy_conditions.
    ('facets.growth.min_views_gained', None, {'growth': {'min_views_gained': '-5'}}, 'UTC', grown(leaf(GAINED, 'gte', '-5'))),
    ('facets.growth.max_views_gained', None, {'growth': {'max_views_gained': '009'}}, 'UTC', grown(leaf(GAINED, 'lte', '9'))),
    ('facets.growth.min_tracked_days', None, {'growth': {'min_tracked_days': '7'}}, 'UTC', grown(leaf(INTERVAL, 'gte', '604800'))),
    # No interval reaches 2**64 - 1: the largest v1 bound keeps matching nothing.
    ('facets.growth.min_tracked_days', None, {'growth': {'min_tracked_days': '1000000000000000'}}, 'UTC',
     grown(leaf(INTERVAL, 'gte', '18446744073709551615'))),
    ('facets.growth.full_history', None, {'growth': {'full_history': True}}, 'UTC', grown(leaf(STATUS, 'eq', 'ready'))),
    ('facets.growth.min_views_gained', None,
     {'growth': {'min_views_gained': '0', 'max_views_gained': '100', 'min_tracked_days': '1', 'full_history': True}}, 'UTC',
     grown(leaf(GAINED, 'gte', '0'), leaf(GAINED, 'lte', '100'), leaf(INTERVAL, 'gte', '86400'), leaf(STATUS, 'eq', 'ready'))),
]
ORDERS = {'published_at': 'video.published_at', 'views': 'video.views', 'like_count': 'video.likes',
          'comment_count': 'video.comments', 'duration': 'video.duration_seconds', 'title': 'video.title',
          'views_gained': GAINED}
# (v1 key, filters, facets, exact missing capabilities and the conditions needing them).
# Every v1 condition has a registry field now: none needs one.
NEEDS = []
# Covered elsewhere: static bounds travel in catalogue_filters.bounds and the viewer
# zone in catalogue_filters.time_zone (here: time_zone=); limit/offset are paging;
# sort_order is tested with every sort field; the facets version is always 1.
TRANSPORT = {'catalogue_filters.bounds', 'catalogue_filters.time_zone', 'limit', 'offset', 'facets.version', 'filters.sort_order'}


def ts_list(source, pattern):
    return re.findall(r"'([a-z_]+)'", re.search(pattern, source, re.S).group(1))


class CoverageTests(unittest.TestCase):
    def test_every_v1_key_in_the_code_has_a_table_row(self):
        frontend = ROOT / 'application/frontend/src/explorer'
        query_ts, filters_ts = (frontend / 'catalogueQuery.ts').read_text('utf-8'), (frontend / 'filters.ts').read_text('utf-8')
        facets_ts = set(ts_list(query_ts, r"record\(value, 'facets', \[(.*?)\]\)"))
        growth_ts = set(ts_list(query_ts, r"record\(value, 'facets.growth', \[(.*?)\]\)"))
        static_ts = set(ts_list(filters_ts, r"STATIC_CATALOGUE_FILTER_KEYS = \[(.*?)\]"))
        base_ts = set(re.findall(r"^  ([a-z_]+): '", re.search(r"INITIAL_EXPLORER_FILTERS: ExplorerFilters = \{(.*?)\};", filters_ts, re.S).group(1), re.M))
        sorts_ts = set(ts_list(query_ts, r"const sortFields = new Set\(\[(.*?)\]\)"))
        python_facets = set(FACET_KEYS)
        static_bound_keys = set(STATIC_BOUND_KEYS)
        self.assertEqual(base_ts, set(FILTER_KEYS))
        self.assertEqual(static_ts, static_bound_keys)
        self.assertEqual(growth_ts, set(GROWTH_FILTER_KEYS))
        self.assertEqual(sorts_ts, set(SORT_FIELDS))
        self.assertEqual(facets_ts | {'bounds', 'time_zone'}, python_facets)
        covered = {row[0] for row in TRANSLATIONS} | {row[0] for row in NEEDS} | TRANSPORT
        covered |= {'filters.sort_by=' + key for key in ORDERS}
        expected = ({'filters.' + key for key in set(FILTER_KEYS) - {'sort_by'}} | {'filters.' + key for key in static_bound_keys}
                    | {'facets.' + key for key in facets_ts - {'growth'}} | {'facets.growth.' + key for key in growth_ts}
                    | {'filters.sort_by=' + key for key in sorts_ts} | {'catalogue_filters.bounds', 'catalogue_filters.time_zone', 'limit', 'offset'})
        self.assertEqual(expected - covered, set())
        self.assertEqual({row[0] for row in NEEDS if not row[0].startswith('filters.sort_by')} & {row[0] for row in TRANSLATIONS}, set())
        self.assertEqual(set(CAPABILITIES) - {'query.where_leaf_budget'}, {c for row in NEEDS for c in row[3]})
        self.assertEqual(set(CAPABILITIES), {'query.where_leaf_budget'})


class TranslationTests(unittest.TestCase):
    def test_each_condition_has_its_exact_translation(self):
        for key, filters, facets, zone, expected in TRANSLATIONS:
            with self.subTest(key=key, filters=filters, facets=facets, zone=zone):
                self.assertEqual(where(filters, facets, zone), expected)

    def test_order_and_query_shell(self):
        for legacy, field in ORDERS.items():
            for direction in ('asc', 'desc'):
                with self.subTest(sort=legacy, direction=direction):
                    result = translate(v1({'sort_by': legacy, 'sort_order': direction}), channel_id=CHANNEL)
                    self.assertEqual(result.query['order_by'], [{'field': field, 'direction': direction, 'nulls': 'last'}])
                    self.assertEqual(result.query['scope'], {'kind': 'channel', 'id': CHANNEL})
                    self.assertEqual(result.query['video_types'], ['long'])
                    self.assertEqual(result.query['select'], list(LIBRARY_SELECT))
                    self.assertEqual(result.query['page'], {'size': 50, 'after': None})
                    self.assertNotIn('where', {k: v for k, v in result.query.items() if v is not None})

    def test_library_tab_chooses_the_video_type(self):
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind):
                result = translate(v1({'q': 'x'}), channel_id=CHANNEL, video_type=kind)
                self.assertEqual(result.query['video_types'], [kind])
                self.assertEqual(result.query['where'], leaf('video.title', 'contains_casefold_v1', 'x'))
        for kind in ('live', 'Long', None, ['long']):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                translate(v1(), channel_id=CHANNEL, video_type=kind)

    def test_calendar_target_of_publication_conditions_is_documented(self):
        # Dates and years in the request's time zone through the computed fields.
        filters = {**EMPTY, 'published_from': '2024-01-01', 'published_before': '2024-03-31'}
        facets = {'published_years': [2025, 2023], 'published_older_than': 2023}
        self.assertEqual(publication_date_conditions(filters, facets), [
            leaf(PUBLISHED_DATE, 'gte', '2024-01-01'), leaf(PUBLISHED_DATE, 'lt', '2024-03-31'),
            {'any': [leaf(PUBLISHED_YEAR, 'in', ['2023', '2025']), leaf(PUBLISHED_YEAR, 'lt', '2023')]}])
        self.assertEqual(publication_date_conditions({**EMPTY}, {'published_years': [2024], 'published_older_than': None}),
                         [leaf(PUBLISHED_YEAR, 'in', ['2024'])])

    def test_calendar_translator_uses_date_fields(self):
        # Merged: the calendar fields exist and every translation targets them in the viewer's zone.
        from research_query.registry import FIELDS
        self.assertEqual((FIELDS[PUBLISHED_DATE].value_type, FIELDS[PUBLISHED_YEAR].value_type), ('local_date', 'uint_decimal'))
        for filters, facets, expected in (({'published_from': '2024-01-01'}, None, leaf(PUBLISHED_DATE, 'gte', '2024-01-01')),
                                          (None, {'published_years': [2024]}, leaf(PUBLISHED_YEAR, 'in', ['2024']))):
            result = translate(v1(filters, facets), channel_id=CHANNEL, time_zone='Asia/Tokyo')
            self.assertEqual(result.query['time'], {'mode': 'current', 'timezone': 'Asia/Tokyo'})
            self.assertEqual(result.query['where'], expected)

    def test_several_conditions_are_one_conjunction_in_table_order(self):
        result = translate(v1({'q': 'a', 'min_views': '1', 'min_likes': '2'}, {'duration_buckets': ['60m_plus']}), channel_id=CHANNEL)
        self.assertEqual(result.query['where'], {'all': [leaf('video.title', 'contains_casefold_v1', 'a'), leaf('video.views', 'gte', '1'),
                                                          leaf('video.likes', 'gte', '2'), leaf('video.duration_seconds', 'gte', '3600')]})

    def test_missing_capabilities_are_named_and_never_partial(self):
        for key, filters, facets, expected in NEEDS:
            with self.subTest(key=key):
                result = translate(v1({'q': 'kept only if complete', **(filters or {})}, facets), channel_id=CHANNEL)
                self.assertEqual(result.status, 'needs_capability')
                self.assertIsNone(result.query)
                self.assertEqual({item.capability: list(item.conditions) for item in result.missing}, expected)
        # Calculated values, growth and its sort translate together.
        combined = translate(v1({'min_like_rate': '1.5', 'sort_by': 'views_gained'}, {'growth': {'full_history': True}, 'tag_count_buckets': ['zero']}),
                             channel_id=CHANNEL).payload()
        self.assertEqual((combined['status'], combined['missing']), ('translated', []))
        self.assertEqual(combined['query']['where'], {'all': [leaf('video.like_rate', 'gte', '1.5'), span('video.tag_count', '0', '1'),
                                                              grown(leaf(STATUS, 'eq', 'ready'))]})
        self.assertEqual(combined['query']['order_by'], [{'field': GAINED, 'direction': 'desc', 'nulls': 'last'}])
        self.assertEqual(set(combined), {'status', 'query', 'time_zone', 'missing'})

    def test_the_leaf_budget_is_named_and_never_partial(self):
        # The richest v1 object stays far below one query's budget, so the guard
        # is proved with a smaller budget: a typed answer, never a partial query.
        from unittest.mock import patch
        from research_query.contracts import MAX_LEAVES
        richest = v1({'q': 'a', 'category_id': '22', 'min_likes': '1', 'max_likes': '9', 'min_comments': '1', 'max_comments': '9', 'min_title_length': '1',
                      'max_title_length': '9', 'min_tag_count': '1', 'max_tag_count': '9', 'min_like_rate': '1', 'max_like_rate': '9',
                      'min_comment_rate': '1', 'max_comment_rate': '9'},
                     {'view_buckets': ['under_1k', '10k_100k', '1m_10m'], 'duration_buckets': ['under_5m', '10m_20m', '40m_60m'],
                      'published_years': [2020, 2022], 'published_older_than': 2010,
                      'cohort': {'kind': 'overview', 'published_from': '2020-01-01T00:00:00Z', 'published_through': '2026-01-01T00:00:00Z'},
                      'growth': {'min_views_gained': '1', 'max_views_gained': '9', 'min_tracked_days': '1', 'full_history': True}})
        found = translate(richest, channel_id=CHANNEL)
        self.assertEqual(found.status, 'translated')
        leaves = leaf_count(found.query['where'])
        self.assertLess(leaves, MAX_LEAVES // 2)
        with patch('research_query.contracts.MAX_LEAVES', leaves - 1):
            result = translate(richest, channel_id=CHANNEL).payload()
        self.assertEqual(result, {'status': 'needs_capability', 'query': None, 'time_zone': 'UTC',
                                  'missing': [{'capability': 'query.where_leaf_budget', 'conditions': ['conditions']}]})

    def test_many_years_are_one_condition(self):
        # Local years are one `in` condition however many are chosen (no instant ranges, no leaf budget).
        alternate = [year for year in range(1802, 2002, 2)]
        result = translate(v1(facets={'published_years': alternate}), channel_id=CHANNEL)
        self.assertEqual(result.query['where'], leaf(PUBLISHED_YEAR, 'in', [str(year) for year in alternate]))
        self.assertEqual(leaf_count(result.query['where']), 1)

    def test_extreme_dates_and_years_stay_calendar_values(self):
        self.assertEqual(where({'published_from': '0001-01-01'}, zone='Etc/GMT-9'), leaf(PUBLISHED_DATE, 'gte', '0001-01-01'))
        self.assertEqual(where({'published_from': '9999-12-31'}, zone='America/New_York'), leaf(PUBLISHED_DATE, 'gte', '9999-12-31'))
        self.assertEqual(where(facets={'published_years': [9999]}), leaf(PUBLISHED_YEAR, 'in', ['9999']))
        self.assertEqual(where(facets={'published_years': [1]}, zone='Etc/GMT-9'), leaf(PUBLISHED_YEAR, 'in', ['1']))

    def test_non_v1_invalid_zone_or_channel_is_rejected(self):
        from research_query.saved import from_query
        from test_research_query_contract import query
        with self.assertRaises(ValueError):
            translate(from_query(query(scope={'kind': 'channel', 'id': CHANNEL}))[1], channel_id=CHANNEL)
        for zone in ('Mars/Olympus', 'local', '../etc', 'UTC+3'):
            with self.subTest(zone=zone), self.assertRaises(ValueError):
                translate(v1(), channel_id=CHANNEL, time_zone=zone)
        with self.assertRaises(SavedWorkError):
            translate(v1(), channel_id='UC123')
        with self.assertRaises(SavedWorkError):
            translate(v1({'q': 'x' * 101}), channel_id=CHANNEL)

    def test_saved_wrapper_returns_the_same_payload(self):
        value = v1({'q': 'x'})
        self.assertEqual(translate_legacy(CHANNEL, value, time_zone='Europe/Berlin'),
                         translate(value, channel_id=CHANNEL, time_zone='Europe/Berlin').payload())


class SearchTests(unittest.TestCase):

    def test_query_whitespace_collapses_title_whitespace_does_not(self):
        for raw, literal in SEARCH_LITERALS:
            with self.subTest(raw=raw):
                self.assertEqual(search_literal(raw), literal)
        self.assertTrue(evaluate(where({'q':'a b'}), cells({'title':'xA B'})))
        self.assertEqual(where({'q': 'a  b'}), leaf('video.title', 'contains_casefold_v1', 'a b'))
        self.assertTrue(evaluate(where({'q': 'STRASSE'}), cells({'title': 'Straße'})))
        self.assertIs(evaluate(where({'q': 'a  b'}), cells({'title': 'a  b'})), False)


# SearchTests: (raw query text, the shared literal).
SEARCH_LITERALS = (('  a  b ', 'a b'), ('a　 b', 'a b'), ('﻿a\x1cb', 'a b'), ('\x1c﻿a', '﻿a'),
                             ('A\tB\nC', 'A B C'), ('İstanbul', 'İstanbul'), ('', ''))


def instant(value):
    return None if value is None else datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def cells(row, zone='UTC'):
    """Registry cell values of a synthetic admitted row (None = not known); the
    local date and year are those of the viewer's zone (outside 1..9999: unknown)."""
    published = instant(row.get('published_at'))
    try:
        local = None if published is None else published.astimezone(ZoneInfo(zone))
    except OverflowError:
        local = None
    return {'video.title': row.get('title'), 'video.category_id': row.get('category_id'),
            'video.published_at': published, 'video.views': row.get('views'),
            'video.published_date': None if local is None else local.date().isoformat(),
            'video.title_length': len(row['title']) if row.get('title') is not None else None,
            'video.tag_count': len(row['tags']) if row.get('tags') is not None else None,
            **{'video.'+metric+'_rate': Fraction(row[key]*100,row['views']) if row.get(key) is not None and row.get('views') else None
               for metric,key in (('like','like_count'),('comment','comment_count'))},
            'video.published_year': None if local is None else local.year,
            'video.duration_seconds': youtube_duration_seconds(row.get('duration')),
            'video.likes': row.get('like_count'), 'video.comments': row.get('comment_count')}



def typed(field, value):
    if field in {'video.like_rate', 'video.comment_rate'}:
        return Fraction(value)
    if field == STATUS:
        return value
    return (instant(value) if field == 'video.published_at' else
            value if field in {'video.title', 'video.category_id', 'video.published_date'} else int(value))


def evaluate(node, values):
    """Three-valued shared predicate: unknown yields unknown, as in SQL."""
    if node is None:
        return True
    for key, decisive, neutral in (('all', False, True), ('any', True, False)):
        if key in node:
            answers = [evaluate(child, values) for child in node[key]]
            return decisive if decisive in answers else None if None in answers else neutral
    if 'not' in node:
        answer = evaluate(node['not'], values)
        return None if answer is None else not answer
    value, op = values[node['field']], node['op']
    if op in {'is_known', 'is_unknown'}:
        return (value is not None) == (op == 'is_known')
    if value is None:
        return None
    if op == 'contains_casefold_v1':
        return node['value'].casefold() in value.casefold()
    if op == 'in':
        return value in [typed(node['field'], item) for item in node['value']]
    target = typed(node['field'], node['value'])
    return {'eq': value == target, 'gte': value >= target, 'gt': value > target, 'lte': value <= target, 'lt': value < target}[op]


YEAR_ZONES = (('America/Sao_Paulo', [2018, 2019]), ('Asia/Dhaka', [2009, 2010]), ('Pacific/Apia', [2011, 2012]),
              ('America/Argentina/Buenos_Aires', [2008]), ('Africa/Casablanca', [2030]), ('Europe/Moscow', [2011]))


def year_moments(zone_name, years):
    """The instants test_local_year_boundaries_in_shifting_zones checks around each local new year."""
    from datetime import timedelta
    zone = ZoneInfo(zone_name)
    for year in years + [years[-1] + 1]:
        start = datetime(year, 1, 1, tzinfo=zone).astimezone(timezone.utc)
        for minutes in range(-180, 181, 15):
            yield (start + timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


class SavedOrderTests(unittest.TestCase):
    def test_empty_title_order(self):
        data = [{'video_id': 'b', 'title': ''}, {'video_id': 'a', 'title': 'x'}, {'video_id': 'c', 'title': None}]
        shared = sorted(data, key=lambda r: (not r['title'], (r['title'] or '').casefold(), r['video_id']))
        self.assertEqual([r['video_id'] for r in shared], ['a', 'b', 'c'])
        self.assertEqual(translate(v1({'sort_by': 'title', 'sort_order': 'asc'}), channel_id=CHANNEL).query['order_by'],
                         [{'field': 'video.title', 'direction': 'asc', 'nulls': 'last'}])

    def test_local_year_boundaries_in_shifting_zones(self):
        for zone_name, years in YEAR_ZONES:
            conditions = v1(facets={'published_years': years})
            translated = translate(conditions, channel_id=CHANNEL, time_zone=zone_name).query['where']
            for moment in year_moments(zone_name, years):
                row = {'video_id': 'x', 'published_at': moment}
                with self.subTest(zone=zone_name, moment=moment):
                    self.assertEqual(evaluate(translated, cells(row, zone_name)), instant(moment).astimezone(ZoneInfo(zone_name)).year in years)

    def test_leaf_count_counts_every_predicate(self):
        node = {'all': [leaf('a', 'is_known'), {'any': [leaf('b', 'eq', 'x'), {'not': leaf('c', 'is_unknown')}]}]}
        self.assertEqual(leaf_count(node), 3)


class GrowthOrderTests(unittest.TestCase):
    def test_views_gained_orders_nulls_last(self):
        for direction in ('asc','desc'):
            order = translate(v1({'sort_by':'views_gained','sort_order':direction}), channel_id=CHANNEL).query['order_by']
            self.assertEqual(order, [{'field':GAINED,'direction':direction,'nulls':'last'}])


class RouteTests(unittest.TestCase):
    """POST /saved/views/translate: the one adapter, reachable by the browser."""

    def post(self, body, **headers):
        from unittest.mock import Mock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from saved_views import create_saved_views_router
        app = FastAPI()
        repository = Mock()
        app.include_router(create_saved_views_router(repository))
        with TestClient(app) as client:
            response = client.post('/saved/views/translate', content=json.dumps(body),
                                   headers={'Content-Type': 'application/json', 'Sec-Fetch-Site': 'same-origin', **headers})
        self.assertEqual(repository.method_calls, [])  # Never a database or Saved object read.
        return response

    def test_translation_and_needs_capability_answers(self):
        conditions = v1({'q': 'a  b', 'published_from': '2024-01-01'})
        response = self.post({'channel_id': CHANNEL, 'conditions': conditions, 'time_zone': 'Europe/Berlin', 'video_type': 'short'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.json(), {'contract_version': 1, 'translation': translate(
            conditions, channel_id=CHANNEL, time_zone='Europe/Berlin', video_type='short').payload()})
        self.assertEqual(response.json()['translation']['query']['video_types'], ['short'])
        gained = self.post({'channel_id': CHANNEL, 'conditions': v1({'sort_by': 'views_gained'}), 'time_zone': 'UTC',
                            'video_type': 'long'}).json()['translation']
        self.assertEqual((gained['status'], gained['missing']), ('translated', []))
        self.assertEqual(gained['query']['order_by'], [{'field': GAINED, 'direction': 'desc', 'nulls': 'last'}])
        from unittest.mock import patch
        with patch('research_query.contracts.MAX_LEAVES', 1):
            missing = self.post({'channel_id': CHANNEL, 'conditions': v1({'q': 'a', 'min_views': '1'}), 'time_zone': 'UTC',
                                 'video_type': 'long'}).json()['translation']
        self.assertEqual((missing['status'], missing['query']), ('needs_capability', None))
        self.assertEqual(missing['missing'], [{'capability': 'query.where_leaf_budget', 'conditions': ['conditions']}])

    def test_invalid_input_and_origins_are_refused(self):
        from research_query.saved import from_query
        from test_research_query_contract import query
        _, v2 = from_query(query())
        good = {'channel_id': CHANNEL, 'conditions': v1(), 'time_zone': 'UTC', 'video_type': 'long'}
        for body in ({**good, 'conditions': v2}, {**good, 'time_zone': 'Mars/Olympus'}, {**good, 'time_zone': 3},
                     {**good, 'channel_id': 'UC1'}, {**good, 'conditions': v1({'q': 'x' * 101})},
                     {**good, 'video_type': 'live'}, {**good, 'video_type': None},
                     {key: value for key, value in good.items() if key != 'video_type'},
                     {key: value for key, value in good.items() if key != 'time_zone'}, {**good, 'extra': 1}):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual((response.status_code, response.json()['detail']['code']), (422, 'invalid_request'))
        self.assertEqual(self.post(good, **{'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.assertEqual(self.post(good, **{'Content-Type': 'text/plain'}).status_code, 422)

    def test_route_is_reviewed_local_user_work(self):
        from hot_runtime_routes import ROUTE_ACCESS, route_access
        self.assertEqual(ROUTE_ACCESS['POST /saved/views/translate'], 'user_work')
        self.assertEqual(route_access('POST', '/saved/views/translate'), 'user_work')
        self.assertEqual(route_access('GET', '/saved/views/translate'), 'user_work')  # The existing GET /{view_id}: 422.


if __name__ == '__main__':
    unittest.main()
