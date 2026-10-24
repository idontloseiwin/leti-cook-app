"""Untyped recovery never fabricates a format, counter, or review task."""
from contextlib import nullcontext
from datetime import datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.videos.video_collection import VideoCollectionClaim, VideoCollectionRepository
from db.videos import video_identity_recovery as recovery
from db.base.postgres import _reviewed_statement, DatabaseError, DatabaseServerError
from collection_policy import RecheckSchedules
from collection_schedule import CollectionSchedules
from video_collection_service import VideoCollectionService
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb

VIDEO = 'aaaaaaaaaaa'
CHANNEL = 'UC' + 'a' * 22
STAMP = '2026-10-08T12:00:00+00:00'


def claim():
    return VideoCollectionClaim(VIDEO,CHANNEL,'identity_refresh','lease',STAMP,STAMP,'unknown')


def metadata():
    return {'id':VIDEO,'snippet':{'channelId':CHANNEL,'title':'Fresh title','publishedAt':STAMP},
            'contentDetails':{'duration':'PT5M'},'status':{'privacyStatus':'unlisted','uploadStatus':'processed'}}


class IdentityServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock(spec=['complete_identity_refresh','fail_batch'])
        self.repo.complete_identity_refresh.side_effect = lambda rows: len(rows)
        self.metadata = Mock(return_value=[metadata()])
        self.counters = Mock(side_effect=AssertionError('no statistics request for an unknown type'))
        self.service = VideoCollectionService(self.repo, fetch_full=Mock(), fetch_metadata=self.metadata,
            fetch_counters=self.counters, build_full_payload=Mock(side_effect=AssertionError('no typed payload')),
            build_counter_payload=Mock())
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect', side_effect=AssertionError('network forbidden')).start()

    def test_refresh_saves_metadata_without_requiring_or_inventing_counters(self):
        result = self.service.process_common_claims([claim()])
        self.assertEqual((result['updated'],result['unknown_updated'],result['errors']),(1,1,0))
        row = self.repo.complete_identity_refresh.call_args.args[0][0]
        self.assertEqual((row['title'],row['video_type'],row['missing']),('Fresh title','unknown',False))
        self.assertNotIn('views',row)
        self.counters.assert_not_called()
        self.repo.fail_batch.assert_not_called()

    def test_valid_omission_is_one_observation_not_a_typed_missing_write(self):
        self.metadata.return_value=[]
        result=self.service.process_common_claims([claim()])
        self.assertEqual((result['missing'],result['unknown_missing']),(1,1))
        row=self.repo.complete_identity_refresh.call_args.args[0][0]
        self.assertTrue(row['missing'])
        self.assertNotIn('title',row)
        self.counters.assert_not_called()

    def test_channel_mismatch_or_duplicate_never_persists_provider_data(self):
        for value in ([{**metadata(),'snippet':{**metadata()['snippet'],'channelId':'wrong'}}],
                      [metadata(),metadata()]):
            self.repo.reset_mock();self.metadata.return_value=value
            self.service.process_common_claims([claim()])
            self.repo.complete_identity_refresh.assert_not_called()
            self.repo.fail_batch.assert_called_once()

    def test_lost_ack_stops_batch_without_releasing_an_uncertain_owner(self):
        self.repo.complete_identity_refresh.side_effect=ConnectionError('lost')
        result=self.service.process_common_claims([claim()])
        self.assertTrue(result['settlement_unknown'])
        self.repo.fail_batch.assert_not_called()

    def test_unknown_type_requires_explicit_recovery_mode(self):
        result=self.service.process_common_claims([replace(claim(),collection_mode='hydrate')])
        self.assertEqual(result['errors'],1)
        self.metadata.assert_not_called()
        self.repo.complete_identity_refresh.assert_not_called()

    def test_repository_replays_only_the_identical_receipted_body(self):
        repo=VideoCollectionRepository(object())
        repo._rpc=Mock(side_effect=[ConnectionError('lost'),1])
        rows=[{'video_id':VIDEO,'video_type':'unknown'}]
        self.assertEqual(repo.complete_identity_refresh(rows),1)
        first,second=repo._rpc.call_args_list
        self.assertIs(first.args[1]['p_rows'],second.args[1]['p_rows'])
        self.assertEqual(second.kwargs,{'abort_on_denied':False})


