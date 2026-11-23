"""Browser wire fixtures from this tree's server code on a disposable native PostgreSQL 17.

The Library and research workshop parsers in the browser are tested offline (tests/test_library_*.mjs,
tests/test_workshop_*.mjs) against real server replies kept in tests/fixtures/*_wire.json. A reply
kept from an earlier run of the server code would let a browser parser go stale while every test
stays green, so this script owns those fixtures: it builds every request and reply again with the current server code
on the seeds of the native suites, one fresh database per seed:

  inspector  research_inspector_pg (without its 20,000-video channel)  -> library_inspector_wire.json
  overview   research_overview_pg                                      -> library_overview_wire.json
  videos     research_growth_pg (+ counters, categories and tags)      -> library_videos_wire.json,
                                                                          library_workshop_wire.json,
                                                                          the growth row of the Inspector
  home       research_home_pg                                          -> library_home_wire.json

Replies are what the browser receives: the service result with the HTTP read envelope of
research_query/http.py and web/middleware.py (source_read_at, source_expires_at). Requests are the ones the
browser modules build; the node tests assert that the browser builds exactly them.

Default (CI, tools/check_research_query_postgres.sh): check. Every committed request must equal the
request built here (opaque tokens and instants aside; the published years, which follow the seed's year,
as offsets from it) and every committed reply must have the wire shape
of the reply the current server gives (object keys at every path, value kinds, and the versions,
methods, operations and modes it names). Values that follow the clock (instants, today, calendar
buckets) are not compared, and the node tests read such values from the fixture. A difference fails
with the path: regenerate, then run the node tests (only literal counts or values they assert may change).

  --write    rebuild the fixtures (run on a disposable cluster, then run the node tests).

ONLY on a dedicated disposable cluster (LIC_RESEARCH_QUERY_DISPOSABLE=1): the database "leti-cook" and
the fixture roles are dropped and created again between seeds.
"""
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
FIXTURES = ROOT / 'tests' / 'fixtures'
FROZEN_REF = 'frozen_REF-from-a-page_0123456789'
ZONE = 'Europe/Berlin'
VIDEO_SOURCE = 'public.videos.current.v1'
# The browser's Library select (libraryData/wire.ts LIBRARY_SELECT), frozen calculations and growth.
LIBRARY_SELECT = ['video.id', 'video.title', 'video.thumbnail_url', 'video.published_at', 'video.duration_seconds',
                  'video.views', 'video.likes', 'video.comments', 'video.category_id']
DERIVED = ['video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate']
GROWTH = ['observation.growth_28d_status', 'observation.views_gained_28d', 'observation.views_per_day_28d',
          'observation.growth_28d_interval_seconds', 'observation.growth_28d_tracked_days']
FACET_GROUPS = ['views', 'duration', 'likes', 'comments', 'title_length', 'tag_count', 'published', 'category']
GROUP_FIELDS = {'views': ['video.views'], 'duration': ['video.duration_seconds'], 'likes': ['video.likes'],
                'comments': ['video.comments'], 'title_length': ['video.title_length'], 'tag_count': ['video.tag_count'],
                'published': ['video.published_date', 'video.published_year'], 'category': ['video.category_id']}
HOME_SELECT = ['video.id', 'video.channel_id', 'video.title', 'video.thumbnail_url', 'video.published_at',
               'video.views', 'video.likes', 'video.comments', 'video.lifetime_views_per_day']
# Kept from the committed file: the frozen session size at 20,000 synthetic rows, which is not a server reply.
CARRIED = {'library_videos_wire.json': ('frozen_session_bytes_20000_rows',)}


def canonical_body(value):
    from research_query.validation import canonical
    return canonical(value)


# ---------------------------------------------------------------------------
# One fresh database per seed.

def connection(dbname):
    import psycopg
    return psycopg.connect(host='127.0.0.1', port=int(os.environ['LIC_QUERY_TEST_PORT']), dbname=dbname, user='postgres',
                           password=os.environ['LIC_QUERY_TEST_PASSWORD'], autocommit=True)


def fresh_database():
    """Drop the seed's database and roles (created by research_query_pg.bootstrap) and create it empty."""
    from psycopg import sql
    with connection('postgres') as admin:
        admin.execute('DROP DATABASE IF EXISTS "leti-cook" WITH (FORCE)')
        for role in ('authenticator', 'lic_reader', 'service_role', 'authenticated', 'anon'):
            admin.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(role)))
        admin.execute('CREATE DATABASE "leti-cook"')


