"""Durable restore intent and atomic bind -> initial enqueue; no provider I/O.

The pending row is the only pre-channel lease owner. Once bound, the existing
operator job/control owners take over inside this same transaction. Writes are
never automatically retried, including a missing commit acknowledgement.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from saved_work import channel_id, revision, SavedWorkError
from user_work_backup import LIMITS, UserWorkError, uid
from user_work_reconnect import authorize_reconnect, reconnect_snapshot
from ..base.postgres import DatabaseServerError, Operation
from ..base.operation_contracts import get_operation
from ..jobs.operator_job_enqueuing import (
    estimate_channel_initial_inventory_job, enqueue_channel_initial_inventory_job,
)
from .user_work_restore_bind import bind_user_channel_restore_item, _ERRORS as _BIND_ERRORS

_ERRORS = _BIND_ERRORS | get_operation('enqueue_channel_initial_inventory_job').error_codes
_LOCK = 'SELECT pg_try_advisory_xact_lock(815152610,1) AS locked'
_FACTS = '''SELECT id::text AS id,revision,is_hidden,reconnect_state,reconnect_revision
 FROM public.user_channel_restore_items ORDER BY id LIMIT 10001'''
_LOCKED_FACTS = _FACTS + ' FOR UPDATE NOWAIT'
_ENQUEUE = '''UPDATE public.user_channel_restore_items
 SET reconnect_state=CASE WHEN user_input IS NULL THEN 'blocked' ELSE 'queued' END,
 reconnect_revision=revision,reconnect_requested_at=clock_timestamp(),
 reconnect_error_code=CASE WHEN user_input IS NULL THEN 'needs_input' END
 WHERE id=ANY(%s::uuid[]) AND reconnect_state='idle' AND NOT is_hidden
 RETURNING id::text AS id,reconnect_state,revision'''
_RETRY_SINGLE = '''UPDATE public.user_channel_restore_items
 SET revision=revision+1,reconnect_revision=revision+1,reconnect_state='queued',
 reconnect_requested_at=clock_timestamp(),reconnect_error_code=NULL
 WHERE id=%s::uuid AND revision=%s AND revision<9007199254740991 AND reconnect_state='blocked'
 AND user_input IS NOT NULL AND NOT is_hidden
 RETURNING id::text AS id,revision,reconnect_state,reconnect_revision'''
# Candidates are advisory IDs only. Leasing happens later for one chosen ID;
# identical eligibility/order prevents a RAM-ready queue from reserving work.
# Old manual-admission waits no longer apply to backup reconnect. Provider
# quota/rate waits retain their database-clock boundary in every candidate/claim.
_ELIGIBLE = """NOT candidate.is_hidden AND candidate.reconnect_revision=candidate.revision AND candidate.user_input IS NOT NULL
 AND (candidate.parent_ref IS NULL OR EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=candidate.parent_ref))
 AND (candidate.reconnect_state='queued'
  OR (candidate.reconnect_state='waiting' AND (candidate.reconnect_not_before<=clock_timestamp()
      OR candidate.reconnect_error_code IN ('channel_add_rate_limited','channel_add_admission_unavailable')))
  OR (candidate.reconnect_state='running' AND candidate.reconnect_lease_expires_at<=clock_timestamp()))"""
_FAIR_ORDER = 'candidate.reconnect_last_attempt_at NULLS FIRST,candidate.reconnect_requested_at,candidate.id'
_CANDIDATES = f"""SELECT candidate.id::text AS id FROM public.user_channel_restore_items candidate
 WHERE {_ELIGIBLE} ORDER BY {_FAIR_ORDER} LIMIT %(limit)s"""

_PAGE_CLOCK = 'SELECT clock_timestamp() AS at'
_PAGE_SEEK = """(%(after_id)s::uuid IS NULL
 OR (candidate.reconnect_last_attempt_at IS NULL AND %(after_attempt)s::timestamptz IS NULL
     AND (candidate.reconnect_requested_at,candidate.id)>(%(after_requested)s::timestamptz,%(after_id)s::uuid))
 OR (candidate.reconnect_last_attempt_at IS NOT NULL
     AND (%(after_attempt)s::timestamptz IS NULL
          OR (candidate.reconnect_last_attempt_at,candidate.reconnect_requested_at,candidate.id)>
             (%(after_attempt)s::timestamptz,%(after_requested)s::timestamptz,%(after_id)s::uuid))))"""
_CANDIDATE_PAGE = f"""WITH physical AS MATERIALIZED (
 SELECT candidate.id,candidate.reconnect_last_attempt_at,candidate.reconnect_requested_at
 FROM public.user_channel_restore_items candidate
 WHERE candidate.reconnect_state IN ('queued','waiting','running')
 AND candidate.reconnect_requested_at<=%(ceiling)s::timestamptz
 AND (candidate.reconnect_last_attempt_at IS NULL OR candidate.reconnect_last_attempt_at<=%(ceiling)s::timestamptz)
 AND {_PAGE_SEEK}
 ORDER BY {_FAIR_ORDER} LIMIT %(limit)s)
 SELECT physical.id::text AS id,physical.reconnect_last_attempt_at AS last_attempt_at,
 physical.reconnect_requested_at AS requested_at,({_ELIGIBLE}) AS eligible
 FROM physical JOIN public.user_channel_restore_items candidate ON candidate.id=physical.id
 ORDER BY physical.reconnect_last_attempt_at NULLS FIRST,physical.reconnect_requested_at,physical.id"""

_CLAIM = f"""UPDATE public.user_channel_restore_items p SET reconnect_state='running',
 reconnect_not_before=NULL,reconnect_error_code=NULL,reconnect_last_attempt_at=clock_timestamp(),
 reconnect_attempt_count=CASE WHEN p.reconnect_attempt_count<2147483647 THEN p.reconnect_attempt_count+1 ELSE p.reconnect_attempt_count END,
 reconnect_lease_token=gen_random_uuid(),reconnect_lease_expires_at=clock_timestamp()+make_interval(secs=>%(lease_seconds)s)
 WHERE p.id IN (
 SELECT candidate.id FROM public.user_channel_restore_items candidate
 WHERE {_ELIGIBLE} AND (%(identity)s::uuid IS NULL OR candidate.id=%(identity)s::uuid)
 ORDER BY {_FAIR_ORDER}
 FOR UPDATE OF candidate SKIP LOCKED LIMIT %(limit)s)
 RETURNING p.id::text AS id,p.revision,p.user_input,p.parent_ref::text AS parent_ref,
 p.reconnect_lease_token::text AS lease_token,
 to_jsonb(p.reconnect_lease_expires_at)#>>'{{}}' AS lease_expires_at,p.reconnect_attempt_count AS attempt_count"""
