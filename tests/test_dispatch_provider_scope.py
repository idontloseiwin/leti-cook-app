"""Actual network-worker context propagation, with no HTTP or database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'application'))
from collection.dispatch import Ack, Coordinator, Prepared, Work
import provider_runtime as runtime


class ScopeTests(unittest.TestCase):
    def test_nested_scopes_preserve_stricter_deadline_and_restore_parent(self):
        self.assertIsNone(runtime.request_deadline())
        with runtime.request_scope('auto', deadline=300):
            with runtime.request_scope('operator'):
                self.assertEqual(runtime.request_deadline(), 300)
            with runtime.request_scope('operator', deadline=400):
                self.assertEqual(runtime.request_deadline(), 300)
            with runtime.request_scope('operator', deadline=200):
                self.assertEqual(runtime.request_deadline(500), 200)
                self.assertEqual(runtime.request_deadline(100), 100)
            self.assertEqual(runtime.request_deadline(), 300)
        self.assertIsNone(runtime.request_deadline())

    def test_invalid_scope_deadline_is_rejected_without_mutating_context(self):
        for value in (True, '100', float('inf'), float('nan'), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with runtime.request_scope('auto', deadline=value):
                    self.fail('invalid deadline admitted')
            self.assertIsNone(runtime.request_deadline())

    def test_real_threads_receive_lane_deadline_and_independent_cancellation(self):
        owner = threading.get_ident()
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        observations, admissions, settled = {}, [], []
        deadline = time.monotonic()+30

        @contextmanager
        def acquire(kind, *, timeout, cancel):
            admissions.append(kind)
            yield SimpleNamespace(observe=lambda **kw: None)

        backend = SimpleNamespace(pid=os.getpid(), stop=threading.Event(),
                                  gate=SimpleNamespace(acquire=acquire))
        with patch.object(runtime, '_runtime', backend), ThreadPoolExecutor(max_workers=2) as pool:
            coordinator = Coordinator(pool, 2, fetch_scope=runtime.request_scope)
            def work(index, lane):
                def prepare():
                    self.assertEqual(threading.get_ident(), owner)
                    def fetch(cancel):
                        self.assertNotEqual(threading.get_ident(), owner)
                        with runtime.admit_request() as combined:
                            observed_deadline = runtime.request_deadline()
                            entered[index].set()
                            if not release.wait(3):
                                raise RuntimeError('test_release_timeout')
                            observations[index] = (observed_deadline, combined.is_set())
                        return index
                    return Prepared(fetch, deadline)
                def settle(result):
                    self.assertEqual(threading.get_ident(), owner)
                    settled.append((index, result.phase))
                    return Ack()
                return Work(str(index), lane, str(index), 'channel', prepare, settle)
            coordinator.offer(work(0,'auto'))
            coordinator.offer(work(1,'operator'))
            try:
                coordinator.step()
                self.assertTrue(all(event.wait(3) for event in entered))
                coordinator.cancel('0')
            finally:
                release.set()
            until = time.monotonic()+3
            while coordinator.snapshot().held and time.monotonic()<until:
                coordinator.wait(.05)
                coordinator.step()
            self.assertEqual(coordinator.snapshot().held, 0)
        self.assertCountEqual(admissions, ['auto','operator'])
        self.assertEqual(observations, {0:(deadline,True),1:(deadline,False)})
        self.assertCountEqual(settled, [(0,'response'),(1,'response')])
        self.assertIsNone(runtime.request_deadline())

    def test_continuation_uses_its_new_tighter_deadline(self):
        deadlines = []
        stages = []
        deadline = time.monotonic()+30
        with ThreadPoolExecutor(max_workers=1) as pool:
            coordinator = Coordinator(pool, 1, fetch_scope=runtime.request_scope)
            def fetch(cancel):
                deadlines.append(runtime.request_deadline())
            def settle(completion):
                stages.append(completion.phase)
                if len(stages)==1:
                    return Prepared(fetch, deadline-1)
                return Ack()
            coordinator.offer(Work('same','operator','channel','long',
                                   lambda:Prepared(fetch,deadline),settle))
            coordinator.step()
            until = time.monotonic()+3
            while coordinator.snapshot().held and time.monotonic()<until:
                coordinator.wait(.05)
                coordinator.step()
            self.assertEqual(coordinator.snapshot().held,0)
        self.assertEqual(stages,['response','response'])
        self.assertEqual(deadlines,[deadline,deadline-1])


if __name__ == '__main__':
    unittest.main()
