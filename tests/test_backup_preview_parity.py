"""A backup preview accepts exactly what a restore will accept (audit S3-7). No database."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import user_work_backup  # noqa: E402
from user_work_backup import UserWorkError, encode_backup, import_payload, normalize_backup  # noqa: E402
from user_work_service import UserWorkService  # noqa: E402

CASE='c0000000-0000-4000-8000-000000000001'
REQUEST='e0000000-0000-4000-8000-000000000001'

def document(decision_revision=2, collections=()):
    review={'case_id':CASE,'revision':2,'state':'resolved','decision_type':'long','decision_reason':'Checked',
            'decision_at':'2026-09-02T00:00:00+00:00','decision_revision':decision_revision,
            'history':[{'revision':2,'video_type':'long','reason':'Checked','decided_at':'2026-09-02T00:00:00+00:00',
                        'request_id':REQUEST,'expected_revision':1}]}
    return {'format':'let-it-cook-user-work','version':1,'created_at':'2026-09-20T00:00:00+00:00',
            'user_work':{'folders':[],'labels':[],'channels':[],'collections':list(collections),'reviews':[review]},
            'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]}}

class Repository:
    def status(self):
        return {'can_restore':True,'channel_mappings':[],'user_channel_refs':[],'folder_ids':[],'pending':[],'reconnect':{'snapshot':'reconnect-v1:'+'a'*64,'counts':{},'excluded_hidden':0}}

class PreviewParityTests(unittest.TestCase):
    def test_decision_revision_must_be_an_integer(self):
        self.assertEqual(normalize_backup(document())['user_work']['reviews'][0]['decision_revision'],2)
        for bad in (2.0,'2',True):
            with self.assertRaises(UserWorkError,msg=bad):
                normalize_backup(document(bad))

    def test_preview_returns_the_file_without_fingerprints(self):
        collection={'id':'b0000000-0000-4000-8000-000000000001','name':'Saved','revision':1,
                    'created_at':'2026-09-01T00:00:00+00:00','updated_at':'2026-09-01T00:00:00+00:00','members':[]}
        preview=UserWorkService(Repository()).preview(document(collections=[collection]))
        self.assertNotIn('create_fingerprint',preview['backup']['user_work']['collections'][0])
        self.assertIn('create_fingerprint',import_payload(document(collections=[collection]))['collections'][0])

    def test_size_is_measured_as_the_database_prints_it(self):
        members=[{'video_id':f'v{i:010d}','added_at':'2026-09-01T00:00:00+00:00'} for i in range(200)]
        collection={'id':'b0000000-0000-4000-8000-000000000001','name':'Saved','revision':1,
                    'created_at':'2026-09-01T00:00:00+00:00','updated_at':'2026-09-01T00:00:00+00:00','members':members}
        doc=normalize_backup(document(collections=[collection]))
        compact=len(encode_backup(doc))
        printed=len(json.dumps(import_payload(doc,normalized=True),ensure_ascii=False,separators=(', ',': ')).encode())
        self.assertLess(compact,printed)
        # A file that fits compactly but not as printed is refused by preview and restore alike.
        with patch.object(user_work_backup,'MAX_BYTES',printed-1):
            normalize_backup(document(collections=[collection]))
            with self.assertRaises(UserWorkError) as raised:
                UserWorkService(Repository()).preview(doc,normalized=True)
            self.assertEqual((raised.exception.code,raised.exception.status),('user_work_limit_exceeded',413))

if __name__=='__main__':unittest.main()
