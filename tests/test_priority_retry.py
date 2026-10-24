"""Bounded exact blocker retries; local clocks/fakes, no database or provider."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.hot.hot_retention import (CleanupPage, HotRetentionRepository, PriorityReference,
                                 PriorityResult, RetentionError)
from hot_retention import HotCleanupCycle, PriorityRetrySet
from hot_runtime import HotRuntimeController, HotRuntimeUnavailable

DAY=86400

def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat()


def ref(target,identity):
    return PriorityReference(target,(identity,))


def page(target,now,refs=(),*,complete=True,cursor=None,covered=True,unknown=False):
    return CleanupPage.parse(target,dict(as_of=iso(now),cutoff=iso(now-29*DAY),
        scanned=len(refs),deleted=0,scrubbed=0,deferred=len(refs),complete=complete,
        next_cursor=cursor,oldest_remaining_expired_source_at=iso(now-29.6*DAY) if refs and not unknown else None,
        invalid_clock_remaining=0,unknown_expiry_remaining=len(refs) if unknown else 0,
        priority_references=[item.wire() for item in refs] if covered else [],priority_complete=covered),100)


def receipt(now,requested,remaining=(),*,complete=True,checked=None):
    return PriorityResult.parse(dict(as_of=iso(now),checked=[item.wire() for item in requested if checked is None or item in checked],
        remaining=[item.wire() for item in remaining],complete=complete,deleted=0,
        scrubbed=len(set(requested)-set(remaining)) if complete else 0),requested)


class Pages:
    def __init__(self):
        self.wall=1_800_000_000.0
        self.mono=500.0
        self.a=ref('video_metadata','video-A')
        self.b=ref('video_counters','video-B')
        self.held={self.a,self.b}
        self.calls=[]
        self.retries=[]
        self.checks=[]
        self.partial=False
        self.failure=None
        self.drift=False
        self.on_retry=lambda:None
        self.catchup=False

    def contract(self):
        return dict(version=1,schema_ready=True,as_of=iso(self.wall))

    def startup_expiry(self,*,cutoff,strict_entry=False):
        self.checks.append((cutoff,strict_entry))
        return dict(as_of=iso(self.wall),expired=self.catchup)

    def cleanup_page(self,target,*,after_id=None,limit=100):
        self.calls.append((target,after_id))
        if target=='inventory':
            # Simulate the unfinished 216000-key traversal without a large fixture.
            position=int(after_id or 0)+1000
            return page(target,self.wall,complete=False,cursor=str(position))
        refs=tuple(item for item in self.held if item.target==target)
        return page(target,self.wall,refs)

    def cleanup_priority(self,requested):
        self.retries.append((self.mono,tuple(requested)))
        if self.failure:
            raise self.failure
        if self.drift:
            self.wall+=30
        self.on_retry()
        if self.partial:
            return receipt(self.wall,requested,complete=False,checked=requested[:1])
        return receipt(self.wall,requested,tuple(item for item in requested if item in self.held))


def runtime():
    repository=Pages()
    controller=HotRuntimeController(repository,local_cleanup=lambda:{'status':'current'},clear_caches=lambda:None,
        clock=lambda:repository.wall,monotonic=lambda:repository.mono,
        time_trust=type('Time',(),{'status':lambda self:{'state':'unverified'}})())
    controller.cycle=HotCleanupCycle(repository,targets=('video_metadata','video_counters','inventory'))
    controller.initialize()
    return controller,repository


def tick(controller,repository,seconds=0.1):
    repository.wall+=seconds
    repository.mono+=seconds
    controller.step()


class RuntimePriorityTests(unittest.TestCase):
    def discover(self,controller,repository):
        for _ in range(3):tick(controller,repository)
        self.assertTrue(controller.status()['collection_pause_requested'])
        self.assertEqual(len(controller._priority.references),2)
        self.assertEqual(controller.cycle.summary()['status'],'in_progress')

    def test_two_blockers_release_after_last_clears_without_global_scan_or_cursor_rewind(self):
        controller,repository=runtime()
        self.discover(controller,repository)
        before=tuple(controller.cycle._pending)
        repository.held.remove(repository.a)
        tick(controller,repository,5)
        self.assertTrue(controller.status()['collection_pause_requested'])
        self.assertEqual(set(controller._priority.references),{repository.b})
        self.assertEqual(tuple(controller.cycle._pending),before)
        repository.held.clear()
        tick(controller,repository,5)
        self.assertFalse(controller.status()['collection_pause_requested'])
        self.assertTrue(controller.status()['collection']['allowed'])
        self.assertEqual(tuple(controller.cycle._pending),before)
        self.assertEqual(controller.cycle.summary()['status'],'in_progress')
        self.assertEqual(repository.retries[1][0]-repository.retries[0][0],5)
        self.assertTrue(controller.status()['api_read']['allowed'])
        self.assertTrue(controller.status()['user_work']['allowed'])
        self.assertGreater(controller.cycle.summary()['counts']['deferred'],0)
        tick(controller,repository)
        self.assertFalse(controller.status()['collection_pause_requested'],'Old cumulative counts cannot revive resolved causes.')

    def test_empty_time_budget_slice_cannot_revive_resolved_historical_causes(self):
        controller,repository=runtime()
        self.discover(controller,repository)
        repository.held.clear()
        tick(controller,repository,5)
        original=controller.cycle.run_slice
        def no_time_left(**kwargs):
            return original(clock=iter((0,3)).__next__,**kwargs)
        controller.cycle.run_slice=no_time_left
        before=len(repository.calls)
        tick(controller,repository)
        self.assertEqual(len(repository.calls),before)
        self.assertFalse(controller._priority.unresolved)
        self.assertFalse(controller.status()['collection_pause_requested'])
        self.assertTrue(controller.status()['collection']['allowed'])

    def test_clean_unrelated_page_and_partial_proof_cannot_release(self):
        controller,repository=runtime()
        self.discover(controller,repository)
        tick(controller,repository)
        self.assertTrue(controller.status()['collection_pause_requested'])
        repository.held.clear()
        repository.partial=True
        tick(controller,repository,5)
        self.assertTrue(controller.status()['collection_pause_requested'])
        self.assertEqual(len(controller._priority.references),2)
        repository.partial=False
        tick(controller,repository,5)
        self.assertFalse(controller.status()['collection_pause_requested'])

    def test_unknown_commit_keeps_all_causes_and_priority(self):
        controller,repository=runtime()
        self.discover(controller,repository)
        repository.held.clear()
        repository.failure=RetentionError('cleanup_outcome_unknown')
        tick(controller,repository,5)
        self.assertTrue(controller.status()['collection_pause_requested'])
        self.assertEqual(len(controller._priority.references),2)
        self.assertTrue(controller.status()['maintenance_failed'])
        self.assertEqual(len(repository.retries),1)
        tick(controller,repository,5)
        self.assertEqual(len(repository.retries),1,'No immediate replay of an unknown cleanup write.')

    def test_watchdog_interruption_requires_fallback_even_if_exact_rows_are_now_clear(self):
        controller,repository=runtime()
        self.discover(controller,repository)
        repository.held.clear()
        tick(controller,repository,21)
        self.assertTrue(controller._priority.incomplete)
        self.assertTrue(controller.status()['collection_pause_requested'])
        self.assertEqual(controller.cycle.summary()['batches'],0)

    def test_clock_change_during_retry_cannot_acknowledge_or_release(self):
        controller,repository=runtime()
        self.discover(controller,repository)
        repository.held.clear()
        repository.drift=True
        tick(controller,repository,5)
        self.assertTrue(controller._priority.incomplete)
        self.assertEqual(len(controller._priority.references),2)
        self.assertTrue(controller.status()['collection_pause_requested'])
        self.assertFalse(controller.status()['api_read']['allowed'])

    def test_interruption_during_ack_wait_cannot_release(self):
        for stopped in (False,True):
            with self.subTest(stopped=stopped):
                controller,repository=runtime()
                self.discover(controller,repository)
                repository.held.clear()
                def interrupt():
                    if stopped:controller._stop.set()
                    else:
                        repository.wall+=21
                        repository.mono+=21
                repository.on_retry=interrupt
                tick(controller,repository,5)
                self.assertTrue(controller._priority.incomplete)
                self.assertEqual(len(controller._priority.references),2)
                self.assertTrue(controller.status()['collection_pause_requested'])
                self.assertTrue(controller._recheck_pending)

    def test_priority_release_does_not_complete_startup_or_move_original_cutoff(self):
        controller,repository=runtime()
        repository.catchup=True
        controller._startup_checked=False
        controller._check_startup()
        cutoff=controller._catchup_cutoff
        entry_checks=tuple(repository.checks)
        self.discover(controller,repository)
        repository.held.clear()
        tick(controller,repository,5)
        self.assertFalse(controller.status()['collection_pause_requested'])
        self.assertEqual(controller._catchup_cutoff,cutoff)
        self.assertTrue(controller._catchup)
        self.assertEqual(controller.status()['api_read']['reason'],'startup_cleanup')
        self.assertTrue(controller.status()['user_work']['allowed'])
        self.assertEqual(tuple(repository.checks),entry_checks,
                         'Exact retry cannot replace the original startup completion proof.')


class RegistryProofTests(unittest.TestCase):
    def test_overflow_never_discards_an_unresolved_cause_or_releases_after_tracked_rows_clear(self):
        pending=PriorityRetrySet()
        first=tuple(ref('video_metadata',str(i)) for i in range(100))
        pending.observe(page('video_metadata',1_800_000_000,first),0)
        pending.observe(page('video_metadata',1_800_000_000,(ref('video_metadata','overflow'),)),1)
        self.assertEqual(len(pending.references),100)
        self.assertTrue(pending.incomplete)
        generation,requested=pending.request()
        self.assertFalse(pending.acknowledge(generation,requested,receipt(1_800_000_000,requested),5))
        self.assertTrue(pending.unresolved)

    def test_unsupported_target_and_missing_coverage_remain_conservative(self):
        pending=PriorityRetrySet()
        raw=dict(as_of=iso(1_800_000_000),cutoff=iso(1_800_000_000-29*DAY),scanned=1,deleted=0,scrubbed=0,
                 deferred=1,complete=True,next_cursor=None,confirmed_expired_remaining=1)
        pending.observe(CleanupPage.parse('deleted_channels',raw,100),0)
        pending.observe(page('video_counters',1_800_000_000),1)
        self.assertTrue(pending.incomplete)
        self.assertTrue(pending.unresolved)
        self.assertFalse(pending.due(10))

    def test_new_cause_invalidates_older_all_clear_receipt(self):
        pending=PriorityRetrySet()
        a,b=ref('video_metadata','a'),ref('video_metadata','b')
        pending.observe(page(a.target,1_800_000_000,(a,)),0)
        generation,requested=pending.request()
        pending.observe(page(b.target,1_800_000_000,(b,)),1)
        self.assertFalse(pending.acknowledge(generation,requested,receipt(1_800_000_000,requested),5))
        self.assertEqual(set(pending.references),{a,b})

    def test_complete_receipt_must_cover_requested_set_and_private_refs_never_enter_summary(self):
        a,b=ref('video_metadata','private-video-a'),ref('video_metadata','private-video-b')
        with self.assertRaises(RetentionError):receipt(1_800_000_000,(a,b),checked=(a,))
        value=page(a.target,1_800_000_000,(a,))
        self.assertNotIn('private-video',str(value.summary()))
        with self.assertRaises(RetentionError):
            PriorityResult.parse(dict(as_of=iso(1_800_000_000),checked=[a.wire(),a.wire()],remaining=[],complete=True,deleted=0,scrubbed=0),(a,))

    def test_closed_maintenance_repository_cannot_dispatch_exact_cleanup(self):
        from db.hot.hot_runtime import MaintenanceRepository
        database=Mock()
        repository=MaintenanceRepository(database)
        repository.close()
        with self.assertRaisesRegex(RetentionError,'^maintenance_repository_closed$'):
            repository.cleanup_priority((ref('video_metadata','a'),))
        database.cleanup_hot_priority.assert_not_called()

    def test_repository_validates_receipt_and_never_retries_an_unknown_write(self):
        a=ref('video_metadata','a')
        database=Mock()
        database.cleanup_hot_priority.side_effect=OSError('private connection error')
        repository=HotRetentionRepository(database)
        with self.assertRaisesRegex(RetentionError,'^cleanup_outcome_unknown$'):
            repository.cleanup_priority((a,))
        database.cleanup_hot_priority.assert_called_once_with(references=[a.wire()])


if __name__=='__main__':unittest.main()
