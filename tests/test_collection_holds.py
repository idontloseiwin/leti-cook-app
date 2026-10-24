"""Transient failures do not stop a long video or a reactivated channel for good. Source only: no database or provider."""
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

from test_error_codes_not_text import module_parts

ROOT=Path(__file__).resolve().parents[1]
AVAILABILITY=ROOT/'application/collection/availability.py'
CHANNEL_METADATA=ROOT/'application/collection/channel_metadata.py'
sys.path.insert(0,str(ROOT/'application'))
from db.videos.availability import AvailabilityRepository  # noqa: E402
from collection_schedule import CollectionSchedules
from collection.provider_steps import run_provider_steps
from collection_policy import RecheckSchedules
from dataclasses import replace
from db.channel.channel_sync import ChannelSyncRepository  # noqa: E402
from channel_collection_scope import channel_scope, require_control_epoch  # noqa: E402

NOW=datetime(2026,9,25,12,0,tzinfo=timezone.utc)

def recheck_functions(candidates, count_changed=False):
    """The recheck with a recording selector and collector; returns (namespace, selector calls)."""
    calls=[]
    def load(channel_id,limit=50,recheck_after=None,*,context):
        calls.append({'channel_id':channel_id,'recheck_after':recheck_after})
        return list(candidates)
    ns=module_parts(AVAILABILITY,{'run_channel_availability_recheck','availability_recheck_steps','_sync_video_steps','should_trigger_availability_recheck',
                          'should_run_stale_availability_recheck','AVAILABILITY_STALE_RECHECK_AFTER',
                          'AVAILABILITY_RECHECK_STATUSES','AVAILABILITY_RECHECK_LIMIT_PER_CHANNEL'},
                    {'partial':partial,'run_provider_steps':run_provider_steps,
                     'require_control_epoch':require_control_epoch,'timedelta':timedelta,'AVAILABILITY_PRIVATE':'private','AVAILABILITY_UNKNOWN_UNAVAILABLE':'unknown_unavailable',
                     'channel_video_count_changed_since_last_discovery':lambda channel_id,*,context:count_changed,
                     'load_availability_recheck_candidate_ids':load,
                     'collection_outcome_fields':lambda summary,**kw:{},'copy_collection_outcome':lambda target,source:None,
                     '_copy_collection_stop':lambda target,source:False,'source_log_message':lambda *a,**k:''})
    context=SimpleNamespace(video=SimpleNamespace(
        run_batch=lambda **kw:{'video_ids':list(kw['explicit_video_ids']),'missing':0,'errors':0}))
    ns['run_channel_availability_recheck']=partial(ns['run_channel_availability_recheck'],context=context,expected_epoch=7)
    return ns,calls

HEAD_SCAN={'status':'no_new_videos','latest_uulf_known':True}

class PausedLongVideoRecheckTests(unittest.TestCase):
    def test_a_missing_long_video_is_a_recheck_candidate(self):
        ns,_=recheck_functions([])
        # With an API key, videos.list omits private videos: the collector records unknown_unavailable.
        self.assertEqual(ns['AVAILABILITY_RECHECK_STATUSES'],('private','unknown_unavailable'))
        self.assertEqual(ns['AVAILABILITY_RECHECK_LIMIT_PER_CHANNEL'],50)

    def test_count_change_rechecks_every_unavailable_video(self):
        ns,calls=recheck_functions(['vid1'],count_changed=True)
        summary=ns['run_channel_availability_recheck']('UC1',HEAD_SCAN,trigger_allowed=True)
        self.assertEqual(calls,[{'channel_id':'UC1','recheck_after':None}])
        self.assertEqual((summary['trigger'],summary['restored']),('video_count_changed',1))

    def test_without_a_count_change_old_misses_are_rechecked(self):
        for allowed in (True,False):
            ns,calls=recheck_functions(['vid1','vid2'])
            summary=ns['run_channel_availability_recheck']('UC1',HEAD_SCAN,trigger_allowed=allowed)
            self.assertEqual(calls,[{'channel_id':'UC1','recheck_after':timedelta(days=1)}])
            self.assertTrue(summary['triggered'])
            self.assertEqual((summary['trigger'],summary['requested'],summary['restored']),('stale_unavailable',2,2))

    def test_no_old_miss_is_not_a_triggered_recheck(self):
        ns,calls=recheck_functions([])
        summary=ns['run_channel_availability_recheck']('UC1',HEAD_SCAN,trigger_allowed=False)
        self.assertEqual(len(calls),1)
        self.assertEqual((summary['triggered'],summary['status']),(False,'not_triggered_no_increase_signal'))

    def test_only_a_settled_head_scan_rechecks_old_misses(self):
        for result in ({'status':'new_videos_found'},{'status':'error'},
                       {'status':'no_new_videos','scan_kind':'full_reconciliation'}):
            ns,calls=recheck_functions(['vid1'])
            summary=ns['run_channel_availability_recheck']('UC1',result,trigger_allowed=False)
            self.assertEqual((calls,summary['triggered']),([],False),result)