_FENCE = '''SELECT id::text AS id,revision,reconnect_revision,reconnect_state,is_hidden,
 reconnect_lease_token::text AS lease_token,reconnect_lease_expires_at>clock_timestamp() AS live
 FROM public.user_channel_restore_items WHERE id=%s::uuid FOR UPDATE NOWAIT'''
_RELEASE = '''UPDATE public.user_channel_restore_items
 SET reconnect_state=%s,reconnect_not_before=CASE WHEN %s::integer IS NULL THEN NULL
 ELSE clock_timestamp()+make_interval(secs=>%s::integer) END,
 reconnect_error_code=%s,reconnect_lease_token=NULL,reconnect_lease_expires_at=NULL
 WHERE id=%s::uuid AND revision=%s AND reconnect_revision=%s AND reconnect_state='running'
 AND reconnect_lease_token=%s::uuid AND reconnect_lease_expires_at>clock_timestamp()
 RETURNING id::text AS id'''
_CONTROL = '''SELECT c.initial_long_inventory_status,s.is_hidden,s.owner_permission,s.control_epoch
 FROM public.channels c JOIN public.channel_collection_state s USING(channel_id)
 WHERE c.channel_id=%s AND s.user_work_id=%s::uuid'''
_RETRY_REASONS = frozenset(('channel_add_rate_limited','channel_add_admission_unavailable',
 'provider_rate_limited','provider_unavailable','provider_quota_exceeded','channel_lookup_unavailable',
 'initial_inventory_count_unavailable','database_unavailable','parent_pending','busy'))
