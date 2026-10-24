"""RR15 all-family exact retry proofs through the public transaction, offline."""
from datetime import timedelta
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from db.hot.hot_retention import CleanupPage, PRIORITY_TARGETS, TARGET_FUNCTIONS, RETENTION_WINDOWS, PriorityReference, RetentionError
from db.retention import priority_retention as dispatch, evidence_retention as evidence
from db.retention import deleted_channel_retention as deleted
from hot_retention import HotCleanupCycle
import test_priority_retention as support
from test_priority_retention import one, count, Result, ServerFailure, AT, FLOOR, OLD, VIDEO, ORIGINAL
from test_priority_retry import runtime, tick, iso, DAY

UUID='00000000-0000-4000-8000-000000000001'
KEYS={
 'video_counters':(VIDEO,), 'video_metadata':(VIDEO,), 'video_availability':(VIDEO,),
 'channel_profile':(ORIGINAL,), 'channel_country':(ORIGINAL,), 'inventory':(VIDEO,),
 'memberships':(VIDEO,ORIGINAL,'long'), 'video_snapshots':('42',),
 'channel_snapshots':(ORIGINAL,OLD), 'typed_video_collection_receipts':(VIDEO,UUID),
 'availability_events':('42',), 'availability_incidents':('42',), 'review_evidence':(UUID,),
 'playlist_state':(ORIGINAL,'long'), 'inventory_scans':(UUID,),
 'inventory_receipts':('scan_transition',UUID), 'operator_jobs':(UUID,),
 'operator_job_targets':(UUID,VIDEO), 'sync_runs':('42',), 'quota_attempts':(UUID,),
 'deleted_saved_objects':('collection',UUID), 'category_reference':('singleton',),
 'deleted_channels':('channel',ORIGINAL),
}


