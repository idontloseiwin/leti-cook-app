"""Source-free termination of already owned HOT collection work.

These closed database operations never admit source reads, claims, provider work or
successful settlement. A transport error is not retried here.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from uuid import UUID

from hot_runtime import HotDatabaseAdmissionDenied, collection_control_access

VIDEO_RPCS = frozenset({
    'complete_initial_video_batch',
    'complete_video_counter_collection', 'complete_video_profile_metadata_collection',
    'complete_typed_video_metadata_collection', 'complete_stream_lifecycle_probe',
    'complete_typed_video_missing', 'fail_video_collection_claims',
    'complete_video_identity_refresh',
    'fail_typed_video_collection_claims', 'fail_video_collection_batch',
})
PLAYLIST_RPCS = frozenset({
    'begin_typed_inventory_scan', 'settle_typed_inventory_page', 'fail_typed_inventory_scan',
})
OPERATOR_RPCS = frozenset({
    'prepare_operator_job_chunk', 'prepare_initial_inventory_job_chunk',
    'bind_initial_inventory_scan', 'claim_initial_inventory_source',
    'prepare_initial_hydration_job_chunk', 'settle_initial_hydration_job_chunk',
    'settle_initial_inventory_job_chunk', 'settle_initial_inventory_stats',
    'fail_initial_inventory_job_chunk', 'settle_operator_job_chunk',
    'verify_initial_hydration_job', 'release_operator_job',
})


_PRIOR_ACK_UNKNOWN = ContextVar("hot_prior_ack_unknown", default=False)


@contextmanager
def uncertain_collection_ack():
    """Fence retries whose original transport response was not received."""
    token = _PRIOR_ACK_UNKNOWN.set(True)
    try:
        yield
    except HotDatabaseAdmissionDenied as error:
        error.outcome_may_be_committed = True
        raise
    finally:
        _PRIOR_ACK_UNKNOWN.reset(token)


def _identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 33 for c in value):
        raise ValueError('hot_control_identity_invalid')
    return value


def _token(value):
    if not isinstance(value, str): raise ValueError('hot_control_lease_invalid')
    return str(UUID(value))


def _claims(rows, identity, maximum):
    if not isinstance(rows, (list, tuple)) or len(rows) > maximum:
        raise ValueError('hot_control_claim_bound')
    result = []
    seen = set()
    fields = {identity, 'lease_token'} | ({'video_type'} if identity != 'job_id' else set())
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != fields:
            raise ValueError('hot_control_claim_fields')
        value = {identity: _token(row[identity]) if identity == 'job_id' else _identity(row[identity]),
                 'lease_token': _token(row['lease_token'])}
        if identity != 'job_id':
            if row['video_type'] not in (('long', 'short', 'stream', 'unknown') if identity == 'video_id' else ('long', 'short', 'stream')):
                raise ValueError('hot_control_video_type')
            value['video_type'] = row['video_type']
        key = (value[identity], value.get('video_type'))
        if key in seen: raise ValueError('hot_control_duplicate_claim')
        seen.add(key)
        result.append(value)
    return result


def _result(response):
    value = response
    if isinstance(value, list) and len(value) == 1: value = value[0]
    if not isinstance(value, dict): raise RuntimeError('hot_control_receipt_invalid')
    return value


class HotCollectionControlRepository:
    def __init__(self, database): self.database = database

    def abort(self, *, videos=(), playlists=(), operators=()):
        groups = {
            'video': _claims(videos, 'video_id', 50),
            'playlist': _claims(playlists, 'channel_id', 500),
            'operator': _claims(operators, 'job_id', 1),
        }
        if not any(groups.values()): return {'status': 'aborted', **{k+'_'+s: 0 for k in groups for s in ('requested','released')}}
        with collection_control_access():
            value = _result(self.database.abort_hot_collection_claims(
                video_claims=groups['video'], playlist_claims=groups['playlist'],
                operator_claims=groups['operator']))
        expected = {'status'} | {kind+'_'+suffix for kind in groups for suffix in ('requested','released')}
        if set(value) != expected or value['status'] != 'aborted': raise RuntimeError('hot_control_receipt_invalid')
        for kind, rows in groups.items():
            requested, released = value[kind+'_requested'], value[kind+'_released']
            if type(requested) is not int or type(released) is not int or requested != len(rows) or not 0 <= released <= requested:
                raise RuntimeError('hot_control_receipt_invalid')
        return value

    def drain(self, *, videos=(), playlists=(), operators=()):
        groups = {
            'video': _claims(videos, 'video_id', 50),
            'playlist': _claims(playlists, 'channel_id', 500),
            'operator': _claims(operators, 'job_id', 1),
        }
        if not any(groups.values()):
            return {'status': 'drained', 'remaining': 0,
                    **{k+'_'+s: 0 for k in groups for s in ('requested', 'released')}}
        with collection_control_access():
            value = _result(self.database.drain_hot_collection_claims(
                video_claims=groups['video'], playlist_claims=groups['playlist'],
                operator_claims=groups['operator']))
        expected = {'status', 'remaining'} | {kind+'_'+suffix for kind in groups for suffix in ('requested', 'released')}
        if (set(value) != expected or value['status'] != 'drained'
                or type(value['remaining']) is not int or value['remaining'] != 0):
            raise RuntimeError('hot_control_drain_receipt_invalid')
        for kind, rows in groups.items():
            requested, released = value[kind+'_requested'], value[kind+'_released']
            if type(requested) is not int or type(released) is not int or requested != len(rows) or not 0 <= released <= requested:
                raise RuntimeError('hot_control_drain_receipt_invalid')
        return value

    def interrupt_run(self, run_id, started_at):
        if type(run_id) is not int or run_id <= 0 or not isinstance(started_at, str):
            raise ValueError('hot_control_run_identity_invalid')
        parsed = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError('hot_control_run_identity_invalid')
        with collection_control_access():
            value = _result(self.database.interrupt_hot_sync_run(
                run_id=run_id, started_at=started_at))
        if set(value) != {'status', 'matched'} or value['status'] != 'interrupted' or type(value['matched']) is not int or value['matched'] not in (0, 1):
            raise RuntimeError('hot_control_receipt_invalid')
        return value


def dispatch_owned_rpc(database, name, params, action, *, abort_on_denied=True):
    """Only definite pre-dispatch denial permits abandoning this call's lease.

    Idempotent retries after a lost response must set abort_on_denied=False:
    the first write might have committed even if the retry was not dispatched.
    """
    try:
        return action()
    except HotDatabaseAdmissionDenied as error:
        if not abort_on_denied or _PRIOR_ACK_UNKNOWN.get():
            error.outcome_may_be_committed = True
            error.control_video_ids = tuple(row.get('video_id') for row in (params or {}).get('p_rows', ()))
            raise
        params = params or {}
        groups = {}
        if name in VIDEO_RPCS:
            groups['videos'] = [{
                'video_id': row.get('video_id'),
                'video_type': row.get('video_type') or params.get('p_video_type') or 'long',
                'lease_token': row.get('lease_token'),
            } for row in params.get('p_rows', [])]
        elif name in PLAYLIST_RPCS:
            groups['playlists'] = [{'channel_id': params.get('p_channel_id'),
                'video_type': params.get('p_video_type') or 'long', 'lease_token': params.get('p_lease_token')}]
        elif name == 'defer_typed_inventory_claims_after_daily_quota':
            groups['playlists'] = params.get('p_claims', [])
        elif name in OPERATOR_RPCS:
            groups['operators'] = [{'job_id': params.get('p_job_id'),
                'lease_token': params.get('p_job_lease_token') if name in {'bind_initial_inventory_scan', 'claim_initial_inventory_source'} else params.get('p_lease_token')}]
        if groups:
            error.control_video_ids = tuple(row['video_id'] for row in groups.get('videos', ()))
            error.control_abort_attempted = True
            try:
                error.control_receipt = HotCollectionControlRepository(database).abort(**groups)
            except Exception:
                # No second abort on unknown ACK. The primary call still failed.
                error.control_abort_failed = True
        raise


def admission_denial(error):
    """Find the known rejected dispatch through bounded service exception wrappers."""
    for _ in range(4):
        if isinstance(error, HotDatabaseAdmissionDenied): return error
        error = getattr(error, '__cause__', None)
        if error is None: break
    return None


def abort_pending_playlists(database, claims):
    rows = [{'channel_id': claim.channel_id, 'video_type': getattr(claim, 'video_type', 'long'),
             'lease_token': claim.lease_token} for claim in claims]
    released = 0
    for start in range(0, len(rows), 500):
        value = HotCollectionControlRepository(database).abort(playlists=rows[start:start+500])
        released += value['playlist_released']
    return released


def verify_control_contract(database):
    """Probe closed HOT control contracts without selecting or changing work."""
    zero = {"status": "aborted", **{kind + "_" + suffix: 0
        for kind in ("video", "playlist", "operator")
        for suffix in ("requested", "released")}}
    with collection_control_access():
        value = _result(database.abort_hot_collection_claims(
            video_claims=[], playlist_claims=[], operator_claims=[]))
        if value != zero or any(type(value[key]) is not int for key in zero if key != "status"):
            raise RuntimeError("hot_control_probe_receipt_invalid")
        try:
            database.interrupt_hot_sync_run(
                run_id=0, started_at="1970-01-01T00:00:00+00:00")
        except Exception as error:
            if str(getattr(error, "code", "")) != "P0001" or str(getattr(error, "message", "")) != "hot_interrupt_invalid_run":
                raise RuntimeError("hot_control_probe_contract_unavailable") from None
        else:
            raise RuntimeError("hot_control_probe_contract_invalid")
    return {"status": "ready", "abort_contract": True, "interrupt_contract": True}
