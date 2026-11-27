"""Real threads verify bounded waiting, cancellation, release, and authorization."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from unittest.mock import patch
from research_query import frozen_service, service as service_module
from research_query.admission import LimitedReadAdmission, ReadAdmission
from research_query.frozen import FrozenStore
from research_query.validation import QueryRejected, canonical
from test_research_query_service import Repository, QueryService, CONTEXT, NOW
from test_research_query_contract import query
import test_research_frozen as frozen_fixture


class AdmissionTests(unittest.TestCase):
    def test_capacity_queue_timeout_cancellation_and_exception_release(self):
        admission = ReadAdmission(concurrency=1, max_waiting=1)
        cancel, waiting = threading.Event(), threading.Event()
        def waiter():
            waiting.set()
            with admission.enter(deadline=time.monotonic()+2, cancelled=cancel.is_set):
                self.fail('cancelled waiter entered')
        with ThreadPoolExecutor(max_workers=1) as pool:
            with admission.enter(deadline=time.monotonic()+2, cancelled=lambda: False):
                future = pool.submit(waiter)
                self.assertTrue(waiting.wait(1))
                limit = time.monotonic()+1
                while time.monotonic() < limit:
                    with admission._condition:
                        if admission._waiting: break
                    time.sleep(.001)
                with self.assertRaisesRegex(QueryRejected, 'query_busy'):
                    with admission.enter(deadline=time.monotonic()+1, cancelled=lambda: False): pass
                cancel.set()
                with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'): future.result(1)
                with self.assertRaisesRegex(QueryRejected, 'query_busy'):
                    with admission.enter(deadline=time.monotonic()+.02, cancelled=lambda: False): pass
            with self.assertRaisesRegex(RuntimeError, 'work_failed'):
                with admission.enter(deadline=time.monotonic()+1, cancelled=lambda: False):
                    raise RuntimeError('work_failed')
            with admission.enter(deadline=time.monotonic()+1, cancelled=lambda: False): pass
        self.assertEqual((admission._active, len(admission._waiting)), (0, 0))

    def test_concurrent_reads_never_exceed_limit(self):
        admission = ReadAdmission(concurrency=2)
        lock, active, peak = threading.Lock(), 0, 0
        def read(_):
            nonlocal active, peak
            with admission.enter(deadline=time.monotonic()+3, cancelled=lambda: False):
                with lock: active += 1; peak = max(peak, active)
                time.sleep(.01)
                with lock: active -= 1
        with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(read, range(16)))
        self.assertEqual(peak, 2)

    def test_access_and_preparation_rechecked_after_wait_before_sql(self):
        repository, context = Repository(), CONTEXT
        @contextmanager
        def enter(**kwargs):
            nonlocal context
            context = replace(CONTEXT, access_revision='revoked')
            yield
        admission = type('Admission', (), {'enter': staticmethod(enter)})()
        service = QueryService(repository, lambda: context, clock=lambda: NOW, admission=admission)
        with self.assertRaisesRegex(QueryRejected, 'cursor_context_changed'):
            service.execute(canonical(query()))
        self.assertEqual(repository.reads, 0)

    def test_invalid_configuration(self):
        for kwargs in ({'concurrency': 0}, {'concurrency': True}, {'max_waiting': -1}):
            with self.assertRaises(ValueError): ReadAdmission(**kwargs)


class FrozenWaitBudgetTests(unittest.TestCase):
    """A frozen build's slot wait is bounded on its own and is not its work.

    Real threads and one shared slot; the 4 s / 8 s limits are scaled down."""
    def setUp(self):
        self.admission = ReadAdmission(concurrency=1)
        self.repo = frozen_fixture.Repository()
        self.store = FrozenStore(clock=lambda: frozen_fixture.NOW, monotonic=lambda: 1000.0, auto_prune=False)
        self.service = QueryService(self.repo, lambda: frozen_fixture.CONTEXT, clock=lambda: frozen_fixture.NOW,
                                    admission=self.admission, frozen=self.store)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.addCleanup(self.pool.shutdown)

    def limits(self, wait, work):
        # create=True: the same test runs against a version without a separate wait limit.
        for target in (patch.object(frozen_service, 'FROZEN_WAIT_SECONDS', wait, create=True),
                       patch.object(frozen_service, 'FROZEN_BUILD_SECONDS', work),
                       patch.object(service_module, 'FROZEN_BUILD_SECONDS', work)):
            target.start()
            self.addCleanup(target.stop)

    def hold_slot(self, *, seconds=None, release=None):
        """Another read holding the only slot; returns once it is held."""
        held = threading.Event()
        def other_read():
            with self.admission.enter(deadline=time.monotonic()+30, cancelled=lambda: False):
                held.set()
                if release is not None: release.wait(seconds)
                else: time.sleep(seconds)
        future = self.pool.submit(other_read)
        self.assertTrue(held.wait(2))
        return future

    def build(self):
        return self.service.execute(canonical(frozen_fixture.frozen_query()))

    def refused(self, call):
        with self.assertRaises(QueryRejected) as caught:
            call()
        return caught.exception.payload()

    def wait_for_build_queue(self, count):
        end = time.monotonic() + 1
        while time.monotonic() < end:
            with self.store._builds._condition:
                if len(self.store._builds._waiting) == count:
                    return
            time.sleep(.002)
        self.fail('unexpected number of queued builds')

    def test_builds_wait_in_order_without_borrowing_a_read_slot(self):
        # A peer MCP read may occupy one shared slot. A queued owner build
        # still leaves the second slot available to an ordinary owner read.
        shared = ReadAdmission(concurrency=2)
        self.service.admission = shared
        agent = LimitedReadAdmission(shared, concurrency=1)
        self.store = FrozenStore(max_bytes=1000000, max_session_bytes=750000,
                                 clock=lambda: frozen_fixture.NOW, auto_prune=False)
        self.service.frozen = self.store
        self.limits(wait=3, work=2)
        order = []
        local = threading.local()
        self.repo.on_read = lambda: order.append(local.index)
        def build(index):
            local.index = index
            return self.build()
        gate = dict(deadline=time.monotonic()+3, cancelled=lambda: False)
        with agent.enter(**gate):
            with self.store.build_slot(**gate):
                builds = []
                for index in (1, 2):
                    builds.append(self.pool.submit(build, index))
                    self.wait_for_build_queue(index)
                self.assertEqual(self.repo.reads, 0)
                with shared.enter(**gate):
                    self.assertEqual(shared._active, 2)
            self.assertEqual([job.result(2)['page']['total_count'] for job in builds], [120, 120])
        self.assertEqual(order, [1, 2])

    def test_build_and_read_admission_share_the_total_wait_deadline(self):
        # Capture absolute deadlines. The shared slot may use only the time
        # left after waiting for build capacity.
        self.limits(wait=2, work=3)
        received = []
        original = self.admission.enter
        @contextmanager
        def read_slot(*, deadline, cancelled):
            received.append(deadline)
            with original(deadline=deadline, cancelled=cancelled):
                yield
        self.admission.enter = read_slot
        gate = dict(deadline=time.monotonic()+4, cancelled=lambda: False)
        with patch.object(frozen_service, 'FROZEN_BUILD_QUEUE_SECONDS', 1):
            with self.store.build_slot(**gate), self.store.build_slot(**gate):
                started = time.monotonic()
                job = self.pool.submit(self.build)
                self.wait_for_build_queue(1)
            self.assertEqual(job.result(2)['page']['total_count'], 120)
        self.assertGreaterEqual(received[0], started+1)
        self.assertLess(received[0], started+1.3)
        self.assertGreater(received[1], received[0]+1)

    def test_cancelled_build_has_no_rows_and_releases_its_queue_position(self):
        self.limits(wait=3, work=2)
        cancel = threading.Event()
        gate = dict(deadline=time.monotonic()+3, cancelled=lambda: False)
        with self.store.build_slot(**gate), self.store.build_slot(**gate):
            future = self.pool.submit(self.service.execute, canonical(frozen_fixture.frozen_query()), cancelled=cancel.is_set)
            self.wait_for_build_queue(1)
            cancel.set()
            with self.assertRaisesRegex(QueryRejected, 'query_budget_exceeded'):
                future.result(1)
            self.assertEqual(self.repo.reads, 0)
            self.assertEqual(self.store.usage()['sessions'], 0)
            self.assertEqual(len(self.store._builds._waiting), 0)
        self.assertEqual(self.build()['page']['total_count'], 120)

    def test_work_budget_starts_when_the_slot_is_granted(self):
        self.limits(wait=2.0, work=1.5)
        budgets = []
        def read():
            # What the read statement gets: the previous timeout included the queue delay.
            budgets.append(self.read_deadline - time.monotonic())
            time.sleep(.8)
        original = self.repo.read_frozen
        def read_frozen(plan, *, deadline, cancelled):
            self.read_deadline = deadline
            return original(plan, deadline=deadline, cancelled=cancelled)
        self.repo.read_frozen, self.repo.on_read = read_frozen, read
        other = self.hold_slot(seconds=.8)
        started = time.monotonic()
        page = self.build()
        waited_and_worked = time.monotonic() - started
        other.result(2)
        self.assertEqual(page['page']['total_count'], 120)
        self.assertGreater(budgets[0], 1.2)
        self.assertGreater(waited_and_worked, 1.5)  # More than the work budget alone: the wait is not in it.

    def test_wait_is_bounded_on_its_own_and_named_busy(self):
        self.limits(wait=.3, work=5.0)
        release = threading.Event()
        other = self.hold_slot(seconds=3, release=release)
        started = time.monotonic()
        refusal = self.refused(self.build)
        waited = time.monotonic() - started
        release.set()
        other.result(2)
        self.assertEqual(refusal, {'code': 'query_busy', 'path': None})
        self.assertGreaterEqual(waited, .29)
        self.assertLess(waited, 1.5)
        self.assertEqual(self.repo.reads, 0)
        # The refused build released its build slot and stored nothing.
        self.assertEqual(self.store.usage()['sessions'], 0)
        self.assertEqual(self.build()['page']['total_count'], 120)

    def test_work_past_its_budget_is_named_time_and_bounded(self):
        self.limits(wait=2.0, work=.4)
        self.repo.on_read = lambda: time.sleep(.6)
        other = self.hold_slot(seconds=.3)
        started = time.monotonic()
        refusal = self.refused(self.build)
        elapsed = time.monotonic() - started
        other.result(2)
        self.assertEqual(refusal, {'code': 'query_budget_exceeded', 'path': None, 'limit': 'time'})
        self.assertLess(elapsed, 2.0 + .4)
        self.assertEqual(self.store.usage()['sessions'], 0)

    def test_recheck_slot_wait_is_part_of_the_work_budget(self):
        # Worst answer: the wait limit plus the work budget, never more.
        self.limits(wait=.5, work=.8)
        release, queued = threading.Event(), threading.Event()
        def queue_behind_the_read():
            def other_read():
                queued.set()
                with self.admission.enter(deadline=time.monotonic()+30, cancelled=lambda: False):
                    release.wait(3)
            self.future = self.pool.submit(other_read)
            self.assertTrue(queued.wait(2))
            limit = time.monotonic() + 1
            while time.monotonic() < limit:
                with self.admission._condition:
                    if self.admission._waiting: break
                time.sleep(.001)
        self.repo.on_read = queue_behind_the_read
        started = time.monotonic()
        refusal = self.refused(self.build)
        elapsed = time.monotonic() - started
        release.set()
        self.future.result(2)
        self.assertEqual(refusal, {'code': 'query_busy', 'path': None})
        self.assertEqual((self.repo.reads, self.repo.rechecks), (1, 0))
        self.assertGreaterEqual(elapsed, .75)
        self.assertLess(elapsed, .5 + .8 + .4)
        self.assertEqual(self.store.usage()['sessions'], 0)

    def test_current_pages_keep_one_budget_waits_included(self):
        # Deliberately unchanged: a lower wait cap would refuse cheap reads that now succeed after a wait.
        repository, budgets = Repository(), []
        original = repository.read_page
        def read_page(plan, continuation, *, deadline, cancelled):
            budgets.append(deadline - time.monotonic())
            return original(plan, continuation, deadline=deadline, cancelled=cancelled)
        repository.read_page = read_page
        service = QueryService(repository, lambda: CONTEXT, clock=lambda: NOW, admission=self.admission)
        other = self.hold_slot(seconds=.5)
        service.execute(canonical(query(select=['video.id', 'video.title', 'video.views'])))
        other.result(2)
        self.assertLess(budgets[0], 8.0 - .4)
        self.assertGreater(budgets[0], 8.0 - 1.5)


if __name__ == '__main__': unittest.main()
