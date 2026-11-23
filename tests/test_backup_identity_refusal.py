"""A stored ID that is not a UUIDv4 stops the backup with a refusal that names the item.

Leti Cook makes only version 4 IDs; another one comes from manual SQL or a data
migration. Before, the first such row failed the whole export with a generic code
("A UUIDv4 saved view ID is required.") and named nothing, and a channel or folder
with one also failed the status read that opens the Backup dialog. No database.
"""
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from saved_work import SavedWorkError  # noqa: E402
from user_work_backup import (MESSAGE_LIMIT, PAGE_UNSAFE_TEXT, UNSUPPORTED_IDENTITY, UserWorkError,  # noqa: E402
                              build_backup, normalize_backup)
from user_work_service import UserWorkService  # noqa: E402

STAMP = '2026-09-01T00:00:00+00:00'
CHANNEL = 'UC' + 'a' * 22
V4 = 'a0000000-0000-4000-8000-00000000000%d'
NIL_ONE = '00000000-0000-0000-0000-000000000001'  # UUID(int=1), as the SCALE fixture made them
V1 = '6ba7b810-9dad-11d1-80b4-00c04fd430c%d'      # version 1


def channel(user_work_id, channel_id=CHANNEL):
    return {'channel_id': channel_id, 'user_work_id': user_work_id, 'user_input': None, 'folder_id': None,
            'parent_channel_id': None, 'is_hidden': False, 'hidden_at': None, 'added_at': STAMP}


def saved(identity, name, kind='collection', channel_id=None, conditions=None):
    return {'kind': kind, 'id': identity, 'name': name, 'channel_id': channel_id, 'conditions': conditions,
            'revision': 1, 'created_at': STAMP, 'updated_at': STAMP}


def snapshot(**sections):
    base = {key: [] for key in ('folders', 'labels', 'channels', 'assignments', 'saved_objects', 'members',
                                'pending', 'reviews')}
    base.update(sections)
    return base


def review(case_id, request_id):
    return {'case_id': case_id, 'revision': 2, 'state': 'resolved', 'decision_type': 'short', 'decision_reason': 'r',
            'decision_at': STAMP, 'decision_revision': 2, 'channel_ref': None,
            'history': [{'revision': 2, 'video_type': 'short', 'reason': 'r', 'decided_at': STAMP,
                         'request_id': request_id, 'expected_revision': 1}]}


