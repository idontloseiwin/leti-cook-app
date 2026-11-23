"""Opt-in durable inventory chains; no provider calls or independent leases.

The parent job serializes all transitions. Child rows hold one checkpoint per
format; playlist/scan owners retain leases, page tokens and provider receipts.
The supervised operator worker composes these owners under one parent lease.
"""
from contextlib import contextmanager

from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError, Operation
from ..channel.channel_control import assert_channel_scope
from ..inventory.inventory_scan_clock import SCAN_IS_HOT_SQL
from . import operator_job_inventory_binding as binding
from . import operator_job_inventory_preparation as planning
from .operator_job_inventory_evidence import (
    _NativeObject, _encoded, initial_inventory_checkpoint_evidence,
)
from .operator_job_inventory_chain_proof import validate_inventory_chain_copies, validate_inventory_chain_replay
from .operator_job_scope import assert_full_initial_scope
from .operator_job_reconciliation import reconcile_full_initial_targets

_INPUT = """SELECT %s::uuid::text AS job_id,%s::uuid::text AS lease_token,
 %s::text AS video_type,%s::uuid::text AS transition,%s::uuid::text AS scan_id,
 %s::uuid::text AS playlist_lease_token"""
_LEASE = planning._LEASE.replace('<=now()', '<=clock_timestamp()').replace(
    "OR (j.phase='inventory' AND j.summary_json ? 'inventory_chain_version')", '')
_CHAIN = """SELECT to_jsonb(c)::text AS value FROM public.operator_job_inventory_chains c
 WHERE job_id=%(job_id)s::uuid AND channel_id=%(channel_id)s::text
 AND video_type=%(video_type)s::text FOR UPDATE"""
_ROWS = """SELECT count(*)::integer AS count,coalesce(bool_and(channel_id=%(channel_id)s::text),false) AS same_channel,
 coalesce(bool_and(checkpoint='{}'::jsonb),false) AS idle,
 coalesce(bool_and(coalesce(terminal_outcome IN('completed','playlist_not_found'),false)),false) AS done
 FROM public.operator_job_inventory_chains WHERE job_id=%(job_id)s::uuid"""
_INIT = """INSERT INTO public.operator_job_inventory_chains
 (job_id,channel_id,video_type,scan_id,checkpoint,last_ack,terminal_outcome)
 SELECT j.id,j.channel_id,kind,
 CASE WHEN kind=j.inventory_video_type THEN coalesce(j.inventory_scan_id,
 (j.summary_json->'full_inventory_sources'->kind->>'scan_id')::uuid)
 ELSE (j.summary_json->'full_inventory_sources'->kind->>'scan_id')::uuid END,
 CASE WHEN kind=j.inventory_video_type THEN j.inventory_checkpoint ELSE '{}'::jsonb END,
 CASE WHEN j.summary_json->'inventory_ack'->>'video_type'=kind
 THEN j.summary_json->'inventory_ack' ELSE '{}'::jsonb END,
 j.summary_json->'full_inventory_sources'->kind->>'terminal_outcome'
 FROM public.operator_jobs j CROSS JOIN unnest(ARRAY['long','short','stream']::text[]) kind
 WHERE j.id=%(job_id)s::uuid ORDER BY kind"""
_ACTIVATE = """UPDATE public.operator_jobs SET inventory_checkpoint='{}'::jsonb,inventory_scan_id=NULL,
 resume_page_token=NULL,summary_json=summary_json||jsonb_build_object('inventory_chain_version',1),
 updated_at=now() WHERE id=%(job_id)s::uuid"""
_OVERLAY = """SELECT (to_jsonb(j)||jsonb_build_object('inventory_video_type',c.video_type,
 'inventory_scan_id',c.scan_id,'inventory_checkpoint',c.checkpoint))::text AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 CROSS JOIN jsonb_populate_record(NULL::public.operator_job_inventory_chains,%(chain)s::jsonb) c"""
