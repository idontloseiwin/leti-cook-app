"""Frozen query results (consistency "frozen") without a database.

The repository is a fake that returns what the frozen SQL statement returns;
tests/research_frozen_pg.py proves the SQL itself on PostgreSQL 17.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gc
import json
from pathlib import Path
import sys
import threading
import time
import tracemalloc
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from research_query.contracts import MAX_FROZEN_ROWS, MAX_RESPONSE_BYTES, QueryRequest
from research_query.cursor import CursorCodec
from research_query.frozen import FROZEN_TTL_SECONDS, FrozenStore
from research_query.frozen_service import _freeze, _in_budget, conditions_key
from research_query.http import create_router
from research_query.operations import OPERATIONS
from research_query.service import QueryService
from research_query.validation import AccessContext, QueryRejected, canonical, capabilities, validate
from test_research_query_contract import query

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
CHANNEL, OTHER = 'UC' + 'a'*22, 'UC' + 'b'*22
CONTEXT = AccessContext('fixture', 'owner-v1', enabled=True, allowed_operations=frozenset(OPERATIONS))
SELECT = ['video.id', 'video.title', 'video.views']


def source_row(n, **changes):
    row = {'video_id': 'a%010d' % n, 'channel_id': CHANNEL, 'video_type': 'long',
           'identity_clock': NOW-timedelta(hours=1), 'identity_expiry': NOW+timedelta(hours=695),
           'metadata_checked_at': NOW-timedelta(hours=1), 'checked_at': NOW-timedelta(hours=1),
           'title': 'Title %d' % n, 'title_state': 'known', 'views': 1000-n, 'views_state': 'known', 'sort_keys': None}
    row.update(changes)
    return row


def frozen_data(count=120, **changes):
    data = {'source_exists': True, 'scope_revision': 'a'*64, 'channels_in_scope': 1, 'channels_complete': 1,
            'rejected_rows': 0, 'expiring_rows': 0, 'required_missing': 0, 'unresolved_collection_members': 0,
            'hidden_collection_members': 0, 'evaluated_at': NOW, 'fresh_until': NOW+timedelta(seconds=FROZEN_TTL_SECONDS),
            'frozen_count': count, 'scope_channels': [CHANNEL], 'rows': [source_row(n) for n in range(count)]}
    data.update(changes)
    return data


def frozen_query(**changes):
    q = query(scope={'kind': 'channel', 'id': CHANNEL}, select=list(SELECT), consistency='frozen', page={'size': 50})
    q.update(changes)
    return q


class Repository:
    def __init__(self, data=None):
        self.data = data or frozen_data()
        self.reads = self.rechecks = 0
        self.revision = 'a'*64
        self.on_read = lambda: None
        self.gate = None

    def read_frozen(self, plan, *, deadline, cancelled):
        self.reads += 1
        self.on_read()
        if self.gate is not None:
            self.gate.wait(5)
        return deepcopy(self.data)

    def recheck_scope(self, plan, revision, *, deadline, cancelled):
        self.rechecks += 1
        if revision != self.revision:
            raise QueryRejected('cursor_context_changed')


class FrozenTests(unittest.TestCase):
    def setUp(self):
        self.now, self.mono, self.context = NOW, 1000.0, CONTEXT
        self.repo = Repository()
        self.store = FrozenStore(clock=lambda: self.now, monotonic=lambda: self.mono, auto_prune=False)
        self.codec = CursorCodec()
        self.service = self.make_service()

    def make_service(self, repo=None, store=None, codec=None):
        return QueryService(repo or self.repo, lambda: self.context, clock=lambda: self.now,
                            codec=codec or self.codec, frozen=store or self.store)

    def execute(self, q=None, service=None, **page):
        q = q or frozen_query()
        if page:
            q = {**q, 'page': {**q['page'], **page}}
        return (service or self.service).execute(canonical(q))

    def rejected(self, code, call, path=None):
        with self.assertRaises(QueryRejected) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if path is not None:
            self.assertEqual(caught.exception.path, path)

    def ids(self, page):
        return [row['id'] for row in page['rows']]

    def test_packed_rows_reject_malformed_shape_or_count_without_expanding_all_rows(self):
        from research_query.frozen_service import _json_rows
        stream = _json_rows(' [ [1], ["a,b", [2]], broken ] ', 3)
        self.assertEqual(next(stream), [1])
        self.assertEqual(next(stream), ["a,b", [2]])
        with self.assertRaises(ValueError):
            next(stream)
        for text, count in (('[]', 1), ('[[1]]', 0), ('[[1],]', 1), ('[[1]] extra', 1), ('[3]', 1)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(_json_rows(text, count))
        self.assertEqual(list(_json_rows('[]', 0)), [])

    def test_disconnected_client_stops_a_build_during_row_packing(self):
        from unittest.mock import patch
        from research_query.frozen import compact_cells
        self.repo.data = frozen_data(1000)
        packed = 0

        def pack(row_id, cells):
            nonlocal packed
            packed += 1
            return compact_cells(row_id, cells)

        with patch('research_query.frozen_service.compact_cells', pack):
            self.rejected('query_budget_exceeded', lambda: self.service.execute(
                canonical(frozen_query()), cancelled=lambda: packed > 0))
        self.assertGreater(packed, 0)
        self.assertLessEqual(packed, 128)
        self.assertEqual(self.store.usage(), {'sessions': 0, 'bytes': 0})

    def test_pages_next_previous_total_and_start_index_from_one_read(self):
        first = self.execute()
        self.assertEqual(first['context']['consistency'], 'frozen')
        self.assertEqual(first['warnings'], ['frozen_values_as_of_evaluated_at'])
        page = first['page']
        self.assertEqual((page['start_index'], page['total_count'], page['previous_cursor'], page['anchor_id']), (0, 120, None, None))
        self.assertTrue(page['has_more'])
        second = self.execute(after=page['next_cursor'])
        third = self.execute(after=second['page']['next_cursor'])
        self.assertEqual((second['page']['start_index'], third['page']['start_index']), (50, 100))
        self.assertEqual((len(third['rows']), third['page']['has_more'], third['page']['next_cursor']), (20, False, None))
        back = self.execute(after=third['page']['previous_cursor'])
        start = self.execute(after=back['page']['previous_cursor'])
        self.assertEqual((back['page']['start_index'], start['page']['start_index']), (50, 0))
        self.assertEqual(self.ids(back), self.ids(second))
        self.assertEqual(self.ids(start), self.ids(first))
        self.assertEqual(self.ids(first)+self.ids(second)+self.ids(third), ['a%010d' % n for n in range(120)])
        # One complete read; every later page came from memory after a scope check.
        self.assertEqual((self.repo.reads, self.repo.rechecks), (1, 5))
        self.assertEqual(second['context']['valid_before'], first['context']['valid_before'])

    def test_anchor_selects_the_first_page_and_is_echoed_only_when_present(self):
        anchored = self.execute(anchor_id='a%010d' % 75)
        self.assertEqual((anchored['page']['start_index'], anchored['page']['anchor_id']), (50, 'a%010d' % 75))
        self.assertIn('a%010d' % 75, self.ids(anchored))
        self.assertEqual(self.execute(anchor_id='a%010d' % 119)['page']['start_index'], 100)
        missing = self.execute(anchor_id='zzzzzzzzzzz')
        self.assertEqual((missing['page']['start_index'], missing['page']['anchor_id']), (0, None))
        # Anchors belong to a new frozen result of the same source grain only.
        for q in (frozen_query(consistency='current', page={'size': 50, 'anchor_id': 'a%010d' % 1}),
                  frozen_query(page={'size': 50, 'anchor_id': 'a%010d' % 1, 'after': missing['page']['next_cursor']}),
                  frozen_query(page={'size': 50, 'anchor_id': CHANNEL})):
            self.rejected('invalid_query', lambda: self.service.execute(canonical(q)))
        channels = query(source={'id': 'public.channels.current.v1'}, scope={'kind': 'channel', 'id': CHANNEL},
                         select=['channel.id'], order_by=[{'field': 'channel.id', 'direction': 'asc'}],
                         consistency='frozen', page={'size': 5, 'anchor_id': 'a%010d' % 1})
        channels.pop('video_types'); channels.pop('observations', None)
        self.rejected('invalid_query', lambda: self.service.execute(canonical(channels)))

    def test_lifetime_is_the_ttl_by_wall_and_monotonic_clock(self):
        first = self.execute()
        lifetime = datetime.fromisoformat(first['context']['valid_before'])-datetime.fromisoformat(first['context']['evaluated_at'])
        self.assertEqual(lifetime, timedelta(seconds=600))
        self.now = NOW+timedelta(seconds=599)
        self.assertEqual(self.execute(after=first['page']['next_cursor'])['page']['start_index'], 50)
        self.now = NOW+timedelta(seconds=600)
        self.rejected('cursor_expired', lambda: self.execute(after=first['page']['next_cursor']))
        # A wall clock set back cannot extend it: the monotonic deadline holds.
        self.now = NOW
        first = self.execute()
        self.now, self.mono = NOW-timedelta(hours=1), self.mono+600
        self.rejected('cursor_expired', lambda: self.execute(after=first['page']['next_cursor']))
        self.assertEqual(self.store.usage(), {'sessions': 0, 'bytes': 0})

    def test_only_admitted_sources_bound_the_lifetime(self):
        # Withheld values and rows never shorten it.
        rows = self.repo.data['rows']
        rows[3].update(checked_at=NOW-timedelta(hours=695, minutes=55), views_state='expired')
        self.repo.data['rejected_rows'] = 4
        first = self.execute()
        self.assertEqual(first['context']['valid_before'], '2026-10-29T12:10:00Z')
        cell = first['rows'][3]['cells']['video.views']
        self.assertEqual((cell['state'], cell['value']), ('expired', None))
        # A known value whose clock ends early still bounds it (defensive).
        rows[3].update(views_state='known')
        self.assertEqual(self.execute()['context']['valid_before'], '2026-10-29T12:05:00Z')
        rows[3].update(checked_at=NOW-timedelta(hours=1), type_expiry=NOW+timedelta(seconds=90))
        self.assertEqual(self.execute()['context']['valid_before'], '2026-10-29T12:01:30Z')
        # Never longer than the policy, never already over.
        self.repo.data['fresh_until'] = NOW+timedelta(seconds=601)
        self.rejected('backend_unavailable', self.execute)

    def test_withheld_rows_and_expired_values_are_counted(self):
        self.repo.data.update(rejected_rows=2, expiring_rows=3)
        for n in (1, 2):
            self.repo.data['rows'][n].update(title_state='expired')
        coverage = self.execute()['coverage']
        self.assertEqual(coverage['reasons'], {'source_rows_unavailable': 2, 'source_rows_expiring': 3, 'rows_with_expired_values': 2})
        self.assertEqual(coverage['status'], 'partial')
        self.rejected('incomplete_source', lambda: self.execute(frozen_query(coverage={'mode': 'require_complete'})))
        self.assertEqual(self.store.usage()['sessions'], 1)

    def test_lru_session_count_and_byte_limits(self):
        store = FrozenStore(max_sessions=2, clock=lambda: self.now, monotonic=lambda: self.mono, auto_prune=False)
        service = self.make_service(store=store)
        a, b = self.execute(service=service), self.execute(service=service)
        self.execute(service=service, after=a['page']['next_cursor'])  # a is now the most recent
        c = self.execute(service=service)
        self.rejected('cursor_expired', lambda: self.execute(service=service, after=b['page']['next_cursor']))
        for page in (a, c):
            self.assertEqual(self.execute(service=service, after=page['page']['next_cursor'])['page']['start_index'], 50)
        size = store.usage()['bytes']//2
        tiny = FrozenStore(max_session_bytes=size-1, clock=lambda: self.now, monotonic=lambda: self.mono, auto_prune=False)
        self.rejected('query_budget_exceeded', lambda: self.execute(service=self.make_service(store=tiny)), 'consistency')
        self.assertEqual(tiny.usage(), {'sessions': 0, 'bytes': 0})
        pair = FrozenStore(max_bytes=size*2-1, clock=lambda: self.now, monotonic=lambda: self.mono, auto_prune=False)
        service = self.make_service(store=pair)
        first = self.execute(service=service)
        self.execute(service=service)
        self.assertEqual(pair.usage()['sessions'], 1)
        self.rejected('cursor_expired', lambda: self.execute(service=service, after=first['page']['next_cursor']))

    def test_facet_cache_yields_to_sessions_and_drops_invalidated_values(self):
        query = frozen_query()
        first = self.execute(query)
        session = self.service.frozen_reference(first["context"]["frozen_ref"]).session
        store = FrozenStore(max_bytes=2 * session.size, clock=lambda: self.now,
                            monotonic=lambda: self.mono, auto_prune=False)
        service = self.make_service(store=store)
        first = self.execute(query, service=service)
        session = service.frozen_reference(first["context"]["frozen_ref"]).session
        before = store.usage()["bytes"]
        store.remember_facets(session, b"facet data")
        self.assertGreaterEqual(store.usage()["bytes"], before + len(b"facet data"))
        self.assertEqual(store.facet_values(session), b"facet data")
        self.execute(query, service=service)
        self.assertEqual(store.usage()["sessions"], 2)
        self.assertIsNone(store.facet_values(session))
        store.require(session)
        store.invalidate_channel(CHANNEL)
        store.remember_facets(session, b"stale")
        self.assertIsNone(store.facet_values(session))

    def test_third_build_runs_after_the_first_pair_releases_capacity(self):
        self.repo.gate = threading.Event()
        with ThreadPoolExecutor(3) as pool:
            jobs = [pool.submit(self.execute) for _ in range(3)]
            try:
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    with self.store._builds._condition:
                        if self.repo.reads == 2 and len(self.store._builds._waiting) == 1:
                            break
                    time.sleep(.005)
                else:
                    self.fail('expected two active builds and one queued')
            finally:
                self.repo.gate.set()
            pages = [job.result(3) for job in jobs]
        self.assertEqual([page['page']['total_count'] for page in pages], [120, 120, 120])
        self.assertEqual(self.repo.reads, 3)

    def test_invalidation_fences_builds_and_drops_only_matching_results(self):
        self.repo.on_read = lambda: self.store.invalidate_channel(OTHER)
        self.rejected('cursor_context_changed', self.execute)
        self.assertEqual(self.store.usage()['sessions'], 0)
        self.repo.on_read = lambda: None
        mine = self.execute()
        other_repo = Repository(frozen_data(10, scope_channels=[OTHER]))
        other_service = self.make_service(repo=other_repo)
        other = self.execute(frozen_query(scope={'kind': 'channel', 'id': OTHER}, page={'size': 5}), service=other_service)
        self.store.invalidate_channel(OTHER)
        self.rejected('cursor_expired', lambda: self.execute(frozen_query(scope={'kind': 'channel', 'id': OTHER}, page={'size': 5}),
                                                             service=other_service, after=other['page']['next_cursor']))
        self.assertEqual(self.execute(after=mine['page']['next_cursor'])['page']['start_index'], 50)
        self.store.invalidate_all()
        self.rejected('cursor_expired', lambda: self.execute(after=mine['page']['next_cursor']))

    def test_scope_or_access_change_refuses_and_retires_the_result(self):
        first = self.execute()
        self.repo.revision = 'b'*64  # hidden channel, moved folder member, collection edit
        self.rejected('cursor_context_changed', lambda: self.execute(after=first['page']['next_cursor']))
        self.repo.revision = 'a'*64
        self.rejected('cursor_expired', lambda: self.execute(after=first['page']['next_cursor']))
        first = self.execute()
        self.context = replace(CONTEXT, preferences_revision='changed')
        self.rejected('cursor_context_changed', lambda: self.execute(after=first['page']['next_cursor']))
        self.context = CONTEXT
        self.rejected('cursor_expired', lambda: self.execute(after=first['page']['next_cursor']))
        first = self.execute()
        self.context = replace(CONTEXT, enabled=False)
        self.rejected('policy_denied', lambda: self.execute(after=first['page']['next_cursor']))
        self.rejected('policy_denied', lambda: self.service.frozen_reference(first['context']['frozen_ref']))
        # A new owner access revision: neither pages nor the ID export.
        self.context = replace(CONTEXT, access_revision='owner-v2')
        reference = {'version': 1, 'frozen_ref': first['context']['frozen_ref']}
        self.rejected('cursor_context_changed', lambda: self.service.frozen_ids(canonical(reference)))
        self.context = CONTEXT
        self.rejected('cursor_expired', lambda: self.service.frozen_ids(canonical(reference)))

    def test_tampered_foreign_and_restarted_cursors(self):
        first = self.execute()
        cursor, ref = first['page']['next_cursor'], first['context']['frozen_ref']
        flipped = cursor[:-2] + ('A' if cursor[-2] != 'A' else 'B') + cursor[-1]
        for token in (flipped, ref, cursor+'x', 'not-a-cursor'):
            self.rejected('cursor_expired', lambda: self.execute(after=token))
        self.rejected('cursor_expired', lambda: self.execute(service=self.make_service(codec=CursorCodec()), after=cursor))
        # Modes never accept each other's continuations.
        self.rejected('cursor_expired', lambda: self.execute(frozen_query(consistency='current'), after=cursor))
        current = self.make_service(repo=type('Current', (), {
            'read_page': lambda s, *a, **k: {**frozen_data(2), 'valid_before': NOW+timedelta(seconds=120),
                                             'rows': [source_row(n, sort_keys=['a%010d' % n]) for n in range(2)]},
            'recheck_scope': lambda s, *a, **k: None})())
        current_cursor = self.execute(frozen_query(consistency='current', page={'size': 1}), service=current)['page']['next_cursor']
        self.rejected('cursor_expired', lambda: self.execute(after=current_cursor))
        # A cursor is bound to its query: projection and page size included.
        self.rejected('cursor_context_changed', lambda: self.execute(frozen_query(select=['video.id', 'video.views']), after=cursor))
        self.rejected('cursor_context_changed', lambda: self.execute(frozen_query(page={'size': 40}), after=cursor))
        self.rejected('cursor_expired', lambda: self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': cursor})))

    def test_matching_id_export_is_bounded_and_ordered(self):
        first = self.execute()
        exported = self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': first['context']['frozen_ref']}))
        self.assertEqual(exported['ids'], ['a%010d' % n for n in range(120)])
        self.assertEqual((exported['total_count'], exported['query_fingerprint']), (120, first['query_fingerprint']))
        self.assertEqual(exported['context'], {key: first['context'][key] for key in
                                               ('source_id', 'evaluated_at', 'valid_before', 'consistency', 'time_zone')})
        for raw, code in ((b'{"version":1}', 'invalid_query'), (b'{"version":2,"frozen_ref":"x"}', 'invalid_query'),
                          (b'{"version":1,"frozen_ref":"x","extra":1}', 'invalid_query'), (b'[]', 'invalid_query'),
                          (b'not json', 'invalid_query'), (b'x'*(64*1024+1), 'query_budget_exceeded'),
                          (b'{"version":1,"frozen_ref":"x"}', 'cursor_expired')):
            self.rejected(code, lambda: self.service.frozen_ids(raw))
        # The largest admitted result still fits one response.
        largest = Repository(frozen_data(MAX_FROZEN_ROWS))
        big = self.execute(service=self.make_service(repo=largest), size=200)
        exported = self.service.frozen_ids(canonical({'version': 1, 'frozen_ref': big['context']['frozen_ref']}))
        self.assertEqual(exported['total_count'], MAX_FROZEN_ROWS)
        self.assertLess(len(canonical(exported)), MAX_RESPONSE_BYTES)

    def test_over_limit_results_are_refused_never_truncated(self):
        self.repo.data.update(frozen_count=MAX_FROZEN_ROWS+1, rows=None)
        self.rejected('query_budget_exceeded', self.execute, 'consistency')
        self.repo.data = frozen_data(3)
        self.repo.data['frozen_count'] = 4  # rows disagree with the count
        self.rejected('backend_unavailable', self.execute)
        self.assertEqual(self.store.usage()['sessions'], 0)

    def test_too_large_refusals_name_the_limit_they_reached(self):
        # One code, query_budget_exceeded, for more than 50,000 rows, the 64 MiB session
        # store and the read's deadline; an added "limit" key says which, so the page does not claim
        # "more than 50,000 videos" for the other two. Older clients read only code and path.
        def payload(call):
            with self.assertRaises(QueryRejected) as caught:
                call()
            return caught.exception.payload()
        self.repo.data.update(frozen_count=MAX_FROZEN_ROWS+1, rows=None)
        self.assertEqual(payload(self.execute), {'code': 'query_budget_exceeded', 'path': 'consistency', 'limit': 'rows'})
        self.repo.data = frozen_data(120)
        self.execute()
        size = self.store.usage()['bytes']
        tiny = FrozenStore(max_session_bytes=size-1, clock=lambda: self.now, monotonic=lambda: self.mono, auto_prune=False)
        self.assertEqual(payload(lambda: self.execute(service=self.make_service(store=tiny))),
                         {'code': 'query_budget_exceeded', 'path': 'consistency', 'limit': 'memory'})
        # Once just the fixed-size row bookkeeping exceeds the store, the
        # service can stop before decoding even the first packed row.
        tiny = FrozenStore(max_session_bytes=64, clock=lambda: self.now, monotonic=lambda: self.mono, auto_prune=False)
        invalid = Repository(frozen_data(120, packed_video_rows=True, rows='deliberately unreadable'))
        self.assertEqual(payload(lambda: self.execute(service=self.make_service(repo=invalid, store=tiny))),
                         {'code': 'query_budget_exceeded', 'path': 'consistency', 'limit': 'memory'})
        self.assertEqual(tiny.usage(), {'sessions': 0, 'bytes': 0})
        session = next(iter(self.store._sessions.values()))
        self.assertEqual(payload(lambda: tiny.insert(session, tiny.generation())),
                         {'code': 'query_budget_exceeded', 'path': 'consistency', 'limit': 'memory'})
        self.assertEqual(payload(lambda: _in_budget(time.monotonic()-1, lambda: False)),
                         {'code': 'query_budget_exceeded', 'path': None, 'limit': 'time'})
        # A client that left is not called a time limit.
        self.assertEqual(payload(lambda: _in_budget(time.monotonic()+60, lambda: True)),
                         {'code': 'query_budget_exceeded', 'path': None})
        app = FastAPI()
        app.include_router(create_router(lambda request: self.make_service(store=tiny), lambda: None))
        with TestClient(app) as client:
            answer = client.post('/research/queries/execute', json=frozen_query())
        self.assertEqual((answer.status_code, answer.json()), (422, {'code': 'query_budget_exceeded', 'path': 'consistency', 'limit': 'memory'}))
        # A page over one response is a different bound: the response limit.
        for row in self.repo.data['rows']:
            row.update(tags=['x'*25000, 'y'*600], tags_state='known')
        self.assertEqual(payload(lambda: self.execute(frozen_query(select=SELECT+['video.tags'], page={'size': 100}))),
                         {'code': 'query_budget_exceeded', 'path': 'page', 'limit': 'response'})
        # Every other refusal keeps its exact old body.
        self.assertEqual(QueryRejected('cursor_expired').payload(), {'code': 'cursor_expired', 'path': None})
        for code, limit in (('cursor_expired', 'rows'), ('query_budget_exceeded', 'bytes'), ('query_budget_exceeded', '')):
            with self.assertRaises(ValueError):
                QueryRejected(code, limit=limit)
        from research_query.contracts import QueryError
        self.assertEqual(QueryError(code='query_budget_exceeded', path='consistency', limit='memory').limit, 'memory')
        with self.assertRaises(ValueError):
            QueryError(code='query_budget_exceeded', limit='bytes')

    def test_a_page_too_large_for_one_response_is_refused_at_build(self):
        self.repo.data = frozen_data(120)
        for row in self.repo.data['rows']:
            row.update(tags=['x'*25000, 'y'*600], tags_state='known')  # the 25,600-byte bound
        self.rejected('query_budget_exceeded', lambda: self.execute(frozen_query(select=SELECT+['video.tags'], page={'size': 100})), 'page')
        self.assertEqual(self.store.usage()['sessions'], 0)
        self.assertEqual(self.execute(frozen_query(select=SELECT+['video.tags'], page={'size': 50}))['page']['total_count'], 120)

    def test_dependent_read_reference(self):
        first = self.execute()
        reference = self.service.frozen_reference(first['context']['frozen_ref'])
        self.assertTrue(reference.contains('a%010d' % 7))
        self.assertFalse(reference.contains('zzzzzzzzzzz'))
        member = reference.member('a%010d' % 7)
        self.assertEqual(member.row, first['rows'][7])
        self.assertEqual((member.position, member.evaluated_at, member.video_type), (7, NOW, 'long'))
        self.assertEqual(member.valid_before, NOW+timedelta(seconds=600))
        self.assertEqual(member.conditions_key, reference.conditions_key)
        self.assertEqual(reference.ids()[:3], ('a0000000000', 'a0000000001', 'a0000000002'))
        self.rejected('source_unavailable', lambda: reference.member('zzzzzzzzzzz'))
        self.assertEqual(self.service.frozen_member(first['context']['frozen_ref'], 'a%010d' % 9).position, 9)
        reference.require_current()
        self.store.invalidate_channel(CHANNEL)
        self.rejected('cursor_expired', reference.require_current)
        self.rejected('cursor_expired', lambda: self.service.frozen_reference(first['context']['frozen_ref']))

    def test_conditions_key_ignores_sort_projection_and_paging_only(self):
        def key(q):
            return conditions_key(validate(canonical(q), CONTEXT))
        base = key(frozen_query())
        self.assertEqual(base, key(frozen_query(order_by=[{'field': 'video.title', 'direction': 'asc'}], select=['video.id'],
                                                 page={'size': 7}, consistency='current')))
        self.assertNotEqual(base, key(frozen_query(where={'field': 'video.views', 'op': 'gte', 'value': '1'})))
        self.assertNotEqual(base, key(frozen_query(time={'mode': 'current', 'timezone': 'Europe/Berlin'})))
        self.assertNotEqual(base, key(frozen_query(video_types=['short'])))

    def test_time_zone_is_validated_keyed_and_returned(self):
        for zone in ('UTC', 'Europe/Berlin', 'America/Argentina/Buenos_Aires', 'Pacific/Kiritimati', 'Etc/GMT-14'):
            self.assertEqual(QueryRequest.model_validate(frozen_query(time={'mode': 'current', 'timezone': zone})).time.timezone, zone)
        for zone in ('Mars/Phobos', '+03:00', 'UTC+3', '../etc/passwd', 'Europe/../Berlin', 'America', 'utc', '', 'Europe/Berlin ', 3):
            self.rejected('invalid_query', lambda: validate(canonical(frozen_query(time={'mode': 'current', 'timezone': zone})), CONTEXT))
        berlin = frozen_query(time={'mode': 'current', 'timezone': 'Europe/Berlin'})
        self.assertNotEqual(validate(canonical(berlin), CONTEXT).fingerprint, validate(canonical(frozen_query()), CONTEXT).fingerprint)
        self.assertEqual(self.execute(berlin)['context']['time_zone'], 'Europe/Berlin')
        self.assertEqual(self.execute()['context']['time_zone'], 'UTC')
        # A cursor of one zone is not a continuation of another.
        first = self.execute()
        self.rejected('cursor_context_changed', lambda: self.execute(berlin, after=first['page']['next_cursor']))

    def test_retained_memory_is_within_the_accounted_size(self):
        plan = validate(canonical(frozen_query(select=['video.id', 'video.channel_id', 'video.type', 'video.title',
                                                       'video.published_at', 'video.views'])), CONTEXT)
        data = frozen_data(0)
        rows = [source_row(n, published_at=NOW-timedelta(minutes=n), published_at_state='known', type_method='unknown',
                           checked_at=NOW-timedelta(hours=1, seconds=n)) for n in range(20000)]
        text = json.dumps(rows, default=lambda v: v.isoformat())
        gc.collect()
        tracemalloc.start()
        try:
            data.update(frozen_count=20000, rows=json.loads(text))  # IDs allocated while traced
            before = tracemalloc.get_traced_memory()[0]
            session = _freeze(self.service, plan, CONTEXT, data)
            data['rows'] = None
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        self.assertLessEqual(retained, session.size)
        self.assertLess(session.size, 64*1024*1024)
        self.assertGreater(before, retained)

    def test_frozen_sql_differs_only_by_horizon_and_final_select(self):
        from db.reads.research_sql import compile_query, compile_scope_recheck
        plan = validate(canonical(frozen_query(select=SELECT+['video.type', 'video.description', 'channel.title'],
                                                where={'field': 'video.title', 'op': 'contains_casefold_v1', 'value': 'x'})),
                        replace(CONTEXT, allowed_operations=frozenset(OPERATIONS)))
        statement, params = compile_query(plan)
        for fragment in ('fresh_until', 'AS admitted_now', 'AS expiring_rows', 'frozen_count', 'scope_channels', '%(frozen_rows)s'):
            self.assertIn(fragment, statement)
        self.assertEqual((params['horizon'], params['limit'], params['frozen_rows']),
                         (f'{FROZEN_TTL_SECONDS} seconds', MAX_FROZEN_ROWS+1, MAX_FROZEN_ROWS))
        self.assertNotIn('jsonb_build_array', statement)
        self.assertNotIn('120 seconds', statement.split('page AS MATERIALIZED')[1])
        current = compile_query(validate(canonical(frozen_query(consistency='current')), CONTEXT))[0]
        self.assertNotIn('fresh_until', current)
        self.assertEqual(compile_scope_recheck(plan), compile_scope_recheck(validate(canonical(frozen_query(consistency='current')), CONTEXT)))
        self.rejected('invalid_query', lambda: compile_query(plan, population=True))
        self.rejected('invalid_query', lambda: compile_query(plan, {'keys': ['1', 'a0000000001'], 's': 'a'*64}))

    def test_operations_and_graphs_do_not_take_a_frozen_query(self):
        from research_query.aggregate import validate_operation, SUMMARY
        from research_query.graph import validate_graph
        from db.reads.research_aggregate import compile_operation
        from test_research_graph import graph_document
        operation = validate_operation(canonical({'version': 1, 'operation': SUMMARY, 'query': frozen_query(), 'parameters': {}}), CONTEXT)
        self.rejected('invalid_query', lambda: compile_operation(operation))
        doc = graph_document()
        doc['query'] = {**doc['query'], 'consistency': 'frozen'}
        self.rejected('invalid_query', lambda: validate_graph(canonical(doc), CONTEXT))

    def test_capabilities_and_http_routes(self):
        self.assertEqual(capabilities(CONTEXT)['consistency_modes'], ['current', 'frozen'])
        self.assertEqual(capabilities(CONTEXT)['frozen'], {'ttl_seconds': 600, 'max_rows': MAX_FROZEN_ROWS, 'max_page_size': 200})
        authorized = []
        def owner():
            if not authorized:
                raise HTTPException(status_code=401)
        app = FastAPI()
        app.include_router(create_router(lambda request: self.service, owner))
        with TestClient(app) as client:
            self.assertEqual(client.post('/research/queries/frozen-ids', json={}).status_code, 401)
            authorized.append(True)
            first = client.post('/research/queries/execute', json=frozen_query())
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.headers['cache-control'], 'no-store')
            ref = first.json()['context']['frozen_ref']
            exported = client.post('/research/queries/frozen-ids', json={'version': 1, 'frozen_ref': ref})
            self.assertEqual((exported.status_code, len(exported.json()['ids'])), (200, 120))
            self.store.invalidate_all()
            for url, body in (('/research/queries/frozen-ids', {'version': 1, 'frozen_ref': ref}),
                              ('/research/queries/execute', {**frozen_query(), 'page': {'size': 50, 'after': first.json()['page']['next_cursor']}})):
                answer = client.post(url, json=body)
                self.assertEqual((answer.status_code, answer.json()), (409, {'code': 'cursor_expired', 'path': None}))

    def test_process_wiring_shares_one_store_with_the_invalidation_hooks(self):
        from web.cache import create_read_cache
        self.assertIsInstance(create_read_cache().research_frozen, FrozenStore)
        server = (ROOT/'application/server.py').read_text(encoding='utf-8')
        self.assertEqual(server.count('read_cache.research_frozen.invalidate_channel(channel_id)'), 2)
        self.assertIn('create_research_router(database, frozen=read_cache.research_frozen)', server)
        lifecycle = (ROOT/'application/web/lifecycle.py').read_text(encoding='utf-8')
        self.assertIn('RESEARCH_FROZEN.invalidate_all()', lifecycle)


if __name__ == '__main__':
    unittest.main()
