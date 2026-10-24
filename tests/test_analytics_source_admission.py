"""Analytical caches expire with every used source, including earlier witnesses."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.reads import analytics_read_queries as queries
from db.reads.channel_timeline import ChannelTimelineRepository
from hot_storage import HOT_DATA_WINDOW, HotSourceReadExpired, capture_read_lifetime
from video_history import build_video_history
from video_launch import build_video_launch

NOW=datetime(2026,10,9,12,tzinfo=timezone.utc)

class Rows:
    def __init__(self,rows):self.rows=rows
    def read_rows(self,*args,**kwargs):return [dict(row) for row in self.rows]
    def load_snapshots_by_video_ids(self,*args,**kwargs):return [dict(row) for row in self.rows]

class AnalyticalAdmissionTests(unittest.TestCase):
    def test_title_needs_its_own_availability_clock(self):
        for clock,expected in ((None,{}),((NOW-HOT_DATA_WINDOW-timedelta(seconds=1)).isoformat(),{}),
                               ((NOW+timedelta(seconds=1)).isoformat(),{}),(NOW.isoformat(),{'v':{'title':'Title','source_expires_at':None}})):
            row=dict(video_id='v',title='Title',metadata_checked_at=NOW.isoformat(),
                     availability_status='public',availability_checked_at=clock)
            self.assertEqual(ChannelTimelineRepository(Rows([row]),read_clock=lambda:NOW).load_video_titles(['v']),expected)

    def test_aggregate_registers_earlier_scan_witness_and_keeps_independent_item_clock(self):
        row=dict(bucket_at=NOW.isoformat(),recorded_at=NOW.isoformat(),views=2,subscribers=1,
                 correction_within_bucket=True,scan_truncated=False,snapshot_count=3,
                 source_expires_at=(NOW+timedelta(seconds=20)).isoformat(),
                 response_source_expires_at=(NOW+timedelta(seconds=5)).isoformat())
        with patch.object(queries,'utc_now',return_value=NOW),capture_read_lifetime() as lifetime:
            result=queries.read_channel_timeline_series(Rows([row]),channel_id='c',metric='views',
                recorded_from=None,recorded_through=NOW,bucket_seconds=300)
        self.assertEqual(lifetime.expires_at,NOW+timedelta(seconds=5))
        self.assertEqual(result[0]['source_expires_at'],(NOW+timedelta(seconds=20)).isoformat().replace('+00:00','Z'))
        self.assertNotIn('response_source_expires_at',result[0])
        self.assertTrue(result[0]['correction_within_bucket'])
        with patch.object(queries,'utc_now',return_value=NOW+timedelta(seconds=5)):
            with self.assertRaises(HotSourceReadExpired):
                queries.read_channel_timeline_series(Rows([row]),channel_id='c',metric='views',
                    recorded_from=None,recorded_through=NOW,bucket_seconds=300)
        row.pop('source_expires_at')
        with self.assertRaises(queries.AnalyticsReadResultError):
            queries.read_channel_timeline_series(Rows([row]),channel_id='c',metric='views',
                recorded_from=None,recorded_through=NOW,bucket_seconds=300)

    def context(self):
        return dict(channel_id='c',video_id='v',browse_session='session',browse_created_at=NOW.isoformat(),
            expires_at=(NOW+timedelta(minutes=1)).isoformat(),
            video=dict(video_id='v',checked_at=NOW.isoformat(),views='20',like_count='2',comment_count='1'))

    def test_history_points_set_response_lifetime(self):
        early=NOW-timedelta(days=27)
        rows=[dict(id=1,video_id='v',recorded_at=early.isoformat(),views=10,like_count=1,comment_count=1),
              dict(id=2,video_id='v',recorded_at=NOW.isoformat(),views=20,like_count=2,comment_count=1)]
        with capture_read_lifetime() as lifetime:
            result=build_video_history(Rows(rows),self.context(),now=NOW)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(lifetime.expires_at,early+HOT_DATA_WINDOW)
        self.assertEqual(result['window']['requested_days'],28)

    def test_launch_points_set_response_lifetime(self):
        context=self.context()
        video=dict(context['video'],published_at=(NOW-timedelta(days=3)).isoformat())
        context['catalogue']=dict(video_type='long',channel=dict(channel_id='c'),videos=[video],
            coverage=dict(scan_mode='full_catalogue',truncated=False,inventory_complete=True,scanned_count=1),
            pagination=dict(offset=0,returned_count=1,matched_count=1,has_more_in_scan=False))
        observed=NOW-timedelta(days=2)
        rows=[dict(id=1,video_id='v',recorded_at=observed.isoformat(),views=10)]
        with capture_read_lifetime() as lifetime:
            result=build_video_launch(Rows(rows),context,now=NOW)
        self.assertEqual(result['window']['days'],28)
        self.assertEqual(lifetime.expires_at,observed+HOT_DATA_WINDOW)

if __name__=='__main__':unittest.main()
