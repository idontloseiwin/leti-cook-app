"""Shared bounded scheduled-claim writer: ordering, policies and atomic failure."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from db.videos import video_scheduled_claims as subject
from db.base.postgres import DatabaseError, _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
import test_video_scheduled_claims as support
Tx=support.Tx


class BatchClaimsTests(unittest.TestCase):
    setUp=support.ClaimsTests.setUp
    rows=support.ClaimsTests.rows
    invoke=support.ClaimsTests.invoke
    def writes(self,sql):return [event[1] for event in self.tx.events if event[0]==sql]

    def test_fifty_long_uses_two_fact_reads_one_lease_write_leaves_prefetched_tail(self):
        self.rows(*(['long']*80))
        self.tx.reverse_returned=True
        result=self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']],[str(i) for i in range(50)])
        self.assertEqual([len(row['video_ids']) for row in self.writes(subject._LONG_FACTS)],[10,40])
        self.assertEqual([len(row['video_ids']) for row in self.writes(subject._CLAIM)],[50])
        self.assertEqual(self.tx.prefixes,[[str(i) for i in range(10)],[str(i) for i in range(10,50)]])
        self.assertEqual(self.tx.fetched,[str(i) for i in range(60)])
        self.assertEqual(len(self.tx.events),8)  # args/window, 3 batch statements, open/close/fence
        self.assertEqual(self.tx.rows[0]['video_type'],'long')

    def test_common_writer_handles_long_short_stream_unknown_in_one_prefix(self):
        self.rows('stream',None,'long','short','long','stream',None,'short')
        self.tx.reverse_returned=True
        self.tx.mode={'0':'stream_lifecycle','2':'hydrate','3':'hydrate','4':'metadata'}
        result=self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']],self.tx.ids)
        self.assertEqual([row['collection_mode'] for row in result['rows']],
            ['stream_lifecycle','identity_refresh','hydrate','hydrate','metadata','metadata','identity_refresh','metadata'])
        self.assertEqual(len(self.writes(subject._CLAIM)),1)
        self.assertEqual(self.writes(subject._CLAIM)[0]['video_types'],['stream',None,'long','short','long','stream',None,'short'])
        self.assertEqual(self.writes(subject._LONG_FACTS)[0]['video_ids'],['2','4'])
        self.assertEqual(self.writes(subject._TYPED_FACTS)[0]['video_ids'],['0','3','5','7'])
        self.assertEqual(self.writes(subject._RECOVERY_FACTS)[0]['video_ids'],['1','6'])
        self.assertIsNone(self.tx.rows[1]['video_type'])

    def test_legacy_typed_fifty_uses_same_writer_and_reorders_modes(self):
        for kind in ('short','stream'):
            with self.subTest(kind=kind):
                self.tx=Tx();self.rows(*([kind]*60));self.tx.reverse_returned=True
                result=self.invoke(kind)
                self.assertEqual([row['video_id'] for row in result['rows']],[str(i) for i in range(50)])
                self.assertTrue(all(row['priority']==0 for row in result['rows']))
                self.assertEqual([len(params['video_ids']) for params in self.writes(subject._TYPED_FACTS)],[10,40])
                for sql in (subject._CLAIM,):
                    self.assertEqual([len(params['video_ids']) for params in self.writes(sql)],[50])
                self.assertEqual(self.writes(subject._LONG_FACTS),[])

    def test_pause_and_unknown_skip_at_fetch_boundaries_fill_without_touching_tail(self):
        self.rows(*(['long']*80))
        for index in (9,10,48,49,50,59):self.tx.eligible[str(index)]=False
        self.tx.rows[10]['video_type']=None
        self.tx.rows[59]['video_type']='short'
        result=self.invoke()
        rejected={'9','10','48','49','50'}
        expected=[str(i) for i in range(55) if str(i) not in rejected]
        self.assertEqual([row['video_id'] for row in result['rows']],expected)
        self.assertEqual(result['paused_count'],4)
        self.assertEqual([len(params['video_ids']) for params in self.writes(subject._CLAIM)],[50])
        touched={identity for event in self.tx.events if isinstance(event[-1],dict)
                 for identity in event[-1].get('video_ids',[])}
        self.assertEqual(touched,{str(i) for i in range(55)})
        self.assertEqual(self.tx.fetched,[str(i) for i in range(60)])

    def test_individual_handoff_overrides_channel_pause_but_not_identity(self):
        self.rows('long','long','long');self.tx.pause['channel']='channel_inventory_not_ready'
        self.tx.handoff={'0','1'};self.tx.eligible['1']=False
        result=self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']],['0'])
        self.assertEqual(self.writes(subject._PAUSE)[0]['pause_reasons'],['inventory_not_long','channel_inventory_not_ready'])
        self.assertEqual(result['paused_count'],2)

    def test_missing_channel_facts_preserve_old_null_pause_before_final_fence(self):
        self.rows('long')
        original=self.tx.fetch_all
        def missing(sql,params,*,max_rows):
            rows=original(sql,params,max_rows=max_rows)
            if sql==subject._LONG_FACTS:rows[0]['policy_facts']=None
            return rows
        self.tx.fetch_all=missing
        with patch.object(subject,'assert_channel_scopes',side_effect=DatabaseError('channel_control_stale')):
            with self.assertRaises(DatabaseError):self.invoke()
        self.assertTrue(self.tx.failed)
        self.assertEqual(len(self.writes(subject._CLAIM)),1)
        self.assertEqual(self.writes(subject._PAUSE),[])

    def test_missing_duplicate_foreign_or_moved_returning_aborts_without_fence(self):
        for change in ('missing','duplicate','foreign','channel','type'):
            with self.subTest(change=change):
                self.tx=Tx();self.rows('long','short');self.fences.clear()
                original=self.tx.returning_all
                def changed(sql,params,*,max_rows):
                    rows=original(sql,params,max_rows=max_rows)
                    if sql==subject._CLAIM:
                        if change=='missing':rows.pop()
                        elif change=='duplicate':rows[-1]=rows[0]
                        elif change=='foreign':rows[0]['value']['video_id']='foreign'
                        elif change=='channel':rows[0]['value']['channel_id']='foreign'
                        else:rows[0]['value']['video_type']='stream'
                    return rows
                self.tx.returning_all=changed
                with self.assertRaises(DatabaseError):self.invoke()
                self.assertTrue(self.tx.failed);self.assertEqual(self.fences,[])
                self.assertEqual(len(self.writes(subject._CLAIM)),1)

    def test_missing_fact_or_pause_returning_aborts_without_lease(self):
        for target in ('facts','pause'):
            with self.subTest(target=target):
                self.tx=Tx();self.rows('long','long');self.tx.eligible={'0':False,'1':False}
                original=self.tx.fetch_all if target=='facts' else self.tx.returning_all
                def changed(sql,params,*,max_rows):
                    rows=original(sql,params,max_rows=max_rows)
                    rows.pop()
                    return rows
                if target=='facts':self.tx.fetch_all=changed
                else:self.tx.returning_all=changed
                with self.assertRaises(DatabaseError):self.invoke()
                self.assertTrue(self.tx.failed);self.assertEqual(self.writes(subject._CLAIM),[])

    def test_second_prefix_failure_prevents_lease_write_and_final_fence(self):
        self.rows(*(['long']*60));original=self.tx.fetch_all
        def fail_second(sql,params,*,max_rows):
            if sql==subject._LONG_FACTS and params['video_ids'][0]=='10':
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
            return original(sql,params,max_rows=max_rows)
        self.tx.fetch_all=fail_second
        with self.assertRaises(DatabaseError) as error:self.invoke()
        self.assertTrue(error.exception.outcome_unknown);self.assertTrue(self.tx.failed)
        self.assertEqual(self.fences,[])
        self.assertEqual(self.writes(subject._CLAIM),[])

    def test_bulk_sql_uses_reviewed_shape_and_bound_arrays(self):
        states=[dict(video_id='video',channel_id='channel',video_type='long')]
        params=dict(subject._batch_parameters(states),worker='worker',run_id='00000000-0000-4000-8000-000000000001',
                    lease_seconds=90,pause_reasons=['inventory_not_long'])
        for sql in (subject._LONG_FACTS,subject._TYPED_FACTS,subject._RECOVERY_FACTS,subject._CLAIM,subject._PAUSE):
            with self.subTest(sql=sql[:40]):
                returning=sql in (subject._CLAIM,subject._PAUSE)
                wrapped=_reviewed_statement(sql,read=True,max_rows=1,returning=returning)
                query=PostgresQuery(Transformer());query.convert(wrapped,params)
                self.assertEqual(len(query.params),6 if sql==subject._CLAIM else 4 if sql==subject._PAUSE else 3)
                self.assertNotIn(b'%s',query.query)
                if returning:self.assertIn(b'IS NOT DISTINCT FROM input.video_type',query.query)




class OperationBatchClaimsTests(unittest.TestCase):
    """Operation packing reuses the real claim writer, with a locked-row double."""
    def setUp(self):
        from test_auto_scoped_claims import ScopedTx, CHANNEL, OTHER
        self.tx = ScopedTx()
        self.channels = (CHANNEL, OTHER)
        self.fences = []
        def fence(tx, scopes):
            self.assertFalse(tx.cursor)
            self.fences.append(scopes)
        guard = patch.object(subject, 'assert_channel_scopes', side_effect=fence)
        guard.start()
        self.addCleanup(guard.stop)

    def seed(self, modes, kinds=None):
        kinds = kinds or ['long'] * len(modes)
        self.tx.rows = [dict(video_id=str(index), channel_id=self.channels[index % 2],
                            video_type=kinds[index]) for index in range(len(modes))]
        self.tx.ids = [row['video_id'] for row in self.tx.rows]
        self.tx.mode = dict(zip(self.tx.ids, modes))

    def invoke(self, limit=50):
        from test_video_scheduled_claims import RUN
        scopes = list({(row['channel_id'], row['video_type'] or 'unknown'): None
                       for row in self.tx.rows})
        return subject.claim_due_video_collection_batch(
            None, limit, 'worker', RUN, transaction=self.tx,
            scopes=[dict(channel_id=channel, video_type=kind) for channel, kind in scopes])

    def writes(self, sql):
        return [event[1] for event in self.tx.events if event[0] == sql]

    def test_metadata_and_counters_are_packed_without_mutating_skipped_operation(self):
        self.seed(['metadata', 'counters'] * 50)
        result = self.invoke()
        expected = [str(index) for index in range(0, 100, 2)]
        self.assertEqual([row['video_id'] for row in result['rows']], expected)
        self.assertEqual(result['inspected_count'], 100)
        self.assertFalse(result['scan_exhausted'])
        self.assertEqual(result['paused_count'], 0)
        changed = [identity for params in self.writes(subject._CLAIM) for identity in params['video_ids']]
        self.assertEqual(changed, expected)
        self.assertEqual(self.writes(subject._PAUSE), [])
        self.assertEqual(len(self.fences), 1)

    def test_counters_oldest_anchor_does_not_pull_metadata_into_the_batch(self):
        self.seed(['counters', 'metadata'] * 40)
        result = self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']],
                         [str(index) for index in range(0, 80, 2)])
        self.assertEqual({row['collection_mode'] for row in result['rows']}, {'counters'})
        self.assertFalse(result['scan_exhausted'])
    def test_mixed_typed_counter_modes_pack_into_the_same_stats_batch(self):
        self.seed(['counters']*6,['long','short','stream']*2)
        result=self.invoke()
        self.assertEqual([row['video_type'] for row in result['rows']],['long','short','stream']*2)
        self.assertEqual({row['collection_mode'] for row in result['rows']},{'counters'})
        self.assertEqual(len(self.writes(subject._CLAIM)),1)
    def test_lost_proof_during_stats_selection_promotes_general_without_lease_or_attempt(self):
        from test_video_scheduled_claims import RUN
        self.seed(['counters'],['short'])
        self.tx.eligible['0']=False;self.tx.recovery['0']=True
        scopes=[dict(channel_id=self.channels[0],video_type='short')]
        result=subject.claim_due_video_collection_batch(None,50,'worker',RUN,
            transaction=self.tx,scopes=scopes,operation='counters')
        self.assertEqual(result['rows'],[])
        self.assertFalse(result['scan_exhausted'])
        self.assertEqual(self.writes(subject._CLAIM),[])
        self.assertEqual(self.writes(subject._PAUSE),[])
        self.assertEqual(self.writes(subject._PROMOTE_METADATA)[0]['video_ids'],['0'])
        self.assertNotIn('attempt_count',subject._PROMOTE_METADATA)
        self.assertNotIn('next_due_at=',subject._PROMOTE_METADATA)
        result=subject.claim_due_video_collection_batch(None,50,'worker',RUN,
            transaction=self.tx,scopes=scopes,operation='metadata')
        self.assertEqual([(row['video_type'],row['collection_mode']) for row in result['rows']],
                         [('short','identity_refresh')])

    def test_ineligible_oldest_row_cannot_choose_operation_for_eligible_work(self):
        self.seed(['metadata', 'counters', 'metadata', 'counters'])
        self.tx.eligible['0'] = False
        result = self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']], ['1', '3'])
        self.assertEqual(result['paused_count'], 1)
        self.assertEqual(self.writes(subject._PAUSE)[0]['video_ids'], ['0'])
        self.assertFalse(result['scan_exhausted'])

    def test_unknown_and_all_typed_metadata_share_the_general_operation(self):
        self.seed(['identity_refresh', 'metadata', 'stream_lifecycle', 'hydrate', 'counters'],
                  [None, 'short', 'stream', 'long', 'long'])
        result = self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']], ['0', '1', '2', '3'])
        self.assertEqual([row['collection_mode'] for row in result['rows']],
                         ['identity_refresh', 'metadata', 'stream_lifecycle', 'hydrate'])
        self.assertFalse(result['scan_exhausted'])

    def test_one_and_33_due_rows_return_immediately_without_waiting_for_fifty(self):
        for size in (1, 33):
            with self.subTest(size=size):
                from test_auto_scoped_claims import ScopedTx
                self.tx = ScopedTx()
                self.seed(['metadata'] * size)
                result = self.invoke()
                self.assertEqual(len(result['rows']), size)
                self.assertTrue(result['scan_exhausted'])
                self.assertEqual(sum(event[0] == 'cursor_open' for event in self.tx.events), 1)

    def test_sparse_last_match_keeps_fact_reads_batched_and_claims_at_most_fifty(self):
        self.seed(['metadata'] * 49 + ['counters'] * 950 + ['metadata'])
        result = self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']],
                         [str(index) for index in range(49)] + ['999'])
        self.assertEqual(result['inspected_count'], 1000)
        self.assertLessEqual(len(self.writes(subject._LONG_FACTS)), 21)
        self.assertEqual([len(params['video_ids']) for params in self.writes(subject._LONG_FACTS)],
                         [10] + [50] * 19 + [40])
        self.assertEqual([len(params['video_ids']) for params in self.writes(subject._CLAIM)], [50])
        self.assertEqual(len(self.tx.fetched), 1000)
        self.assertEqual(self.writes(subject._PAUSE), [])

    def test_one_general_among_999_counters_is_a_finite_partial_batch(self):
        self.seed(['metadata'] + ['counters'] * 999)
        result = self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']], ['0'])
        self.assertEqual(result['inspected_count'], 1000)
        self.assertFalse(result['scan_exhausted'])
        self.assertEqual(len(self.writes(subject._LONG_FACTS)), 21)
        self.assertEqual(len(self.writes(subject._CLAIM)), 1)

    def test_mode_is_rechecked_from_locked_facts_and_disagreeing_update_aborts(self):
        self.seed(['metadata', 'counters'])
        original = self.tx.returning_all
        def changed(sql, params, *, max_rows):
            rows = original(sql, params, max_rows=max_rows)
            if sql == subject._CLAIM:
                rows[0]['collection_mode'] = 'counters'
            return rows
        self.tx.returning_all = changed
        with self.assertRaises(DatabaseError):
            self.invoke()
        self.assertTrue(self.tx.failed)
        self.assertEqual(self.fences, [])

    def test_explicit_operation_rechecks_locked_mode_without_stealing_opposite_work(self):
        self.seed(['counters', 'metadata', 'hydrate', 'counters'])
        from test_video_scheduled_claims import RUN
        result = subject.claim_due_video_collection_batch(
            None, 50, 'worker', RUN, transaction=self.tx, operation='metadata',
            scopes=[dict(channel_id=channel, video_type='long') for channel in self.channels])
        self.assertEqual([row['video_id'] for row in result['rows']], ['1','2'])
        self.assertEqual(result['claim_stop_reason'], 'admission_changed_or_locked')
        self.assertEqual([identity for params in self.writes(subject._CLAIM)
                          for identity in params['video_ids']], ['1','2'])
        self.assertEqual(self.writes(subject._PAUSE), [])
        args = self.writes(subject._MIXED_WINDOW)[0]
        self.assertEqual(args['operation'], 'metadata')
        self.assertEqual(args['run_id'], RUN)

    def test_scope_policy_still_rechecks_before_operation_selection(self):
        self.seed(['metadata', 'counters', 'metadata'])
        self.tx.rows[0]['channel_id'] = 'foreign'
        # Explicit valid scopes omit the moved row; it must not be paused or
        # used as an anchor even when it was first in the advisory ID window.
        from test_video_scheduled_claims import RUN
        result = subject.claim_due_video_collection_batch(
            None, 50, 'worker', RUN, transaction=self.tx,
            scopes=[dict(channel_id=channel, video_type='long') for channel in self.channels])
        self.assertEqual([row['video_id'] for row in result['rows']], ['1'])
        self.assertEqual(self.writes(subject._PAUSE), [])


if __name__=='__main__':unittest.main()
