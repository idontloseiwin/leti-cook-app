"""Assigning a folder checks the channel and the folder first (audit S6-7). No database."""
from http_test_support import cache_stub, library_stub, endpoint
from fastapi import HTTPException
from web.models import FolderAssign
from web.organization import create_organization_router
from unittest.mock import patch
import unittest

FOLDER='f0000000-0000-4000-8000-000000000001'


def assign_folder(visible, assigned):
    def require_visible_channel_user_work(channel_id):
        if channel_id not in visible:
            raise HTTPException(404, 'Channel not found')
        return {'channel_id': channel_id}
    class Repository:
        def __init__(self, db): pass
        def assign_folder(self, channel_id, folder_id): assigned.append((channel_id, folder_id))
    router = create_organization_router(cache=cache_stub(), database=None,
        library=library_stub(require_visible_channel_user_work=require_visible_channel_user_work,
                             load_folders=lambda: [{'id': FOLDER, 'name': 'Music'}]))
    route = endpoint(router, 'assign_folder')
    def call(channel_id, folder_id):
        with patch('web.organization.ChannelOrganizationRepository', Repository):
            return route(channel_id, FolderAssign(folder_id=folder_id))
    return call

class FolderAssignmentTests(unittest.TestCase):
    def test_known_folder_and_unassignment_are_written(self):
        assigned=[]
        route=assign_folder({'UC1'},assigned)
        self.assertEqual(route('UC1',FOLDER),{'status':'ok'})
        self.assertEqual(route('UC1',''),{'status':'ok'})
        self.assertEqual(assigned,[('UC1',FOLDER),('UC1',None)])

    def test_unknown_folder_or_hidden_channel_is_refused_before_writing(self):
        assigned=[]
        route=assign_folder({'UC1'},assigned)
        for channel_id,folder_id,detail in (('UC1','f0000000-0000-4000-8000-00000000ffff','Folder not found'),
                                            ('UChidden',FOLDER,'Channel not found')):
            with self.assertRaises(HTTPException) as raised:
                route(channel_id,folder_id)
            self.assertEqual((raised.exception.status_code,raised.exception.detail),(404,detail))
        self.assertEqual(assigned,[])

if __name__=='__main__':unittest.main()
