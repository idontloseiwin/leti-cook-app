"""Actual shared Coordinator/adapters with owner-thread DB and provider doubles."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
import collection_shutdown
from operator_jobs import OperatorJobSettings, run_operator_jobs_dispatch
from db.base.postgres import DatabaseUnavailable
from test_initial_video_parallel import AtomicRepository, service, video
from test_shared_inventory_engine import Repository as InventoryRepository, claim, page


class Videos(AtomicRepository):
    def __init__(self, jobs):
        super().__init__()
        self.jobs = jobs

    def claim_initial_video_ids(self, ids, **scope):
        job = self.jobs.by_id[scope['job_id']]
        assert scope['run_id'] == job['lease_token']
        assert scope['worker_id'] == job['lease_owner']
        return [replace(row, channel_id=scope['channel_id']) for row in super().claim_initial_video_ids(ids, **scope)]


class Jobs:
    def __init__(self, counts, phase='hydrate_videos'):
        self.owner = get_ident()
        self.database = object()
        self.serial = 0
        self.events = []
        self.by_id = {}
        self.remaining = {}
        self.windows = {}
        self.chain = {}
        self.pages = []
        self.claim_failure = self.settle_failure = None
        for index, count in enumerate(counts):
            identity, channel = str(uuid4()), 'UC' + chr(97 + index) * 22
            self.by_id[identity] = dict(id=identity, channel_id=channel, accepted_epoch=7,
                initial_scope_version=1, initial_video_types=['long', 'short', 'stream'],
                job_type='channel_initial_long_inventory', phase=phase, status='queued', rank=index)
            self.remaining[identity] = [f'v{index:03d}{n:07d}' for n in range(count)]
        self.serial = len(counts)

    def check(self):
        assert get_ident() == self.owner

    def candidate_page(self, *, limit, after=None, ceiling=None):
        self.check()
        rows = sorted(self.by_id.values(), key=lambda job: (job['rank'], job['id']))
        ceiling = self.serial if ceiling is None else ceiling
        rows = [job for job in rows if job['rank'] <= ceiling and (after is None or (job['rank'], job['id']) > after)]
        rows = rows[:limit]
        self.pages.append((limit, after))
        candidates = [dict(job_id=j['id'], channel_id=j['channel_id'], accepted_epoch=7,
            job_type=j['job_type'], phase=j['phase']) for j in rows if j['status']=='queued']
        return dict(candidates=candidates, inspected=len(rows), exhausted=len(rows)<limit,
            next_after=(rows[-1]['rank'], rows[-1]['id']) if rows else after, ceiling=ceiling)

    def claim_id(self, identity, channel, epoch, worker, *, lease_seconds):
        self.check()
        self.events.append(('claim', identity))
        if self.claim_failure:
            raise self.claim_failure
        job = self.by_id[identity]
        assert (job['channel_id'], job['accepted_epoch']) == (channel, epoch)
        if job['status'] != 'queued':
            return None
        self.serial += 1
        job.update(status='running', rank=self.serial, lease_token=str(uuid4()), lease_owner=worker)
        return dict(job)

    def prepare_initial_hydration_chunk(self, identity, token, *, limit, lease_seconds):
        self.check()
        self.windows[identity] = self.remaining[identity][:limit]
        return dict(job=self.by_id[identity], video_ids=self.windows[identity])

    def settle_initial_hydration_chunk(self, identity, token, **values):
        self.check()
        self.events.append(('settle', identity, values))
        if self.settle_failure:
            raise self.settle_failure
        retained = values['summary']['hydration_unstarted_video_ids']
        self.remaining[identity] = retained + self.remaining[identity][len(self.windows[identity]):]
        self.by_id[identity].update(status='queued', phase='hydrate_videos' if self.remaining[identity] else 'verify')
        return dict(self.by_id[identity])

    def verify_initial_hydration(self, identity, token):
        self.check()
        self.events.append(('verify', identity))
        self.by_id[identity]['status'] = 'completed'
        return dict(self.by_id[identity])

    def initialize_inventory_chains(self, identity, token):
        self.check()
        return dict(status='initialized', chain_count=3)

    def prepare_inventory_chain(self, identity, token, kind):
        self.check()
        if (identity, kind) in self.chain and self.chain[identity,kind].get('done'):
            return dict(action='done')
        value = self.chain[identity,kind] = dict(action='inventory', reserved_units=1, transition_id=str(uuid4()))
        return dict(value)

    def claim_inventory_chain_source(self, identity, token, kind, **kwargs):
        self.check()
        return dict(claims=[asdict(claim(kind, channel_id=self.by_id[identity]['channel_id']))])

    def bind_inventory_chain(self, identity, token, kind, **kw):
        self.check()
        self.events.append(('bind', identity, kind))
        return dict(status='bound', replayed=False, dispatch_allowed=True, **kw)

    def acknowledge_inventory_chain(self, identity, token, kind, **kw):
        self.check()
        self.events.append(('ack', identity, kind))
        self.chain[identity,kind]['done'] = True
        return dict(ack={}, dispatch_allowed=False)

    def cancel_prepared_inventory_chain(self, identity, token, kind, **kw):
        self.check()
        self.events.append(('cancel_prepared', identity, kind))
        return dict(status='cancelled', dispatch_allowed=False)

    def cancel_unsent_inventory_chain(self, identity, token, kind, **kw):
        self.check()
        assert kw['fetch_unsent'] is True
        self.events.append(('cancel_unsent', identity, kind))
        return dict(status='cancelled', dispatch_allowed=False)

    def finish_inventory_chains(self, identity, token):
        self.check()
        self.events.append(('finish', identity))
        done = all(self.chain.get((identity,k),{}).get('done') for k in ('long','short','stream'))
        self.by_id[identity].update(status='queued', phase='hydrate_videos' if done else 'inventory')
        return dict(self.by_id[identity])


class OperatorSharedDispatchTests(unittest.TestCase):
    def setUp(self):
        gate=patch.object(collection_shutdown,'_requested',Event()); gate.start(); self.addCleanup(gate.stop)
        self.settings=OperatorJobSettings(None,3,50,1200,initial_load_concurrency=2)

    def run_slice(self, jobs, *, concurrency=2, chunks=20, provider=None, inventory=None, stop=None, **kw):
        videos=Videos(jobs)
        self.videos=videos
        def fetch(ids, reason):
            self.assertNotEqual(get_ident(),jobs.owner)
            if provider:
                provider(ids)
            values=[]
            for value in ids:
                row=video(value)
                row['snippet']['channelId']=next(j['channel_id'] for identity,j in jobs.by_id.items()
                    if value in jobs.windows.get(identity,[]))
                values.append(row)
            return values
        repository=inventory or InventoryRepository()
        native_settle = repository.settle_page
        def settle_page(*a, **kw):
            receipt = native_settle(*a, **kw)
            receipt['observed_video_ids'] = receipt['canonical_video_ids']
            return receipt
        repository.settle_page = settle_page
        def fetch_page(*args, **kwargs):
            self.assertNotEqual(get_ident(),jobs.owner)
            return page()
        return run_operator_jobs_dispatch(jobs, inventory_repository=repository,
            video_service=service(videos,fetch),fetch_page=fetch_page,settings=self.settings,
            concurrency=concurrency,max_chunks=chunks,max_seconds=20,require_ready=lambda:None,
            should_stop=stop or (lambda:False),legacy_chunk=Mock(side_effect=AssertionError('unexpected legacy')),**kw)

    def test_small_channel_settles_while_large_window_is_still_inflight(self):
        jobs=Jobs([200,1]); large,small=jobs.by_id
        release=Event()
        original=jobs.settle_initial_hydration_chunk
        def settle(identity,*a,**kw):
            value=original(identity,*a,**kw)
            if identity==small: release.set()
            return value
        jobs.settle_initial_hydration_chunk=settle
        def provider(ids):
            if ids[0].startswith('v000') and ids[0].endswith('0000000'):
                self.assertTrue(release.wait(5),'small channel was starved by large window')
        result=self.run_slice(jobs,provider=provider)
        self.assertEqual(result['stop_reason'],'idle')
        settled=[e[1] for e in jobs.events if e[0]=='settle']
        self.assertEqual(settled[0],small)
        self.assertEqual(set(settled),{large,small})
        self.assertTrue(all(job['status']=='completed' for job in jobs.by_id.values()))
        self.assertLessEqual(result['peak_ready'],4)
        self.assertLessEqual(result['peak_held'],2)
        self.assertTrue(all(len(ids)<=50 for ids,_ in self.videos.claims))

    def test_concurrency_one_parent_admission_does_not_consume_the_leaf_slot(self):
        jobs=Jobs([101,1])
        result=self.run_slice(jobs,concurrency=1)
        self.assertEqual(result['processed_video_ids'],102)
        self.assertEqual(result['stop_reason'],'idle')
        self.assertEqual(len(self.videos.commits),4)

    def test_hydration_window_is_one_chunk_with_multiple_fifty_id_leaves(self):
        jobs=Jobs([150])
        result=self.run_slice(jobs,chunks=1)
        self.assertEqual((result['chunks_processed'],result['processed_video_ids']),(1,100))
        self.assertEqual(len(self.videos.commits),2)
        self.assertEqual(result['stop_reason'],'max_chunks')
        self.assertEqual(len(next(iter(jobs.remaining.values()))),50)

    def test_inventory_chunk_budget_is_not_multiplied_by_three_formats(self):
        for amount in (1,2,3):
            with self.subTest(amount=amount):
                jobs=Jobs([1],phase='inventory')
                result=self.run_slice(jobs,chunks=amount)
                self.assertEqual(result['chunks_processed'],amount)
                self.assertEqual(result['processed_pages'],amount)
                self.assertEqual(len([e for e in jobs.events if e[0]=='ack']),amount)

    def test_three_chain_formats_are_dispatched_without_a_second_parent_claim(self):
        jobs=Jobs([1],phase='inventory')
        result=self.run_slice(jobs,chunks=3,concurrency=3)
        self.assertEqual(len([e for e in jobs.events if e[0]=='claim']),1)
        self.assertEqual({e[2] for e in jobs.events if e[0]=='ack'},{'long','short','stream'})
        self.assertEqual(result['stop_reason'],'max_chunks')

    def test_unknown_parent_claim_never_retries_or_sends_provider(self):
        jobs=Jobs([1]); jobs.claim_failure=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        result=self.run_slice(jobs)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(len(jobs.events),1)
        self.assertEqual(self.videos.claims,[])

    def test_unknown_parent_settlement_is_called_once(self):
        jobs=Jobs([1]); jobs.settle_failure=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        result = self.run_slice(jobs)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(len([e for e in jobs.events if e[0]=='settle']),1)
        self.assertEqual(len(self.videos.commits),1)

    def test_partial_claim_refunds_only_offered_but_unclaimed_ids(self):
        jobs = Jobs([3])
        original = Videos.claim_initial_video_ids
        def partial_claim(repo, ids, **scope):
            return original(repo, ids[:-1], **scope)
        with patch.object(Videos, 'claim_initial_video_ids', partial_claim):
            result = self.run_slice(jobs, chunks=1)
        self.assertFalse(result['settlement_unknown'])
        settlement = next(e[2] for e in jobs.events if e[0] == 'settle')
        self.assertEqual(settlement['summary']['hydration_unstarted_video_ids'],
                         [next(iter(jobs.windows.values()))[-1]])

    def test_handoff_after_chain_prepare_cancels_parent_without_claim_or_fetch(self):
        jobs = Jobs([1], phase='inventory')
        original = jobs.prepare_inventory_chain
        def prepare(*a):
            result = original(*a)
            collection_shutdown._requested.set()
            return result
        jobs.prepare_inventory_chain = prepare
        inventory = InventoryRepository()
        result = self.run_slice(jobs, inventory=inventory, stop=collection_shutdown.handoff_requested)
        self.assertEqual(result['chunks_processed'], 0)
        self.assertEqual(result['stop_reason'], 'scheduled_handoff')
        self.assertEqual(inventory.events, [])
        self.assertEqual(len([e for e in jobs.events if e[0] == 'cancel_prepared']), 1)
        self.assertEqual(len([e for e in jobs.events if e[0] == 'finish']), 1)

    def test_recovered_quota_ack_prevents_already_ready_sibling_request(self):
        jobs = Jobs([1, 1], phase='inventory')
        first = next(iter(jobs.by_id))
        original = jobs.prepare_inventory_chain
        def prepare(identity, token, kind):
            result = original(identity, token, kind)
            if identity == first:
                result.update(action='reconcile_failure', evidence={'pages_processed': 0})
            return result
        ack = jobs.acknowledge_inventory_chain
        def acknowledge(*a, **kw):
            result = ack(*a, **kw)
            result['ack'] = dict(outcome='quota', provider_attempts=1)
            return result
        jobs.prepare_inventory_chain = prepare
        jobs.acknowledge_inventory_chain = acknowledge
        inventory = InventoryRepository()
        result = self.run_slice(jobs, concurrency=1, inventory=inventory)
        self.assertTrue(result['quota_exhausted'])
        self.assertEqual(result['stop_reason'], 'quota_exhausted')
        self.assertEqual(inventory.events, [])
        self.assertEqual(len([e for e in jobs.events if e[0] == 'ack']), 1)

    def test_reconnect_only_walks_ineligible_prefix_and_shares_chunk_limit(self):
        from test_reconnect_feeder import CandidateRepository, identity
        from collection.reconnect_feeder import ReconnectFeeder
        from channel_reconnect_outcome import ReconnectResponse
        from test_reconnect_dispatch import CHANNEL
        repo = CandidateRepository(73, {identity(i) for i in range(60, 74)})
        def factory(**options):
            return ReconnectFeeder(repo,
                resolve_channel=lambda *a, **kw: ReconnectResponse(channel={'channel_id': CHANNEL}),
                require_ready=lambda: None, require_source=lambda row: None, **options)
        result = self.run_slice(Jobs([]), chunks=3, reconnect_factory=factory)
        self.assertEqual(result['chunks_processed'], 3)
        self.assertEqual(result['stop_reason'], 'max_chunks')
        self.assertEqual(len([e for e in repo.events if e[0] == 'bind']), 3)
        self.assertTrue(any(page['after'] is not None and page['after'].id >= identity(59) for page in repo.pages))

    def test_reconnect_quota_ack_cancels_ready_tail_before_second_request(self):
        from test_reconnect_feeder import CandidateRepository, identity
        from collection.reconnect_feeder import ReconnectFeeder
        from channel_reconnect_outcome import ReconnectResponse
        repo = CandidateRepository(2, {identity(1), identity(2)})
        provider = Mock(return_value=ReconnectResponse(code='provider_quota_exceeded'))
        def factory(**options):
            return ReconnectFeeder(repo,
                resolve_channel=provider, require_ready=lambda: None, require_source=lambda row: None, **options)
        result = self.run_slice(Jobs([]), concurrency=1, reconnect_factory=factory)
        self.assertTrue(result['quota_exhausted'])
        self.assertFalse(result['settlement_unknown'])
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(result['chunks_processed'], 1)

    def test_bound_unsent_inventory_uses_confirmed_cancel_not_page_ack(self):
        from concurrent.futures import Future
        class UnstartedExecutor:
            def __init__(self): self.futures = []
            def submit(self, *args):
                future = Future()
                self.futures.append(future)
                return future
        executor = UnstartedExecutor()
        jobs = Jobs([1], phase='inventory')
        with patch('collection.inventory_dispatch.HotCollectionControlRepository') as control:
            result = self.run_slice(jobs, chunks=1, executor=executor,
                                    stop=lambda: bool(executor.futures))
        self.assertEqual(len([e for e in jobs.events if e[0] == 'cancel_unsent']), 1)
        self.assertEqual(len([e for e in jobs.events if e[0] == 'ack']), 0)
        self.assertEqual(result['chunks_processed'], 0)
        self.assertTrue(all(future.cancelled() for future in executor.futures))
        control.return_value.drain.assert_called_once()

    def test_unknown_parent_ack_drains_other_known_parent_without_replay(self):
        jobs = Jobs([1, 1]); first, second = jobs.by_id
        original = jobs.settle_initial_hydration_chunk
        def settle(identity, *a, **kw):
            if identity == first:
                jobs.events.append(('unknown_ack', identity))
                raise DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
            return original(identity, *a, **kw)
        jobs.settle_initial_hydration_chunk = settle
        with patch('db.hot.hot_collection_control.HotCollectionControlRepository.drain'):
            result = self.run_slice(jobs)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(len([e for e in jobs.events if e[0] == 'unknown_ack']), 1)
        self.assertEqual(len([e for e in jobs.events if e[0] == 'settle' and e[1] == second]), 1)

    def sweep_owner(self, jobs):
        from collection.dispatch import Coordinator
        from collection.reconnect_feeder import ReconnectFeeder
        from operator_jobs import operator_dispatch_turn
        from test_collection_dispatch import Clock, ManualExecutor
        from test_reconnect_feeder import CandidateRepository
        clock, executor = Clock(), ManualExecutor()
        coordinator = Coordinator(executor, 2, clock=clock)
        feeder = ReconnectFeeder(CandidateRepository(0, set()),
            resolve_channel=Mock(), require_ready=lambda: None, require_source=lambda row: None)
        videos = Videos(jobs)
        def fetch(ids, reason):
            rows = [video(value) for value in ids]
            for row in rows:
                row['snippet']['channelId'] = next(iter(jobs.by_id.values()))['channel_id']
            return rows
        owner = operator_dispatch_turn(jobs, inventory_repository=object(),
            video_service=service(videos, fetch), fetch_page=Mock(), settings=self.settings,
            concurrency=2, max_chunks=20, max_seconds=20, require_ready=lambda: None,
            should_stop=lambda: False, legacy_chunk=Mock(), coordinator=coordinator,
            clock=clock, reconnect_factory=lambda **kw: feeder)
        return owner, coordinator, executor, feeder, videos

    def test_paused_ack_keeps_sweep_until_admitted_then_verifies_without_replay(self):
        jobs = Jobs([1])
        owner, coordinator, executor, feeder, videos = self.sweep_owner(jobs)
        owner.advance(ready_limit=4)
        owner.advance(coordinator.step(), ready_limit=4)
        owner.advance(coordinator.step(), ready_limit=0)
        self.assertEqual(len(executor.jobs), 1)
        executor.run(0)
        cursor = (feeder.after, feeder.ceiling, feeder.inspected)
        coordinator.pause()
        owner.advance(coordinator.step(), ready_limit=0)
        self.assertEqual(len(videos.commits), 1)
        self.assertEqual(next(iter(jobs.by_id.values()))['phase'], 'verify')
        self.assertEqual(len([e for e in jobs.events if e[0] == 'settle']), 1)
        self.assertTrue(feeder.exhausted)
        self.assertEqual((feeder.after, feeder.ceiling, feeder.inspected), cursor)
        self.assertFalse(owner.finished)
        self.assertFalse(owner.summary['settlement_unknown'])
        coordinator.pause(False)
        owner.advance(ready_limit=0)
        self.assertFalse(feeder.exhausted)
        for unused in range(3):
            owner.advance(ready_limit=4)
            owner.advance(coordinator.step(), ready_limit=0)
        self.assertTrue(owner.finished)
        self.assertEqual(owner.summary['stop_reason'], 'idle')
        self.assertEqual(next(iter(jobs.by_id.values()))['status'], 'completed')
        self.assertEqual(len([e for e in jobs.events if e[0] == 'verify']), 1)
        self.assertEqual(len(videos.commits), 1)
        self.assertEqual(len(executor.jobs), 1)

    def test_idle_resume_preserves_sweep_until_coordinator_admits(self):
        from collection.dispatch import DispatchBlocked, Work
        for refusal in ('paused', 'stopped', 'blocked'):
            with self.subTest(refusal=refusal):
                owner, coordinator, executor, feeder, unused = self.sweep_owner(Jobs([]))
                owner.advance(ready_limit=4)
                self.assertTrue(owner.finished)
                cursor = (feeder.after, feeder.ceiling, feeder.inspected)
                if refusal == 'paused':
                    coordinator.pause()
                elif refusal == 'stopped':
                    coordinator.stop()
                else:
                    coordinator.offer(Work('failed', 'auto', 'channel', 'long',
                        Mock(side_effect=RuntimeError('known test failure')), Mock()))
                    with self.assertRaises(DispatchBlocked):
                        coordinator.step()
                self.assertFalse(owner.resume_idle())
                self.assertTrue(owner.finished)
                self.assertEqual(owner.summary['stop_reason'], 'idle')
                self.assertTrue(feeder.exhausted)
                self.assertEqual((feeder.after, feeder.ceiling, feeder.inspected), cursor)
                if refusal == 'paused':
                    coordinator.pause(False)
                    self.assertTrue(owner.resume_idle())
                    self.assertFalse(owner.finished)
                    self.assertFalse(feeder.exhausted)

    def test_standalone_owner_advance_failure_does_not_retry_in_exception_handler(self):
        import operator_jobs
        original = operator_jobs.operator_dispatch_turn
        calls = []
        def factory(*args, **kw):
            owner = original(*args, **kw)
            def failed(*a, **k):
                calls.append(1)
                raise OSError('unknown owner operation')
            owner.advance = failed
            return owner
        with patch.object(operator_jobs, 'operator_dispatch_turn', side_effect=factory):
            result = self.run_slice(Jobs([]))
        self.assertEqual(len(calls), 1)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(result['chunks_processed'], 0)

    def test_legacy_unknown_parent_write_is_not_drained_retried_or_refunded(self):
        jobs = Jobs([1])
        for value in jobs.by_id.values():
            value['initial_scope_version'] = 0
        legacy = Mock(side_effect=DatabaseUnavailable('database_connection_failed', outcome_unknown=True))
        with patch('db.hot.hot_collection_control.HotCollectionControlRepository.drain') as drain:
            result = run_operator_jobs_dispatch(jobs, inventory_repository=object(), video_service=object(),
                fetch_page=Mock(), settings=self.settings, concurrency=1, max_chunks=1, max_seconds=20,
                require_ready=lambda: None, should_stop=lambda: False, legacy_chunk=legacy)
        legacy.assert_called_once()
        drain.assert_not_called()
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(result['chunks_processed'], 0)
        self.assertEqual(next(iter(jobs.by_id.values()))['status'], 'running')

    def test_actual_worker_slice_binds_existing_services_and_exact_legacy_parent(self):
        import importlib.util
        import os
        core = SimpleNamespace(database=object(), process_database_resources=Mock(),
            API_KEY='synthetic-key', YOUTUBE_QUOTA_ATTEMPT_RECORDER=object(),
            YOUTUBE_API_TIMEOUT_SECONDS=17, COLLECTION_SCHEDULES=object())
        spec = importlib.util.spec_from_file_location('shared_worker_composition_test',
            ROOT / 'application/scripts/run_operator_job_worker.py')
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'core': core}):
            spec.loader.exec_module(worker)
        jobs = Jobs([])
        callbacks = {key: object() for key in ('inventory_repository', 'video_service', 'fetch_page',
                                              'run_stats', 'run_initial_inventory_chunk', 'run_initial_stats')}
        with patch.object(worker, 'run_operator_jobs_dispatch', return_value={'status': 'idle'}) as driver, \
             patch.object(worker, 'run_one_operator_job_chunk') as legacy, \
             patch('runtime.collection.bind_reconnect_feeder') as binder, \
             patch('db.user_work.reconnect_admission.configured_channel_add_admission',
                   side_effect=AssertionError('reconnect must not load manual admission')) as admission, \
             patch.dict(os.environ, {'LIC_YOUTUBE_REQUEST_CONCURRENCY': '3'}):
            result = worker._run_dispatch_slice(jobs, callbacks, self.settings,
                                               max_chunks=4, max_seconds=9, clock=lambda: 0)
            options = driver.call_args.kwargs
            self.assertEqual(result, {'status': 'idle'})
            self.assertEqual((options['concurrency'], options['max_chunks'], options['max_seconds']), (3, 4, 9))
            self.assertIs(options['video_service'], callbacks['video_service'])
            observer, stopped = Mock(), Mock(return_value=False)
            options['reconnect_factory'](on_settled=observer, should_stop=stopped)
            self.assertIs(binder.call_args.args[0], jobs.database)
            self.assertIs(binder.call_args.kwargs['on_settled'], observer)
            self.assertIs(binder.call_args.kwargs['should_stop'], stopped)
            admission.assert_not_called()
            self.assertNotIn('admission', binder.call_args.kwargs)
            selected = {'id': 'selected-parent'}
            options['legacy_chunk'](selected)
            self.assertIs(legacy.call_args.kwargs['claimed_job'], selected)

    def test_no_work_and_handoff_send_nothing(self):
        jobs=Jobs([1])
        result=self.run_slice(jobs,stop=lambda:True)
        self.assertEqual(result['stop_reason'],'scheduled_handoff')
        self.assertEqual(jobs.events,[])


if __name__=='__main__': unittest.main()
