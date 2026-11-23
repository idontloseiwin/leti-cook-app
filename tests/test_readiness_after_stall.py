"""A resumed process rechecks source expiry, not a database-age certificate."""
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
os.environ.setdefault('LIC_DEPLOYMENT_PROFILE','self-hosted')
from hot_runtime import HotRuntimeController  # noqa: E402

class Clocks:
    def __init__(self):self.wall,self.mono=1_000_000.0,500.0
    def advance(self,wall,mono=None):
        self.wall+=wall; self.mono+=wall if mono is None else mono

class Repository:
    def __init__(self,clocks):self.clocks=clocks
    def contract(self):
        return {'version':1,'schema_ready':True,'as_of':datetime.fromtimestamp(self.clocks.wall,timezone.utc).isoformat()}

    def startup_expiry(self, *, cutoff, strict_entry=False):
        return {'as_of':datetime.fromtimestamp(self.clocks.wall,timezone.utc).isoformat(),
                'expired':getattr(self,'expired',False)}

class Cycle:
    """Each pass takes `pages` steps."""
    def __init__(self,clocks,pages):self.clocks,self.pages,self.done=clocks,pages,0
    def summary(self):return {'status':'in_progress','batches':self.done}
    def reset(self):self.done=0
    def run_slice(self,**kw):
        self.done+=1
        return {'status':'scan_exhausted' if self.done>=self.pages else 'in_progress','batches':1,
                'counts':{'scanned':1,'deleted':0,'scrubbed':0,'deferred':0},'targets_not_visited':[],'targets_pending':[],
                'targets_with_observed_deferrals':[],'invalid_clocks_observed':0,'unknown_expiry_observed':0,'oldest_observed_expired_source_at':None,
                'pages':[{'as_of':datetime.fromtimestamp(self.clocks.wall,timezone.utc).isoformat()}]}

def ready_runtime():
    clocks=Clocks()
    runtime=HotRuntimeController(Repository(clocks),local_cleanup=lambda:{'status':'current'},clear_caches=lambda:None,
                                 clock=lambda:clocks.wall,monotonic=lambda:clocks.mono)
    runtime.cycle=Cycle(clocks,pages=3)
    for _ in range(3):
        clocks.advance(1); runtime.step()
    assert runtime.status()['status']=='ready'
    return runtime,clocks

class ReadinessAfterStallTests(unittest.TestCase):
    def test_a_short_stall_keeps_readiness_while_the_next_pass_runs(self):
        runtime,clocks=ready_runtime()
        clocks.advance(30)                     # a 30-second stall: past the 20-second watchdog
        runtime.step()
        self.assertEqual(runtime.status()['status'],'ready')
        self.assertEqual(runtime.cycle.done,1)  # the reset restarted the pass; its first page ran

    def test_a_suspended_host_waits_for_a_new_pass(self):
        runtime,clocks=ready_runtime()
        runtime.repository.expired=True
        clocks.advance(960,mono=5)
        self.assertEqual(runtime.status()['reason'],'clock_changed')
        runtime.step()
        self.assertEqual(runtime.status()['status'],'blocked')
        runtime.repository.expired=False
        for _ in range(3):
            clocks.advance(1); runtime.step()
        self.assertEqual(runtime.status()['status'],'ready')

if __name__=='__main__':unittest.main()
