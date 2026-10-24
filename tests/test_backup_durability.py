"""A backup of any stored user work can be exported and restored. Source only: no server or database."""
from pathlib import Path
import re
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from saved_work import SavedWorkError, saved_name  # noqa: E402
from user_work_backup import UserWorkError, text  # noqa: E402

SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')
from web.common import has_control_characters
from web.models import FolderRequest, LabelCreateRequest
from web.organization import create_organization_router
from fastapi import HTTPException
from http_test_support import cache_stub, library_stub, endpoint
DB_CONTROL=re.compile('[\x01-\x1f\x7f]')

class StoredNameTests(unittest.TestCase):
    def test_database_rejects_control_characters_in_exported_names(self):
        for constraint in ('folders_name_check','channel_labels_name_check','video_type_review_history_reason_check',
                           'video_type_review_cases_decision_reason_check'):
            line=next(l for l in SCHEMA.splitlines() if f'CONSTRAINT {constraint} ' in l)
            self.assertIn("!~ '[\\u0001-\\u001f\\u007f]'::text",line,constraint)

    def test_names_the_database_accepts_are_exported(self):
        # Every name that passes the DB rule and the API rule also passes the backup rule.
        for name in ('Музыка','🎵 Mix','a\u0085b','Ωmega','x'*80):
            self.assertIsNone(DB_CONTROL.search(name))
            self.assertEqual(saved_name(name),name)
            self.assertEqual(text(name,120),name)

    def test_folder_creation_uses_the_backup_rule(self):
        router = create_organization_router(cache=cache_stub(), library=library_stub(), database=None)
        create = endpoint(router, 'create_folder')
        for bad in ('a\tb', '\ufeff', 'x\x01'):
            with self.assertRaises(SavedWorkError): saved_name(bad)
            with self.assertRaises(HTTPException) as refused: create(FolderRequest(name=bad))
            self.assertEqual(refused.exception.status_code, 400)

    def test_label_creation_rejects_control_characters(self):
        self.assertTrue(has_control_characters('x\x01y'))
        self.assertTrue(has_control_characters('x\x7f'))
        self.assertFalse(has_control_characters('Топ label'))
        router = create_organization_router(cache=cache_stub(), library=library_stub(), database=None)
        with self.assertRaises(HTTPException) as refused:
            endpoint(router, 'create_channel_label')(LabelCreateRequest(name='x\x01y'))
        self.assertEqual(refused.exception.status_code, 400)
        with self.assertRaises(UserWorkError): text('x\x01y', 40)

