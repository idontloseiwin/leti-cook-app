"""Real Uvicorn subprocess lifespans/signals; no application, sockets, DB or API."""
from __future__ import annotations
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT/'application/ops/self_hosted_container.py'

CHILD = r'''
import asyncio, importlib.util, signal, sys
import uvicorn
spec=importlib.util.spec_from_file_location('container_launcher',sys.argv[1])
launcher=importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
mode=sys.argv[2]
async def app(scope,receive,send):
    assert scope['type']=='lifespan'
    assert (await receive())['type']=='lifespan.startup'
    if mode=='startup_failed':
        await send({'type':'lifespan.startup.failed'}); return
    if mode=='startup_error': raise RuntimeError('synthetic_startup_failure')
    if mode!='startup_no_ack':
        await send({'type':'lifespan.startup.complete'})
    print('READY',flush=True)
    if not mode.startswith('external_'):
        sig=signal.SIGINT if mode=='force_exit' else signal.SIGTERM
        asyncio.get_running_loop().call_later(0.05,signal.raise_signal,sig)
    if mode=='startup_no_ack': return
    assert (await receive())['type']=='lifespan.shutdown'
    print('SHUTDOWN_STARTED',flush=True)
    if mode=='external_kill':
        await asyncio.Event().wait()
    if mode=='force_exit':
        asyncio.get_running_loop().call_later(0.02,signal.raise_signal,signal.SIGINT)
        await asyncio.sleep(0.08)
    if mode=='shutdown_error': raise RuntimeError('synthetic_cleanup_failure')
    if mode=='shutdown_failed':
        await send({'type':'lifespan.shutdown.failed'}); return
    if mode=='shutdown_no_ack': return
    await send({'type':'lifespan.shutdown.complete'})
    print('SHUTDOWN_COMPLETE',flush=True)
    if mode=='after_ack_error': raise RuntimeError('synthetic_after_ack_failure')
    if mode=='after_ack_unfinished': await asyncio.sleep(30)
config=uvicorn.Config(app,workers=1,access_log=False,proxy_headers=False,
                      lifespan='on',loop='asyncio',log_config=None,log_level='critical')
code=launcher.run_backend(config,sockets=[])
print('RESULT='+str(code),flush=True)
raise SystemExit(code)
'''


class ContainerShutdownTests(unittest.TestCase):
    def child(self, mode):
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([sys.executable,'-B','-c',CHILD,str(LAUNCHER),mode],
                              capture_output=True,text=True,timeout=8,env=env)

    def test_real_sigterm_after_successful_teardown_exits_zero(self):
        result=self.child('success')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('SHUTDOWN_COMPLETE',result.stdout)
        self.assertIn('RESULT=0',result.stdout)

    def test_lifespan_failures_or_missing_ack_never_exit_zero(self):
        for mode in ('startup_failed','startup_error','startup_no_ack','shutdown_failed',
                     'shutdown_error','shutdown_no_ack','after_ack_error','after_ack_unfinished'):
            with self.subTest(mode=mode):
                result=self.child(mode)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertNotIn('RESULT=0',result.stdout)

    def test_second_sigint_force_exit_is_not_clean_even_if_app_acknowledges(self):
        result=self.child('force_exit')
        self.assertIn('SHUTDOWN_COMPLETE',result.stdout)
        self.assertNotEqual(result.returncode,0,result.stderr)
        self.assertNotIn('RESULT=0',result.stdout)

    @unittest.skipUnless(os.name=='posix','Parent-delivered POSIX signals require Linux/macOS')
    def test_external_sigterm_and_sigkill_keep_distinct_exit_results(self):
        for mode in ('external_success','external_kill'):
            with self.subTest(mode=mode):
                child=subprocess.Popen([sys.executable,'-B','-u','-c',CHILD,str(LAUNCHER),mode],
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    # Bound readiness with select rather than an unbounded readline.
                    import select
                    self.assertTrue(select.select([child.stdout],[],[],5)[0])
                    self.assertEqual(child.stdout.readline().strip(),'READY')
                    child.send_signal(signal.SIGTERM)
                    if mode=='external_kill':
                        self.assertTrue(select.select([child.stdout],[],[],5)[0])
                        self.assertEqual(child.stdout.readline().strip(),'SHUTDOWN_STARTED')
                        child.kill()
                    stdout,stderr=child.communicate(timeout=5)
                    if mode=='external_success':
                        self.assertEqual(child.returncode,0,stderr)
                        self.assertIn('SHUTDOWN_COMPLETE',stdout)
                    else:
                        self.assertEqual(child.returncode,-signal.SIGKILL)
                        self.assertNotIn('SHUTDOWN_COMPLETE',stdout)
                finally:
                    if child.poll() is None: child.kill()
                    child.communicate(timeout=5)


if __name__=='__main__': unittest.main()
