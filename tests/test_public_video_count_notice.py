"""Passive count-change notice uses two protected stored observations only."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from channel_explorer import build_channel_profile
from db.reads.channel_explorer import ChannelExplorerRepository
from hot_storage import HOT_DATA_WINDOW

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def snapshot(count, hours=0):
    at = NOW - timedelta(hours=hours)
    return dict(video_count=count, recorded_at=at.isoformat(),
                source_expires_at=(at + HOT_DATA_WINDOW).isoformat())


class PublicVideoCountNoticeTests(unittest.TestCase):
    def profile(self, rows, *, count=99, metadata_at=None):
        repository = Mock()
        repository.load_channel.return_value = dict(channel_id='channel', title='Example',
            video_count=count, channel_metadata_status='ok',
            channel_metadata_checked_at=(metadata_at or NOW).isoformat())
        repository.load_recent_public_video_counts.return_value = rows
        return build_channel_profile(repository, 'channel', now=NOW), repository

    def test_decrease_is_informational_and_preserves_both_source_deadlines(self):
        result, repository = self.profile([snapshot(99), snapshot(100, 1)])
        notice = result['public_video_count_notice']
        self.assertEqual(notice['code'], 'public_video_count_decreased')
        self.assertEqual(notice['source_expires_at'],
                         (NOW - timedelta(hours=1) + HOT_DATA_WINDOW).isoformat().replace('+00:00', 'Z'))
        self.assertNotIn('video_ids', notice)
        repository.load_recent_public_video_counts.assert_called_once_with(
            'channel', recorded_through=NOW.isoformat())
        self.assertEqual({call[0] for call in repository.mock_calls},
                         {'load_channel', 'load_recent_public_video_counts'})

    def test_no_notice_for_increase_same_count_missing_or_unusable_evidence(self):
        for rows in ([], [snapshot(99)], [snapshot(99), snapshot(99, 1)],
                     [snapshot(99), snapshot(98, 1)], [snapshot(None), snapshot(100, 1)],
                     [snapshot(99), snapshot(True, 1)], [snapshot(99), snapshot(100, 30 * 24)],
                     [snapshot(99), snapshot(100, -1)], [snapshot(99, 1), snapshot(100, 2)]):
            with self.subTest(rows=rows):
                self.assertIsNone(self.profile(rows)[0]['public_video_count_notice'])

    def test_old_profile_never_borrows_a_new_snapshot(self):
        result, repository = self.profile([snapshot(99), snapshot(100, 1)],
            metadata_at=NOW - HOT_DATA_WINDOW - timedelta(seconds=1))
        self.assertIsNone(result['public_video_count_notice'])
        repository.load_recent_public_video_counts.assert_not_called()

    def test_repository_reads_at_most_two_protected_rows(self):
        database = Mock()
        database.read_rows.return_value = [snapshot(99), snapshot(100, 1)]
        repository = ChannelExplorerRepository(database, read_clock=lambda: NOW)
        result = repository.load_recent_public_video_counts('channel', recorded_through=NOW.isoformat())
        self.assertEqual(len(result), 2)
        call = database.read_rows.call_args
        self.assertIn('FROM readable.channel_snapshots', call.args[1])
        self.assertIn('ORDER BY recorded_at DESC LIMIT 2', call.args[1])
        self.assertEqual(call.args[2], ('channel', NOW.isoformat()))
        self.assertEqual(call.kwargs['max_rows'], 2)
        self.assertEqual({entry[0] for entry in database.mock_calls}, {'read_rows'})


if __name__ == '__main__':
    unittest.main()
