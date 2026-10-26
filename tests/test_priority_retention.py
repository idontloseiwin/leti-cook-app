"""RR15 fixed public capability; recording PostgreSQL transactions, no DB."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.base.postgres import DatabaseError, DatabaseServerError, PostgresConfig, PostgresDatabase, _reviewed_statement
from db.hot.hot_retention import HotRetentionRepository, PriorityReference, RetentionError
from db.retention import inventory_retention as inventory_owner, priority_retention as priority_owner
from db.inventory import inventory_review_events
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN

AT='2026-10-06T12:00:00+00:00'
FLOOR='2026-09-07T12:00:00+00:00'
OLD='2026-09-01T12:00:00+00:00'
VIDEO='aaaaaaaaaaa'
ORIGINAL='UC0000000000000000000001'
CURRENT='UC0000000000000000000002'


def one(**values):return Result(rows=(values,))
def count(n=1):return Result(has_columns=False,rowcount=n)
def ref(target,*key):return PriorityReference(target,tuple(key or (VIDEO,)))


class PublicPriorityTests(unittest.TestCase):
    def setUp(self):
        self.events=[]
        self.connection=Connection(self.events)
        self.pool=Pool(self.connection,self.events)
        self.gates=[]
        self.database=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda config:self.pool,
                                       admission=self.gates.append).open()
        self.repository=HotRetentionRepository(self.database)
        self.addCleanup(self.database.close)

    def queue(self,*values):self.connection.results.extend(values)

    def start(self):self.queue(one(at=AT))

    def source_header(self,target='video_counters'):
        self.queue(one(target=target,after_id=None,limit=1,at=AT,cutoff=FLOOR))

    def inventory_header(self,target='inventory'):
        self.queue(one(target=target,cursor=None,cursor_length=None,limit=1,at=AT,cutoff=FLOOR))

    def source_candidate(self):
        self.queue(Result(rows=(dict(id=VIDEO,source_key=OLD,invalid_clock=False,cursor=VIDEO),)),one(ord=1))

    def lease(self,**values):
        return one(**dict(dict(video_id=VIDEO,present=True,has_token=False,expires_at=None),**values))

    def inventory_candidate(self,target='inventory'):
        self.inventory_header(target)
        self.queue(Result(rows=(dict(video_id=VIDEO,channel_id=ORIGINAL,video_type='long',cursor=VIDEO,noop=False),)),one(noop=False))

    def owners(self,channel=ORIGINAL):
        self.queue(one(acquired=True),one(channel_id=channel),Result(),Result(),
                   one(channel_id=channel,value='{}'),one(at=AT),Result())

    def test_missing_exact_source_resolves_with_one_pool_borrow_and_no_global_scan(self):
        self.start();self.source_header();self.queue(Result())
        result=self.repository.cleanup_priority((ref('video_counters'),))
        self.assertTrue(result.complete);self.assertEqual(result.remaining,())
        self.assertEqual(self.pool.borrowed,1);self.assertEqual(self.connection.commits,1)
        sql,params=self.connection.queries[-1]
        self.assertIn('video_id=ANY(%(ids)s::text[])',sql)
        self.assertEqual(params['ids'],[VIDEO])
        self.assertFalse(any('FOR UPDATE' in sql for sql,p in self.connection.queries))
        self.assertEqual(set(self.gates),{'cleanup_hot_priority'})
        setup=next(p for sql,p in self.connection.setup if "set_config('search_path'" in sql)
        self.assertEqual(setup,('5000ms','250ms'))

    def test_live_queue_lease_remains_exact_cause_and_source_is_not_touched(self):
        requested=(ref('video_counters'),)
        self.start();self.source_header();self.source_candidate()
        self.queue(self.lease(has_token=True,expires_at=AT),one(video_id=VIDEO,active=True),one(value=OLD))
        result=self.repository.cleanup_priority(requested)
        self.assertEqual(result.remaining,requested)
        self.assertFalse(any(sql.startswith(('UPDATE','DELETE')) for sql,p in self.connection.queries))
        queries=[sql for sql,p in self.connection.queries]
        lock=next(i for i,sql in enumerate(queries) if 'FOR UPDATE SKIP LOCKED' in sql)
        self.assertIn('clock_timestamp()',queries[lock+1])
        self.assertFalse(any('FROM public.videos t' in sql and 'FOR UPDATE' in sql for sql in queries))

    def test_post_lock_source_refresh_wins_and_clocks_stay_unchanged(self):
        self.start();self.source_header();self.source_candidate()
        self.queue(self.lease(),one(id=VIDEO),count(0))
        result=self.repository.cleanup_priority((ref('video_counters'),))
        self.assertEqual(result.remaining,());self.assertEqual(result.scrubbed,0)
        # The one scrub rechecks the locked row's clock; a refresh in between wins.
        writes=[sql for sql,p in self.connection.queries if sql.startswith(('UPDATE','DELETE'))]
        self.assertEqual(len(writes),1);self.assertIn('t.checked_at<%(cutoff)s::timestamptz',writes[0])
        locks=[sql for sql,p in self.connection.queries if 'FOR UPDATE' in sql]
        self.assertIn('video_collection_state',locks[0]);self.assertIn('public.videos',locks[1])

    def test_source_children_precede_inventory_and_only_payload_fields_are_scrubbed(self):
        self.start();self.source_header();self.source_candidate()
        self.queue(self.lease(),one(id=VIDEO),count())
        self.inventory_header();self.queue(Result())
        requested=(ref('inventory'),ref('video_counters'))
        result=self.repository.cleanup_priority(requested)
        self.assertEqual(result.checked,requested);self.assertEqual(result.remaining,())
        self.assertEqual(result.scrubbed,1);self.assertEqual(self.pool.borrowed,1)
        queries=[sql for sql,p in self.connection.queries]
        write=next(i for i,sql in enumerate(queries) if sql.startswith('UPDATE'))
        root=next(i for i,sql in enumerate(queries) if 'FROM public.video_inventory iv WHERE iv.video_id=ANY' in sql)
        self.assertLess(write,root)
        self.assertIn('views=NULL,like_count=NULL,comment_count=NULL',queries[write])
        self.assertNotIn('checked_at=',queries[write].split(' WHERE ')[0])

    def test_membership_retry_locks_original_and_current_owner_and_keeps_native_effect(self):
        requested=(ref('memberships',VIDEO,ORIGINAL,'long'),)
        self.start();self.inventory_candidate('memberships');self.owners(CURRENT)
        self.queue(one(value='NATIVE_MEMBER_OLD'),one(source_valid=False,first_valid=False,positive_valid=False),
                   one(value='NATIVE_MEMBER_DELETED'),
                   one(video_id=None,old_video_id=VIDEO,withdrawal=None,identity_changed=None),Result())
        with patch.object(inventory_owner,'after_membership_source_write',
                          wraps=inventory_review_events.after_membership_source_write) as effect, \
             patch.object(inventory_review_events,'refresh_review_case') as review:
            result=self.repository.cleanup_priority(requested)
        review.assert_called_once()
        self.assertEqual(review.call_args.args[1],VIDEO)
        self.assertEqual(review.call_args.kwargs,dict(source_removed=True))
        self.assertEqual(result.remaining,());self.assertEqual(result.deleted,1)
        effect.assert_called_once()
        self.assertEqual(effect.call_args.kwargs,dict(operation='delete',old_text='NATIVE_MEMBER_DELETED'))
        locks=[(sql,p) for sql,p in self.connection.queries if 'FOR UPDATE' in sql]
        self.assertEqual(len(locks),5)
        for (sql,p),name in zip(locks,('channel_playlist_state','video_collection_state','video_inventory iv',
                                      'video_type_review_cases','video_inventory_memberships')):
            self.assertIn(name,sql);self.assertIn('NOWAIT',sql)
        self.assertEqual(locks[0][1]['channel_id'],ORIGINAL)
        self.assertEqual(locks[0][1]['observed_channel'],CURRENT)
        self.assertIn('ORDER BY s.channel_id,s.video_type',locks[0][0])
        self.assertEqual(self.pool.borrowed,1)

    def test_inventory_children_are_not_cascaded_and_keep_exact_root_pending(self):
        requested=(ref('inventory'),)
        self.start();self.inventory_candidate();self.owners()
        self.queue(one(value=True),one(value=True),one(value=True),one(value=False),one(present=True))
        result=self.repository.cleanup_priority(requested)
        self.assertEqual(result.remaining,requested)
        self.assertFalse(any(sql.startswith('DELETE') for sql,p in self.connection.queries))

    def test_late_nowait_rolls_back_candidate_and_keeps_reference(self):
        requested=(ref('inventory'),)
        self.start();self.inventory_candidate();self.owners()
        self.queue(one(value=False),count(),one(value=True),one(value=True),one(value=False),
                   one(present=False),Result(execute_error=ServerFailure('55P03')))
        result=self.repository.cleanup_priority(requested)
        self.assertEqual(result.remaining,requested);self.assertEqual(result.scrubbed,0)
        self.assertEqual(self.connection.rollbacks,1);self.assertEqual(self.connection.commits,1)
        self.assertEqual(self.pool.borrowed,1)

    def test_unknown_commit_never_replays_or_produces_a_clear_receipt(self):
        self.start();self.source_header();self.queue(Result())
        self.connection.commit_error=psycopg.OperationalError('private transport detail')
        with self.assertRaisesRegex(RetentionError,'^cleanup_outcome_unknown$'):
            self.repository.cleanup_priority((ref('video_counters'),))
        self.assertEqual(self.pool.borrowed,1);self.assertEqual(len(self.connection.queries),3)

    def test_invalid_and_oversized_identity_sets_dispatch_no_data_statement(self):
        for bad in ([],[dict(target='deleted_channels',key=[VIDEO])],
                    [dict(target='memberships',key=[VIDEO,ORIGINAL,'bad'])],
                    [ref('inventory').wire()]*2,
                    [ref('inventory',str(i)).wire() for i in range(101)]):
            with self.subTest(value_type=type(bad)),self.assertRaises(DatabaseServerError) as error:
                self.database.cleanup_hot_priority(references=bad)
            self.assertEqual(error.exception.message,'hot_priority_references_invalid')
        self.assertEqual(self.connection.queries,[]);self.assertEqual(self.connection.commits,0)

    def test_incomplete_owner_proof_aborts_transaction_before_ack(self):
        self.start()
        raw=dict(as_of=AT,cutoff=FLOOR,scanned=1,deleted=0,scrubbed=0,deferred=1,complete=True,
                 next_cursor=None,unknown_expiry_remaining=1,priority_complete=False)
        with patch.object(priority_owner,'cleanup_hot_current_source',return_value=raw):
            with self.assertRaises(RetentionError):self.repository.cleanup_priority((ref('video_counters'),))
        self.assertEqual(self.connection.commits,0);self.assertEqual(self.connection.rollbacks,1)

    def test_unrequested_owner_reference_aborts_transaction_before_ack(self):
        self.start()
        raw=dict(as_of=AT,cutoff=FLOOR,scanned=1,deleted=0,scrubbed=0,deferred=1,complete=True,
                 next_cursor=None,unknown_expiry_remaining=1,priority_complete=True,
                 priority_references=[ref('video_counters','different').wire()])
        with patch.object(priority_owner,'cleanup_hot_current_source',return_value=raw):
            with self.assertRaises(RetentionError):self.repository.cleanup_priority((ref('video_counters'),))
        self.assertEqual(self.connection.commits,0);self.assertEqual(self.connection.rollbacks,1)

    def test_all_supported_source_targets_use_bound_exact_queries(self):
        for target in ('video_counters','video_metadata','video_availability','channel_profile','channel_country'):
            self.start();self.source_header(target);self.queue(Result())
            self.repository.cleanup_priority((ref(target),))
            sql,p=self.connection.queries[-1]
            self.assertIn('=ANY(%(ids)s::text[])',sql)
            _reviewed_statement(sql,read=True,max_rows=1)
            PostgresQuery(Transformer()).convert(sql,p)
        for sql,p in ((inventory_owner._INVENTORY_REFERENCES,dict(ids=[VIDEO])),
                      (inventory_owner._MEMBERSHIP_REFERENCES,dict(identities=Jsonb([
                          dict(video_id=VIDEO,channel_id=ORIGINAL,video_type='long')])))):
            _reviewed_statement(sql,read=True,max_rows=100)
            PostgresQuery(Transformer()).convert(sql,p)


if __name__=='__main__':unittest.main()
