"""Offline chain protocol tests; native constraints/concurrency need schema probe."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
import psycopg
from db.jobs import operator_job_inventory_chains as chains
from db.jobs import operator_job_inventory_chain_settlement as settlement
from db.jobs.operator_job_inventory_evidence import _NativeObject
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseError, DatabaseServerError, PostgresConfig, PostgresDatabase, _reviewed_statement
from test_operator_job_lifecycle import Database
from test_direct_pg_transactions import Connection, Pool, Result, DSN

JOB = 'f9600000-0000-4000-8000-000000000001'
LEASE = 'f9600000-0000-4000-8000-000000000002'
SCAN = 'f9600000-0000-4000-8000-000000000003'
TRANSITION = 'f9600000-0000-4000-8000-000000000004'
PLAYLIST_LEASE = 'f9600000-0000-4000-8000-000000000005'
CHANNEL = 'UC0000000000000000000001'


def raw(**changes):
    return json.dumps(dict(id=JOB, channel_id=CHANNEL, phase='inventory', initial_scope_version=1,
        inventory_checkpoint={}, inventory_scan_id=None, inventory_video_type='long',
        summary_json={'inventory_chain_version': 1, 'full_inventory_sources': {}}, **changes))


def args(kind='long', transition=None, scan=None, playlist=None):
    return dict(job_id=JOB, lease_token=LEASE, video_type=kind, transition=transition,
        scan_id=scan, playlist_lease_token=playlist)


def checkpoint(phase='prepared', lease=LEASE):
    value = dict(phase=phase, job_lease_token=lease, transition_id=TRANSITION, reserved_units=1)
    if phase == 'bound':
        value['playlist_lease_token'] = PLAYLIST_LEASE
    return value


def child(kind='long', cp=None, terminal=None):
    return json.dumps(dict(job_id=JOB, channel_id=CHANNEL, video_type=kind, scan_id=SCAN,
        checkpoint=cp or {}, terminal_outcome=terminal, last_ack={}))


def held(job=None, *, invalid=False):
    return [dict(channel_id=CHANNEL), dict(channel_id=CHANNEL), dict(value=job or raw()),
        dict(epoch=17, accepted_epoch=17, invalid=invalid)]


def rows(*, idle=True, done=False, count=3):
    return dict(count=count, same_channel=True, idle=idle, done=done)


def sqls(tx):
    return [sql for _, sql, _ in tx.calls]


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.scope = patch.object(chains, 'assert_full_initial_scope').start()
        self.fence = patch.object(chains, 'assert_channel_scope').start()
        self.evidence = patch.object(chains, 'initial_inventory_checkpoint_evidence').start()
        self.copy_proof = patch.object(chains, 'validate_inventory_chain_copies').start()
        self.replay_proof = patch.object(chains, 'validate_inventory_chain_replay',return_value={'outcome':'unknown'}).start()
        self.addCleanup(patch.stopall)

    def test_initialize_adopts_existing_scalar_checkpoint_without_refund_or_new_transition(self):
        job = json.loads(raw())
        job.update(inventory_checkpoint=checkpoint('bound'), inventory_scan_id=SCAN)
        job['summary_json'].pop('inventory_chain_version')
        tx = Database(args(None), *held(json.dumps(job)), rows(count=0), 3, 1, dict(valid=True))
        result = chains.initialize_inventory_chains(tx, JOB, LEASE)
        self.assertEqual(result['chain_count'], 3)
        self.assertEqual(sqls(tx)[-3:], [chains._INIT, chains._ACTIVATE, chains._FINAL])
        self.assertNotIn(chains._REFUND, sqls(tx))
        self.assertIn('THEN j.inventory_checkpoint', chains._INIT)
        self.assertNotIn('ON CONFLICT', chains._INIT)
        self.assertEqual(tx.events[-1], 'commit')

    def test_initialization_idempotent_requires_three_matching_children(self):
        tx = Database(args(None), *held(), rows(), dict(valid=True))
        chains.initialize_inventory_chains(tx, JOB, LEASE)
        self.assertNotIn(chains._INIT, sqls(tx))
        tx = Database(args(None), *held(), rows(count=2))
        with self.assertRaisesRegex(DatabaseError, 'chain_state_invalid'):
            chains.initialize_inventory_chains(tx, JOB, LEASE)

    def test_scalar_prepared_zero_refuses_adoption_before_copy_or_write(self):
        job=json.loads(raw());job['summary_json'].pop('inventory_chain_version')
        job['inventory_checkpoint']=dict(checkpoint(),reserved_units=0)
        tx=Database(args(None),*held(json.dumps(job)),rows(count=0))
        with self.assertRaisesRegex(DatabaseError,'chain_adoption_deferred'):
            chains.initialize_inventory_chains(tx,JOB,LEASE)
        self.copy_proof.assert_not_called()
        self.assertNotIn(chains._INIT,sqls(tx))
        self.assertEqual(tx.events[-1],'rollback')

    def test_source_proof_failure_prevents_scalar_copy_and_replay_ack(self):
        error=DatabaseServerError('initial_inventory_receipt_expired',code='P0001')
        self.copy_proof.side_effect=error
        job=json.loads(raw());job['summary_json'].pop('inventory_chain_version')
        tx=Database(args(None),*held(json.dumps(job)),rows(count=0))
        with self.assertRaises(DatabaseError):chains.initialize_inventory_chains(tx,JOB,LEASE)
        self.assertNotIn(chains._INIT,sqls(tx))
        self.replay_proof.side_effect=error
        for ack in (False,True):
            tx=Database(args('long',TRANSITION,SCAN,PLAYLIST_LEASE),dict(value='{}') if ack else dict(value=True))
            with self.assertRaises(DatabaseError):
                if ack:chains.acknowledge_inventory_chain(tx,JOB,LEASE,'long',TRANSITION)
                else:chains.bind_inventory_chain(tx,JOB,LEASE,'long',TRANSITION,SCAN,PLAYLIST_LEASE)
            self.assertEqual(tx.events[-1],'rollback')
            self.assertFalse(any(kind=='execute' for kind,_,_ in tx.calls))

    def test_full_scope_and_marker_fail_closed_before_child_lock(self):
        for update, error in (({'initial_scope_version': 0}, 'full_initial_scope_invalid'),
                ({'summary_json': {}}, 'chain_not_initialized'),
                ({'summary_json': {'inventory_chain_version': True}}, 'chain_state_invalid'),
                ({'inventory_checkpoint': checkpoint()}, 'chain_state_invalid')):
            job = json.loads(raw())
            job.update(update)
            tx = Database(args(), *held(json.dumps(job)))
            with self.assertRaisesRegex(DatabaseError, error):
                chains.prepare_inventory_chain(tx, JOB, LEASE, 'long')
            self.assertNotIn(chains._CHAIN, sqls(tx))

    def test_three_formats_each_prepare_one_page_and_reserve_once(self):
        for kind in ('long', 'short', 'stream'):
            chain = child(kind)
            tx = Database(args(kind), *held(), dict(value=chain), dict(value=raw()),
                dict(reason=None, retry_not_before=None), 1, 1, dict(value=TRANSITION),
                dict(value=child(kind, checkpoint())), 1, dict(valid=True))
            result = chains.prepare_inventory_chain(tx, JOB, LEASE, kind)
            self.assertEqual((result['video_type'], result['page_limit'], result['dispatch_allowed']), (kind, 1, False))
            self.assertEqual(sqls(tx).count(chains._RESERVE), 1)
            self.assertEqual(sqls(tx).count(chains._PREPARE), 1)
            self.assertEqual(tx.calls[5][2]['video_type'], kind)
            self.assertEqual(sqls(tx)[-1], chains._FINAL)

    def test_same_prepared_replay_does_not_rereserve_or_rewrite(self):
        tx = Database(args(), *held(), dict(value=child(cp=checkpoint())), dict(value=raw()),
            dict(reason=None, retry_not_before=None), dict(valid=True))
        result = chains.prepare_inventory_chain(tx, JOB, LEASE, 'long')
        self.assertEqual(result['transition_id'], TRANSITION)
        self.assertNotIn(chains._PREPARE, sqls(tx))
        self.assertNotIn(chains._RESERVE, sqls(tx))

    def test_old_prepared_lease_adopts_same_transition_without_new_cost(self):
        tx = Database(args(), *held(), dict(value=child(cp=checkpoint(lease=SCAN))), dict(value=raw()),
            dict(reason=None, retry_not_before=None), dict(value=child(cp=checkpoint())), dict(valid=True))
        chains.prepare_inventory_chain(tx, JOB, LEASE, 'long')
        self.assertIn(chains._PREPARE, sqls(tx))
        self.assertNotIn(chains._RESERVE, sqls(tx))
        self.assertEqual(tx.calls[-2][2]['transition'], TRANSITION)

    def test_bound_never_dispatches_again_even_after_parent_reclaim(self):
        for action in ('reconcile_inventory', 'reconcile_failure', 'await_receipt', 'reconcile_unknown'):
            self.evidence.return_value = {'kind': 'none'}
            tx = Database(args(), *held(), dict(value=child(cp=checkpoint('bound', SCAN))),
                dict(value=raw()), dict(value=action), dict(valid=True))
            result = chains.prepare_inventory_chain(tx, JOB, LEASE, 'long')
            self.assertEqual(result['action'], action)
            self.assertFalse(result['dispatch_allowed'])
            self.assertNotIn(chains._PREPARE, sqls(tx))
            self.assertNotIn(chains._RESERVE, sqls(tx))

    def test_confirmation_and_retry_pause_new_work_but_return_due_time(self):
        tx = Database(args(), *held(), dict(value=child()), dict(value=raw()),
            dict(reason='initial_inventory_retry_pending', retry_not_before='future'), dict(valid=True))
        result = chains.prepare_inventory_chain(tx, JOB, LEASE, 'long')
        self.assertEqual(result['action'], 'paused')
        self.assertEqual(result['retry_not_before'], 'future')
        self.assertNotIn(chains._PREPARE, sqls(tx))

    def test_exact_bound_ack_replay_precedes_leases_and_never_dispatches(self):
        tx = Database(args('short', TRANSITION, SCAN, PLAYLIST_LEASE), dict(value=True))
        result = chains.bind_inventory_chain(tx, JOB, LEASE, 'short', TRANSITION, SCAN, PLAYLIST_LEASE)
        self.assertTrue(result['replayed'])
        self.assertFalse(result['dispatch_allowed'])
        self.assertEqual(len(tx.calls), 2)
        self.scope.assert_not_called()
        self.replay_proof.assert_called_once_with(tx,args('short',TRANSITION,SCAN,PLAYLIST_LEASE),ack=False)
        self.fence.assert_not_called()

    def binding_answers(self, final=True):
        return [args('long', TRANSITION, SCAN, PLAYLIST_LEASE), dict(value=False), *held(),
            dict(value=child(cp=checkpoint())), dict(value=raw()), dict(reason=None),
            dict(value='{}'), dict(invalid=False), dict(value='{}'),
            dict(identity_invalid=False, kind_invalid=False), dict(invalid=False, has_source=False),
            1, 1, dict(valid=final)]

    def test_fresh_bind_uses_channel_job_chain_playlist_scan_order_and_real_parent_final_fence(self):
        tx = Database(*self.binding_answers())
        result = chains.bind_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION, SCAN, PLAYLIST_LEASE)
        locks = [sql for sql in sqls(tx) if 'FOR UPDATE' in sql]
        self.assertEqual(locks, [chains.planning._CHANNEL, chains.planning._JOB, chains._CHAIN,
            chains.binding._PLAYLIST, chains.binding._SCAN])
        self.assertTrue(result['dispatch_allowed'])
        self.assertEqual(sqls(tx)[-3:], [chains.binding._REGISTER_SOURCE, chains._BIND, chains._FINAL])
        self.assertIn('FROM public.operator_jobs', chains._FINAL)
        self.assertIn('clock_timestamp()', chains._FINAL)
        self.assertNotIn('jsonb_populate_record', chains._FINAL)

    def test_late_real_parent_expiry_rolls_back_binding_and_source_registration(self):
        tx = Database(*self.binding_answers(final=False))
        with self.assertRaisesRegex(DatabaseError, 'operator_job_lease_lost'):
            chains.bind_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION, SCAN, PLAYLIST_LEASE)
        self.assertEqual(tx.events[-1], 'rollback')
        self.fence.assert_not_called()

    def test_zero_row_source_or_bind_cannot_return_dispatch_permission(self):
        for index, final_sql in ((-3, chains.binding._REGISTER_SOURCE), (-2, chains._BIND)):
            answers = self.binding_answers()
            answers[index] = 0
            tx = Database(*answers)
            with self.assertRaisesRegex(DatabaseError, 'chain_state_invalid'):
                chains.bind_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION, SCAN, PLAYLIST_LEASE)
            self.assertEqual(tx.events[-1], 'rollback')
            self.assertEqual(sqls(tx)[-1], final_sql)

    def test_unknown_commit_never_returns_dispatch_permission_or_retries(self):
        events = []
        connection = Connection(events)
        pool = Pool(connection, events)
        database = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
            pool_factory=lambda config: pool).open()
        self.addCleanup(database.close)
        connection.results.extend(Result((value,)) if isinstance(value, dict)
            else Result(has_columns=False, rowcount=value) for value in self.binding_answers())
        connection.commit_error = psycopg.OperationalError('private')
        with self.assertRaises(DatabaseError) as caught:
            chains.bind_inventory_chain(database, JOB, LEASE, 'long', TRANSITION, SCAN, PLAYLIST_LEASE)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(pool.borrowed, 1)
        self.assertEqual(sum(chains._BIND in sql for sql, _ in connection.queries), 1)

    def test_ack_replay_is_per_format_and_requires_source_proof_without_mutation(self):
        tx = Database(args('stream', TRANSITION), dict(value='{"outcome":"page"}'))
        result = chains.acknowledge_inventory_chain(tx, JOB, LEASE, 'stream', TRANSITION)
        self.assertTrue(result['replayed'])
        self.assertEqual(len(tx.calls), 2)
        self.evidence.assert_not_called()
        self.assertIn('video_type=%(video_type)s', chains._ACK_REPLAY)
        self.assertIn("last_ack->>'job_lease_token'", chains._ACK_REPLAY)

    def test_unknown_receipt_retains_checkpoint_and_credit(self):
        self.evidence.return_value = {'kind': 'none', 'bound_live': False}
        tx = Database(args('long', TRANSITION), None, *held(),
            dict(value=child(cp=checkpoint('bound'))), dict(value=raw()))
        with self.assertRaisesRegex(DatabaseError, 'initial_inventory_outcome_unresolved'):
            chains.acknowledge_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION)
        self.assertFalse(any(kind == 'execute' for kind, _, _ in tx.calls))
        self.assertEqual(tx.events[-1], 'rollback')

    def test_ack_calls_receipt_settlement_under_same_transaction_then_final_fence(self):
        self.evidence.return_value = {'kind': 'page'}
        tx = Database(args('short', TRANSITION), None, *held(),
            dict(value=child('short', checkpoint('bound'))), dict(value=raw()), dict(valid=True))
        with patch.object(settlement, 'settle_inventory_chain_receipt', return_value={'outcome': 'page'}) as apply:
            result = chains.acknowledge_inventory_chain(tx, JOB, LEASE, 'short', TRANSITION)
        self.assertIs(apply.call_args.args[0], tx)
        self.assertFalse(result['replayed'])
        self.assertEqual(sqls(tx)[-1], chains._FINAL)

    def test_cancel_can_only_release_definitely_prepared_credit(self):
        tx = Database(args('long', TRANSITION), *held(), dict(value=child(cp=checkpoint())),
            1, 1, dict(valid=True))
        chains.cancel_prepared_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION)
        self.assertEqual(sqls(tx)[-3:], [chains._CANCEL, chains._REFUND, chains._FINAL])
        tx = Database(args('long', TRANSITION), *held(), dict(value=child(cp=checkpoint('bound'))))
        with self.assertRaisesRegex(DatabaseError, 'checkpoint_mismatch'):
            chains.cancel_prepared_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION)
        self.assertNotIn(chains._REFUND, sqls(tx))

    def test_finish_rejects_outstanding_child_and_only_reconciles_three_terminal(self):
        tx = Database(args(None), *held(), rows(idle=False))
        with self.assertRaisesRegex(DatabaseError, 'chain_outstanding'):
            chains.finish_inventory_chains(tx, JOB, LEASE)
        self.assertIn("bool_and(coalesce(terminal_outcome", chains._ROWS)
        tx = Database(args(None), *held(), rows(done=True), dict(valid=True))
        with patch.object(chains, 'reconcile_full_initial_targets', return_value={'phase': 'hydrate_videos'}) as finish:
            result = chains.finish_inventory_chains(tx, JOB, LEASE)
        finish.assert_called_once_with(tx, JOB, 250, lease_guard=(LEASE, 17))
        self.assertEqual(result['phase'], 'hydrate_videos')

    def test_yield_releases_parent_only_after_empty_children_and_uses_pause_reason(self):
        tx = Database(args(None), *held(), rows(), dict(valid=True),
            dict(reason='initial_inventory_confirmation_required'), dict(value='{"status":"blocked"}'))
        result = chains.finish_inventory_chains(tx, JOB, LEASE)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(tx.calls[-1][2]['blocked'])
        self.assertEqual(sqls(tx)[-3:], [chains._FINAL, chains._PAUSE, chains._YIELD])

    def test_scalar_drivers_reject_marker_but_chain_lease_uses_native_wall_clock(self):
        from db.jobs import operator_job_inventory_page_settlement, operator_job_inventory_failure
        for sql in (chains.planning._LEASE, chains.binding._JOB_CHECK,
                operator_job_inventory_page_settlement._LEASE, operator_job_inventory_failure._LEASE):
            self.assertIn("j.summary_json ? 'inventory_chain_version'", sql)
        self.assertNotIn("j.summary_json ? 'inventory_chain_version'", chains._LEASE)
        self.assertIn('clock_timestamp()', chains._LEASE)

    def test_facade_opt_in_mapping_has_no_production_consumer(self):
        database = object()
        repository = OperatorJobRepository(database)
        with patch.object(chains, 'prepare_inventory_chain', return_value={'action': 'inventory'}) as prepare:
            self.assertEqual(repository.prepare_inventory_chain(JOB, LEASE, 'short'), {'action': 'inventory'})
        prepare.assert_called_once_with(database, JOB, LEASE, 'short')


    def test_explicit_unknown_ack_requires_expired_original_lease_and_estimates_one_attempt(self):
        for live in (True, None):
            self.evidence.return_value = dict(kind='none', bound_live=live)
            tx = Database(args('long', TRANSITION), None, *held(),
                dict(value=child(cp=checkpoint('bound'))), dict(value=raw()))
            with self.assertRaisesRegex(DatabaseError, 'outcome_unresolved'):
                chains.acknowledge_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION, resolve_unknown=True)
        self.evidence.return_value = dict(kind='none', bound_live=False)
        tx = Database(args('long', TRANSITION), None, *held(),
            dict(value=child(cp=checkpoint('bound'))), dict(value=raw()), dict(valid=True))
        with patch.object(settlement, 'settle_inventory_chain_receipt', return_value={'outcome': 'unknown'}) as apply:
            result = chains.acknowledge_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION, resolve_unknown=True)
        proof = apply.call_args.args[2]
        self.assertEqual((proof['kind'], proof['provider_attempts'], proof['pages_processed']), ('unknown', 1, 0))
        self.assertFalse(proof['format_finished'])
        self.assertFalse(result['dispatch_allowed'])
        self.assertEqual(sqls(tx)[-1], chains._FINAL)

    def test_chain_source_claim_owns_exact_prepared_child_and_passes_native_overlay(self):
        from db.inventory import inventory_claims
        tx = Database(args('short', TRANSITION), *held(),
            dict(value=child('short', checkpoint())), dict(value=raw()), dict(reason=None),
            dict(job_id=JOB, job_lease_token=LEASE, worker='worker', run_id=TRANSITION, lease_seconds=900),
            dict(invalid=False, epoch=17, video_type='short'), dict(valid=True))
        with patch.object(inventory_claims, 'claim_prepared_initial_source', return_value=[{'video_type': 'short'}]) as claim:
            result = chains.claim_inventory_chain_source(tx, JOB, LEASE, 'short', TRANSITION, 'worker', TRANSITION)
        self.assertEqual(result['status'], 'claimed')
        self.assertFalse(result['dispatch_allowed'])
        self.assertIs(claim.call_args.args[0], tx)
        self.assertEqual(claim.call_args.kwargs, {'chain_transition': TRANSITION})
        self.assertEqual(sqls(tx)[-1], chains._FINAL)

    def test_chain_source_claim_rejects_changed_transition_before_playlist_or_quota_work(self):
        tx = Database(args('short', SCAN), *held(), dict(value=child('short', checkpoint())))
        with self.assertRaisesRegex(DatabaseError, 'checkpoint_mismatch'):
            chains.claim_inventory_chain_source(tx, JOB, LEASE, 'short', SCAN, 'worker', TRANSITION)
        self.assertNotIn(chains._OVERLAY, sqls(tx))

    def test_opt_in_candidate_preserves_fair_order_and_only_locks_one_exact_prepared_chain(self):
        from db.inventory import inventory_claims
        self.assertNotIn('operator_job_inventory_chains', inventory_claims._CANDIDATES)
        order = 'ORDER BY d.dispatch_ordinal,d.priority DESC,d.channel_id,d.video_type'
        self.assertIn(order, inventory_claims._CHAIN_CANDIDATES)
        self.assertIn('FOR UPDATE OF d SKIP LOCKED LIMIT %(claim_limit)s', inventory_claims._CHAIN_CANDIDATES)
        self.assertIn("chain.checkpoint=jsonb_build_object('phase','prepared'", inventory_claims._CHAIN_CANDIDATES)
        self.assertIn('chain.channel_id=j.channel_id', inventory_claims._CHAIN_CANDIDATES)
        values = dict(claim_limit=1, initial_job_id=JOB, initial_job_lease_token=LEASE,
            channel_ids=[CHANNEL], video_types=['short'], chain_transition=TRANSITION)
        tx = Database(None)
        with inventory_claims._chain_candidates(tx, values) as selected:
            self.assertEqual(selected, [])
        self.assertEqual(len(tx.calls), 1)
        for key, value in (('claim_limit', 2), ('initial_job_id', None), ('video_types', ['long', 'short'])):
            with self.subTest(key=key):
                with self.assertRaisesRegex(DatabaseError, 'claim_limit_invalid'):
                    with inventory_claims._chain_candidates(Database(), dict(values, **{key: value})):
                        pass


    def test_bind_rechecks_exact_replay_after_waiting_for_parent_lock(self):
        tx = Database(args('long', TRANSITION, SCAN, PLAYLIST_LEASE), dict(value=False),
            *held(), dict(value=child(cp=checkpoint('bound'))))
        result = chains.bind_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION, SCAN, PLAYLIST_LEASE)
        self.assertTrue(result['replayed'])
        self.assertFalse(result['dispatch_allowed'])
        self.assertEqual(sqls(tx)[-1], chains._CHAIN)
        self.assertFalse(any(kind != 'one' for kind, _, _ in tx.calls))

    def test_ack_rechecks_committed_receipt_after_waiting_for_parent_lock(self):
        state = json.loads(child())
        state['last_ack'] = dict(transition_id=TRANSITION, job_lease_token=LEASE, outcome='unknown')
        tx = Database(args('long', TRANSITION), None, *held(), dict(value=json.dumps(state)))
        result = chains.acknowledge_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION)
        self.assertTrue(result['replayed'])
        self.assertEqual(result['ack']['outcome'], 'unknown')
        self.assertFalse(result['dispatch_allowed'])
        self.evidence.assert_not_called()
        self.assertEqual(sqls(tx)[-1], chains._CHAIN)

    def test_yield_expiry_at_final_update_rolls_back_instead_of_releasing_another_owner(self):
        tx = Database(args(None), *held(), rows(), dict(valid=True), dict(reason=None), None)
        with self.assertRaisesRegex(DatabaseError, 'operator_job_lease_lost'):
            chains.finish_inventory_chains(tx, JOB, LEASE)
        self.assertEqual(tx.events[-1], 'rollback')
        self.assertEqual(sqls(tx)[-1], chains._YIELD)
        self.assertIn('j.lease_expires_at>clock_timestamp()', chains._YIELD)
        self.assertIn('j.lease_token=%(lease_token)s::uuid', chains._YIELD)
        self.assertIn('j.lease_channel_epoch=%(epoch)s::bigint', chains._YIELD)


class ReceiptTests(unittest.TestCase):
    def receipt(self, kind='page', absent=False):
        return dict(kind=kind, outcome='playlist_not_found' if absent else 'error',
            format_finished=absent, format_absent=absent)

    def values(self, kind='page', **changes):
        result = dict(attempts=1, pages=int(kind == 'page'), items=2 if kind == 'page' else 0,
            inserted=1 if kind == 'page' else 0, updated=1 if kind == 'page' else 0,
            retry='2026-10-11 00:00:00+00' if kind == 'quota' else None)
        result.update(changes)
        return result

    def call(self, tx, evidence):
        return settlement.settle_inventory_chain_receipt(tx,
            dict(args('short', TRANSITION), job=raw(), channel_id=CHANNEL), evidence)

    def test_page_target_work_bounded_and_parent_lease_siblings_not_cleared(self):
        for count in (0, 1, 50):
            tx = Database(self.values(), dict(value='{"outcome":"page"}'),
                dict(ids=['v'+str(n).zfill(10) for n in range(count)], oversized=False), count, 0,
                dict(proof='{}'), dict(results='{}', has_long=False), 1, 1)
            self.call(tx, self.receipt())
            self.assertEqual(len(tx.calls), 9)
            self.assertEqual(sqls(tx)[-2:], [settlement._CHAIN, settlement._PARENT])
            self.assertNotIn('lease_token=NULL', settlement._PARENT)
            self.assertNotIn('inventory_video_type=', settlement._PARENT)
            self.assertIn('video_type=%(video_type)s', settlement._CHAIN)
            self.assertNotIn('summary_json', settlement._CHAIN)

    def test_absence_requires_exact_receipt_proof_not_404_status(self):
        tx = Database(self.values('failure'))
        with self.assertRaisesRegex(DatabaseError, 'absence_receipt_required'):
            self.call(tx, dict(kind='failure', outcome='playlist_not_found', format_absent=False))
        self.assertEqual(len(tx.calls), 1)

    def test_quota_does_not_add_targets_or_count_terminal_or_error(self):
        tx = Database(self.values('quota'), dict(value='{"outcome":"quota"}'), 1, 1)
        self.call(tx, self.receipt('quota'))
        self.assertNotIn(settlement.full._INSERT, sqls(tx))
        self.assertTrue(tx.calls[-1][2]['quota'])
        self.assertFalse(tx.calls[-1][2]['done'])
        self.assertIn('clock_timestamp()+interval', settlement._FIELDS)


    def test_quota_missing_or_nonfinite_native_retry_cannot_ack_or_clear_checkpoint(self):
        # PostgreSQL CASE + isfinite returns NULL for missing/null/+/-infinity;
        # malformed timestamp casts raise before any write.
        tx = Database(self.values('quota', retry=None))
        with self.assertRaisesRegex(DatabaseError, 'quota_retry_invalid'):
            self.call(tx, self.receipt('quota'))
        self.assertEqual(len(tx.calls), 1)
        self.assertEqual(tx.events[-1], 'poison')
        self.assertIn('AND isfinite(', settlement._FIELDS)
        cast_error = DatabaseServerError('database_operation_failed', code='22007')
        tx = Database(cast_error)
        with self.assertRaises(DatabaseServerError) as caught:
            self.call(tx, self.receipt('quota'))
        self.assertIs(caught.exception, cast_error)
        self.assertEqual(len(tx.calls), 1)

    def test_rate_retained_receipt_delays_parent_without_restart_or_terminal(self):
        proof=dict(self.receipt('failure'),error_code='provider_rate_limited',scan_retained=True)
        tx=Database(self.values('failure',retry='2026-10-11 00:00:00+00'),
            dict(value='{"outcome":"error","error_code":"provider_rate_limited"}'),1,1)
        self.call(tx,proof)
        values=tx.calls[-1][2]
        self.assertTrue(values['rate_limited'])
        self.assertFalse(values['success'])
        self.assertFalse(values['quota'])
        self.assertFalse(values['done'])
        self.assertIn('OR %(rate_limited)s::boolean THEN 0',settlement._PARENT)
        self.assertIn('AND NOT %(rate_limited)s::boolean',settlement._PARENT)
        self.assertNotIn('lease_token=NULL',settlement._PARENT)

    def test_rate_without_resumable_proof_or_finite_clock_never_clears_child(self):
        for retained,retry in ((None,'2026-10-11 00:00:00+00'),(False,'2026-10-11 00:00:00+00'),(True,None)):
            tx=Database(self.values('failure',retry=retry))
            with self.assertRaisesRegex(DatabaseError,'outcome_unresolved'):
                self.call(tx,dict(self.receipt('failure'),error_code='provider_rate_limited',scan_retained=retained))
            self.assertEqual(len(tx.calls),1)
            self.assertEqual(tx.events[-1],'poison')

    def test_failure_stops_new_work_but_does_not_release_sibling_leases(self):
        tx = Database(self.values('failure'), dict(value='{"outcome":"error"}'), 1, 1)
        self.call(tx, self.receipt('failure'))
        self.assertFalse(tx.calls[-1][2]['success'])
        self.assertFalse(tx.calls[-1][2]['done'])
        self.assertIn('initial_inventory_restart_required', settlement._PARENT)
        self.assertNotIn('lease_token=NULL', settlement._PARENT)

    def test_expired_unknown_records_estimated_cost_and_block_without_terminal_proof(self):
        tx = Database(self.values('unknown'), dict(value='{"outcome":"unknown","provider_attempts":1}'), 1, 1)
        result = self.call(tx, self.receipt('unknown'))
        self.assertEqual(result['provider_attempts'], 1)
        values = tx.calls[-1][2]
        self.assertTrue(values['unknown'])
        self.assertFalse(values['done'])
        self.assertFalse(values['success'])
        self.assertFalse(values['quota'])
        self.assertEqual((values['attempts'], values['pages']), (1, 0))

    def test_oversized_or_inconsistent_counts_poison_borrowed_transaction(self):
        values = self.values()
        values['items'] = 51
        tx = Database(values)
        with self.assertRaisesRegex(DatabaseError, 'counts_mismatch'):
            self.call(tx, self.receipt())
        self.assertEqual(tx.events[-1], 'poison')

    def test_zero_row_chain_or_parent_update_never_returns_unknown_ack(self):
        for counts, last_sql in (((0,), settlement._CHAIN), ((1, 0), settlement._PARENT)):
            tx = Database(self.values('unknown'), dict(value='{"outcome":"unknown"}'), *counts)
            with self.assertRaisesRegex(DatabaseError, 'chain_state_invalid'):
                self.call(tx, self.receipt('unknown'))
            self.assertEqual(tx.events[-1], 'poison')
            self.assertEqual(sqls(tx)[-1], last_sql)

    def test_settlement_error_poisoning_prevents_claimed_success(self):
        tx = Database(self.values('failure'), dict(value='{"outcome":"error"}'), 1,
            DatabaseServerError('operator_job_lease_lost', code='P0001'))
        with self.assertRaises(DatabaseError):
            self.call(tx, self.receipt('failure'))
        self.assertEqual(tx.events[-1], 'poison')

    def test_sql_fixed_parameter_binding_and_reviewed_single_statements(self):
        values = dict(args('short', TRANSITION, SCAN, PLAYLIST_LEASE), channel_id=CHANNEL, job='{}',
            chain='{}', scan='{}', playlist='{}', epoch=17, reason=None, blocked=False, evidence='{}', attempts=1, pages=1,
            items=1, inserted=1, updated=0, retry=None, success=True, absent=False, quota=False,
            done=False, unknown=False, rate_limited=False, added=1, proof='{}', results='{}', ack='{}')
        writes = {chains._INIT, chains._ACTIVATE, chains._RESERVE, chains._BIND,
            chains._CANCEL, chains._REFUND, chains._UNSENT_REFUND, settlement._CHAIN, settlement._PARENT}
        returning = {chains._PREPARE, chains._YIELD}
        for module in (chains, settlement):
            for name, sql in vars(module).items():
                if not name.startswith('_') or not isinstance(sql, str) or not sql.lstrip().startswith(('SELECT ', 'UPDATE ', 'INSERT ')):
                    continue
                with self.subTest(module=module.__name__, name=name):
                    params = (JOB, LEASE, 'short', TRANSITION, SCAN, PLAYLIST_LEASE) if sql == chains._INPUT else values
                    if sql == chains._UNSENT_PLAYLIST: params = ('{}',)
                    elif sql == chains._UNSENT_SCAN: params = (SCAN,)
                    PostgresQuery(Transformer()).convert(sql, params)
                    _reviewed_statement(sql, read=sql not in writes,
                        max_rows=0 if sql in writes else 1, returning=sql in returning)



class FinishGuardTests(unittest.TestCase):
    def test_final_finish_and_block_match_real_owner_after_target_work(self):
        from db.jobs import operator_job_reconciliation as reconcile
        job = dict(value=raw(target_count=2))
        for code, statement in ((None, reconcile._GUARDED_FINISH),
                ('initial_inventory_confirmation_required', reconcile._GUARDED_BLOCK)):
            for returned in (None, dict(value='{"status":"queued"}')):
                with self.subTest(code=code, row=returned is not None):
                    tx = Database(dict(job_id=JOB, limit=250), job, dict(invalid=False), 0,
                        dict(value=False), job, dict(value=True), dict(value=2), dict(code=code), returned)
                    with patch.object(reconcile, 'assert_full_initial_scope'):
                        if returned is None:
                            with self.assertRaisesRegex(DatabaseError, 'operator_job_lease_lost'):
                                reconcile.reconcile_full_initial_targets(tx, JOB, lease_guard=(LEASE, 17))
                            self.assertEqual(tx.events[-1], 'poison')
                        else:
                            result = reconcile.reconcile_full_initial_targets(tx, JOB, lease_guard=(LEASE, 17))
                            self.assertEqual(result['status'], 'queued')
                    self.assertEqual(sqls(tx)[-1], statement)
                    self.assertLess(sqls(tx).index(reconcile._RESOLVE), len(tx.calls)-1)
                    self.assertEqual(tx.calls[-1][2][-3:], (LEASE, 17, 17))

    def test_partial_reconciliation_requeue_has_same_final_guard_and_zero_rows_error(self):
        from db.jobs import operator_job_reconciliation as reconcile
        tx = Database(dict(job_id=JOB, limit=250), dict(value=raw(target_count=2)),
            dict(invalid=False), 1, dict(value=True), None)
        with patch.object(reconcile, 'assert_full_initial_scope'):
            with self.assertRaisesRegex(DatabaseError, 'operator_job_lease_lost'):
                reconcile.reconcile_full_initial_targets(tx, JOB, lease_guard=(LEASE, 17))
        self.assertEqual(tx.calls[-1], ('returning', reconcile._GUARDED_REQUEUE, (JOB, LEASE, 17, 17)))
        self.assertEqual(tx.events[-1], 'poison')

    def test_guarded_statements_bind_native_and_scalar_sql_remains_unchanged(self):
        from db.jobs import operator_job_reconciliation as reconcile
        for original, guarded, parameters in (
                (reconcile._BLOCK, reconcile._GUARDED_BLOCK, ('initial_inventory_confirmation_required', JOB)),
                (reconcile._FINISH, reconcile._GUARDED_FINISH, (JOB,)),
                (reconcile._REQUEUE, reconcile._GUARDED_REQUEUE, (JOB,))):
            self.assertNotIn('clock_timestamp()', original)
            self.assertIn('j.lease_expires_at>clock_timestamp()', guarded)
            self.assertIn("j.status='running' AND j.lease_token=%s::uuid", guarded)
            self.assertIn('j.accepted_epoch=%s::bigint AND j.lease_channel_epoch=%s::bigint', guarded)
            PostgresQuery(Transformer()).convert(guarded, (*parameters, LEASE, 17, 17))
            _reviewed_statement(guarded, read=True, max_rows=1, returning=True)


if __name__ == '__main__':
    unittest.main()
