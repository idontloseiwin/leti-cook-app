"""Offline inventory paging and exact due driver contracts; no native SQL/provider."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).parent)]
from db.base import postgres as pg
from collection_schedule import CollectionSchedules
from db.inventory import inventory_candidates as owner, inventory_claims as claims
from db.inventory.typed_inventory import TypedInventoryRepository
from test_inventory_claims import ClaimConnection, args, one, source, TOKEN, SCAN
from test_direct_pg_transactions import Pool, Result, DSN


def channel(number):
    return 'UC'+str(number).zfill(22)


def row(number, *, eligible=True, due=True, ordinal=None, priority=0, kind='long'):
    return dict(channel_id=channel(number),video_type=kind,dispatch_ordinal=number if ordinal is None else ordinal,
                priority=priority,control_epoch=17,eligible=eligible,due=due)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.conn=ClaimConnection(self.events);self.pool=Pool(self.conn,self.events)
        self.db=pg.PostgresDatabase(pg.PostgresConfig(DSN),admission=lambda name:None,
            pool_factory=Mock(return_value=self.pool),
            cadence_snapshot=CollectionSchedules().cadence_snapshot()).open()
        self.addCleanup(self.db.close)
        self.repository=TypedInventoryRepository(self.db)
        patched=patch.object(claims,'assert_channel_scopes');self.fence=patched.start();self.addCleanup(patched.stop)

    def queue(self,*results):
        self.conn.results.extend(results)

    def page(self,*rows,ceiling=500,initial=True):
        self.queue(one(invalid=False))
        if initial:self.queue(one(ceiling=ceiling))
        self.queue(Result(rows))

    def empty_claim(self, *, only_due=True):
        values=args();values.update(claim_limit=500,channel_ids=[channel(1)],video_types=['short'],
            only_due=only_due,scopes_text='[]',scopes=[dict(channel_id=channel(1),control_epoch=17)])
        self.queue(one(**values),one(invalid=False),Result())

    def due_claim(self):
        return self.repository.claim_channels([channel(1)],video_types=['short'],worker_id='worker',run_id='stable-pass',
            expected_channel_scopes=[dict(channel_id=channel(1),control_epoch=17)],only_due=True)

    def test_page_is_read_only_metadata_and_uses_database_ceiling(self):
        self.page(row(1),row(2,due=False))
        result=self.repository.candidate_page(limit=2)
        self.assertEqual((result['ceiling'],result['inspected_count'],result['scan_exhausted']),(500,2,False))
        self.assertEqual(result['next_after'],owner.InventoryCandidateCursor(2,0,channel(2),'long'))
        self.assertEqual(result['rows'],[dict(channel_id=channel(1),video_type='long',control_epoch=17,due=True),
                                       dict(channel_id=channel(2),video_type='long',control_epoch=17,due=False)])
        self.assertIn(('SET TRANSACTION READ ONLY',()),self.conn.setup)
        self.assertEqual((self.pool.borrowed,self.conn.commits,len(self.conn.queries)),(1,1,3))
        self.assertNotIn('FOR UPDATE',owner._PAGE)
        self.assertNotIn('nextval(',owner._PAGE)
        self.assertLess(owner._PAGE.index('LIMIT %(limit)s'),owner._PAGE.index('AS eligible'))
        self.fence.assert_not_called()

    def test_hidden_or_held_prefix_over_300_scopes_advances_to_tail(self):
        after=ceiling=None;seen=[];inspected=0
        for start in range(1,402,100):
            values=[row(i,eligible=i>300) for i in range(start,min(402,start+100))]
            self.page(*values,initial=ceiling is None)
            result=self.repository.candidate_page(limit=100,after=after,ceiling=ceiling)
            after,ceiling=result['next_after'],result['ceiling']
            inspected+=result['inspected_count'];seen.extend(r['channel_id'] for r in result['rows'])
        self.assertEqual((inspected,len(seen),self.pool.borrowed),(401,101,5))
        self.assertEqual(seen,[channel(i) for i in range(301,402)])
        self.assertTrue(result['scan_exhausted'])

    def test_locked_exact_scope_returns_empty_without_rewinding_cursor_or_fallback(self):
        self.page(row(1,kind='short'))
        first=self.repository.candidate_page(limit=1)
        self.empty_claim();self.assertEqual(self.due_claim(),[])
        selected=self.conn.streams[-1].calls[0][1]
        self.assertEqual((selected['channel_ids'],selected['video_types'],selected['only_due']),([channel(1)],['short'],True))
        captured=next(params[6].obj for sql,params in self.conn.queries if claims._INPUT in sql)
        self.assertEqual(captured,[dict(channel_id=channel(1),control_epoch=17)])
        self.page(row(2),initial=False)
        tail=self.repository.candidate_page(limit=2,after=first['next_after'],ceiling=first['ceiling'])
        self.assertEqual(tail['rows'][0]['channel_id'],channel(2))
        self.assertFalse(any(claims._CLAIM in sql for sql,_ in self.conn.queries))

    def test_priority_int32_minimum_and_c_collation_tuple_order(self):
        values=[row(1,ordinal=1,priority=100,kind='short'),row(1,ordinal=1,priority=100,kind='stream'),
                row(2,ordinal=1,priority=100),row(1,ordinal=1,priority=-(2**31))]
        self.page(*values)
        result=self.repository.candidate_page(limit=4)
        self.assertEqual(result['next_after'].key,(1,2**31,channel(1),'long'))
        self.assertIn('-(priority::bigint)',owner._PAGE)
        self.assertIn('COLLATE "C"',owner._PAGE)

    def test_reused_ceiling_does_not_read_new_max_and_empty_page_retains_cursor(self):
        cursor=owner.InventoryCandidateCursor(10,0,channel(10),'long')
        self.page(initial=False)
        result=self.repository.candidate_page(after=cursor,ceiling=500)
        self.assertEqual(result['next_after'],cursor)
        self.assertEqual((result['ceiling'],result['inspected_count'],len(self.conn.queries)),(500,0,2))
        self.assertFalse(any(owner._CEILING in sql for sql,_ in self.conn.queries))
        self.page(row(600),ceiling=600)
        wake=self.repository.candidate_page()
        self.assertEqual(wake['ceiling'],600)

    def test_invalid_public_bounds_are_rejected_before_borrow(self):
        cursor=owner.InventoryCandidateCursor(1,0,channel(1),'long')
        for values in (dict(limit=True),dict(limit=0),dict(limit=101),dict(limit='1'),dict(after={}),
                dict(after=cursor),dict(after=cursor,ceiling=0),dict(ceiling=True),dict(ceiling=-1),
                dict(ceiling=2**63),dict(continuations=None),dict(continuations=[{}]*501)):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.repository.candidate_page(**values)
        self.assertEqual(self.pool.borrowed,0)

    def test_cursor_is_strict_canonical_and_immutable(self):
        valid=[1,0,channel(1),'long']
        for index,value in ((0,True),(0,0),(0,2**63),(1,True),(1,2**31),(1,-2**31-1),
                            (2,' '+channel(1)),(3,'unknown')):
            values=valid.copy();values[index]=value
            with self.subTest(values=values),self.assertRaises(ValueError):owner.InventoryCandidateCursor(*values)
        cursor=owner.InventoryCandidateCursor(*valid)
        with self.assertRaises(AttributeError):cursor.priority=5

    def test_continuation_payload_is_bounded_before_json_or_database(self):
        proof=dict(channel_id=channel(1),video_type='short',scan_id=SCAN,
                   control_epoch=17,last_transition_id=TOKEN)
        for values in ([{}],[proof|{'extra':True}],[proof|{'channel_id':'UC'+'x'*10000}],
                [proof|{'control_epoch':True}],[proof|{'scan_id':SCAN.upper()}],[proof,proof]):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.repository.candidate_page(continuations=values)
        self.assertEqual(self.pool.borrowed,0)

    def test_native_invalid_continuation_stops_before_population_read(self):
        self.queue(one(invalid=True))
        proof=dict(channel_id=channel(1),video_type='short',scan_id=SCAN,
                   control_epoch=17,last_transition_id=TOKEN)
        with self.assertRaises(ValueError):self.repository.candidate_page(continuations=[proof])
        self.assertEqual((len(self.conn.queries),self.conn.commits,self.conn.rollbacks),(1,0,1))

    def test_shared_eligibility_preserves_proof_daily_and_current_initial_guards(self):
        self.assertIn(claims._CANDIDATE_PREDICATE,owner._PAGE)
        for clause in ("sha256(convert_to(own_scan.resume_page_token,'UTF8'))",
                       "j.status<>'completed'",'own_scan.token_history_verified',
                       "coalesce(d.source_next_after,'infinity'::timestamptz)<=now()"):
            self.assertIn(clause,owner._PAGE)

    def test_corrupt_rows_or_ceiling_are_not_returned_as_advisory_authority(self):
        for values in ([row(2),row(1)],[row(1),row(1)],[row(501)],
                       [{**row(1),'eligible':None}],[{**row(1),'due':1}],
                       [{**row(1),'control_epoch':True}]):
            self.page(*values)
            with self.subTest(values=values),self.assertRaises(ValueError):self.repository.candidate_page()
        self.queue(one(invalid=False),one(ceiling=-1))
        with self.assertRaises(ValueError):self.repository.candidate_page()
        self.assertEqual(self.conn.commits,0)

    def test_read_commit_failure_returns_no_page_and_does_not_retry(self):
        self.page(row(1));self.conn.commit_error=psycopg.OperationalError('synthetic')
        with self.assertRaises(pg.DatabaseError):self.repository.candidate_page()
        self.assertEqual(self.pool.borrowed,1)

    def test_exact_due_requires_one_pair_and_captured_epoch_before_sql(self):
        base=dict(channel_ids=[channel(1)],video_types=['short'],worker_id='w',run_id='r',only_due=True,
                  expected_channel_scopes=[dict(channel_id=channel(1),control_epoch=17)])
        for change in (dict(only_due=1),dict(channel_ids=[channel(1),channel(2)]),
                dict(video_types=['short','stream']),dict(expected_channel_scopes=None),
                dict(expected_channel_scopes=[dict(channel_id=channel(1),control_epoch=True)])):
            with self.subTest(change=change),self.assertRaises(ValueError):
                claims.claim_typed_inventory_channels(self.db,**(base|change))
        self.assertEqual(self.pool.borrowed,0)

    def test_exact_due_reuses_original_lease_write_final_fence_and_run(self):
        values=args();values.update(claim_limit=500,channel_ids=[channel(1)],video_types=['short'],only_due=True,
            scopes_text='[]',scopes=[dict(channel_id=channel(1),control_epoch=17)])
        self.queue(one(**values),one(invalid=False),Result((source('short',channel(1)),)),
            one(token=TOKEN,expires='2026-10-10'),one(scan='{}',scan_kind='discovery'),
            one(channel_id=channel(1),video_type='short'))
        result=claims.claim_typed_inventory_channels(self.db,[channel(1)],['short'],'worker','stable-pass',
            expected_channel_scopes=values['scopes'],only_due=True)
        self.assertEqual(result,[dict(channel_id=channel(1),video_type='short')])
        self.assertEqual(self.conn.queries[0][1][4],'stable-pass')
        self.assertIs(self.conn.queries[0][1][7],True)
        self.fence.assert_called_once();self.assertEqual(self.fence.call_args.args[1],values['scopes'])
        self.assertEqual(sum(claims._CLAIM in sql for sql,_ in self.conn.queries),1)

    def test_due_unknown_commit_returns_no_claim_and_never_retries(self):
        self.empty_claim();self.conn.commit_error=psycopg.OperationalError('synthetic')
        with self.assertRaises(pg.DatabaseError) as caught:self.due_claim()
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(self.pool.borrowed,1)

    def test_exact_continuation_reuses_existing_owner_without_any_due_fallback(self):
        proof=dict(channel_id=channel(1),video_type='short',scan_id=SCAN,
                   control_epoch=17,last_transition_id=TOKEN)
        values=args();values.update(claim_limit=1,continuations_text='[]',include_due=False)
        self.queue(one(**values),one(invalid=False),Result())
        result=self.repository.claim_pass(continuations=[proof],limit=1,include_due=False,
            worker_id='worker',run_id='stable-pass')
        self.assertEqual(result,[])
        sent=self.conn.queries[0][1]
        self.assertEqual(sent[0],1)
        self.assertIsNone(sent[1])
        self.assertIs(sent[9],False)
        self.assertEqual(sent[8].obj,[proof])
        self.assertEqual(self.pool.borrowed,1)
        self.assertFalse(any(claims._CLAIM in sql for sql,_ in self.conn.queries))

    def test_only_due_false_default_preserves_legacy_rpc_envelope(self):
        with patch.object(self.repository,'_rpc',return_value=[]) as rpc:
            self.repository.claim_channels([channel(1)],video_types=['short'],worker_id='w',run_id='r')
        self.assertNotIn('p_only_due',rpc.call_args.args[1])

    def test_fixed_queries_bind_and_pass_reviewed_driver(self):
        self.page(row(1));self.repository.candidate_page()
        parameters=self.conn.queries[-1][1]
        for sql,params in ((owner._PAGE,parameters),(owner._CEILING,()),(claims._CONTINUATIONS,parameters)):
            pg._reviewed_statement(sql,read=True,max_rows=100)
            PostgresQuery(Transformer()).convert(sql,params)


if __name__=='__main__':unittest.main()
