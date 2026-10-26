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
from research_query.admission import ReadAdmission
from research_query.validation import QueryRejected, canonical
from test_research_query_service import Repository, QueryService, CONTEXT, NOW
from test_research_query_contract import query


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


if __name__ == '__main__': unittest.main()
