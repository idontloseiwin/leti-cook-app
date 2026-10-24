"""Actual shared Coordinator and real video units; no network or database."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).parent)]
import collection_shutdown
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.cycle import AutomaticDispatchWindow, run_common_operator_phase
from collection.common_feeder import CommonAutoFeeder
from collection.dispatch import Coordinator, Prepared, Ack, Work
from operator_jobs import OperatorJobSettings, operator_dispatch_turn
from hot_runtime import HotRuntimeUnavailable
from test_common_video_dispatch import Repository as RegularRepository
from test_operator_shared_dispatch import Jobs, Videos
from test_initial_video_parallel import service, video, CHANNEL
from test_collection_dispatch import ManualExecutor, Clock
from youtube_errors import YouTubeDailyQuotaExceededError


class DueRepository(RegularRepository):
    def __init__(self, count):
        super().__init__()
        self.remaining = count
        self.run_ids, self.pages, self.preparations = [], [], []
        self.sent_ids = 0

    def reconcile_typed(self, kind, **kw):
        assert get_ident() == self.owner
        self.preparations.append((kind, kw))
        return {'affected_count': 0}

    def due_candidate_page(self, **request):
        assert get_ident() == self.owner
        self.pages.append(request)
        physical = request['after'] is None
        operation = 'counters' if self.mode == 'counters' else 'metadata'
        frontier = request.get('frontiers', {}).get(operation, {})
        eligible = (physical and request['video_type'] == 'long' and self.remaining
                    and request.get('operation') in (None, operation)
                    and not frontier.get('exhausted', False)
                    and CHANNEL > (frontier.get('after') or ''))
        rows = ([dict(channel_id=CHANNEL, video_type='long', operation=operation,
                      ready_count=min(50, self.remaining), due_at=1)] if eligible else [])
        return dict(rows=rows, next_after=CHANNEL if physical else request['after'],
                    inspected_count=int(physical), scan_exhausted=int(physical) < request['limit'])

    def claim_due_batch(self, **request):
        assert get_ident() == self.owner
        self.run_ids.append(request['run_id'])
        batch = self.claim()
        count = min(request['limit'], self.remaining)
        batch['rows'] = [replace(batch['rows'][0], video_id=f'a{self.sent_ids + n:010d}') for n in range(count)]
        self.remaining -= count
        self.sent_ids += count
        batch['inspected_count'] = count
        return batch


class SharedPhaseTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(collection_shutdown, '_requested', Event())
        guard.start(); self.addCleanup(guard.stop)
        self.auto_network, self.operator_network = [], []
        self.owner_thread = get_ident()
        self.operator_settings = OperatorJobSettings(None, 3, 50, 1200, initial_load_concurrency=2)

    def run_phase(self, *, count=151, limit=151, chunks=1, jobs=None, concurrency=2,
                  auto_hook=None, operator_hook=None, ready=None, stop=None, cursor=None, progress=None, clock=None, wake=None,
                  fallback_required=False):
        self.jobs = jobs or Jobs([200])
        self.regular = DueRepository(count)
        self.videos = Videos(self.jobs)
        regular = service(self.regular, Mock())
        def metadata(ids, reason):
            self.assertNotEqual(get_ident(), self.owner_thread)
            self.auto_network.append(('general', tuple(ids)))
            if auto_hook: auto_hook(ids)
            items = [video(value) for value in ids]
            if fallback_required:
                for item in items: item['statistics'].pop('viewCount')
            return items
        def counters(ids, reason):
            self.assertNotEqual(get_ident(), self.owner_thread)
            self.auto_network.append(('stats', tuple(ids)))
            return {'items': [dict(id=value, views=123, like_count=7, comment_count=2) for value in ids],
                    'summary': {'failed_ids': []}}
        regular.fetch_metadata, regular.fetch_counters = metadata, counters
        def initial(ids, reason):
            self.assertNotEqual(get_ident(), self.owner_thread)
            self.operator_network.append(tuple(ids))
            if operator_hook: operator_hook(ids)
            items = []
            for value in ids:
                item = video(value)
                item['snippet']['channelId'] = next(j['channel_id'] for identity, j in self.jobs.by_id.items()
                    if value in self.jobs.windows.get(identity, []))
                items.append(item)
            return items
        self.initial_service = service(self.videos, initial)
        def factory(**options):
            return operator_dispatch_turn(self.jobs, inventory_repository=object(),
                video_service=self.initial_service, fetch_page=Mock(), settings=self.operator_settings, clock=clock,
                concurrency=concurrency, max_chunks=chunks, max_seconds=20,
                legacy_chunk=Mock(side_effect=AssertionError('legacy unused')), **options)
        with patch('collection.common_video_dispatch._drain_unsent') as regular_drain, \
                patch('collection.initial_video_hydration._drain_unsent', side_effect=self.videos.release):
            self.regular_drain = regular_drain
            return run_common_operator_phase(regular, limit=limit, concurrency=concurrency,
                worker_id='shared-test', require_ready=ready or (lambda: None),
                should_stop=stop or (lambda: False), operator_factory=factory,
                cursor=cursor or CommonCandidateCursor(), progress_callback=progress,
                **({'clock': clock} if clock is not None else {}), wake_revision=wake or (lambda: None))

    def test_global_stop_cancels_ready_before_draining_known_held_response(self):
        from collection.cycle import AutomaticDispatchWindow
        executor=ManualExecutor();clock=Clock();settled=[]
        operator=SimpleNamespace(advance=Mock(),stop=Mock(),resume_idle=Mock(),summary={},finished=True)
        window=AutomaticDispatchWindow(concurrency=1,require_ready=lambda:None,should_stop=lambda:False,
            operator_factory=lambda **kwargs:operator,executor=executor,clock=clock)
        owner=window.coordinator
        owner.offer(Work('held','auto',CHANNEL,'long',lambda:Prepared(lambda cancel:'known',clock()+100),
            lambda completion:settled.append(completion.value) or Ack()))
        owner.offer(Work('ready','operator',CHANNEL,'short',Mock(side_effect=AssertionError('must not prepare')),
            Mock(side_effect=AssertionError('ready work has no settlement'))))
        owner.step();executor.run(0)
        self.assertEqual((owner.snapshot().ready,owner.snapshot().held),(1,1))
        window.stop_all(dict(quota_exhausted=True))
        window.close()
        self.assertEqual(settled,['known'])
        self.assertFalse(owner.keys())
        self.assertFalse(window._drain_failed)
        self.assertFalse(window.stop_flags.get('settlement_unknown'))

    def test_operator_progresses_during_auto_and_own_chunk_cap_does_not_stop_auto(self):
        initial_started = Event()
        def auto(ids):
            self.assertTrue(initial_started.wait(4), 'operator was starved by automatic phase')
        result = self.run_phase(auto_hook=auto, operator_hook=lambda ids: initial_started.set())
        self.assertEqual(result['updated'], 151)
        self.assertEqual(result['operator']['chunks_processed'], 1)
        self.assertEqual(result['operator']['processed_video_ids'], 100)
        self.assertEqual(result['operator']['stop_reason'], 'max_chunks')
        self.assertFalse(result.get('infrastructure_error'))
        self.assertEqual(len(set(self.regular.run_ids)), 1)
        self.assertEqual([len(ids) for kind, ids in self.auto_network if kind == 'general'], [50, 50, 50, 1])
        self.assertFalse([ids for kind, ids in self.auto_network if kind == 'stats'])
        self.assertEqual([kind for kind, _ in self.regular.preparations], ['short', 'stream'])
        self.assertEqual(self.regular.claimed, 4)
        self.regular_drain.assert_not_called()

    def test_c1_bookkeeping_preserves_common_continuation_and_operator_credit(self):
        result = self.run_phase(count=51, limit=51, concurrency=1, fallback_required=True)
        self.assertEqual(result['updated'], 51)
        self.assertGreater(len(self.operator_network), 0)
        self.assertEqual([kind for kind, _ in self.auto_network], ['general', 'stats', 'general', 'stats'])
        self.assertLessEqual(result['operator']['chunks_processed'], 1)
        self.assertLessEqual(result['operator']['peak_ready'], 2)

    def test_common_quota_ack_stops_operator_ready_and_all_later_provider_work(self):
        def quota(ids):
            raise YouTubeDailyQuotaExceededError('synthetic')
        result = self.run_phase(auto_hook=quota, concurrency=1)
        self.assertTrue(result['quota_exhausted'])
        self.assertEqual(len(self.auto_network), 1)
        self.assertFalse(self.operator_network)
        self.assertFalse(any(event[0] == 'claim' for event in self.jobs.events))

    def test_operator_quota_ack_stops_automatic_ready_work(self):
        events = []
        def quota(ids):
            events.append('quota')
            raise YouTubeDailyQuotaExceededError('synthetic')
        result = self.run_phase(operator_hook=quota,
            auto_hook=lambda ids: events.append('auto'), concurrency=1)
        self.assertTrue(result['quota_exhausted'])
        self.assertEqual(len(self.operator_network), 1)
        # Preparing the operator's bootstrap and then its provider unit takes
        # separate fair turns. Denser candidate pages may make another auto
        # batch ready between them; no auto request may follow the quota ACK.
        self.assertLessEqual(events.count('auto'), 2)
        self.assertEqual(events[-1], 'quota')

    def test_unknown_operator_parent_commit_is_not_retried_or_refunded(self):
        jobs = Jobs([1]); jobs.settle_failure = OSError('unknown commit')
        result = self.run_phase(jobs=jobs, concurrency=1)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(len([e for e in jobs.events if e[0] == 'settle']), 1)
        self.assertEqual(result['operator']['chunks_processed'], 0)
        self.assertEqual(len(self.operator_network), 1)

    def test_hot_denial_after_general_drains_known_auto_claim_and_stops_operator(self):
        deny = Event()
        def ready():
            if deny.is_set(): raise HotRuntimeUnavailable()
        result = self.run_phase(auto_hook=lambda ids: deny.set(), ready=ready, concurrency=1,
            fallback_required=True)
        self.assertTrue(result['hot_runtime_interrupted'])
        self.assertTrue(result['infrastructure_error'])
        self.assertEqual([kind for kind, _ in self.auto_network], ['general'])
        self.regular_drain.assert_called_once()
        self.assertFalse(self.operator_network)

    def test_handoff_before_work_has_no_claims_and_no_provider(self):
        result = self.run_phase(stop=lambda: True)
        self.assertTrue(result['yield_requested'])
        self.assertFalse(self.regular.run_ids)
        self.assertFalse(self.jobs.events)
        self.assertFalse(self.auto_network or self.operator_network)

    def test_unknown_progress_write_is_not_called_again_during_drain_or_finish(self):
        progress = Mock(side_effect=OSError('unknown progress acknowledgement'))
        result = self.run_phase(progress=progress, concurrency=1)
        self.assertTrue(result['settlement_unknown'])
        progress.assert_called_once()

    def test_owner_advance_failure_is_never_called_again_by_stop_drain(self):
        original = operator_dispatch_turn
        calls = []
        def factory(*args, **kw):
            owner = original(*args, **kw)
            def failed(*a, **k):
                calls.append(1)
                raise OSError('unknown owner operation')
            owner.advance = failed
            return owner
        with patch(__name__ + '.operator_dispatch_turn', side_effect=factory):
            result = self.run_phase(concurrency=1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result['settlement_unknown'])
        self.assertFalse(self.jobs.events)
        self.assertFalse(self.operator_network)

    def test_failed_known_drain_is_called_once_and_keeps_unknown_outcome(self):
        handoff = Event()
        with patch.object(Coordinator, 'drain_known', side_effect=OSError('lost drain acknowledgement')) as drain:
            result = self.run_phase(concurrency=1, auto_hook=lambda ids: handoff.set(), stop=handoff.is_set)
        drain.assert_called_once()
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual([kind for kind, _ in self.auto_network], ['general'])
        self.assertFalse(self.operator_network)

    def test_late_job_wake_reopens_idle_window_while_auto_is_still_held(self):
        jobs = Jobs([])
        empty_read = Event()
        original = jobs.candidate_page
        def candidates(**kw):
            result = original(**kw)
            if not jobs.by_id: empty_read.set()
            return result
        jobs.candidate_page = candidates
        clock = Clock()
        revision = [0]
        added = False
        def auto(ids):
            nonlocal added
            if added: return
            self.assertTrue(empty_read.wait(4))
            small = Jobs([1])  # Simulates a user enqueue, not a collector DB call.
            jobs.by_id.update(small.by_id); jobs.remaining.update(small.remaining)
            jobs.serial = small.serial
            added = True
            revision[0] += 1
            clock.value += 3
        automatic_done_at_initial = []
        result = self.run_phase(jobs=jobs, auto_hook=auto, clock=clock, wake=lambda: revision[0],
            operator_hook=lambda ids: automatic_done_at_initial.append(
                sum(len(rows) for operation, rows in self.regular.writes)))
        self.assertTrue(added)
        self.assertLess(automatic_done_at_initial[0], 151)
        self.assertEqual(result['operator']['processed_video_ids'], 1)
        self.assertEqual(result['operator']['chunks_processed'], 1)
        self.assertEqual(result['updated'], 151)
        self.assertTrue(self.operator_network)

    def test_idle_resume_preserves_time_and_chunk_window_and_never_reopens_local_stop(self):
        executor, clock = ManualExecutor(), Clock()
        coordinator = Coordinator(executor, 1, clock=clock)
        owner = operator_dispatch_turn(Jobs([]), inventory_repository=object(), video_service=object(),
            fetch_page=Mock(), settings=self.operator_settings, concurrency=1, max_chunks=1,
            max_seconds=4, require_ready=lambda: None, should_stop=lambda: False,
            legacy_chunk=Mock(), coordinator=coordinator, clock=clock)
        owner.advance()
        self.assertTrue(owner.finished)
        clock.value += 3
        self.assertTrue(owner.resume_idle())
        owner.advance()
        self.assertTrue(owner.finished)
        clock.value += 1
        self.assertFalse(owner.resume_idle())
        owner.stop('time_budget')
        self.assertFalse(owner.resume_idle())

    def test_operator_local_time_stop_does_not_cancel_unrelated_auto_descriptor(self):
        executor, clock = ManualExecutor(), Clock()
        coordinator = Coordinator(executor, 1, clock=clock)
        sent = []
        coordinator.offer(Work('auto-unrelated', 'auto', CHANNEL, 'long',
            lambda: Prepared(lambda cancel: sent.append(True), 500), lambda done: Ack()))
        owner = operator_dispatch_turn(Jobs([]), inventory_repository=object(), video_service=object(),
            fetch_page=Mock(), settings=self.operator_settings, concurrency=1, max_chunks=1,
            max_seconds=1, require_ready=lambda: None, should_stop=lambda: False,
            legacy_chunk=Mock(), coordinator=coordinator, clock=clock)
        clock.value += 2
        owner.advance(ready_limit=0)
        self.assertTrue(owner.finished)
        self.assertTrue(coordinator.contains('auto-unrelated'))
        self.assertFalse(coordinator.snapshot().paused)
        coordinator.step(); executor.run(0); coordinator.step()
        self.assertEqual(sent, [True])


class RefillPhaseTests(unittest.TestCase):
    """Real driver/feeder/service; controlled pending futures expose wait placement."""
    def setUp(self):
        guard = patch.object(collection_shutdown, '_requested', Event())
        guard.start(); self.addCleanup(guard.stop)

    def run_manual_phase(self, repository, *, limit, concurrency=8, churn=False, terminal=None):
        executor, clock = ManualExecutor(), Clock()
        repository.mode = 'counters'
        collector = service(repository, Mock(side_effect=AssertionError('counters only')))
        fetched = []
        def counters(ids, reason):
            fetched.append(tuple(ids))
            if terminal == 'quota' and len(fetched) == 1:
                raise YouTubeDailyQuotaExceededError('synthetic')
            return {'items': [dict(id=value, views=123, like_count=7, comment_count=2) for value in ids],
                'summary': {'failed_ids': []}}
        collector.fetch_counters = counters
        def build(item, recorded_at):
            self.assertEqual(get_ident(), repository.owner)
            return {'snapshot': {**item, 'recorded_at': recorded_at}}
        collector.build_counter_payload = build
        events, waits = [], []
        def advance(turn=None, *, ready_limit=0):
            if not ready_limit:
                return
            events.append('operator')
            if terminal and len(executor.jobs) >= 2 and not executor.jobs[0][0].done():
                executor.run(0)
            if churn:
                # Keep the first slow request pending, complete later work out of
                # order so productive turns do not necessarily increase held.
                for index, (future, *_) in enumerate(executor.jobs):
                    if index and not future.done():
                        executor.run(index)
        operator = SimpleNamespace(advance=advance, stop=Mock(), resume_idle=Mock())
        window = AutomaticDispatchWindow(concurrency=concurrency, require_ready=lambda: None,
            should_stop=lambda: False, operator_factory=lambda **kw: operator,
            executor=executor, clock=clock)
        self.addCleanup(window.close)
        state = AutoPassState(limit, concurrency=concurrency)
        feeder = CommonAutoFeeder(collector, state=state, cursor=CommonCandidateCursor(),
            worker_id='refill-test', require_ready=window.ready,
            should_stop=window.stopping, on_settled=window.stop_all)
        original_offer = feeder.offer_to
        def offer(owner, *, limit=None):
            if limit:
                events.append('auto')
            return original_offer(owner, limit=limit)
        feeder.offer_to = offer
        def wait(maximum):
            self.assertEqual(maximum, .05)
            waits.append(dict(snapshot=window.coordinator.snapshot(), claims=repository.claimed,
                submitted=len(executor.jobs), events=tuple(events)))
            for index in reversed(range(len(executor.jobs))):
                if not executor.jobs[index][0].done():
                    executor.run(index)
        window.coordinator.wait = wait
        with patch('collection.common_video_dispatch._drain_unsent'):
            result = window.run_phase(feeder)
        if terminal == 'unknown':
            self.assertTrue(window.coordinator.snapshot().blocked)
            self.assertEqual(window.coordinator.snapshot().held, 1)
            self.assertIs(feeder.cursor._active, state)
            self.assertFalse(window.checkpoint_ready)
        else:
            self.assertFalse(window.coordinator.keys())
            self.assertIsNone(feeder.cursor._active)
        self.assertLessEqual(state.inspected, state.scan_limit)
        self.assertLessEqual(state.candidate_inspected, state.candidate_scan_limit)
        if terminal is None:
            self.assertFalse(result.get('settlement_unknown'))
            self.assertEqual(result['errors'], 0)
        else:
            self.assertEqual(repository.claimed, 2)
            self.assertEqual(len(executor.jobs), 2)
            # The already admitted sibling may finish; no third claim or
            # provider submission can follow the known terminal outcome.
            self.assertLessEqual(len(fetched), 2)
        return result, waits

    def test_candidate_progress_refills_within_eight_turn_bound_and_keeps_operator_turns(self):
        repository = DueRepository(450)
        result, waits = self.run_manual_phase(repository, limit=450)
        first = waits[0]
        self.assertEqual((first['submitted'], first['snapshot'].held), (4, 4))
        # One finite advisory-only turn between claims shares the existing C8 burst.
        self.assertEqual(len(first['events']), 16)
        self.assertEqual(first['claims'], 4)
        self.assertEqual(first['events'][:16], tuple(
            event for turn in range(8)
            for event in (('auto', 'operator') if turn % 2 == 0 else ('operator', 'auto'))))
        self.assertEqual((result['claimed'], result['updated']), (450, 450))
        self.assertEqual(repository.sent_ids, 450)

    def test_empty_candidate_turn_waits_with_free_credits(self):
        result, waits = self.run_manual_phase(DueRepository(50), limit=100)
        first = waits[0]
        self.assertEqual((first['submitted'], first['snapshot'].held), (1, 1))
        self.assertEqual(first['events'], ('auto', 'operator', 'operator', 'auto')*2)
        self.assertEqual((result['claimed'], result['updated']), (50, 50))

    def test_offered_but_locked_claim_waits_without_spinning(self):
        class LockedRepository(DueRepository):
            def claim_due_batch(self, **request):
                if not self.claimed:
                    return super().claim_due_batch(**request)
                self.run_ids.append(request['run_id'])
                self.claimed += 1
                self.remaining = 0
                return dict(rows=[], inspected_count=1000, paused_count=0, scan_exhausted=False)
        repository = LockedRepository(100)
        result, waits = self.run_manual_phase(repository, limit=100)
        first = waits[0]
        self.assertEqual((first['claims'], first['submitted'], first['snapshot'].held), (2, 1, 1))
        self.assertEqual(repository.claimed, 2)
        self.assertEqual((result['claimed'], result['inspected'], result['updated']), (50, 1050, 50))

    def test_c8_refill_settles_quota_before_preparing_the_next_descriptor(self):
        result, _ = self.run_manual_phase(DueRepository(450), limit=450, terminal='quota')
        self.assertTrue(result['quota_exhausted'])
        self.assertFalse(result.get('settlement_unknown'))

    def test_c8_refill_keeps_unknown_credit_without_replaying_the_write(self):
        repository = DueRepository(450)
        repository.complete_counters = Mock(side_effect=OSError('unknown commit'))
        result, _ = self.run_manual_phase(repository, limit=450, terminal='unknown')
        repository.complete_counters.assert_called_once()
        self.assertTrue(result['settlement_unknown'])

    def test_productive_churn_has_finite_refill_burst_and_c1_still_waits(self):
        for concurrency in (1, 8):
            with self.subTest(concurrency=concurrency):
                result, waits = self.run_manual_phase(DueRepository(600), limit=600,
                    concurrency=concurrency, churn=True)
                first = waits[0]
                self.assertEqual(first['submitted'], 1 if concurrency == 1 else 4)
                self.assertEqual(first['snapshot'].held, min(concurrency, 2))
                self.assertEqual((result['claimed'], result['updated']), (600, 600))


if __name__ == '__main__':
    unittest.main()
