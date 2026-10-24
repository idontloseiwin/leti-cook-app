"""Two source dates through actual common/initial planning; offline repositories."""
from dataclasses import replace
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).resolve().parent)]
from collection import initial_video_hydration as initial
from collection.provider_steps import ProviderResult
from collection.video_payloads import build_video_counter_payload
from db import VideoCollectionClaim
from db.videos import video_metadata_completion as metadata_sql
from db.videos import video_typed_metadata_completion as typed_sql
from test_common_video_dispatch import Repository
from test_initial_video_parallel import AtomicRepository, CHANNEL, STAMP, service, video as profile_video
from test_video_metadata_completion import source
from test_collection_policy import rows as cadence_rows

GENERAL = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
STATS = datetime(2026, 10, 10, 0, 1, tzinfo=timezone.utc)
video = partial(profile_video, observed_at=GENERAL)


class Clock:
    stamp = GENERAL
    @classmethod
    def now(cls, tz):
        return cls.stamp


def claim(identity='abcdefghijk', kind='long', mode='metadata'):
    return VideoCollectionClaim(video_id=identity, channel_id=CHANNEL, video_type=kind,
        collection_mode=mode, lease_token='token-' + identity,
        lease_expires_at=STAMP, next_due_at=STAMP)


class Repo(Repository):
    def complete_typed_metadata(self, rows): return self.record('typed', rows)
    def complete_stream_lifecycle(self, rows): return self.record('lifecycle', rows)
    def complete_identity_refresh(self, rows): return self.record('identity', rows)


