"""Actual availability/addressed steps and Coordinator; no database or network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event,get_ident
from types import SimpleNamespace
import time
import os
import unittest
from unittest.mock import Mock,patch

from collection.availability import AddressedAvailabilityUnit
from collection.dispatch import Ack,Completion,Coordinator,DispatchBlocked
from test_common_video_dispatch import Repository as CommonRepository
from test_initial_video_parallel import service,video,CHANNEL
import collection_shutdown


class Repository(CommonRepository):
    def load_video_channel_map(self, ids):
        assert get_ident()==self.owner
        return dict.fromkeys(ids,CHANNEL)
    def ensure_video_ids(self, ids, **options):
        assert get_ident()==self.owner
        assert options['channel_scopes']==[dict(channel_id=CHANNEL,control_epoch=7)]
    def claim_video_metadata_ids(self, ids, **options):
        assert get_ident()==self.owner
        base=self.claim()['rows'][0]
        return [replace(base,video_id=value) for value in ids]


class AvailabilityDispatchTests(unittest.TestCase):
    def setUp(self):
        self.repo=Repository();self.collector=service(self.repo,Mock())
        self.fallback_required=False
        self.network=[];self.ids=['v'+str(i).zfill(10) for i in range(250)]
        def metadata(ids,reason):
            self.assertNotEqual(get_ident(),self.repo.owner)
            self.network.append(('general',tuple(ids)))
            items=[video(value) for value in ids]
            if self.fallback_required:
                for item in items:item['statistics'].pop('viewCount')
            return items
        def counters(ids,reason):
            self.assertNotEqual(get_ident(),self.repo.owner)
            self.network.append(('stats',tuple(ids)))
            return dict(items=[dict(id=value,views=123,like_count=7,comment_count=2) for value in ids],summary=dict(failed_ids=[]))
        self.collector.fetch_metadata,self.collector.fetch_counters=metadata,counters
        self.progress=Mock()
        self.patches=[patch.dict(os.environ,{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}),patch.object(collection_shutdown,'_requested',Event()),
            patch('collection.availability.load_initial_long_inventory_status',return_value='completed'),
            patch('collection.availability.load_long_availability_loss_candidate_ids',side_effect=self.candidates),
            patch('collection.availability.load_video_availability_status_counts',return_value={'public':250}),
            patch('collection.initial_video_hydration._drain_unsent')]
        for value in self.patches:value.start();self.addCleanup(value.stop)

    def candidates(self,*args,**options):
        self.assertEqual(get_ident(),self.repo.owner)
        return self.ids

    def unit(self,**options):
        return AddressedAvailabilityUnit(SimpleNamespace(database=object()),self.collector,
            key='availability',channel_id=CHANNEL,expected_epoch=7,kind='loss',
            require_ready=lambda:None,on_settled=self.progress,**options)

    def drive(self, unit):
        with ThreadPoolExecutor(2) as pool:
            owner=Coordinator(pool,2);owner.offer(unit.work)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                owner.step()
                if not owner.keys():return owner
                owner.wait(.01)
        self.fail('availability did not drain')

    def test_250_policy_remains_five_claims_and_provider_only_executor(self):
        unit=self.unit();owner=self.drive(unit)
        self.assertEqual(unit.summary['updated'],250)
        self.assertEqual(self.repo.claimed,5)
        self.assertEqual(len(self.network),5)
        self.assertTrue(all(kind=='general' for kind,_ in self.network))
        self.assertTrue(all(len(ids)==50 for kind,ids in self.network))
        self.assertFalse(unit.claims)
        self.progress.assert_called_once()

    def test_cancel_unsent_drains_only_current_claim_once(self):
        unit=self.unit();unit.prepare()
        with patch('collection.initial_video_hydration._drain_unsent') as drain:
            self.assertIsInstance(unit.settle(Completion('cancelled')),Ack)
            self.assertEqual(len(drain.call_args.args[1]),50)
            self.assertEqual(self.repo.claimed,1)
            self.assertFalse(self.network)

    def test_global_stop_between_source_steps_drains_without_stats_request(self):
        self.fallback_required=True  # Keep a real Stats continuation pending.
        stopped=Event();unit=self.unit(should_stop=stopped.is_set)
        prepared=unit.prepare()
        with ThreadPoolExecutor(1) as pool:response=pool.submit(prepared.fetch,Event()).result(timeout=2)
        stopped.set()
        with patch('collection.initial_video_hydration._drain_unsent') as drain:
            self.assertIsInstance(unit.settle(Completion('response',value=response,fetch_started=True)),Ack)
            drain.assert_called_once()
        self.assertEqual([kind for kind,_ in self.network],['general'])
        self.assertEqual(self.repo.claimed,1)

    def fetch(self, prepared):
        with ThreadPoolExecutor(1) as pool:
            return pool.submit(prepared.fetch,Event()).result(timeout=2)

    def test_completed_batch_survives_cancel_of_next_general_without_repeat_finish(self):
        self.fallback_required=True
        unit=self.unit();prepared=unit.prepare()
        finish=self.collector.finish_common_claims
        with patch.object(self.collector,'finish_common_claims',wraps=finish) as finalize:
            stats=unit.settle(Completion('response',value=self.fetch(prepared),fetch_started=True))
            next_general=unit.settle(Completion('response',value=self.fetch(stats),fetch_started=True))
            self.assertEqual(self.repo.claimed,2)
            writes=list(self.repo.writes)
            self.assertIsInstance(unit.settle(Completion('cancelled')),Ack)
            finalize.assert_called_once()
        self.assertEqual(unit.summary['updated'],50)
        self.assertEqual(unit.summary['video_ids'],self.ids[:50])
        self.assertEqual(unit.summary['metadata_requests'],1)
        self.assertEqual(unit.summary['batch_requests'],1)
        self.assertEqual(unit.summary['requested'],250)
        self.assertEqual(unit.summary['explicit_unclaimed'],150)
        self.assertEqual(self.repo.writes,writes)
        self.assertEqual([kind for kind,_ in self.network],['general','stats'])
        self.progress.assert_called_once()

    def test_general_missing_ack_survives_unsent_stats_without_finish(self):
        self.fallback_required=True
        original=self.collector.fetch_metadata
        self.collector.fetch_metadata=lambda ids,reason:original(ids,reason)[1:]
        unit=self.unit();prepared=unit.prepare()
        with patch.object(self.collector,'finish_common_claims',wraps=self.collector.finish_common_claims) as finalize:
            unit.settle(Completion('response',value=self.fetch(prepared),fetch_started=True))
            writes=list(self.repo.writes)
            self.assertEqual(writes[0][0],'missing')
            self.assertIsInstance(unit.settle(Completion('cancelled')),Ack)
            finalize.assert_not_called()
        self.assertEqual(unit.summary['missing'],1)
        self.assertEqual(unit.summary['unavailable_marked'],1)
        self.assertEqual(unit.summary['missing_video_ids'],self.ids[:1])
        self.assertEqual(unit.summary['metadata_requests'],1)
        self.assertEqual(unit.summary['batch_requests'],0)
        self.assertEqual(self.repo.writes,writes)
        self.assertEqual([kind for kind,_ in self.network],['general'])
        self.progress.assert_called_once()

    def test_unknown_queue_claim_commit_and_observer_never_retry(self):
        for stage in ('ensure_video_ids','claim_video_metadata_ids','commit','observer'):
            with self.subTest(stage=stage):
                self.repo=Repository();self.collector.repository=self.repo
                self.progress=Mock(side_effect=RuntimeError('unknown') if stage=='observer' else None)
                if stage=='commit':self.repo.fail_commit=True
                if stage in ('ensure_video_ids','claim_video_metadata_ids'):
                    setattr(self.repo,stage,Mock(side_effect=OSError('unknown')))
                unit=self.unit()
                with ThreadPoolExecutor(1) as pool:
                    owner=Coordinator(pool,1);owner.offer(unit.work)
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        try:owner.step()
                        except DispatchBlocked:break
                        owner.wait(.01)
                    else:self.fail('uncertainty did not block')
                    with self.assertRaises(DispatchBlocked):owner.step()
                    self.assertEqual(owner.snapshot().held,1)
                if stage in ('ensure_video_ids','claim_video_metadata_ids'):
                    getattr(self.repo,stage).assert_called_once()
                self.assertEqual(self.progress.call_count,int(stage=='observer'))
