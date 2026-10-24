"""An exhausted daily quota defers work to the Pacific-midnight reset without counting a failure. Source only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_error_codes_not_text import sync_functions

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from collection import initial as initial_collection
from collection.settings import CollectionSettings
SCHEMA=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')

class ResetHelperTests(unittest.TestCase):
    def test_python_helper_matches_across_daylight_saving(self):
        reset=sync_functions()['youtube_quota_reset_at']
        for now,expected in (('2026-09-25T06:50:00+00:00','2026-09-25T07:00:00+00:00'),
                             ('2026-09-25T07:10:00+00:00','2026-09-26T07:00:00+00:00'),
                             ('2026-11-01T12:00:00+00:00','2026-11-02T08:00:00+00:00'),
                             ('2026-03-08T12:00:00+00:00','2026-03-09T07:00:00+00:00')):
            self.assertEqual(reset(datetime.fromisoformat(now)),datetime.fromisoformat(expected))

class OperatorJobDeferralTests(unittest.TestCase):
    def test_current_schema_keeps_the_job_retry_clock(self):
        self.assertIn('    retry_not_before timestamp with time zone,',SCHEMA)

    def test_worker_reports_quota_codes(self):
        source=(ROOT/'application/operator_jobs.py').read_text(encoding='utf-8')
        self.assertIn('else "initial_inventory_quota_exhausted"\n                if _is_daily_quota_error(error)',source)
        self.assertIn('"quota_deferred" if stop_reason and stop_reason.endswith("quota_exhausted")',source)
        for stop in ('quota_exhausted','general_quota_exhausted','batch_quota_exhausted'):
            self.assertRegex('video_collection_'+stop,'quota_exhausted$')

class NewChannelTypedInventoryTests(unittest.TestCase):
    """Shorts/Streams of a new channel: an exhausted quota defers the job, not an ordinary error."""

    def chunk(self, summary):
        class YouTubeDailyQuotaExceededError(RuntimeError):pass
        def run(channel_id, **options):
            with patch.object(initial_collection, 'run_typed_inventory_batch', return_value=summary), \
                 patch.object(initial_collection, 'TypedInventoryRepository', return_value=object()):
                return initial_collection.collect_initial_inventory_chunk(
                    channel_id, **options, database=object(), expected_epoch=7, worker_id="test-worker",
                    settings=CollectionSettings(True, True, True),
                    fetch_page=lambda *args: {},
                    get_longform_playlist_id=lambda value: 'UULF'+value[2:],
                    parse_dt=lambda value: datetime.fromisoformat(value) if value else None,
                    playlist_not_found_error=LookupError,
                    daily_quota_exceeded_error=YouTubeDailyQuotaExceededError,
                )
        return run

    def test_quota_raises_the_quota_error_the_worker_recognizes(self):
        chunk=self.chunk({'quota_exhausted':True,'errors':1})
        with self.assertRaises(RuntimeError) as caught:
            chunk('UC'+'a'*22,video_type='short')
        self.assertEqual(type(caught.exception).__name__,'YouTubeDailyQuotaExceededError')
        import ast
        source=(ROOT/'application/operator_jobs.py').read_text(encoding='utf-8')
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_is_daily_quota_error')
        ns={}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'operator_jobs.py','exec'),ns)
        self.assertTrue(ns['_is_daily_quota_error'](caught.exception))

    def test_other_errors_stay_ordinary(self):
        with self.assertRaises(RuntimeError) as caught:
            self.chunk({'errors':1})('UC'+'a'*22,video_type='stream')
        self.assertEqual(type(caught.exception).__name__,'RuntimeError')

class PythonDeferralTests(unittest.TestCase):

    def test_metadata_quota_keeps_the_error_count(self):
        now=datetime(2026,9,25,6,50,tzinfo=timezone.utc)
        for error in ('metadata_recheck_quota_deferred',RuntimeError('reason=quotaExceeded')):
            updates=[]
            sync_functions(updates)['mark_channel_metadata_error']({'channel_id':'UC1','collection_control_epoch':7,'channel_metadata_error_count':3},error,now)
            self.assertEqual(updates[0]['channel_metadata_error_count'],3)
            self.assertEqual(updates[0]['channel_metadata_next_after'],(now+timedelta(hours=1)).isoformat())

    def test_metadata_failure_still_backs_off(self):
        now=datetime(2026,9,25,6,50,tzinfo=timezone.utc)
        updates=[]
        sync_functions(updates)['mark_channel_metadata_error']({'channel_id':'UC1','collection_control_epoch':7,'channel_metadata_error_count':3},'channel_metadata_failed',now)
        self.assertEqual(updates[0]['channel_metadata_error_count'],4)
        self.assertEqual(updates[0]['channel_metadata_next_after'],(now+timedelta(hours=16)).isoformat())

class ProfileDeferralStatusTests(unittest.TestCase):
    # RR-25: waiting for YouTube must not hide the last confirmed profile.
    NOW=datetime(2026,10,12,10,19,tzinfo=timezone.utc)

    def mark(self,error,status):
        updates=[]
        sync_functions(updates)['mark_channel_metadata_error'](
            {'channel_id':'UC1','collection_control_epoch':7,'channel_metadata_error_count':2,
             'channel_metadata_status':status},error,self.NOW)
        return updates[0]

    def test_quota_and_rate_limit_keep_a_confirmed_profile_ok(self):
        for error in ('metadata_recheck_quota_deferred','metadata_recheck_quota_exhausted',
                      RuntimeError('reason=quotaExceeded'),'provider_rate_limited'):
            with self.subTest(error=error):
                values=self.mark(error,'ok')
                self.assertEqual(values['channel_metadata_status'],'ok')
                self.assertEqual(values['channel_metadata_error_count'],2)
                self.assertIsNone(values['channel_metadata_started_at'])
                self.assertNotIn('channel_metadata_checked_at',values)

    def test_real_failure_still_marks_error(self):
        values=self.mark('channel_metadata_failed','ok')
        self.assertEqual(values['channel_metadata_status'],'error')
        self.assertEqual(values['channel_metadata_error_count'],3)

    def test_deferral_does_not_upgrade_an_unconfirmed_profile(self):
        for status in ('error',None,'channel_not_found'):
            with self.subTest(status=status):
                self.assertEqual(self.mark('metadata_recheck_quota_deferred',status)['channel_metadata_status'],'error')

if __name__=='__main__':unittest.main()
