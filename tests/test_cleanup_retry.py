"""A failed maintenance step is retried after a growing pause (audit S5-7). No database."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
os.environ.setdefault('LIC_DEPLOYMENT_PROFILE','self-hosted')
from hot_runtime import HotRuntimeController, RETRY_FIRST_SECONDS, RETRY_MAX_SECONDS  # noqa: E402

class Clock:
    def __init__(self):self.now=1000.0
    def __call__(self):return self.now

class Repository:
    def __init__(self,clock):self.clock=clock
    def contract(self):
        return {'version':1,'schema_ready':True,'as_of':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()}

    def startup_expiry(self, *, cutoff, strict_entry=False):
        return {'as_of':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat(),'expired':False}

class Cycle:
    """One page per step: fails while `failures` remain, then finishes the pass."""
    def __init__(self,clock,failures):
        self.clock,self.failures,self.error,self.retried,self.batches=clock,failures,False,0,0
    def summary(self):
        return {'status':'error' if self.error else 'in_progress','batches':self.batches}
    def retry_failed(self):
        assert self.error; self.error=False; self.retried+=1
    def reset(self):self.batches=0
    def run_slice(self,**kw):
        self.batches+=1
        if self.failures:
            self.failures-=1; self.error=True
            return {'status':'error','failed_target':'video_snapshots','pages':[]}
        return {'status':'scan_exhausted','batches':1,'counts':{'scanned':1,'deleted':0,'scrubbed':0,'deferred':0},
                'targets_not_visited':[],'targets_pending':[],'targets_with_observed_deferrals':[],
                'invalid_clocks_observed':0,'unknown_expiry_observed':0,'oldest_observed_expired_source_at':None,
                'pages':[{'as_of':datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()}]}

def controller(failures):
    clock=Clock()
    runtime=HotRuntimeController(Repository(clock),local_cleanup=lambda:{'status':'current'},clear_caches=lambda:None,
                                 clock=clock,monotonic=clock)
    runtime.cycle=Cycle(clock,failures)
    return runtime,clock

def tick(runtime,clock,seconds):
    """Advance in heartbeats short enough not to look like a stalled process."""
    for _ in range(int(seconds)):
        clock.now+=1; runtime.step()

class CleanupRetryTests(unittest.TestCase):
    def test_a_failed_page_is_retried_and_readiness_returns(self):
        runtime,clock=controller(failures=2)
        runtime.step()
        status=runtime.status()
        self.assertEqual((status['status'],status['maintenance_failed'],status['failure_code']),
                         ('ready',True,'hot_runtime_cleanup_failed'))
        tick(runtime,clock,RETRY_FIRST_SECONDS-2)
        self.assertEqual(runtime.cycle.retried,0)   # still waiting
        tick(runtime,clock,2)
        self.assertEqual(runtime.cycle.retried,1)   # retried and failed again
        self.assertTrue(runtime.status()['maintenance_failed'])
        tick(runtime,clock,2*RETRY_FIRST_SECONDS)   # the second pause is twice as long
        self.assertEqual(runtime.cycle.retried,2)
        status=runtime.status()
        self.assertEqual((status['status'],status['maintenance_failed'],status['failure_code']),('ready',False,None))

    def test_the_pause_is_bounded(self):
        runtime,clock=controller(failures=50)
        runtime.step()
        for _ in range(10):
            runtime._retry_at=clock.now
            tick(runtime,clock,1)
        self.assertLessEqual(runtime._retry_at-clock.now,RETRY_MAX_SECONDS)
        self.assertGreater(runtime._retry_at-clock.now,RETRY_MAX_SECONDS-5)

if __name__=='__main__':unittest.main()
