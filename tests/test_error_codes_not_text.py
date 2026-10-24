"""Stored channel errors and HTTP responses carry codes, never exception text. Source only: no server or database."""
import ast
from functools import partial
from types import SimpleNamespace
from pathlib import Path
import re
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import youtube_errors
CHANNEL_METADATA=ROOT/'application/collection/channel_metadata.py'
SERVER=ROOT/'application/server.py'
CHANNEL_SERVICE=ROOT/'application/channels/service.py'
CHANNEL_PREPARATION=ROOT/'application/channels/preparation.py'
KEY='AIza'+'k'*35
PROVIDER_TEXT=f"HTTPSConnectionPool(host='www.googleapis.com'): Max retries exceeded with url: /youtube/v3/channels?id=UC1&key={KEY}"
ERROR_COLUMNS={'channel_metadata_error','deep_inventory_error','channel_discovery_error'}
CODE=re.compile(r'[a-z0-9_]{1,64}')

from collection.channel_metadata import stored_channel_error_code, mark_channel_metadata_error, youtube_quota_reset_at
from collection.context import CollectionContext
from youtube_errors import YouTubeAPIError, YouTubePlaylistNotFoundError
from http_test_support import channel_http, endpoint
from web.constants import OPERATOR_JOBS_MODE_INVALID, VISIBLE_CHANNEL_FIELDS
from web.models import ChannelRequest
from unittest.mock import patch
import asyncio


# Existing collection/provider fixture support. HTTP tests below use real owners;
# the independent domain suites still select their legacy source fixtures here.
def module_parts(path,names,namespace):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    body=[n for n in tree.body if (isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names)
          or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets))]
    selected={n.name for n in body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    selected.update(t.id for n in body if isinstance(n,ast.Assign) for t in n.targets if isinstance(t,ast.Name))
    missing=names-selected
    if missing:
        raise AssertionError(f'{path}: missing exact-owner definitions {sorted(missing)}')
    namespace['CollectionContext']=CollectionContext
    exec(compile(ast.Module(body=body,type_ignores=[]),str(path),'exec'),namespace)
    return namespace


def sync_functions(updates=None):
    """Call current metadata/lifecycle owners with a recording repository."""
    classifier = partial(youtube_errors.is_youtube_daily_quota_exhausted,
                         is_batch_stats_quota_error=lambda error: False)
    context = SimpleNamespace(sources=SimpleNamespace(is_youtube_daily_quota_exhausted=classifier))
    class Repository:
        def update_process_state(self, channel_id, values, *, expected_epoch):
            assert expected_epoch == 7
            (updates if updates is not None else []).append(values)
            return True
    return {'youtube_quota_reset_at': youtube_quota_reset_at,
            'stored_channel_error_code': partial(stored_channel_error_code, context=context),
            'mark_channel_metadata_error': partial(mark_channel_metadata_error, context=context, repository=Repository())}

class StoredErrorCodeTests(unittest.TestCase):
    def test_provider_text_becomes_a_code(self):
        code=sync_functions()['stored_channel_error_code']
        for error in (RuntimeError(PROVIDER_TEXT),ConnectionError(PROVIDER_TEXT),PROVIDER_TEXT,'Recovered stale running status'):
            self.assertEqual(code(error,default='channel_metadata_failed'),'channel_metadata_failed')
        self.assertEqual(code(RuntimeError('reason=quotaExceeded '+KEY),default='x'),'youtube_quota_exhausted')
        self.assertEqual(code(YouTubePlaylistNotFoundError(PROVIDER_TEXT),default='x'),'long_playlist_not_found')
        self.assertEqual(code(YouTubeAPIError(PROVIDER_TEXT),default='x'),'youtube_api_error')
        self.assertEqual(code('metadata_recheck_quota_deferred',default='x'),'metadata_recheck_quota_deferred')

    def test_metadata_error_state_stores_a_code(self):
        updates=[]
        from datetime import datetime,timezone
        sync_functions(updates)['mark_channel_metadata_error']({'channel_id':'UC1','collection_control_epoch':7},RuntimeError(PROVIDER_TEXT),datetime.now(timezone.utc))
        self.assertEqual(updates[0]['channel_metadata_error'],'channel_metadata_failed')
        self.assertNotIn(KEY,repr(updates))

    def test_every_error_column_write_is_a_code(self):
        allowed_names={'code','long_inventory_error','STALE_RUNNING_RECOVERY_CODE'}
        constants={}
        owners = [CHANNEL_METADATA, CHANNEL_SERVICE, CHANNEL_PREPARATION, SERVER, *(ROOT/'application/web').glob('*.py')]
        writes_by_owner = {path: 0 for path in owners}
        for path in owners:
            tree=ast.parse(path.read_text(encoding='utf-8'))
            for node in tree.body:
                if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name) \
                        and isinstance(node.value,ast.Constant):
                    constants[node.targets[0].id]=node.value.value
            for node in ast.walk(tree):
                values=[]
                if isinstance(node,ast.Dict):
                    values=[(k.value,v) for k,v in zip(node.keys,node.values) if isinstance(k,ast.Constant) and k.value in ERROR_COLUMNS]
                elif isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Subscript) \
                        and isinstance(node.targets[0].slice,ast.Constant) and node.targets[0].slice.value in ERROR_COLUMNS:
                    values=[(node.targets[0].slice.value,node.value)]
                for column,value in values:
                    writes_by_owner[path]+=1
                    where=f'{path.name}:{value.lineno} {column}'
                    if isinstance(value,ast.Constant):
                        self.assertTrue(value.value is None or CODE.fullmatch(value.value),where)
                    elif isinstance(value,ast.Name):
                        self.assertIn(value.id,allowed_names,where)
                    else:
                        self.assertTrue(isinstance(value,ast.Call) and getattr(value.func,'id','')=='stored_channel_error_code',where)
        self.assertEqual(constants['STALE_RUNNING_RECOVERY_CODE'],'stale_running_recovered')
        self.assertEqual(writes_by_owner[CHANNEL_METADATA], 7)
        self.assertEqual(writes_by_owner[CHANNEL_PREPARATION], 1)
        self.assertTrue(all(count == 0 for path, count in writes_by_owner.items()
                            if path not in {CHANNEL_METADATA, CHANNEL_PREPARATION}))