class RecheckSelectorTests(unittest.TestCase):
    class Repository(AvailabilityRepository):
        def __init__(self):
            self.params=[]
            self.database=SimpleNamespace(cadence_snapshot=CollectionSchedules().cadence_snapshot())
        def _read_recheck_candidates(self,params,**kw):
            self.params.append(params)
            return [{'video_id':'vid1'}]

    def test_interval_is_sent_in_seconds_and_bounded(self):
        repository=self.Repository()
        self.assertEqual(repository.load_recheck_candidate_ids('UC1',['private'],limit=5),['vid1'])
        self.assertNotIn('p_recheck_after',repository.params[0])
        repository.load_recheck_candidate_ids('UC1',['private'],limit=5,recheck_after=timedelta(days=1))
        self.assertEqual(repository.params[1]['p_recheck_after'],'86400 seconds')
        repository.load_recheck_candidate_ids('UC1',['private'],limit=5,recheck_after=timedelta(days=28))
        self.assertEqual(repository.params[2]['p_recheck_after'],'2419200 seconds')
        self.assertTrue(all(p['p_max_recheck_seconds']==2419200 for p in repository.params))
        for bad in (timedelta(minutes=30),timedelta(days=28,seconds=1),timedelta(days=29),86400):
            with self.assertRaises(ValueError):
                repository.load_recheck_candidate_ids('UC1',['private'],limit=5,recheck_after=bad)

    def test_configured_cap_may_be_less_than_the_one_day_base(self):
        repository=self.Repository()
        repository.database.cadence_snapshot=replace(repository.database.cadence_snapshot,
            rechecks=RecheckSchedules(unavailable_long_max_seconds=21600))
        repository.load_recheck_candidate_ids('UC1',['private'],limit=5,recheck_after=timedelta(days=1))
        self.assertEqual(repository.params[0]['p_recheck_after'],'86400 seconds')
        self.assertEqual(repository.params[0]['p_max_recheck_seconds'],21600)

    def test_rejected_intervals_do_not_reach_database(self):
        repository=self.Repository()
        for interval in (timedelta(days=28,microseconds=1),timedelta(hours=1,microseconds=-1)):
            with self.assertRaises(ValueError):
                repository.load_recheck_candidate_ids('UC1',['private'],limit=5,recheck_after=interval)
        self.assertEqual(repository.params,[])

def reactivation_functions(updates):
    repository=object()
    ns=module_parts(CHANNEL_METADATA,{'NO_LONG_REACTIVATION_ATTEMPTS','no_long_reactivation_retry_due',
                                     'defer_failed_no_long_reactivation','youtube_quota_reset_at',
                                     '_metadata_options','_metadata_update'},
                    {'timedelta':timedelta,'datetime':datetime,'timezone':timezone,'ZoneInfo':__import__('zoneinfo').ZoneInfo,
                     'ChannelSyncRepository':lambda database:repository,
                     'channel_scope':channel_scope,
                     'update_channel_values':lambda channel_id,values,*,repository,expected_epoch:updates.append((channel_id,values))})
    ns['defer_failed_no_long_reactivation']=partial(ns['defer_failed_no_long_reactivation'],
                                                  context=SimpleNamespace(database=object()))
    return ns


def skipped(count, code='youtube_api_error'):
    return {'channel_id':'UC1','collection_control_epoch':7,'initial_long_inventory_status':'skipped_no_long_playlist',
            'initial_long_inventory_error_code':code,'initial_long_inventory_error_count':count}

