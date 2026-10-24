"""Real feeder/coordinator, controlled Futures and bounded advisory DB doubles."""
from pathlib import Path
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_feeder import CommonAutoFeeder
from collection.dispatch import Ack,Coordinator,DispatchBlocked,Prepared,Work
from collection.provider_steps import ProviderStep
import collection_shutdown
from test_collection_dispatch import ManualExecutor,Clock


def channel(n):
    return 'UC'+str(n).zfill(22)


class Repository:
    def __init__(self, rows, *, kind='long', physical=None):
        self.owner=threading.get_ident()
        self.rows=dict(rows)
        self.kind=kind
        self.physical=sorted(physical if physical is not None else rows)
        self.pages=[]
        self.claims=[]
        self.unknown=False
        self.wrong_scope=False
        self.issued = {}

    def due_candidate_page(self, **request):
        assert threading.get_ident()==self.owner
        self.pages.append(request)
        physical=[key for key in self.physical if key>(request['after'] or '')][:request['limit']]
        operation = 'counters' if self.kind == 'long' else 'metadata'
        return dict(rows=[dict(channel_id=key,video_type=self.kind, operation=operation,
                    ready_count=min(50, self.rows[key]), due_at='2026-10-14T00:00:00Z') for key in physical
                    if request['video_type']==self.kind and self.rows.get(key,0)>0
                    and request.get('operation') in (None, operation)
                    and not request.get('frontiers',{}).get(operation,{}).get('exhausted',False)
                    and key > (request.get('frontiers',{}).get(operation,{}).get('after') or '')],
                    next_after=physical[-1] if physical else request['after'],
                    inspected_count=len(physical),scan_exhausted=len(physical)<request['limit'])

    def claim_due_batch(self, **request):
        assert threading.get_ident()==self.owner
        self.claims.append(request)
        if self.unknown:
            raise OSError('synthetic lost commit acknowledgement')
        scopes = request.get('scopes') or [request['scope']]
        rows = []
        while len(rows) < request['limit']:
            before = len(rows)
            for scope in scopes:
                key = scope['channel_id']
                if self.rows.get(key, 0) and len(rows) < request['limit']:
                    number = self.issued.get(key, 0)
                    self.issued[key] = number + 1
                    self.rows[key] -= 1
                    rows.append(SimpleNamespace(video_id=f'{key}:{number}',
                        channel_id=channel(9999) if self.wrong_scope else key,
                        video_type=scope['video_type']))
            if len(rows) == before:
                break
        return dict(rows=rows,inspected_count=len(rows),paused_count=0,
                    scan_exhausted=len(rows)<request['limit'])



class FeederTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(collection_shutdown,'_requested',threading.Event())
        guard.start()
        self.addCleanup(guard.stop)
        self.sent=[]
        self.unknown_settlement=False

    def create(self, repository, *, limit=100, concurrency=2, cursor=None):
        def steps(claims,**kwargs):
            result=yield ProviderStep(lambda ids,reason:self.sent.append(tuple(ids)),
                                      tuple(row.video_id for row in claims),'auto')
            result.unwrap()
            return dict(updated=len(claims),batches=1,video_ids=[row.video_id for row in claims],
                        settlement_unknown=self.unknown_settlement)
        service=SimpleNamespace(repository=repository,common_claim_steps=steps,
                                finish_common_claims=lambda claims,summary:summary)
        state=AutoPassState(limit,concurrency=concurrency)
        feeder=CommonAutoFeeder(service,state=state,cursor=cursor or CommonCandidateCursor(),worker_id='worker',require_ready=lambda:None)
        executor=ManualExecutor()
        coordinator=Coordinator(executor,concurrency,clock=Clock())
        return feeder,coordinator,executor

    def drive(self,feeder,coordinator,executor,maximum=1500):
        for _ in range(maximum):
            feeder.offer_to(coordinator)
            coordinator.step()
            for index,(future,*_) in enumerate(executor.jobs):
                if not future.done():executor.run(index)
            snapshot=coordinator.snapshot()
            self.assertLessEqual(snapshot.ready,2*coordinator.concurrency)
            self.assertLessEqual(snapshot.held,coordinator.concurrency)
            if feeder.exhausted and not snapshot.ready and not snapshot.held:
                return
        self.fail('bounded fake pass did not finish')

    def test_one_pass_budget_is_shared_across_50_plus_1_and_queued_descriptors(self):
        repo=Repository({channel(i):1000 for i in range(1,6)})
        feeder,coordinator,executor=self.create(repo,limit=51)
        self.drive(feeder,coordinator,executor)
        self.assertEqual([row['limit'] for row in repo.claims],[50,1])
        self.assertEqual(len({row['run_id'] for row in repo.claims}),1)
        self.assertEqual(feeder.state.claimed,51)
        self.assertEqual(feeder.summary['updated'],51)
        self.assertEqual(len(feeder.summary['video_ids']),32)
        self.assertEqual([len(ids) for ids in self.sent],[50,1])

    def test_empty_physical_prefix_advances_without_a_total_channel_cap(self):
        repo=Repository({channel(501):1},physical=[channel(i) for i in range(1,502)])
        feeder,coordinator,executor=self.create(repo,limit=1000)
        self.drive(feeder,coordinator,executor)
        self.assertEqual(feeder.summary['updated'],1)
        self.assertEqual(repo.claims[0]['scopes'][0]['channel_id'],channel(501))
        self.assertGreater(len(repo.pages),10)
        self.assertTrue(all(1<=row['limit']<=50 for row in repo.pages))

    def test_unknown_identity_scope_uses_channel_fairness_group(self):
        repo=Repository({channel(1):1},kind='unknown')
        feeder,coordinator,executor=self.create(repo,limit=1)
        self.drive(feeder,coordinator,executor)
        self.assertEqual(repo.claims[0]['scopes'][0]['video_type'],'unknown')
        self.assertEqual(feeder.summary['updated'],1)

    def test_unknown_claim_retains_reservation_and_sends_nothing(self):
        repo=Repository({channel(1):50,channel(2):50})
        repo.unknown=True
        feeder,coordinator,executor=self.create(repo)
        feeder.offer_to(coordinator)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual(feeder.state.held,1)
        self.assertTrue(feeder.state.paused)
        self.assertEqual(coordinator.snapshot().held,1)
        self.assertFalse(self.sent)
        self.assertEqual(feeder.offer_to(coordinator),0)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual(len(repo.claims),1)

    def test_unknown_settlement_does_not_count_progress_or_repeat(self):
        repo=Repository({channel(1):1})
        feeder,coordinator,executor=self.create(repo,limit=1)
        self.unknown_settlement=True
        feeder.offer_to(coordinator)
        coordinator.step()
        executor.run(0)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual(feeder.summary['updated'],0)
        self.assertEqual(coordinator.snapshot().held,1)
        self.assertEqual(feeder.offer_to(coordinator),0)
        self.assertEqual(len(self.sent),1)

    def test_bad_scope_ack_is_not_allowed_to_send_another_channels_ids(self):
        repo=Repository({channel(1):1})
        repo.wrong_scope=True
        feeder,coordinator,executor=self.create(repo,limit=1)
        feeder.offer_to(coordinator)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertTrue(feeder.state.paused)
        self.assertFalse(self.sent)

    def test_operator_interleaves_without_resetting_run_or_budget(self):
        repo=Repository({channel(i):40 for i in range(1,6)})
        feeder,coordinator,executor=self.create(repo,limit=135)
        order=[]
        coordinator.offer(Work('operator','operator',channel(99),'channel',
            lambda:Prepared(lambda cancel:order.append(feeder.state.claimed),200),lambda value:Ack()))
        self.drive(feeder,coordinator,executor)
        self.assertEqual(feeder.state.claimed,135)
        self.assertEqual(len({row['run_id'] for row in repo.claims}),1)
        self.assertEqual(len(order),1)
        self.assertLess(order[0],135)
        self.assertEqual(feeder.summary['updated'],135)

    def test_explicit_zero_offer_preserves_cursor_and_bounded_allowance(self):
        repository = Repository({channel(i): 1 for i in range(1, 5)})
        feeder, coordinator, executor = self.create(repository)
        self.assertEqual(feeder.offer_to(coordinator, limit=0), 0)
        self.assertFalse(repository.pages)
        for _ in range(10):
            if feeder.offer_to(coordinator, limit=1):
                break
        self.assertEqual(coordinator.snapshot().ready, 1)
        self.assertEqual(repository.pages[0]['limit'], 50)
        for value in (-1, 101, True, 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                feeder.offer_to(coordinator, limit=value)

    def test_ready_capacity_stops_candidate_reads_without_a_private_backlog(self):
        repo=Repository({channel(i):100 for i in range(1,15)})
        feeder,coordinator,executor=self.create(repo,limit=500)
        for index in range(4):
            coordinator.offer(Work(f'operator:{index}', 'operator', channel(99), 'channel',
                lambda: Prepared(lambda cancel: None, 200), lambda result: Ack()))
        self.assertEqual(feeder.offer_to(coordinator),0)
        self.assertFalse(repo.pages)
        coordinator.cancel('operator:0')
        self.assertEqual(feeder.offer_to(coordinator),1)
        page_count=len(repo.pages)
        self.assertEqual(coordinator.snapshot().ready,4)
        self.assertEqual(feeder.offer_to(coordinator),0)
        self.assertEqual(len(repo.pages),page_count)
        self.assertFalse(repo.claims)

    def test_sparse_channels_fill_physical_requests(self):
        for channels, per_channel, expected in ((10,1,[10]),(10,5,[50]),(50,1,[50]),(100,1,[50,50])):
            with self.subTest(channels=channels, per_channel=per_channel):
                self.sent.clear()
                repo=Repository({channel(i):per_channel for i in range(1,channels+1)})
                feeder,owner,executor=self.create(repo,limit=channels*per_channel,concurrency=8)
                self.drive(feeder,owner,executor)
                self.assertEqual([len(ids) for ids in self.sent],expected)
                ids=[value for batch in self.sent for value in batch]
                self.assertEqual(len(set(ids)),channels*per_channel)
                self.assertEqual(feeder.summary['updated'],channels*per_channel)
                self.assertTrue(all(len(row['scopes'])<=50 for row in repo.claims))

    def test_short_operation_batch_is_sent_then_other_operation_revisited(self):
        class SplitOperationRepository(Repository):
            def claim_due_batch(inner, **request):
                first = not inner.claims
                result = super().claim_due_batch(**dict(request,
                    limit=1 if first else request['limit']))
                if first:
                    # One General row among 33 already-due counter-only rows:
                    # the real scoped claim keeps the other operation pending.
                    result.update(inspected_count=34, scan_exhausted=False)
                return result
        repo = SplitOperationRepository({channel(1):34})
        feeder, owner, executor = self.create(repo, limit=34, concurrency=8)
        self.drive(feeder, owner, executor)
        self.assertEqual([len(ids) for ids in self.sent], [1,33])
        self.assertEqual(feeder.summary['updated'],34)
        self.assertEqual(len(repo.claims),2)
        self.assertEqual(len({row['run_id'] for row in repo.claims}),1)
        self.assertEqual(repo.rows[channel(1)],0)

    def test_small_population_refills_eight_slots_before_provider_settlement(self):
        repo=Repository({channel(i):1000 for i in range(1,11)})
        feeder,owner,executor=self.create(repo,limit=500,concurrency=8)
        for _ in range(80):
            feeder.offer_to(owner)
            owner.step()
            if len(executor.jobs)==8:
                break
        self.assertEqual(len(executor.jobs),8)
        self.assertEqual(owner.snapshot().inflight,8)
        self.assertEqual(feeder.state.claimed,400)
        self.assertEqual(feeder.state.held,8)
        self.assertFalse(self.sent)
        self.assertTrue(all(len(row['scopes'])==10 for row in repo.claims))
        self.assertTrue(all(count==960 for count in repo.rows.values()))
        with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):
            feeder.finish_pass(owner)
        self.drive(feeder,owner,executor)
        self.assertEqual(feeder.summary['updated'],500)

    def test_large_scope_does_not_delay_small_scope_and_eight_slots_can_fill(self):
        repo=Repository({channel(1):1000,**{channel(i):1 for i in range(2,352)}})
        feeder,owner,executor=self.create(repo,limit=500,concurrency=8)
        for _ in range(80):
            feeder.offer_to(owner)
            owner.step()
            if len(executor.jobs)==8:
                break
        self.assertEqual(len(executor.jobs),8)
        self.assertEqual(owner.snapshot().inflight,8)
        self.assertEqual(repo.rows[channel(2)],0)
        self.assertGreater(repo.rows[channel(1)],0)
        self.assertFalse(self.sent)  # All eight are still pending, not serially settled.
        self.assertEqual(feeder.state.held,8)
        self.drive(feeder,owner,executor)
        self.assertEqual(feeder.summary['updated'],500)