class RefusalTests(unittest.TestCase):
    def refusal(self, data):
        with self.assertRaises(UserWorkError) as raised:
            build_backup(data, now=STAMP)
        error = raised.exception
        self.assertEqual((error.code, error.status), (UNSUPPORTED_IDENTITY, 422))
        return error

    def test_the_audit_case_names_the_channel_instead_of_a_generic_code(self):
        # The SCALE round trip failed on the saved video whose owner is this channel.
        data = snapshot(channels=[channel(NIL_ONE)], saved_objects=[saved(V4 % 1, 'Picks')],
                        members=[{'collection_id': V4 % 1, 'video_id': 'AAAAAAAAAA1', 'added_at': STAMP,
                                  'channel_ref': NIL_ONE}])
        error = self.refusal(data)
        self.assertEqual(error.details, {'count': 1, 'items': [{'kind': 'channel', 'id': NIL_ONE, 'channel_id': CHANNEL}]})
        self.assertEqual(error.message,
                         'Backup stopped: the channel ' + CHANNEL + ' has an internal ID that Leti Cook did not '
                         'create, so a backup cannot hold it. Nothing was downloaded. Undo the database change that '
                         'made it; see “Backup is refused” in Troubleshooting.')
        self.assertEqual(error.answer(), {'code': UNSUPPORTED_IDENTITY, 'message': error.message, **error.details})

    def test_every_kind_of_item_is_found_and_the_others_are_counted(self):
        data = snapshot(
            channels=[channel(V4 % 1), channel(V1 % 1, 'UC' + 'b' * 22)],
            pending=[{'id': V1 % 2, 'user_input': None, 'folder_id': None, 'parent_ref': None, 'is_hidden': False,
                      'hidden_at': None, 'added_at': None, 'label_keys': [], 'revision': 1,
                      'saved_views': [{'id': V1 % 3, 'name': 'Pending view', 'revision': 1, 'created_at': STAMP,
                                       'updated_at': STAMP, 'conditions': {}}]}],
            folders=[{'id': V4 % 2, 'name': 'Fine', 'created_at': STAMP}, {'id': V1 % 4, 'name': 'Music', 'created_at': STAMP}],
            labels=[{'label_key': 'monetized', 'name': 'Monetized', 'label_type': 'preset', 'created_at': STAMP},
                    {'label_key': 'custom:' + V1 % 5, 'name': 'Top', 'label_type': 'custom', 'created_at': STAMP}],
            saved_objects=[saved(V1 % 6, 'Study', kind='view'), saved(V1 % 7, 'Clips')],
            reviews=[review(V1 % 8, V4 % 3), review(V4 % 4, V1 % 9)])
        error = self.refusal(data)
        self.assertEqual([(item['kind'], item.get('name', item.get('channel_id'))) for item in error.details['items']],
                         [('channel', 'UC' + 'b' * 22), ('restored_channel', None), ('saved_item', 'Pending view'),
                          ('folder', 'Music'), ('label', 'Top'), ('saved_item', 'Study'), ('collection', 'Clips'),
                          ('video_type_decision', None), ('video_type_decision', None)])
        self.assertEqual([item['id'] for item in error.details['items']][-2:], [V1 % 8, V1 % 9])
        self.assertEqual(error.details['count'], 9)
        self.assertIn('the channel UC' + 'b' * 22 + ' has', error.message)
        self.assertIn('Nothing was downloaded. 8 more items have the same problem.', error.message)

    def test_a_named_item_says_it_can_be_recreated(self):
        error = self.refusal(snapshot(folders=[{'id': V1 % 1, 'name': 'Music', 'created_at': STAMP}]))
        self.assertIn('Backup stopped: the folder “Music” has', error.message)
        self.assertIn('Undo the database change that made it, or delete the item and create it again;', error.message)
        decision = self.refusal(snapshot(reviews=[review(V1 % 1, V4 % 1)]))
        self.assertIn('Backup stopped: a video type decision made on 2026-09-01 has', decision.message)
        self.assertNotIn('delete the item', decision.message)

    def test_more_than_ten_items_are_counted_and_ten_listed(self):
        error = self.refusal(snapshot(folders=[{'id': '6ba7b810-9dad-11d1-80b4-%012d' % i, 'name': 'F%d' % i,
                                                'created_at': STAMP} for i in range(12)]))
        self.assertEqual((error.details['count'], len(error.details['items'])), (12, 10))
        self.assertIn('11 more items have the same problem.', error.message)

    def test_version_4_ids_and_other_malformed_values_keep_their_old_paths(self):
        built = build_backup(snapshot(channels=[channel(V4 % 1)]), now=STAMP)
        self.assertEqual(built['backup']['user_work']['channels'][0]['id'], V4 % 1)
        # Not a UUID at all is a damaged answer, not an item to name: the generic refusal stays.
        with self.assertRaises(UserWorkError) as raised:
            build_backup(snapshot(channels=[channel('not-a-uuid')]), now=STAMP)
        self.assertEqual(raised.exception.code, 'invalid_user_work_backup')
        # A file holding such an ID still reads as an unsupported backup: restore is unchanged.
        document = {'format': 'leti-cook-user-work', 'version': 1, 'created_at': STAMP,
                    'user_work': {'folders': [{'id': V1 % 1, 'name': 'Music', 'created_at': STAMP}], 'labels': [],
                                  'channels': [], 'collections': [], 'reviews': []},
                    'browser_preferences': {'catalogue_density': 'large', 'bookmarked_channel_refs': [],
                                            'collapsed_folder_ids': []}}
        with self.assertRaises(UserWorkError) as raised:
            normalize_backup(document)
        self.assertEqual((raised.exception.code, raised.exception.status), ('invalid_user_work_backup', 422))