class TrashedChannelTests(unittest.TestCase):
    """Channels in the trash are backed up with their organization and original removal time."""

    def document(self, **channel):
        base={'id':'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','user_input':'https://www.youtube.com/@one','folder_id':None,
              'parent_ref':None,'is_hidden':False,'added_at':'2026-09-01T00:00:00+00:00','label_keys':[],'saved_views':[]}
        base.update(channel)
        return {'format':'let-it-cook-user-work','version':1,'created_at':'2026-09-20T00:00:00+00:00',
                'user_work':{'folders':[],'labels':[],'channels':[base],'collections':[],'reviews':[]},
                'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]}}

    def test_hidden_channel_keeps_its_removal_time(self):
        from user_work_backup import normalize_backup, import_payload
        doc=normalize_backup(self.document(is_hidden=True,hidden_at='2026-09-10T00:00:00+00:00'))
        self.assertEqual(doc['user_work']['channels'][0]['hidden_at'],'2026-09-10T00:00:00+00:00')
        self.assertEqual(import_payload(doc)['channels'][0]['hidden_at'],'2026-09-10T00:00:00+00:00')

    def test_backups_without_removal_time_still_load(self):
        from user_work_backup import normalize_backup
        for hidden in (False,True):
            self.assertNotIn('hidden_at',normalize_backup(self.document(is_hidden=hidden))['user_work']['channels'][0])

    def test_removal_time_only_for_a_hidden_channel_and_not_in_the_future(self):
        from user_work_backup import normalize_backup
        for channel in ({'is_hidden':False,'hidden_at':'2026-09-10T00:00:00+00:00'},{'is_hidden':True,'hidden_at':'2999-01-01T00:00:00+00:00'}):
            with self.assertRaises(UserWorkError):
                normalize_backup(self.document(**channel))

    def test_trash_clock_constraint_and_frontend_field_are_kept(self):
        self.assertIn('CONSTRAINT user_channel_restore_items_hidden_at_check CHECK (((hidden_at IS NULL) OR (is_hidden AND isfinite(hidden_at))))',SCHEMA)
        frontend=(ROOT/'application/frontend/src/explorer/userWorkBackup.ts').read_text(encoding='utf-8')
        self.assertIn("'saved_views', 'label_keys'], ['hidden_at']);",frontend)

class OwnershipTests(unittest.TestCase):
    """Saved videos and manual decisions keep their channel through a backup and restore."""

    def document(self, member=None, review_ref=None):
        doc=TrashedChannelTests().document(is_hidden=True,hidden_at='2026-09-10T00:00:00+00:00')
        ref=doc['user_work']['channels'][0]['id']
        doc['user_work']['collections']=[{'id':'bbbbbbbb-aaaa-4aaa-8aaa-aaaaaaaaaaaa','name':'C','revision':1,
            'created_at':'2026-09-01T00:00:00+00:00','updated_at':'2026-09-01T00:00:00+00:00',
            'members':[member or {'video_id':'AAAAAAAAAA1','added_at':'2026-09-01T00:00:00+00:00','channel_ref':ref}]}]
        return doc,ref

    def test_member_owner_survives_normalization(self):
        from user_work_backup import normalize_backup, import_payload
        doc,ref=self.document()
        member=import_payload(normalize_backup(doc))['collections'][0]['members'][0]
        self.assertEqual(member['channel_ref'],ref)

    def test_owner_must_be_a_channel_of_the_backup(self):
        from user_work_backup import normalize_backup
        doc,_=self.document({'video_id':'AAAAAAAAAA1','added_at':'2026-09-01T00:00:00+00:00','channel_ref':'cccccccc-aaaa-4aaa-8aaa-aaaaaaaaaaaa'})
        with self.assertRaises(UserWorkError):
            normalize_backup(doc)

    def test_current_schema_binds_and_references_the_restore_owner(self):
        self.assertIn('NEW.user_channel_ref:=coalesce(owner_id,NEW.user_channel_ref,',SCHEMA)
        for table in ('saved_collection_members','video_type_review_cases'):
            self.assertIn(f'ADD CONSTRAINT {table}_restore_owner_ref_fkey FOREIGN KEY (restore_owner_ref) REFERENCES public.user_channel_restore_items(id);',SCHEMA)

class StaleBookmarkTests(unittest.TestCase):
    def test_a_bookmark_of_a_removed_channel_does_not_break_the_backup(self):
        from user_work_backup import build_backup
        snapshot={'folders':[],'labels':[],'channels':[],'assignments':[],'saved_objects':[],'members':[],'pending':[],'reviews':[]}
        out=build_backup(snapshot,{'catalogue_density':'large','bookmarked_channel_ids':['UC'+'a'*22],
            'pending_bookmark_refs':['dddddddd-aaaa-4aaa-8aaa-aaaaaaaaaaaa'],'collapsed_folder_ids':[]})
        self.assertEqual(out['backup']['browser_preferences']['bookmarked_channel_refs'],[])

class BackupFormatNameTests(unittest.TestCase):
    def test_old_and_current_names_restore_identical_work_for_both_supported_versions(self):
        from copy import deepcopy
        from user_work_backup import normalize_backup, import_payload, encode_backup, decode_backup
        for version in (1, 2):
            old = TrashedChannelTests().document(is_hidden=True, hidden_at='2026-09-10T00:00:00+00:00')
            old['version'] = version
            original = deepcopy(old)
            current = deepcopy(old)
            current['format'] = 'leti-cook-user-work'
            normalized = normalize_backup(old)
            self.assertEqual(normalized['format'], 'leti-cook-user-work')
            self.assertEqual(normalized, normalize_backup(current))
            self.assertEqual(import_payload(old), import_payload(current))
            self.assertEqual(decode_backup(encode_backup(old)), normalized)
            self.assertEqual(old, original)
            self.assertEqual(normalized['user_work']['channels'][0]['hidden_at'],
                             '2026-09-10T00:00:00+00:00')

    def test_unknown_format_is_still_rejected(self):
        from user_work_backup import normalize_backup
        for invalid in ('let-it-cook-user-work-v2', 'leti-cook', 'LETI-COOK-USER-WORK', None, []):
            value = TrashedChannelTests().document()
            value['format'] = invalid
            with self.subTest(format=invalid), self.assertRaises(UserWorkError):
                normalize_backup(value)


if __name__=='__main__':unittest.main()