@contextmanager
def seeded(suite_class, **patches):
    fresh_database()
    with patch.multiple(suite_class, **patches) if patches else nullcontext():
        suite_class.setUpClass()
    try:
        yield suite_class
    finally:
        suite_class.tearDownClass()


def jsonable(value):
    return json.loads(json.dumps(value, default=str))


def http_reply(call):
    """What the browser receives from a research route (research_query/http.py, web/middleware.py): the reply, its
    valid_before registered as a read deadline, and the read envelope (source_read_at, source_expires_at)."""
    from hot_storage import capture_read_lifetime, iso_utc, register_read_deadline, utc_now
    from research_query.service import instant
    with capture_read_lifetime() as lifetime:
        result = call()
        register_read_deadline(instant(result['context']['valid_before']))
    value = jsonable(result)
    value['source_read_at'] = iso_utc(utc_now())
    value['source_expires_at'] = iso_utc(lifetime.expires_at)
    return value


# ---------------------------------------------------------------------------
# Seeds and their replies.

def inspector_replies():
    import research_inspector_pg as suite
    T = suite.InspectorPostgres
    with seeded(T, seed_big=classmethod(lambda cls, now: 0)):
        case = T('test_static_details_current_and_frozen_for_every_format')
        frozen, _now = case.freeze(suite.MAIN, 'long')
        operations = {}
        for name, kind, video, mode in (('video.static_details.v1', 'long', 'L0000000020', 'frozen'),
                                        ('video.observed_history.v1', 'long', 'L0000000020', 'frozen'),
                                        ('video.launch_comparison.v1', 'long', 'L0000000020', 'frozen'),
                                        ('video.lifetime_baseline.v1', 'long', 'L0000000020', 'frozen'),
                                        ('video.launch_comparison.v1', 'stream', 'S0000000005', 'query'),
                                        ('video.static_details.v1', 'short', 'H0000000001', 'query')):
            output = http_reply(lambda: case.operation(name, suite.MAIN, kind, video, frozen=frozen if mode == 'frozen' else None)[0])
            reference = {'kind': 'frozen', 'frozen_ref': FROZEN_REF} if mode == 'frozen' else {'kind': 'query'}
            operations[f'{name}:{kind}:{mode}'] = {
                'request': dict(version=1, operation=name, query=case.query(suite.MAIN, kind),
                                parameters={'video_id': video, 'population': reference}),
                'response': output}
        # The browser never sees another session's token: the fixture names one stable reference.
        return json.loads(json.dumps(operations).replace(frozen, FROZEN_REF))


def overview_replies():
    import research_overview_pg as fx
    with seeded(fx.OverviewPostgres) as T:
        out = {}

        def run(label, q, name, parameters):
            body = dict(version=1, operation=name, query=q, parameters=parameters)
            out[label] = {'request': body, 'response': http_reply(lambda: T.service.execute_operation(canonical_body(body)))}
        zoned = dict(fx.query(fx.MAIN), time={'mode': 'current', 'timezone': 'Europe/Kyiv'})
        run('fingerprint', fx.query(fx.MAIN), 'video.fingerprint.v1', {'period': '28d'})
        run('rhythm', zoned, 'video.publication_rhythm.v1', {'period': '28d'})
        run('years', zoned, 'video.publication_years.v1', {})
        run('breakdown', fx.query(fx.MAIN), 'video.content_breakdown.v1', {'period': 'max'})
        run('counter_change', fx.channel_query(fx.CC_FIVE), 'channel.counter_change.v1', {})
        run('format_totals', fx.channel_query(fx.TOTALS), 'channel.format_totals.v1', {})
        return out