class PageMessageTests(unittest.TestCase):
    def test_the_check_matches_the_page_rule(self):
        source = (ROOT / 'application/frontend/src/serverText.ts').read_text(encoding='utf-8')
        page = re.search(r'^const UNSAFE_TEXT = /(.*)/i;$', source, re.MULTILINE).group(1)
        self.assertEqual(page.replace('\\/', '/'), PAGE_UNSAFE_TEXT.pattern)
        api = (ROOT / 'application/frontend/src/explorer/userWorkBackupApi.ts').read_text(encoding='utf-8')
        # No fixed wording for this code: the page shows the server's sentence.
        self.assertIn('const message = errorMessages[code] ?? plainServerText(detail.message, %d)' % MESSAGE_LIMIT, api)
        self.assertNotIn(UNSUPPORTED_IDENTITY, api)

    def test_a_name_the_page_would_drop_is_left_out(self):
        # A second item adds "1 more item has the same problem." to every sentence.
        for name, shown in (('Notes https://example.invalid/x', None), ('x' * 45, None),
                            ('A' + ' long' * 23, 'A' + ' long' * 11 + ' lo…'), ('Music', 'Music'),
                            # 60 emoji fit in Python's 400 characters but are 120 of the page's UTF-16 units
                            ('\U0001F3B5' * 60, None)):
            with self.subTest(name=name):
                with self.assertRaises(UserWorkError) as raised:
                    build_backup(snapshot(saved_objects=[saved(V1 % 1, name), saved(V1 % 2, 'Other')]), now=STAMP)
                message = raised.exception.message
                self.assertIn('1 more item has the same problem.', message)
                self.assertIsNone(PAGE_UNSAFE_TEXT.search(message))
                self.assertLessEqual(len(message.encode('utf-16-le')) // 2, MESSAGE_LIMIT)
                if shown is None:
                    self.assertIn('Backup stopped: a collection created on 2026-09-01 has', message)
                else:
                    self.assertIn('the collection “%s” has' % shown, message)
                self.assertEqual(raised.exception.details['items'][0]['name'], name)


class StatusTests(unittest.TestCase):
    def status(self, mappings, refs, folders):
        repository = Mock()
        repository.status.return_value = {
            'can_restore': False, 'channel_mappings': mappings, 'user_channel_refs': refs, 'folder_ids': folders,
            'pending': [], 'reconnect': {}}
        return UserWorkService(repository).status()

    def test_ids_of_another_version_are_left_out_so_the_dialog_opens(self):
        result = self.status([{'id': V4 % 1, 'channel_id': CHANNEL}, {'id': NIL_ONE, 'channel_id': 'UC' + 'b' * 22}],
                             [V4 % 1, NIL_ONE], [V1 % 1, V4 % 2])
        self.assertEqual(result['channel_mappings'], [{'id': V4 % 1, 'channel_id': CHANNEL}])
        self.assertEqual((result['user_channel_refs'], result['folder_ids']), ([V4 % 1], [V4 % 2]))

    def test_a_value_that_is_not_a_uuid_is_still_refused(self):
        for mappings, refs, folders in (([{'id': 'x', 'channel_id': CHANNEL}], [], []), ([], ['x'], []), ([], [], ['x'])):
            with self.subTest(mappings=mappings, refs=refs, folders=folders):
                with self.assertRaises(SavedWorkError):
                    self.status(mappings, refs, folders)


class RouteTests(unittest.TestCase):
    def test_the_backup_route_sends_the_sentence_and_the_items(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from user_work_service import create_user_work_router
        repository = Mock()
        repository.snapshot.return_value = snapshot(channels=[channel(NIL_ONE)])
        app = FastAPI()
        app.include_router(create_user_work_router(repository))
        with TestClient(app) as client:
            answer = client.post('/user-work/backup', headers={'x-saved-work': '1'}, json={'browser_preferences': {
                'catalogue_density': 'large', 'bookmarked_channel_ids': [], 'pending_bookmark_refs': [],
                'collapsed_folder_ids': []}})
        self.assertEqual(answer.status_code, 422, answer.text)
        body = answer.json()
        self.assertEqual(set(body), {'code', 'message', 'count', 'items'})
        self.assertEqual((body['code'], body['count'], body['items']),
                         (UNSUPPORTED_IDENTITY, 1, [{'kind': 'channel', 'id': NIL_ONE, 'channel_id': CHANNEL}]))
        self.assertIn(CHANNEL, body['message'])
        self.assertEqual(answer.headers['cache-control'], 'no-store')


if __name__ == '__main__':
    unittest.main()
