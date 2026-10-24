"""Folders and labels can be renamed and deleted; backups keep the preset label as it is (T-06). No database."""
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from fastapi import HTTPException  # noqa: E402
from user_work_backup import UserWorkError, normalize_backup  # noqa: E402

from http_test_support import cache_stub, library_stub, endpoint
from unittest.mock import patch
from web.organization import create_organization_router
FOLDER = 'f0000000-0000-4000-8000-000000000006'
OTHER = 'f0000000-0000-4000-8000-000000000007'
CUSTOM = 'custom:0b000000-0000-4000-8000-000000000006'


class Request:
    def __init__(self, name):
        self.name = name


class FolderRouteTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls

        class Repository:
            def __init__(self, db): pass
            def rename_folder(self, folder_id, name): calls.append(('rename', folder_id, name))
            def delete_folder(self, folder_id): calls.append(('delete', folder_id))
        router = create_organization_router(cache=cache_stub(), database=None,
            library=library_stub(load_folders=lambda: [{'id': FOLDER, 'name': 'Music'}, {'id': OTHER, 'name': 'News'}]))
        self.ns = {name: endpoint(router, name) for name in ('rename_folder', 'delete_folder')}
        mock = patch('web.organization.ChannelOrganizationRepository', Repository)
        mock.start(); self.addCleanup(mock.stop)

    def status(self, call):
        with self.assertRaises(HTTPException) as raised:
            call()
        return raised.exception.status_code

    def test_rename_checks_name_existence_and_duplicates(self):
        self.assertEqual(self.ns['rename_folder'](FOLDER, Request('  Songs ')), {'id': FOLDER, 'name': 'Songs'})
        self.assertEqual(self.ns['rename_folder'](FOLDER, Request('music')), {'id': FOLDER, 'name': 'music'})  # own name
        self.assertEqual(self.status(lambda: self.ns['rename_folder'](FOLDER, Request('NEWS'))), 409)
        self.assertEqual(self.status(lambda: self.ns['rename_folder'](str(uuid.uuid4()), Request('X'))), 404)
        self.assertEqual(self.status(lambda: self.ns['rename_folder']('not-a-uuid', Request('X'))), 404)
        self.assertEqual(self.status(lambda: self.ns['rename_folder'](FOLDER, Request(''))), 400)
        self.assertEqual(self.calls, [('rename', FOLDER, 'Songs'), ('rename', FOLDER, 'music')])

    def test_delete_keeps_the_channels(self):
        self.assertEqual(self.ns['delete_folder'](FOLDER.upper()), {'status': 'deleted', 'id': FOLDER})
        self.assertEqual(self.status(lambda: self.ns['delete_folder'](str(uuid.uuid4()))), 404)
        self.assertEqual(self.calls, [('delete', FOLDER)])


class LabelRouteTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        rows = [{'label_key': 'monetized', 'name': 'Monetized', 'label_type': 'preset'},
                {'label_key': CUSTOM, 'name': 'Fav', 'label_type': 'custom'}]

        class Repository:
            def __init__(self, db): pass
            def load_labels(self): return [dict(r) for r in rows]
            def rename_label(self, key, name): calls.append(('rename', key, name))
            def delete_label(self, key): calls.append(('delete', key))
        router = create_organization_router(cache=cache_stub(), library=library_stub(), database=None)
        self.ns = {name: endpoint(router, name) for name in ('rename_channel_label', 'delete_channel_label')}
        mock = patch('web.organization.ChannelLabelRepository', Repository)
        mock.start(); self.addCleanup(mock.stop)

    def status(self, call):
        with self.assertRaises(HTTPException) as raised:
            call()
        return raised.exception.status_code

    def test_the_preset_label_is_renamed_and_deleted_like_any_other(self):
        self.assertEqual(self.ns['rename_channel_label']('monetized', Request(' Paid  work '))['name'], 'Paid work')
        self.assertEqual(self.ns['delete_channel_label']('monetized'), {'status': 'deleted', 'label_key': 'monetized'})
        self.assertEqual(self.calls, [('rename', 'monetized', 'Paid work'), ('delete', 'monetized')])

    def test_duplicates_unknown_keys_and_bad_names_are_refused(self):
        self.assertEqual(self.status(lambda: self.ns['rename_channel_label'](CUSTOM, Request('monetized'))), 409)
        self.assertEqual(self.status(lambda: self.ns['rename_channel_label']('custom:nope', Request('X'))), 404)
        self.assertEqual(self.status(lambda: self.ns['delete_channel_label']('custom:' + str(uuid.uuid4()))), 404)
        self.assertEqual(self.status(lambda: self.ns['rename_channel_label'](CUSTOM, Request('x' * 41))), 400)
        self.assertEqual(self.calls, [])


def backup(version, labels, label_keys=()):
    return {'format': 'let-it-cook-user-work', 'version': version, 'created_at': '2026-09-20T00:00:00+00:00',
            'user_work': {'folders': [], 'labels': labels, 'collections': [], 'reviews': [],
                          'channels': [{'id': 'a0000000-0000-4000-8000-000000000006', 'user_input': 'https://www.youtube.com/@probe',
                                        'folder_id': None, 'parent_ref': None, 'is_hidden': False,
                                        'added_at': '2026-09-19T00:00:00+00:00', 'label_keys': list(label_keys), 'saved_views': []}]},
            'browser_preferences': {'catalogue_density': 'large', 'bookmarked_channel_refs': [], 'collapsed_folder_ids': []}}


def label(key, name, kind):
    return {'label_key': key, 'name': name, 'label_type': kind, 'created_at': '2026-09-01T00:00:00+00:00'}


class BackupFormatTests(unittest.TestCase):
    def test_format_1_implies_the_preset_label_unchanged(self):
        result = normalize_backup(backup(1, [label(CUSTOM, 'Fav', 'custom')], ['monetized', CUSTOM]))
        self.assertEqual(result['version'], 2)
        self.assertEqual({(r['label_key'], r['name']) for r in result['user_work']['labels']},
                         {(CUSTOM, 'Fav'), ('monetized', 'Monetized')})
        with self.assertRaises(UserWorkError):
            normalize_backup(backup(1, [label('monetized', 'Paid', 'preset')]))

    def test_format_2_keeps_a_renamed_or_deleted_preset_label(self):
        renamed = normalize_backup(backup(2, [label('monetized', 'Paid', 'preset'), label(CUSTOM, 'Monetized', 'custom')],
                                          ['monetized']))
        self.assertEqual({(r['label_key'], r['name']) for r in renamed['user_work']['labels']},
                         {('monetized', 'Paid'), (CUSTOM, 'Monetized')})
        deleted = normalize_backup(backup(2, [label(CUSTOM, 'Fav', 'custom')], [CUSTOM]))
        self.assertEqual([r['label_key'] for r in deleted['user_work']['labels']], [CUSTOM])
        # A deleted preset label cannot still be assigned.
        with self.assertRaises(UserWorkError):
            normalize_backup(backup(2, [], ['monetized']))
        with self.assertRaises(UserWorkError):
            normalize_backup(backup(2, [label('monetized', 'Paid', 'custom')]))


if __name__ == '__main__':
    unittest.main()
