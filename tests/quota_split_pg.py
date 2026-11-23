"""On a disposable PG17: an exhausted bucket moves only its own deadline.

Never auto-discovered; the same disposable-cluster contract as research_query_pg.
One synthetic channel and synthetic videos, no YouTube. The actual failure,
claim, candidate, counter completion and quota-ledger owners run as the
collector's role (authenticator -> service_role).
"""
from datetime import timedelta
import os
from pathlib import Path
import sys
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query_pg import bootstrap
from collection_policy import youtube_quota_reset_at
from collection_schedule import CollectionSchedules
from db.base.postgres import PostgresConfig, PostgresDatabase
from db.channel.channel_sync import ChannelSyncRepository
from db.quota.youtube_quota import YouTubeQuotaRepository
from db.videos.video_collection import VideoCollectionRepository

CHANNEL = 'UC' + 'quotasplit'.ljust(22, '0')


class QuotaSplitPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
            raise RuntimeError('disposable_cluster_required')
        port, password = int(os.environ['LIC_QUERY_TEST_PORT']), os.environ['LIC_QUERY_TEST_PASSWORD']
        cls.admin = psycopg.connect(host='127.0.0.1', port=port, dbname='leti-cook', user='postgres',
                                    password=password, autocommit=True)
        if not 170000 <= int(cls.admin.execute('SHOW server_version_num').fetchone()[0]) < 180000:
            raise RuntimeError('pg17_required')
        bootstrap(cls.admin)
        cls.admin.row_factory = dict_row
        cls.db = PostgresDatabase(PostgresConfig(
            f'host=127.0.0.1 port={port} dbname=leti-cook user=authenticator password={password}', max_size=2),
            cadence_snapshot=CollectionSchedules().cadence_snapshot(), admission=lambda _: None).open()
        result = ChannelSyncRepository(cls.db).insert_channel({
            'channel_id': CHANNEL, 'title': 'Quota split synthetic',
            'user_input': 'https://www.youtube.com/channel/' + CHANNEL,
            'initial_long_inventory_status': 'completed', 'initial_28d_stats_status': 'completed'},
            permission='accepted')
        assert isinstance(result, dict), result
        cls.repository = VideoCollectionRepository(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.close()

    def video(self, *, hydration=False, stats_hours=-1, metadata_hours=-2, first_missing=False):
        """One due long video: negative hours are due, positive are later."""
        identity = 'q' + uuid.uuid4().hex[:10]
        old = self.admin.execute("SELECT clock_timestamp()-interval '2 days' AS at").fetchone()['at']
        with self.admin.transaction():
            self.admin.execute("""INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,
                published_acquired_at) VALUES(%s,%s,'long',%s,%s)""", (identity, CHANNEL, old - timedelta(days=30), old))
            if not hydration:
                self.admin.execute("""INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,
                    metadata_checked_at,availability_checked_at,availability_status,privacy_status,upload_status,
                    views,like_count,comment_count) VALUES(%s,%s,%s,%s,%s,%s,'public','public','processed',1,1,0)""",
                    (identity, CHANNEL, old - timedelta(days=30), old, old, old))
            self.admin.execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
                hydration_needed,profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,
                stats_source_checked_at,metadata_source_checked_at,availability_first_missing_at)
                SELECT %(id)s,%(channel)s,'long',%(hydration)s,1,stats,metadata,least(stats,metadata),
                  CASE WHEN NOT %(hydration)s THEN %(old)s END,CASE WHEN NOT %(hydration)s THEN %(old)s END,
                  CASE WHEN %(missing)s THEN %(old)s END
                FROM (SELECT CASE WHEN %(hydration)s THEN NULL ELSE now()+make_interval(hours=>%(stats)s) END AS stats,
                             now()+make_interval(hours=>%(metadata)s) AS metadata) due""",
                dict(id=identity, channel=CHANNEL, hydration=hydration, stats=stats_hours, metadata=metadata_hours,
                     old=old, missing=first_missing))
        self.addCleanup(self.forget, identity)
        return identity

    def forget(self, identity):
        with self.admin.transaction():
            for table in ('video_collection_state', 'videos', 'video_inventory'):
                self.admin.execute(f'DELETE FROM public.{table} WHERE video_id=%s', (identity,))

    def state(self, identity):
        return self.admin.execute("""SELECT state.*,now() AS clock FROM public.video_collection_state state
            WHERE video_id=%s""", (identity,)).fetchone()

    def claim(self, operation):
        result = self.repository.claim_due_batch(limit=50, worker_id='quota-split-pg', run_id=str(uuid.uuid4()),
            lease_seconds=300, scopes=[{'channel_id': CHANNEL, 'video_type': 'long'}], operation=operation)
        return {row.video_id: row for row in result['rows']}

    def fail(self, claims, bucket, error='youtube_quota_exhausted'):
        rows = [{'video_id': row.video_id, 'video_type': row.video_type, 'lease_token': row.lease_token,
                 'error_type': error, 'retry_after_seconds': 3600,
                 **({'quota_bucket': bucket} if bucket else {})} for row in claims]
        self.assertEqual(self.repository.fail_batch(rows), len(rows))

    def reset_at(self, row):
        # failure_policy: the next Pacific midnight, at least an hour away.
        return max(row['updated_at'] + timedelta(hours=1), youtube_quota_reset_at(row['updated_at']))

    def test_general_refusal_turns_a_video_with_due_counters_into_stats_work(self):
        video = self.video(stats_hours=-1, metadata_hours=-2)
        before = self.state(video)
        claims = self.claim('metadata')
        self.assertEqual(claims[video].collection_mode, 'metadata')
        self.fail([claims[video]], 'general')
        after = self.state(video)
        # Only the details deadline moved; the counters keep their own.
        self.assertEqual(after['metadata_due_at'], self.reset_at(after))
        self.assertEqual(after['stats_due_at'], before['stats_due_at'])
        self.assertEqual(after['next_due_at'], before['stats_due_at'])
        self.assertEqual((after['last_error_type'], after['consecutive_failures'], after['lease_token']),
                         ('youtube_quota_exhausted', before['consecutive_failures'], None))
        # The next pass gives it to Stats, not to General.
        self.assertNotIn(video, self.claim('metadata'))
        candidates = self.repository.due_candidate_page(video_type='long', run_id=str(uuid.uuid4()),
                                                        after=None, limit=10, operation='counters')
        self.assertEqual([row['channel_id'] for row in candidates['rows']], [CHANNEL])
        claims = self.claim('counters')
        self.assertEqual(claims[video].collection_mode, 'counters')
        # Counters arrive; the details still wait for the reset: no loop, no lost deadline.
        self.assertEqual(self.repository.complete_counters([{
            'video_id': video, 'lease_token': claims[video].lease_token, 'collection_attempt_id': str(uuid.uuid4()),
            'views': 7, 'like_count': 1, 'comment_count': 0, 'recorded_at': after['clock'].isoformat()}]), 1)
        done = self.state(video)
        self.assertEqual(done['metadata_due_at'], after['metadata_due_at'])
        self.assertGreater(done['stats_due_at'], done['clock'])
        self.assertEqual(done['next_due_at'], min(done['stats_due_at'], done['metadata_due_at']))
        self.assertNotIn(video, self.claim('counters'))
        self.assertNotIn(video, self.claim('metadata'))

    def test_general_refusal_keeps_work_only_general_can_do_waiting_whole(self):
        first = self.video(hydration=True, metadata_hours=-2)
        missing = self.video(stats_hours=-1, metadata_hours=-2, first_missing=True)
        claims = self.claim('metadata')
        self.assertEqual({claims[first].collection_mode, claims[missing].collection_mode}, {'hydrate', 'metadata'})
        before = {video: self.state(video) for video in (first, missing)}
        self.fail([claims[first], claims[missing]], 'general')
        for video in (first, missing):
            after = self.state(video)
            self.assertEqual(after['next_due_at'], self.reset_at(after))
            self.assertEqual((after['metadata_due_at'], after['stats_due_at']),
                             (before[video]['metadata_due_at'], before[video]['stats_due_at']))
        self.assertFalse({first, missing} & set(self.claim('counters')))

    def test_stats_refusal_moves_only_the_counters_deadline(self):
        video = self.video(stats_hours=-1, metadata_hours=3)
        before = self.state(video)
        claims = self.claim('counters')
        self.assertEqual(claims[video].collection_mode, 'counters')
        self.fail([claims[video]], 'stats')
        after = self.state(video)
        self.assertEqual(after['stats_due_at'], self.reset_at(after))
        self.assertEqual(after['metadata_due_at'], before['metadata_due_at'])
        # The next General check, when earlier, brings the counters with the details.
        self.assertEqual(after['next_due_at'], min(after['stats_due_at'], before['metadata_due_at']))
        self.assertNotIn(video, self.claim('counters'))
        self.assertNotIn(video, self.claim('metadata'))

    def test_an_unnamed_quota_refusal_waits_whole_as_before(self):
        video = self.video(stats_hours=-1, metadata_hours=-2)
        before = self.state(video)
        self.fail([self.claim('metadata')[video]], None)
        after = self.state(video)
        self.assertEqual(after['next_due_at'], self.reset_at(after))
        self.assertEqual((after['metadata_due_at'], after['stats_due_at']),
                         (before['metadata_due_at'], before['stats_due_at']))

    def test_ledger_names_only_buckets_refused_with_quota_today(self):
        ledger = YouTubeQuotaRepository(self.db, project_id='primary')
        self.assertEqual(ledger.exhausted_buckets(), ())
        today = self.admin.execute("SELECT (now() AT TIME ZONE 'America/Los_Angeles')::date AS day").fetchone()['day']

        def attempt(bucket, *, error_type, day=today, status=403):
            identity = str(uuid.uuid4())
            ledger.start_attempt({'request_attempt_id': identity, 'quota_bucket': bucket, 'quota_day': day.isoformat(),
                                  'phase': 'auto_video_collection', 'endpoint': 'videos', 'quota_units': 1,
                                  'status': 'started', 'started_at': self.state_clock().isoformat()})
            ledger.finish_attempt(identity, {'status': 'http_error', 'finished_at': self.state_clock().isoformat(),
                                             'duration_ms': 5, 'http_status': status, 'error_type': error_type})
        # A forbidden answer, a refusal of yesterday and another project's refusal are not today's quota.
        attempt('video_batch_get_stats', error_type=None)
        attempt('video_batch_get_stats', error_type='quotaExceeded', day=today - timedelta(days=1))
        YouTubeQuotaRepository(self.db, project_id='other').start_attempt({
            'request_attempt_id': str(uuid.uuid4()), 'quota_bucket': 'general', 'quota_day': today.isoformat(),
            'phase': 'auto_video_collection', 'endpoint': 'videos', 'quota_units': 1, 'status': 'started',
            'started_at': self.state_clock().isoformat()})
        self.assertEqual(ledger.exhausted_buckets(), ())
        attempt('general', error_type='quotaExceeded')
        stored = self.admin.execute("""SELECT error_type FROM public.youtube_api_request_attempts
            WHERE project_id='primary' AND quota_bucket='general'""").fetchall()
        self.assertEqual([row['error_type'] for row in stored], ['quotaExceeded'])
        self.assertEqual(ledger.exhausted_buckets(), ('general',))
        attempt('video_batch_get_stats', error_type='quotaExceeded')
        self.assertEqual(ledger.exhausted_buckets(), ('general', 'stats'))

    def state_clock(self):
        return self.admin.execute('SELECT clock_timestamp() AS at').fetchone()['at']


if __name__ == '__main__':
    unittest.main()