def library_query(translated, *, fields=(), growth=(), size=50, anchor=None, after=None):
    """libraryData/wire.ts libraryQuery: the translated membership plus card fields, a frozen page."""
    page = {'size': size, 'after': after} if after else {'size': size, **({'anchor_id': anchor} if anchor else {})}
    return {'query_version': 1, 'source': {'id': VIDEO_SOURCE}, 'scope': {'kind': 'channel', 'id': translated['scope']['id']},
            'video_types': [translated['video_types'][0]], 'select': [*LIBRARY_SELECT, *fields, *growth],
            **({'where': translated['where']} if translated.get('where') is not None else {}),
            'order_by': [{'field': item['field'], 'direction': item['direction'], 'nulls': 'last'} for item in translated['order_by']],
            'time': {'mode': 'current', 'timezone': ZONE}, 'coverage': {'mode': 'available', 'required_fields': []},
            'consistency': 'frozen', 'page': page}


def node_fields(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        if isinstance(node.get('field'), str):
            found.add(node['field'])
        for key in ('all', 'any', 'not'):
            node_fields(node.get(key), found)
    elif isinstance(node, list):
        for item in node:
            node_fields(item, found)
    return found


def facets_request(translated, frozen_ref):
    """libraryData/videos.ts facetsRequest over a frozen list: each group without its own condition."""
    where = translated.get('where')
    items = [] if where is None else where['all'] if set(where) == {'all'} else [where]
    base, grouped = [], {}
    for item in items:
        fields = node_fields(item)
        group = next((name for name in FACET_GROUPS if fields and fields <= set(GROUP_FIELDS[name])), None)
        (grouped.setdefault(group, []) if group else base).append(item)
    join = lambda nodes: nodes[0] if len(nodes) == 1 else {'all': nodes}  # noqa: E731
    query = {'query_version': 1, 'source': {'id': VIDEO_SOURCE}, 'scope': {'kind': 'channel', 'id': translated['scope']['id']},
             'video_types': [translated['video_types'][0]], 'select': ['video.id'], **({'where': join(base)} if base else {}),
             'order_by': [{'field': item['field'], 'direction': item['direction'], 'nulls': 'last'} for item in translated['order_by']],
             'time': {'mode': 'current', 'timezone': ZONE}}
    parameters = {'groups': list(FACET_GROUPS), **({'conditions': {k: join(v) for k, v in grouped.items()}} if grouped else {}),
                  'population': {'kind': 'frozen', 'frozen_ref': frozen_ref}}
    return {'version': 1, 'operation': 'video.facets.v1', 'query': query, 'parameters': parameters}


def handoff_study(library_request, frozen_ref=None, ids=None):
    """workshopHandoff.ts: Library's executed query as the study's root query (current reads, no paging tokens).
    The whole selection opens as Source -> Gallery on Library's fixed list; selected IDs as Source -> Selection -> Table."""
    query = json.loads(json.dumps(library_request))
    query['page'] = {'size': query['page']['size']}
    query['consistency'] = 'current'
    if ids is None:
        nodes = [{'id': 'source', 'type': 'source', 'parameters': {}}, {'id': 'result', 'type': 'gallery', 'parameters': {}}]
        graph = {'version': 1, 'nodes': nodes, 'edges': [{'from': 'source', 'to': 'result', 'input': 'query', 'output': 'query'}],
                 'notes': '', 'layout': {'source': {'x': 24, 'y': 36}, 'result': {'x': 304, 'y': 36}}}
        run = {'consistency': 'frozen', 'frozen_ref': frozen_ref}
    else:
        query['where'] = None
        nodes = [{'id': 'source', 'type': 'source', 'parameters': {}},
                 {'id': 'selected', 'type': 'selection', 'parameters': {'mode': 'include', 'ids': list(ids)}},
                 {'id': 'result', 'type': 'table', 'parameters': {}}]
        graph = {'version': 1, 'nodes': nodes, 'edges': [{'from': 'source', 'to': 'selected', 'input': 'query', 'output': 'query'},
                                                         {'from': 'selected', 'to': 'result', 'input': 'query', 'output': 'query'}],
                 'notes': '', 'layout': {'source': {'x': 24, 'y': 36}, 'selected': {'x': 304, 'y': 36}, 'result': {'x': 584, 'y': 36}}}
        run = {'consistency': 'current'}
    return {'version': 1, 'query': query, 'graph': graph, 'run': run}


def videos_replies(check):
    import research_growth_pg as suite
    from research_query import growth
    from research_query.library_v1 import translate
    from research_query.validation import QueryRejected
    from saved_work import translate_library_conditions
    presets = json.loads((ROOT / 'application' / 'shared' / 'catalogue-filter-presets.json').read_text(encoding='utf-8'))
    T = suite.GrowthPostgres
    fresh_database()
    # Statuses that depend on the UTC date may cross midnight while a check seeds: shapes do not change.
    with patch.object(suite.time, 'sleep', lambda seconds: None) if check else nullcontext():
        T.setUpClass()
    try:
        admin, service = T.admin, T.service
        # Realistic counters for the calculated rates, categories and tags (the catalogue benchmark).
        admin.execute("UPDATE public.videos SET like_count=greatest(views,0)/40, comment_count=greatest(views,0)/400, "
                      "category_id=CASE WHEN video_id LIKE 'L%%' THEN '22' ELSE '24' END, tags=ARRAY['growth','fixture'] "
                      "WHERE channel_id=%s", (suite.CHANNEL,))
        admin.execute("UPDATE public.videos SET like_count=views/50, comment_count=views/500, category_id='27', "
                      "tags=ARRAY['a','b'] WHERE video_id LIKE 'gZ%%'")
        T.refresh()
        out, handoff = {}, {}
        run = lambda request: http_reply(lambda: service.execute(canonical_body(request)))  # noqa: E731
        operate = lambda request: http_reply(lambda: service.execute_operation(canonical_body(request)))  # noqa: E731

        def plain(kind, select, *, order=None, size=50, zone=ZONE, page=None):
            return {'query_version': 1, 'source': {'id': VIDEO_SOURCE}, 'scope': {'kind': 'channel', 'id': suite.CHANNEL},
                    'video_types': [kind], 'select': select,
                    'order_by': order or [{'field': 'video.published_at', 'direction': 'desc'}],
                    'time': {'mode': 'current', 'timezone': zone}, 'coverage': {'mode': 'available', 'required_fields': []},
                    'consistency': 'frozen', 'page': page or {'size': size}}
        gained = [{'field': growth.GAINED, 'direction': 'desc', 'nulls': 'last'}]
        # One frozen session of the scenario channel: the first page at an anchor, next, previous, every id.
        probe = run(plain('long', ['video.id'], size=200, order=gained))
        ids = [row['id'] for row in probe['rows']]
        first_request = plain('long', LIBRARY_SELECT + DERIVED + ['video.tags'] + GROWTH, order=gained,
                              page={'size': 4, 'anchor_id': ids[min(5, len(ids) - 1)]})
        first = run(first_request)
        out['long_first_anchor'] = {'request': first_request, 'response': first}
        for name, cursor in (('long_next', 'next_cursor'), ('long_previous', 'previous_cursor')):
            request = dict(first_request, page={'size': 4, 'after': first['page'][cursor]})
            out[name] = {'request': request, 'response': run(request)}
        ids_request = {'version': 1, 'frozen_ref': first['context']['frozen_ref']}
        out['long_frozen_ids'] = {'request': ids_request, 'response': http_reply(lambda: service.frozen_ids(canonical_body(ids_request)))}
        for kind in ('short', 'stream'):
            request = plain(kind, LIBRARY_SELECT + DERIVED + GROWTH)
            out[f'{kind}_first'] = {'request': request, 'response': run(request)}
        request = plain('long', list(LIBRARY_SELECT), zone='UTC')
        out['long_plain'] = {'request': request, 'response': run(request)}
        facet_query = {key: value for key, value in plain('long', ['video.id']).items() if key not in ('consistency', 'page', 'coverage')}
        facet_request = {'version': 1, 'operation': 'video.facets.v1', 'query': facet_query,
                         'parameters': {'population': {'kind': 'query'}, 'groups': FACET_GROUPS + ['growth']}}
        out['facets_query'] = {'request': facet_request, 'response': operate(facet_request)}
        # The filtered Library list as the browser builds it (translation + libraryQuery), its facets and static details.
        filters = {'q': '', 'category_id': '', 'published_from': '', 'published_before': '', 'min_views': '', 'max_views': '',
                   'min_duration_seconds': '', 'max_duration_seconds': '', 'sort_by': 'published_at', 'sort_order': 'desc'}
        title = [item['key'] for item in presets['title_length']]
        tags = [item['key'] for item in presets['tag_count']]
        # The seed follows the clock, so its years do too: the seed's year in the Library zone and the one before
        # hold its newest rows on any date (the comparison takes them as offsets from the first one, seed_year).
        year = T.T.astimezone(ZoneInfo(ZONE)).year
        conditions = {'version': 1, 'filters': filters, 'facets': {
            'version': 1, 'view_buckets': [presets['views'][0]['key'], presets['views'][1]['key']], 'duration_buckets': [],
            'published_years': [year, year - 1], 'published_older_than': None, 'cohort': None,
            'title_length_buckets': title[:3], 'tag_count_buckets': tags[:2]}}
        translated = translate(conditions, channel_id=suite.CHANNEL, time_zone=ZONE, video_type='long').payload()
        assert translated['status'] == 'translated', translated
        list_request = library_query(translated['query'], fields=DERIVED, growth=GROWTH)
        listed = run(list_request)
        # The requests below name the seed's newest rows (gLbg0000000, then gLtoday0000): research_growth_pg keeps
        # its order by publication the same at any time of day.
        ref = listed['context']['frozen_ref']
        frozen_facets = facets_request(translated['query'], ref)
        out['facets_frozen'] = {'conditions': conditions, 'translated': translated['query'], 'list_request': list_request,
                                'request': frozen_facets, 'response': operate(frozen_facets),
                                'list_total': listed['page']['total_count']}
        static_request = {'version': 1, 'operation': 'video.static_details.v1',
                          'query': {'query_version': 1, 'source': {'id': VIDEO_SOURCE}, 'scope': {'kind': 'channel', 'id': suite.CHANNEL},
                                    'video_types': ['long'], 'select': ['video.id'], 'order_by': [{'field': 'video.id', 'direction': 'asc'}]},
                          'parameters': {'video_id': listed['rows'][0]['id'], 'population': {'kind': 'frozen', 'frozen_ref': ref}}}
        out['static_frozen_tag_count'] = {'request': static_request,
                                          'response': operate(static_request)}
        body = {'channel_id': suite.CHANNEL, 'time_zone': ZONE, 'video_type': 'long', 'conditions': {
            'version': 1, 'filters': dict(filters, sort_by='views'),
            'facets': {'version': 1, 'view_buckets': ['1k_10k'], 'duration_buckets': [], 'published_years': [year],
                       'published_older_than': None, 'cohort': None}}}
        out['translate'] = {'request': body, 'response': jsonable(translate_library_conditions(body))}

        # The Inspector's "Observed views": a real row with a partial 28-day window (the longest such history).
        rows = run(plain('long', ['video.id', *GROWTH], size=200, zone='UTC'))['rows']
        partial = [row for row in rows if row['cells'][GROWTH[0]]['value'] == 'partial']
        growth_row = max(partial, key=lambda row: (int(row['cells'][GROWTH[4]]['value'] or 0), row['id']))
        growth_row = {'id': growth_row['id'], 'cells': {field: growth_row['cells'][field] for field in GROWTH}}

        # The workshop: Library hand-offs of every format run as the study the browser opens.
        for kind in ('long', 'short', 'stream'):
            translated = translate({'version': 1, 'filters': filters, 'facets': {
                'version': 1, 'view_buckets': [], 'duration_buckets': [], 'published_years': [], 'published_older_than': None,
                'cohort': None}}, channel_id=suite.CHANNEL, time_zone=ZONE, video_type=kind).payload()['query']
            # Eight rows a page keep the fixture small; Library sends 50 (the study keeps the size it gets).
            library_request = library_query(translated, fields=DERIVED, growth=GROWTH, size=8)
            library_reply = run(library_request)
            study = handoff_study(library_request, library_reply['context']['frozen_ref'])
            try:
                reply = http_reply(lambda: service.execute_graph(canonical_body(study)))
            except QueryRejected as error:
                raise AssertionError(f'{kind}: the server refused the hand-off study: {error.code}') from error
            handoff[f'{kind}_selection'] = {'library_request': library_request, 'library_frozen': {
                'context': {key: library_reply['context'][key] for key in ('frozen_ref', 'evaluated_at', 'valid_before')},
                'page': {'total_count': library_reply['page']['total_count']}}, 'request': study, 'response': reply}
            if kind == 'long':
                chosen = [row['id'] for row in library_reply['rows'][:2]]
                study = handoff_study(library_request, ids=chosen)
                handoff['long_selected_ids'] = {'library_request': library_request, 'ids': chosen, 'request': study,
                                                'response': http_reply(lambda: service.execute_graph(canonical_body(study)))}
        return out, handoff, growth_row
    finally:
        T.tearDownClass()


def home_replies():
    import research_home_pg as suite

    def feed(hours, where=None):
        return {'query_version': 1, 'source': {'id': VIDEO_SOURCE}, 'scope': {'kind': 'library'}, 'video_types': ['long'],
                'select': list(HOME_SELECT), 'where': where or {'field': 'video.published_at', 'op': 'within_last_hours_v1', 'value': hours},
                'order_by': [{'field': 'video.published_at', 'direction': 'desc', 'nulls': 'last'},
                             {'field': 'video.id', 'direction': 'desc', 'nulls': 'last'}],
                'time': {'mode': 'current', 'timezone': 'UTC'}, 'coverage': {'mode': 'available', 'required_fields': []},
                'consistency': 'current', 'page': {'size': 20}}
    with seeded(suite.HomePostgres) as T:
        first_request = feed('168')
        first = http_reply(lambda: T.service.execute(canonical_body(first_request)))
        # libraryData/home.ts: "Load 20 more" continues after the last row and every shown row at its instant.
        last = first['rows'][-1]['cells']['video.published_at']['value']
        tied = sorted(row['id'] for row in first['rows'] if row['cells']['video.published_at']['value'] == last)
        next_request = feed('168', {'all': [{'field': 'video.published_at', 'op': 'within_last_hours_v1', 'value': '168'},
                                            {'field': 'video.published_at', 'op': 'lte', 'value': last},
                                            {'not': {'field': 'video.id', 'op': 'in', 'value': tied}}]})
        pace_request = {'version': 1, 'operation': 'video.view_pace_median.v1', 'query': feed('168'), 'parameters': {}}
        return {
            'feed_7d_first': {'request': first_request, 'response': first},
            'feed_7d_next': {'request': next_request, 'key': {'published_at': last, 'tied': tied},
                             'response': http_reply(lambda: T.service.execute(canonical_body(next_request)))},
            'pace_7d': {'request': pace_request, 'response': http_reply(lambda: T.service.execute_operation(canonical_body(pace_request)))},
        }


def build(check=False):
    inspector = inspector_replies()
    overview = overview_replies()
    videos, handoff, growth_row = videos_replies(check)
    home = home_replies()
    return {
        'library_inspector_wire.json': {
            'frozen_ref': FROZEN_REF, 'operations': inspector, 'growth_row': growth_row},
        'library_overview_wire.json': {'operations': overview},
        'library_videos_wire.json': {**videos},
        'library_workshop_wire.json': {**handoff},
        'library_home_wire.json': {**home},
    }


# ---------------------------------------------------------------------------
# Comparison: requests exactly (tokens and instants aside, published years from the seed's year), replies by wire shape.

INSTANT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$')
TOKENS = frozenset({'frozen_ref', 'after', 'next_cursor', 'previous_cursor', 'anchor_id'})
# Keys whose values name a contract (versions, methods, operations, modes): compared as values.
PINNED = frozenset({'registry_version', 'source_schema_version', 'query_version', 'version', 'method', 'operation',
                    'source_id', 'consistency', 'basis', 'output'})
# Objects keyed by data (provenance references, coverage reasons): their values are merged.
MAPS = frozenset({'provenance', 'reasons'})
REQUESTS = frozenset({'request', 'list_request', 'library_request', 'conditions', 'translated', 'key', 'ids'})


def seed_year(document):
    """The year a document's published years follow: the first year of its filtered Library list (videos_replies)."""
    return ((document.get('facets_frozen') or {}).get('conditions') or {}).get('facets', {}).get('published_years', [None])[0]


def offsets(value, year):
    """Published years as offsets from the seed's year, each keeping its JSON type (number or text)."""
    if isinstance(value, list):
        return [offsets(item, year) for item in value]
    if isinstance(value, int) and not isinstance(value, bool):
        return value - year
    return str(int(value) - year) if isinstance(value, str) and value.isdigit() else value


def normalized(value, key=None, year=None):
    if isinstance(value, dict):
        if year is not None and value.get('field') == 'video.published_year':
            value = dict(value, value=offsets(value.get('value'), year))
        return {k: normalized(v, k, year) for k, v in value.items()}
    if isinstance(value, list):
        if year is not None and key == 'published_years':
            return offsets(value, year)
        return [normalized(v, year=year) for v in value]
    if isinstance(value, str) and (key in TOKENS or INSTANT.match(value)):
        return '<token>' if key in TOKENS else '<instant>'
    return value


def signature(value, path='$', sig=None):
    sig = {} if sig is None else sig
    add = lambda p, item: sig.setdefault(p, set()).add(item)  # noqa: E731
    name = path.rsplit('.', 1)[-1]
    if isinstance(value, dict):
        add(path, 'object')
        for key, item in value.items():
            if name in MAPS:
                signature(item, path + '.*', sig)
            else:
                add(path + '{}', key)
                signature(item, f'{path}.{key}', sig)
    elif isinstance(value, list):
        add(path, 'array')
        for item in value:
            signature(item, path + '[]', sig)
    else:
        add(path, 'null' if value is None else 'boolean' if isinstance(value, bool)
            else 'number' if isinstance(value, (int, float)) else 'string')
        if name in PINNED and not isinstance(value, (dict, list)):
            add(path + '=', json.dumps(value))
    return sig


def split(value, path='$', requests=None, year=None):
    """The request parts (compared exactly) and the rest (compared by shape)."""
    requests = {} if requests is None else requests
    if isinstance(value, dict):
        rest = {}
        for key, item in value.items():
            if key in REQUESTS:
                requests[f'{path}.{key}'] = normalized(item, year=year)
            else:
                # A reply is compared by its shape only, never as a request.
                rest[key] = item if key == 'response' else split(item, f'{path}.{key}', requests, year)[1]
        return requests, rest
    return requests, value


def differences(name, committed, current):
    problems = []
    for key in CARRIED.get(name, ()):
        committed, current = {k: v for k, v in committed.items() if k != key}, {k: v for k, v in current.items() if k != key}
    old_requests, old_rest = split(committed, year=seed_year(committed))
    new_requests, new_rest = split(current, year=seed_year(current))
    for path in sorted(set(old_requests) | set(new_requests)):
        if old_requests.get(path) != new_requests.get(path):
            problems.append(f'{name} {path}: the request differs from the one built from the current code')
    old, new = signature(old_rest), signature(new_rest)
    for path in sorted(set(old) | set(new)):
        if path.endswith('{}') or path.endswith('='):
            # Keys of an object (or a contract value) seen on one side only: a changed wire shape.
            # An object present on one side only (null or an empty list on the other) is not compared.
            parent = path[:-2] if path.endswith('{}') else path[:-1]
            if parent in old and parent in new and old.get(path, set()) != new.get(path, set()):
                kind = 'keys' if path.endswith('{}') else 'values'
                problems.append(f'{name} {parent}: {kind} {sorted(old.get(path, set()))} in the fixture, '
                                f'{sorted(new.get(path, set()))} from the current server')
        elif path in old and path in new:
            a, b = old[path] - {'null'}, new[path] - {'null'}
            if a and b and a != b:
                problems.append(f'{name} {path}: {sorted(a)} in the fixture, {sorted(b)} from the current server')
    return problems


def dump(value):
    return (json.dumps(value, indent=1, ensure_ascii=True) + '\n').encode('utf-8')


def main(argv):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
        raise RuntimeError('disposable_cluster_required')
    write = argv[1:] == ['--write']
    if argv[1:] and not write:
        raise SystemExit('usage: library_wire_fixtures_pg.py [--write]')
    fixtures = build(check=not write)
    problems = []
    for name, value in fixtures.items():
        path = FIXTURES / name
        committed = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if write:
            for key in CARRIED.get(name, ()):
                value[key] = committed[key]
            path.write_bytes(dump(value))
            print('written', path, flush=True)
        elif committed is None:
            problems.append(f'{name}: missing; run with --write')
        else:
            problems.extend(differences(name, committed, value))
    if problems:
        print('The committed browser wire fixtures no longer match the server:', *problems,
              'Regenerate them (tests/library_wire_fixtures_pg.py --write on a disposable cluster) and update the node tests.',
              sep='\n', flush=True)
        return 1
    print(json.dumps({'stage': 'library_wire_fixtures', 'fixtures': sorted(fixtures), 'status': 'pass' if not write else 'written'}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
