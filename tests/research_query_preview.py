"""Manual synthetic browser fixture. Never loads core, YouTube or a user database.

Requires the disposable-cluster environment used by research_query_pg.py. The
query router, SQL executor, owner middleware, lifetime wrapper and built UI are
real. Only sidebar organization and the HOT readiness signal are fixture data.
"""
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from contextlib import asynccontextmanager, ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from fastapi import FastAPI
from db.self_hosted_auth import OwnerAuthRepository
from self_hosted_auth import SelfHostedAuthMiddleware
from request_limits import RequestBodyLimitMiddleware
from web.middleware import install_runtime_middleware, HttpReadScopeMiddleware
from web.research import create_research_router
from web.frontend import create_frontend_router
from db.inventory.search_projection import SearchProjectionRepository
from research_projection_refresher import SearchProjectionRefresher


def create_preview_app(database, auth_path, origin, folders, *, projection=None, start_projection=True, sidebar_channels=(), sidebar_labels=(), collections=()):
    now = int(time.time())
    owner = OwnerAuthRepository.initialize(auth_path, 'fixture-dummy', now)
    owner.issue_setup('fixture-setup', now)
    assert owner.claim_setup(lambda value: value == 'fixture-setup', 'fixture-session', int(time.time())) == 'ok'
    # These are separate simulated requests. The claim's committed session can
    # be newer than the timestamp captured before filesystem/SQLite setup.
    assert owner.complete_setup('fixture-session', 'local', None, lambda: None, None, int(time.time())) == 'ok'
    projection = projection or SearchProjectionRefresher(SearchProjectionRepository(database), ready=lambda: None)
    @asynccontextmanager
    async def lifespan(app):
        if start_projection: projection.start()
        try:
            yield
        finally:
            if start_projection: projection.close()
    app = FastAPI(lifespan=lifespan)
    app.state.fixture_requests = []
    @app.middleware('http')
    async def count_fixture_request(request, call_next):
        app.state.fixture_requests.append(request.method + ' ' + request.url.path)
        return await call_next(request)
    app.state.research_projection = projection
    app.state.hot_runtime = SimpleNamespace(status=lambda: {'status': 'ready', 'api_read': {'allowed': True},
                                                           'collection': {'allowed': False}, 'user_work': {'allowed': True}})
    install_runtime_middleware(app, core=SimpleNamespace())
    app.include_router(create_research_router(database))
    from web.agents import create_agents_router
    app.include_router(create_agents_router())
    from saved_views import create_saved_views_router
    from db.user_work.saved_views import SavedViewsRepository
    app.include_router(create_saved_views_router(SavedViewsRepository(database)))
    @app.get('/sidebar-data')
    @app.get('/sidebar-user-data')
    def sidebar():
        return {'channels': list(sidebar_channels), 'folders': [{'id': f, 'name': 'Synthetic folder '+str(i+1)} for i,f in enumerate(folders)], 'labels': list(sidebar_labels)}
    @app.get('/saved/collections')
    def saved_collections():
        return {'contract_version':1, 'collections':list(collections), 'next_cursor':None}
    app.include_router(create_frontend_router(FRONTEND_DIST_DIR=ROOT/'application/frontend/dist-self-hosted'))
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(SelfHostedAuthMiddleware, repository=owner, origin=origin)
    app.add_middleware(HttpReadScopeMiddleware)
    return app


if __name__ == '__main__':
    import argparse
    import json
    import os
    import threading
    from unittest.mock import patch
    import uvicorn
    from research_query_pg import NativeQueryTests, FIXTURE
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1': raise RuntimeError('disposable_cluster_required')
    os.environ.update(TELEMETRY_ENABLED='false',CHANNEL_BASELINE_ANALYTICS_ENABLED='true',CHANNEL_DERIVED_INSIGHTS_ENABLED='true')
    for i in range(31):
        FIXTURE['rows'].append({**FIXTURE['rows'][0], 'id': f'p{i:010}', 'title': f'Synthetic workshop video {i+1:02}', 'views': str(1000+i)})
    NativeQueryTests.setUpClass()
    NativeQueryTests.admin.execute("INSERT INTO public.video_snapshots(video_id,recorded_at,views,views_definition_epoch) SELECT video_id,checked_at,views,'fixture.current.v1' FROM public.videos WHERE views>=0 AND checked_at IS NOT NULL")
    NativeQueryTests.admin.execute("UPDATE public.channels SET title='Synthetic channel '||right(channel_id,2),subscribers=12345,views=987654,video_count=50,channel_metadata_status='ok',channel_metadata_checked_at=clock_timestamp()-interval '1 hour'")
    try:
        origin = f'http://127.0.0.1:{args.port}'
        fixture_sources = ExitStack()
        test = NativeQueryTests()
        collection = fixture_sources.enter_context(test.collection(('a0000000001','d0000000001')))
        label = fixture_sources.enter_context(test.label((0,1,3)))
        timestamp = NativeQueryTests.admin.execute('SELECT clock_timestamp()').fetchone()[0].isoformat()
        channels = [{'channel_id':c['id'], 'title':'Synthetic channel '+str(i+1), 'folder_id':FIXTURE['folders'][c['folder']],
                     'is_hidden':c['hidden']} for i,c in enumerate(FIXTURE['channels']) if not c['hidden']]
        app = create_preview_app(NativeQueryTests.db, args.state/'owner.sqlite3', origin, FIXTURE['folders'],
            sidebar_channels=channels, sidebar_labels=[{'label_key':label['id'],'name':'Synthetic label','label_type':'custom'}],
            collections=[{'id':collection['id'],'kind':'collection','name':'One visible and one hidden','revision':1,
                          'member_count':2,'created_at':timestamp,'updated_at':timestamp}])
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False))
        def stop_watch():
            while not server.started: time.sleep(.1)
            (args.state/'ready.json').write_text(json.dumps({'url': origin+'/app-explorer-v3/research'}))
            while not (args.state/'stop').exists(): time.sleep(.2)
            server.should_exit = True
        threading.Thread(target=stop_watch, daemon=True).start()
        with patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None): server.run()
        (args.state/'browser-requests.json').write_text(json.dumps(app.state.fixture_requests,indent=2))
        fixture_sources.close()
    finally:
        NativeQueryTests.tearDownClass()