_FINAL = """SELECT j.status='running' AND j.lease_token=%(lease_token)s::uuid
 AND j.lease_expires_at>clock_timestamp() AND j.accepted_epoch=%(epoch)s::bigint
 AND j.lease_channel_epoch=%(epoch)s::bigint AS valid
 FROM public.operator_jobs j WHERE j.id=%(job_id)s::uuid"""
_PAUSE = """SELECT CASE WHEN j.last_error_code='initial_inventory_restart_required'
 THEN j.last_error_code WHEN j.target_count>10000 AND NOT coalesce((j.summary_json->>'confirmed')::boolean,false)
 THEN 'initial_inventory_confirmation_required' WHEN j.retry_not_before>clock_timestamp()
 THEN 'initial_inventory_retry_pending' END AS reason,
 j.retry_not_before::text AS retry_not_before
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_PREPARE = """UPDATE public.operator_job_inventory_chains SET checkpoint=jsonb_build_object(
 'phase','prepared','job_lease_token',%(lease_token)s::uuid,'transition_id',%(transition)s::uuid,'reserved_units',1),
 updated_at=now() WHERE job_id=%(job_id)s::uuid AND video_type=%(video_type)s::text
 RETURNING to_jsonb(operator_job_inventory_chains)::text AS value"""
_RESERVE = """UPDATE public.operator_jobs SET quota_units_reserved=quota_units_reserved+1,
 heartbeat_at=now(),updated_at=now() WHERE id=%(job_id)s::uuid"""
_BIND = """UPDATE public.operator_job_inventory_chains SET scan_id=%(scan_id)s::uuid,
 checkpoint=checkpoint||jsonb_build_object('phase','bound','playlist_lease_token',%(playlist_lease_token)s::uuid),
 updated_at=now() WHERE job_id=%(job_id)s::uuid AND video_type=%(video_type)s::text"""
_BIND_REPLAY = """SELECT EXISTS(SELECT 1 FROM public.operator_job_inventory_chains
 WHERE job_id=%(job_id)s::uuid AND video_type=%(video_type)s::text AND scan_id=%(scan_id)s::uuid
 AND checkpoint=jsonb_build_object('phase','bound','job_lease_token',%(lease_token)s::uuid,
 'transition_id',%(transition)s::uuid,'reserved_units',1,'playlist_lease_token',%(playlist_lease_token)s::uuid)) AS value"""
_ACK_REPLAY = """SELECT last_ack::text AS value FROM public.operator_job_inventory_chains
 WHERE job_id=%(job_id)s::uuid AND video_type=%(video_type)s::text
 AND last_ack->>'transition_id'=%(transition)s::uuid::text
 AND last_ack->>'job_lease_token'=%(lease_token)s::uuid::text"""
_CANCEL = """UPDATE public.operator_job_inventory_chains SET checkpoint='{}'::jsonb,updated_at=now()
 WHERE job_id=%(job_id)s::uuid AND video_type=%(video_type)s::text"""
_REFUND = """UPDATE public.operator_jobs SET quota_units_reserved=greatest(quota_units_reserved-1,0),
 updated_at=now() WHERE id=%(job_id)s::uuid"""
# Cancellation proves source continuity under the ordinary admitted owner; it
# never treats token absence alone as evidence that a provider was not called.
_UNSENT_PLAYLIST = binding._PLAYLIST + " NOWAIT"
_UNSENT_SCAN = binding._SCAN + " NOWAIT"
_UNSENT_SOURCE = """SELECT coalesce(
 state.active_scan_id=%(scan_id)s::uuid
 AND state.resume_page_token IS NOT DISTINCT FROM scan.resume_page_token
 AND state.scan_boundary_at IS NOT DISTINCT FROM scan.scan_boundary_at
 AND ((state.lease_token=%(playlist_lease_token)s::uuid AND state.status='leased'
       AND state.lease_channel_epoch=%(epoch)s::bigint)
      OR (state.lease_token IS NULL AND state.status='ready'))
 AND proof->>'scan_id'=%(scan_id)s::uuid::text AND proof->>'terminal_outcome' IS NULL
 AND scan.pages_completed=(proof->>'acked_pages')::integer
 AND scan.last_transition_id IS NOT DISTINCT FROM (proof->>'last_transition_id')::uuid
 AND scan.started_at=(proof->>'started_at')::timestamptz
 AND scan.scan_boundary_at=(proof->>'scan_boundary_at')::timestamptz
 AND scan.source_checked_at IS NOT DISTINCT FROM (proof->>'source_checked_at')::timestamptz
 AND """ + SCAN_IS_HOT_SQL.replace('%(scan_at)s::timestamptz', 'native.at') + """,false) AS valid
 FROM jsonb_populate_record(NULL::public.channel_video_inventory_scans,%(scan)s::jsonb) scan
 CROSS JOIN jsonb_populate_record(NULL::public.channel_playlist_state,%(playlist)s::jsonb) state
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 CROSS JOIN LATERAL (SELECT j.summary_json->'full_inventory_sources'->j.inventory_video_type AS proof) source
 CROSS JOIN (SELECT clock_timestamp() AS at OFFSET 0) native"""
_UNSENT_RECEIPTS = """SELECT receipt_scope FROM public.typed_inventory_receipts
 WHERE transition_id=%(transition)s::uuid AND receipt_scope IN('scan_transition','quota_defer')
 ORDER BY receipt_scope FOR UPDATE NOWAIT"""
_UNSENT_REFUND = """UPDATE public.operator_jobs SET quota_units_reserved=quota_units_reserved-1,
 updated_at=now() WHERE id=%(job_id)s::uuid AND quota_units_reserved>=1"""
_YIELD = """UPDATE public.operator_jobs j SET status=CASE WHEN %(blocked)s::boolean THEN 'blocked' ELSE 'queued' END,
 last_error_code=CASE WHEN %(blocked)s::boolean THEN %(reason)s::text ELSE last_error_code END,
 finished_at=CASE WHEN %(blocked)s::boolean THEN now() END,
 lease_token=NULL,lease_owner=NULL,lease_channel_epoch=NULL,lease_expires_at=NULL,updated_at=now()
 WHERE id=%(job_id)s::uuid AND j.status='running' AND j.lease_token=%(lease_token)s::uuid
 AND j.lease_expires_at>clock_timestamp() AND j.accepted_epoch=%(epoch)s::bigint
 AND j.lease_channel_epoch=%(epoch)s::bigint RETURNING to_jsonb(j)::text AS value"""

_ERRORS = frozenset((
    'initial_inventory_chain_input_invalid', 'initial_inventory_chain_state_invalid',
    'initial_inventory_chain_not_initialized', 'initial_inventory_chain_outstanding',
    'initial_inventory_chain_paused', 'initial_inventory_job_checkpoint_mismatch',
    'initial_inventory_outcome_unresolved', 'initial_inventory_chain_terminal',
    'initial_inventory_quota_retry_invalid', 'initial_inventory_chain_adoption_deferred',
))
for _name in ('prepare_initial_inventory_job_chunk', 'bind_initial_inventory_scan',
              'settle_initial_inventory_job_chunk', 'fail_initial_inventory_job_chunk', 'claim_initial_inventory_source'):
    _ERRORS = _ERRORS | get_operation(_name).error_codes


def _fail(code):
    raise DatabaseServerError(code, code='P0001')


@contextmanager
def _transaction(database, transaction, name):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        spec = get_operation('settle_initial_inventory_job_chunk')
        with database.transaction(Operation(name, statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms, total_timeout_ms=spec.statement_timeout_ms,
                error_codes=_ERRORS)) as tx:
            yield tx


def _args(tx, job_id, lease_token, video_type=None, transition=None, scan_id=None, playlist_lease_token=None):
    args = tx.fetch_one(_INPUT, (job_id, lease_token, video_type, transition, scan_id, playlist_lease_token))
    if args['job_id'] is None or args['lease_token'] is None:
        _fail('initial_inventory_chain_input_invalid')
    if video_type is not None and args['video_type'] not in ('long', 'short', 'stream'):
        _fail('initial_inventory_chain_input_invalid')
    return args


def _hold(tx, args, *, initialize=False):
    row = tx.fetch_one(planning._CHANNEL_ID, (args['job_id'],))
    if row is None:
        _fail('operator_job_lease_lost')
    args['channel_id'] = row['channel_id']
    if tx.fetch_one(planning._CHANNEL, (args['channel_id'],)) is None:
        _fail('operator_job_lease_lost')
    row = tx.fetch_one(planning._JOB, args)
    if row is None:
        _fail('operator_job_lease_lost')
    args['job'] = row['value']
    job = _NativeObject(args['job'])
    held = tx.fetch_one(_LEASE, args)
    args['epoch'] = held['epoch']
    if (held['invalid'] is not False or held['epoch'] is None
            or held['accepted_epoch'] != held['epoch']):
        _fail('operator_job_lease_lost')
    if job.get('initial_scope_version') != 1 or job.get('phase') != 'inventory':
        _fail('full_initial_scope_invalid')
    assert_full_initial_scope(tx, args['job'], False)
    marker = job.get('summary_json', {}).get('inventory_chain_version')
    if marker is not None and (type(marker) is not int or marker != 1):
        _fail('initial_inventory_chain_state_invalid')
    if not initialize and marker != 1:
        _fail('initial_inventory_chain_not_initialized')
    if marker == 1 and (job.get('inventory_checkpoint') != {} or job.get('inventory_scan_id') is not None):
        _fail('initial_inventory_chain_state_invalid')
    return job


def _chain(tx, args):
    row = tx.fetch_one(_CHAIN, args)
    if row is None:
        _fail('initial_inventory_chain_state_invalid')
    args['chain'] = row['value']
    return _NativeObject(args['chain'])


def _overlay(tx, args):
    args['job'] = tx.fetch_one(_OVERLAY, args)['value']
    return _NativeObject(args['job'])


def _fence(tx, args):
    # Read the real parent row after all blocking child locks and writes.
    # An overlay or an earlier captured snapshot cannot prove a live lease.
    row = tx.fetch_one(_FINAL, args)
    if row is None or row['valid'] is not True:
        _fail('operator_job_lease_lost')
    assert_channel_scope(tx, args['channel_id'], args['epoch'])


def _paused(tx, args):
    return tx.fetch_one(_PAUSE, args)


def _prepared(chain):
    checkpoint = chain['checkpoint']
    return dict(action='inventory', video_type=chain['video_type'], page_limit=1,
        transition_id=checkpoint['transition_id'], scan_id=chain['scan_id'],
        reserved_units=checkpoint['reserved_units'], dispatch_allowed=False)


def initialize_inventory_chains(database, job_id, lease_token, *, transaction=None):
    """Atomically adopt scalar full-scope progress; never reset an unknown bind."""
    with _transaction(database, transaction, 'initialize_inventory_chains') as tx:
        args = _args(tx, job_id, lease_token)
        job = _hold(tx, args, initialize=True)
        rows = tx.fetch_one(_ROWS, args)
        if job['summary_json'].get('inventory_chain_version') == 1:
            if rows['count'] != 3 or rows['same_channel'] is not True:
                _fail('initial_inventory_chain_state_invalid')
            validate_inventory_chain_copies(tx, args['job_id'], lock_sources=True)
        else:
            if rows['count'] != 0:
                _fail('initial_inventory_chain_state_invalid')
            checkpoint = job['inventory_checkpoint']
            if checkpoint.get('phase') == 'prepared' and checkpoint.get('reserved_units') == 0:
                # No current scope-v1 owner creates this state. Do not invent a
                # reservation or promise scalar reconciliation can finish it.
                _fail('initial_inventory_chain_adoption_deferred')
            if checkpoint and checkpoint.get('reserved_units') != 1:
                _fail('initial_inventory_chain_state_invalid')
            validate_inventory_chain_copies(tx, args['job_id'], scalar=True, lock_sources=True)
            tx.execute(_INIT, args)
            tx.execute(_ACTIVATE, args)
        _fence(tx, args)
        return dict(status='initialized', job_id=args['job_id'], chain_count=3)


def prepare_inventory_chain(database, job_id, lease_token, video_type, *, transaction=None):
    """Return one prepared identity or existing receipt action; never dispatch."""
    with _transaction(database, transaction, 'prepare_inventory_chain') as tx:
        args = _args(tx, job_id, lease_token, video_type)
        _hold(tx, args)
        chain = _chain(tx, args)
        _overlay(tx, args)
        checkpoint = chain['checkpoint']
        if checkpoint.get('phase') == 'bound':
            evidence = initial_inventory_checkpoint_evidence(tx, args['job'])
            args['evidence'] = _encoded(evidence)
            action = tx.fetch_one(planning._ACTION, args)['value']
            result = dict(action=action, video_type=video_type, transition_id=checkpoint['transition_id'],
                scan_id=chain['scan_id'], evidence=evidence, dispatch_allowed=False)
        elif chain['terminal_outcome'] is not None:
            if checkpoint:
                _fail('initial_inventory_chain_state_invalid')
            result = dict(action='done', video_type=video_type, dispatch_allowed=False)
        else:
            pause = _paused(tx, args)
            if pause['reason'] is not None:
                result = dict(action='paused', **pause, dispatch_allowed=False)
            else:
                if checkpoint and (checkpoint.get('phase') != 'prepared' or checkpoint.get('reserved_units') != 1):
                    _fail('initial_inventory_chain_state_invalid')
                # An old prepared command was never authorized for HTTP. Adopt
                # its exact transition without duplicating its reservation.
                if not checkpoint:
                    tx.execute(planning._PLAYLIST_INSERT, (args['job'],))
                    tx.execute(planning._WAKE, (args['job'],))
                    args['transition'] = tx.fetch_one('SELECT gen_random_uuid()::text AS value')['value']
                else:
                    args['transition'] = checkpoint['transition_id']
                if not checkpoint or checkpoint['job_lease_token'] != args['lease_token']:
                    chain = _NativeObject(tx.returning_one(_PREPARE, args)['value'])
                    if not checkpoint:
                        tx.execute(_RESERVE, args)
                result = _prepared(chain)
        _fence(tx, args)
        return result


def bind_inventory_chain(database, job_id, lease_token, video_type, transition_id,
                         scan_id, playlist_lease_token, *, transaction=None):
    """Exact replay is ACK only; fresh permission is valid only after COMMIT."""
    with _transaction(database, transaction, 'bind_inventory_chain') as tx:
        args = _args(tx, job_id, lease_token, video_type, transition_id, scan_id, playlist_lease_token)
        if any(args[k] is None for k in ('video_type', 'transition', 'scan_id', 'playlist_lease_token')):
            _fail('initial_inventory_chain_input_invalid')
        result = dict(status='bound', job_id=args['job_id'], video_type=video_type,
            scan_id=args['scan_id'], transition_id=args['transition'])
        if tx.fetch_one(_BIND_REPLAY, args)['value'] is True:
            validate_inventory_chain_replay(tx, args, ack=False)
            return dict(result, replayed=True, dispatch_allowed=False)
        _hold(tx, args)
        chain = _chain(tx, args)
        # A concurrent first bind may commit while this call waits for parent
        # locks. Recheck the durable exact identity after those locks too.
        expected_bound = dict(phase='bound', job_lease_token=args['lease_token'],
            transition_id=args['transition'], reserved_units=1,
            playlist_lease_token=args['playlist_lease_token'])
        if chain['scan_id'] == args['scan_id'] and chain['checkpoint'] == expected_bound:
            validate_inventory_chain_replay(tx, args, ack=False)
            return dict(result, replayed=True, dispatch_allowed=False)
        if chain['terminal_outcome'] is not None:
            _fail('initial_inventory_chain_terminal')
        _overlay(tx, args)
        if _paused(tx, args)['reason'] is not None:
            _fail('initial_inventory_chain_paused')
        expected = dict(phase='prepared', job_lease_token=args['lease_token'],
            transition_id=args['transition'], reserved_units=1)
        if chain['checkpoint'] != expected:
            _fail('initial_inventory_bind_conflict')
        args.update(job_id=args['job_id'], job_lease_token=args['lease_token'], transition_id=args['transition'])
        row = tx.fetch_one(binding._PLAYLIST, (args['job'],))
        if row is None:
            _fail('initial_inventory_playlist_lease_lost')
        args['playlist'] = row['value']
        if tx.fetch_one(binding._PLAYLIST_CHECK, args)['invalid'] is not False:
            _fail('initial_inventory_playlist_lease_lost')
        row = tx.fetch_one(binding._SCAN, (args['scan_id'],))
        if row is None:
            _fail('initial_inventory_scan_identity_invalid')
        args['scan'] = row['value']
        facts = tx.fetch_one(binding._SCAN_CHECK, args)
        if facts['identity_invalid'] is not False:
            _fail('initial_inventory_scan_identity_invalid')
        if facts['kind_invalid'] is not False:
            _fail('initial_inventory_scan_kind_invalid')
        facts = tx.fetch_one(binding._FULL_CHECK, args)
        if facts['invalid'] is not False:
            _fail('full_initial_source_coverage_invalid' if facts['has_source'] else 'full_initial_fresh_generation_required')
        if facts['has_source'] is not True and tx.execute(binding._REGISTER_SOURCE, args) != 1:
            _fail('initial_inventory_chain_state_invalid')
        if tx.execute(_BIND, args) != 1:
            _fail('initial_inventory_chain_state_invalid')
        _fence(tx, args)
        return dict(result, replayed=False, dispatch_allowed=True)


def acknowledge_inventory_chain(database, job_id, lease_token, video_type, transition_id, *,
                                resolve_unknown=False, transaction=None):
    """Settle counts from a durable provider receipt, then record a per-chain ACK."""
    from .operator_job_inventory_chain_settlement import settle_inventory_chain_receipt
    with _transaction(database, transaction, 'acknowledge_inventory_chain') as tx:
        args = _args(tx, job_id, lease_token, video_type, transition_id)
        if args['video_type'] is None or args['transition'] is None:
            _fail('initial_inventory_chain_input_invalid')
        if type(resolve_unknown) is not bool:
            _fail('initial_inventory_chain_input_invalid')
        replay = tx.fetch_one(_ACK_REPLAY, args)
        if replay is not None:
            proved_ack = validate_inventory_chain_replay(tx, args, ack=True)
            return dict(ack=proved_ack, replayed=True, dispatch_allowed=False)
        _hold(tx, args)
        chain = _chain(tx, args)
        # First settlement can become visible only after the parent lock wait.
        last_ack = chain['last_ack']
        if (last_ack.get('transition_id') == args['transition']
                and last_ack.get('job_lease_token') == args['lease_token']):
            proved_ack = validate_inventory_chain_replay(tx, args, ack=True)
            return dict(ack=proved_ack, replayed=True, dispatch_allowed=False)
        checkpoint = chain['checkpoint']
        if checkpoint.get('phase') != 'bound' or checkpoint.get('transition_id') != args['transition']:
            _fail('initial_inventory_job_checkpoint_mismatch')
        _overlay(tx, args)
        evidence = initial_inventory_checkpoint_evidence(tx, args['job'])
        if evidence.get('kind') not in ('page', 'failure', 'quota'):
            if not (resolve_unknown and evidence.get('kind') == 'none' and evidence.get('bound_live') is False):
                _fail('initial_inventory_outcome_unresolved')
            # Expired original playlist lease + live current parent are both
            # required. This is an estimated unknown attempt, never success.
            evidence = dict(kind='unknown', provider_attempts=1, pages_processed=0,
                items_processed=0, inserted=0, updated=0, format_finished=False,
                format_absent=False, error_code='initial_inventory_outcome_unknown')
        result = settle_inventory_chain_receipt(tx, args, evidence)
        _fence(tx, args)
        return dict(ack=result, replayed=False, dispatch_allowed=False)


def cancel_prepared_inventory_chain(database, job_id, lease_token, video_type, transition_id, *, transaction=None):
    """Cancel only a definitely unsent prepared command; bound work needs proof."""
    with _transaction(database, transaction, 'cancel_prepared_inventory_chain') as tx:
        args = _args(tx, job_id, lease_token, video_type, transition_id)
        _hold(tx, args)
        chain = _chain(tx, args)
        checkpoint = chain['checkpoint']
        if (checkpoint.get('phase') != 'prepared' or checkpoint.get('transition_id') != args['transition']
                or checkpoint.get('reserved_units') != 1):
            _fail('initial_inventory_job_checkpoint_mismatch')
        tx.execute(_CANCEL, args)
        tx.execute(_REFUND, args)
        _fence(tx, args)
        return dict(status='cancelled', dispatch_allowed=False)



def cancel_unsent_inventory_chain(database, job_id, lease_token, video_type, transition_id,
                                 scan_id, playlist_lease_token, *, fetch_unsent, transaction=None):
    """Cancel one exact bound but definitely unsent command, never replay an ACK.

    The caller must prove its fetch was not dispatched and cannot still run.
    Unknown submit/claim/bind/cancel outcomes do not satisfy that prerequisite.
    Playlist absence may follow an already confirmed leaf drain, but is not
    itself provider evidence. All remaining mutation stays in this transaction.
    """
    if fetch_unsent is not True:
        _fail('initial_inventory_chain_input_invalid')
    from ..hot.hot_collection_termination import drain_hot_collection_claims
    with _transaction(database, transaction, 'cancel_unsent_inventory_chain') as tx:
        args = _args(tx, job_id, lease_token, video_type, transition_id, scan_id, playlist_lease_token)
        if any(args[k] is None for k in ('video_type', 'transition', 'scan_id', 'playlist_lease_token')):
            _fail('initial_inventory_chain_input_invalid')
        _hold(tx, args)
        chain = _chain(tx, args)
        expected = dict(phase='bound', job_lease_token=args['lease_token'],
            transition_id=args['transition'], reserved_units=1,
            playlist_lease_token=args['playlist_lease_token'])
        if (chain['scan_id'] != args['scan_id'] or chain['checkpoint'] != expected
                or chain['terminal_outcome'] is not None):
            _fail('initial_inventory_job_checkpoint_mismatch')
        _overlay(tx, args)
        row = tx.fetch_one(_UNSENT_PLAYLIST, (args['job'],))
        if row is None:
            _fail('initial_inventory_playlist_lease_lost')
        args['playlist'] = row['value']
        row = tx.fetch_one(_UNSENT_SCAN, (args['scan_id'],))
        if row is None:
            _fail('initial_inventory_scan_identity_invalid')
        args['scan'] = row['value']
        facts = tx.fetch_one(binding._SCAN_CHECK, args)
        if facts['identity_invalid'] is not False or facts['kind_invalid'] is not False:
            _fail('initial_inventory_scan_identity_invalid')
        if tx.fetch_one(_UNSENT_SOURCE, args)['valid'] is not True:
            _fail('initial_inventory_outcome_unresolved')
        if tx.fetch_all(_UNSENT_RECEIPTS, args, max_rows=2):
            _fail('initial_inventory_outcome_unresolved')
        # Source writers require the playlist/scan locks held above. Their
        # receipt cannot appear between the absence proof and the exact drain.
        receipt = drain_hot_collection_claims(tx, [], [dict(channel_id=args['channel_id'],
            video_type=args['video_type'], lease_token=args['playlist_lease_token'])], [])
        if receipt.get('status') != 'drained' or receipt.get('remaining') != 0:
            _fail('initial_inventory_outcome_unresolved')
        if tx.execute(_CANCEL, args) != 1 or tx.execute(_UNSENT_REFUND, args) != 1:
            _fail('initial_inventory_chain_state_invalid')
        _fence(tx, args)
        return dict(status='cancelled', dispatch_allowed=False, reserved_units_refunded=1)


def finish_inventory_chains(database, job_id, lease_token, *, transaction=None):
    """Explicit yield/finish only after all bound/prepared commands are drained."""
    with _transaction(database, transaction, 'finish_inventory_chains') as tx:
        args = _args(tx, job_id, lease_token)
        _hold(tx, args)
        rows = tx.fetch_one(_ROWS, args)
        if rows['count'] != 3 or rows['same_channel'] is not True:
            _fail('initial_inventory_chain_state_invalid')
        if rows['idle'] is not True:
            _fail('initial_inventory_chain_outstanding')
        _fence(tx, args)
        if rows['done'] is True:
            result = reconcile_full_initial_targets(tx, args['job_id'], 250,
                lease_guard=(args['lease_token'], args['epoch']))
        else:
            pause = _paused(tx, args)
            args.update(reason=pause['reason'], blocked=pause['reason'] in (
                'initial_inventory_confirmation_required', 'initial_inventory_restart_required'))
            released = tx.returning_one(_YIELD, args)
            if released is None:
                _fail('operator_job_lease_lost')
            result = _NativeObject(released['value'])
        # The explicit finish owner intentionally clears the parent lease.
        assert_channel_scope(tx, args['channel_id'], args['epoch'])
        return result


def claim_inventory_chain_source(database, job_id, lease_token, video_type, transition_id,
                                 worker_id, run_id, lease_seconds=900, *, transaction=None):
    """Claim one exact prepared chain through the existing playlist lease owner."""
    from ..inventory import inventory_claims
    with _transaction(database, transaction, 'claim_inventory_chain_source') as tx:
        args = _args(tx, job_id, lease_token, video_type, transition_id)
        if args['video_type'] is None or args['transition'] is None:
            _fail('initial_inventory_chain_input_invalid')
        _hold(tx, args)
        chain = _chain(tx, args)
        expected = dict(phase='prepared', job_lease_token=args['lease_token'],
            transition_id=args['transition'], reserved_units=1)
        if chain['checkpoint'] != expected or chain['terminal_outcome'] is not None:
            _fail('initial_inventory_job_checkpoint_mismatch')
        _overlay(tx, args)
        if _paused(tx, args)['reason'] is not None:
            _fail('initial_inventory_chain_paused')
        args.update(tx.fetch_one(inventory_claims._INITIAL_INPUT,
            (job_id, lease_token, worker_id, run_id, lease_seconds)))
        held = tx.fetch_one(inventory_claims._JOB_CHECK, args)
        if held['invalid'] is not False or held['video_type'] != args['video_type'] or held['epoch'] != args['epoch']:
            _fail('operator_job_lease_lost')
        claims = inventory_claims.claim_prepared_initial_source(tx, args, chain_transition=args['transition'])
        _fence(tx, args)
        return dict(status='claimed' if claims else 'waiting', claims=claims, dispatch_allowed=False)