_BLOCK_REASONS = frozenset(('channel_not_found','needs_input','unsupported_legacy_channel_link',
 'provider_key_rejected','provider_access_denied'))


def _operation(name):
    return Operation(name, statement_timeout_ms=15000, lock_timeout_ms=1000,
                     total_timeout_ms=60000, error_codes=_ERRORS)


def _facts(tx, *, locked=False):
    values = tx.fetch_all(_LOCKED_FACTS if locked else _FACTS, max_rows=10001)
    if len(values) > LIMITS['channels']:
        raise UserWorkError('user_work_limit_exceeded', 413)
    return values


def reconnect_status(tx):
    """Read inside the caller's existing status snapshot; source-free summary."""
    values = _facts(tx)
    counts = {state: 0 for state in ('idle','queued','running','waiting','blocked')}
    hidden = 0
    for row in values:
        if row['is_hidden']:
            hidden += 1
        else:
            counts[row['reconnect_state']] += 1
    return {'snapshot': reconnect_snapshot(values), 'counts': counts, 'excluded_hidden': hidden}


def _enqueue_reconnect(database, body, *, item_id=None):
    with database.transaction(_operation('user_work_reconnect_enqueue')) as tx:
        if not tx.fetch_one(_LOCK)['locked']:
            return {'outcome': 'busy'}
        values = _facts(tx, locked=True)
        if item_id is None:
            selection = authorize_reconnect(values, body)
            identities = {identity for identity, _ in selection.items}
            excluded = selection.excluded_hidden
        else:
            identity = uid(item_id)
            if type(body) is not dict or set(body) != {'expected_revision'}:
                raise UserWorkError('invalid_user_work_request')
            expected = revision(body['expected_revision'])
            row = next((row for row in values if row['id'] == identity), None)
            if row is None:
                return {'outcome': 'not_found'}
            if row['revision'] != expected:
                return {'outcome': 'conflict'}
            if row['is_hidden']:
                return {'outcome': 'hidden'}
            if row['reconnect_state'] == 'blocked':
                if expected >= 9007199254740991:
                    return {'outcome': 'conflict'}
                retried = tx.returning_one(_RETRY_SINGLE, (identity,expected))
                if retried is None:
                    return {'outcome': 'needs_input'}
                row.update(retried)
                return {'outcome': 'ready', 'snapshot': reconnect_snapshot(values),
                        'queued_count': 1, 'blocked_count': 0, 'already_requested_count': 0, 'excluded_hidden': 0}
            identities, excluded = {identity}, 0
        fresh = [row['id'] for row in values if row['id'] in identities and row['reconnect_state'] == 'idle']
        changed = []
        for start in range(0, len(fresh), 500):
            changed.extend(tx.returning_all(_ENQUEUE, (fresh[start:start+500],), max_rows=500))
        blocked = sum(row['reconnect_state'] == 'blocked' for row in changed)
        return {'outcome': 'ready', 'snapshot': reconnect_snapshot(values),
                'queued_count': len(changed)-blocked, 'blocked_count': blocked,
                'already_requested_count': len(identities)-len(changed), 'excluded_hidden': excluded}


def enqueue_reconnect(database, body, *, item_id=None):
    try:
        return _enqueue_reconnect(database, body, item_id=item_id)
    except DatabaseServerError as error:
        if error.code == '55P03':
            return {'outcome': 'busy'}
        raise



def _canonical_cursor_clock(value):
    if type(value) is not str or len(value) != 32:
        raise ValueError('reconnect_candidate_cursor_invalid')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('reconnect_candidate_cursor_invalid') from None
    if (parsed.utcoffset() != timedelta(0)
            or parsed.astimezone(timezone.utc).isoformat(timespec='microseconds') != value):
        raise ValueError('reconnect_candidate_cursor_invalid')
    return value


