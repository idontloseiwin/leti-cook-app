"""Saved collection members of every type, one reason each; no database or YouTube.

The shared collection scope (db/reads/research_sql.py) decides availability and
the reason. The route keeps its contract, adds each shown member's video type
and never publishes a value past its own earliest source clock.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]

from saved_collections import PUBLIC_VIDEO_FIELDS
from hot_storage import HOT_DATA_WINDOW, iso_utc, utc_instant
from saved_collections import SOURCE_UNAVAILABLE_REASONS, SavedCollectionsService
from saved_work import SavedWorkError

ID = 'c0000000-0000-4000-8000-000000000001'
AT = '2026-10-20T10:00:00+00:00'
NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
CHANNEL = 'UC' + 'a' * 22


def available(video_id, video_type, *, deadline=None, clock=NOW - timedelta(hours=1)):
    raw = {field: None for field in PUBLIC_VIDEO_FIELDS}
    raw.update(video_id=video_id, video_type=video_type, title='Title ' + video_type, views=7, duration='PT1M',
               checked_at=iso_utc(clock), metadata_checked_at=iso_utc(clock), availability_checked_at=iso_utc(clock),
               membership_checked_at=iso_utc(clock), source_expires_at=iso_utc(deadline or clock + HOT_DATA_WINDOW))
    return {'video_id': video_id, 'channel_id': CHANNEL, 'source_status': 'available', 'source_reason': None, 'video_row': raw}


def unavailable(video_id, reason):
    return {'video_id': video_id, 'channel_id': None, 'source_status': 'unavailable', 'source_reason': reason, 'video_row': None}


def service(sources):
    members = [{'video_id': source['video_id'], 'added_at': AT} for source in sources]
    row = {'id': ID, 'kind': 'collection', 'name': 'Every type', 'revision': 3, 'member_count': len(members),
           'created_at': AT, 'updated_at': AT, 'deleted_at': None}
    repo = Mock()
    repo.get_object.side_effect = lambda identity: dict(row)
    repo.list_members.side_effect = lambda identity, limit, after: members[:limit]
    repo.load_sources.side_effect = lambda collection_id, ids, as_of: [dict(s) for s in sources]
    return SavedCollectionsService(repo, now=lambda: NOW), repo


class MemberTypeTests(unittest.TestCase):
    def read(self, sources):
        result, repo = service(sources)
        with patch('saved_collections.youtube_video_category_name', return_value=None):
            return result.members(ID), repo

    def test_members_of_all_three_types_are_shown_with_their_type(self):
        sources = [available('l' * 11, 'long'), available('s' * 11, 'short'), available('t' * 11, 'stream')]
        page, repo = self.read(sources)
        self.assertEqual(repo.load_sources.call_args.args[:2], (ID, ['l' * 11, 's' * 11, 't' * 11]))
        self.assertEqual([(m['status'], m['video']['video_type']) for m in page['members']],
                         [('available', 'long'), ('available', 'short'), ('available', 'stream')])
        self.assertTrue(all(m['channel_id'] == CHANNEL and m['reason'] is None for m in page['members']))

    def test_every_shared_reason_is_exposed_per_member(self):
        from db.reads.research_sql import COLLECTION_MEMBER_REASONS
        self.assertEqual(SOURCE_UNAVAILABLE_REASONS, set(COLLECTION_MEMBER_REASONS))
        self.assertNotIn('type_unsupported', SOURCE_UNAVAILABLE_REASONS)
        sources = [unavailable(chr(97 + index) * 11, reason) for index, reason in enumerate(sorted(SOURCE_UNAVAILABLE_REASONS))]
        page, _ = self.read(sources)
        self.assertEqual([m['reason'] for m in page['members']], sorted(SOURCE_UNAVAILABLE_REASONS))
        self.assertTrue(all(m['video'] is None and m['channel_id'] is None and m['source_expires_at'] is None for m in page['members']))

    def test_unknown_retired_or_malformed_answers_fail_closed(self):
        for sources in ([unavailable('a' * 11, 'type_unsupported')], [unavailable('a' * 11, None)],
                        [{**available('a' * 11, 'long'), 'video_row': {**available('a' * 11, 'long')['video_row'], 'video_type': 'live'}}],
                        [{**available('a' * 11, 'long'), 'video_row': {k: v for k, v in available('a' * 11, 'long')['video_row'].items() if k != 'video_type'}}]):
            with self.subTest(sources=sources), self.assertRaises(SavedWorkError) as error:
                self.read(sources)
            self.assertEqual(error.exception.status_code, 503)

    def test_a_member_removed_meanwhile_is_a_collection_change(self):
        with self.assertRaises(SavedWorkError) as error:
            self.read([available('a' * 11, 'long'), unavailable('b' * 11, 'not_saved')])
        self.assertEqual((error.exception.status_code, error.exception.code), (409, 'collection_changed'))

    def test_recent_identity_evidence_does_not_hide_a_new_video(self):
        # A video first seen 10 days ago keeps membership evidence
        # until first sighting + 29 days, earlier than its other clocks allow.
        # Before: the route required deadline == earliest clock + 29 days, so every
        # recently discovered video showed "needs to be refreshed". After: it is
        # shown until that earlier deadline, never past its earliest clock.
        first_seen = NOW - timedelta(days=10)
        page, _ = self.read([available('n' * 11, 'short', deadline=first_seen + HOT_DATA_WINDOW)])
        [member] = page['members']
        self.assertEqual((member['status'], member['source_expires_at']), ('available', iso_utc(first_seen + HOT_DATA_WINDOW)))
        for deadline in (NOW - timedelta(seconds=1), NOW - timedelta(hours=1) + HOT_DATA_WINDOW + timedelta(seconds=1)):
            with self.subTest(deadline=deadline):
                page, _ = self.read([available('n' * 11, 'short', deadline=deadline)])
                self.assertEqual((page['members'][0]['status'], page['members'][0]['reason']), ('unavailable', 'source_stale'))

    def test_dead_add_outcome_mapping_is_gone(self):
        # No membership write returns source_unavailable; adds of any type are accepted.
        result, _ = service([])
        with self.assertRaises(SavedWorkError) as error:
            result._outcome({'outcome': 'source_unavailable'})
        self.assertEqual(error.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
