"""Saved IDs are local user work, including stale cursor/racing edit failures."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

import unittest
from unittest.mock import Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from saved_collections import SavedCollectionsService, create_saved_collections_router
from saved_work import SavedWorkError
from hot_storage import utc_instant

ID='c0000000-0000-4000-8000-000000000001'
AT='2026-09-26T10:00:00+00:00'

def repository():
    row={'id':ID,'kind':'collection','name':'Local list','revision':1,'member_count':2,
         'created_at':AT,'updated_at':AT,'deleted_at':None}
    members=[{'video_id':item*11,'added_at':AT} for item in ('b','a')]
    repo=Mock()
    repo.get_object.side_effect=lambda identity:dict(row)
    repo.list_members.side_effect=lambda identity,limit,after: [m for m in members if after is None or (utc_instant(m['added_at']),m['video_id'])<(utc_instant(after[0]),after[1])][:limit]
    repo.load_sources.side_effect=AssertionError('API projection is forbidden')
    return repo,row,members

class MemberIdentityTests(unittest.TestCase):
    def test_actual_endpoint_pages_only_saved_identities_and_preserves_cursor_contract(self):
        repo,_,_=repository();app=FastAPI();app.include_router(create_saved_collections_router(repo))
        with TestClient(app) as client:
            first=client.get(f'/saved/collections/{ID}/member-identities?limit=1')
            self.assertEqual(first.status_code,200)
            value=first.json();self.assertEqual(value['members'],[{'video_id':'b'*11,'added_at':AT}])
            self.assertEqual(set(value),{'contract_version','collection','members','next_cursor'})
            second=client.get(f'/saved/collections/{ID}/member-identities',params={'limit':1,'cursor':value['next_cursor']})
            self.assertEqual(second.status_code,200)
            self.assertEqual(second.json()['members'],[{'video_id':'a'*11,'added_at':AT}])
            self.assertIsNone(second.json()['next_cursor'])
        repo.load_sources.assert_not_called()
        # Same cursor is accepted by the enriched service, which only then tries API data.
        repo.load_sources.side_effect=None
        repo.load_sources.return_value=[{'video_id':'a'*11,'source_status':'unavailable',
            'source_reason':'source_missing','channel_id':None,'video_row':None}]
        self.assertEqual(SavedCollectionsService(repo).members(ID,1,value['next_cursor'])['members'][0]['video_id'],'a'*11)

    def test_changed_revision_and_stale_cursor_never_splice_membership(self):
        repo,row,_=repository();service=SavedCollectionsService(repo)
        cursor=service.member_identities(ID,1)['next_cursor'];row['revision']=2
        with self.assertRaises(SavedWorkError) as caught:service.member_identities(ID,1,cursor)
        self.assertEqual(caught.exception.status_code,409)
        before=dict(row);after={**row,'revision':3};repo.get_object.side_effect=[before,after]
        with self.assertRaises(SavedWorkError) as caught:service.member_identities(ID)
        self.assertEqual(caught.exception.code,'collection_changed')
        repo.load_sources.assert_not_called()

    def test_incomplete_or_malformed_saved_rows_fail_without_api_fallback(self):
        for rows in ([{'video_id':'bad','added_at':AT}],[],[{'video_id':'a'*11,'added_at':'invalid'}]):
            repo,_,_=repository();repo.list_members.side_effect=None;repo.list_members.return_value=rows
            with self.subTest(rows=rows),self.assertRaises(SavedWorkError):
                SavedCollectionsService(repo).member_identities(ID)
            repo.load_sources.assert_not_called()

if __name__=='__main__':unittest.main()
