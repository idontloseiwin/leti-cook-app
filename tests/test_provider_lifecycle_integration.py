"""Provider ownership composition with real coordinator/worker entry; no HTTP or DB."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from pathlib import Path
from types import SimpleNamespace
import json, os, sys, tempfile, threading, time, unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection.dispatch import Ack, Coordinator, Prepared, Work
from collection_schedule import CollectionSchedules
from self_hosted_collection import CollectionSupervisor
from self_hosted_configuration import CONFIG_ENV
from self_hosted_settings import YOUTUBE_REQUEST_CONCURRENCY_ENV as C_ENV, preset
from provider_admission import AdmissionUnavailable, AdmissionCancelled
import provider_runtime as provider
from ops import self_hosted_collect as child
from test_provider_admission import IDENTITY, GENERATION, BOOT


def snapshot(limit=8):
    return {provider.SNAPSHOT_ENV:json.dumps(dict(installation_id=IDENTITY,generation=GENERATION,
        boot_id=BOOT,concurrency=limit)),C_ENV:str(limit)}


def settings(limit=8):
    return dict(preset='standard',limits=preset('standard'),automatic_collection=True,
                interval_minutes=30,manual_stats_quota_units=200,handoff_grace_seconds=120,
                youtube_request_concurrency=limit)


class ProviderSupervisorTests(unittest.TestCase):
    def supervisor(self, limit=8, captured=None):
        value=dict(prepared=True,phase='ready',settings=settings(limit))
        manager=SimpleNamespace(lock=threading.RLock(),store=SimpleNamespace(read=lambda:value))
        spawn=Mock(return_value=SimpleNamespace(stdin=None,stderr=None,poll=lambda:None))
        unit=CollectionSupervisor(manager,ready=lambda:None,schedules=CollectionSchedules(),
            spawn=spawn,clock=lambda:1000,wall_clock=lambda:1000,provider_snapshot=captured)
        return unit,spawn,value

    def launch(self,unit):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{
            'OPERATOR_JOB_WAKE_PATH':str(Path(folder)/'wake'),**snapshot(50)}):
            unit.tick()

    def test_backend_snapshot_concurrency_wins_over_changed_store(self):
        unit,spawn,value=self.supervisor(limit=16,captured=snapshot(8))
        value['settings']['youtube_request_concurrency']=50
        self.launch(unit)
        environment=spawn.call_args.kwargs['env']
        self.assertEqual(environment[C_ENV],'8')
        self.assertEqual(json.loads(environment[provider.SNAPSHOT_ENV])['concurrency'],8)
        self.assertEqual(environment[provider.SNAPSHOT_ENV],snapshot(8)[provider.SNAPSHOT_ENV])
        self.assertEqual(environment['LIC_COLLECTION_SUPERVISED'],'1')

    def test_captured_provider_map_is_defensive_and_wins_over_process_environment(self):
        captured=snapshot(8);unit,spawn,_=self.supervisor(captured=captured)
        captured.update(snapshot(50))
        self.launch(unit)
        environment=spawn.call_args.kwargs['env']
        self.assertEqual(environment[C_ENV],'8')
        self.assertEqual(environment[provider.SNAPSHOT_ENV],snapshot(8)[provider.SNAPSHOT_ENV])

    def test_explicit_incomplete_or_invalid_snapshot_never_falls_back_to_store(self):
        for captured in ({}, {provider.SNAPSHOT_ENV:snapshot()[provider.SNAPSHOT_ENV]},
                         {C_ENV:'8'}, {**snapshot(),C_ENV:'0'}, {**snapshot(),C_ENV:'51'},
                         {**snapshot(),C_ENV:True}):
            with self.subTest(keys=sorted(captured)),self.assertRaises((ValueError,AdmissionUnavailable)):
                self.supervisor(limit=8,captured=captured)


class ProviderChildTests(unittest.TestCase):
    @contextmanager
    def context(self, *, kind='operator', prepared=True, automatic=True, handoff=False,
                begin_error=None, run_error=None, real_start=False):
        events=[];captured=snapshot(8)
        value=dict(prepared=prepared,settings=settings(16));value['settings']['automatic_collection']=automatic
        runtime=object()
        def begin(environment):
            events.append('provider.start')
            self.assertEqual(environment[C_ENV],'8')
            self.assertEqual(environment[provider.SNAPSHOT_ENV],captured[provider.SNAPSHOT_ENV])
            if begin_error:raise begin_error
            return runtime
        def run(value):
            events.append('collection.run')
            self.assertEqual(provider._scope.get()[0],kind)
            if run_error:raise run_error
            return 7
        def close(actual):
            self.assertIs(actual,runtime);events.append('provider.close')
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ,{CONFIG_ENV:'offline-settings',
                'LIC_COLLECTION_SUPERVISED':'1','LIC_DEPLOYMENT_PROFILE':'self-hosted',**captured}))
            stack.enter_context(patch.object(sys,'argv',['self_hosted_collect.py',kind]))
            stack.enter_context(patch.dict(sys.modules,{'core':SimpleNamespace(COLLECTION_SCHEDULES=CollectionSchedules())}))
            stack.enter_context(patch('self_hosted_configuration.ConfigurationStore',return_value=SimpleNamespace(read=lambda:value)))
            ready=stack.enter_context(patch('hot_runtime.require_hot_runtime_ready',side_effect=lambda **kw:events.append('hot.ready')))
            stack.enter_context(patch('collection_shutdown.handoff_requested',return_value=handoff))
            if not real_start:stack.enter_context(patch.object(provider,'start_worker',side_effect=begin))
            closed=stack.enter_context(patch.object(provider,'stop_runtime',side_effect=close))
            executed=stack.enter_context(patch.object(child,'_run_collection',side_effect=run))
            yield events,ready,closed,executed

    def test_worker_opens_before_collection_and_closes_after_return_with_scope_reset(self):
        for kind in ('auto','operator'):
            with self.subTest(kind=kind),self.context(kind=kind) as (events,_,closed,executed):
                self.assertEqual(child._collect(),7)
                self.assertEqual(events,['hot.ready','provider.start','collection.run','provider.close'])
                closed.assert_called_once();executed.assert_called_once()
            self.assertEqual(provider._scope.get(),('interactive',None))

    def test_collection_failure_still_closes_child_provider_and_resets_scope(self):
        with self.context(run_error=RuntimeError('collector failed')) as (events,_,closed,_):
            with self.assertRaisesRegex(RuntimeError,'collector failed'):child._collect()
            self.assertEqual(events[-2:],['collection.run','provider.close']);closed.assert_called_once()
        self.assertEqual(provider._scope.get(),('interactive',None))

    def test_provider_open_failure_never_enters_collection_or_closes_unowned_runtime(self):
        with self.context(begin_error=AdmissionUnavailable('snapshot refused')) as (events,_,closed,executed):
            with self.assertRaises(AdmissionUnavailable):child._collect()
            executed.assert_not_called();closed.assert_not_called()
            self.assertEqual(events,['hot.ready','provider.start'])

    def test_missing_snapshot_fails_closed_without_gate_initialize_or_collection(self):
        with self.context(real_start=True) as (_,_,closed,executed),patch.object(provider,'AdmissionGate') as gate:
            os.environ.pop(provider.SNAPSHOT_ENV,None)
            with self.assertRaisesRegex(AdmissionUnavailable,'snapshot_required'):child._collect()
            gate.assert_not_called();gate.for_backend.assert_not_called()
            executed.assert_not_called();closed.assert_not_called()

    def test_known_handoff_and_disabled_collection_do_not_open_provider(self):
        for options in (dict(handoff=True),dict(prepared=False),dict(automatic=False)):
            with self.subTest(options=options),self.context(**options) as (events,_,closed,executed):
                self.assertEqual(child._collect(),0)
                self.assertNotIn('provider.start',events)
                executed.assert_not_called();closed.assert_not_called()


class ProviderThreadContextTests(unittest.TestCase):
    def test_coordinator_copies_scope_and_all_cancel_sources_remain_live_after_permit(self):
        for reason in ('scoped','per_call','backend_stop'):
            with self.subTest(reason=reason):
                owner=threading.get_ident();entered=threading.Event();release=threading.Event()
                scoped=threading.Event();per_call=threading.Event();calls=[];results=[]
                class Gate:
                    @contextmanager
                    def acquire(self,kind,*,timeout,cancel):
                        calls.append((kind,threading.get_ident()))
                        if cancel.is_set():raise AdmissionCancelled('cancelled')
                        try:yield
                        finally:calls.append(('released',threading.get_ident()))
                    def _notify(self):pass
                runtime=provider.ProviderRuntime(Gate())
                def fetch(unused):
                    with provider.admit_request(cancel=per_call) as combined:
                        entered.set()
                        if not release.wait(2):raise AssertionError('owner did not signal')
                        return combined.is_set()
                def settle(completion):
                    self.assertEqual(threading.get_ident(),owner)
                    self.assertEqual(completion.phase,'response')
                    results.append(completion.value);return Ack()
                with patch.object(provider,'_runtime',runtime),ThreadPoolExecutor(max_workers=1) as executor:
                    coordinator=Coordinator(executor,1)
                    coordinator.offer(Work('page','auto','UC'+'a'*22,'long',
                        lambda:Prepared(fetch,time.monotonic()+5),settle))
                    with provider.request_scope('auto',scoped):
                        coordinator.step()
                        self.assertTrue(entered.wait(2))
                        if reason=='scoped':scoped.set()
                        elif reason=='per_call':per_call.set()
                        else:runtime.close()
                        release.set()
                    deadline=time.monotonic()+3
                    while coordinator.snapshot().held and time.monotonic()<deadline:
                        coordinator.wait(.01);coordinator.step()
                    self.assertEqual(coordinator.snapshot().held,0)
                self.assertEqual(results,[True]);self.assertEqual([x[0] for x in calls],['auto','released'])
                self.assertTrue(all(thread!=owner for _,thread in calls))
                self.assertEqual(provider._scope.get(),('interactive',None))


if __name__=='__main__':unittest.main(verbosity=2)