class TypedStatsFeederTests(unittest.TestCase):
    """Exercise candidate cursor -> feeder -> coordinator -> real video service."""

    def test_short_and_finished_stream_reach_stats_without_unknown_or_live_stream(self):
        from db import VideoCollectionClaim
        from test_initial_video_parallel import service,video,CHANNEL,STAMP
        from test_common_video_dispatch import Repository as CompletionRepository
        class DueRepository(CompletionRepository):
            def __init__(self):
                super().__init__()
                self.pending={
                    'short000001':('short','counters'),
                    'ended000001':('stream','counters'),
                    'live0000001':('stream','stream_lifecycle'),
                    'unknown0001':('unknown','identity_refresh'),
                }
                self.pages=[]

            def due_candidate_page(self,**request):
                self.pages.append(request)
                physical=[CHANNEL] if (request['after'] or '')<CHANNEL else []
                candidates={mode:sum(kind==request['video_type'] and
                    ('counters' if operation=='counters' else 'metadata')==mode
                    for kind,operation in self.pending.values()) for mode in ('metadata','counters')}
                rows=[]
                for operation,count in candidates.items():
                    frontier=request.get('frontiers',{}).get(operation,{})
                    if (physical and count and request.get('operation') in (None,operation)
                            and not frontier.get('exhausted') and (frontier.get('after') or '')<CHANNEL):
                        rows.append(dict(channel_id=CHANNEL,video_type=request['video_type'],
                            operation=operation,ready_count=count,due_at=STAMP))
                # A native unfiltered head projects the earliest operation for
                # a scope; the filtered read later returns its entire cohort.
                return dict(rows=rows[:1],inspected_count=len(physical),
                    next_after=CHANNEL if physical else request['after'],scan_exhausted=True)

            def claim_due_batch(self,**request):
                allowed={scope['video_type'] for scope in request['scopes']}
                selected=[identity for identity,(kind,mode) in self.pending.items()
                    if kind in allowed and ('counters' if mode=='counters' else 'metadata')==request['operation']]
                rows=[]
                for identity in selected[:request['limit']]:
                    kind,mode=self.pending.pop(identity)
                    rows.append(VideoCollectionClaim(identity,CHANNEL,mode,'lease',STAMP,STAMP,kind))
                return dict(rows=rows,inspected_count=len(rows),paused_count=0,scan_exhausted=True)

            def complete_stream_lifecycle(self,rows):
                return self.record('stream_lifecycle',rows)

        with patch.object(collection_shutdown,'_requested',threading.Event()):
            repo=DueRepository()
            collector=service(repo,None)
            stats,general=[],[]
            def fetch_stats(ids,reason):
                stats.extend(ids)
                return dict(items=[dict(id=identity,views=123,like_count=7,comment_count=2) for identity in ids],
                            summary=dict(failed_ids=[]))
            def fetch_general(ids,reason):
                general.extend(ids)
                return [video(identity) for identity in ids]
            collector.fetch_counters=fetch_stats
            collector.fetch_metadata=fetch_general
            collector.build_counter_payload=lambda item,recorded_at:dict(snapshot={**item,'recorded_at':recorded_at})
            feeder=CommonAutoFeeder(collector,state=AutoPassState(10,concurrency=2),cursor=CommonCandidateCursor(),
                worker_id='typed-stats-proof',require_ready=lambda:None)
            with ThreadPoolExecutor(max_workers=2) as executor:
                coordinator=Coordinator(executor,2)
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    feeder.offer_to(coordinator)
                    coordinator.step()
                    current=coordinator.snapshot()
                    if feeder.exhausted and not current.ready and not current.held:
                        break
                    coordinator.wait(.001)
                else:
                    self.fail('typed Stats pass did not finish')
            self.assertEqual(set(stats),{'short000001','ended000001'})
            self.assertEqual(set(general),{'live0000001','unknown0001'})
            self.assertEqual(len(stats),2)
            self.assertFalse(repo.pending)
            self.assertEqual({row['video_id'] for operation,rows in repo.writes if operation=='counters'
                              for row in rows},{'short000001','ended000001'})
            self.assertFalse(any(operation=='failure' for operation,_ in repo.writes))


if __name__=='__main__':
    unittest.main()
