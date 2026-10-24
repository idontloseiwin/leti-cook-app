"""Restored label names follow the label-creation rule (audit S3-6). No database."""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from user_work_backup import UserWorkError, normalize_backup  # noqa: E402

def document(*names):
    labels=[{'label_key':f'custom:0000000{i}-0000-4000-8000-000000000000','name':name,'label_type':'custom',
             'created_at':'2026-09-01T00:00:00+00:00'} for i,name in enumerate(names)]
    return {'format':'let-it-cook-user-work','version':1,'created_at':'2026-09-20T00:00:00+00:00',
            'user_work':{'folders':[],'labels':labels,'channels':[],'collections':[],'reviews':[]},
            'browser_preferences':{'catalogue_density':'large','bookmarked_channel_refs':[],'collapsed_folder_ids':[]}}

class BackupLabelNameTests(unittest.TestCase):
    def test_space_is_trimmed_and_collapsed(self):
        labels=normalize_backup(document('  Music   Videos ','News'))['user_work']['labels']
        self.assertEqual([label['name'] for label in labels if label['label_type']=='custom'],['Music Videos','News'])
        # A format 1 file implies the preset label, unchanged.
        self.assertEqual([label['name'] for label in labels if label['label_type']=='preset'],['Monetized'])

    def test_names_that_differ_only_in_space_or_case_are_duplicates(self):
        for names in (('Music','Music '),('Music','MUSIC'),('Straße','STRASSE'),('Monetized',)):
            with self.assertRaises(UserWorkError,msg=names):
                normalize_backup(document(*names))

if __name__=='__main__':unittest.main()
