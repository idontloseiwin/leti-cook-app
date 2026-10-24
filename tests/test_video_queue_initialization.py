"""Borrowed queue initialization preserves native clock range and queue ownership."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from datetime import timedelta
from pathlib import Path
import sys
import unittest
import psycopg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import collection_policy as policy
from db.videos.collection_policy import load_cadence_snapshot_in_transaction
from db.videos.video_queue_initialization import ensure_video_collection_state_in_transaction, _LONG_INPUT, _LONG_UPSERT, _TYPED_INSERT
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,PostgresConfig,PostgresDatabase,Operation
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_collection_policy import rows,seed_snapshot,NOW

VIDEO='aaaaaaaaaaa';CHANNEL='UC0000000000000000000001'


def one(**values):return Result((values,))
def source(**changes):
    result = dict(dict(video_id=VIDEO,channel_id=CHANNEL,needs_hydration=False,published_at='10000-01-01T00:00:00+00:00',
        checked_at='0001-01-01T00:00:00+00:00 BC',metadata_checked_at='0001-01-01T00:00:00+00:00 BC',
        availability_checked_at='10000-01-01T00:00:00+00:00',availability_status='public',is_unavailable=False,
        collection_mode='active',owner_permission='accepted',as_of=NOW.isoformat(),age_seconds=0,published_invalid=False,
        stats_now=False,metadata_now=False,availability_now=True,as_of_valid=True),**changes)
    result['policy_facts']=native_policy_facts(result.pop('collection_mode'))
    return result


class QueueInitializationTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def run_helper(self,ids):
        with self.database.transaction(Operation('queue_initialization_test')) as tx:
            return ensure_video_collection_state_in_transaction(tx,ids)
    def header(self,sources=None,cadence=None):
        self.queue(one(ids=[VIDEO],count=1),one(sources=[source()] if sources is None else sources,cadence=rows() if cadence is None else cadence))

    def test_extended_year_source_clocks_are_projected_and_bound_without_python_datetime_conversion(self):
        self.header();self.queue(one(video_id=VIDEO),Result())
        self.assertEqual(self.run_helper([VIDEO]),1)
        payload=self.connection.queries[2][1][0].obj[0]
        self.assertEqual(self.connection.queries[2][1][1],2419200)
        self.assertEqual(payload['published_at'],'10000-01-01T00:00:00+00:00')
        self.assertEqual(payload['checked_at'],'0001-01-01T00:00:00+00:00 BC')
        self.assertEqual(payload['stats_seconds'],seed_snapshot().rules[0].cadence_seconds)
        self.assertTrue(payload['availability_supported']);self.assertIsNone(payload['paused_reason'])
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
        self.assertIn('extract(epoch FROM (now()-published_at))::bigint',self.connection.queries[1][0])
        self.assertIn("make_interval(secs=>input.stats_seconds::double precision)",self.connection.queries[2][0])
        self.assertIn("input.availability_checked_at+make_interval(secs=>%s::integer)",self.connection.queries[2][0])

    def test_source_and_cadence_share_one_statement_snapshot_and_have_no_static_policy_defaults(self):
        changed=rows();changed[0]['cadence_seconds']=43
        self.header(cadence=changed);self.queue(one(video_id=VIDEO),Result())
        self.run_helper([VIDEO]);payload=self.connection.queries[2][1][0].obj[0]
        self.assertEqual(payload['stats_seconds'],43)
        self.assertIn('cadence AS MATERIALIZED',_LONG_INPUT);self.assertIn('sources AS MATERIALIZED',_LONG_INPUT)
        self.assertEqual(len(self.connection.queries),4)

    def test_empty_input_returns_before_source_read_but_native_cast_happens_first(self):
        self.queue(one(ids=[],count=0));self.assertEqual(self.run_helper([]),0)
        self.assertEqual(len(self.connection.queries),1)
        for count in (None,1001):
            self.queue(one(ids=None,count=count))
            with self.assertRaises(DatabaseServerError) as caught:self.run_helper(None)
            self.assertEqual((caught.exception.code,caught.exception.message),('P0001','database_operation_failed'))

    def test_native_infinity_age_error_precedes_all_mutations(self):
        self.queue(one(ids=[VIDEO],count=1),Result(execute_error=ServerFailure('22008','private infinity')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([VIDEO])
        self.assertEqual(caught.exception.code,'22008');self.assertEqual(self.connection.commits,0)
        self.assertFalse(any('INSERT INTO' in query for query,_ in self.connection.queries))

    def test_typed_only_does_not_require_a_long_cadence_match_and_is_insert_only(self):
        self.header(sources=[],cadence=[]);self.queue(Result(),one(video_id=VIDEO))
        self.assertEqual(self.run_helper([VIDEO]),1)
        self.assertEqual(self.connection.queries[2][1][0].obj,[])
        self.assertIn('ON CONFLICT(video_id) DO NOTHING',_TYPED_INSERT)
        self.assertNotIn('DO UPDATE',_TYPED_INSERT)
        self.assertIn('NOT EXISTS(SELECT 1 FROM public.video_collection_state',_TYPED_INSERT)
        self.assertIn('OFFSET 0',_TYPED_INSERT)

    def test_long_upsert_preserves_the_retained_retry_and_lease_predicate(self):
        self.assertIn('WHERE state.lease_token IS NULL',_LONG_UPSERT)
        self.assertIn('THEN state.next_due_at ELSE excluded.next_due_at END',_LONG_UPSERT)
        self.assertIn("control.channel_id=state.channel_id",_LONG_UPSERT)
        self.assertIn("control.collection_mode='active'",_LONG_UPSERT)
        self.assertNotIn('lease_token=NULL',_LONG_UPSERT)
        self.assertNotIn('public.channel_sync_pause_reason(',_LONG_UPSERT)
        self.assertIn('state.availability_first_missing_at IS NULL',_LONG_UPSERT)
        self.assertIn("state.paused_reason IS DISTINCT FROM 'availability_retry_expired'",_LONG_UPSERT)

    def test_missing_episode_keeps_calendar_while_claims_own_channel_control(self):
        vectors=[(dict(is_unavailable=True,availability_status='private',collection_mode='metadata_only'),None),
                 (dict(availability_status='private',collection_mode='metadata_only'),'availability_unsupported'),
                 (dict(collection_mode='metadata_only'),None),
                 (dict(collection_mode='initial_inventory'),'channel_inventory_not_ready')]
        for changes,expected in vectors:
            self.header(sources=[source(**changes)]);self.queue(one(video_id=VIDEO),Result())
            self.run_helper([VIDEO]);self.assertEqual(self.connection.queries[-2][1][0].obj[0]['paused_reason'],expected)

    def test_recreated_missing_queue_keeps_original_episode_start_and_stop(self):
        for age, expected_day, pause in ((6, 7, None), (20, 27, None), (29, None, 'availability_retry_expired')):
            first=NOW-timedelta(days=age)
            self.header(sources=[source(is_unavailable=True,availability_status='unknown_unavailable',
                collection_mode='metadata_only',
                unavailable_first_missing_at=first.isoformat())])
            self.queue(one(video_id=VIDEO),Result())
            self.run_helper([VIDEO])
            payload=self.connection.queries[-2][1][0].obj[0]
            self.assertEqual(payload['first_missing_at'],first.isoformat())
            self.assertEqual(payload['schedule_type'],'long')
            self.assertEqual(payload['paused_reason'],pause)
            self.assertEqual(payload['episode_next_due_at'],
                (first+timedelta(days=expected_day)).isoformat() if expected_day is not None else None)

    def test_missing_metadata_cadence_preserves_null_nominal_and_native_ceiling(self):
        cadence=[row for row in rows() if row['collection_kind']!='metadata']
        self.header(cadence=cadence);self.queue(one(video_id=VIDEO),Result())
        self.run_helper([VIDEO]);payload=self.connection.queries[-2][1][0].obj[0]
        self.assertIsNone(payload['metadata_seconds']);self.assertEqual(payload['policy_version'],1)
        self.assertIn('coalesce(nominal.metadata_due_at,input.as_of)',_LONG_UPSERT)

    def test_empty_cadence_is_no_match_without_fabricating_rules(self):
        self.header(cadence=[]);self.queue(one(video_id=VIDEO),Result())
        self.assertEqual(self.run_helper([VIDEO]),1)
        payload=self.connection.queries[-2][1][0].obj[0]
        self.assertIsNone(payload['stats_seconds']);self.assertIsNone(payload['metadata_seconds'])
        self.assertEqual(payload['policy_version'],1)

    def test_late_typed_failure_poison_rolls_back_long_changes(self):
        self.header();self.queue(one(video_id=VIDEO),Result(execute_error=ServerFailure('42501')))
        with self.assertRaises(DatabaseServerError):self.run_helper([VIDEO])
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))

    def test_unknown_parent_commit_is_not_retried_or_acknowledged(self):
        self.header();self.queue(one(video_id=VIDEO),Result())
        self.connection.commit_error=psycopg.OperationalError('private commit detail')
        with self.assertRaises(DatabaseUnavailable) as caught:self.run_helper([VIDEO])
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(self.pool.borrowed,1)
        self.assertEqual(len(self.connection.queries),4)

    def test_borrowed_cadence_loader_never_opens_a_second_transaction(self):
        with self.database.transaction(Operation('cadence_borrow_test')) as tx:
            self.assertIs(load_cadence_snapshot_in_transaction(tx),self.database.cadence_snapshot)
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
        self.assertEqual(self.connection.queries,[])

    def test_invalid_borrowed_cadence_cannot_be_caught_to_commit_prior_write(self):
        self.database.close()
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=None,
            admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        self.queue(Result(has_columns=False,rowcount=1))
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation('cadence_borrow_test')) as tx:
                tx.execute("UPDATE public.video_collection_state SET policy_version=1 WHERE video_id=%s",(VIDEO,))
                with self.assertRaisesRegex(DatabaseError,'snapshot_required'):
                    load_cadence_snapshot_in_transaction(tx)
        self.assertEqual(caught.exception.message,'database_transaction_aborted')
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))


class NativeAgePolicyTests(unittest.TestCase):
    def test_projected_age_uses_exact_integer_thresholds_including_bc_scale(self):
        snapshot=seed_snapshot()
        for age in (0,1,86400,90*86400,10**12):
            rule=policy.select_long_policy_at_age(snapshot,'counters','public',age)
            expected=max((r for r in snapshot.rules if r.video_type=='long' and r.collection_kind=='counters'
                and r.availability_class=='public' and r.enabled and r.min_age_seconds<=age
                and (r.max_age_seconds is None or age<r.max_age_seconds)),key=lambda r:(r.policy_version,r.min_age_seconds),default=None)
            self.assertEqual(rule,expected)

    def test_existing_datetime_selector_delegates_same_rounding_and_future_clamp(self):
        snapshot=seed_snapshot()
        for seconds in (-1.5,-0.5,0.49,0.5,1.5):
            anchor=NOW-timedelta(seconds=seconds)
            age=max(0,int(seconds+0.5))
            self.assertEqual(policy.select_policy(snapshot,'long','counters','public',anchor,None,NOW),
                             policy.select_long_policy_at_age(snapshot,'counters','public',age))


if __name__=='__main__':unittest.main()
