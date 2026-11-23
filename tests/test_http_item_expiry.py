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
from saved_collections import SavedCollectionsService
from saved_collections import PUBLIC_VIDEO_FIELDS
from test_collection_member_identities import repository, ID

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


class PublicItemExpiryTests(unittest.TestCase):
    def saved_source(self, video_id):
        clock = NOW - timedelta(hours=1)
        raw = {field: None for field in PUBLIC_VIDEO_FIELDS}
        raw.update(video_id=video_id, video_type='short', title='Current title', views=5, category_id='28',
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
