"""Bounded automatic type repair, child ownership and integration ordering offline.

Recording results exercise Python control flow. Native PostgreSQL tests separately
prove the SELECT lock/admission predicates; these tests do not emulate SQL.
"""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.inventory import automatic_type_reconciliation as repair
from db.inventory import inventory_review_events as events, page_effect_batches as pages
from db.base.postgres import DatabaseError, _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb


class Transaction:
    def __init__(self, *, needed=('a','b'), queued=('a','b'), denied=(), busy=(), invalidated=()):
        self.needed=set(needed);self.queued=set(queued);self.denied=set(denied)
        self.busy=set(busy);self.invalidated=set(invalidated)
        self.calls=[];self.failed=False;self.short_return=False;self.write_error=None
    @contextmanager
    def borrow(self):
        try:yield self
        except BaseException:self.failed=True;raise
    def fetch_all(self,sql,params,*,max_rows):
        self.calls.append((sql,params,max_rows))
        assert max_rows<=50
        ids=params['ids'] if isinstance(params,dict) else params[0]
        assert len(ids)<=50
        if sql==repair._NEEDED:return [dict(video_id=v) for v in ids if v in self.needed]
        if sql==repair._ADVISORY:return [dict(video_id=v,owned=v not in self.denied) for v in ids]
        if sql==repair._QUEUE:return [dict(video_id=v) for v in ids if v in self.queued]
        if sql==repair._INVENTORY:return [dict(video_id=v) for v in ids if v not in self.busy]
        if sql==repair._CANDIDATES:
            return [dict(video_id=v,channel_id='channel',video_type='short',published_at=None,
                         published_acquired_at=None,metadata_checked_at='2026-10-15T12:00:00+00:00',
                         availability_checked_at='2026-10-15T12:00:00+00:00',hydrated=True)
                    for v in ids if v not in self.invalidated]
        raise AssertionError(sql)
    def returning_all(self,sql,params,*,max_rows):
        self.calls.append((sql,params,max_rows));assert sql==repair._INVENTORY_UPDATE
        return [] if self.short_return else [dict(video_id=row['video_id']) for row in params[0].obj]
    def execute(self,sql,params):
        self.calls.append((sql,params,None));assert sql==repair._QUEUE_UPDATE
        if self.write_error:raise self.write_error
        return len(params[0].obj)


