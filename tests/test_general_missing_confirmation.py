"""One General omission is confirmed in the next pass before it counts.

Actual common service, payload builders and failure policy; recording
repository, synthetic provider responses. No database, network or YouTube call.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).resolve().parent)]
from collection_outcome import collection_explanation, sync_completion_fields
from collection_policy import STATS_AVAILABILITY_CHECK_REQUIRED, failure_policy
from collection.video_payloads import build_video_counter_payload
from db import VideoCollectionClaim
from db.runs.sync_run_summary import persisted_summary_copy
from db.videos import video_claims, video_initial_claims, video_scheduled_claims
from test_initial_video_parallel import CHANNEL, STAMP, service
from test_video_source_clocks import GENERAL, STATS, Clock, Repo, video

LABEL = 'youtube_video_missing_unconfirmed'


def claim(identity, kind='long', mode='metadata'):
    # Claims default to no prior omission.
    return VideoCollectionClaim(video_id=identity, channel_id=CHANNEL, video_type=kind,
        collection_mode=mode, lease_token='token-' + identity, lease_expires_at=STAMP, next_due_at=STAMP)


def repeated(value):
    """The queue already holds omission evidence for this claim."""
    return replace(value, prior_omission=True)


class GeneralMissingConfirmationTests(unittest.TestCase):
    def run_claims(self, claims, items, counters=None):
        repo = Repo()
        collector = service(repo, Mock())
        collector.build_counter_payload = build_video_counter_payload
        self.calls = []

        def general(ids, reason):
            self.calls.append(('general', tuple(ids)))
            return items

        def stats(ids, reason):
            self.calls.append(('stats', tuple(ids)))
            return counters(ids) if counters else {'items': [dict(id=identity, views=5, like_count=1,
                comment_count=0) for identity in ids], 'summary': {'failed_ids': []}}

        collector.fetch_metadata, collector.fetch_counters = general, stats
        steps = collector.common_claim_steps(claims)
        try:
            step = next(steps)
            while True:
                Clock.stamp = GENERAL if step.fetch == general else STATS
                with patch('collection.provider_steps.datetime', Clock):
                    response = step.execute()
                step = steps.send(response)
        except StopIteration as done:
            return repo, done.value
        finally:
            steps.close()

    @staticmethod
    def writes(repo):
        return [(operation, row['video_id']) for operation, rows in repo.writes for row in rows]

    def test_first_omission_is_released_for_one_more_general_look(self):
        repo, result = self.run_claims([claim('absentvideo'), claim('presentvideo')], [video('presentvideo')])
        self.assertEqual(self.writes(repo), [('failure', 'absentvideo'), ('metadata', 'presentvideo')])
        released = repo.writes[0][1][0]
        self.assertEqual((released['error_type'], released['retry_after_seconds']), (LABEL, 30))
        self.assertEqual((result['missing'], result['unavailable_marked'], result['missing_unconfirmed'],
                          result['errors'], result['updated']), (0, 0, 1, 0, 1))
        self.assertEqual(result['missing_video_ids'], [])
        self.assertEqual(result['completion_outcome'], 'partial')
        self.assertEqual([call[0] for call in self.calls], ['general'])
        self.assertIn({'code': 'missing_unconfirmed', 'count': 1}, collection_explanation(result)['reasons'])

    def test_first_omission_of_every_type_and_general_mode_waits(self):
        claims = [claim('longvideo00'), claim('shortvideo0', 'short', 'hydrate'),
                  claim('streamvideo', 'stream', 'stream_lifecycle')]
        repo, result = self.run_claims(claims, [])
        self.assertEqual(repo.writes[0][0], 'failure')
        self.assertEqual(len(repo.writes), 1)
        self.assertEqual({row['video_id']: (row['video_type'], row['error_type']) for row in repo.writes[0][1]},
                         {'longvideo00': ('long', LABEL), 'shortvideo0': ('short', LABEL), 'streamvideo': ('stream', LABEL)})
        self.assertEqual((result['missing_unconfirmed'], result['unavailable_marked']), (3, 0))
        for kind in ('long', 'short', 'stream'):
            self.assertEqual((result[kind + '_missing'], result[kind + '_unavailable_marked']), (0, 0))

    def test_second_omission_marks_the_video_unavailable(self):
        repo, result = self.run_claims([repeated(claim('absentvideo'))], [])
        self.assertEqual(self.writes(repo), [('missing', 'absentvideo')])
        row = repo.writes[0][1][0]
        self.assertEqual((row['availability_status'], row['recorded_at']), ('unknown_unavailable', GENERAL.isoformat()))
        self.assertEqual((result['missing'], result['unavailable_marked'], result['missing_unconfirmed']), (1, 1, 0))
        self.assertEqual(result['long_unavailable_marked'], 1)

    def test_video_returned_on_confirmation_is_an_ordinary_write(self):
        repo, result = self.run_claims([repeated(claim('backagain00'))], [video('backagain00')])
        self.assertEqual(self.writes(repo), [('metadata', 'backagain00')])
        self.assertEqual((result['updated'], result['missing'], result['missing_unconfirmed']), (1, 0, 0))

    def test_mixed_batch_releases_only_first_omissions(self):
        claims = [claim('firstmiss00'), repeated(claim('againmiss00', 'short')), claim('present0000')]
        repo, result = self.run_claims(claims, [video('present0000')])
        self.assertEqual(sorted(self.writes(repo)),
                         [('failure', 'firstmiss00'), ('metadata', 'present0000'), ('missing', 'againmiss00')])
        self.assertEqual((result['missing_unconfirmed'], result['unavailable_marked'], result['updated']), (1, 1, 1))

    def test_identity_refresh_keeps_its_own_missing_receipt(self):
        repo, result = self.run_claims([claim('unknownvid0', 'unknown', 'identity_refresh')], [])
        self.assertEqual(self.writes(repo), [('identity', 'unknownvid0')])
        self.assertTrue(repo.writes[0][1][0]['missing'])
        self.assertEqual(result['missing_unconfirmed'], 0)
        self.assertEqual((result['completion_outcome'], result['unresolved']), ('completed', 0))
        self.assertEqual((result['identity_missing_settled'], result['unavailable_marked']), (1, 0))
        stored = persisted_summary_copy({'video_collection': result})
        self.assertEqual(stored['video_collection']['identity_missing_settled'], 1)
        self.assertEqual(sync_completion_fields(stored)['completion_outcome'], 'completed')

    def test_settled_identity_does_not_cover_an_unconfirmed_omission(self):
        claims = [claim('unknownvid0', 'unknown', 'identity_refresh'), claim('firstmiss00'),
                  repeated(claim('againmiss00', 'short')), claim('present0000')]
        _, result = self.run_claims(claims, [video('present0000')])
        self.assertEqual((result['completion_outcome'], result['unresolved']), ('partial', 1))
        self.assertEqual((result['missing'], result['unavailable_marked'],
                          result['identity_missing_settled'], result['missing_unconfirmed']), (2, 1, 1, 1))

    def test_stats_omission_still_hands_off_to_general(self):
        empty = lambda ids: {'items': [], 'summary': {'failed_ids': []}}
        repo, result = self.run_claims([claim('statsonly00', mode='counters')], [], counters=empty)
        self.assertEqual([call[0] for call in self.calls], ['stats'])
        row = repo.writes[0][1][0]
        self.assertEqual((repo.writes[0][0], row['error_type'], row['retry_after_seconds']),
                         ('failure', STATS_AVAILABILITY_CHECK_REQUIRED, 30))
        self.assertEqual((result['metadata_deferred'], result['missing_unconfirmed']), (1, 0))

    def test_malformed_response_is_not_an_omission(self):
        repo, result = self.run_claims([claim('absentvideo')], [{'id': 'foreignvide'}])
        self.assertEqual(self.writes(repo), [('failure', 'absentvideo')])
        self.assertEqual(repo.writes[0][1][0]['error_type'], 'malformed_youtube_metadata_response')
        self.assertEqual(result['missing_unconfirmed'], 0)


class ConfirmationPolicyTests(unittest.TestCase):
    def test_release_is_thirty_seconds_without_backoff(self):
        now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        for failures in (0, 12):
            policy = failure_policy(now=now, consecutive_failures=failures, error_type=LABEL, retry_after_seconds=30)
            self.assertEqual((policy.retry_at, policy.consecutive_failures, policy.error_type),
                             (now + timedelta(seconds=30), failures, LABEL))

    def test_claim_reads_prior_omission_only_as_true(self):
        row = dict(video_id='abcdefghijk', channel_id=CHANNEL, collection_mode='metadata', lease_token='t',
                   lease_expires_at=STAMP, next_due_at=STAMP, video_type='long')
        self.assertFalse(VideoCollectionClaim.from_row(row).prior_omission)
        for value, expected in ((True, True), (False, False), (None, False), ('true', False), (1, False)):
            self.assertIs(VideoCollectionClaim.from_row({**row, 'prior_omission': value}).prior_omission, expected)

    def test_every_claim_path_returns_the_same_omission_evidence(self):
        from collection_policy import PRIOR_OMISSION_SQL
        for label in (LABEL, STATS_AVAILABILITY_CHECK_REQUIRED, 'availability_first_missing_at IS NOT NULL'):
            self.assertIn(label, PRIOR_OMISSION_SQL)
        for statement in (video_scheduled_claims._CLAIM, video_claims._CLAIM_DUE, video_claims._CLAIM_METADATA,
                          video_initial_claims._CLAIM):
            self.assertEqual(statement.count(PRIOR_OMISSION_SQL), 1)
            self.assertIn('prior_omission', statement)


if __name__ == '__main__':
    unittest.main()
