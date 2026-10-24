"""Private bounded cursor protocol; real pipes/parser, no live services."""
from copy import deepcopy
from io import BytesIO
import json
import os
import unittest
from unittest.mock import patch

import test_collector_handoff as handoff
from collection_shutdown import (CURSOR_SNAPSHOT_ENV,CURSOR_COMPLETE_PREFIX,CURSOR_MAX_BYTES,
    encode_cursor_checkpoint,decode_cursor_checkpoint)
from self_hosted_collection import read_exit_reason,CollectionSupervisor
from runtime.process_collection import _capture_collection_cursors,_restore_collection_cursors
from collection.auto_pass import CommonCandidateCursor
from collection.inventory_feeder import AutoInventoryCursor
from collection.metadata_feeder import AutoMetadataCursor

CHANNEL='UC'+'a'*22


def checkpoint():
    return dict(version=3,common=dict(after=dict(
        metadata=dict(long=CHANNEL,short=None,stream=None,unknown=None),
        counters=dict(long=None,short=CHANNEL,stream=None,unknown=None)),
        formats=['short','stream','unknown','long'],
        exhausted=dict(metadata=['long'],counters=['short','stream','unknown']),revisit=True),
        metadata=dict(after=dict(next_after=None,channel_id=CHANNEL),
            due_ceiling='2026-10-10T00:00:00.000000+00:00',exhausted=False),
        inventory=dict(after=dict(dispatch_ordinal=6,priority=0,channel_id=CHANNEL,video_type='long'),
            ceiling=20,exhausted=False))


def record(value=None):
    return CURSOR_COMPLETE_PREFIX+encode_cursor_checkpoint(value or checkpoint()).encode('ascii')+b'\n'


class CursorCodecTests(unittest.TestCase):
    def test_roundtrip_is_exact_and_only_positions(self):
        value=checkpoint();encoded=encode_cursor_checkpoint(value)
        self.assertLess(len(encoded),CURSOR_MAX_BYTES)
        self.assertEqual(decode_cursor_checkpoint(encoded),value)
        cursors=(CommonCandidateCursor(),AutoMetadataCursor(),AutoInventoryCursor())
        _restore_collection_cursors(cursors,value)
        self.assertEqual(_capture_collection_cursors(cursors),value)
        cursors[2]._active=object()
        with self.assertRaises(RuntimeError):_capture_collection_cursors(cursors)

    def test_restore_only_exhausts_unknown_counter_frontier(self):
        value=checkpoint()
        value['common']['exhausted']['counters']=[]
        cursors=(CommonCandidateCursor(),AutoMetadataCursor(),AutoInventoryCursor())
        _restore_collection_cursors(cursors,decode_cursor_checkpoint(encode_cursor_checkpoint(value)))
        normalized=_capture_collection_cursors(cursors)
        self.assertEqual(normalized['common']['after'],value['common']['after'])
        self.assertEqual(normalized['common']['exhausted']['counters'],['unknown'])
        self.assertEqual(normalized['common']['exhausted']['metadata'],['long'])

    def test_typed_counter_frontiers_survive_checkpoint_with_independent_positions(self):
        value=checkpoint()
        value['common']['after']['counters']=dict(long=None,short=CHANNEL,
            stream='UC'+'b'*22,unknown=None)
        value['common']['exhausted']['counters']=['unknown']
        cursors=(CommonCandidateCursor(),AutoMetadataCursor(),AutoInventoryCursor())
        _restore_collection_cursors(cursors,decode_cursor_checkpoint(encode_cursor_checkpoint(value)))
        self.assertEqual(_capture_collection_cursors(cursors),value)
        for kind in ('long','short','stream'):
            self.assertNotIn(('counters',kind),cursors[0]._exhausted)

    def test_bad_types_noncanonical_ids_extra_payload_and_duplicate_json_fail_closed(self):
        mutations=[lambda v:v.update(version=True),lambda v:v.update(version=1),
            lambda v:v['common'].pop('revisit'),lambda v:v['common'].update(revisit=1),
            lambda v:v['common'].update(revisit=[]),lambda v:v.update(token='secret'),
            lambda v:v['common'].update(formats=[{},'short','stream','unknown']),
            lambda v:v['common']['after']['metadata'].update(long=' '+CHANNEL),
            lambda v:v['common']['after'].pop('counters'),
            lambda v:v['common']['after']['metadata'].update(other=CHANNEL),
            lambda v:v['common']['exhausted'].update(metadata=['long','long']),
            lambda v:v['common']['exhausted'].update(counters=['other']),
            lambda v:v['common']['exhausted'].update(other=[]),
            lambda v:v['metadata']['after'].update(channel_id='https://www.youtube.com/channel/'+CHANNEL),
            lambda v:v['metadata'].update(due_ceiling='2026-10-10T00:00:00Z'),
            lambda v:v['inventory']['after'].update(dispatch_ordinal=21),
            lambda v:v['inventory'].update(ceiling=True)]
        for mutate in mutations:
            value=checkpoint();mutate(value)
            with self.assertRaises(ValueError):encode_cursor_checkpoint(value)
            with self.assertRaises(ValueError):decode_cursor_checkpoint(json.dumps(value))
        for text in ('{}','{"version":1,"version":1}','x'*(CURSOR_MAX_BYTES+1)):
            with self.assertRaises(ValueError):decode_cursor_checkpoint(text)

    def test_previous_shared_frontier_version_is_refused_explicitly(self):
        value=checkpoint();value['version']=2
        value['common']['after']=value['common']['after']['metadata']
        value['common']['exhausted']=['long']
        with self.assertRaisesRegex(ValueError,'collection_cursor_checkpoint_invalid'):
            decode_cursor_checkpoint(json.dumps(value))
        found={}
        read_exit_reason(BytesIO(CURSOR_COMPLETE_PREFIX+json.dumps(value).encode()+b'\n'),found)
        self.assertTrue(found['cursor_invalid'])
        self.assertNotIn('cursor_checkpoint',found)

    def test_parser_keeps_one_complete_control_record_never_echoes(self):
        found={};stream=BytesIO(b'ignored source text\n'+record()+b'exit_reason=KeyError\n')
        read_exit_reason(stream,found)
        self.assertTrue(stream.closed)
        self.assertEqual(found['cursor_checkpoint'],encode_cursor_checkpoint(checkpoint()))
        self.assertEqual(found['reason'],'exception:KeyError')
        self.assertTrue(found['cursor_stream_complete'])
        self.assertNotIn('ignored',str(found))

    def test_partial_duplicate_malformed_or_large_stream_discards_pending_cursor(self):
        for content in (record()[:-1],record()+record(),record()+CURSOR_COMPLETE_PREFIX+b'{}\n',
                CURSOR_COMPLETE_PREFIX+b'x'*(CURSOR_MAX_BYTES+1)+b'\n',
                record()+b'x\n'*(1<<19)):
            found={};read_exit_reason(BytesIO(content),found)
            self.assertTrue(found['cursor_invalid'])
            self.assertNotIn('cursor_checkpoint',found)