def _database_clock(value):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() != timedelta(0)):
        raise RuntimeError('reconnect_candidate_clock_invalid')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


@dataclass(frozen=True)
class ReconnectCandidateCursor:
    """Internal advisory fair key, not a lease, API token or snapshot authority."""
    last_attempt_at: str | None
    requested_at: str
    id: str

    def __post_init__(self):
        if self.last_attempt_at is not None:
            _canonical_cursor_clock(self.last_attempt_at)
        _canonical_cursor_clock(self.requested_at)
        try:
            canonical = uid(self.id)
        except (SavedWorkError, TypeError, ValueError):
            raise ValueError('reconnect_candidate_cursor_invalid') from None
        if self.id != canonical:
            raise ValueError('reconnect_candidate_cursor_invalid')

    def order_key(self):
        # Canonical UTC clocks have one fixed precision/offset. Canonical UUID
        # bytes follow the same order as PostgreSQL uuid; NULL attempts go first.
        return (self.last_attempt_at is not None, self.last_attempt_at or '',
                self.requested_at, UUID(self.id).int)


def reconnect_candidates(database, *, limit=16):
    """At most 2C advisory identities (C<=50); no locks or leases are acquired."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('reconnect_candidates_invalid')
    operation = Operation('user_work_reconnect_candidates',read_only=True,
                          statement_timeout_ms=5000,lock_timeout_ms=1000,total_timeout_ms=15000)
    with database.transaction(operation) as tx:
        return tx.fetch_all(_CANDIDATES, {'limit':limit}, max_rows=limit)



def reconnect_candidate_page(database, *, limit=16, after=None, ceiling=None):
    """Bounded physical fair page before eligibility; no locks, leases or source data.

    Reuse the returned UTC ceiling/cursor through one sweep and advance after
    locked/empty exact claims. A wake can start a fresh sweep. This is advisory:
    concurrent changes are revalidated by the unchanged exact claim owner.
    """
    if (type(limit) is not int or not 1 <= limit <= 100
            or (after is not None and type(after) is not ReconnectCandidateCursor)
            or (after is not None and ceiling is None)):
        raise ValueError('reconnect_candidate_page_invalid')
    if ceiling is not None:
        _canonical_cursor_clock(ceiling)
        if after is not None and (after.requested_at > ceiling
                or (after.last_attempt_at is not None and after.last_attempt_at > ceiling)):
            raise ValueError('reconnect_candidate_page_invalid')
    operation = Operation('user_work_reconnect_candidate_page', read_only=True,
                          statement_timeout_ms=5000, lock_timeout_ms=1000, total_timeout_ms=15000)
    with database.transaction(operation) as tx:
        # Read the native clock on each bounded call; a submitted future ceiling
        # cannot admit work outside the current sweep's wall-time boundary.
        now = _database_clock(tx.fetch_one(_PAGE_CLOCK)['at'])
        if ceiling is None:
            ceiling = now
        elif ceiling > now:
            raise ValueError('reconnect_candidate_page_invalid')
        params = {'limit': limit, 'ceiling': ceiling,
                  'after_id': None if after is None else after.id,
                  'after_attempt': None if after is None else after.last_attempt_at,
                  'after_requested': None if after is None else after.requested_at}
        rows = tx.fetch_all(_CANDIDATE_PAGE, params, max_rows=limit)
        candidates, next_after = [], after
        for row in rows:
            cursor = ReconnectCandidateCursor(
                None if row['last_attempt_at'] is None else _database_clock(row['last_attempt_at']),
                _database_clock(row['requested_at']), row['id'])
            if (type(row['eligible']) is not bool or cursor.requested_at > ceiling
                    or (cursor.last_attempt_at is not None and cursor.last_attempt_at > ceiling)
                    or (next_after is not None and cursor.order_key() <= next_after.order_key())):
                raise RuntimeError('reconnect_candidate_page_unconfirmed')
            next_after = cursor
            if row['eligible']:
                candidates.append({'id': cursor.id})
        return {'candidates': candidates, 'next_after': next_after, 'ceiling': ceiling,
                'inspected': len(rows), 'exhausted': len(rows) < limit}


def claim_reconnect(database, *, limit=1, lease_seconds=180, identity=None):
    if type(limit) is not int or not 1 <= limit <= 50 or type(lease_seconds) is not int or not 30 <= lease_seconds <= 600:
        raise ValueError('reconnect_claim_invalid')
    identity = None if identity is None else uid(identity)
    if identity is not None and limit != 1:
        raise ValueError('reconnect_claim_invalid')
    with database.transaction(_operation('user_work_reconnect_claim')) as tx:
        # Short serialization with bulk/edit/bind; no lock survives this method.
        if not tx.fetch_one(_LOCK)['locked']:
            return []
        return tx.returning_all(_CLAIM, {'limit':limit,'lease_seconds':lease_seconds,'identity':identity}, max_rows=limit)


def defer_reconnect(database, identity, expected_revision, lease_token, *, reason, retry_seconds=None):
    identity, expected, token = uid(identity), revision(expected_revision), uid(lease_token)
    if retry_seconds is None:
        if reason not in _BLOCK_REASONS:
            raise ValueError('reconnect_defer_invalid')
        state = 'blocked'
    else:
        if reason not in _RETRY_REASONS or type(retry_seconds) is not int or not 1 <= retry_seconds <= 90000:
            raise ValueError('reconnect_defer_invalid')
        state = 'waiting'
    with database.transaction(_operation('user_work_reconnect_defer')) as tx:
        result = tx.returning_one(_RELEASE, (state,retry_seconds,retry_seconds,reason,identity,expected,expected,token))
        return {'outcome': 'ready' if result else 'lease_lost'}


def bind_reconnect(database, identity, expected_revision, lease_token, prepared_channel, *, max_attempts=3):
    identity, expected, token = uid(identity), revision(expected_revision), uid(lease_token)
    canonical = channel_id(prepared_channel['channel_id'])
    if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
        raise ValueError('reconnect_attempts_invalid')
    try:
        with database.transaction(_operation('user_work_reconnect_bind')) as tx:
            if not tx.fetch_one(_LOCK)['locked']:
                return {'outcome': 'busy'}
            item = tx.fetch_one(_FENCE, (identity,))
            if (item is None or item['is_hidden'] or item['revision'] != expected or item['reconnect_revision'] != expected
                    or item['reconnect_state'] != 'running' or item['lease_token'] != token or item['live'] is not True):
                return {'outcome': 'lease_lost'}
            bound = bind_user_channel_restore_item(database, identity=identity,expected_revision=expected,
                channel_id=canonical,channel_data=prepared_channel,transaction=tx,reconnect_lease_token=token)
            if bound.get('outcome') != 'ready':
                return bound
            result = {'outcome': 'ready', 'id': identity, 'channel_id': canonical, 'job_id': None,
                      'merged': bool(bound.get('merged')), 'manual_required': False}
            if bound.get('merged') or bound.get('replayed'):
                return result
            control = tx.fetch_one(_CONTROL, (canonical, identity))
            if control is None or control['is_hidden']:
                raise DatabaseServerError('channel_collection_scope_lost',code='P0001')
            if control['owner_permission'] == 'manual_required':
                result['manual_required'] = True
                return result
            if control['owner_permission'] != 'accepted' or control['initial_long_inventory_status'] != 'pending':
                raise DatabaseServerError('channel_collection_scope_lost',code='P0001')
            estimate = estimate_channel_initial_inventory_job(database, canonical, transaction=tx)
            enqueued = enqueue_channel_initial_inventory_job(database,canonical,
                estimate['estimated_items'],estimate['estimated_pages'],estimate['estimated_quota_units'],
                None,None,None,max_attempts=max_attempts,confirmed=False,expected_epoch=control['control_epoch'],transaction=tx)
            result['job_id'] = str(enqueued['job']['id'])
            return result
    except DatabaseServerError as error:
        if error.code == '55P03':
            return {'outcome': 'busy'}
        raise