class ReactivationRetryTests(unittest.TestCase):
    def test_a_failed_reactivation_returns_to_no_long_with_a_retry(self):
        updates=[]
        ns=reactivation_functions(updates)
        channel=skipped(0,code=None)
        self.assertTrue(ns['defer_failed_no_long_reactivation'](channel,{'status':'error'},NOW))
        channel_id,values=updates[0]
        self.assertEqual(channel_id,'UC1')
        self.assertEqual(values['initial_long_inventory_status'],'skipped_no_long_playlist')
        self.assertEqual(values['initial_28d_stats_status'],'skipped')
        self.assertIsNone(values['initial_long_inventory_started_at'])
        self.assertEqual(values['channel_metadata_next_after'],(NOW+timedelta(hours=2)).isoformat())
        # The recorded failure (code and count) stays: it is the retry marker.
        self.assertNotIn('initial_long_inventory_error_count',values)

    def test_quota_waits_for_the_reset_without_using_an_attempt(self):
        updates=[]
        ns=reactivation_functions(updates)
        self.assertTrue(ns['defer_failed_no_long_reactivation'](skipped(2),{'status':'error','quota_exhausted':True},NOW))
        values=updates[0][1]
        self.assertEqual(values['initial_long_inventory_error_count'],2)
        self.assertEqual(values['channel_metadata_next_after'],ns['youtube_quota_reset_at'](NOW).isoformat())

    def test_repeated_failures_keep_the_operator_hold(self):
        updates=[]
        ns=reactivation_functions(updates)
        self.assertFalse(ns['defer_failed_no_long_reactivation'](skipped(2),{'status':'error'},NOW))
        self.assertEqual(updates,[])

    def test_the_marker_makes_a_retry_due(self):
        due=reactivation_functions([])['no_long_reactivation_retry_due']
        self.assertTrue(due(skipped(0)))
        self.assertTrue(due(skipped(2)))
        self.assertFalse(due(skipped(3)))
        self.assertFalse(due(skipped(0,code=None)))
        self.assertFalse(due({**skipped(0),'initial_long_inventory_status':'error'}))

class ReactivationPreparationTests(unittest.TestCase):
    def prepare(self, channel, count):
        rechecks=[]
        def recheck(channel,values,*,context):
            yield from ()
            rechecks.append(channel['channel_id'])
            values.update(initial_long_inventory_status='pending')
            return {'status':'long_playlist_available','run_initial_inventory':True}
        ns=module_parts(CHANNEL_METADATA,{'_prepare_channel_metadata_update','channel_metadata_update_steps','no_long_reactivation_retry_due','NO_LONG_REACTIVATION_ATTEMPTS','_metadata_options'},
                        {'channel_metadata_update_values':lambda item,observed_at:{'video_count':count},
                         'int_or_none':lambda v:None if v is None else int(v),
                         'has_completed_initial_long_inventory':lambda c:c.get('initial_long_inventory_status')=='completed',
                         'run_provider_steps':run_provider_steps,
                         'recheck_no_long_playlist_steps':recheck})
        return ns['_prepare_channel_metadata_update'](channel,{},NOW,context=SimpleNamespace()),rechecks

    def test_a_retry_rechecks_without_a_count_change(self):
        prepared,rechecks=self.prepare({**skipped(1),'video_count':7},7)
        self.assertEqual(rechecks,['UC1'])
        self.assertFalse(prepared['video_count_changed'])
        self.assertTrue(prepared['run_initial_inventory'])

    def test_no_marker_and_no_count_change_does_not_call_youtube(self):
        prepared,rechecks=self.prepare({**skipped(0,code=None),'video_count':7},7)
        self.assertEqual((rechecks,prepared['run_initial_inventory']),([],False))

    def test_a_count_change_still_reactivates(self):
        prepared,rechecks=self.prepare({**skipped(0,code=None),'video_count':7},8)
        self.assertEqual(rechecks,['UC1'])
        self.assertTrue(prepared['run_initial_inventory'])

    def test_the_metadata_pass_reads_the_marker(self):
        database=Mock(spec_set=['read_rows'])
        row={'channel_id':'UC1','initial_long_inventory_error_code':'provider_quota_exceeded',
             'initial_long_inventory_error_count':2}
        database.read_rows.return_value=[row]
        self.assertEqual(ChannelSyncRepository(database).load_due_metadata_page(NOW,page=0,page_size=25),[row])
        database.read_rows.assert_called_once()
        operation,statement,parameters=database.read_rows.call_args.args
        self.assertEqual(operation,'channel_sync_due_metadata')
        self.assertIn('FROM public.channels_with_control',statement)
        columns={name.strip() for name in statement.removeprefix('SELECT ').split(' FROM ',1)[0].split(',')}
        self.assertTrue({'initial_long_inventory_error_code','initial_long_inventory_error_count'} <= columns)
        self.assertEqual(parameters,('2026-09-25T12:00:00Z',25,0))
        self.assertEqual(database.read_rows.call_args.kwargs,{'max_rows':25})

if __name__=='__main__':unittest.main()