class CursorSupervisorTests(unittest.TestCase):
    setUp=handoff.SupervisorTests.setUp

    def accepted(self):
        read_exit_reason(BytesIO(record()),self.supervisor.exit_found)

    def test_only_clean_auto_exit_adopts_and_next_auto_keeps_original_cadence(self):
        self.accepted();self.process.code=0
        next_auto=self.supervisor.next_auto
        self.supervisor.tick()
        self.assertEqual(self.supervisor._cursor_checkpoint,encode_cursor_checkpoint(checkpoint()))
        self.assertEqual(self.supervisor.next_auto,next_auto)
        self.assertEqual(self.starts[-1][0],'operator')
        self.assertNotIn(CURSOR_SNAPSHOT_ENV,self.starts[-1][1]['env'])
        self.starts[-1][2].code=0;self.clock.now=next_auto
        self.supervisor.tick()
        self.assertEqual(self.starts[-1][0],'auto')
        self.assertEqual(decode_cursor_checkpoint(self.starts[-1][1]['env'][CURSOR_SNAPSHOT_ENV]),checkpoint())
        self.assertNotIn('cursor',str(self.supervisor.status()))

    def test_confirmed_finite_child_frontier_is_adopted_then_restored_for_next_pass(self):
        import test_common_feeder as fixtures
        from test_common_cursor_frontier import PartialRepository
        driver=fixtures.FeederTests();driver.setUp();self.addCleanup(driver.doCleanups)
        repo=PartialRepository({fixtures.channel(1):1,fixtures.channel(2):1,fixtures.channel(3):2})
        cursor=CommonCandidateCursor()
        feeder,owner,executor=driver.create(repo,limit=2,cursor=cursor)
        driver.drive(feeder,owner,executor);feeder.finish_pass(owner)
        value=_capture_collection_cursors((cursor,AutoMetadataCursor(),AutoInventoryCursor()))
        self.assertEqual(value['version'],3)
        self.assertTrue(value['common']['revisit'])
        self.assertEqual(value['common']['after']['counters']['long'],fixtures.channel(2))
        read_exit_reason(BytesIO(record(value)),self.supervisor.exit_found)
        self.process.code=0;next_auto=self.supervisor.next_auto;self.supervisor.tick()
        self.starts[-1][2].code=0;self.clock.now=next_auto;self.supervisor.tick()
        self.assertEqual(self.starts[-1][0],'auto')
        restored=(CommonCandidateCursor(),AutoMetadataCursor(),AutoInventoryCursor())
        _restore_collection_cursors(restored,decode_cursor_checkpoint(
            self.starts[-1][1]['env'][CURSOR_SNAPSHOT_ENV]))
        second,owner,executor=driver.create(repo,limit=2,cursor=restored[0])
        driver.drive(second,owner,executor);second.finish_pass(owner)
        self.assertEqual(second.summary['updated'],2)
        self.assertEqual(repo.claims[-1]['scopes'][0]['channel_id'],fixtures.channel(3))

    def test_crash_forced_unknown_or_operator_record_keeps_previous_checkpoint(self):
        previous=encode_cursor_checkpoint(checkpoint())
        for fail in ('code','forced','kill','incomplete','malformed','operator','reason'):
            self.supervisor._cursor_checkpoint=previous
            self.supervisor.exit_found={};self.accepted();self.supervisor.info['worker']='auto'
            self.supervisor.terminated_at=None;self.supervisor.kill_sent=False
            if fail=='forced':self.supervisor.terminated_at=100
            if fail=='kill':self.supervisor.kill_sent=True
            if fail=='incomplete':self.supervisor.exit_found.pop('cursor_stream_complete')
            if fail=='malformed':self.supervisor.exit_found['cursor_invalid']=True
            if fail=='operator':self.supervisor.info['worker']='operator'
            if fail=='reason':self.supervisor.exit_found['reason']='exception:RuntimeError'
            self.supervisor._adopt_cursor(1 if fail=='code' else 0)
            self.assertEqual(self.supervisor._cursor_checkpoint,previous)
        restarted=CollectionSupervisor(self.supervisor.manager,ready=lambda:None,
            schedules=handoff.CollectionSchedules(),spawn=self.supervisor.spawn)
        self.assertIsNone(restarted._cursor_checkpoint)