class ExactFamilyTests(unittest.TestCase):
    setUp=support.PublicPriorityTests.setUp
    queue=support.PublicPriorityTests.queue
    start=support.PublicPriorityTests.start

    def header(self,target):
        if target in ('video_counters','video_metadata','video_availability','channel_profile','channel_country'):
            self.queue(one(target=target,after_id=None,limit=1,at=AT,cutoff=FLOOR))
        elif target in evidence._TARGETS or target in ('deleted_saved_objects','category_reference'):
            floor='2026-09-06T12:00:00+00:00' if target=='deleted_saved_objects' else FLOOR
            self.queue(one(target=target,after_id=None,limit=1),one(at=AT,cutoff=floor))
        elif target=='deleted_channels':
            self.queue(one(roles_valid=True,relations_valid=True,privileges_valid=True))
            self.queue(one(target=target,after_id=None,limit=1,at=AT,cutoff=FLOOR))
        else:
            self.queue(one(target=target,cursor=None,cursor_length=None,limit=1,at=AT,cutoff=FLOOR))
            if target=='operator_jobs':self.queue(one(value=None))

    def invoke(self,target,key=None):
        requested=(PriorityReference(target,key or KEYS[target]),)
        value=self.repository.cleanup_priority(requested)
        self.assertEqual(value.checked,requested)
        self.assertTrue(value.complete)
        self.assertFalse(self.connection.results,'Every queued statement result must be consumed')
        return value

    def test_every_blocking_family_has_one_exact_dispatch_and_missing_row_resolves(self):
        self.assertEqual(set(KEYS),set(PRIORITY_TARGETS))
        self.assertEqual(set(dispatch._ORDER),set(KEYS))
        self.assertEqual(set(TARGET_FUNCTIONS)-set(KEYS),{'channel_inventory_estimate'})
        for target in KEYS:
            with self.subTest(target=target):
                before=len(self.connection.queries)
                self.start();self.header(target);self.queue(Result())
                if target=='deleted_channels':self.queue(Result(),count(0))
                result=self.invoke(target)
                self.assertEqual(result.remaining,())
                statements=self.connection.queries[before:]
                self.assertFalse(any('after_clock' in q or 'after_video' in q or 'after_id)s' in q for q,p in statements))
                for sql,params in statements:PostgresQuery(Transformer()).convert(sql,params)
                if target!='category_reference':
                    self.assertTrue(any('ANY(' in q or 'jsonb_to_recordset' in q for q,p in statements))
        self.assertEqual(self.connection.rollbacks,0)
        self.assertEqual(self.pool.borrowed,23)
        self.assertEqual(set(self.gates),{'cleanup_hot_priority'})

    def test_deleted_channel_readiness_is_required_before_its_owner_starts(self):
        self.start();self.queue(one(roles_valid=True,relations_valid=True,privileges_valid=False))
        with self.assertRaises(RetentionError):self.invoke('deleted_channels')
        self.assertEqual(len(self.connection.queries),2)
        self.assertNotIn('FROM public.channels',self.connection.queries[-1][0])
        self.assertEqual(self.connection.rollbacks,1)

    def test_priority_inherits_only_the_two_fixed_child_cursor_capabilities(self):
        from test_quota_retry_cursor import create as quota_cursor
        from test_channel_membership_cascade import create as channel_cursor
        for create,method,kwargs,size in (
            (quota_cursor,'quota_retry_children',dict(parent_id=UUID),1),
            (channel_cursor,'channel_membership_cascade_rows',dict(channel_ids=[ORIGINAL]),50),
        ):
            tx,connection,gate=create([],rpc_name='cleanup_hot_priority')
            with getattr(tx,method)(**kwargs) as rows:self.assertEqual(list(rows),[])
            self.assertEqual(connection.fetch_sizes,[size])
            self.assertEqual(connection.closed,1)
            self.assertTrue(all(call.args==('cleanup_hot_priority',) for call in gate.call_args_list))

    def test_all_evidence_primary_keys_remain_exact_when_owner_is_busy(self):
        for target,spec in evidence._TARGETS.items():
            with self.subTest(target=target):
                self.start();self.header(target)
                native=dict(zip((name for name,cast in evidence._REFERENCE_KEYS[target]),KEYS[target]))
                candidate=dict(row_data=native,work_at=OLD,work_key='key',video_id=None if target=='channel_snapshots' else VIDEO,
                               expired=True,invalid_clock=False,cursor=None)
                self.queue(Result(rows=(candidate,)),one(eligible=True))
                if target=='review_evidence':self.queue(one(acquired=False))
                elif target=='channel_snapshots':self.queue(Result())
                else:self.queue(Result(execute_error=ServerFailure('55P03')))
                self.queue(one(value=OLD))
                self.assertEqual(self.invoke(target).remaining,(PriorityReference(target,KEYS[target]),))
        self.assertFalse(any(sql.startswith(('DELETE','UPDATE')) for sql,p in self.connection.queries))

    def test_inventory_copy_locks_before_readable_clock_remain_unknown_causes(self):
        for target in ('playlist_state','inventory_scans','inventory_receipts'):
            with self.subTest(target=target):
                self.start();self.header(target)
                key=KEYS[target]
                self.queue(Result(rows=(dict(a=key[0],b=key[1] if len(key)>1 else '',cursor=None),)),one(at=AT),
                           Result(execute_error=ServerFailure('55P03')))
                if target=='playlist_state':self.queue(Result())  # exact MVCC observation unavailable
                self.assertEqual(self.invoke(target).remaining,(PriorityReference(target,key),))
        self.assertEqual(self.connection.commits,3)
        self.assertEqual(self.connection.rollbacks,3)

    def test_operational_copy_locks_before_clock_preserve_exact_cause(self):
        for target in ('operator_jobs','operator_job_targets','sync_runs','quota_attempts'):
            with self.subTest(target=target):
                self.start();self.header(target)
                key=KEYS[target]
                self.queue(Result(rows=(dict(id=key[0],video=key[1] if len(key)>1 else None,cursor=None),)),
                           Result(execute_error=ServerFailure('55P03')),one(oldest=None,invalid=0))
                self.assertEqual(self.invoke(target).remaining,(PriorityReference(target,key),))
        self.assertEqual(self.pool.borrowed,4)

    def test_saved_invalid_tombstone_cannot_be_cleared_by_unlock_or_retry(self):
        target='deleted_saved_objects'
        for invalid in (False,True):
            self.start();self.header(target)
            self.queue(Result(rows=(dict(kind='collection',id=UUID,deleted_at='infinity' if invalid else OLD,
                                        expired=not invalid,invalid_clock=invalid),)))
            if not invalid:self.queue(Result(execute_error=ServerFailure('55P03')),one(value=OLD))
            self.assertEqual(self.invoke(target).remaining,(PriorityReference(target,KEYS[target]),))
        self.assertFalse(any(sql.startswith('DELETE') for sql,p in self.connection.queries))

    def test_category_and_pending_restore_have_exact_non_video_identity(self):
        target='category_reference'
        self.start();self.header(target)
        self.queue(Result(rows=(dict(source_at=OLD,expired=True,invalid_clock=False),)),Result(execute_error=ServerFailure('55P03')))
        self.assertEqual(self.invoke(target).remaining,(PriorityReference(target,KEYS[target]),))
        self.start();self.header('deleted_channels');self.queue(Result(),Result(rows=(dict(id=UUID),)),
            Result(execute_error=ServerFailure('55P03')),count(0))
        self.assertEqual(self.invoke('deleted_channels',('restore',UUID)).remaining,(PriorityReference('deleted_channels',('restore',UUID)),))

    def test_channel_child_chunks_keep_reason_until_done_and_final_fence_still_applies(self):
        for done in (False,True):
            self.start();self.header('deleted_channels')
            self.queue(Result(rows=(dict(channel_id=ORIGINAL),)),
                       one(channel_id=ORIGINAL,owner=UUID,control_epoch=7,has_payload=False),one(busy=False),
                       Result(),one(channel_id=ORIGINAL,valid=True),count(int(done)))
            with patch.object(deleted,'_children',return_value=(4,done)),patch.object(deleted,'drain_channel_memberships') as drain:
                value=self.invoke('deleted_channels')
            self.assertEqual(bool(value.remaining),not done)
            self.assertEqual(value.deleted,4+int(done))
            drain.assert_called_once_with(unittest.mock.ANY,[ORIGINAL] if done else [])
        # A final hidden generation mismatch remains outside row-local catches.
        self.start();self.header('deleted_channels')
        self.queue(Result(rows=(dict(channel_id=ORIGINAL),)),
                   one(channel_id=ORIGINAL,owner=UUID,control_epoch=7,has_payload=True),one(busy=True),
                   Result(),one(channel_id=ORIGINAL,valid=False))
        with self.assertRaises(RetentionError):self.invoke('deleted_channels')
        self.assertEqual(self.connection.rollbacks,1)

    def test_pending_restore_only_page_proof_and_second_sample_overflow(self):
        base=dict(as_of=AT,cutoff=FLOOR,scanned=0,deleted=0,scrubbed=0,deferred=1,complete=True,
                  next_cursor=None,confirmed_expired_remaining=1,priority_complete=True,
                  priority_references=[dict(target='deleted_channels',key=['restore',UUID])])
        page=CleanupPage.parse('deleted_channels',base,1)
        self.assertTrue(page.priority_complete)
        from hot_retention import PriorityRetrySet
        registry=PriorityRetrySet();registry.observe(page,0)
        self.assertFalse(registry.incomplete)
        large=dict(base,scanned=100,deferred=200,confirmed_expired_remaining=200,priority_complete=False,
                   priority_references=[dict(target='deleted_channels',key=['channel',str(i)]) for i in range(100)])
        registry.observe(CleanupPage.parse('deleted_channels',large,100),1)
        self.assertTrue(registry.incomplete)

    def test_confirmed_removal_expiry_contract_and_oldest_validation(self):
        base=dict(as_of=AT,cutoff=FLOOR,scanned=1,deleted=0,scrubbed=0,deferred=1,
                  complete=True,next_cursor=None,priority_complete=True,
                  priority_references=[dict(target='deleted_channels',key=['channel',ORIGINAL])])
        self.assertEqual(CleanupPage.parse('deleted_channels',base,1).confirmed_expired_remaining,0)
        page=CleanupPage.parse('deleted_channels',dict(base,confirmed_expired_remaining=1),1)
        self.assertEqual(page.summary()['confirmed_expired_remaining'],1)
        self.assertEqual(page.unknown_expiry_remaining,0)
        for value in (-1,True,'1',2):
            with self.subTest(value=value),self.assertRaises(RetentionError):
                CleanupPage.parse('deleted_channels',dict(base,confirmed_expired_remaining=value),1)
        with self.assertRaises(RetentionError):
            CleanupPage.parse('inventory',dict(base,confirmed_expired_remaining=1,priority_complete=False,priority_references=[]),1)
        for oldest in (FLOOR,AT,'infinity'):
            with self.subTest(oldest=oldest),self.assertRaises(RetentionError):
                CleanupPage.parse('inventory',dict(base,priority_complete=False,priority_references=[],
                    oldest_remaining_expired_source_at=oldest),1)


