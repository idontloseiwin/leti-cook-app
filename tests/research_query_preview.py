"""Manual synthetic browser fixture. Never loads core, YouTube or a user database.

Requires the disposable-cluster environment used by research_query_pg.py. The
query router, SQL executor, owner middleware, lifetime wrapper and built UI are
real. Only sidebar organization and the HOT readiness signal are fixture data.
"""
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from fastapi import FastAPI
from db.self_hosted_auth import OwnerAuthRepository
from self_hosted_auth import SelfHostedAuthMiddleware
from request_limits import RequestBodyLimitMiddleware
from web.middleware import install_runtime_middleware, HttpReadScopeMiddleware
from web.research import create_research_router
from web.frontend import create_frontend_router


def create_preview_app(database, auth_path, origin, folders):
    now = int(time.time())
    owner = OwnerAuthRepository.initialize(auth_path, 'fixture-dummy', now)
    owner.issue_setup('fixture-setup', now)
    assert owner.claim_setup(lambda value: value == 'fixture-setup', 'fixture-session', int(time.time())) == 'ok'
    # These are separate simulated requests. The claim's committed session can
    # be newer than the timestamp captured before filesystem/SQLite setup.
    assert owner.complete_setup('fixture-session', 'local', None, lambda: None, None, int(time.time())) == 'ok'
    app = FastAPI()
    app.state.hot_runtime = SimpleNamespace(status=lambda: {'status': 'ready', 'api_read': {'allowed': True},
                                                           'collection': {'allowed': False}, 'user_work': {'allowed': True}})
    install_runtime_middleware(app, core=SimpleNamespace())
    app.include_router(create_research_router(database))
    @app.get('/sidebar-data')
    @app.get('/sidebar-user-data')
    def sidebar():
        return {'channels': [], 'folders': [{'id': f, 'name': 'Synthetic folder '+str(i+1)} for i,f in enumerate(folders)], 'labels': []}
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
    os.environ.update(LIC_RESEARCH_QUERY_PREVIEW='1', TELEMETRY_ENABLED='false')
    for i in range(31):
        FIXTURE['rows'].append({**FIXTURE['rows'][0], 'id': f'p{i:010}', 'title': f'Synthetic workshop video {i+1:02}', 'views': str(1000+i)})
    NativeQueryTests.setUpClass()
    try:
        origin = f'http://127.0.0.1:{args.port}'
        app = create_preview_app(NativeQueryTests.db, args.state/'owner.sqlite3', origin, FIXTURE['folders'])
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False))
        def stop_watch():
            while not server.started: time.sleep(.1)
            (args.state/'ready.json').write_text(json.dumps({'url': origin+'/app-explorer-v3/research'}))
            while not (args.state/'stop').exists(): time.sleep(.2)
            server.should_exit = True
        threading.Thread(target=stop_watch, daemon=True).start()
        with patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None): server.run()
    finally:
        NativeQueryTests.tearDownClass()
