"""Research graph runs on fixed lists on native PG17.

ONLY in a dedicated disposable cluster: never auto-discovered and never given a
production DSN. All channels, videos and clocks are synthetic. The claims are
those of the frozen-list execution rules: a row block fed directly by the
source shows Library's frozen result for the same query (rows, order, values,
coverage, totals; only opaque tokens differ), a new fixed list equals a frozen
query on unchanged data, current blocks equal the query route, and fixed and
current reads differ only by sources that expire within the fixed lifetime.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
sys.path.insert(0, str(ROOT/'tests'))
from research_query.validation import AccessContext, QueryRejected, canonical
from research_query.service import QueryService
from research_query.frozen import FrozenStore
from research_query.admission import ReadAdmission
from research_query.cursor import CursorCodec
from research_query.operations import OPERATIONS
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresConfig, PostgresDatabase
from research_query_pg import bootstrap, seed
from research_frozen_pg import add_channel, add_videos
from test_research_query_contract import FIXTURE, query

BIG = 'UC' + 'Z'*22      # 20,000 long videos, 300 Shorts, 100 Streams.
SMALL = 'UC' + 'X'*22    # 40 long videos whose clocks a test may move.
LONG, SHORT, STREAM, FEW = 20000, 300, 100, 40
# Library's grid columns.
LIBRARY = ['video.id', 'video.title', 'video.thumbnail_url', 'video.published_at', 'video.duration_seconds',
           'video.views', 'video.likes', 'video.comments', 'video.category_id']
WINDOW = timedelta(hours=696)


def x(n):
    return 'x%010d' % n


class NativeGraphFrozenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        password, port = os.environ['LIC_QUERY_TEST_PASSWORD'], int(os.environ['LIC_QUERY_TEST_PORT'])
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres',
                                    password=password, autocommit=True)
        version = cls.admin.execute('SHOW server_version_num').fetchone()[0]
        if not 170000 <= int(version) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        now = cls.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        seed(cls.admin, now)  # The query fixture: a hidden channel, an incomplete inventory, stale rows.
        cls.big_folder, cls.small_folder = str(uuid.uuid4()), str(uuid.uuid4())
        for folder in (cls.big_folder, cls.small_folder):
            cls.admin.execute("INSERT INTO public.folders(id,name) VALUES(%s,'Graph frozen fixture')", (folder,))
        started = time.perf_counter()
        add_channel(cls.admin, BIG, cls.big_folder, now)
        add_videos(cls.admin, BIG, 'long', 1, LONG, now)
        add_videos(cls.admin, BIG, 'short', LONG+1, SHORT, now)
        add_videos(cls.admin, BIG, 'stream', LONG+SHORT+1, STREAM, now)
        add_channel(cls.admin, SMALL, cls.small_folder, now)
        add_videos(cls.admin, SMALL, 'long', 1, FEW, now, letter='x')
        seeded = time.perf_counter()
        cls.context = AccessContext('synthetic-installation', 'owner-1', enabled=True, allowed_operations=frozenset(OPERATIONS))
        cls.db = PostgresDatabase(PostgresConfig(f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=3),
                                  cadence_snapshot=__import__('collection_schedule').CollectionSchedules().cadence_snapshot(),
                                  admission=lambda _: None).open()
        cls.repo = ResearchQueryRepository(cls.db)
        cls.refresh()
        print('GRAPH_FIXED_FIXTURE ' + json.dumps({'videos': LONG+SHORT+STREAM+FEW, 'seed_s': round(seeded-started, 2),
                                         'refresh_s': round(time.perf_counter()-seeded, 2), 'server_version_num': version}), flush=True)

    @classmethod
    def refresh(cls):
        from db.inventory.search_projection import SearchProjectionRepository
        projection = SearchProjectionRepository(cls.db)
        while projection.refresh():
            pass

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    def setUp(self):
        # One API process: one cursor key, one admission, one frozen store. The
        # service and store clocks are injected (real time plus a shift).
        self.shift = timedelta(0)
        self.store = FrozenStore(clock=self.clock, monotonic=lambda: time.monotonic() + self.shift.total_seconds(),
                                 auto_prune=False)
        self.codec, self.admission = CursorCodec(), ReadAdmission(concurrency=2)
        self.builds = []
        self.service = self.make_service()
        self.started = time.perf_counter()

    def tearDown(self):
        print(f'TIMING {self.id().rsplit(".", 1)[-1]} {time.perf_counter()-self.started:.1f}s', flush=True)

    def clock(self):
        return datetime.now(timezone.utc) + self.shift

    def make_service(self, context=None):
        repo, builds = self.repo, self.builds

        class Counted:
            """The real repository; fixed-list builds are counted and timed."""
            def read_frozen(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    return repo.read_frozen(*args, **kwargs)
                finally:
                    builds.append((time.perf_counter()-started)*1000)

            def __getattr__(self, name):
                return getattr(repo, name)
        return QueryService(Counted(), lambda: context or self.context, clock=self.clock, codec=self.codec,
                            admission=self.admission, frozen=self.store)

    # Queries and studies.
    def library_query(self, scope=None, kind='long', **changes):
        q = query(scope=scope or {'kind': 'channel', 'id': BIG}, video_types=[kind], select=list(LIBRARY),
                  order_by=[{'field': 'video.published_at', 'direction': 'desc', 'nulls': 'last'}],
                  time={'mode': 'current', 'timezone': 'Europe/Moscow'}, consistency='frozen', page={'size': 50})
        q.update(changes)
        return q

    def study(self, q, blocks, run=None):
        root = {**deepcopy(q), 'consistency': 'current', 'page': {'size': q['page']['size']}}
        nodes, edges, layout = [dict(id='source', type='source', parameters={})], [], {'source': dict(x=0, y=0)}
        for index, (identity, kind, parameters, parent) in enumerate(blocks, 1):
            nodes.append(dict(id=identity, type=kind, parameters=deepcopy(parameters)))
            edges.append({'from': parent, 'to': identity, 'input': 'query', 'output': 'query'})
            layout[identity] = dict(x=200*index, y=0)
        document = dict(version=1, query=root, graph=dict(version=1, nodes=nodes, edges=edges, notes='', layout=layout))
        if run is not None:
            document['run'] = run
        return document

    def run_study(self, document, service=None):
        return (service or self.service).execute_graph(canonical(document))

    def query(self, q, service=None):
        return (service or self.service).execute(canonical(q))

    def next_page(self, q, page):
        return self.query({**q, 'page': {'size': q['page']['size'], 'after': page['page']['next_cursor']}})

    def frozen_ids(self, page):
        return self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': page['context']['frozen_ref']}))['ids']

    def rejected(self, code, call):
        """The whole run is refused with this code (cursor_* codes are HTTP 409)."""
        with self.assertRaises(QueryRejected) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if code.startswith('cursor_'):
            self.assertEqual(caught.exception.status, 409)

    # Comparisons.
    def position(self, token, reference=False):
        decoded = self.codec.decode_frozen(token, reference=reference)
        return decoded.session_id, decoded.offset

    def without_tokens(self, page):
        page = deepcopy(page)
        page['context'].pop('frozen_ref')
        for key in ('next_cursor', 'previous_cursor'):
            page['page'][key] = page['page'][key] is not None
        return page

    def same_page(self, page, expected):
        """Everything equal but the tokens, which name the same result and offsets."""
        self.assertEqual(self.without_tokens(page), self.without_tokens(expected))
        self.assertEqual(self.position(page['context']['frozen_ref'], True), self.position(expected['context']['frozen_ref'], True))
        for key in ('next_cursor', 'previous_cursor'):
            if expected['page'][key] is not None:
                self.assertEqual(self.position(page['page'][key]), self.position(expected['page'][key]))

    def same_rows(self, page, expected):
        """Two reads of unchanged data: the same rows, values, coverage and paging state."""
        for key in ('columns', 'rows', 'provenance', 'coverage', 'query_fingerprint'):
            self.assertEqual(page[key], expected[key], key)
        for key in ('has_more', 'start_index', 'total_count', 'anchor_id'):
            self.assertEqual(page['page'].get(key), expected['page'].get(key), key)

    @contextmanager
    def collection(self, videos):
        identity = str(uuid.uuid4())
        self.admin.execute("INSERT INTO public.saved_objects(kind,id,name,create_fingerprint) VALUES('collection',%s,'Fixed',%s)", (identity, 'c'*64))
        try:
            for video in videos:
                self.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                                   (identity, video))
            self.admin.execute("UPDATE public.saved_objects SET member_count=%s WHERE id=%s AND kind='collection'", (len(videos), identity))
            yield {'kind': 'collection', 'id': identity}
        finally:
            self.admin.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s', (identity,))
            self.admin.execute("DELETE FROM public.saved_objects WHERE kind='collection' AND id=%s", (identity,))

    # Check how a filtered list is passed to the next node.
    def test_fixed_run_reuses_library_frozen_result(self):
        q = self.library_query()
        library = self.query(q)
        self.assertEqual((library['page']['total_count'], len(self.builds)), (LONG, 1))
        started = time.perf_counter()
        result = self.run_study(self.study(q, (('result', 'gallery', {}, 'source'),),
                                           run={'consistency': 'frozen', 'frozen_ref': library['context']['frozen_ref']}))
        graph_ms = (time.perf_counter()-started)*1000
        self.assertEqual(len(self.builds), 1)  # The same list: no new read of rows.
        self.assertEqual((result['status'], result['context']['consistency']), ('ready', 'fixed_source_list'))
        self.assertEqual(result['run']['fixed'], {'reused': True, 'evaluated_at': library['context']['evaluated_at'],
                                                  'valid_before': library['context']['valid_before'], 'total_count': LONG})
        node = result['nodes']['result']
        self.assertEqual((node['consistency'], node['output']), ('frozen', 'page'))
        self.same_page(node['value'], library)
        self.assertEqual(node['value']['context']['time_zone'], 'Europe/Moscow')
        # Page 2 through the query route with the block's query equals Library's page 2.
        self.same_page(self.next_page(node['query'], node['value']), self.next_page(q, library))
        print('GRAPH_FIXED_REUSE ' + json.dumps({'rows': LONG, 'graph_ms': round(graph_ms, 1)}), flush=True)

    def test_fixed_run_without_reference_equals_frozen_query_at_same_now(self):
        big = [f'z{n:010d}' for n in range(1, 121)]
        with self.collection(big + ['d0000000001', 'q0000000001']) as collection:
            for scope, select in (({'kind': 'channel', 'id': BIG}, LIBRARY),
                                  # Hidden channel, operator hold, unfinished inventory, stale rows.
                                  (query()['scope'], query()['select']),
                                  (collection, LIBRARY)):
                with self.subTest(scope=scope['kind']):
                    q = self.library_query(scope, select=list(select))
                    builds = len(self.builds)
                    library = self.query(q)
                    result = self.run_study(self.study(q, (('result', 'table', {}, 'source'),), run={'consistency': 'frozen'}))
                    self.assertEqual(len(self.builds), builds + 2)
                    self.assertEqual(result['run']['fixed']['reused'], False)
                    block = result['nodes']['result']['value']
                    self.same_rows(block, library)
                    self.assertNotEqual(self.position(block['context']['frozen_ref'], True)[0],
                                        self.position(library['context']['frozen_ref'], True)[0])
                    self.assertEqual(self.frozen_ids(block), self.frozen_ids(library))
                    if scope['kind'] == 'collection':
                        self.assertEqual(block['page']['total_count'], 120)
                        self.assertEqual(block['coverage']['reasons'].get('collection_members_hidden'), 1)

    def test_current_graph_page_equals_query_route_page_and_continuation(self):
        q = self.library_query(consistency='current', order_by=[{'field': 'video.views', 'direction': 'desc', 'nulls': 'last'}])
        direct = self.query(q)
        plain = self.run_study(self.study(q, (('result', 'table', {}, 'source'),)))
        self.assertNotIn('run', plain)
        self.same_rows(plain['nodes']['result']['value'], direct)
        labelled = self.run_study(self.study(q, (('result', 'table', {}, 'source'),), run={'consistency': 'current'}))
        node = labelled['nodes']['result']
        self.assertEqual((node['consistency'], labelled['run']), ('current', {'consistency': 'current', 'fixed': None}))
        self.same_rows(node['value'], direct)
        self.assertEqual(self.store.usage()['sessions'], 0)
        following, expected = self.next_page(node['query'], node['value']), self.next_page(q, direct)
        self.same_rows(following, expected)
        self.assertEqual(following['rows'][0]['id'], expected['rows'][0]['id'])

    def test_fixed_and_current_differ_only_by_expiring_sources(self):
        q = self.library_query({'kind': 'channel', 'id': SMALL}, order_by=[{'field': 'video.views', 'direction': 'desc', 'nulls': 'last'}])
        saved = {row[0]: row[1:] for row in self.admin.execute(
            'SELECT video_id,checked_at,metadata_checked_at,availability_checked_at,availability_status FROM public.videos WHERE channel_id=%s', (SMALL,))}
        membership = self.admin.execute('SELECT first_seen_at,last_seen_at,last_present_at FROM public.video_inventory_memberships WHERE video_id=%s', (x(2),)).fetchone()
        ends_soon = "clock_timestamp()-interval '696 hours'+interval '300 seconds'"  # Ends 5 minutes from now.
        try:
            # x2 identity proof and x3 availability end inside the fixed lifetime (rows);
            # x4 counters and x5 metadata too (values); x6 is private; x7 counters are already stale.
            self.admin.execute(f'UPDATE public.video_inventory_memberships SET first_seen_at={ends_soon},last_seen_at={ends_soon},'
                               f'last_present_at={ends_soon} WHERE video_id=%s', (x(2),))
            for video, clock in ((x(3), 'availability_checked_at'), (x(4), 'checked_at'), (x(5), 'metadata_checked_at')):
                self.admin.execute(f'UPDATE public.videos SET {clock}={ends_soon} WHERE video_id=%s', (video,))
            self.admin.execute("UPDATE public.videos SET availability_status='private' WHERE video_id=%s", (x(6),))
            self.admin.execute("UPDATE public.videos SET checked_at=clock_timestamp()-interval '700 hours' WHERE video_id=%s", (x(7),))
            self.refresh()
            document = self.study(q, (('result', 'table', {}, 'source'),))
            fixed = self.run_study({**document, 'run': {'consistency': 'frozen'}})['nodes']['result']['value']
            current = self.run_study({**document, 'run': {'consistency': 'current'}})['nodes']['result']['value']
            self.assertFalse(fixed['page']['has_more'] or current['page']['has_more'])
            until = datetime.fromisoformat(fixed['context']['valid_before'])
            frozen_rows = {r['id']: r['cells'] for r in fixed['rows']}
            current_rows = {r['id']: r['cells'] for r in current['rows']}
            # Every row in only one of them is admitted now but its own source
            # (identity proof or availability) ends inside the fixed lifetime.
            difference = set(frozen_rows) ^ set(current_rows)
            self.assertLessEqual(set(frozen_rows), set(current_rows))
            for video in difference:
                availability = self.admin.execute('SELECT availability_checked_at FROM public.videos WHERE video_id=%s', (video,)).fetchone()[0]
                identity = datetime.fromisoformat(current_rows[video]['video.id']['expires_at'])
                self.assertLessEqual(min(identity, availability + WINDOW), until, video)
            self.assertLessEqual(len(difference), fixed['coverage']['reasons'].get('source_rows_expiring', 0))
            # Every differing value in a shared row is one whose own clock ends inside that lifetime.
            changed = set()
            for video in set(frozen_rows) & set(current_rows):
                for field, cell in frozen_rows[video].items():
                    if cell != current_rows[video][field]:
                        changed.add((video, field))
                        self.assertEqual(cell['state'], 'expired', (video, field))
                        self.assertLessEqual(datetime.fromisoformat(current_rows[video][field]['expires_at']), until, (video, field))
            # Without rows whose sort value differs, the order is the same.
            moved = {video for video, field in changed if field == 'video.views'}
            self.assertEqual([r['id'] for r in fixed['rows'] if r['id'] not in moved],
                             [r['id'] for r in current['rows'] if r['id'] in frozen_rows and r['id'] not in moved])
            # The fixture's own expectations: which rows and values moved, and nothing else.
            self.assertEqual(difference, {x(2), x(3)})
            self.assertEqual({video for video, _ in changed}, {x(4), x(5)})
            self.assertNotIn(x(6), current_rows)
            self.assertEqual(current_rows[x(7)]['video.views']['state'], 'expired')
            print('GRAPH_FIXED_DIFFERENCE ' + json.dumps({'rows_only_current': sorted(difference), 'values': sorted(map(list, changed)),
                                                 'source_rows_expiring': fixed['coverage']['reasons'].get('source_rows_expiring')}), flush=True)
        finally:
            for video, (checked, metadata, availability, status) in saved.items():
                self.admin.execute('UPDATE public.videos SET checked_at=%s,metadata_checked_at=%s,availability_checked_at=%s,availability_status=%s WHERE video_id=%s',
                                   (checked, metadata, availability, status, video))
            self.admin.execute('UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s WHERE video_id=%s', (*membership, x(2)))
            self.refresh()

    def test_reference_expiry_invalidation_and_context_change(self):
        q = self.library_query({'kind': 'channel', 'id': SMALL})
        def fixed_run(ref, service=None, study_query=None):
            return self.run_study(self.study(study_query or q, (('result', 'table', {}, 'source'),),
                                             run={'consistency': 'frozen', 'frozen_ref': ref}), service)
        ref = self.query(q)['context']['frozen_ref']
        self.assertEqual(fixed_run(ref)['run']['fixed']['reused'], True)
        builds = len(self.builds)
        # +601 s: expired (409), never replaced by a new list.
        self.shift = timedelta(seconds=601)
        self.rejected('cursor_expired', lambda: fixed_run(ref))
        self.shift = timedelta(0)
        self.assertEqual(len(self.builds), builds)
        # A type decision or channel trash invalidates the channel's lists.
        ref = self.query(q)['context']['frozen_ref']
        self.store.invalidate_channel(SMALL)
        self.rejected('cursor_expired', lambda: fixed_run(ref))
        # Another owner access context: changed, and the list is retired.
        ref = self.query(q)['context']['frozen_ref']
        self.rejected('cursor_context_changed', lambda: fixed_run(ref, self.make_service(replace(self.context, access_revision='owner-2'))))
        self.rejected('cursor_expired', lambda: fixed_run(ref))
        # The folder lost its channel after the list was fixed: the scope changed.
        folder_q = self.library_query({'kind': 'folder', 'id': self.small_folder, 'include_descendants': False})
        ref = self.query(folder_q)['context']['frozen_ref']
        try:
            self.admin.execute('UPDATE public.channels SET folder_id=%s WHERE channel_id=%s', (self.big_folder, SMALL))
            self.rejected('cursor_context_changed', lambda: fixed_run(ref, study_query=folder_q))
        finally:
            self.admin.execute('UPDATE public.channels SET folder_id=%s WHERE channel_id=%s', (self.small_folder, SMALL))
        self.rejected('cursor_expired', lambda: fixed_run(ref, study_query=folder_q))  # Retired, not waiting.
        # Library's list of another sort is not this study's list; it stays Library's.
        library = self.query(q)
        resorted = self.library_query({'kind': 'channel', 'id': SMALL}, order_by=[{'field': 'video.title', 'direction': 'asc', 'nulls': 'last'}])
        self.rejected('cursor_context_changed', lambda: fixed_run(library['context']['frozen_ref'], study_query=resorted))
        self.assertEqual(self.frozen_ids(library)[:2], [r['id'] for r in library['rows'][:2]])

    def test_two_row_blocks_share_one_fixed_build(self):
        q = self.library_query()
        blocks = (('table_rows', 'table', {}, 'source'), ('all_rows', 'filter', {'where': None}, 'source'),
                  ('gallery_rows', 'gallery', {}, 'all_rows'))
        result = self.run_study(self.study(q, blocks, run={'consistency': 'frozen'}))
        self.assertEqual((len(self.builds), self.store.usage()['sessions']), (1, 1))
        table, gallery = (result['nodes'][i]['value'] for i in ('table_rows', 'gallery_rows'))
        self.same_page(gallery, table)
        self.assertEqual({result['nodes'][i]['consistency'] for i in ('table_rows', 'gallery_rows')}, {'frozen'})
        again = self.run_study(self.study(q, blocks, run={'consistency': 'frozen', 'frozen_ref': table['context']['frozen_ref']}))
        self.assertEqual(len(self.builds), 1)
        self.same_page(again['nodes']['gallery_rows']['value'], table)

    def test_selection_and_filter_paths_stay_current_and_say_so(self):
        q = self.library_query()
        library = self.query(q)
        picked = [r['id'] for r in library['rows'][3:8]]
        condition = {'field': 'video.views', 'op': 'gte', 'value': '4000'}
        blocks = (('fixed', 'table', {}, 'source'), ('picked', 'selection', {'mode': 'include', 'ids': picked}, 'source'),
                  ('chosen', 'table', {}, 'picked'), ('popular', 'filter', {'where': condition}, 'source'),
                  ('popular_rows', 'gallery', {}, 'popular'), ('summary', 'summary', {}, 'source'))
        result = self.run_study(self.study(q, blocks, run={'consistency': 'frozen', 'frozen_ref': library['context']['frozen_ref']}))
        nodes = result['nodes']
        self.assertEqual(result['status'], 'ready')
        self.assertEqual({i: nodes[i]['consistency'] for i in ('fixed', 'chosen', 'popular_rows', 'summary')},
                         {'fixed': 'frozen', 'chosen': 'current', 'popular_rows': 'current', 'summary': 'current'})
        self.same_page(nodes['fixed']['value'], library)
        current = {**q, 'consistency': 'current'}
        for identity, where in (('chosen', {'field': 'video.id', 'op': 'in', 'value': picked}), ('popular_rows', condition)):
            self.assertEqual(nodes[identity]['value']['context']['consistency'], 'current')
            self.same_rows(nodes[identity]['value'], self.query({**current, 'where': where}))
        self.assertEqual([r['id'] for r in nodes['chosen']['value']['rows']], picked)
        direct = self.service.execute_operation(canonical({'version': 1, 'operation': 'video.summary.v1',
                                                           'query': {**current, 'page': {'size': 50}}, 'parameters': {}}))
        self.assertEqual(nodes['summary']['value']['result'], direct['result'])
        self.assertEqual(nodes['summary']['value']['result']['population'], LONG)
        # Each block keeps its own lifetime; the run's is the earliest.
        lifetimes = {i: nodes[i]['value']['context']['valid_before'] for i in ('fixed', 'chosen', 'popular_rows', 'summary')}
        self.assertEqual(lifetimes['fixed'], library['context']['valid_before'])
        self.assertEqual(result['context']['valid_before'], min(lifetimes.values()))
        self.assertLess(result['context']['valid_before'], lifetimes['fixed'])

    def test_fixed_run_on_20000_video_channel_within_budget(self):
        timings = {}
        for kind, count in (('long', LONG), ('short', SHORT), ('stream', STREAM)):
            q = self.library_query(kind=kind)
            builds = len(self.builds)
            started = time.perf_counter()
            result = self.run_study(self.study(q, (('result', 'gallery', {}, 'source'),), run={'consistency': 'frozen'}))
            elapsed = (time.perf_counter()-started)*1000
            node = result['nodes']['result']
            self.assertEqual((node['state'], node['value']['page']['total_count']), ('ready', count))
            self.assertTrue(all(r['id'][0] == 'z' for r in node['value']['rows']))
            self.assertEqual(len(self.builds), builds + 1)
            self.assertLess(len(canonical(result)), 4*1024*1024)
            timings[kind] = {'graph_ms': round(elapsed), 'build_sql_ms': round(self.builds[-1]),
                             'node_ms': node['elapsed_ms'], 'response_bytes': len(canonical(result))}
            if kind == 'long':
                started = time.perf_counter()
                reused = self.run_study(self.study(q, (('result', 'gallery', {}, 'source'),),
                                                   run={'consistency': 'frozen', 'frozen_ref': node['value']['context']['frozen_ref']}))
                timings['long_reused_graph_ms'] = round((time.perf_counter()-started)*1000, 1)
                self.assertEqual(reused['run']['fixed']['reused'], True)
                self.assertLess(elapsed, 8000)  # Within FROZEN_BUILD_SECONDS, far within the 30 s run.
        timings['store'] = self.store.usage()
        print('GRAPH_FIXED_SCALE ' + json.dumps(timings), flush=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
