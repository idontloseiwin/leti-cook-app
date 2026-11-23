"""HTTP reader role on a disposable native PostgreSQL 17: an executable consumer of the reader-route manifest.

ONLY on a dedicated disposable cluster (LIC_RESEARCH_QUERY_DISPOSABLE=1; tools/check_research_query_postgres.sh
or a local runner). Exercises PROTECTED_ROUTES using the database reader role.

Roles, grants and schema are those of database/bootstrap.sh (research_query_pg.bootstrap). The backend logs in
as the NOINHERIT authenticator; every read-only transaction of an HTTP request runs SET LOCAL ROLE lic_reader
(db/base/postgres.py, is_http_read). Each case sends a real request through the middleware stack of server.py
(HttpReadScopeMiddleware, owner middleware, body limit, runtime middleware) to the shipped routers, and the
effective role of every transaction is read back from PostgreSQL itself (current_user, session_user,
transaction_read_only) right after the gateway applied its settings.

Every manifest route is a case here or is named in NOT_HERE with the reason; a new protected route fails
test_00 until it is placed. Writes: lic_reader holds no write privilege on any relation, sequence or schema and
executes no SECURITY DEFINER function; a write as lic_reader is refused by PostgreSQL even outside a read-only
transaction; a GET handler that tries a write is refused before SQL; a read-only operation that tries one is
refused by the database. No YouTube request is sent: the acquisition case answers from a recorded fixture.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import psycopg  # noqa: E402

import research_query_pg as native  # noqa: E402  (a module: unittest must not run its own tests here)
from research_query_pg import FIXTURE  # noqa: E402
from test_reader_role_routes import PROTECTED_ROUTES, READER_ROLE_READS  # noqa: E402
from test_research_graph import graph_document  # noqa: E402
from test_research_query_contract import query  # noqa: E402

ORIGIN = 'http://127.0.0.1:8002'
VISIBLE, HIDDEN = FIXTURE['channels'][0]['id'], FIXTURE['channels'][3]['id']
VISIBLE_VIDEO, HIDDEN_VIDEO = 'a0000000001', 'd0000000001'
GATEWAY_SETTINGS = "SELECT set_config('search_path'"
ROLE_PROBE = ("SELECT current_user::text AS role, session_user::text AS login, "
              "current_setting('transaction_read_only') AS read_only")
READER = ('lic_reader', 'authenticator', 'on')
# Protected routes this harness does not run, and why. Their reads follow the same gateway rule.
NOT_HERE = {
    'GET /channels/{channel_id}/backfill-current-stats': 'channel command router (web/channels.py) needs the collection runtime',
    'GET /channels/{channel_id}/videos/long/backfill-current-stats': 'same router',
    'GET /channels/{channel_id}/hydration': 'same router',
    'POST /channels/{channel_id}/backfill-current-stats': 'command: starts collection work',
    'POST /channels/{channel_id}/videos/long/backfill-current-stats': 'command: starts collection work',
    'POST /channels/{channel_id}/hydration': 'command: starts collection work',
    'POST /add-channel': 'command: resolves the channel at YouTube',
    'POST /starter-channels/add': 'command: resolves channels at YouTube',
    'POST /system/video-type-reviews/{case_id}/decision': 'command: a user decision write (service_role)',
}


class Recorder:
    def __init__(self):
        self.started = []

    def start_attempt(self, **values):
        self.started.append(values)
        return f'00000000-0000-4000-8000-{len(self.started):012d}'

    def finish_attempt(self, attempt_id, **values):
        return True


class ReaderRoleHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(CHANNEL_BASELINE_ANALYTICS_ENABLED='true', TELEMETRY_ENABLED='false')
        native.NativeQueryTests.setUpClass()
        cls.admin, cls.db = native.NativeQueryTests.admin, native.NativeQueryTests.db
        cls.admin.execute("UPDATE public.channels SET title='Synthetic channel '||right(channel_id,2),subscribers=12345,"
                          "views=987654,video_count=50,channel_metadata_status='ok',"
                          "channel_metadata_checked_at=clock_timestamp()-interval '1 hour'")
        cls.collection = str(uuid.uuid4())
        cls.admin.execute("INSERT INTO public.saved_objects(kind,id,name,create_fingerprint) VALUES('collection',%s,'Reader',%s)",
                          (cls.collection, 'c' * 64))
        for video in (VISIBLE_VIDEO, HIDDEN_VIDEO):
            cls.admin.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) VALUES(%s,%s,clock_timestamp())',
                              (cls.collection, video))
        cls.admin.execute("UPDATE public.saved_objects SET member_count=2 WHERE kind='collection' AND id=%s", (cls.collection,))
        cls.seen, cls.lock = [], threading.Lock()
        original = psycopg.Connection.execute

        def execute(connection, statement, params=None, *args, **kwargs):
            result = original(connection, statement, params, *args, **kwargs)
            if isinstance(statement, str) and statement.startswith(GATEWAY_SETTINGS):
                row = original(connection, ROLE_PROBE).fetchone()
                row = tuple(row.values()) if isinstance(row, dict) else tuple(row)
                with cls.lock:
                    cls.seen.append(row)
            return result
        cls.patches = [patch.object(psycopg.Connection, 'execute', execute),
                       patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None)]
        for item in cls.patches:
            item.start()
        cls.folder = tempfile.TemporaryDirectory()
        cls.recorder = Recorder()
        cls.app = cls.build_app(Path(cls.folder.name))
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.app, base_url=ORIGIN, headers={'Origin': ORIGIN}, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        for item in reversed(cls.patches):
            item.stop()
        cls.folder.cleanup()
        native.NativeQueryTests.tearDownClass()

    @classmethod
    def build_app(cls, state):
        from fastapi import FastAPI
        from channel_trash import create_channel_trash_router
        from db.base.postgres import Operation
        from db.channel.channel_trash import ChannelTrashRepository
        from db.quota.youtube_quota import YouTubeQuotaRepository
        from db.reviews.video_type_reviews import VideoTypeReviewsRepository
        from db.self_hosted_auth import OwnerAuthRepository
        from db.user_work.saved_collections import SavedCollectionsRepository
        from provider_sources.daily_cap import CAPPED_BUCKET, PHASE
        from provider_sources.service import AcquisitionService
        from request_limits import RequestBodyLimitMiddleware
        from saved_collections import create_saved_collections_router
        from self_hosted_auth import SelfHostedAuthMiddleware
        from video_type_reviews import create_video_type_reviews_router
        from web.cache import create_read_cache
        from web.explorer import create_explorer_router
        from web.home import create_home_router
        from web.library import create_library_http
        from web.middleware import HttpReadScopeMiddleware, install_runtime_middleware
        from web.provider_sources import create_provider_sources_router
        from web.research import create_research_router
        from web.system import create_system_router
        from zoneinfo import ZoneInfo
        db, now = cls.db, int(time.time())
        owner = OwnerAuthRepository.initialize(state / 'owner.sqlite3', 'fixture-dummy', now)
        owner.issue_setup('fixture-setup', now)
        assert owner.claim_setup(lambda value: value == 'fixture-setup', 'fixture-session', int(time.time())) == 'ok'
        assert owner.complete_setup('fixture-session', 'local', None, lambda: None, None, int(time.time())) == 'ok'
        app = FastAPI()
        app.state.research_projection = SimpleNamespace(initial_fill_complete=True, preparation_failing=False)
        app.state.hot_runtime = SimpleNamespace(status=lambda: {'status': 'ready', 'api_read': {'allowed': True},
                                                               'collection': {'allowed': False}, 'user_work': {'allowed': True}})
        install_runtime_middleware(app, core=SimpleNamespace())
        cache = cls.read_cache = create_read_cache()
        library = create_library_http(cache=cache, database=db)
        core = SimpleNamespace(VIDEO_TYPE_LONG='long', VIDEO_TYPE_SHORT='short', VIDEO_TYPE_STREAM='stream')
        quota = YouTubeQuotaRepository(db, project_id='synthetic-reader')
        sources = AcquisitionService(attempt_recorder=cls.recorder, ledger_reader=lambda day, quota_bucket=CAPPED_BUCKET:
                                     quota.list_phase_attempts(day, phase=PHASE, quota_bucket=quota_bucket))
        for router in (create_research_router(db), create_explorer_router(database=db), library.router,
                       create_home_router(cache=cache, library=library, core=core, database=db),
                       create_saved_collections_router(SavedCollectionsRepository(db)),
                       create_video_type_reviews_router(VideoTypeReviewsRepository(db)),
                       create_channel_trash_router(ChannelTrashRepository(db)),
                       create_provider_sources_router(sources),
                       create_system_router(cache=cache, PT=ZoneInfo('America/Los_Angeles'), core=core, database=db,
                                            VIDEO_TYPE_REVIEWS_REPOSITORY=VideoTypeReviewsRepository(db))):
            app.include_router(router)

        @app.get('/reader-probe/write')                 # a GET handler that tries to write
        def get_write():
            with db.transaction(Operation('reader_probe_write')) as tx:
                tx.execute('INSERT INTO public.folders(id,name) VALUES(%s,%s)', (str(uuid.uuid4()), 'reader probe'))
            return {}

        @app.post('/reader-probe/read-only-write')      # a read-only operation that tries to write
        def read_only_write():
            with db.transaction(Operation('reader_probe_read_only_write', read_only=True)) as tx:
                tx.execute('INSERT INTO public.folders(id,name) VALUES(%s,%s)', (str(uuid.uuid4()), 'reader probe'))
            return {}
        app.add_middleware(RequestBodyLimitMiddleware)
        app.add_middleware(SelfHostedAuthMiddleware, repository=owner, origin=ORIGIN)
        app.add_middleware(HttpReadScopeMiddleware)
        return app

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def transactions(self):
        with self.lock:
            self.seen.clear()
        found = []
        yield found
        with self.lock:
            found.extend(self.seen)

    def read(self, method, path, *, status=200, **kwargs):
        with self.transactions() as found:
            answer = getattr(self.client, method)(path, **kwargs)
        self.assertEqual(answer.status_code, status, (path, answer.text[:300]))
        self.assertTrue(found, ('no database transaction', path))
        self.assertEqual(set(found), {READER}, path)
        return answer

    # ------------------------------------------------------------------ cases
    def case_research_query(self):
        page = self.read('post', '/research/queries/execute', json=query()).json()
        self.assertTrue(page['rows'])
        self.assertNotIn(HIDDEN_VIDEO, {row['id'] for row in page['rows']})
        frozen = self.read('post', '/research/queries/execute', json=query(consistency='frozen', page={'size': 2})).json()
        self.assertEqual(frozen['context']['consistency'], 'frozen')
        return frozen

    def case_frozen_ids(self):
        frozen = self.case_research_query()
        ids = self.read('post', '/research/queries/frozen-ids',
                        json={'version': 1, 'frozen_ref': frozen['context']['frozen_ref']}).json()
        self.assertEqual(ids['total_count'], frozen['page']['total_count'])

    def case_operation(self):
        from research_query.aggregate import SUMMARY
        answer = self.read('post', '/research/operations/execute',
                           json={'version': 1, 'operation': SUMMARY, 'query': query(), 'parameters': {}}).json()
        self.assertGreater(answer['result']['population'], 0)

    def case_graph(self):
        answer = self.read('post', '/research/graphs/execute', json=graph_document()).json()
        self.assertIn('result', answer['nodes'])

    def case_video_channel(self):
        matched = self.read('get', '/explorer/channel-library/video-channel', params={'video_id': VISIBLE_VIDEO}).json()
        hidden = self.read('get', '/explorer/channel-library/video-channel', params={'video_id': HIDDEN_VIDEO}).json()
        self.assertEqual((matched['status'], matched['channel_id'], hidden['status'], hidden['channel_id']),
                         ('matched', VISIBLE, 'not_tracked', None))

    def case_home_summary(self):
        # The route answers 200 without counts when they fail; the read model is lic_reader's.
        saved = self.read('get', '/explorer/home-summary').json()['saved_videos']
        self.assertIsNotNone(saved)
        self.assertEqual(saved['total'], saved['long'] + saved['short'] + saved['stream'])
        self.assertGreater(saved['long'], 0)

    def case_sidebar(self):
        channels = self.read('get', '/sidebar-data').json()['channels']
        self.assertIn(VISIBLE, {channel['channel_id'] for channel in channels})
        # A channel YouTube no longer returns (docs/DATA.md): listed last, and its removal date is the
        # latest deadline of its readable data, here a channel snapshot taken just now.
        gone = FIXTURE['channels'][4]['id']
        recorded = None
        try:
            # The first missing day is read back as lic_reader, too.
            since = self.admin.execute("UPDATE public.channels SET channel_metadata_status='channel_not_found',"
                                       "channel_metadata_not_found_since=date_trunc('second',clock_timestamp())-interval '3 days' "
                                       "WHERE channel_id=%s RETURNING channel_metadata_not_found_since", (gone,)).fetchone()[0]
            recorded, deadline = self.admin.execute(
                "INSERT INTO public.channel_snapshots(channel_id,recorded_at,subscribers,source) "
                "VALUES(%s,clock_timestamp(),1,'youtube_channel_metadata_refresh') "
                "RETURNING recorded_at,recorded_at+interval '696 hours'", (gone,)).fetchone()
            self.read_cache.clear_frontend_read_cache()
            channels = self.read('get', '/sidebar-data').json()['channels']
            self.assertEqual((channels[-1]['channel_id'], channels[-1]['title']), (gone, None))
            self.assertEqual(datetime.fromisoformat(channels[-1]['youtube_data_expires_at'].replace('Z', '+00:00')), deadline)
            self.assertEqual([channel['channel_id'] for channel in channels if 'youtube_data_expires_at' in channel], [gone])
            self.assertEqual(datetime.fromisoformat(channels[-1]['channel_metadata_not_found_since'].replace('Z', '+00:00')), since)
            self.assertEqual([channel['channel_id'] for channel in channels if 'channel_metadata_not_found_since' in channel], [gone])
        finally:
            if recorded is not None:
                self.admin.execute('DELETE FROM public.channel_snapshots WHERE channel_id=%s AND recorded_at=%s', (gone, recorded))
            self.admin.execute("UPDATE public.channels SET channel_metadata_status='ok',channel_metadata_not_found_since=NULL "
                               "WHERE channel_id=%s", (gone,))
            self.read_cache.clear_frontend_read_cache()

    def case_profile(self):
        self.assertEqual(self.read('get', f'/channels/{VISIBLE}/explorer/profile').json()['channel']['channel_id'], VISIBLE)
        self.read('get', f'/channels/{HIDDEN}/explorer/profile', status=404)

    def case_list_inventory(self):
        value = self.read('get', f'/channels/{VISIBLE}/explorer/list-inventory').json()
        self.assertEqual((value['contract_version'], value['channel_id'], value['source']['database_writes']), (1, VISIBLE, False))
        self.assertEqual(value['inventory']['youtube_video_count'], 50)     # the fresh profile's public count
        self.assertTrue(value['inventory']['source_expires_at'])
        self.assertEqual(set(value['library']['formats']), {'long', 'short', 'stream'})
        self.assertFalse(value['source']['youtube_requests'])
        self.read('get', f'/channels/{HIDDEN}/explorer/list-inventory', status=404)
        self.read('get', '/channels/UC' + 'Z' * 22 + '/explorer/list-inventory', status=404)

    def case_reviews(self):
        self.read('get', '/system/video-type-reviews')

    def case_review(self):
        self.read('get', f'/system/video-type-reviews/{uuid.uuid4()}', status=404)

    def case_members(self):
        value = self.read('get', f'/saved/collections/{self.collection}/members').json()
        self.assertTrue(value)

    def case_deleted_titles(self):
        self.assertEqual(self.read('get', '/user-work/deleted-channels/titles', params={'ids': str(uuid.uuid4())}).json(),
                         {'items': []})

    def case_websub(self):
        # Health's WebSub totals. The read runs as lic_reader, which reads no
        # public.websub_* table, only readable.websub_health.
        from db.websub import WebSubManagerRepository, WebSubRepository
        base = 'https://hooks.example.com/lc'
        try:
            WebSubManagerRepository(self.db).plan(base)
            planned = self.admin.execute("SELECT count(*) FROM public.websub_subscriptions WHERE state='pending'").fetchone()[0]
            self.assertGreater(planned, 0)
            WebSubRepository(self.db).delivery(VISIBLE, [], size=10)
            with patch('websub_service.configured_websub', lambda environ: (base, object())):
                value = self.read('get', '/system/websub').json()
            self.assertEqual((value['configured'], value['public_host']), (True, 'hooks.example.com'))
            summary = value['summary']
            self.assertEqual((summary['subscriptions'], summary['deliveries_24h'], summary['events_pending']),
                             ({'pending': planned}, 1, 0))
            self.assertTrue(summary['last_delivery_at'])
            self.assertEqual((summary['breaker_open'], summary['events_24h']), (False, {}))
            # Totals only: no channel, video, callback or key reaches the answer.
            self.assertNotIn('UC', json.dumps(summary))
        finally:
            for table in ('websub_events', 'websub_journal', 'websub_subscriptions'):
                self.admin.execute(f'DELETE FROM public.{table}')

    def case_acquisition(self):
        import requests
        import youtube_client
        from provider_sources_support import load
        fixture = load('youtube.playlists.list', 'full_two_pages')
        pages = []
        for item in fixture['responses']:
            response = requests.Response()
            response.status_code, response._content, response._content_consumed = item['status'], json.dumps(item['body']).encode(), True
            response.headers['Date'] = 'Thu, 29 Oct 2026 10:00:00 GMT'
            response.iter_content = (lambda body: lambda chunk_size: iter((body,)))(response._content)
            response.close = lambda: None
            pages.append(response)
        preview = self.read('post', '/sources/acquisitions/preview', json=fixture['request']).json()
        body = {'version': 1, 'request': fixture['request'], 'plan_fingerprint': preview['plan_fingerprint'],
                'confirmed_cost': preview['worst_cost']}
        with patch.object(youtube_client.requests, 'get', lambda url, **kwargs: pages.pop(0)), \
                patch('provider_runtime.admit_request', side_effect=lambda **kw: __import__('contextlib').nullcontext(
                    __import__('provider_runtime')._UnregulatedRequest(kw.get('cancel')))), \
                patch.object(youtube_client.time_trust, 'record_date'):
            answer = self.read('post', '/sources/acquisitions/execute', json=body, headers={'X-Saved-Work': '1'}).json()
        self.assertEqual((answer['completeness'], len(answer['rows']), pages), ('complete', 3, []))

    CASES = {
        'POST /research/queries/execute': case_research_query,
        'POST /research/queries/frozen-ids': case_frozen_ids,
        'POST /research/operations/execute': case_operation,
        'POST /research/graphs/execute': case_graph,
        'GET /explorer/channel-library/video-channel': case_video_channel,
        'GET /explorer/home-summary': case_home_summary,
        'GET /sidebar-data': case_sidebar,
        'GET /channels/{channel_id}/explorer/profile': case_profile,
        'GET /channels/{channel_id}/explorer/list-inventory': case_list_inventory,
        'GET /system/video-type-reviews': case_reviews,
        'GET /system/video-type-reviews/{case_id}': case_review,
        'GET /saved/collections/{collection_id}/members': case_members,
        'GET /user-work/deleted-channels/titles': case_deleted_titles,
        'POST /sources/acquisitions/execute': case_acquisition,
        'GET /system/websub': case_websub,
    }

    # ------------------------------------------------------------------ tests
    def test_00_every_protected_route_is_placed(self):
        self.assertEqual(set(self.CASES) | set(NOT_HERE), set(PROTECTED_ROUTES) | set(READER_ROLE_READS))
        self.assertEqual(set(self.CASES) & set(NOT_HERE), set())
        from hot_runtime_routes import route_access
        for route in self.CASES:
            method, path = route.split(' ', 1)
            self.assertIsNotNone(route_access(method, path), route)

    def test_01_every_read_runs_as_lic_reader_read_only(self):
        for route, case in self.CASES.items():
            with self.subTest(route=route):
                case(self)

    def test_02_lic_reader_holds_no_write_privilege(self):
        def rows(sql):
            return [tuple(row) for row in self.admin.execute(sql).fetchall()]
        relations = rows("""SELECT n.nspname||'.'||c.relname, p.priv FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN unnest(ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p(priv)
            WHERE c.relkind IN ('r','v','m','p','f') AND n.nspname NOT IN ('pg_catalog','information_schema')
              AND (has_table_privilege('lic_reader',c.oid,p.priv)
                   OR (p.priv IN ('INSERT','UPDATE','REFERENCES') AND has_any_column_privilege('lic_reader',c.oid,p.priv)))""")
        sequences = rows("""SELECT c.relname, p.priv FROM pg_catalog.pg_class c
            CROSS JOIN unnest(ARRAY['USAGE','UPDATE']) p(priv) WHERE c.relkind='S' AND has_sequence_privilege('lic_reader',c.oid,p.priv)""")
        schemas = rows("SELECT nspname FROM pg_catalog.pg_namespace WHERE has_schema_privilege('lic_reader',oid,'CREATE')")
        definers = rows("""SELECT n.nspname||'.'||p.proname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE p.prosecdef AND has_function_privilege('lic_reader',p.oid,'EXECUTE')""")
        readable = rows("""SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='readable' AND has_table_privilege('lic_reader',c.oid,'SELECT')""")[0][0]
        self.assertEqual((relations, sequences, schemas, definers), ([], [], [], []))
        self.assertGreater(readable, 0)

    def test_03_a_write_as_lic_reader_is_refused_by_postgres(self):
        port, password = int(os.environ['LIC_QUERY_TEST_PORT']), os.environ['LIC_QUERY_TEST_PASSWORD']
        statements = (
            ("INSERT INTO public.folders(id,name) VALUES(%s,'reader')", (str(uuid.uuid4()),)),
            ("UPDATE public.videos SET views=0 WHERE video_id=%s", (VISIBLE_VIDEO,)),
            ("DELETE FROM public.saved_collection_members WHERE collection_id=%s", (self.collection,)),
            ("UPDATE readable.videos SET title='x' WHERE video_id=%s", (VISIBLE_VIDEO,)),
            ("DELETE FROM readable.quota_attempts", ()),
            ("TRUNCATE public.youtube_api_request_attempts", ()),
            ("CREATE TABLE public.reader_probe(x int)", ()),
        )
        with psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='authenticator', password=password) as login:
            for statement, parameters in statements:
                with self.subTest(statement=statement[:48]):
                    # A join view is refused as not updatable before privileges are checked: also a refusal.
                    with self.assertRaises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectNotInPrerequisiteState)):
                        with login.transaction():
                            login.execute('SET LOCAL ROLE lic_reader')      # no READ ONLY: privileges alone refuse
                            login.execute(statement, parameters)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.saved_collection_members WHERE collection_id=%s",
                                            (self.collection,)).fetchone()[0], 2)

    def test_04_http_reads_cannot_write(self):
        before = self.admin.execute("SELECT count(*) FROM public.folders").fetchone()[0]
        with self.transactions() as found:
            refused = self.client.get('/reader-probe/write')
        self.assertEqual((refused.status_code, found), (500, []))      # refused before any SQL (GET scope)
        with self.transactions() as found:
            refused = self.client.post('/reader-probe/read-only-write')
        self.assertEqual(refused.status_code, 500)
        self.assertEqual(set(found), {READER})                          # the database refused it as lic_reader
        self.assertEqual(self.admin.execute("SELECT count(*) FROM public.folders").fetchone()[0], before)

    def test_05_the_typed_read_model_view_is_the_http_readers_alone(self):
        # readable.video_search_projection_types, the read model's own columns that Home's
        # typed counts read. It shows nothing readable.video_search_projection does not: a subset of its
        # columns with their types, the same owner, options and grantees, the same number of rows.
        narrow, full = 'readable.video_search_projection_types', 'readable.video_search_projection'
        own = ['video_id', 'channel_id', 'proof_current', 'inventory_type', 'hot_present', 'identity_clock', 'identity_expiry']

        def rows(sql, *args):
            return [tuple(row) for row in self.admin.execute(sql, args).fetchall()]
        columns = ("SELECT attname::text,format_type(atttypid,atttypmod) FROM pg_catalog.pg_attribute"
                   " WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")
        self.assertEqual([name for name, _ in rows(columns, narrow)], own)
        self.assertLessEqual(set(rows(columns, narrow)), set(rows(columns, full)))
        meta = "SELECT relkind::text,pg_get_userbyid(relowner)::text,reloptions FROM pg_catalog.pg_class WHERE oid=%s::regclass"
        self.assertEqual(rows(meta, narrow), [('v', 'postgres', ['security_barrier=true', 'security_invoker=false'])])
        self.assertEqual(rows(meta, narrow), rows(meta, full))
        granted = ("SELECT r.rolname::text,p.priv FROM pg_catalog.pg_roles r CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE',"
                   "'DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN']) p(priv) WHERE NOT r.rolsuper AND r.rolname !~ '^pg_'"
                   " AND has_table_privilege(r.oid,%s::regclass,p.priv) ORDER BY 1,2")
        self.assertEqual(rows(granted, narrow), [('lic_reader', 'SELECT')])
        self.assertEqual(rows(granted, narrow), rows(granted, full))
        port, password = int(os.environ['LIC_QUERY_TEST_PORT']), os.environ['LIC_QUERY_TEST_PASSWORD']
        with psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='authenticator', password=password) as login:
            with login.transaction(force_rollback=True):
                login.execute('SET LOCAL ROLE lic_reader')
                login.execute('SET TRANSACTION READ ONLY')
                count = login.execute(f'SELECT (SELECT count(*) FROM {narrow}),(SELECT count(*) FROM {full})').fetchone()
                self.assertGreater(count[0], 0)
                self.assertEqual(count[0], count[1])
            # The login itself (NOINHERIT) and the collector's role read nothing here.
            for role in (None, 'service_role'):
                with self.subTest(role=role), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with login.transaction():
                        if role:
                            login.execute(f'SET LOCAL ROLE {role}')
                        login.execute(f'SELECT 1 FROM {narrow} LIMIT 1')
        for role in ('anon', 'authenticated'):
            with self.subTest(role=role), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.admin.transaction():
                    self.admin.execute(f'SET LOCAL ROLE {role}')
                    self.admin.execute(f'SELECT 1 FROM {narrow} LIMIT 1')

    # Metadata states of the next two cases (rolled back): fresh, at both edges of the 696-hour window, past
    # it, a future clock, infinite clocks, and no publication time.
    METADATA_STATES = ("now()-interval '1 hour'", "now()-interval '696 hours'+interval '1 minute'", "now()",
                       "now()-interval '697 hours'", "now()+interval '1 hour'", "'infinity'", "'-infinity'")

    def metadata_states(self):
        ids = [row[0] for row in self.admin.execute('SELECT video_id FROM public.videos ORDER BY video_id').fetchall()]
        self.assertGreaterEqual(len(ids), len(self.METADATA_STATES) + 1)
        for video_id, clock in zip(ids, self.METADATA_STATES):
            self.admin.execute(f'UPDATE public.videos SET metadata_checked_at={clock} WHERE video_id=%s', (video_id,))
        self.admin.execute('UPDATE public.videos SET published_at=NULL WHERE video_id=%s', (ids[len(self.METADATA_STATES)],))

    def as_reader(self, sql):
        self.admin.execute('SET LOCAL ROLE lic_reader')
        try:
            return tuple(self.admin.execute(sql).fetchone())
        finally:
            self.admin.execute('RESET ROLE')

    def test_06_the_read_model_view_appends_the_videos_payload_unchanged(self):
        # readable.video_search_projection appends the payload catalogue reads used to
        # look up again in readable.videos, from its own readable.videos row: the same types, and for every row the
        # same values readable.videos shows lic_reader (masked there), in every metadata state.
        full, appended = 'readable.video_search_projection', ['like_count', 'comment_count', 'tags', 'youtube_topic_categories',
                                                               'category_id', 'default_audio_language', 'default_language',
                                                               'caption_available', 'scheduled_start_at']
        columns = ("SELECT attname::text,format_type(atttypid,atttypmod) FROM pg_catalog.pg_attribute"
                   " WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")
        own = [tuple(row) for row in self.admin.execute(columns, (full,)).fetchall()]
        videos = dict(tuple(row) for row in self.admin.execute(columns, ('readable.videos',)).fetchall())
        self.assertEqual([name for name, _ in own[-len(appended):]], appended)
        self.assertEqual(own[-len(appended) - 1][0], 'stream_actual_end_at')
        self.assertEqual(own[-len(appended):], [(name, videos[name]) for name in appended])
        listed = ','.join(appended)
        with self.admin.transaction(force_rollback=True):
            self.metadata_states()
            self.admin.execute('UPDATE public.videos SET like_count=7,comment_count=3,tags=ARRAY[\'a\'],category_id=\'10\'')
            counted = self.as_reader(f"SELECT (SELECT count(*) FROM {full}),(SELECT count(*) FROM {full} p LEFT JOIN readable.videos v"
                                     f" USING(video_id) WHERE (p.{listed.replace(',', ',p.')}) IS DISTINCT FROM"
                                     f" (v.{listed.replace(',', ',v.')})),(SELECT count(*) FROM {full} WHERE like_count IS NOT NULL)")
        self.assertGreater(counted[0], 0)
        self.assertEqual(counted[1], 0)
        self.assertGreater(counted[2], 0)

    def test_07_the_publication_times_view_shows_only_what_readable_videos_shows(self):
        # readable.video_publication_times, Home's publication window. Its rows are exactly
        # the (video, publication time) pairs readable.videos shows unmasked, in every metadata state; a window on
        # it reaches idx_videos_published_at_desc as an index condition. Owner, options and grantees as the read model.
        view, model = 'readable.video_publication_times', 'readable.video_search_projection'
        columns = ("SELECT attname::text,format_type(atttypid,atttypmod) FROM pg_catalog.pg_attribute"
                   " WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")
        own = [tuple(row) for row in self.admin.execute(columns, (view,)).fetchall()]
        self.assertEqual(own, [('video_id', 'text'), ('published_at', 'timestamp with time zone')])
        self.assertLessEqual(set(own), {tuple(row) for row in self.admin.execute(columns, ('readable.videos',)).fetchall()})
        meta = "SELECT relkind::text,pg_get_userbyid(relowner)::text,reloptions FROM pg_catalog.pg_class WHERE oid=%s::regclass"
        self.assertEqual(self.admin.execute(meta, (view,)).fetchall(), [('v', 'postgres', ['security_barrier=true', 'security_invoker=false'])])
        granted = ("SELECT r.rolname::text,p.priv FROM pg_catalog.pg_roles r CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE',"
                   "'DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN']) p(priv) WHERE NOT r.rolsuper AND r.rolname !~ '^pg_'"
                   " AND has_table_privilege(r.oid,%s::regclass,p.priv) ORDER BY 1,2")
        self.assertEqual(self.admin.execute(granted, (view,)).fetchall(), [('lic_reader', 'SELECT')])
        self.assertEqual(self.admin.execute(granted, (view,)).fetchall(), self.admin.execute(granted, (model,)).fetchall())
        shown = "SELECT video_id,published_at FROM readable.videos WHERE published_at IS NOT NULL"
        compare = (f"SELECT (SELECT count(*) FROM (SELECT video_id,published_at FROM {view} EXCEPT ALL {shown}) a),"
                   f"(SELECT count(*) FROM ({shown} EXCEPT ALL SELECT video_id,published_at FROM {view}) b),(SELECT count(*) FROM {view})")
        for states in (False, True):
            with self.subTest(states=states), self.admin.transaction(force_rollback=True):
                if states:
                    self.metadata_states()
                only_view, only_videos, rows = self.as_reader(compare)
                self.assertEqual((only_view, only_videos), (0, 0))
                self.assertGreater(rows, 0)
                if states:
                    self.assertLess(rows, self.admin.execute('SELECT count(*) FROM public.videos WHERE published_at IS NOT NULL').fetchone()[0])
        with self.admin.transaction(force_rollback=True):
            self.admin.execute('SET LOCAL ROLE lic_reader')
            self.admin.execute('SET LOCAL enable_seqscan=off')
            plan = '\n'.join(row[0] for row in self.admin.execute(
                f"EXPLAIN (COSTS OFF) SELECT video_id FROM {view} WHERE published_at>=statement_timestamp()-'24 hours'::interval").fetchall())
        self.assertIn('idx_videos_published_at_desc', plan)
        # The window is an index condition (with the view's own IS NOT NULL), not a filter after the scan.
        self.assertRegex(plan, r'Index Cond: [^\n]*\((?:s\.)?published_at >= \(statement_timestamp\(\) - ')
        port, password = int(os.environ['LIC_QUERY_TEST_PORT']), os.environ['LIC_QUERY_TEST_PASSWORD']
        with psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='authenticator', password=password) as login:
            for role in (None, 'service_role'):
                with self.subTest(role=role), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with login.transaction():
                        if role:
                            login.execute(f'SET LOCAL ROLE {role}')
                        login.execute(f'SELECT 1 FROM {view} LIMIT 1')
        for role in ('anon', 'authenticated'):
            with self.subTest(role=role), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.admin.transaction():
                    self.admin.execute(f'SET LOCAL ROLE {role}')
                    self.admin.execute(f'SELECT 1 FROM {view} LIMIT 1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
