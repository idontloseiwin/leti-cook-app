"""Bounded metadata paging with the actual Coordinator and metadata iterator."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime
import time
import unittest
from unittest.mock import Mock, patch

import test_channel_metadata_steps as fixtures
from test_channel_metadata_steps import CHANNEL, row
from collection.metadata_feeder import AutoMetadataCursor, AutoMetadataFeeder
from collection.dispatch import Coordinator, DispatchBlocked
from db.channel.channel_sync import MetadataCandidateCursor
import collection_shutdown

CEILING = '2026-10-10T08:00:00.000000+00:00'


class MetadataFeederTests(unittest.TestCase):
    db = fixtures.MetadataProviderStepsTests.db
    fetch = fixtures.MetadataProviderStepsTests.fetch
    persist = fixtures.MetadataProviderStepsTests.persist

    def setUp(self):
        fixtures.MetadataProviderStepsTests.setUp(self)
        self.stack.enter_context(patch.object(collection_shutdown, '_requested', Event()))
        self.context.sources.parse_dt = lambda value: datetime.fromisoformat(value) if value else None
        self.pages, self.captures = [], []
        self.rows = [row()]
        self.repository.metadata_candidate_page.side_effect = self.page
        self.repository.capture_metadata_candidates.side_effect = self.capture

    def page(self, **request):
        self.db('candidate'); self.pages.append(request)
        physical = [value for value in self.rows if request['after'] is None or value['channel_id'] > request['after'].channel_id][:request['limit']]
        return dict(rows=[dict(channel_id=value['channel_id'], control_epoch=7) for value in physical if not value.get('excluded')],
            next_after=MetadataCandidateCursor(None, physical[-1]['channel_id']) if physical else request['after'],
            due_ceiling=CEILING, inspected_count=len(physical), scan_exhausted=len(physical)<request['limit'])

    def capture(self, scopes, **request):
        self.db('capture'); self.captures.append(scopes)
        return [value for value in self.rows if any(scope['channel_id']==value['channel_id'] and scope['control_epoch']==value['collection_control_epoch'] for scope in scopes)]

    def feeder(self, *, limit=100, scan=100, cursor=None, **options):
        return AutoMetadataFeeder(self.repository, self.context, channel_limit=limit,
            candidate_scan_limit=scan, cursor=cursor or AutoMetadataCursor(), require_ready=lambda:None, **options)

    def drive(self, feeder, coordinator):
        until=time.monotonic()+3
        while time.monotonic()<until:
            feeder.offer_to(coordinator, limit=1)
            coordinator.step()
            if feeder.exhausted and not coordinator.keys():
                feeder.finish_pass(coordinator)
                return
            coordinator.wait(.01)
        self.fail('metadata feeder did not drain')

    def test_actual_unit_provider_only_executor_and_capture_only_prepare(self):
        feeder=self.feeder()
        with ThreadPoolExecutor(2) as executor:
            owner=Coordinator(executor,2)
            self.assertEqual(feeder.offer_to(owner),1)
            self.assertFalse(self.captures)
            self.assertEqual(feeder.offer_to(owner),0)
            self.drive(feeder,owner)
        self.assertEqual(feeder.summary['updated'],1)
        self.assertEqual(len(self.captures),1)
        self.assertEqual(self.captures[0],[dict(channel_id=CHANNEL,control_epoch=7)])
        self.assertLess(self.events.index('capture'),self.events.index('running'))

    def test_full_101_channel_handoff_is_three_bounded_requests(self):
        self.rows=[row('UC'+f'{i:022d}') for i in range(101)]
        feeder=self.feeder(limit=101,scan=200)
        with ThreadPoolExecutor(2) as executor:self.drive(feeder,Coordinator(executor,2))
        self.assertEqual((feeder.selected,feeder.summary['updated'],feeder.summary['batches']),(101,101,3))
        self.assertEqual([len(value) for value in self.captures],[50,50,1])
        self.assertEqual(len(feeder.summary['metadata_persisted_channel_scopes']),101)

    def test_empty_physical_prefix_and_next_pass_preserve_tail(self):
        self.rows=[row('UC'+f'{i:022d}',excluded=True) for i in range(8)]+[row('UC'+f'{8:022d}')]
        cursor=AutoMetadataCursor()
        first=self.feeder(limit=1,scan=8,cursor=cursor)
        with ThreadPoolExecutor(1) as executor:self.drive(first,Coordinator(executor,1))
        self.assertEqual(first.inspected,8)
        self.assertFalse(cursor.sweep_exhausted)
        self.assertFalse(self.captures)
        second=self.feeder(limit=1,scan=8,cursor=cursor)
        with ThreadPoolExecutor(1) as executor:self.drive(second,Coordinator(executor,1))
        self.assertEqual(second.summary['updated'],1)
        self.assertEqual(self.captures[0][0]['channel_id'],self.rows[-1]['channel_id'])

    def test_stale_epoch_capture_spends_selection_once_without_provider(self):
        feeder=self.feeder(limit=1)
        with ThreadPoolExecutor(1) as executor:
            owner=Coordinator(executor,1);feeder.offer_to(owner)
            self.rows[0]['collection_control_epoch']=8
            self.drive(feeder,owner)
        self.assertEqual(feeder.selected,1)
        self.assertFalse(any(isinstance(event,tuple) for event in self.events))
        self.repository.mark_metadata_running.assert_not_called()

    def test_global_stop_after_offer_never_captures(self):
        stopped=Event();feeder=self.feeder(should_stop=stopped.is_set)
        with ThreadPoolExecutor(1) as executor:
            owner=Coordinator(executor,1);feeder.offer_to(owner);stopped.set();owner.step()
        self.assertFalse(self.captures)
        self.assertFalse(owner.keys())

    def test_unknown_commit_and_observer_remain_blocked_without_replay(self):
        for fail_observer in (False,True):
            with self.subTest(observer=fail_observer):
                self.repository.update_metadata_batch.side_effect=self.persist if fail_observer else OSError('unknown')
                observer=Mock(side_effect=RuntimeError('unknown handoff')) if fail_observer else Mock()
                feeder=self.feeder(on_settled=observer)
                with ThreadPoolExecutor(1) as executor:
                    owner=Coordinator(executor,1);feeder.offer_to(owner)
                    until=time.monotonic()+3
                    while time.monotonic()<until:
                        try:owner.step()
                        except DispatchBlocked:break
                        owner.wait(.01)
                    else:self.fail('missing unknown block')
                    with self.assertRaises(DispatchBlocked):owner.step()
                    with self.assertRaises(RuntimeError):feeder.finish_pass(owner)
                    self.assertEqual(owner.snapshot().held,1)
                self.assertEqual(observer.call_count,int(fail_observer))

    def test_zero_offer_does_not_read_or_move_cursor(self):
        feeder=self.feeder()
        with ThreadPoolExecutor(1) as executor:
            owner=Coordinator(executor,1)
            self.assertEqual(feeder.offer_to(owner,limit=0),0)
            for value in (-1,True,101,1.5):
                with self.assertRaises(ValueError):feeder.offer_to(owner,limit=value)
        self.assertFalse(self.pages)
        self.assertIsNone(feeder.cursor.after)
