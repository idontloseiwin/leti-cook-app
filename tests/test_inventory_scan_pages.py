"""Page/finish ordering and atomicity on the real driver with offline recording IO."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from pathlib import Path
from dataclasses import replace
from datetime import timedelta
import sys
import unittest
from unittest.mock import patch
import psycopg

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.inventory import inventory_scan_pages as pages,membership_page_effects as member_effects,membership_absence_effects as absent_effects
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresDatabase,PostgresConfig
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_inventory_scan_steps import A,SCAN,LEASE,TRANSITION,STAMP,NOW,HASH,one,changed,control,receipt

VIDEO='aaaaaaaaaaa';OTHER='bbbbbbbbbbb';THIRD='ccccccccccc'


def inputs(**changes):
    return dict(dict(channel_id=A,video_type='short',lease_token=LEASE,scan_id=SCAN,transition_id=TRANSITION,
        expected_page_token='prior',next_page_token=None,page_status='complete',items=[{'video_id':VIDEO,'published_at':'10000-01-01 UTC'}],
        source_checked_at=STAMP),**changes)
def source(**changes):
    return dict(dict(channel_id=A,video_type='short',runtime_enabled=True,status='leased',lease_token=LEASE,
        lease_expires_at='2026-10-06T13:00:00+00:00',active_scan_id=SCAN,lease_channel_epoch=7,
        no_new_streak=4,attempt_count=2,discovery_error_count=2,source_checked_at=STAMP),**changes)
def scan(**changes):
    return dict(dict(scan_id=SCAN,channel_id=A,video_type='short',status='running',resume_page_token='prior',
        scan_kind='initial_inventory',token_history_verified=True,playlist_id='UUSH'+A[2:],
        scan_boundary_at='2026-10-06T11:00:00+00:00',started_at='2026-10-06T11:00:01+00:00',new_memberships_found=1,pages_completed=3),**changes)
def item_fact(video=VIDEO,**changes):
    return dict(dict(kind='object',video_id=video,has_published_at=True,published_kind='string'),**changes)


class InventoryPageTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool,
            cadence_snapshot=CollectionSchedules().cadence_snapshot()).open()
        self.addCleanup(self.database.close)
        queue_patch=patch.object(pages,'ensure_video_collection_state_in_transaction',return_value=1)
        self.queue_helper=queue_patch.start();self.addCleanup(queue_patch.stop)
        # Owner protocol fixtures stop at the separately tested native effect
        # boundaries; source RETURNING capture and atomic final fences stay real.
        for module,name,options in (
            (pages,'after_page_inventory_inserts',{}),
            (member_effects,'after_page_membership_changes',{}),
            (absent_effects,'mark_absent_memberships',{'side_effect':lambda tx,args:tx.execute(pages._ABSENT,args)})):
            pending=patch.object(module,name,**options);setattr(self,name,pending.start());self.addCleanup(pending.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def invoke(self,**changes):return pages.settle_typed_inventory_page(self.database,**inputs(**changes))
    def start(self,values=None,*,stored=None,facts=None):
        self.queue(one(value=values or inputs(),future=False),one(valid=True),Result(tuple([item_fact()] if facts is None else facts)),
            one(value=HASH),one(lock=None),one(value=stored) if stored is not None else Result())
    def owned(self,*,stored_scan=None,discovery=None):
        self.queue(one(value=discovery or source()),one(active_scan_id=SCAN),one(value=stored_scan or scan()),one(valid=True),one(valid=True),one(policy_facts=native_eligible_facts(True)))
    def clocks(self,*,continuation=False):
        self.queue(one(valid=True),one(valid=True),one(before_boundary=False))
        if continuation:self.queue(one(present=False))
    def mutations(self,*,long=False,full=True,inserted=(VIDEO,),canonical=(VIDEO,),new_count=1,total=2,absent=2,queue_ids=None):
        self.queue(one(lock=None),one(count=new_count),Result(tuple({'video_id':v,'effect_new':'NATIVE_INVENTORY'} for v in inserted)),
            one(inserted_ids=list(inserted),observed_ids=[VIDEO],conflicts=0),one(video_id=VIDEO,inserted=True,value='NATIVE_MEMBER'),one(items_seen=4,total_new=total),one(pages_completed=4))
        if full:self.queue(changed(absent))
        self.queue(one(at=NOW))
        if long:self.queue(one(pending=True))
        self.queue(one(next_due_at=STAMP),one(ids=list(canonical)))
        if not long:self.queue(one(ids=list(canonical) if queue_ids is None else queue_ids))
    def tail(self,held=None):self.queue(changed(),one(valid=True),one(**(held or control())))
    def expect(self,token,call=None,state='P0001'):
        with self.assertRaises(DatabaseServerError) as caught:(call or self.invoke)()
        self.assertEqual((caught.exception.code,caught.exception.message),(state,token))

    def test_full_finish_counts_receipt_and_ordered_atomic_transitions(self):
        self.start();self.owned();self.clocks();self.mutations();self.tail()
        result=self.invoke()
        self.assertEqual(result['memberships_marked_absent'],2);self.assertEqual(result['new_memberships_found'],2)
        self.assertEqual((result['status'],result['coverage'],result['pages_completed'],result['items_seen']),('completed','complete',4,4))
        self.assertEqual(result['canonical_video_ids'],[VIDEO]);self.assertEqual(result['new_video_ids'],[VIDEO])
        self.assertEqual(result['request_hash'],HASH);self.assertFalse(result['pending_channel_count_change'])
        statements=[sql for sql,_ in self.connection.queries]
        positions=[next(i for i,sql in enumerate(statements) if fragment in sql) for fragment in
            ("'inventory-transition:'",'FOR UPDATE','hashtextextended(item', 'INSERT INTO public.video_inventory(',
             'INSERT INTO public.video_inventory_memberships','UPDATE public.channel_video_inventory_scans',
             "SET membership_status='absent'",'UPDATE public.channel_playlist_state','INSERT INTO public.typed_inventory_receipts','FOR SHARE')]
        self.assertEqual(positions,sorted(positions))
        self.assertEqual(self.connection.queries[-3][1][2].obj,result)
        self.assertEqual(self.queue_helper.call_args.args[1],[VIDEO])
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
        self.assertEqual(self.after_page_inventory_inserts.call_args.args[1],[dict(video_id=VIDEO,effect_new='NATIVE_INVENTORY')])
        self.assertEqual(self.after_page_membership_changes.call_args.args[1],[dict(operation='insert',old_text=None,new_text='NATIVE_MEMBER')])

    def test_continuation_retains_checkpoint_and_long_full_source_clock_without_absence(self):
        values=inputs(video_type='long',page_status='continuation',next_page_token='next')
        self.start(values);self.owned(discovery=source(video_type='long'),stored_scan=scan(video_type='long',scan_kind='full_reconciliation'))
        self.clocks(continuation=True);self.mutations(long=True,full=False);self.tail()
        result=self.invoke(video_type='long',page_status='continuation',next_page_token='next')
        self.assertEqual((result['status'],result['coverage'],result['resume_page_token']),('running','partial','next'))
        self.assertEqual(result['memberships_marked_absent'],0);self.assertTrue(result['pending_channel_count_change'])
        release=next((sql,params) for sql,params in self.connection.queries if 'UPDATE public.channel_playlist_state' in sql)
        self.assertFalse(release[1]['terminal']);self.assertEqual(release[1]['next_due_at'],NOW)
        self.assertIn("THEN source_checked_at ELSE %(source_checked_at)s",release[0])
        self.assertFalse(any("SET membership_status='absent'" in sql for sql,_ in self.connection.queries))
        self.assertEqual(self.connection.queries[-3][1][-2:],('continuation','next'))

    def test_long_schedules_only_new_ids_still_canonical_after_membership_triggers(self):
        self.start(inputs(video_type='long'));self.owned(discovery=source(video_type='long'),stored_scan=scan(video_type='long'))
        self.clocks();self.mutations(long=True,inserted=(VIDEO,OTHER),canonical=(VIDEO,THIRD),total=3);self.tail()
        result=self.invoke(video_type='long')
        self.assertEqual(self.queue_helper.call_args.args[1],[VIDEO]);self.assertEqual(result['new_video_ids'],[VIDEO,OTHER])
        progress=next(params for sql,params in self.connection.queries if 'AS total_new' in sql)
        self.assertEqual(progress[-1],2)

    def test_typed_schedules_eligible_canonical_ids_after_source_release(self):
        self.start();self.owned();self.clocks();self.mutations(canonical=(VIDEO,OTHER),queue_ids=[OTHER]);self.tail()
        self.invoke();self.assertEqual(self.queue_helper.call_args.args[1],[OTHER])
        statements=[sql for sql,_ in self.connection.queries]
        self.assertLess(next(i for i,s in enumerate(statements) if 'UPDATE public.channel_playlist_state' in s),
                        next(i for i,s in enumerate(statements) if pages._TYPED_QUEUE.strip() in s))

    def test_stream_page_owns_queue_initialization_before_receipt_commit(self):
        self.start(inputs(video_type='stream'))
        self.owned(discovery=source(video_type='stream'),stored_scan=scan(video_type='stream'))
        self.clocks();self.mutations();self.tail()
        result=self.invoke(video_type='stream')
        self.assertEqual(result['video_type'],'stream')
        self.assertEqual(result['queue_inserted'],1)
        self.assertEqual(self.queue_helper.call_args.args[1],[VIDEO])
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
        self.assertTrue(any('INSERT INTO public.typed_inventory_receipts' in sql for sql,_ in self.connection.queries))

    def test_exact_receipt_replays_before_lease_and_publication_timestamp_casts(self):
        values=inputs(items=[{'video_id':VIDEO,'published_at':'bad timestamp'}])
        stored=receipt(transition_kind='page',result={'request_hash':HASH,'complete':True,'replayed':False})
        self.start(values,stored=stored);self.queue(one(valid=True),one(valid=True),one(valid=True))
        self.assertEqual(self.invoke(items=values['items']),{**stored['result'],'replayed':True})
        self.assertFalse(any('FOR UPDATE' in sql or 'INSERT INTO' in sql for sql,_ in self.connection.queries))
        self.queue_helper.assert_not_called()

    def test_page_writer_uses_only_selected_type_schedule(self):
        base=CollectionSchedules()
        for kind, seconds in (('long',1111),('short',2222),('stream',3333)):
            settings=replace(base,**{kind+'_inventory':replace(getattr(base,kind+'_inventory'),found_new_seconds=seconds)})
            self.start(inputs(video_type=kind));self.owned(discovery=source(video_type=kind),stored_scan=scan(video_type=kind))
            self.clocks();self.mutations(long=kind=='long');self.tail()
            with patch.object(self.database,'_cadence_snapshot',settings.cadence_snapshot()):
                self.invoke(video_type=kind)
            release=[params for sql,params in self.connection.queries if 'UPDATE public.channel_playlist_state' in sql][-1]
            self.assertEqual(release['next_due_at'],NOW+timedelta(seconds=seconds))

    def test_acknowledged_page_keeps_original_deadline_without_schedule_recomputation(self):
        stored=receipt(transition_kind='page',result={'request_hash':HASH,'next_due_at':STAMP,'replayed':False})
        self.start(stored=stored);self.queue(one(valid=True),one(valid=True),one(valid=True))
        with patch.object(pages,'require_cadence_snapshot',side_effect=AssertionError('must not recompute')):
            self.assertEqual(self.invoke(),{**stored['result'],'replayed':True})

    def test_native_argument_cast_and_hot_clock_precede_body_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        self.expect('database_operation_failed',lambda:self.invoke(channel_id='',lease_token='bad'),state='22P02')
        self.queue(one(value=inputs(channel_id='',source_checked_at=None),future=None),one(valid=False))
        self.expect('hot_source_clock_out_of_window',lambda:self.invoke(channel_id='',source_checked_at=None),state='22023')

    def test_envelope_validation_order_and_empty_continuation_have_no_advisory_lock(self):
        vectors=[(dict(expected_page_token=''),'typed_inventory_expected_token_invalid'),
            (dict(page_status='continuation',next_page_token='prior'),'typed_inventory_continuation_invalid'),
            (dict(next_page_token='next'),'typed_inventory_terminal_token_invalid'),
            (dict(items=None),'typed_inventory_page_items_invalid'),
            (dict(items=[{}]*51),'typed_inventory_page_too_large'),
            (dict(items=[],page_status='continuation',next_page_token='next'),'typed_inventory_empty_page_with_continuation'),
            (dict(page_status='empty'),'typed_inventory_empty_page_has_items')]
        for changes,token in vectors:
            self.queue(one(value=inputs(**changes),future=False),one(valid=True))
            self.expect(token,lambda:self.invoke(**changes))
        self.assertFalse(any('pg_advisory' in sql for sql,_ in self.connection.queries))

    def test_native_item_text_coercion_and_duplicate_check_precede_receipt_lookup(self):
        values=inputs(items=[{'video_id':12345678901},{'video_id':'12345678901'}])
        self.queue(one(value=values,future=False),one(valid=True),Result((item_fact('12345678901'),item_fact('12345678901'))))
        self.expect('typed_inventory_page_items_not_unique',lambda:self.invoke(items=values['items']))
        self.queue(one(value=inputs(),future=False),one(valid=True),one(**item_fact(published_kind='number')))
        self.expect('typed_inventory_page_item_invalid')
        self.assertFalse(any('typed_inventory_receipts' in sql for sql,_ in self.connection.queries))

    def test_eligibility_checkpoint_token_history_and_hot_boundary_precede_video_locks(self):
        self.start();self.owned(stored_scan=scan(token_history_verified=False))
        self.expect('inventory_token_history_invalid')
        self.start();self.owned(stored_scan=scan(resume_page_token='wrong'))
        self.expect('typed_inventory_page_checkpoint_mismatch')
        self.start();self.owned();self.queue(one(valid=True),one(valid=True),one(before_boundary=True))
        self.expect('typed_inventory_source_before_scan')
        self.assertFalse(any('hashtextextended(item' in sql for sql,_ in self.connection.queries))

    def test_token_cycle_and_boundary_rejection_do_not_mutate(self):
        self.start(inputs(page_status='continuation',next_page_token='next'));self.owned()
        self.queue(one(valid=True),one(valid=True),one(before_boundary=False),one(present=True))
        self.expect('inventory_token_cycle',lambda:self.invoke(page_status='continuation',next_page_token='next'))
        self.start(inputs(page_status='boundary'));self.owned();self.clocks()
        self.expect('typed_inventory_boundary_requires_discovery',lambda:self.invoke(page_status='boundary'))
        self.assertFalse(any('INSERT INTO public.video_inventory' in sql for sql,_ in self.connection.queries))

    def test_native_publication_failure_after_video_locks_rolls_back_before_membership_effects(self):
        self.start();self.owned();self.clocks()
        self.queue(one(lock=None),one(count=1),Result(execute_error=ServerFailure('22007')))
        self.expect('database_operation_failed',state='22007')
        self.assertIn('hashtextextended(item',self.connection.queries[-3][0])
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
        self.queue_helper.assert_not_called()

    def test_null_page_status_preserves_late_policy_failure_after_native_mutations(self):
        self.start(inputs(page_status=None));self.owned();self.clocks()
        self.queue(one(lock=None),one(count=1),one(video_id=VIDEO,effect_new="NATIVE_INVENTORY"),
            one(inserted_ids=[VIDEO],observed_ids=[VIDEO],conflicts=0),one(video_id=VIDEO,inserted=True,value='NATIVE_MEMBER'),one(items_seen=1,total_new=2),
            one(pages_completed=4),one(at=NOW))
        self.expect('inventory_policy_input_invalid',lambda:self.invoke(page_status=None))
        scan_write=next(params for sql,params in self.connection.queries if 'UPDATE public.channel_video_inventory_scans' in sql)
        self.assertIsNone(scan_write['terminal']);self.assertIsNone(scan_write['complete_coverage'])
        self.assertFalse(any('UPDATE public.channel_playlist_state' in sql for sql,_ in self.connection.queries))
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1));self.queue_helper.assert_not_called()

    def test_native_progress_overflow_rolls_back_canonical_and_membership_changes(self):
        self.start();self.owned(stored_scan=scan(new_memberships_found=2147483647));self.clocks()
        self.queue(one(lock=None),one(count=1),one(video_id=VIDEO,effect_new="NATIVE_INVENTORY"),one(inserted_ids=[VIDEO],observed_ids=[VIDEO],conflicts=0),
            one(video_id=VIDEO,inserted=True,value='NATIVE_MEMBER'),Result(execute_error=ServerFailure('22003')))
        self.expect('database_operation_failed',state='22003')
        self.assertIn('coalesce(%s::integer,0)+%s::integer',self.connection.queries[-1][0])
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1));self.queue_helper.assert_not_called()

    def test_receipt_acl_failure_rolls_back_already_initialized_queue(self):
        self.start();self.owned();self.clocks();self.mutations()
        self.queue(Result(execute_error=ServerFailure('42501')))
        self.expect('database_operation_failed',state='42501')
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1));self.queue_helper.assert_called_once()

    def test_stale_final_scope_rolls_back_memberships_queue_scan_and_receipt(self):
        self.start();self.owned();self.clocks();self.mutations();self.tail(control(control_epoch=8))
        self.expect('channel_collection_scope_lost')
        self.queue_helper.assert_called_once();self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
        self.assertIn('INSERT INTO public.typed_inventory_receipts',self.connection.queries[-3][0])

    def test_membership_preserves_later_observation_and_absence_uses_scan_boundary(self):
        self.assertIn('WHERE membership.last_present_at IS NULL OR excluded.last_present_at>=membership.last_present_at',pages._UPSERT_MEMBERSHIPS)
        self.assertIn('last_present_at<=%(scan_boundary_at)s::timestamptz',pages._ABSENT)
        self.assertIn('last_seen_scan_id IS DISTINCT FROM %(scan_id)s::uuid',pages._ABSENT)
        self.assertNotIn('DELETE',pages._ABSENT)

    def test_unknown_commit_has_one_dispatch_sequence_and_no_acknowledgement(self):
        self.start();self.owned();self.clocks();self.mutations();self.tail()
        self.connection.commit_error=psycopg.OperationalError('private commit')
        with self.assertRaises(DatabaseUnavailable) as caught:self.invoke()
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
        self.queue_helper.assert_called_once()

    def test_caught_borrowed_error_cannot_commit_parent_write(self):
        self.queue(changed(),one(value=inputs(expected_page_token=''),future=False),one(valid=True))
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation('page_borrow_test')) as tx:
                tx.execute("UPDATE public.channels SET title='prior' WHERE false")
                with self.assertRaises(DatabaseServerError):pages.settle_page_in_transaction(tx,**inputs(expected_page_token=''))
        self.assertEqual(caught.exception.message,'database_transaction_aborted');self.assertEqual(self.connection.commits,0)


if __name__=='__main__':unittest.main()