class ResponseTests(unittest.TestCase):
    def add_result(self, result, *, prepare=False):
        with patch('web.channels.ChannelCommandRepository'), patch('web.channels.ChannelTrashRepository'), \
             patch('web.channels.channel_service.add_channel', return_value=result), \
             patch('web.channels.FrontendRepository') as admitted:
            admitted.return_value.load_channel.return_value = [result.get('channel', {})]
            http = channel_http()
            if prepare:
                return asyncio.run(http.resolve_user_work_channel('@test', None))
            return asyncio.run(endpoint(http.add_router, 'add_channel')(ChannelRequest(url='@test'), None))

    def test_add_channel_returns_sidebar_fields_only(self):
        row = {'channel_id': 'UC1', 'title': 'T', 'is_hidden': False,
               'deep_inventory_error': PROVIDER_TEXT, 'channel_metadata_error': PROVIDER_TEXT,
               'channel_discovery_resume_page_token': 'token', 'deep_bypass_reason': 'x', 'user_input': '@t'}
        result = self.add_result({'status': 'exists', 'title': 'T', 'channel': row, 'hydration': {'channel_id': 'UC1'}})
        self.assertEqual((result['status'], result['channel']['channel_id'], result['channel']['title']),
                         ('exists', 'UC1', 'T'))
        self.assertEqual(result['hydration']['channel_id'], 'UC1')
        self.assertEqual(result['hydration']['status'], 'error')
        self.assertNotIn(KEY, repr(result))
        self.assertEqual(set(result['channel']), set(VISIBLE_CHANNEL_FIELDS))
        self.assertEqual(self.add_result({'error': 'Channel not found'}), {'error': 'Channel not found'})

    def test_http_errors_carry_no_exception_text(self):
        sources = [SERVER, *(ROOT/'application/web').glob('*.py')]
        for path in sources:
            source = path.read_text(encoding='utf-8')
            for unsafe in ('detail={"message": str(error)}', 'configuration is incomplete: {error}',
                           'Backfill failed: {error}', 'error=str(error)'):
                self.assertNotIn(unsafe, source, str(path))
        self.assertEqual(OPERATOR_JOBS_MODE_INVALID['code'], 'operator_jobs_mode_invalid')

    def test_restore_preparation_keeps_the_full_row(self):
        row = {'channel_id': 'UC1', 'user_input': '@test', 'internal_restore_field': 'keep'}
        result = self.add_result({'status': 'prepared', 'channel': row}, prepare=True)
        self.assertIs(result['channel'], row)

if __name__=='__main__':unittest.main()
