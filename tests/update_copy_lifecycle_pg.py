"""Native copy admission for absent memberships and retained first-page failures.

Only a new disposable PG17 cluster. No production data, provider or secret files.
All mutation cases roll back; constraints/triggers stay enabled.
"""
from datetime import timedelta
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from research_query_pg import bootstrap
from db.hot.update_copy_sources import UPDATE_COPY_WINDOW_SQL, _SCAN_CLOCK
from update_copy_protocol import marker_from_window, UpdateCopyError


class CopyLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
            raise RuntimeError('disposable_only')
        cls.admin=psycopg.connect(host='127.0.0.1',port=int(os.environ['LIC_QUERY_TEST_PORT']),
            dbname='leti-cook',user='postgres',password=os.environ['LIC_QUERY_TEST_PASSWORD'],autocommit=True)
        bootstrap(cls.admin)
        cls.admin.execute("SET TIME ZONE 'UTC'")

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def setUp(self):
        transaction=self.admin.transaction(force_rollback=True)
        transaction.__enter__()
        self.addCleanup(transaction.__exit__,None,None,None)
        self.now=self.admin.execute('SELECT clock_timestamp()').fetchone()[0]
        self.channel='UC'+'a'*22
        self.admin.execute('INSERT INTO public.channels(channel_id,user_work_id) VALUES(%s,%s)',
                           (self.channel,uuid.uuid4()))
        self.assert_admitted()

    def facts(self):
        with self.admin.cursor(row_factory=dict_row) as cursor:
            cursor.execute(UPDATE_COPY_WINDOW_SQL)
            return cursor.fetchone()

    def assert_admitted(self):
        value=self.facts()
        self.assertFalse(value['invalid_source_clock'],value)
        self.assertFalse(value['expired'],value)
        return marker_from_window(value,created_at=int(time.time()))

    def assert_blocked(self):
        value=self.facts()
        self.assertTrue(value['invalid_source_clock'],value)
        with self.assertRaisesRegex(UpdateCopyError,'unconfirmed'):
            marker_from_window(value,created_at=int(time.time()))

    def completed_scan(self,kind='long'):
        scan=uuid.uuid4()
        playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+self.channel[2:]
        self.admin.execute('''INSERT INTO public.channel_video_inventory_scans
          (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,
           started_at,scan_boundary_at,source_checked_at,completed_at,pages_completed,items_seen)
          VALUES(%s,%s,%s,%s,'full_reconciliation','completed','complete',%s,%s,%s,%s,1,0)''',
          (scan,self.channel,kind,playlist,self.now-timedelta(hours=2),self.now-timedelta(hours=2),
           self.now-timedelta(hours=1),self.now-timedelta(hours=1)))
        return scan,playlist

    def absent(self,kind='long'):
        scan,playlist=self.completed_scan(kind)
        video={'long':'a','short':'b','stream':'c'}[kind]+'0'*10
        self.admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)',
                           (video,self.channel,kind))
        observed=self.now-timedelta(hours=3)
        self.admin.execute('''INSERT INTO public.video_inventory_memberships
          (video_id,channel_id,video_type,playlist_id,first_seen_at,last_seen_at,last_present_at,
           membership_status,absent_since,absence_confirmed_by_scan_id,evidence_source)
          VALUES(%s,%s,%s,%s,%s,%s,%s,'absent',%s,%s,'legacy_inventory_backfill')''',
          (video,self.channel,kind,playlist,observed,observed,observed,self.now-timedelta(hours=1),scan))
        return scan,video

    def failure(self,kind='long',outcome='playlist_not_found'):
        scan,transition=uuid.uuid4(),uuid.uuid4()
        observed=self.now-timedelta(minutes=1)
        error='typed_playlist_not_found' if outcome=='playlist_not_found' else 'youtube_transport_error'
        playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+self.channel[2:]
        self.admin.execute('''INSERT INTO public.channel_video_inventory_scans
          (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,started_at,scan_boundary_at,
           token_history_verified,last_transition_id,last_transition_kind,error_code,error_at)
          VALUES(%s,%s,%s,%s,'full_reconciliation','running','partial',%s,%s,true,%s,'failure',%s,%s)''',
          (scan,self.channel,kind,playlist,self.now-timedelta(minutes=2),self.now-timedelta(minutes=2),transition,error,observed))
        result={'scan_id':str(scan),'channel_id':self.channel,'video_type':kind,'scan_kind':'full_reconciliation',
                'outcome':outcome,'error_code':error,'scan_retained':True,'resume_page_token':None,
                'page_items':0,'canonical_video_ids':[]}
        self.admin.execute('''INSERT INTO public.typed_inventory_receipts
          (receipt_scope,transition_id,scan_id,transition_kind,result,source_checked_at)
          VALUES('scan_transition',%s,%s,'failure',%s,%s)''',(transition,scan,Jsonb(result),observed))
        return scan,transition,result

    def test_all_formats_with_full_scan_absence_and_retained_attempt_can_be_copied(self):
        for kind in ('long','short','stream'):
            self.absent(kind)
            self.assert_admitted()
        self.failure()
        self.assert_admitted()

    def test_absence_does_not_change_canonical_type_or_current_membership(self):
        _,video=self.absent()
        self.assert_admitted()
        self.assertEqual(self.admin.execute('SELECT video_type FROM public.video_inventory WHERE video_id=%s',(video,)).fetchone()[0],'long')
        self.assertEqual(self.admin.execute('SELECT membership_status FROM public.video_inventory_memberships WHERE video_id=%s',(video,)).fetchone()[0],'absent')

    def test_absence_requires_complete_full_scan_matching_time_and_prior_membership(self):
        scan,video=self.absent()
        cases=[("UPDATE public.channel_video_inventory_scans SET coverage='partial' WHERE scan_id=%s",(scan,)),
               ("UPDATE public.channel_video_inventory_scans SET scan_kind='discovery' WHERE scan_id=%s",(scan,)),
               ("UPDATE public.channel_video_inventory_scans SET source_checked_at=source_checked_at-interval '1 second' WHERE scan_id=%s",(scan,)),
               ("UPDATE public.video_inventory_memberships SET last_seen_scan_id=%s WHERE video_id=%s",(scan,video)),
               ("UPDATE public.video_inventory_memberships SET last_present_at=absent_since WHERE video_id=%s",(video,)),
               ("UPDATE public.video_inventory_memberships SET last_seen_at='infinity' WHERE video_id=%s",(video,))]
        for sql,params in cases:
            with self.subTest(sql=sql),self.admin.transaction(force_rollback=True):
                self.admin.execute(sql,params)
                self.assert_blocked()
        self.assert_admitted()

    def test_missing_historical_evidence_still_blocks(self):
        self.admin.execute('INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,%s)',
                           ('x0000000000',self.channel,'long'))
        self.assert_blocked()

    def test_completed_scan_of_another_format_or_channel_is_not_absence_proof(self):
        scan,video=self.absent()
        other='UC'+'b'*22
        self.admin.execute('INSERT INTO public.channels(channel_id,user_work_id) VALUES(%s,%s)',(other,uuid.uuid4()))
        cases=[("UPDATE public.channel_video_inventory_scans SET channel_id=%s WHERE scan_id=%s",(other,scan)),
               ("UPDATE public.channel_video_inventory_scans SET video_type='short',playlist_id=%s WHERE scan_id=%s",('UUSH'+self.channel[2:],scan))]
        for sql,params in cases:
            with self.subTest(sql=sql),self.admin.transaction(force_rollback=True):
                self.admin.execute(sql,params)
                self.assert_blocked()

    def test_valid_absence_does_not_hide_expired_historical_evidence(self):
        _,video=self.absent()
        self.admin.execute("UPDATE public.video_inventory_memberships SET first_seen_at=now()-interval '30 days' WHERE video_id=%s",(video,))
        value=self.facts()
        self.assertTrue(value['expired'],value)
        self.assertFalse(value['invalid_source_clock'],value)
        with self.assertRaisesRegex(UpdateCopyError,'expired'):
            marker_from_window(value,created_at=int(time.time()))

    def test_retained_profile_exception_never_masks_bad_absence_or_failure(self):
        self.absent()
        scan,transition,_=self.failure()
        self.admin.execute("UPDATE public.channels SET title='retained synthetic',channel_metadata_status='channel_not_found',channel_metadata_checked_at=now()-interval '1 hour' WHERE channel_id=%s",(self.channel,))
        value=self.facts()
        self.assertTrue(value['retained_profile_only'],value)
        # Standard installations still refuse this profile, even though both
        # lifecycle cases are valid. The keep policy remains separate.
        with self.assertRaisesRegex(UpdateCopyError,'unconfirmed'):
            marker_from_window(value,created_at=int(time.time()))
        self.admin.execute('DELETE FROM public.typed_inventory_receipts WHERE transition_id=%s',(transition,))
        self.assertFalse(self.facts()['retained_profile_only'])
        self.assert_blocked()

    def test_first_failure_uses_observation_time_and_never_fills_database_clock(self):
        for outcome in ('playlist_not_found','error'):
            with self.subTest(outcome=outcome),self.admin.transaction(force_rollback=True):
                scan,_,_=self.failure(outcome=outcome)
                self.assert_admitted()
                actual=self.admin.execute('SELECT '+_SCAN_CLOCK+' FROM public.channel_video_inventory_scans source WHERE scan_id=%s',(scan,)).fetchone()[0]
                self.assertEqual(actual,self.now-timedelta(minutes=1))
                self.assertIsNone(self.admin.execute('SELECT source_checked_at FROM public.channel_video_inventory_scans WHERE scan_id=%s',(scan,)).fetchone()[0])

    def test_mismatched_or_missing_failure_receipt_still_blocks(self):
        scan,transition,result=self.failure()
        for key,value in [('scan_id',str(uuid.uuid4())),('channel_id','UC'+'b'*22),('video_type','short'),
                          ('scan_retained',False),('page_items',1),('canonical_video_ids',['x0000000000']),
                          ('resume_page_token','next'),('outcome','complete'),('error_code','other')]:
            with self.subTest(key=key),self.admin.transaction(force_rollback=True):
                self.admin.execute('UPDATE public.typed_inventory_receipts SET result=%s WHERE transition_id=%s',
                                   (Jsonb({**result,key:value}),transition))
                self.assert_blocked()
        with self.admin.transaction(force_rollback=True):
            self.admin.execute('DELETE FROM public.typed_inventory_receipts WHERE transition_id=%s',(transition,))
            self.assert_blocked()
        self.assert_admitted()

    def test_first_failure_exception_cannot_mask_missing_successful_page_clock(self):
        scan,_,_=self.failure()
        self.admin.execute('UPDATE public.channel_video_inventory_scans SET pages_completed=1 WHERE scan_id=%s',(scan,))
        self.assert_blocked()

    def test_failure_cannot_replace_an_existing_observation_or_extend_expiry(self):
        scan,_,_=self.failure()
        old=self.now-timedelta(days=30)
        self.admin.execute('UPDATE public.channel_video_inventory_scans SET source_checked_at=%s WHERE scan_id=%s',(old,scan))
        facts=self.facts()
        self.assertTrue(facts['expired'],facts)
        self.assertFalse(facts['invalid_source_clock'],facts)
        with self.assertRaisesRegex(UpdateCopyError,'expired'):
            marker_from_window(facts,created_at=int(time.time()))

    def test_future_negative_observation_still_blocks(self):
        scan,transition,_=self.failure()
        future=self.now+timedelta(days=1)
        self.admin.execute('UPDATE public.channel_video_inventory_scans SET error_at=%s WHERE scan_id=%s',(future,scan))
        self.admin.execute('UPDATE public.typed_inventory_receipts SET source_checked_at=%s WHERE transition_id=%s',(future,transition))
        self.assert_blocked()


if __name__=='__main__':
    unittest.main(verbosity=2)