class VideoSourceClockTests(unittest.TestCase):
    def common(self, claims, items, *, general_clock=GENERAL, stats_clock=STATS):
        repo = Repo()
        collector = service(repo, Mock())
        collector.build_counter_payload = build_video_counter_payload
        collector.fetch_metadata = lambda ids, reason: items
        collector.fetch_counters = lambda ids, reason: {'items': [dict(id=identity, views=123,
            like_count=7, comment_count=2) for identity in ids], 'summary': {'failed_ids': []}}
        steps = collector.common_claim_steps(claims)
        attempted = []
        try:
            step = next(steps)
            while True:
                is_general = step.fetch == collector.fetch_metadata
                attempted.append('general' if is_general else 'stats')
                Clock.stamp = general_clock if is_general else stats_clock
                with patch('collection.provider_steps.datetime', Clock):
                    response = step.execute()
                # Date remains fixed even though the generator resumes after
                # the source clock override ends on a later owner wall clock.
                step = steps.send(response)
        except StopIteration as done:
            return repo, done.value, attempted
        finally:
            steps.close()

    def test_common_general_and_stats_across_midnight_keep_separate_dates(self):
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind):
                item = video('abcdefghijk', ended=(kind == 'stream'))
                item["statistics"].pop("viewCount")  # Required counter fallback crosses midnight.
                repo, result, calls = self.common([claim(kind=kind)], [item])
                row = repo.writes[-1][1][0]
                self.assertEqual(calls, ['general', 'stats'])
                self.assertEqual(row['metadata_observed_at'], GENERAL.isoformat())
                self.assertEqual(row['availability_checked_at'], GENERAL.isoformat())
                self.assertEqual(row['recorded_at'], STATS.isoformat())
                if kind == 'long':
                    self.assertEqual(row['availability_restored_at'], GENERAL.isoformat())
                self.assertEqual(result['updated'], 1)

    def test_counter_only_needs_only_stats_observation(self):
        repo, result, calls = self.common([claim(mode='counters')], [], general_clock=None)
        self.assertEqual(calls, ['stats'])
        self.assertEqual(repo.writes[-1][0], 'counters')
        self.assertEqual(repo.writes[-1][1][0]['recorded_at'], STATS.isoformat())
        self.assertNotIn('metadata_observed_at', repo.writes[-1][1][0])

    def test_missing_lifecycle_and_identity_use_only_general_time(self):
        cases = ([claim()], [], 'missing'), ([claim(kind='stream', mode='stream_lifecycle')],
                 [video('abcdefghijk')], 'lifecycle'), ([claim(kind='unknown', mode='identity_refresh')],
                 [video('abcdefghijk')], 'identity')
        for claims, items, operation in cases:
            with self.subTest(operation=operation):
                repo, result, calls = self.common(claims, items)
                self.assertEqual(calls, ['general'])
                self.assertEqual(repo.writes[-1][0], operation)
                self.assertEqual(repo.writes[-1][1][0]['recorded_at'], GENERAL.isoformat())

    def test_missing_or_naive_provider_clock_never_renews_that_source(self):
        cases = ({'general_clock': None}, {'general_clock': GENERAL.replace(tzinfo=None)},
                 {'stats_clock': None}, {'stats_clock': STATS.replace(tzinfo=None)})
        for values in cases:
            with self.subTest(values=values):
                item = video('abcdefghijk')
                item["statistics"].pop("viewCount")
                repo, result, calls = self.common([claim()], [item], **values)
                self.assertEqual(result['updated'], 0)
                if 'general_clock' in values:
                    self.assertTrue(all(operation == 'failure' for operation, _ in repo.writes))
                else:
                    self.assertEqual(repo.writes[0][0],'identity')
                    self.assertEqual(repo.writes[0][1][0]['recorded_at'],GENERAL.isoformat())
                    self.assertTrue(repo.writes[0][1][0]['counterless_profile'])
                    self.assertNotIn('views',repo.writes[0][1][0])

    def initial_response(self, *, fallback_error=False):
        repo = AtomicRepository()
        items = [video('healthy'), video('fallback')]
        items[1]['statistics'].pop('viewCount')
        collector = service(repo, lambda ids, reason: items)
        def counters(ids, reason):
            self.assertEqual(ids, ['fallback'])
            if fallback_error:
                raise RuntimeError('synthetic batch failure')
            return {'items': [dict(id='fallback', views=789, like_count=8, comment_count=3)],
                    'summary': {'failed_ids': []}}
        collector.fetch_counters = counters
        claims = [claim('healthy', mode='hydrate'), claim('fallback', mode='hydrate')]
        stamps = iter((GENERAL, STATS))
        class InitialClock:
            @staticmethod
            def now(tz): return next(stamps)
        with patch.object(initial, 'datetime', InitialClock):
            response = initial.fetch_initial_response(collector, claims, reason='initial')
        return collector, repo, claims, response

    def test_initial_only_successful_batch_fallback_ids_receive_stats_date(self):
        collector, repo, claims, response = self.initial_response()
        self.assertEqual(response.metadata_observed_at, GENERAL)
        self.assertEqual(response.counters_observed_at, STATS)
        self.assertEqual(response.counter_observed_ids, ('fallback',))
        result = initial.settle_initial_response(collector, claims, response, reason='initial')
        rows = {row['video_id']: row for command in repo.commits[0] for row in command['rows']}
        self.assertEqual(rows['healthy']['recorded_at'], GENERAL.isoformat())
        self.assertEqual(rows['fallback']['recorded_at'], STATS.isoformat())
        self.assertTrue(all(row['metadata_observed_at'] == GENERAL.isoformat() for row in rows.values()))
        self.assertEqual(result['updated'], 2)
        self.assertEqual(len(repo.commits), 1)

    def test_initial_fallback_refusal_keeps_healthy_general_source_date(self):
        collector, repo, claims, response = self.initial_response(fallback_error=True)
        self.assertIsNone(response.counters_observed_at)
        self.assertEqual(response.counter_observed_ids, ())
        initial.settle_initial_response(collector, claims, response, reason='initial')
        rows = {row['video_id']: (command['operation'], row)
                for command in repo.commits[0] for row in command['rows']}
        self.assertEqual(rows['healthy'][1]['recorded_at'], GENERAL.isoformat())
        self.assertEqual(rows['healthy'][1]['metadata_observed_at'], GENERAL.isoformat())
        self.assertEqual(rows['fallback'][0], 'complete_identity_refresh')
        self.assertEqual(rows['fallback'][1]['recorded_at'],GENERAL.isoformat())
        self.assertTrue(rows['fallback'][1]['counterless_profile'])
        self.assertNotIn('views',rows['fallback'][1])

    def test_initial_missing_clock_or_invalid_fallback_provenance_cannot_commit(self):
        collector, repo, claims, response = self.initial_response()
        bad = (replace(response, metadata_observed_at=None), replace(response, counters_observed_at=None),
               replace(response, counter_observed_ids=('foreign',)),
               replace(response, counter_observed_ids=('fallback', 'fallback')))
        for value in bad:
            with self.subTest(value=value.counter_observed_ids), self.assertRaises(ValueError):
                initial.settle_initial_response(collector, claims, value, reason='initial')
        self.assertEqual(repo.commits, [])

    def test_long_policy_selects_each_existing_cadence_at_its_own_source_age(self):
        rows = cadence_rows()
        captured = {'cadence': rows, 'sources': [source(metadata_observed_at=GENERAL.isoformat(),
                    recorded_at=STATS.isoformat(), age_seconds=86401, metadata_age_seconds=86399)]}
        from db.videos.video_metadata_completion import select_long_policy_at_age as actual
        from db.videos.video_metadata_completion import select_counter_policy_at_age as counters_actual
        with patch.object(metadata_sql, 'select_long_policy_at_age', wraps=actual) as policy, \
             patch.object(metadata_sql,'select_counter_policy_at_age',wraps=counters_actual) as counters:
            schedules = metadata_sql._schedules(captured)
        self.assertEqual([(call.args[1], call.args[3]) for call in policy.call_args_list],
                         [('metadata', 86399)])
        self.assertEqual([(call.args[1],call.args[3]) for call in counters.call_args_list],[('long',86401)])
        self.assertEqual(schedules[0]['recorded_at'], STATS.isoformat())
        self.assertEqual(schedules[0]['metadata_observed_at'], GENERAL.isoformat())

    def test_direct_sql_retains_both_clock_guards_and_existing_columns(self):
        self.assertIn('metadata_at>=wall-interval', metadata_sql._AVAILABILITY_CLOCKS)
        self.assertIn('metadata_at<=wall', metadata_sql._AVAILABILITY_CLOCKS)
        self.assertIn('(metadata_supplied AND metadata_observed_at IS NULL)', metadata_sql._VALID)
        self.assertIn('metadata_source_checked_at=input.metadata_observed_at', metadata_sql._COMPLETE)
        self.assertIn('stats_source_checked_at=input.recorded_at', metadata_sql._COMPLETE)
        self.assertIn('excluded.metadata_checked_at>=video.metadata_checked_at', metadata_sql._UPSERT)
        self.assertIn('excluded.availability_checked_at>=video.availability_checked_at', metadata_sql._UPSERT)
        self.assertIn('metadata_source_checked_at=coalesce(input.metadata_observed_at,input.recorded_at)', typed_sql._COMPLETE)
        self.assertIn('md5(raw.payload::text)', typed_sql._INPUT)
        self.assertIn('receipt.payload_fingerprint IS DISTINCT FROM input.payload_fingerprint', typed_sql._MISMATCH)
        # Legacy callers' explicit recorded_at remains the sole fallback; no
        # owner/DB wall clock is substituted for a new source observation.
        self.assertIn('coalesce(input.metadata_observed_at,input.recorded_at)', metadata_sql._UPSERT)


if __name__ == '__main__':
    unittest.main()
