"""Settlements the database will always refuse do not stop collection (audit S7-8). No database or provider."""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.videos.video_collection import VideoCollectionClaim  # noqa: E402
from typed_playlist import VIDEO_ID_RE  # noqa: E402
from video_collection_service import VideoCollectionService, _common_empty_summary  # noqa: E402
from youtube_source_fields import youtube_counter_integer  # noqa: E402

class DatabaseError(Exception):
    def __init__(self, code):super().__init__(code); self.code=code

class Repository:
    def __init__(self, error):self.error,self.failed=error,[]
    def complete_missing(self, rows):raise self.error
    def fail_batch(self, rows):self.failed.extend(rows)

def service(repository):
    unused=lambda *a,**k:None
    return VideoCollectionService(repository,fetch_full=unused,fetch_counters=unused,
                                  build_full_payload=unused,build_counter_payload=unused)

CLAIMS=[VideoCollectionClaim(video_id=f'vid0000000{i}',channel_id='UC'+'c'*22,collection_mode='metadata',
                             lease_token=f'token{i}',lease_expires_at='2026-09-26T00:00:00Z',next_due_at='2026-09-26T00:00:00Z')
        for i in range(2)]

class SettlementRejectionTests(unittest.TestCase):
    def run_missing(self, error):
        repository=Repository(error)
        summary=_common_empty_summary('probe')
        service(repository)._complete_missing('long',CLAIMS,summary,attempt_id='a',recorded_at='2026-09-26T00:00:00Z')
        return summary,repository

    def test_a_rejected_batch_fails_its_claims_and_the_run_continues(self):
        for code in ('22003','23514','22P02'):
            summary,repository=self.run_missing(DatabaseError(code))
            self.assertFalse(summary['settlement_unknown'],code)
            self.assertFalse(summary['infrastructure_error'],code)
            self.assertEqual([row['error_type'] for row in repository.failed],['settlement_rejected']*2)
            self.assertIn(f'missing completion rejected: {code}',summary['error_messages'])

    def test_an_uncertain_outcome_still_stops_the_run(self):
        for error in (DatabaseError('PGRST000'),DatabaseError('40001'),ConnectionError('lost')):
            summary,repository=self.run_missing(error)
            self.assertTrue(summary['settlement_unknown'],error)
            self.assertEqual(repository.failed,[])

class InputBoundTests(unittest.TestCase):
    def test_counters_beyond_bigint_are_invalid(self):
        top=9_223_372_036_854_775_807
        for value in (top,str(top),float(2**52)):
            self.assertIsNotNone(youtube_counter_integer(value),value)
        for value in (top+1,str(top+1),float(2**64)):
            self.assertIsNone(youtube_counter_integer(value),value)

    def test_video_ids_have_eleven_characters(self):
        self.assertIsNotNone(VIDEO_ID_RE.fullmatch('dQw4w9WgXcQ'))
        for value in ('short','dQw4w9WgXcQx','a'*128,'dQw4w9WgXc,'):
            self.assertIsNone(VIDEO_ID_RE.fullmatch(value),value)

if __name__=='__main__':unittest.main()