class FamilyRuntimeReleaseTests(unittest.TestCase):
    def test_each_family_releases_within_two_short_retries_only_after_both_causes_clear(self):
        for target,key in KEYS.items():
            with self.subTest(target=target):
                controller,repository=runtime()
                a=PriorityReference(target,key)
                if target=='category_reference':b=PriorityReference('video_metadata',('second',))
                else:
                    changed=list(key)
                    index=1 if target in ('deleted_channels','deleted_saved_objects') else 0
                    changed[index]+='-second'
                    b=PriorityReference(target,tuple(changed))
                repository.held={a,b}
                # First complete three real traversals; then leave an unrelated
                # target unfinished to prove that exact ACK releases the pause.
                endless='channel_inventory_estimate'
                full_rounds=0
                def pages(current,*,after_id=None,limit=100):
                    nonlocal full_rounds
                    window=RETENTION_WINDOWS.get(current,timedelta(days=29)).total_seconds()
                    if current==endless:
                        full_rounds+=1
                        done=full_rounds<=3
                        return CleanupPage.parse(current,dict(as_of=iso(repository.wall),cutoff=iso(repository.wall-window),
                            scanned=0,deleted=0,scrubbed=0,deferred=0,complete=done,
                            next_cursor=None if done else str(full_rounds)),100)
                    refs=tuple(x for x in repository.held if x.target==current)
                    return CleanupPage.parse(current,dict(as_of=iso(repository.wall),cutoff=iso(repository.wall-window),
                        scanned=len(refs),deleted=0,scrubbed=0,deferred=len(refs),complete=True,next_cursor=None,
                        invalid_clock_remaining=len(refs) if current!='deleted_channels' else 0,
                        confirmed_expired_remaining=len(refs) if current=='deleted_channels' else 0,
                        priority_complete=True,priority_references=[x.wire() for x in refs]),100)
                repository.cleanup_page=pages
                controller.cycle=HotCleanupCycle(repository,targets=tuple(dict.fromkeys((a.target,b.target,endless))))
                for _ in range(300):
                    tick(controller,repository,0.1)
                    if full_rounds>=4:break
                self.assertEqual(full_rounds,4)
                self.assertTrue(controller.status()['collection_pause_requested'])
                self.assertGreaterEqual(controller._held_passes,3)
                self.assertEqual(controller.cycle.summary()['status'],'in_progress')
                prior_retries=len(repository.retries)
                before=tuple(controller.cycle._pending)
                repository.held.remove(a);tick(controller,repository,5)
                self.assertTrue(controller.status()['collection_pause_requested'])
                self.assertEqual(set(controller._priority.references),{b})
                repository.held.clear();tick(controller,repository,5)
                self.assertFalse(controller.status()['collection_pause_requested'])
                self.assertEqual(tuple(controller.cycle._pending),before)
                self.assertEqual(len(repository.retries)-prior_retries,2)
                self.assertTrue(controller.status()['api_read']['allowed'])
                self.assertTrue(controller.status()['user_work']['allowed'])


if __name__=='__main__':unittest.main()
