"""Bounded discovery obligations; provider I/O and video writes live elsewhere.

Exact identities are frozen before HTTP. A video owner's durable receipt, never
enqueue counts or a high-water event ID, permits ACK. Claim expiry only releases
the coordinator: it does not erase an in-flight video writer's binding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from .videos.video_initial_policy import PUBLICATION_EVIDENCE_REVIEW
from .base.postgres import DatabaseServerError, Operation
from .channel.channel_read_policy import CHANNEL_CONTROL_SQL

_TABLES = {'websub': ('websub_events', 'event_id', 'bigint'),
           'repair': ('discovery_repair_targets', 'target_id', 'uuid')}
_REASONS = frozenset(('metadata_not_returned', 'not_public_observed', 'membership_not_observed',
    'lifecycle_wait', 'format_off', 'quota', 'budget', 'owner_busy', 'held', 'needs_review', 'retry', 'channel_mismatch',
    'long_wait', 'availability_retry_expired'))


def operation(name):
    return Operation(name, statement_timeout_ms=5000, lock_timeout_ms=1000, total_timeout_ms=8000)


def fail(code):
    raise DatabaseServerError(code, code='P0001')


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        fail('discovery_identity_invalid')


def _claims(claims):
    if not isinstance(claims, (list, tuple)) or not 1 <= len(claims) <= 500:
        fail('discovery_batch_invalid')
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict) or claim.get('origin') not in _TABLES:
            fail('discovery_identity_invalid')
        key = (claim['origin'], str(claim.get('work_id')))
        if key in seen:
            fail('discovery_batch_duplicate')
        seen.add(key)
        _uuid(claim.get('claim_token'))
        if type(claim.get('claim_channel_epoch')) is not int or claim['claim_channel_epoch'] < 1:
            fail('discovery_identity_invalid')
    return sorted(claims, key=lambda c: (c['origin'], str(c['work_id'])))


def locked_claim(tx, claim, *, fresh=False):
    """Finite table vocabulary, exact primary key and claim/epoch fence."""
    table, key, cast = _TABLES[claim['origin']]
    row = tx.fetch_one(f'SELECT * FROM public.{table} WHERE {key}=%s::{cast} FOR UPDATE', (claim['work_id'],))
    if (row is None or row['status'] != 'pending' or str(row['claim_token']) != str(claim['claim_token'])
            or row['claim_channel_epoch'] != claim['claim_channel_epoch']):
        fail('discovery_claim_stale')
    current = tx.fetch_one('SELECT control_epoch,is_hidden FROM public.channel_collection_state WHERE channel_id=%s FOR SHARE',
                           (row['channel_id'],))
    if current is None or current['is_hidden'] or current['control_epoch'] != row['claim_channel_epoch']:
        fail('discovery_channel_scope_changed')
    if fresh and not tx.fetch_one('SELECT %s::timestamptz>clock_timestamp() AS live', (row['claim_expires_at'],))['live']:
        fail('discovery_claim_expired')
    return row


def _update(tx, claim, assignments, values):
    table, key, cast = _TABLES[claim['origin']]
    return tx.execute(f'UPDATE public.{table} SET {assignments} WHERE {key}=%s::{cast}', (*values, claim['work_id']))


def _clock_settings(settings, **legacy):
    """Use the captured policy when available. Individual clock settings supply defaults for callers.

    Rebuilding a full policy from a subset would compare custom review timing
    with an unrelated default retry ladder and reject valid installed profiles.
    """
    from discovery_policy import DiscoverySettings
    if settings is None:
        return DiscoverySettings(**legacy)
    if type(settings) is not DiscoverySettings:
        fail('discovery_settings_required')
    return settings


def _review_due_at(clock, observed, next_try_at, reason, settings):
    """Slow unresolved work without advancing the canonical owner's deadline.

    A currently confirmed future publication keeps its lifecycle cadence; an
    unknown/past end does not exempt old work from the review interval.
    """
    from discovery_policy import needs_review
    due = next_try_at if next_try_at is not None else observed
    if reason not in ('metadata_not_returned', 'not_public_observed',
                      'membership_not_observed', 'retry', 'lifecycle_wait'):
        return due
    if reason != 'membership_not_observed' and not needs_review(clock, observed, settings):
        return due
    if (reason == 'lifecycle_wait' and clock.future_wait_until is not None
            and observed < clock.future_wait_until):
        return due
    return max(due, observed + timedelta(seconds=settings.review_retry_seconds))


class DiscoveryRepository:
    def __init__(self, database):
        self.database = database

    def enabled(self):
        from .discovery_runtime import runtime_enabled
        return runtime_enabled(self.database)

    def runtime_status(self, **options):
        from .discovery_runtime import runtime_status
        return runtime_status(self.database, **options)

    def observe_head(self, claims, **arguments):
        from .inventory.inventory_heads import observe_notification_head
        return observe_notification_head(self.database, claims, **arguments)

    def record_membership(self, claims, **arguments):
        from .inventory.addressed_membership import record_addressed_membership
        return record_addressed_membership(self.database,claims,**arguments)

    def create_repair_campaign(self, campaign_id, **limits):
        from .discovery_repair import create_campaign
        return create_campaign(self.database,campaign_id,**limits)

    def settle_disabled(self, claims, *, enabled_types):
        from .discovery_disabled import settle_disabled
        return settle_disabled(self.database,claims,enabled_types=enabled_types)

    def terminal_observation(self, claims, outcome, **arguments):
        from .discovery_observations import terminal_observation
        return terminal_observation(self.database, claims, outcome, **arguments)

    def pause(self, claims, reason):
        from .discovery_observations import pause
        return pause(self.database, claims, reason)

    def resume(self, channel_id, video_ids):
        from .discovery_observations import resume
        return resume(self.database, channel_id, video_ids)

    def activate(self, *, prepared_host=None, video_types=('long', 'short'), settings=None):
        """Only an explicitly prepared host may cross the activation gate."""
        from .discovery_activation import activate
        return activate(self.database, prepared_host=prepared_host, video_types=video_types, settings=settings)

    def claim_event_batch(self, limit=100, lease_seconds=120, *, origin='websub', needs_review_seconds=604800,
                          unresolved_pause_seconds=2592000, settings=None):
        if origin not in _TABLES or type(limit) is not int or not 1 <= limit <= 500 or type(lease_seconds) is not int or not 10 <= lease_seconds <= 3600:
            fail('discovery_claim_invalid')
        table, key, _ = _TABLES[origin]
        from discovery_policy import WaitClock,needs_review,unresolved_status
        settings=_clock_settings(settings,needs_review_seconds=needs_review_seconds,unresolved_pause_seconds=unresolved_pause_seconds)
        with self.database.transaction(operation('discovery_claim')) as tx:
            active = tx.fetch_one('SELECT enabled FROM public.discovery_runtime_control WHERE singleton')
            if not active or not active['enabled']:
                return []
            extra=''
            if origin=='repair':
                from .discovery_repair import coalesce_event_owner,refresh_campaign_states
                # A resume token must reopen the same campaign before the
                # pending-state filter, without recreating its allowance.
                refresh_campaign_states(tx)
                coalesce_event_owner(tx)
                extra=""" AND EXISTS(SELECT 1 FROM public.discovery_repair_campaigns campaign
                    WHERE campaign.campaign_id=e.campaign_id AND campaign.state='pending')
                    AND (e.work_phase='owner_wait' OR NOT EXISTS(SELECT 1 FROM public.websub_events live
                      WHERE live.channel_id=e.channel_id AND live.video_id=e.video_id AND live.status='pending'
                        AND (live.paused_at IS NULL OR live.resume_token IS DISTINCT FROM live.resume_served_token
                             OR live.work_phase='owner_wait'
                             OR (live.claim_token IS NOT NULL AND live.claim_expires_at>clock_timestamp()))))"""
            select=f'''SELECT e.*,c.control_epoch FROM public.{table} e
                JOIN public.channel_collection_state c USING(channel_id)
                WHERE e.status='pending' AND e.work_phase<>'settled' AND e.next_try_at<=clock_timestamp()
                AND (e.claim_token IS NULL OR e.claim_expires_at<=clock_timestamp())
                AND coalesce((SELECT p.typed_runtime_eligible FROM (''' + CHANNEL_CONTROL_SQL + ''') p
                    WHERE p.channel_id=e.channel_id),false)''' + extra
            # Separate predicates match the active/resume partial indexes.
            # Each arm locks at most limit rows; only the globally earliest
            # limit receive leases. Dormant paused rows are in neither arm.
            order=' ORDER BY e.next_try_at,e.'+key+' FOR UPDATE OF e SKIP LOCKED LIMIT %s'
            rows=tx.fetch_all('WITH active AS MATERIALIZED ('+select+' AND e.paused_at IS NULL'+order+'),'
                ' resumed AS MATERIALIZED ('+select+' AND e.paused_at IS NOT NULL'
                ' AND e.resume_token IS DISTINCT FROM e.resume_served_token'+order+')'
                ' SELECT * FROM active UNION ALL SELECT * FROM resumed ORDER BY next_try_at,'+key+' LIMIT %s',
                (limit,limit,limit),max_rows=500)
            result = []
            observed=tx.fetch_one('SELECT clock_timestamp() AS at')['at']
            for row in rows:
                clock=WaitClock(row['received_at'],row['unresolved_since'] or row['received_at'],
                    row['future_wait_started_at'],row['future_wait_until'])
                # A requested resume remains outstanding until a meaningful
                # result. A budget/transport refusal must not consume it.
                resumed = row['resume_token'] != row['resume_served_token']
                if (row['work_phase'] != 'owner_wait' and row['wait_reason'] != 'membership_not_observed'
                        and not resumed and unresolved_status(clock,observed,settings)=='paused'):
                    tx.execute(f'''UPDATE public.{table} SET paused_at=coalesce(paused_at,clock_timestamp()),
                        pause_reason=coalesce(pause_reason,'long_wait'),wait_reason=coalesce(pause_reason,'long_wait'),
                        review_required=true,claim_token=NULL,claim_expires_at=NULL WHERE {key}=%s''', (row[key],))
                    continue
                # A stale owner binding cannot be applied to a new channel epoch.
                if row['work_phase'] == 'owner_wait' and row['claim_channel_epoch'] != row['control_epoch']:
                    tx.execute(f'''UPDATE public.{table} SET work_phase='metadata_wait',
                        handoff_owner_kind=NULL,handoff_owner_id=NULL,handoff_attempt_id=NULL,
                        handoff_lease_token=NULL,handoff_at=NULL,membership_receipt_id=NULL
                        WHERE {key}=%s''', (row[key],))
                review=row['wait_reason'] in ('membership_not_observed','needs_review') or needs_review(clock,observed,settings)
                updated = tx.returning_one(f'''UPDATE public.{table} SET claim_token=gen_random_uuid(),
                    claim_expires_at=clock_timestamp()+make_interval(secs=>%s),
                    claim_channel_epoch=%s,unresolved_since=coalesce(unresolved_since,received_at),review_required=%s
                    WHERE {key}=%s RETURNING *''', (lease_seconds, row['control_epoch'], review, row[key]))
                result.append({**updated, 'origin': origin, 'work_id': str(row[key])})
        return result

    def start_request(self, claims, request_id, source_started_at):
        claims = _claims(claims)
        request_id = _uuid(request_id)
        with self.database.transaction(operation('discovery_start_request')) as tx:
            if not tx.fetch_one('SELECT isfinite(%s::timestamptz) AND %s::timestamptz BETWEEN clock_timestamp()-interval \'5 minutes\' AND clock_timestamp() AS valid',
                    (source_started_at, source_started_at))['valid']:
                fail('discovery_source_clock_invalid')
            for claim in claims:
                row = locked_claim(tx, claim, fresh=True)
                if row['work_phase'] == 'owner_wait':
                    fail('discovery_owner_unsettled')
                if str(row['source_request_id']) == request_id:
                    if row['source_request_started_at'] != source_started_at:
                        fail('discovery_request_conflict')
                    continue
                if row['source_request_started_at'] and row['source_request_started_at'] > source_started_at:
                    fail('discovery_source_clock_regression')
                _update(tx, claim, 'source_request_id=%s,source_request_started_at=%s',
                        (request_id, source_started_at))
        return [c['work_id'] for c in claims]

    def record_metadata_result(self, claims, request_id, observed_at=None):
        """One accepted metadata round, not one increment per playlist request."""
        with self.database.transaction(operation('discovery_metadata_result')) as tx:
            for claim in _claims(claims):
                row=locked_claim(tx,claim)
                if str(row['source_request_id'])!=str(request_id):
                    fail('discovery_request_stale')
                at=observed_at or row['source_request_started_at']
                if not tx.fetch_one('SELECT isfinite(%s::timestamptz) AND %s::timestamptz BETWEEN %s::timestamptz AND clock_timestamp() AS valid',
                    (at,at,row['source_request_started_at']))['valid']:
                    fail('discovery_source_clock_invalid')
                if str(row['discovery_attempt_request_id'])!=str(request_id):
                    _update(tx,claim,'discovery_attempts=discovery_attempts+1,discovery_attempt_request_id=%s::uuid,'
                        'metadata_source_checked_at=%s,technical_metadata_failures=0',(request_id,at))
                elif row['metadata_source_checked_at']!=at:
                    fail('discovery_request_conflict')

    complete_attempt = record_metadata_result

    def record_membership_check(self, claims, request_id, observed_at):
        """Reset membership faults only after the whole membership stage."""
        from .discovery_observations import request_proof
        with self.database.transaction(operation('discovery_membership_result')) as tx:
            for claim in _claims(claims):
                row=locked_claim(tx,claim)
                request_proof(tx,row,request_id,observed_at)
                _update(tx,claim,'technical_membership_failures=0',())

    def mark_disabled_probe(self, claims, request_id, observed_at):
        from .discovery_observations import request_proof
        with self.database.transaction(operation('discovery_disabled_probe')) as tx:
            for claim in _claims(claims):
                row=locked_claim(tx,claim)
                request_proof(tx,row,request_id,observed_at)
                _update(tx,claim,'disabled_stream_probe_at=%s',(observed_at,))

    def resolve_collected(self, claims, *, enabled_types, queue_if_stale=False, settings=None):
        """Close exact notifications already covered by the canonical video writer."""
        settled = []
        with self.database.transaction(operation('discovery_current_video')) as tx:
            for claim in sorted(_claims(claims), key=lambda c: c.get('video_id', '')):
                row = locked_claim(tx, claim, fresh=True)
                if row['work_phase'] == 'owner_wait':
                    continue
                if row['kind']=='deleted' and tx.fetch_one(
                        'SELECT video_id FROM public.video_availability_incidents WHERE video_id=%s AND channel_id=%s '
                        'AND restored_at IS NULL FOR SHARE', (row['video_id'],row['channel_id'])):
                    _update(tx, claim, "status='done',outcome='confirmed_unavailable',review_required=false,processed_at=clock_timestamp(),"
                        "work_phase='settled',wait_reason=NULL,claim_token=NULL,claim_expires_at=NULL", ())
                    settled.append(claim['work_id'])
                    continue
                # Lock the writer before taking the read snapshot. An ordinary
                # completion racing this notification can already have set the next due.
                owner = None
                if queue_if_stale:
                    if claim['origin'] != 'websub' or row['membership_receipt_id'] is not None:
                        fail('discovery_request_stale')
                    owner = tx.fetch_one('SELECT paused_reason,last_error_type FROM public.video_collection_state '
                                         'WHERE video_id=%s AND channel_id=%s FOR UPDATE',
                                         (row['video_id'], row['channel_id']))
                current = tx.fetch_one('''SELECT
                    EXISTS(SELECT 1 FROM public.open_video_availability a
                           WHERE a.video_id=q.video_id AND a.channel_id=q.channel_id) AS unavailable,
                    ((NOT q.hydration_needed AND v.publication_status IS NULL
                      AND v.availability_status IN ('public','unlisted')) OR
                     (%s AND q.typed_collection_status='awaiting_completion'
                      AND v.publication_status IN ('upcoming','live','awaiting_completion')
                      AND q.typed_collection_checked_at=q.metadata_source_checked_at
                      AND q.next_due_at IS NOT NULL AND q.metadata_due_at IS NOT NULL
                      AND q.paused_reason IS NULL AND (q.last_error_type IS NULL OR q.last_error_type=%s)
                      AND v.availability_status IN ('public','unlisted','private')))
                    AND q.metadata_source_checked_at>=greatest(%s::timestamptz,%s::timestamptz)
                    AND v.metadata_checked_at>=greatest(%s::timestamptz,%s::timestamptz)
                    AND q.availability_first_missing_at IS NULL
                    AND q.availability_resume_observed_at IS NULL AS fresh
                    FROM public.video_collection_state q JOIN public.video_inventory i USING(video_id,channel_id)
                    LEFT JOIN public.videos v USING(video_id,channel_id)
                    WHERE q.video_id=%s AND q.channel_id=%s AND q.video_type=i.video_type
                    AND q.video_type=ANY(%s::text[]) FOR SHARE OF q''',
                    (queue_if_stale,PUBLICATION_EVIDENCE_REVIEW,row['received_at'],row['metadata_source_checked_at'],row['received_at'],
                     row['metadata_source_checked_at'],row['video_id'],row['channel_id'],list(enabled_types)))
                if current and current['fresh'] and not current['unavailable']:
                    _update(tx, claim, "status='done',outcome='current_metadata',review_required=false,processed_at=clock_timestamp(),"
                        "work_phase='settled',wait_reason=NULL,claim_token=NULL,claim_expires_at=NULL", ())
                    settled.append(claim['work_id'])
                    continue
                if not queue_if_stale:
                    continue
                if owner and (owner['paused_reason'] or owner['last_error_type'] not in (None, PUBLICATION_EVIDENCE_REVIEW)):
                    _update(tx, claim, "wait_reason='needs_review',review_required=true,"
                        "next_try_at=clock_timestamp()+make_interval(secs=>%s),resume_served_token=resume_token,"
                        "claim_token=NULL,claim_expires_at=NULL", (_clock_settings(settings).review_retry_seconds,))
                    continue
                if current and not current['unavailable']:
                    tx.execute('''UPDATE public.video_collection_state q
                        SET metadata_due_at=least(q.metadata_due_at,now()),next_due_at=least(q.next_due_at,now())
                        WHERE q.video_id=%s AND q.channel_id=%s AND q.video_type=ANY(%s::text[])
                        AND q.paused_reason IS NULL AND (q.last_error_type IS NULL OR q.last_error_type=%s)
                        AND q.availability_first_missing_at IS NULL AND q.availability_resume_observed_at IS NULL
                        AND (q.lease_token IS NULL OR q.lease_expires_at<=now())''',
                        (row['video_id'],row['channel_id'],list(enabled_types),PUBLICATION_EVIDENCE_REVIEW))
                _update(tx, claim, "wait_reason='owner_busy',next_try_at=clock_timestamp()+interval '10 minutes',"
                    "claim_token=NULL,claim_expires_at=NULL", ())
        return settled

    def target_state(self, claims):
        from .videos.video_initial_policy import VIDEO_INITIAL_PENDING_STATE_SQL
        result={}
        with self.database.transaction(operation('discovery_target_state')) as tx:
            for claim in _claims(claims):
                row=locked_claim(tx,claim)
                target=tx.fetch_one('''SELECT i.video_type,i.channel_id,q.video_id IS NOT NULL AS queue_exists,
                    q.lease_token,q.lease_expires_at,q.next_due_at,q.paused_reason,
                    q.last_error_type,q.availability_first_missing_at,q.availability_resume_observed_at,
                    (''' + VIDEO_INITIAL_PENDING_STATE_SQL.replace('state.','q.') + ''') AS initial_pending,
                    v.availability_status,v.metadata_checked_at,v.availability_checked_at,
                    greatest(v.checked_at,v.availability_checked_at,v.metadata_checked_at,q.typed_collection_checked_at) AS last_observation
                    FROM public.video_inventory i LEFT JOIN public.video_collection_state q USING(video_id)
                    LEFT JOIN public.videos v USING(video_id) WHERE i.video_id=%s''',(row['video_id'],))
                result[claim['work_id']]={**(target or {'queue_exists':False,'video_type':None}),
                    'channel_mismatch':target is not None and target['channel_id']!=row['channel_id']}
        return result

    def bind_owner(self, claims, attempts):
        """attempts maps video ID to its already acquired lease + planned UUID."""
        claims = _claims(claims)
        with self.database.transaction(operation('discovery_bind_owner')) as tx:
            for claim in claims:
                row = locked_claim(tx, claim, fresh=True)
                value = attempts.get(row['video_id'])
                if not isinstance(value, dict):
                    fail('discovery_owner_binding_invalid')
                lease, attempt = _uuid(value.get('lease_token')), _uuid(value.get('attempt_id'))
                source_id=value.get('source_request_id');source_at=value.get('source_checked_at')
                if ((source_id is None)!=(source_at is None) or (source_id is not None and
                    (str(row['discovery_attempt_request_id'])!=str(source_id) or row['metadata_source_checked_at']!=source_at))):
                    fail('discovery_owner_source_mismatch')
                if row['work_phase'] == 'owner_wait':
                    if str(row['handoff_attempt_id']) == attempt and str(row['handoff_lease_token']) == lease:
                        continue
                    fail('discovery_owner_unsettled')
                owner = tx.fetch_one('SELECT video_id FROM public.video_collection_state WHERE video_id=%s AND channel_id=%s '
                    'AND lease_token=%s::uuid AND lease_channel_epoch=%s AND lease_expires_at>clock_timestamp() FOR SHARE',
                    (row['video_id'], row['channel_id'], lease, row['claim_channel_epoch']))
                if owner is None:
                    fail('discovery_owner_binding_invalid')
                _update(tx, claim, "work_phase='owner_wait',handoff_owner_kind='video_collection_state',handoff_owner_id=video_id,"
                    'handoff_attempt_id=%s,handoff_lease_token=%s,handoff_at=clock_timestamp(),wait_reason=NULL,'
                    'handoff_source_request_id=%s::uuid,handoff_source_checked_at=%s', (attempt, lease,source_id,source_at))
        return [c['work_id'] for c in claims]

    def acknowledge(self, claims):
        claims = _claims(claims)
        settled = []
        with self.database.transaction(operation('discovery_acknowledge')) as tx:
            for claim in claims:
                row = locked_claim(tx, claim)
                if row['work_phase'] != 'owner_wait':
                    continue
                receipt = tx.fetch_one('''SELECT EXISTS(
                    SELECT 1 FROM public.typed_video_collection_receipts r WHERE r.video_id=%s
                      AND r.channel_id=%s AND r.collection_attempt_id=%s::uuid AND r.lease_token=%s::uuid
                      AND r.receipt_kind IN ('metadata','missing')
                    UNION ALL SELECT 1 FROM public.video_snapshots s WHERE s.video_id=%s
                      AND s.collection_attempt_id=%s::uuid) AS present''',
                    (row['video_id'], row['channel_id'], row['handoff_attempt_id'], row['handoff_lease_token'],
                     row['video_id'], row['handoff_attempt_id']))
                if not receipt['present']:
                    continue
                _update(tx, claim, "status='done',outcome='writer_receipt',processed_at=clock_timestamp(),"
                    "work_phase='settled',wait_reason=NULL,claim_token=NULL,claim_expires_at=NULL", ())
                settled.append(claim['work_id'])
        return settled

    def release_failed_owner(self, claims, *, needs_review_seconds=604800,
                             unresolved_pause_seconds=2592000,review_retry_seconds=86400,settings=None):
        """Reconcile success/failure/abandoned ownership under the queue lock.

        A lock plus rechecking receipts resolves unknown COMMIT. An expired
        matching lease is fenced before retry, so its old writer cannot commit.
        Pending lifecycle and identity probes are observations, not final ACKs.
        """
        from discovery_policy import WaitClock,advance_wait_clock,needs_review
        settings=_clock_settings(settings,needs_review_seconds=needs_review_seconds,
            unresolved_pause_seconds=unresolved_pause_seconds,review_retry_seconds=review_retry_seconds)
        released = []
        with self.database.transaction(operation('discovery_owner_failure')) as tx:
            for claim in _claims(claims):
                row = locked_claim(tx, claim)
                if row['work_phase'] != 'owner_wait':
                    continue
                queue = tx.fetch_one('''SELECT next_due_at,last_failure_attempt_id,lease_token,
                    lease_expires_at>clock_timestamp() AS live FROM public.video_collection_state
                    WHERE video_id=%s AND channel_id=%s FOR UPDATE''',(row['video_id'],row['channel_id']))
                if queue is None or (str(queue['lease_token'])==str(row['handoff_lease_token']) and queue['live']):
                    continue
                receipt = tx.fetch_one('''SELECT receipt_kind,recorded_at FROM public.typed_video_collection_receipts
                    WHERE video_id=%s AND channel_id=%s AND collection_attempt_id=%s::uuid AND lease_token=%s::uuid
                    UNION ALL SELECT 'metadata',recorded_at FROM public.video_snapshots WHERE video_id=%s
                    AND collection_attempt_id=%s::uuid LIMIT 1''',
                    (row['video_id'],row['channel_id'],row['handoff_attempt_id'],row['handoff_lease_token'],
                     row['video_id'],row['handoff_attempt_id']))
                if receipt and receipt['receipt_kind'] in ('metadata','missing'):
                    # Keep the binding for acknowledge(); never restart a writer.
                    continue
                if str(queue['lease_token'])==str(row['handoff_lease_token']):
                    tx.execute('''UPDATE public.video_collection_state SET lease_token=NULL,lease_channel_epoch=NULL,
                        lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL WHERE video_id=%s
                        AND lease_token=%s::uuid''',(row['video_id'],row['handoff_lease_token']))
                reason='lifecycle_wait' if receipt and receipt['receipt_kind']=='stream_probe' else 'retry'
                clock=WaitClock(row['received_at'],row['unresolved_since'] or row['received_at'],
                    row['future_wait_started_at'],row['future_wait_until'])
                if reason=='lifecycle_wait':
                    # Only the exact writer's retained current observation can
                    # establish a pause. A newer writer must not be attributed
                    # to this binding, and a transport failure proves nothing.
                    lifecycle=tx.fetch_one('''SELECT scheduled_start_at,publication_status FROM public.videos
                        WHERE video_id=%s AND channel_id=%s AND metadata_checked_at=%s''',
                        (row['video_id'],row['channel_id'],receipt['recorded_at']))
                    if lifecycle and receipt['recorded_at']>=max(clock.unresolved_since,
                            clock.future_wait_started_at or clock.unresolved_since):
                        clock=advance_wait_clock(clock,observed_at=receipt['recorded_at'],
                            scheduled_start_at=lifecycle['scheduled_start_at']
                            if lifecycle['publication_status']=='upcoming' else None)
                observed=tx.fetch_one('SELECT clock_timestamp() AS at')['at']
                review=needs_review(clock,observed,settings)
                due=_review_due_at(clock,observed,queue['next_due_at'],reason,settings)
                _update(tx, claim, "work_phase='metadata_wait',wait_reason=%s,next_try_at=%s,"
                    'handoff_owner_kind=NULL,handoff_owner_id=NULL,handoff_attempt_id=NULL,handoff_lease_token=NULL,'
                    'handoff_at=NULL,handoff_source_request_id=NULL,handoff_source_checked_at=NULL,'
                    'unresolved_since=%s,future_wait_started_at=%s,future_wait_until=%s,review_required=%s,'
                    'claim_token=NULL,claim_expires_at=NULL', (reason,due,clock.unresolved_since,
                        clock.future_wait_started_at,clock.future_wait_until,review))
                released.append(claim['work_id'])
        return released

    def defer(self, claims, reason, next_try_at, *, request_id=None, future_until=None,
              lifecycle_observed=False, needs_review_seconds=604800, unresolved_pause_seconds=2592000,
              review_retry_seconds=86400, technical_phase=None, settings=None):
        claims = _claims(claims)
        if reason not in _REASONS:
            fail('discovery_wait_reason_invalid')
        from discovery_policy import WaitClock,advance_wait_clock,needs_review,unresolved_status
        settings=_clock_settings(settings,needs_review_seconds=needs_review_seconds,
            unresolved_pause_seconds=unresolved_pause_seconds,review_retry_seconds=review_retry_seconds)
        if technical_phase not in (None,'metadata','membership') or (technical_phase is not None and reason!='retry'):
            fail('discovery_technical_phase_invalid')
        if type(lifecycle_observed) is not bool or (future_until is not None and not lifecycle_observed):
            fail('discovery_lifecycle_observation_required')
        with self.database.transaction(operation('discovery_defer')) as tx:
            observed = tx.fetch_one('SELECT clock_timestamp() AS at')['at']
            for claim in claims:
                row = locked_claim(tx, claim)
                if request_id is not None and str(row['source_request_id']) != str(request_id):
                    fail('discovery_request_stale')
                if technical_phase is not None:
                    failure_id=_uuid(request_id or claim['claim_token'])
                    if (str(row['technical_failure_request_id'])!=failure_id
                            or row['technical_failure_phase']!=technical_phase):
                        column='technical_'+technical_phase+'_failures'
                        _update(tx,claim,f'{column}=least({column},2147483646)+1,'
                            'technical_failure_request_id=%s::uuid,technical_failure_phase=%s',
                            (failure_id,technical_phase))
                clock=WaitClock(row['received_at'],row['unresolved_since'] or row['received_at'],
                    row['future_wait_started_at'],row['future_wait_until'])
                if lifecycle_observed:
                    if row['metadata_source_checked_at'] is None:
                        fail('discovery_lifecycle_observation_required')
                    clock=advance_wait_clock(clock,observed_at=row['metadata_source_checked_at'],
                        scheduled_start_at=future_until)
                review=reason=='membership_not_observed' or needs_review(clock,observed,settings)
                status=unresolved_status(clock,observed,settings)
                due=_review_due_at(clock,observed,next_try_at,reason,settings)
                if row['work_phase'] == 'owner_wait':
                    # A failure/unknown commit still belongs to that exact writer.
                    _update(tx, claim, 'wait_reason=%s,next_try_at=%s,review_required=%s,claim_token=NULL,claim_expires_at=NULL',
                        (reason,due,review))
                    continue
                consume=reason not in ('retry','budget','quota','held','owner_busy')
                _update(tx, claim, 'wait_reason=%s,next_try_at=%s,unresolved_since=%s,future_wait_started_at=%s,'
                    'future_wait_until=%s,review_required=%s,claim_token=NULL,claim_expires_at=NULL,'
                    "paused_at=CASE WHEN %s THEN coalesce(paused_at,clock_timestamp()) ELSE paused_at END,"
                    "pause_reason=CASE WHEN %s THEN coalesce(pause_reason,'long_wait') ELSE pause_reason END,"
                    'resume_served_token=CASE WHEN %s THEN resume_token ELSE resume_served_token END',
                    (reason,due,clock.unresolved_since,clock.future_wait_started_at,clock.future_wait_until,review,
                     status=='paused' and reason!='membership_not_observed',
                     status=='paused' and reason!='membership_not_observed',consume))
        return len(claims)
