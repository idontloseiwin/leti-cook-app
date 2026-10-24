"""Local organisation stays readable without consuming any API projection."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from types import SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from web.organization import create_organization_router
from web.models import FolderAssign, LabelAssignRequest
from http_test_support import endpoint, cache_stub
import unittest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from web.library import create_library_http
from db.reads.frontend import FrontendRepository, CHANNEL_ROW_LIMIT

class Database:
    def __init__(self, added_at='2020-01-02T03:04:05+00:00'):
        self.calls=[]
        self.added_at=added_at
    def read_rows(self,operation,sql,params=None,**kwargs):
        self.calls.append((operation,sql,kwargs))
        rows={
            'frontend_load_visible_channel_user_work':[{'channel_id':'channel','folder_id':'folder','added_at':self.added_at}],
            'frontend_load_folder_rows':[{'id':'folder','name':'Local folder'}],
            'channel_labels_list':[{'label_key':'label','name':'Local label','label_type':'user'}],
            'channel_labels_assignments':[{'channel_id':'channel','label_key':'label'}],
        }
        if operation not in rows:raise AssertionError('Unexpected source-bearing query: '+operation)
        return rows[operation]

class SidebarUserDataTests(unittest.TestCase):
    def test_endpoint_reads_only_identities_and_user_work_without_api_projection(self):
        database=Database()
        cache=SimpleNamespace(cached_read=lambda key,seconds,loader:loader())
        library=create_library_http(cache=cache,database=database)
        app=FastAPI();app.include_router(library.router)
        with TestClient(app) as client:
            response=client.get('/sidebar-user-data')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{
            'channels':[{'channel_id':'channel','folder_id':'folder','added_at':database.added_at,'label_keys':['label']}],
            'folders':[{'id':'folder','name':'Local folder'}],
            'labels':[{'label_key':'label','name':'Local label','label_type':'user'}]})
        self.assertEqual(library.load_visible_channel_ids(),{'channel'})
        for operation,sql,_ in database.calls:
            self.assertNotIn('readable.channels',sql)
            self.assertNotIn('source_expires_at',sql)
            self.assertNotIn('title',sql)

    def test_local_assignments_never_load_api_fields_or_register_source_deadline(self):
        from hot_storage import capture_read_lifetime
        database=Database()
        identities=[]
        def read_row(operation,sql,params):
            self.assertEqual(operation,'frontend_load_visible_channel_user_work_identity')
            self.assertIn('FROM readable.channel_user_work',sql)
            self.assertNotIn('source_expires_at',sql)
            identities.append(params[0])
            return {'channel_id':'channel'} if params[0]=='channel' else None
        database.read_row=read_row
        library=create_library_http(cache=cache_stub(),database=database)
        router=create_organization_router(cache=cache_stub(),library=library,database=database)
        with patch('web.organization.ChannelOrganizationRepository') as folders, \
             patch('web.organization.ChannelLabelRepository') as labels, capture_read_lifetime() as lifetime:
            labels.return_value.load_labels.return_value=[{'label_key':'monetized'}]
            self.assertEqual(endpoint(router,'assign_folder')('channel',FolderAssign(folder_id='')),{'status':'ok'})
            self.assertEqual(endpoint(router,'assign_channel_label')('channel','monetized',LabelAssignRequest(enabled=True)),{'status':'ok','enabled':True})
            with self.assertRaises(HTTPException):
                endpoint(router,'assign_folder')('hidden',FolderAssign(folder_id=''))
            self.assertIsNone(lifetime.expires_at)
            folders.return_value.assign_folder.assert_called_once_with('channel',None)
            labels.return_value.assign_label.assert_called_once_with('channel','monetized')
        self.assertEqual(identities,['channel','channel','hidden'])

    def test_local_sidebar_cache_is_invalidated_after_organization_changes(self):
        from web.cache import create_read_cache
        cache = create_read_cache()
        self.assertEqual(cache.cached_read('sidebar-user-data', 20, lambda: ['before']), ['before'])
        cache.clear_frontend_read_cache()
        self.assertEqual(cache.cached_read('sidebar-user-data', 20, lambda: ['after']), ['after'])

    def test_repository_uses_reader_user_work_view_with_same_bounded_limit(self):
        database=Database()
        self.assertEqual(FrontendRepository(database).load_visible_channel_user_work(),
                         [{'channel_id':'channel','folder_id':'folder','added_at':database.added_at}])
        _,sql,options=database.calls[0]
        self.assertIn('SELECT channel_id,folder_id,added_at',sql)
        self.assertIn('FROM readable.channel_user_work',sql)
        self.assertIn('WHERE NOT is_hidden',sql)
        self.assertEqual(options,{'max_rows':CHANNEL_ROW_LIMIT})

    def test_tracking_date_is_local_even_when_old_or_unknown(self):
        from hot_storage import capture_read_lifetime
        for added_at in ('2020-01-02T03:04:05+00:00', None):
            with self.subTest(added_at=added_at):
                database = Database(added_at)
                library = create_library_http(cache=cache_stub(), database=database)
                with capture_read_lifetime() as lifetime:
                    result = endpoint(library.router, 'sidebar_user_data')()
                self.assertEqual(result['channels'][0]['added_at'], added_at)
                self.assertIsNone(lifetime.expires_at)

    def test_regular_sidebar_keeps_tracking_date_when_provider_profile_expired(self):
        from hot_storage import capture_read_lifetime
        from web.constants import VISIBLE_CHANNEL_FIELDS

        class RegularDatabase(Database):
            def read_rows(self, operation, sql, params=None, **kwargs):
                if operation == 'frontend_load_channel_rows':
                    statement = sql.as_string()
                    self.calls.append((operation, statement, kwargs))
                    return [{'channel_id': 'channel', 'folder_id': 'folder',
                             'added_at': self.added_at, 'is_hidden': False,
                             'title': 'Old provider title', 'channel_metadata_status': 'ok',
                             'channel_metadata_checked_at': '2020-01-02T03:04:05+00:00',
                             'source_expires_at': None}]
                return super().read_rows(operation, sql, params, **kwargs)

        database = RegularDatabase()
        library = create_library_http(cache=cache_stub(), database=database)
        with capture_read_lifetime() as lifetime:
            result = endpoint(library.router, 'sidebar_data')()
        self.assertEqual(result['channels'][0]['added_at'], database.added_at)
        self.assertIsNone(result['channels'][0]['title'])
        self.assertIsNone(lifetime.expires_at)
        query = next(sql for name, sql, _ in database.calls if name == 'frontend_load_channel_rows')
        self.assertIn('"added_at"', query)
        self.assertIn('added_at', VISIBLE_CHANNEL_FIELDS, 'Add-channel replies carry the same tracking date.')
if __name__=='__main__':unittest.main()