class RepairTests(unittest.TestCase):
    def setUp(self):
        init=patch('db.videos.video_queue_initialization.ensure_video_collection_state_in_transaction',return_value=0)
        review=patch('db.inventory.review_effect_batches.refresh_review_cases_batch')
        self.initialize=init.start();self.review=review.start()
        self.addCleanup(init.stop);self.addCleanup(review.stop)
    def test_empty_and_invalid_bound_have_no_sql(self):
        tx=Transaction()
        self.assertEqual(repair.reconcile_automatic_video_types(tx,[]),[])
        for ids in ([''],[str(i) for i in range(1001)]):
            with self.assertRaises(ValueError):repair.reconcile_automatic_video_types(tx,ids)
        self.assertEqual(tx.calls,[])
    def test_thousand_ids_use_twenty_bounded_candidate_pages_without_effects(self):
        tx=Transaction(needed=())
        ids=[f'v{i:010d}' for i in range(1000)]
        self.assertEqual(repair.reconcile_automatic_video_types(tx,ids+ids[:2]),[])
        self.assertEqual(len(tx.calls),20)
        self.assertTrue(all(sql==repair._NEEDED and len(params[0])==50 for sql,params,_ in tx.calls))
        self.initialize.assert_not_called();self.review.assert_not_called()
    def test_revalidates_after_queue_and_inventory_locks_before_any_write(self):
        tx=Transaction(invalidated=('a','b'))
        self.assertEqual(repair.reconcile_automatic_video_types(tx,['b','a']),[])
        self.assertEqual([sql for sql,_,_ in tx.calls],
                         [repair._NEEDED,repair._ADVISORY,repair._QUEUE,repair._INVENTORY,repair._CANDIDATES])
        self.review.assert_not_called()
    def test_busy_advisory_and_live_or_unlocked_queue_results_never_mutate(self):
        for kwargs in (dict(denied=('a',)),dict(busy=('a',))):
            tx=Transaction(needed=('a',),**kwargs)
            self.assertEqual(repair.reconcile_automatic_video_types(tx,['a']),[])
            self.assertFalse(any(sql in (repair._INVENTORY_UPDATE,repair._QUEUE_UPDATE) for sql,_,_ in tx.calls))
        self.assertIn('FOR UPDATE SKIP LOCKED',repair._QUEUE)
        self.assertIn('NOT(q.video_id=ANY(%(queue_ids)s::text[]))',repair._INVENTORY)
        self.assertIn('q.lease_token IS NOT NULL AND q.lease_expires_at>clock_timestamp()',repair._INVENTORY)
        self.assertIn('FOR UPDATE OF i SKIP LOCKED',repair._INVENTORY)
    def test_one_locked_result_excluded_after_second_candidate_check(self):
        tx=Transaction(invalidated=('b',))
        self.assertEqual(repair.reconcile_automatic_video_types(tx,['b','a','a']),['a'])
        self.review.assert_called_once_with(tx,['a']);self.initialize.assert_not_called()
        writes=[params[0].obj for sql,params,_ in tx.calls if sql in (repair._INVENTORY_UPDATE,repair._QUEUE_UPDATE)]
        self.assertEqual([[r['video_id'] for r in rows] for rows in writes],[['a'],['a']])
    def test_missing_queue_uses_existing_initializer_after_acknowledged_inventory_write(self):
        tx=Transaction(queued=('a',));order=[]
        self.initialize.side_effect=lambda borrowed,ids:order.append(('initialize',ids,tx.calls[-1][0]))
        self.review.side_effect=lambda borrowed,ids:order.append(('review',ids,tx.calls[-1][0]))
        self.assertEqual(repair.reconcile_automatic_video_types(tx,['b','a']),['a','b'])
        self.assertEqual(order,[('initialize',['b'],repair._QUEUE_UPDATE),('review',['a','b'],repair._QUEUE_UPDATE)])
    def test_missing_inventory_ack_or_unknown_queue_write_aborts_without_retry(self):
        tx=Transaction();tx.short_return=True
        with self.assertRaisesRegex(DatabaseError,'automatic_type_reconciliation_changed'):
            repair.reconcile_automatic_video_types(tx,['a'])
        self.assertTrue(tx.failed);self.assertNotIn(repair._QUEUE_UPDATE,[sql for sql,_,_ in tx.calls])
        tx=Transaction();error=ConnectionError('unknown write outcome');tx.write_error=error
        with self.assertRaises(ConnectionError) as caught:repair.reconcile_automatic_video_types(tx,['a'])
        self.assertIs(caught.exception,error);self.assertTrue(tx.failed)
        self.assertEqual(sum(sql==repair._QUEUE_UPDATE for sql,_,_ in tx.calls),1)
        self.review.assert_not_called();self.initialize.assert_not_called()
    def test_episode_calendar_and_observation_are_preserved_without_manual_decision(self):
        sql=repair._QUEUE_UPDATE
        for field in ('availability_first_missing_at','availability_schedule_type','availability_resume_observed_at','stats_source_checked_at','metadata_source_checked_at'):
            self.assertNotIn(field+'=',sql)
        self.assertIn("q.availability_first_missing_at IS NOT NULL THEN 'unavailable'",sql)
        self.assertIn('coalesce(q.typed_collection_checked_at,p.availability_checked_at)',sql)
        self.assertIn('THEN q.next_due_at',sql);self.assertIn('THEN q.metadata_due_at',sql)
        self.assertIn("q.paused_reason='availability_retry_expired'",sql)
        self.assertNotIn('decision_type',repair._INVENTORY_UPDATE+sql)
        self.assertNotIn('video_snapshots',repair._INVENTORY_UPDATE+sql)
        self.assertIn('public.video_type_automatic_candidates',repair._CANDIDATES)
        self.assertIn('m.video_type=c.automatic_type',repair._CANDIDATES)
        self.assertIn('q.hydration_needed IS FALSE AND q.stats_due_at IS NOT NULL',repair._CANDIDATES)
    def test_fixed_statements_pass_driver_and_binding(self):
        for sql in (repair._NEEDED,repair._ADVISORY,repair._QUEUE,repair._CANDIDATES):
            _reviewed_statement(sql,read=True,max_rows=50)
            PostgresQuery(Transformer()).convert(sql,(['a'],))
        PostgresQuery(Transformer()).convert(repair._INVENTORY,dict(ids=['a'],queue_ids=['a']))
        _reviewed_statement(repair._INVENTORY_UPDATE,read=True,returning=True,max_rows=50)
        _reviewed_statement(repair._QUEUE_UPDATE,read=False,max_rows=0)
        for sql in (repair._INVENTORY_UPDATE,repair._QUEUE_UPDATE):
            PostgresQuery(Transformer()).convert(sql,(Jsonb([]),))


