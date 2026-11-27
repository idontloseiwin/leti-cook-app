"""A backup preview accepts exactly what a restore will accept. No database."""
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
    return {'format':'leti-cook-user-work','version':1,'created_at':'2026-09-20T00:00:00+00:00',
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

    def test_full_alert_and_envelope_budget_fits_with_15_mib_of_other_work(self):
        from alerts import EVENTS, COUNTERS
        from research_field_policy import MAX_BYTES as field_bytes
        from user_work_backup import normalize_alert_rules
        key = 'a0000000-0000-4000-8000-000000000001'
        scopes = [('channel','UC'+'a'*22,key),('video','v'*11,key),
                  ('folder',key,key),('label','custom:'+key,'custom:'+key)]
        printed = lambda value: len(json.dumps(value,ensure_ascii=False).encode('utf-8'))
        largest = 0
        for kind,identity,ref in scopes:
            for event in EVENTS:
                if kind == 'video' and event == 'channel.subscribers':
                    continue
                # A scalar in a valid name occupies at most four UTF-8 bytes.
                # Quotes and backslashes escape to two. C0 controls are refused.
                for char in (chr(0x10ffff),'"',chr(92)):
                    definition = dict(name=char*100,scope=dict(kind=kind,id=identity),event=event)
                    if event in COUNTERS:
                        definition.update(direction='down',threshold=str(2**63-1))
                    if event != 'channel.subscribers':
                        definition['video_type']='short'
                    if event not in ('channel.subscribers','video.restored','video.unavailable'):
                        definition.update(min_duration=2**53-1,max_duration=2**53-1)
                    rule = {'definition':definition,'enabled':False,'source_ref':ref}
                    [normalized] = normalize_alert_rules([rule],{key},{key},{'custom:'+key})
                    largest = max(largest,printed({'alert_rules':[normalized]*100}))
        self.assertLess(largest,80*1024)
        # Include both the downloadable envelope and the restore-only fingerprints
        # together. The real file carries only the former, the import only the latter.
        browser = printed(dict(browser_preferences=dict(catalogue_density='compact',
                          bookmarked_channel_refs=[key]*10000,collapsed_folder_ids=[key]*500)))
        receipts = 1000 * (printed({'create_fingerprint':'f'*64})+2)
        # JSONB prints separators with spaces; even a twofold allowance covers
        # all punctuation in the bounded compact field document.
        envelope = browser + receipts + 2*field_bytes + 4096
        self.assertLess(15*1024**2 + largest + envelope, user_work_backup.MAX_BYTES)

    def test_alert_validation_for_preview_and_restore(self):
        doc = normalize_backup(document())
        rule = dict(definition=dict(name='Missing',scope=dict(kind='label',id='monetized'),
                                    event='channel.subscribers',direction='up',threshold='100'),
                    enabled=False,source_ref=None)
        doc['user_work']['alert_rules']=[rule]
        service=UserWorkService(Repository())
        self.assertEqual(service.preview(doc)['counts']['alert_rules'],1)
        self.assertEqual(import_payload(doc)['alert_rules'],[rule])
        for update in ({'gap_count':7},{'enabled':1},{'source_ref':'absent'},
                       {'definition':{**rule['definition'],'current_value':'90'}}):
            doc['user_work']['alert_rules']=[{**rule,**update}]
            with self.assertRaises(UserWorkError):
                service.preview(doc)
        doc['user_work']['alert_rules']=[rule]*101
        with self.assertRaises(UserWorkError) as exc:
            service.preview(doc)
        self.assertEqual(exc.exception.status,413)

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