class IdentitySqlTests(unittest.TestCase):
    def test_writes_have_bounded_parameterized_statements_and_no_source_clock_fabrication(self):
        rows=Jsonb([])
        for sql in (recovery._VALID,recovery._ROWS):
            _reviewed_statement(sql,read=True,max_rows=50)
            PostgresQuery(Transformer()).convert(sql,(rows,))
        for sql in (recovery._UPSERT,recovery._RECEIPT):
            _reviewed_statement(sql,read=False,max_rows=0)
            PostgresQuery(Transformer()).convert(sql,(rows,))
        self.assertNotIn('video_snapshots',recovery._UPSERT)
        self.assertNotIn('clock_timestamp()',recovery._UPSERT)
        self.assertIn('NULL,lease_token,\'identity_refresh\'',recovery._RECEIPT)
        _reviewed_statement(recovery._RESTORE, read=True, max_rows=1, returning=True)
        PostgresQuery(Transformer()).convert(recovery._RESTORE, {'video_id':VIDEO,'channel_id':CHANNEL})

    def test_restore_selects_publication_and_origin_from_the_same_evidence(self):
        self.assertIn('published_acquired_at=d.published_acquired_at', recovery._RESTORE)
        self.assertIn('m.last_present_at', recovery._RESTORE)
        self.assertIn('ORDER BY source_published_at ASC NULLS LAST,last_present_at ASC LIMIT 1', recovery._RESTORE)
        self.assertIn('CASE WHEN source_published_at IS NOT NULL THEN last_present_at END', recovery._RESTORE)

    def test_empty_or_oversized_completion_opens_no_transaction(self):
        database=Mock()
        for rows in ([],[{}]*51,None):
            with self.assertRaises(DatabaseServerError):recovery.complete_video_identity_refresh(database,rows)
        database.transaction.assert_not_called()


