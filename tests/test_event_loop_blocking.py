"""Blocking work stays off the event loop: route handlers, backup decoding, telemetry streams. No server or database."""
import ast
import asyncio
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import user_work_backup
from user_work_backup import FORMAT, VERSION, LIMITS, UserWorkError, encode_backup
from user_work_service import UserWorkService
try:
    import fastapi  # noqa: F401  (the telemetry routes need it; CI installs it)
    HAVE_FASTAPI=True
except ImportError:
    HAVE_FASTAPI=False

# Async handlers that never await do no blocking work: fixed responses, in-memory state or static files.
NON_BLOCKING_ASYNC={'refresh_all','channel_explorer_capabilities','run_deep_stats_channel','recalculate_viral',
                    'explorer_asset','app_explorer_v3_ui','root','retired_legacy_app','hot_runtime_proof','hot_runtime_status'}
ROUTE_METHODS={'get','post','put','delete','patch','head','api_route'}

def route_handlers(path):
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and any(
                isinstance(d,ast.Call) and getattr(d.func,'attr','') in ROUTE_METHODS for d in node.decorator_list):
            yield node

def document(channels=(),collections=()):
    return {'format':FORMAT,'version':VERSION,'created_at':'2026-09-25T00:00:00+00:00',
            'user_work':{'folders':[],'labels':[],'channels':list(channels),'collections':list(collections),'reviews':[]},
            'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]}}

class Repository:
    def __init__(self):self.imported=[]
    def status(self,*args):return {'can_restore':True,'channel_mappings':[],'user_channel_refs':[],'folder_ids':[],'pending':[],
        'reconnect':{'snapshot':'0'*64,'counts':dict.fromkeys(('idle','queued','running','waiting','blocked'),0),'excluded_hidden':0}}
    def import_document(self,payload):self.imported.append(payload);return {'outcome':'restored'}

class RouteHandlerTests(unittest.TestCase):
    def test_async_handlers_that_never_await_do_no_blocking_work(self):
        found=[]
        for path in [*(ROOT/'application').glob('*.py'), *(ROOT/'application/web').glob('*.py')]:
            for node in route_handlers(path):
                if isinstance(node,ast.AsyncFunctionDef) and not any(isinstance(a,ast.Await) for a in ast.walk(node)):
                    found.append(node.name)
        self.assertTrue(found)
        self.assertEqual(sorted(set(found)-NON_BLOCKING_ASYNC),[])

    def test_database_handlers_run_in_the_thread_pool(self):
        handlers={n.name:n for path in (ROOT/'application/web').glob('*.py') for n in route_handlers(path)}
        for name in ('explorer_channel_library_video_channel','channel_backfill_current_stats_status',
                     'start_channel_backfill_current_stats','channel_hydration_status','enqueue_channel_hydration',
                     'create_channel_label','assign_channel_label','create_folder','assign_folder'):
            self.assertIsInstance(handlers[name],ast.FunctionDef,name)

class BackupTests(unittest.TestCase):
    def test_restore_decodes_and_normalizes_once(self):
        calls=[]
        original=user_work_backup._normalize
        with patch.object(user_work_backup,'_normalize',lambda value:calls.append(1) or original(value)):
            repository=Repository()
            result=UserWorkService(repository).restore_backup(encode_backup(document()))
        self.assertEqual((result['status'],len(calls),len(repository.imported)),('restored',1,1))

    def test_preview_decodes_and_normalizes_once(self):
        calls=[]
        original=user_work_backup._normalize
        with patch.object(user_work_backup,'_normalize',lambda value:calls.append(1) or original(value)):
            result=UserWorkService(Repository()).preview_backup(encode_backup(document()))
        self.assertEqual((result['can_restore'],len(calls)),(True,1))

    def test_oversized_totals_are_rejected_before_item_normalization(self):
        # Items are invalid on purpose: a per-item pass would fail on them first with a generic code.
        per_channel=LIMITS['views']//2+1
        channels=[{'id':f'00000000-0000-4000-8000-00000000000{i}','user_input':None,'folder_id':None,'parent_ref':None,
                   'is_hidden':False,'added_at':None,'label_keys':[],'saved_views':[None]*per_channel} for i in range(2)]
        with self.assertRaises(UserWorkError) as caught:
            UserWorkService(Repository()).preview_backup(json.dumps(document(channels)).encode())
        self.assertEqual(caught.exception.code,'user_work_limit_exceeded')

    def test_routes_decode_backups_in_the_thread_pool_with_separate_body_limits(self):
        source=(ROOT/'application/user_work_service.py').read_text(encoding='utf-8')
        router=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='create_user_work_router')
        read_body=next(n for n in ast.walk(router) if isinstance(n,ast.AsyncFunctionDef) and n.name=='read_body')
        called={ast.unparse(c.func) for c in ast.walk(read_body) if isinstance(c,ast.Call)}
        self.assertNotIn('decode_backup',called)
        self.assertIn('limit = MAX_BYTES if backup else REQUEST_MAX_BYTES',ast.unparse(read_body))
        text=ast.unparse(router)
        self.assertIn('run_in_threadpool(service.preview_backup',text)
        self.assertIn('run_in_threadpool(service.restore_backup',text)
        import user_work_service
        self.assertLess(user_work_service.REQUEST_MAX_BYTES,user_work_service.MAX_BYTES)

@unittest.skipUnless(HAVE_FASTAPI,'fastapi is not installed')
class TelemetryStreamTests(unittest.TestCase):
    def stream(self,routes,disconnect_after=1):
        checks={'count':0}
        async def is_disconnected():
            checks['count']+=1
            return checks['count']>disconnect_after
        request=types.SimpleNamespace(headers={},query_params={},is_disconnected=is_disconnected)
        async def collect():
            response=await routes.telemetry_stream(request)
            return [chunk async for chunk in response.body_iterator]
        return asyncio.run(collect())

    def routes(self):
        import telemetry_routes
        calls=[]
        main=threading.current_thread()
        def snapshot(limit,*,flush=True):
            calls.append(('snapshot',flush,threading.current_thread() is main));return {'position':0,'generation':1,'events':[]}
        def since(position,generation,limit,*,flush=True):
            calls.append(('since',flush,threading.current_thread() is main));return {'position':0,'generation':1,'events':[]}
        real_sleep=asyncio.sleep
        patchers=[patch.object(telemetry_routes,'telemetry_tail_snapshot',snapshot),patch.object(telemetry_routes,'read_events_since',since),
                  patch.object(telemetry_routes.asyncio,'sleep',lambda seconds:real_sleep(0))]
        for patcher in patchers:
            patcher.start();self.addCleanup(patcher.stop)
        return telemetry_routes,calls

    def test_journal_reads_run_in_worker_threads_without_waiting_for_a_flush(self):
        routes,calls=self.routes()
        chunks=self.stream(routes)
        self.assertTrue(chunks[0].startswith('retry: 2000'))
        self.assertEqual(calls,[('snapshot',False,False),('since',False,False)])
        self.assertEqual(routes._streams['active'],0)

    def test_streams_over_the_cap_are_asked_to_reconnect_later(self):
        routes,calls=self.routes()
        routes._streams['active']=routes.MAX_TELEMETRY_STREAMS
        try:
            chunks=self.stream(routes)
        finally:
            routes._streams['active']=0
        self.assertEqual(chunks,[f'retry: {routes.BUSY_RETRY_MILLISECONDS}\n: busy\n\n'])
        self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()
