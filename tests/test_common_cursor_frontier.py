"""Real common feeder/coordinator: acknowledged frontier and bounded fair revisit."""
import unittest

import test_common_feeder as fixtures
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.dispatch import DispatchBlocked
from collection.inventory_feeder import AutoInventoryCursor
from collection.metadata_feeder import AutoMetadataCursor
from runtime.process_collection import _capture_collection_cursors, _restore_collection_cursors
from collection_shutdown import encode_cursor_checkpoint, decode_cursor_checkpoint


class PartialRepository(fixtures.Repository):
    """The real claim reports nonexhausted whenever the requested limit fills."""
    def claim_due_batch(self, **request):
        result = super().claim_due_batch(**request)
        result['scan_exhausted'] = len(result['rows']) < request['limit']
        return result


class CursorFrontierTests(unittest.TestCase):
    setUp = fixtures.FeederTests.setUp
    create = fixtures.FeederTests.create
    drive = fixtures.FeederTests.drive

    def child_checkpoint(self, cursor):
        cursors = (cursor, AutoMetadataCursor(), AutoInventoryCursor())
        encoded = encode_cursor_checkpoint(_capture_collection_cursors(cursors))
        restored = (CommonCandidateCursor(), AutoMetadataCursor(), AutoInventoryCursor())
        _restore_collection_cursors(restored, decode_cursor_checkpoint(encoded))
        return restored[0]

    def test_unstarted_page_tail_survives_finite_child_without_empty_middle_pass(self):
        cursor = CommonCandidateCursor()
        repo = PartialRepository({fixtures.channel(101): 1, fixtures.channel(102): 1, fixtures.channel(103): 2},
            physical=[fixtures.channel(n) for n in range(1,104)])
        results = []
        for number in range(2):
            feeder, owner, executor = self.create(repo, limit=2, cursor=cursor)
            self.drive(feeder, owner, executor)
            feeder.finish_pass(owner)
            results.append(feeder.summary['updated'])
            if number == 0:
                self.assertEqual(cursor._after[('counters','long')], fixtures.channel(102))
                self.assertNotIn(('counters','long'), cursor._exhausted)
                self.assertEqual(repo.rows[fixtures.channel(103)], 2)
            cursor = self.child_checkpoint(cursor)
        self.assertEqual(results, [2,2])
        self.assertEqual([scope['channel_id'] for r in repo.claims for scope in r['scopes']],
                         [fixtures.channel(101), fixtures.channel(102), fixtures.channel(103)])

    def test_partial_scope_revisits_only_after_later_channel_and_no_empty_pass(self):
        cursor = CommonCandidateCursor()
        repo = PartialRepository({fixtures.channel(1): 100, fixtures.channel(2): 1})
        first, owner, executor = self.create(repo, limit=2, cursor=cursor)
        self.drive(first, owner, executor);first.finish_pass(owner)
        second, owner, executor = self.create(repo, limit=2, cursor=cursor)
        self.drive(second, owner, executor);second.finish_pass(owner)
        self.assertEqual(second.summary['updated'], 2)
        self.assertEqual([scope['channel_id'] for r in repo.claims for scope in r['scopes']],
            [fixtures.channel(1), fixtures.channel(2), fixtures.channel(1)])
        self.assertEqual(repo.rows[fixtures.channel(1)], 97)
        self.assertEqual(repo.rows[fixtures.channel(2)], 0)

    def test_partial_early_channel_does_not_starve_501_tail_at_physical_cap(self):
        cursor = CommonCandidateCursor()
        repo = PartialRepository({fixtures.channel(1): 10000, fixtures.channel(501): 1},
            physical=[fixtures.channel(n) for n in range(1,502)])
        for _ in range(5):
            feeder, owner, executor = self.create(repo, limit=50, cursor=cursor)
            self.drive(feeder, owner, executor);feeder.finish_pass(owner)
            self.assertLessEqual(feeder.state.candidate_inspected, 1000)
            cursor = self.child_checkpoint(cursor)
            if repo.rows[fixtures.channel(501)] == 0:break
        else:self.fail('late channel not admitted within the physical sweep')
        self.assertEqual([scope['channel_id'] for r in repo.claims for scope in r['scopes']][:2],
                         [fixtures.channel(1), fixtures.channel(501)])
        self.assertGreater(repo.rows[fixtures.channel(1)], 9000)

    def test_revisit_flag_is_consumed_once_and_empty_sweep_stops(self):
        cursor = CommonCandidateCursor();cursor._revisit=True
        repo = fixtures.Repository({}, physical=[fixtures.channel(n) for n in range(1,6)])
        feeder, owner, executor = self.create(repo, limit=2, cursor=cursor)
        self.drive(feeder, owner, executor);feeder.finish_pass(owner)
        self.assertEqual(feeder.state.candidate_inspected, 40)  # Four formats, two sweeps.
        self.assertFalse(cursor._revisit)
        self.assertTrue(cursor.sweep_exhausted)
        self.assertEqual(feeder.pass_stop_reason, 'sweep_exhausted')

    def test_other_operation_survives_raw_budget_and_child_checkpoint(self):
        class SplitOperationRepository(fixtures.Repository):
            def claim_due_batch(inner, **request):
                first = not inner.claims
                result = super().claim_due_batch(**dict(request,
                    limit=1 if first else request['limit']))
                if first:
                    result.update(inspected_count=1000, scan_exhausted=False)
                return result
        cursor=CommonCandidateCursor()
        repo=SplitOperationRepository({fixtures.channel(1):34})
        first,owner,executor=self.create(repo,limit=34,cursor=cursor)
        self.drive(first,owner,executor);first.finish_pass(owner)
        self.assertEqual(first.summary['updated'],1)
        self.assertEqual(first.pass_stop_reason,'raw_scan_budget')
        self.assertTrue(cursor._revisit)
        self.assertEqual(repo.rows[fixtures.channel(1)],33)
        cursor=self.child_checkpoint(cursor)
        second,owner,executor=self.create(repo,limit=34,cursor=cursor)
        self.drive(second,owner,executor);second.finish_pass(owner)
        self.assertEqual(second.summary['updated'],33)
        self.assertEqual([len(ids) for ids in self.sent],[1,33])
        self.assertEqual(repo.rows[fixtures.channel(1)],0)

    def test_partial_revisit_never_bypasses_raw_claim_scan_limit(self):
        cursor = CommonCandidateCursor()
        repo = PartialRepository({fixtures.channel(1): 1})
        repo.claim_due_batch = lambda **kw: dict(rows=[],inspected_count=1000,
            paused_count=0,scan_exhausted=False)
        feeder, owner, executor = self.create(repo, limit=2, cursor=cursor)
        self.drive(feeder, owner, executor);feeder.finish_pass(owner)
        self.assertEqual(feeder.state.inspected, 1000)
        self.assertEqual(feeder.pass_stop_reason, 'raw_scan_budget')
        self.assertTrue(cursor._revisit)
        self.assertEqual(feeder.state.claimed, 0)

    def test_out_of_order_known_claim_cannot_cross_unstarted_frontier(self):
        state=AutoPassState(2,concurrency=2);cursor=CommonCandidateCursor()
        state.bind_candidate_cursor(cursor);state.candidate_request(2,operation='counters')
        scopes=[dict(channel_id=fixtures.channel(n),video_type='long') for n in (1,2)]
        rows=[dict(scope,operation='counters',ready_count=1,due_at='2026-10-14T00:00:00Z') for scope in scopes]
        state.accept_candidate_page(dict(rows=rows,next_after=fixtures.channel(2),
            inspected_count=2,scan_exhausted=False))
        state.candidate_claimed(scopes[1],operation='counters')
        self.assertIsNone(cursor._after[('counters','long')])
        state.candidate_claimed(scopes[0],operation='counters')
        self.assertEqual(cursor._after[('counters','long')], fixtures.channel(2))
        with self.assertRaisesRegex(RuntimeError,'candidate_claim_unexpected'):
            state.candidate_claimed(scopes[0],operation='counters')

    def test_wrapped_ack_order_keeps_unclaimed_tail_and_other_operation_position(self):
        state=AutoPassState(50,concurrency=2);cursor=CommonCandidateCursor()
        cursor._after[('counters','long')]=fixtures.channel(800)
        cursor._after[('metadata','long')]=fixtures.channel(400)
        cursor._exhausted.update(('counters',kind) for kind in ('short','stream','unknown'))
        state.bind_candidate_cursor(cursor)
        def page(numbers):
            while cursor._formats[0]!='long':cursor._formats.rotate(-1)
            request=state.candidate_request(2,scope_limit=50,operation='counters',extend_types=('long',))
            self.assertEqual(request['video_type'],'long')
            rows=[dict(channel_id=fixtures.channel(n),video_type='long',operation='counters',
                       ready_count=1,due_at='2026-10-14T00:00:00Z') for n in numbers]
            return state.accept_candidate_page(dict(rows=rows,next_after=fixtures.channel(numbers[-1]),
                inspected_count=len(numbers),scan_exhausted=True))
        late=page((900,950))
        self.assertTrue(state.operation_window_exhausted('counters'))
        self.assertTrue(state.wrap_candidate_window('counters',scopes=late))
        # A wrapped page can encounter the same late scope; do not enqueue it twice.
        early=page((10,50,900))
        self.assertEqual([row['channel_id'] for row in early],[fixtures.channel(10),fixtures.channel(50)])
        state.candidate_claimed(early[0],operation='counters')
        self.assertEqual(cursor._after[('counters','long')],fixtures.channel(800))
        state.candidate_claimed(late[0],operation='counters')
        self.assertEqual(cursor._after[('counters','long')],fixtures.channel(900))
        # The pending late scope still gates the wrap. Handoff may safely retry it.
        state.pause('scheduled_handoff');cursor._release(state)
        restored=self.child_checkpoint(cursor)
        self.assertEqual(restored._after[('counters','long')],fixtures.channel(900))
        self.assertEqual(restored._after[('metadata','long')],fixtures.channel(400))
        self.assertNotIn(('counters','long'),restored._exhausted)

    def test_wrapped_known_claim_can_checkpoint_new_sweep_without_rewinding_other_operation(self):
        state=AutoPassState(50,concurrency=2);cursor=CommonCandidateCursor()
        cursor._after[('counters','long')]=fixtures.channel(800)
        cursor._after[('metadata','long')]=fixtures.channel(400)
        cursor._exhausted.update(('counters',kind) for kind in ('short','stream','unknown'))
        state.bind_candidate_cursor(cursor)
        def page(number):
            while cursor._formats[0]!='long':cursor._formats.rotate(-1)
            state.candidate_request(2,scope_limit=50,operation='counters',extend_types=('long',))
            rows=[dict(channel_id=fixtures.channel(number),video_type='long',operation='counters',
                       ready_count=1,due_at='2026-10-14T00:00:00Z')]
            return state.accept_candidate_page(dict(rows=rows,next_after=fixtures.channel(number),
                inspected_count=1,scan_exhausted=True))[0]
        late=page(900)
        self.assertTrue(state.wrap_candidate_window('counters',scopes=[late]))
        early=page(10)
        state.candidate_claimed(early,operation='counters')
        state.candidate_claimed(late,operation='counters')
        cursor._release(state)
        restored=self.child_checkpoint(cursor)
        self.assertEqual(restored._after[('counters','long')],fixtures.channel(10))
        self.assertEqual(restored._after[('metadata','long')],fixtures.channel(400))
        self.assertIn(('counters','long'),restored._exhausted)

    def test_wrap_cannot_change_frontier_owned_by_another_offered_descriptor(self):
        from copy import deepcopy
        state=AutoPassState(50,concurrency=2);cursor=CommonCandidateCursor()
        cursor._exhausted.update(('metadata',kind) for kind in ('stream','unknown'))
        state.bind_candidate_cursor(cursor)
        rows=[]
        for kind,number in (('long',900),('short',10)):
            request=state.candidate_request(2,scope_limit=50,operation='metadata')
            self.assertEqual(request['video_type'],kind)
            row=dict(channel_id=fixtures.channel(number),video_type=kind,operation='metadata',
                     ready_count=1,due_at='2026-10-14T00:00:00Z')
            state.accept_candidate_page(dict(rows=[row],next_after=row['channel_id'],
                inspected_count=1,scan_exhausted=True))
            rows.append(row)
        self.assertTrue(state.operation_window_exhausted('metadata'))
        before=deepcopy((cursor._after,cursor._exhausted,state._candidate_pages))
        # The first descriptor owns Long; the second may only wrap its own Short.
        self.assertFalse(state.wrap_candidate_window('metadata',scopes=[rows[1]]))
        self.assertEqual((cursor._after,cursor._exhausted,state._candidate_pages),before)
        state.candidate_claimed(rows[0],operation='metadata')
        self.assertEqual(cursor._after[('metadata','long')],fixtures.channel(900))
        self.assertIn(('metadata','long'),cursor._exhausted)

    def test_unknown_partial_settlement_retains_credit_and_cannot_checkpoint(self):
        cursor = CommonCandidateCursor();repo = PartialRepository({fixtures.channel(1): 100})
        feeder, owner, executor = self.create(repo,limit=2,cursor=cursor)
        # Sparse candidate windows may take several bounded DB-only turns.
        for _ in range(10):
            feeder.offer_to(owner);owner.step()
            if executor.jobs:
                break
        self.assertTrue(executor.jobs)
        with self.assertRaisesRegex(RuntimeError,'drain_unconfirmed'):
            self.child_checkpoint(cursor)
        self.assertFalse(cursor._revisit)  # A claim alone is not an ACK.
        self.unknown_settlement=True;executor.run(0)
        with self.assertRaises(DispatchBlocked):owner.step()
        with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):feeder.finish_pass(owner)
        with self.assertRaisesRegex(RuntimeError,'drain_unconfirmed'):self.child_checkpoint(cursor)
        self.assertFalse(cursor._revisit)
        self.assertEqual(len(repo.claims),1)

    def test_pending_page_blocks_only_its_format_and_ready_is_not_finished(self):
        cursor=CommonCandidateCursor();repo=PartialRepository({fixtures.channel(1):2})
        feeder,owner,executor=self.create(repo,limit=2,cursor=cursor)
        for _ in range(10):
            if feeder.offer_to(owner):
                break
        self.assertEqual(owner.snapshot().ready, 1)
        # No claim yet. Other formats can inspect/finish, but this known ready
        # descriptor keeps the pass open and its own format's frontier unchanged.
        for _ in range(6):feeder.offer_to(owner)
        self.assertFalse(feeder.pass_finished)
        self.assertIsNone(cursor._after[('counters','long')])
        self.assertEqual({p['video_type'] for p in repo.pages},{'long','short','stream','unknown'})
        self.assertEqual(sum(p['video_type']=='long' for p in repo.pages),2)  # Seed + filtered page.
        with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):feeder.finish_pass(owner)
        self.drive(feeder,owner,executor);feeder.finish_pass(owner)
        self.assertEqual(feeder.summary['updated'],2)

    def test_should_stop_skip_does_not_move_unstarted_frontier(self):
        cursor=CommonCandidateCursor();repo=PartialRepository({fixtures.channel(1):2})
        feeder,owner,executor=self.create(repo,limit=2,cursor=cursor)
        for _ in range(10):
            if feeder.offer_to(owner):
                break
        self.assertEqual(owner.snapshot().ready, 1)
        feeder.should_stop=lambda:True
        turn=owner.step()
        self.assertEqual(len(turn.skipped), 1)
        feeder.pause('scheduled_handoff');feeder.finish_pass(owner)
        self.assertIsNone(cursor._after[('counters','long')])
        self.assertFalse(repo.claims)
        self.assertFalse(executor.jobs)

    def test_known_cancel_keeps_whole_unstarted_page_for_next_child(self):
        cursor=CommonCandidateCursor();repo=PartialRepository({fixtures.channel(1):2,fixtures.channel(2):2})
        feeder,owner,executor=self.create(repo,limit=2,cursor=cursor)
        feeder.offer_to(owner);feeder.pause('quota_exhausted')
        for key in owner.keys():owner.cancel(key)
        feeder.finish_pass(owner)
        self.assertIsNone(cursor._after[('counters','long')])
        self.assertFalse(repo.claims)
        cursor=self.child_checkpoint(cursor)
        next_feeder,owner,executor=self.create(repo,limit=2,cursor=cursor)
        self.drive(next_feeder,owner,executor);next_feeder.finish_pass(owner)
        self.assertEqual(next_feeder.summary['updated'],2)
        self.assertEqual(repo.claims[0]['scopes'][0]['channel_id'],fixtures.channel(1))


if __name__=='__main__':unittest.main()