class MembershipHookTests(unittest.TestCase):
    def test_single_positive_membership_reconciles_before_queue_initialization_and_review(self):
        tx=Mock();tx.borrow.side_effect=lambda:nullcontext(tx)
        tx.fetch_one.side_effect=lambda sql,params:dict(video_id='v',old_video_id='v',withdrawal=False,identity_changed=False) if sql==events._MEMBER_FACTS else {'eligible':False}
        order=[]
        with patch.object(repair,'reconcile_automatic_video_types',side_effect=lambda t,ids:order.append(('automatic',ids))),patch('db.videos.video_queue_initialization.ensure_typed_collection_state_in_transaction',side_effect=lambda t,ids:order.append(('queue',ids))),patch.object(events,'refresh_review_case',side_effect=lambda t,key,**kw:order.append(('review',key))):
            events.after_membership_source_write(tx,operation='update',old_text='old',new_text='new')
        self.assertEqual(order,[('automatic',['v']),('queue',['v']),('review','v')])
    def test_deleted_or_withdrawn_membership_never_runs_automatic_policy(self):
        for operation in ('delete','update'):
            tx=Mock();tx.borrow.side_effect=lambda:nullcontext(tx)
            tx.fetch_one.return_value=dict(video_id='v',old_video_id='v',withdrawal=True,identity_changed=False)
            with patch.object(repair,'reconcile_automatic_video_types') as automatic,patch.object(events,'refresh_review_case') as review:
                events.after_membership_source_write(tx,operation=operation,old_text='old',new_text='new' if operation=='update' else None)
            automatic.assert_not_called();review.assert_called_once_with(tx,'v',source_removed=True)
    def test_page_uses_only_normal_receipts_and_reconciles_before_outer_review(self):
        from test_page_effect_batches import Tx,member,event
        tx=Tx([[member('withdrawn',insert=False,withdrawal=True),member('normal',2,insert=False)]])
        order=[]
        with patch.object(repair,'reconcile_automatic_video_types',side_effect=lambda t,ids:order.append(('automatic',ids))),patch.object(pages,'refresh_review_cases_batch',side_effect=lambda t,ids:order.append(('review',ids))),patch.object(pages,'withdraw_review_cases_batch'),patch.object(pages,'ensure_typed_collection_state_in_transaction',side_effect=lambda t,ids:order.append(('queue',ids))):
            pages.after_page_membership_changes(tx,[event(False),event(False)])
        self.assertEqual(order,[('automatic',['normal']),('queue',['normal']),('review',['normal'])])
        self.assertEqual(tx.calls[-1][1],pages._RESUME_RETURNED)


if __name__=='__main__':unittest.main()
