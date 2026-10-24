"""HTTP projections keep real independent source witnesses and local identity."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hot_storage import HOT_DATA_WINDOW, capture_read_lifetime, register_read_deadline, iso_utc
from web.home import create_home_router
from saved_collections import SavedCollectionsService
from channel_explorer import PUBLIC_VIDEO_FIELDS
from test_collection_member_identities import repository, ID

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class PublicItemExpiryTests(unittest.TestCase):
    def test_recent_items_use_their_own_video_and_membership_clocks(self):
        repo = Mock()
        old = NOW - HOT_DATA_WINDOW + timedelta(seconds=30)
        fresh = NOW - timedelta(hours=1)
        repo.load_recent_long_inventory_page.return_value = ([
            dict(video_id='b' * 11, channel_id='channel', published_at=iso_utc(NOW - timedelta(minutes=1)),
                 source_checked_at=iso_utc(old)),
            dict(video_id='a' * 11, channel_id='channel', published_at=iso_utc(NOW - timedelta(minutes=2)),
                 source_checked_at=iso_utc(fresh)),
        ], 2)
        repo.load_videos_by_ids.return_value = [dict(video_id=video_id, title=video_id,
            checked_at=iso_utc(fresh), metadata_checked_at=iso_utc(fresh), availability_checked_at=iso_utc(fresh))
            for video_id in ('b' * 11, 'a' * 11)]
        cache = SimpleNamespace(cached_read=lambda key, seconds, loader: loader())
        library = SimpleNamespace(load_visible_channel_ids=lambda: {'channel'})
        core = SimpleNamespace(parse_dt=lambda value: datetime.fromisoformat(value))
        app = FastAPI()
        app.include_router(create_home_router(cache=cache, library=library, core=core, database=object()))
        with patch('web.home.FrontendRepository', return_value=repo):
            response = TestClient(app).get('/explorer/recent-videos?window=28d')
        self.assertEqual(response.status_code, 200)
        items = response.json()['items']
        self.assertEqual([item['video_id'] for item in items], ['b' * 11, 'a' * 11])
        self.assertEqual(items[0]['source_expires_at'], iso_utc(old + HOT_DATA_WINDOW))
        self.assertEqual(items[1]['source_expires_at'], iso_utc(fresh + HOT_DATA_WINDOW))
        self.assertGreater(items[1]['source_expires_at'], items[0]['source_expires_at'])

    def saved_source(self, video_id):
        clock = NOW - timedelta(hours=1)
        raw = {field: None for field in PUBLIC_VIDEO_FIELDS}
        raw.update(video_id=video_id, title='Current title', views=5, category_id='28',
                   checked_at=iso_utc(clock), metadata_checked_at=iso_utc(clock),
                   availability_checked_at=iso_utc(clock), membership_checked_at=iso_utc(clock),
                   source_expires_at=iso_utc(clock + HOT_DATA_WINDOW))
        return dict(video_id=video_id, source_status='available', source_reason=None,
                    channel_id='UC' + 'a' * 22, video_row=raw)

    def test_saved_video_category_witness_does_not_shorten_other_saved_members(self):
        repo, _, _ = repository()
        sources = [self.saved_source(letter * 11) for letter in ('b', 'a')]
        sources[1]['video_row']['category_id'] = None
        repo.load_sources.side_effect = None
        repo.load_sources.return_value = sources
        deadline = NOW + timedelta(seconds=10)
        def category(_):
            register_read_deadline(deadline)
            return 'Science & Technology'
        with patch('saved_collections.youtube_video_category_name', side_effect=category), capture_read_lifetime():
            result = SavedCollectionsService(repo, now=lambda: NOW).members(ID)
        first, second = result['members']
        self.assertEqual(first['source_expires_at'], iso_utc(deadline))
        self.assertEqual(first['video']['source_expires_at'], first['source_expires_at'])
        self.assertGreater(second['source_expires_at'], first['source_expires_at'])
        self.assertEqual([item['video_id'] for item in result['members']], ['b' * 11, 'a' * 11])

    def test_missing_category_has_no_manufactured_or_extended_lifetime(self):
        repo, _, _ = repository()
        sources = [self.saved_source(letter * 11) for letter in ('b', 'a')]
        repo.load_sources.side_effect = None
        repo.load_sources.return_value = sources
        with patch('saved_collections.youtube_video_category_name', return_value=None), capture_read_lifetime():
            result = SavedCollectionsService(repo, now=lambda: NOW).members(ID)
        for member, source in zip(result['members'], sources):
            self.assertEqual(member['source_expires_at'], source['video_row']['source_expires_at'])
            self.assertIsNone(member['video']['category_name'])


if __name__ == '__main__':
    unittest.main()
