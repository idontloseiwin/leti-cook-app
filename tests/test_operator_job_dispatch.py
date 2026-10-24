"""Offline operator advisory/exact claim driver tests; no live SQL/provider."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.base import postgres as pg
from db.jobs import operator_job_lifecycle as owner
from db.jobs.operator_jobs import OperatorJobRepository
from channel_policy_fixtures import native_policy_facts
import test_direct_pg_transactions as fixtures

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
REQUESTED = NOW-timedelta(hours=1)
CHANNEL = 'UC0000000000000000000001'


def stamp(value):
    return value.isoformat(timespec='microseconds')


def identity(number):
    return str(UUID(int=number, version=4))


def row(number, *, fair=REQUESTED, requested=REQUESTED, eligible=True, **changes):
    return dict(id=identity(number), fair_at=fair, requested_at=requested,
        eligible=eligible, channel_id=CHANNEL, accepted_epoch=17,
        job_type='channel_initial_long_inventory', phase='hydrate_videos', **changes)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.events=[]
        self.conn=fixtures.Connection(self.events)
        self.pool=fixtures.Pool(self.conn,self.events)
        self.db=pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN),
            pool_factory=Mock(return_value=self.pool), admission=lambda name:None)
        self.db.open();self.addCleanup(self.db.close)
        self.repository=OperatorJobRepository(self.db)
        self.fence_patch=patch.object(owner,'assert_channel_scope')
        self.fence=self.fence_patch.start();self.addCleanup(self.fence_patch.stop)

    def answer(self,*rows):
        self.conn.results.append(fixtures.Result(rows=rows))

    def page(self,*rows,clock=NOW):
        self.answer({'at':clock});self.answer(*rows)

    def claim(self, *, phase='hydrate_videos', epoch=17, held=True, channel=True):
        self.answer(dict(channel_id=CHANNEL,worker='worker',lease_seconds=1200))
        if not channel:
            self.answer();return None
        self.answer(dict(channel_id=CHANNEL,control_epoch=epoch,is_hidden=False,
            policy_facts=native_policy_facts('active')))
        if epoch!=17:return None
        if not held:
            self.answer();return None
        job=dict(id=identity(1),channel_id=CHANNEL,accepted_epoch=17,
            job_type='channel_initial_long_inventory',phase=phase,
            initial_video_types=['long','short','stream'],inventory_video_type='short')
        self.answer({'value':job})
        leased={**job,'last_scheduled_at':stamp(NOW),'lease_token':identity(2),'status':'running'}
        self.answer({'value':leased})
        return leased

    def test_page_returns_metadata_typed_cursor_and_native_ceiling_without_leases(self):
        self.page(row(1),row(2))
        result=self.repository.candidate_page(limit=2)
        self.assertEqual(result['next_after'],owner.OperatorCandidateCursor(stamp(REQUESTED),stamp(REQUESTED),identity(2)))
        self.assertEqual((result['ceiling'],result['inspected'],result['exhausted']),(stamp(NOW),2,False))
        self.assertEqual(set(result['candidates'][0]),{'job_id','channel_id','accepted_epoch','job_type','phase'})
        self.assertIn(('SET TRANSACTION READ ONLY',()),self.conn.setup)
        self.assertEqual((self.pool.borrowed,self.conn.commits,len(self.conn.queries)),(1,1,2))
        self.assertNotIn('FOR UPDATE',owner._FAIR_PAGE)
        self.assertNotIn('lease_token',owner._FAIR_PAGE)
        self.assertLess(owner._FAIR_PAGE.index('LIMIT %(limit)s'),owner._FAIR_PAGE.index('AS eligible'))
        self.fence.assert_not_called()

    def test_over_three_hundred_live_hidden_or_stale_rows_do_not_starve_tail(self):
        after=ceiling=None;seen=[];inspected=0
        for start in range(1,402,100):
            rows=[row(i,eligible=i>300) for i in range(start,min(start+100,402))]
            self.page(*rows)
            page=self.repository.candidate_page(limit=100,after=after,ceiling=ceiling)
            inspected+=page['inspected'];seen.extend(r['job_id'] for r in page['candidates'])
            after,ceiling=page['next_after'],page['ceiling']
        self.assertEqual(inspected,401);self.assertTrue(page['exhausted'])
        self.assertEqual(seen,[identity(i) for i in range(301,402)])
        self.assertEqual(self.pool.borrowed,5)

    def test_locked_chosen_jobs_do_not_rewind_page_or_fall_through(self):
        self.page(row(1))
        page=self.repository.candidate_page(limit=1)
        self.claim(held=False)
        self.assertIsNone(self.repository.claim_id(identity(1),CHANNEL,17,'worker'))
        self.assertEqual(self.conn.queries[-1][1]['job_id'],identity(1))
        self.page(row(2))
        tail=self.repository.candidate_page(limit=2,after=page['next_after'],ceiling=page['ceiling'])
        self.assertEqual(tail['candidates'][0]['job_id'],identity(2))
        self.assertTrue(tail['exhausted'])

    def test_equal_timestamp_uuid_order_and_scheduled_turn_order(self):
        self.page(row(2),row(3),row(1,fair=NOW))
        result=self.repository.candidate_page(limit=3)
        self.assertEqual([r['job_id'] for r in result['candidates']],[identity(2),identity(3),identity(1)])
        self.assertEqual(result['next_after'].fair_at,stamp(NOW))

    def test_fresh_wake_has_new_ceiling_empty_page_does_not_reset_old_cursor(self):
        self.page(row(1))
        first=self.repository.candidate_page()
        self.page()
        end=self.repository.candidate_page(after=first['next_after'],ceiling=first['ceiling'])
        self.assertEqual(end['next_after'],first['next_after']);self.assertTrue(end['exhausted'])
        later=NOW+timedelta(seconds=1);self.page(row(2),clock=later)
        fresh=self.repository.candidate_page()
        self.assertEqual(fresh['ceiling'],stamp(later))
        self.assertIsNone(self.conn.queries[-1][1]['after_id'])

    def test_invalid_public_page_values_never_borrow(self):
        cursor=owner.OperatorCandidateCursor(stamp(REQUESTED),stamp(REQUESTED),identity(1))
        for kwargs in (dict(limit=True),dict(limit=0),dict(limit=101),dict(limit='3'),
                       dict(after={}),dict(after=cursor),dict(ceiling='infinity'),
                       dict(ceiling=NOW),dict(ceiling=stamp(NOW).replace('+00:00','Z')),
                       dict(after=cursor,ceiling=stamp(REQUESTED-timedelta(seconds=1)))):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                self.repository.candidate_page(**kwargs)
        self.assertEqual(self.pool.borrowed,0)

    def test_cursor_rejects_noncanonical_clocks_and_identity(self):
        for values in ((stamp(REQUESTED),stamp(NOW),identity(1)),('infinity',stamp(REQUESTED),identity(1)),
                       (stamp(NOW),stamp(REQUESTED),identity(10).upper()),
                       (stamp(NOW),stamp(REQUESTED),UUID(identity(1))),
                       (stamp(NOW).replace('+00:00','+01:00'),stamp(REQUESTED),identity(1))):
            with self.subTest(values=values),self.assertRaises(ValueError):
                owner.OperatorCandidateCursor(*values)

    def test_future_ceiling_and_bad_native_clock_fail_before_page(self):
        self.answer({'at':NOW})
        with self.assertRaises(ValueError):self.repository.candidate_page(ceiling=stamp(NOW+timedelta(seconds=1)))
        self.assertEqual(len(self.conn.queries),1)
        self.answer({'at':NOW.replace(tzinfo=None)})
        with self.assertRaisesRegex(pg.DatabaseError,'clock_invalid'):self.repository.candidate_page()
        self.assertEqual(self.conn.commits,0)

    def test_bad_order_or_nonboolean_eligibility_never_advances(self):
        for rows in ((row(2),row(1)),(row(1),row(1)),(row(1,eligible=None),),(row(1,fair=NOW+timedelta(seconds=1)),)):
            self.page(*rows)
            with self.assertRaisesRegex(pg.DatabaseError,'unconfirmed'):self.repository.candidate_page()
        self.assertEqual(self.conn.commits,0)

    def test_unknown_read_commit_returns_no_page_and_does_not_repeat(self):
        self.page(row(1));self.conn.commit_error=psycopg.OperationalError(fixtures.PRIVATE)
        with self.assertRaises(pg.DatabaseError):self.repository.candidate_page()
        self.assertEqual((self.pool.borrowed,len(self.conn.queries)),(1,2))

    def test_exact_claim_stamps_turn_in_same_native_lease_update(self):
        expected=self.claim()
        actual=self.repository.claim_id(identity(1),CHANNEL,17,'worker')
        self.assertEqual(actual,expected)
        for query, expected_sql in zip(self.conn.queries,
                (owner._CLAIM_INPUT,owner._CHANNEL,owner._EXACT_JOB,owner._EXACT_LEASE)):
            self.assertIn(expected_sql,query[0])
        self.assertEqual(self.conn.queries[2][1]['expected_epoch'],17)
        self.assertIn('last_scheduled_at=native.at',owner._EXACT_LEASE)
        self.assertNotIn('last_scheduled_at',owner._LEASE)
        self.assertEqual(self.conn.commits,1)
        self.fence.assert_called_once()
        self.assertEqual(self.fence.call_args.args[1:],(CHANNEL,17))

    def test_verify_phase_retains_sixty_second_parent_lease(self):
        self.claim(phase='verify')
        self.repository.claim_id(identity(1),CHANNEL,17,'worker')
        self.assertEqual(self.conn.queries[-1][1]['lease_seconds'],60)

    def test_stale_epoch_or_locked_channel_does_not_update_or_choose_another_job(self):
        for settings in (dict(epoch=18),dict(channel=False),dict(held=False)):
            before=len(self.conn.queries);self.claim(**settings)
            self.assertIsNone(self.repository.claim_id(identity(1),CHANNEL,17,'worker'))
            self.assertFalse(any(owner._EXACT_LEASE in q[0] for q in self.conn.queries[before:]))
        self.fence.assert_not_called()

    def test_exact_claim_invalid_public_identity_or_epoch_never_borrows(self):
        values=[(identity(1),CHANNEL,True,'worker',1200),(identity(1),CHANNEL,0,'worker',1200),
                (identity(1),CHANNEL,2**63,'worker',1200),(identity(1),' '+CHANNEL,17,'worker',1200),
                (identity(1),CHANNEL,17,'',1200),(identity(1),CHANNEL,17,'worker',True),
                (identity(1),CHANNEL,17,'worker',59),(identity(1),CHANNEL,17,'worker',3601),
                ('bad',CHANNEL,17,'worker',1200)]
        for i,c,e,w,l in values:
            with self.subTest(values=(i,c,e,w,l)),self.assertRaises(ValueError):
                self.repository.claim_id(i,c,e,w,lease_seconds=l)
        self.assertEqual(self.pool.borrowed,0)

    def test_lost_final_control_fence_rolls_back_fair_stamp_with_lease(self):
        self.claim();self.fence.side_effect=pg.DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(pg.DatabaseError):self.repository.claim_id(identity(1),CHANNEL,17,'worker')
        self.assertEqual((self.conn.commits,self.conn.rollbacks),(0,1))

    def test_unknown_claim_commit_never_returns_ack_or_repeats_claim(self):
        self.claim();self.conn.commit_error=psycopg.OperationalError(fixtures.PRIVATE)
        with self.assertRaises(pg.DatabaseError) as caught:self.repository.claim_id(identity(1),CHANNEL,17,'worker')
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertNotIn(fixtures.PRIVATE,str(caught.exception))
        self.assertEqual((self.pool.borrowed,len(self.conn.queries)),(1,4))

    def test_native_lease_zero_rows_fails_without_ack(self):
        self.claim();self.conn.results[-1]=fixtures.Result()
        with self.assertRaisesRegex(pg.DatabaseError,'lease_lost'):
            self.repository.claim_id(identity(1),CHANNEL,17,'worker')
        self.assertEqual(self.conn.commits,0);self.fence.assert_not_called()

    def test_mismatched_native_identity_cannot_become_another_lease(self):
        self.claim()
        self.conn.results[-2]=fixtures.Result(rows=({'value':dict(id=identity(99),channel_id=CHANNEL,accepted_epoch=17)},))
        with self.assertRaisesRegex(pg.DatabaseError,'unconfirmed'):
            self.repository.claim_id(identity(1),CHANNEL,17,'worker')
        self.assertEqual(len(self.conn.queries),3)
        self.assertEqual(self.conn.commits,0)
        self.fence.assert_not_called()

    def test_all_new_sql_is_bounded_driver_accepted_and_parameterized(self):
        page=dict(limit=100,ceiling=stamp(NOW),after_fair=None,after_requested=None,after_id=None)
        claim=dict(job_id=identity(1),channel_id=CHANNEL,expected_epoch=17,control_epoch=17,worker='worker',lease_seconds=1200)
        for sql,params,rows,returning in ((owner._FAIR_CLOCK,(),1,False),
            (owner._FAIR_PAGE,page,100,False),(owner._EXACT_JOB,claim,1,False),(owner._EXACT_LEASE,claim,1,True)):
            pg._reviewed_statement(sql,read=True,max_rows=rows,returning=returning)
            PostgresQuery(Transformer()).convert(sql,params)


if __name__=='__main__':unittest.main()