class IdentitySchedulingTests(unittest.TestCase):
    def setUp(self):
        automatic=patch("db.inventory.automatic_type_reconciliation.reconcile_automatic_video_types",return_value=[])
        self.automatic=automatic.start();self.addCleanup(automatic.stop)

    def fixture(self, interval, *, missing=False, replayed=False):
        row=dict(video_id=VIDEO,channel_id=CHANNEL,video_type='unknown',lease_token='lease',
                 recorded_at=STAMP,missing=missing,replayed=replayed,mismatch=False)
        snapshot=replace(CollectionSchedules().cadence_snapshot(),rechecks=RecheckSchedules(identity_seconds=interval))
        tx=Mock(cadence_snapshot=snapshot)
        tx.fetch_all.side_effect=lambda sql,params,**kwargs: [row] if sql==recovery._ROWS else []
        tx.returning_all.return_value=[]
        state=dict(video_type=None,channel_id=CHANNEL,lease_token='lease',lease_expires_at=STAMP,
                   lease_channel_epoch=7)
        inventory=dict(inventory_type=None,inventory_channel=CHANNEL,skeleton_since=STAMP,inventory_before='{}')
        def read(sql, params):
            if sql==recovery._VALID:return {'valid':True}
            if 'pg_try_advisory_xact_lock' in sql:return {'ok':True}
            if sql==recovery._LOCK:return state
            if sql==recovery._INVENTORY_LOCK:return inventory
            if sql==recovery.ELIGIBLE_SQL:return {'eligible':True}
            if sql.startswith('SELECT greatest('):return None
            if sql=='SELECT %s::timestamptz>clock_timestamp() AS valid':return {'valid':True}
            raise AssertionError(sql)
        tx.fetch_one.side_effect=read
        tx.execute.return_value=1
        tx.returning_one.return_value=None
        database=Mock()
        database.transaction.return_value=nullcontext(tx)
        return database,tx,row

    def run_completion(self, database, row):
        with patch.object(recovery,'_clock_check'),patch.object(recovery,'source_clock_invalidation',return_value=nullcontext()),patch.object(recovery,'assert_channel_scopes'):
            return recovery.complete_video_identity_refresh(database,[row])

    def test_positive_identity_reconciles_after_receipt_and_settlement_before_scope(self):
        db,tx,row=self.fixture(1);order=[]
        def reconcile(borrowed,ids):
            self.assertIs(borrowed,tx);self.assertEqual(ids,[VIDEO])
            statements=[call.args[0] for call in tx.execute.call_args_list]
            self.assertIn(recovery._RECEIPT,statements)
            self.assertIn(recovery._SETTLE,statements)
            self.assertTrue(tx.returning_all.called)
            order.append('automatic')
            return []
        self.automatic.side_effect=reconcile
        with patch.object(recovery,'_clock_check'),patch.object(recovery,'source_clock_invalidation',return_value=nullcontext()),patch.object(recovery,'assert_channel_scopes',side_effect=lambda *args:order.append('scope')):
            self.assertEqual(recovery.complete_video_identity_refresh(db,[row]),1)
        self.assertEqual(order,['automatic','scope'])

    def test_automatic_identity_handoff_only_targets_positive_non_counterless_receipts(self):
        for missing,counterless in ((False,False),(False,True),(True,False)):
            db,tx,row=self.fixture(2419200,missing=missing)
            if counterless:
                row.update(counterless_profile=True,availability_status='unlisted',published_at=STAMP,video_type='long')
                original=tx.fetch_one.side_effect
                def read(sql,params):
                    value=original(sql,params)
                    if sql==recovery._LOCK:value['video_type']='long'
                    if sql==recovery._INVENTORY_LOCK:value['inventory_type']='long'
                    return value
                tx.fetch_one.side_effect=read
            self.run_completion(db,row)
            calls=[call for call in tx.execute.call_args_list if call.args[0]==recovery._AUTOMATIC_HANDOFF]
            self.assertEqual(len(calls),int(not missing and not counterless))
            if calls:self.assertEqual(calls[0].args[1],([VIDEO],))
        self.assertIn('public.video_type_automatic_candidates',recovery._AUTOMATIC_HANDOFF)
        self.assertIn('q.availability_first_missing_at IS NULL',recovery._AUTOMATIC_HANDOFF)
        self.assertIn('q.lease_token IS NULL',recovery._AUTOMATIC_HANDOFF)

    def test_present_identity_interval_and_missing_episode_schedule_are_separate(self):
        for interval in (1,21600,2419200):
            for missing in (False,True):
                with self.subTest(interval=interval,missing=missing):
                    db,tx,row=self.fixture(interval,missing=missing)
                    self.assertEqual(self.run_completion(db,row),1)
                    settlement=[c for c in tx.execute.call_args_list if c.args[0]==recovery._SETTLE]
                    self.assertEqual(len(settlement),1)
                    params=settlement[0].args[1]
                    expected = datetime.fromisoformat(STAMP)+timedelta(seconds=86400 if missing else interval)
                    self.assertEqual(params['next_due_at'],expected.isoformat())
                    self.assertEqual(params['first_missing_at'],STAMP if missing else None)
                    self.assertEqual(params['missing'],missing)
                    self.assertFalse(params['stopped'])
                    PostgresQuery(Transformer()).convert(recovery._SETTLE,params)
                    self.assertNotIn('DELETE',recovery._SETTLE)
                    self.assertNotIn('video_inventory',recovery._SETTLE)

    def test_positive_identity_also_closes_open_history_without_a_second_queue_owner(self):
        db,tx,row=self.fixture(1)
        history=dict(video_id=VIDEO,channel_id=CHANNEL,from_status='unknown_unavailable',
                     to_status='unlisted',observed_at=STAMP)
        tx.returning_all.return_value=[history]
        self.run_completion(db,row)
        tx.returning_all.assert_called_once_with(recovery._RESTORE_INCIDENTS,unittest.mock.ANY,max_rows=50)
        events=next(c.args[1][0].obj for c in tx.execute.call_args_list if c.args[0]==recovery._RESTORED_EVENTS)
        self.assertEqual(events,[history])
        self.assertNotIn('video_collection_state',recovery._RESTORE_INCIDENTS)

    def test_identity_miss_uses_first_episode_and_stops_after_day29(self):
        for days, stopped in ((8,False),(29,True),(40,True)):
            db,tx,row=self.fixture(1,missing=True)
            original=tx.fetch_one.side_effect
            first=(datetime.fromisoformat(STAMP)-timedelta(days=days)).isoformat()
            def read(sql,params):
                value=original(sql,params)
                if sql==recovery._LOCK:
                    value.update(availability_first_missing_at=first,availability_schedule_type='short')
                return value
            tx.fetch_one.side_effect=read
            self.run_completion(db,row)
            settled=next(c.args[1] for c in tx.execute.call_args_list if c.args[0]==recovery._SETTLE)
            self.assertEqual(settled['first_missing_at'],first)
            self.assertEqual(settled['stopped'],stopped)
            self.assertEqual(settled['next_due_at'],None if stopped else
                             (datetime.fromisoformat(STAMP)+timedelta(days=7)).isoformat())

    def test_exact_receipt_replay_does_not_reschedule_after_config_change(self):
        db,tx,row=self.fixture(1,replayed=True)
        self.assertEqual(self.run_completion(db,row),1)
        tx.execute.assert_not_called()
        tx.returning_one.assert_not_called()
        self.automatic.assert_not_called()

    def test_counterless_general_ends_episode_without_counter_clock_or_snapshot(self):
        db,tx,row=self.fixture(2419200)
        row.update(video_type='short',counterless_profile=True,availability_status='unlisted',published_at=STAMP)
        original=tx.fetch_one.side_effect
        def read(sql,params):
            value=original(sql,params)
            if sql==recovery._LOCK:
                value.update(video_type='short',availability_first_missing_at=STAMP,availability_schedule_type='short')
            if sql==recovery._INVENTORY_LOCK:value['inventory_type']='short'
            return value
        tx.fetch_one.side_effect=read
        self.assertEqual(self.run_completion(db,row),1)
        settled=next(c.args[1] for c in tx.execute.call_args_list if c.args[0]==recovery._SETTLE)
        self.assertIsNone(settled['first_missing_at'])
        self.assertIsNone(settled['schedule_type'])
        self.assertTrue(settled['counterless_profile'])
        self.assertFalse(settled['stopped'])
        self.assertEqual(settled['next_due_at'],(datetime.fromisoformat(STAMP)+timedelta(days=28)).isoformat())
        self.assertIn('stats_due_at=NULL',recovery._SETTLE)
        self.assertNotIn('stats_source_checked_at=',recovery._SETTLE)
        self.assertFalse(any('video_snapshots' in c.args[0] for c in tx.execute.call_args_list))
        self.assertIn("NOT %(missing)s AND video_type IN ('short','stream')",recovery._SETTLE)
        self.assertIn("typed_collection_status='unavailable' THEN 'pending'",recovery._SETTLE)
        self.assertNotIn("THEN 'hydrated'",recovery._SETTLE)

    def test_missing_hydration_proof_uses_availability_observation_not_metadata_clock(self):
        schema=(Path(__file__).resolve().parents[1]/'schema'/'schema.sql').read_text(encoding='utf-8')
        proof=schema.split('CREATE VIEW readable.video_hydration_proof ',1)[1].split('ALTER VIEW readable.video_hydration_proof ',1)[0]
        unavailable=proof.split("coalesce(state.typed_collection_status = 'unavailable'",1)[1].split('as unavailable_observed',1)[0]
        self.assertNotIn('metadata_source_checked_at',unavailable)
        self.assertIn('unavailable.last_seen_at = state.typed_collection_checked_at',unavailable)
        self.assertIn("interval '696 hours'",unavailable)
        step=(Path(__file__).resolve().parents[1]/'schema'/'steps'/'0028-unified-video-availability.sql').read_text(encoding='utf-8')
        replacement=step.split('CREATE OR REPLACE VIEW readable.video_hydration_proof ',1)[1].split(';',1)[0]
        self.assertEqual(replacement.strip(),proof.split(';',1)[0].strip())

    def test_initial_batch_borrows_existing_transaction(self):
        db,tx,row=self.fixture(1,replayed=True)
        tx.borrow.return_value=nullcontext(tx)
        with patch.object(recovery,'_clock_check'):
            self.assertEqual(recovery.complete_video_identity_refresh(db,[row],transaction=tx),1)
        db.transaction.assert_not_called()
        tx.borrow.assert_called_once()

    def test_missing_snapshot_rejects_before_sql(self):
        db,tx,row=self.fixture(1)
        tx.cadence_snapshot=None
        with self.assertRaises(DatabaseError):self.run_completion(db,row)
        tx.fetch_one.assert_not_called()
        tx.execute.assert_not_called()


if __name__=='__main__':unittest.main()
