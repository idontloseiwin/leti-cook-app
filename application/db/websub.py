"""WebSub subscriptions, durable event inbox and the Hub journal.

Ingress stores accepted envelopes without acknowledging discovery or making
provider requests. Until the discovery runtime is enabled, the backend event
handler below gives them their earlier effect (existing playlist or video work
becomes due) and closes them; once it is enabled, the collector discovery owner
consumes them instead. Exactly one of the two runs at any time. Subscription
management and aggregate Health remain independent observers.
"""
from __future__ import annotations

import random

from .base.postgres import DatabaseError, Operation
from .channel.channel_read_policy import CHANNEL_CONTROL_SQL

TOPIC_PREFIX = 'https://www.youtube.com/xml/feeds/videos.xml?channel_id='
MAX_ATTEMPTS = 5
RETRY_AFTER_NO_VERIFICATION = 1800      # an accepted or unanswered request is re-asked only after this
HOLD_FLOOR_SECONDS = 120                # Hub-wide pause after 429, 503 or no answer
BREAKER_FAILURES = 5                    # failures in a row that stop sending until reviewed
RAMP = ((1, 1), (10, 8))                # (requests sent, verified needed) before new channels go on
BREAKER_RETRY_SECONDS = 21_600          # an open breaker allows one request after this long
JOURNAL_DAYS = 90
FORGET_UNSUBSCRIBED_DAYS = 30            # a closed subscription row is kept this long for the journal
FALLBACK_LEASE_SECONDS = 432_000        # when a verification GET omits hub.lease_seconds
PENDING_DELTA_HOURS = 24                # an unmatched announcement stops explaining count changes


def _op(name, *, read_only=False, statement_ms=5000, lock_ms=1000, total_ms=8000):
    return Operation(name, read_only=read_only, statement_timeout_ms=statement_ms,
                     lock_timeout_ms=0 if read_only else lock_ms, total_timeout_ms=total_ms)


# ---------------------------------------------------------------- ingress

_SUBSCRIPTION = """SELECT channel_id,generation,topic,state FROM public.websub_subscriptions WHERE channel_id=%s"""
_VERIFIED = """UPDATE public.websub_subscriptions SET state='verified',verified_at=clock_timestamp(),
 lease_seconds=%(lease)s,expires_at=clock_timestamp()+make_interval(secs=>%(lease)s),
 renew_at=clock_timestamp()+make_interval(secs=>%(renew)s),attempts=0,last_outcome='verified',updated_at=clock_timestamp()
 WHERE channel_id=%(channel)s AND generation=%(generation)s AND state IN ('pending','verified')"""
_UNSUBSCRIBED = """UPDATE public.websub_subscriptions SET state='unsubscribed',expires_at=NULL,renew_at=NULL,
 last_outcome='unsubscribed',updated_at=clock_timestamp()
 WHERE channel_id=%(channel)s AND generation=%(generation)s AND state='unsubscribing'"""
_DENIED = """UPDATE public.websub_subscriptions SET state='denied',expires_at=NULL,renew_at=NULL,
 next_try_at=clock_timestamp()+interval '24 hours',last_outcome='denied',updated_at=clock_timestamp()
 WHERE channel_id=%(channel)s AND generation=%(generation)s AND state IN ('pending','verified')"""
_EVENTS = """INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind,provider_published_at,provider_updated_at)
 SELECT * FROM unnest(%s::text[],%s::text[],%s::text[],%s::text[],%s::timestamptz[],%s::timestamptz[])
 ON CONFLICT(dedupe_key) DO NOTHING RETURNING event_id,channel_id,video_id"""
_JOURNAL = """INSERT INTO public.websub_journal(kind,channel_id,mode,outcome,http_status,seconds,retry_after,lease_seconds,bytes,events)
 VALUES(%(kind)s,%(channel)s,%(mode)s,%(outcome)s,%(http_status)s,%(seconds)s,%(retry_after)s,%(lease)s,%(bytes)s,%(events)s)"""


def _journal_row(kind, outcome, *, channel=None, mode=None, http_status=None, seconds=None,
                 retry_after=None, lease=None, size=None, events=None):
    return dict(kind=kind, channel=channel, mode=mode, outcome=outcome[:40], http_status=http_status,
                seconds=None if seconds is None else round(float(seconds), 3), retry_after=retry_after,
                lease=lease, bytes=size, events=events)


class WebSubRepository:
    def __init__(self, database, *, rng=random.random):
        self.database = database
        self.rng = rng

    # ------------------------------------------------------------ ingress

    def subscription(self, channel_id):
        return self.database.read_row('websub_subscription', _SUBSCRIPTION, (channel_id,))

    def verification(self, channel_id, generation, mode, lease_seconds, *, outcome):
        """Apply one verified Hub GET (or record a refused one) atomically with its journal row."""
        with self.database.transaction(_op('websub_verification')) as tx:
            changed = 0
            if outcome == 'verified' and mode == 'subscribe':
                lease = lease_seconds if lease_seconds and lease_seconds > 0 else FALLBACK_LEASE_SECONDS
                renew = max(60, int(lease * (0.70 + 0.10 * self.rng())))
                changed = tx.execute(_VERIFIED, dict(lease=lease, renew=renew, channel=channel_id, generation=generation))
            elif outcome == 'verified' and mode == 'unsubscribe':
                changed = tx.execute(_UNSUBSCRIBED, dict(channel=channel_id, generation=generation))
            elif outcome == 'denied':
                changed = tx.execute(_DENIED, dict(channel=channel_id, generation=generation))
            if outcome in ('verified', 'denied') and changed != 1:
                outcome = 'state_mismatch'
            tx.execute(_JOURNAL, _journal_row('verification', outcome, channel=channel_id, mode=mode,
                                              lease=lease_seconds))
            return outcome

    def delivery(self, channel_id, notifications, *, size, outcome='accepted'):
        """Durably store accepted events (duplicates ignored) with one journal row; returns inserted count."""
        rows = []
        with self.database.transaction(_op('websub_delivery')) as tx:
            inserted = 0
            if notifications:
                rows = tx.returning_all(_EVENTS, (
                    [n.dedupe_key for n in notifications], [n.channel_id for n in notifications],
                    [n.video_id for n in notifications], [n.kind for n in notifications],
                    [n.published_at for n in notifications], [n.updated_at for n in notifications]), max_rows=100)
                inserted = len(rows)
                if rows:
                    # Only newly accepted envelopes may resume old paused work;
                    # redelivery of the same dedupe key never refills a retry.
                    from .discovery_observations import resume_matching
                    from collections import defaultdict
                    by_channel=defaultdict(list)
                    for row in rows:
                        by_channel[row['channel_id']].append(row['video_id'])
                    for own_channel,ids in by_channel.items():
                        for offset in range(0,len(ids),50):
                            resume_matching(tx,own_channel,ids[offset:offset+50])
            tx.execute(_JOURNAL, _journal_row('delivery', outcome, channel=channel_id, size=size,
                                              events=len(notifications)))
        import support_journal
        for row in rows:
            support_journal.event('signal', 'accepted', video_id=row['video_id'], channel_id=row['channel_id'],
                                  origin='websub', work_id=str(row['event_id']), reason='websub')
        return inserted

    def journal(self, kind, outcome, **values):
        with self.database.transaction(_op('websub_journal')) as tx:
            tx.execute(_JOURNAL, _journal_row(kind, outcome, **values))


# ---------------------------------------------------------------- subscription manager

_DESIRED = ("SELECT policy.channel_id FROM (" + CHANNEL_CONTROL_SQL + ") policy "
            "WHERE policy.typed_runtime_eligible AND policy.channel_id ~ '^UC[A-Za-z0-9_-]{22}$'")
_PLAN_ADD = ("""INSERT INTO public.websub_subscriptions AS s(channel_id,callback_base,topic)
 SELECT desired.channel_id,%(base)s,%(prefix)s||desired.channel_id FROM (""" + _DESIRED + """) desired
 ON CONFLICT(channel_id) DO UPDATE SET state='pending',generation=s.generation+1,callback_base=excluded.callback_base,
 attempts=0,next_try_at=clock_timestamp(),requested_at=NULL,verified_at=NULL,lease_seconds=NULL,expires_at=NULL,
 renew_at=NULL,last_outcome=NULL,pending_delta=0,pending_since=NULL,updated_at=clock_timestamp()
 WHERE s.state='unsubscribed' OR s.callback_base<>excluded.callback_base""")
_PLAN_DROP = ("""UPDATE public.websub_subscriptions s SET
 state=CASE WHEN s.requested_at IS NULL OR s.state='denied' THEN 'unsubscribed' ELSE 'unsubscribing' END,
 attempts=0,next_try_at=clock_timestamp(),pending_delta=0,pending_since=NULL,updated_at=clock_timestamp()
 WHERE s.state IN ('pending','verified','denied')
 AND NOT EXISTS(SELECT 1 FROM (""" + _DESIRED + """) desired WHERE desired.channel_id=s.channel_id)""")
_PLAN_UNSTICK = """UPDATE public.websub_subscriptions SET attempts=0,updated_at=clock_timestamp()
 WHERE state IN ('pending','denied','unsubscribing') AND attempts>=%s AND updated_at<clock_timestamp()-interval '24 hours'"""
_PLAN_FORGET = """DELETE FROM public.websub_subscriptions
 WHERE state='unsubscribed' AND updated_at<clock_timestamp()-make_interval(days=>%s)"""
_NEXT = """SELECT channel_id,generation,callback_base,state,attempts,requested_at FROM public.websub_subscriptions
 WHERE (state IN ('pending','denied','unsubscribing') AND next_try_at<=clock_timestamp() AND attempts<%(max)s)
    OR (state='verified' AND renew_at<=clock_timestamp())
 ORDER BY CASE state WHEN 'unsubscribing' THEN 0 WHEN 'verified' THEN 1 ELSE 2 END,
  coalesce(renew_at,next_try_at),channel_id LIMIT 1"""
_HUB = """SELECT failures_in_row,breaker_open,ramp_stage,
 greatest(0,coalesce(extract(epoch FROM hold_until-clock_timestamp()),0))::float AS hold_seconds,
 extract(epoch FROM clock_timestamp()-updated_at)::float AS idle_seconds
 FROM public.websub_hub_state WHERE singleton FOR UPDATE"""
_RAMP_COUNTS = """SELECT count(*) FILTER (WHERE state IN ('pending','verified') AND requested_at IS NOT NULL)::integer AS sent,
 count(*) FILTER (WHERE state='verified')::integer AS verified FROM public.websub_subscriptions"""
_REASK = """SELECT channel_id,generation,callback_base,state,attempts,requested_at FROM public.websub_subscriptions
 WHERE state='pending' AND requested_at IS NOT NULL AND next_try_at<=clock_timestamp() AND attempts<%(max)s
 ORDER BY next_try_at,channel_id LIMIT 1"""
_RESERVE = """UPDATE public.websub_subscriptions SET attempts=attempts+1,
 state=CASE WHEN state='denied' THEN 'pending' ELSE state END,
 generation=CASE WHEN state='denied' THEN generation+1 ELSE generation END,
 requested_at=CASE WHEN state='denied' THEN clock_timestamp() ELSE coalesce(requested_at,clock_timestamp()) END,
 verified_at=CASE WHEN state='denied' THEN NULL ELSE verified_at END,
 lease_seconds=CASE WHEN state='denied' THEN NULL ELSE lease_seconds END,
 next_try_at=clock_timestamp()+make_interval(secs=>%(wait)s),
 renew_at=CASE WHEN state='verified' THEN clock_timestamp()+make_interval(secs=>%(wait)s) ELSE renew_at END,
 last_outcome='sending',updated_at=clock_timestamp()
 WHERE channel_id=%(channel)s AND generation=%(generation)s AND state=%(state)s
 RETURNING generation,state,requested_at,attempts"""
_SETTLE_FAILED = """UPDATE public.websub_subscriptions SET last_outcome=%(outcome)s,
 next_try_at=clock_timestamp()+make_interval(secs=>%(wait)s),
 renew_at=CASE WHEN state='verified' THEN clock_timestamp()+make_interval(secs=>%(wait)s) ELSE renew_at END,
 updated_at=clock_timestamp() WHERE channel_id=%(channel)s AND generation=%(generation)s AND last_outcome='sending'"""
_SETTLE_SENT = """UPDATE public.websub_subscriptions SET last_outcome=%(outcome)s,updated_at=clock_timestamp()
 WHERE channel_id=%(channel)s AND generation=%(generation)s AND last_outcome='sending'"""
_HUB_UPDATE = """UPDATE public.websub_hub_state SET failures_in_row=%(failures)s,breaker_open=%(breaker)s,
 hold_until=CASE WHEN %(hold)s::timestamptz IS NULL THEN hold_until
 ELSE greatest(coalesce(hold_until,%(hold)s::timestamptz),%(hold)s::timestamptz) END,ramp_stage=%(ramp)s,
 updated_at=clock_timestamp() WHERE singleton"""
_HUB_RESET = """UPDATE public.websub_hub_state SET failures_in_row=0,breaker_open=false,updated_at=clock_timestamp() WHERE singleton"""
_CLEAN_JOURNAL = """DELETE FROM public.websub_journal WHERE entry_id IN (SELECT entry_id FROM public.websub_journal
 WHERE at<clock_timestamp()-make_interval(days=>%s) ORDER BY at LIMIT 5000)"""
_CLEAN_EVENTS = """DELETE FROM public.websub_events WHERE event_id IN (SELECT event_id FROM public.websub_events
 WHERE status<>'pending' AND received_at<clock_timestamp()-make_interval(days=>%s) ORDER BY received_at LIMIT 5000)"""


def failure_wait(attempts, retry_after):
    """Per-channel wait after a refused request: 10 min doubling to 2 h, never shorter than Retry-After."""
    return max(int(retry_after or 0), min(7200, 600 * 2 ** max(0, attempts - 1)))


class Admission:
    def __init__(self, allowed, reason, *, wait=0.0):
        self.allowed, self.reason, self.wait = allowed, reason, wait


class WebSubManagerRepository(WebSubRepository):
    """Plans and paces Hub requests. One request at a time; each is recorded before it is sent."""

    def plan(self, base):
        with self.database.transaction(_op('websub_plan', statement_ms=20000, total_ms=30000)) as tx:
            tx.execute(_PLAN_ADD, dict(base=base, prefix=TOPIC_PREFIX))
            tx.execute(_PLAN_DROP)
            tx.execute(_PLAN_UNSTICK, (MAX_ATTEMPTS,))
            tx.execute(_PLAN_FORGET, (FORGET_UNSUBSCRIBED_DAYS,))

    def reserve(self):
        """Pick the next due request and record it before sending. Returns (row or None, Admission)."""
        with self.database.transaction(_op('websub_reserve')) as tx:
            hub = tx.fetch_one(_HUB)
            failures, ramp = hub['failures_in_row'], hub['ramp_stage']
            if hub['breaker_open']:
                if hub['idle_seconds'] < BREAKER_RETRY_SECONDS:
                    return None, Admission(False, 'breaker_open')
                # Half-open: one more request; a further failure reopens at once.
                failures = BREAKER_FAILURES - 1
                tx.execute(_HUB_UPDATE, dict(failures=failures, breaker=False, hold=None, ramp=ramp))
                tx.execute(_JOURNAL, _journal_row('hub_request', 'breaker_retry'))
            if hub['hold_seconds'] > 0:
                return None, Admission(False, 'hub_hold', wait=hub['hold_seconds'])
            row = tx.fetch_one(_NEXT, dict(max=MAX_ATTEMPTS))
            if row is None:
                return None, Admission(False, 'idle')
            starts_subscription = row['state'] == 'denied' or (row['state'] == 'pending' and row['requested_at'] is None)
            if ramp < len(RAMP) and starts_subscription:
                counts = tx.fetch_one(_RAMP_COUNTS)
                sent_needed, verified_needed = RAMP[ramp]
                if counts['sent'] >= sent_needed:
                    if counts['verified'] >= verified_needed:
                        ramp += 1
                        tx.execute(_HUB_UPDATE, dict(failures=failures, breaker=False, hold=None, ramp=ramp))
                    else:
                        # New channels wait for confirmations. An unconfirmed request is
                        # still re-asked on its own schedule; nothing else is sent.
                        row = tx.fetch_one(_REASK, dict(max=MAX_ATTEMPTS))
                        if row is None:
                            return None, Admission(False, 'ramp_wait', wait=30.0)
            mode = 'unsubscribe' if row['state'] == 'unsubscribing' else 'subscribe'
            # An explicit denial closes that callback. Only a committed new generation
            # may receive confirmation, including if the process restarts before sending.
            reserved = tx.returning_one(_RESERVE, dict(wait=RETRY_AFTER_NO_VERIFICATION, channel=row['channel_id'],
                                                       generation=row['generation'], state=row['state']))
            if reserved is None:
                return None, Admission(False, 'raced', wait=1.0)
            tx.execute(_JOURNAL, _journal_row('hub_request', 'sending', channel=row['channel_id'], mode=mode))
            return dict(row, mode=mode, **reserved), Admission(True, 'send')

    def settle(self, row, result):
        """Persist one Hub answer: journal, the channel's next time and the Hub-wide hold/breaker."""
        with self.database.transaction(_op('websub_settle')) as tx:
            hub = tx.fetch_one(_HUB)
            ident = dict(channel=row['channel_id'], generation=row['generation'], outcome=result.outcome[:40])
            if result.accepted or result.ambiguous:
                # Accepted: verification arrives asynchronously. No answer: the Hub
                # may still have the request, so the reserved re-ask time stands.
                tx.execute(_SETTLE_SENT, ident)
            else:
                tx.execute(_SETTLE_FAILED, dict(ident, wait=failure_wait(row['attempts'], result.retry_after)))
            failures = 0 if result.accepted else hub['failures_in_row'] + 1
            hold = None
            if result.ambiguous or result.http_status in (429, 503):
                seconds = max(int(result.retry_after or 0), HOLD_FLOOR_SECONDS)
                hold = tx.fetch_one('SELECT clock_timestamp()+make_interval(secs=>%s) AS at', (seconds,))['at']
            breaker = bool(hub['breaker_open']) or failures >= BREAKER_FAILURES
            tx.execute(_HUB_UPDATE, dict(failures=failures, breaker=breaker, hold=hold, ramp=hub['ramp_stage']))
            tx.execute(_JOURNAL, _journal_row('hub_request', result.outcome, channel=row['channel_id'], mode=row['mode'],
                                              http_status=result.http_status, seconds=result.seconds,
                                              retry_after=result.retry_after))
            return breaker

    def reset_breaker(self):
        with self.database.transaction(_op('websub_reset_breaker')) as tx:
            tx.execute(_HUB_RESET)

    def clean(self, days=JOURNAL_DAYS):
        with self.database.transaction(_op('websub_clean', statement_ms=20000, total_ms=30000)) as tx:
            return tx.execute(_CLEAN_JOURNAL, (days,)) + tx.execute(_CLEAN_EVENTS, (days,))


# ---------------------------------------------------------------- events until the discovery runtime is enabled

# Exactly one consumer owns an envelope. This backend handler takes only rows
# the discovery owner has never touched, and only while the discovery runtime is
# not enabled; the owner (db/discovery.py) claims only once it is enabled.
# Activation takes the control row FOR UPDATE, so the FOR SHARE read below keeps
# a handler transaction and an enabled runtime from overlapping.
_CLAIM = """SELECT event_id,channel_id,video_id,kind,provider_published_at,provider_updated_at FROM public.websub_events
 WHERE status='pending' AND work_phase='metadata_wait' AND claim_token IS NULL AND paused_at IS NULL
 AND next_try_at<=clock_timestamp()
 AND EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND NOT enabled)
 ORDER BY next_try_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED"""
_NOT_ENABLED = "SELECT enabled FROM public.discovery_runtime_control WHERE singleton FOR SHARE"
_KNOWN = "SELECT video_id,channel_id FROM public.video_inventory WHERE video_id=ANY(%s::text[])"
_ANNOUNCED = """SELECT DISTINCT video_id FROM public.websub_events WHERE video_id=ANY(%s::text[])
 AND outcome='discovery_requested'"""
_ELIGIBLE = ("SELECT policy.channel_id FROM (" + CHANNEL_CONTROL_SQL + ") policy "
             "WHERE policy.channel_id=ANY(%s::text[]) AND policy.typed_runtime_eligible")
_COUNT_CHANGED = """SELECT channel_id,channel_video_count_changed_at AS changed_at FROM public.channels
 WHERE channel_id=ANY(%s::text[])"""
_SCOPES = """SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object('channel_id',inventory.channel_id,
 'control_epoch',control.control_epoch)),'[]'::jsonb) AS scopes FROM public.video_inventory inventory
 LEFT JOIN public.channel_collection_state control ON control.channel_id=inventory.channel_id
 WHERE inventory.video_id=ANY(%s::text[])"""
# A closed envelope is settled for the discovery owner as well.
_DONE = """UPDATE public.websub_events SET status=%(status)s,outcome=%(outcome)s,processed_at=clock_timestamp(),
 attempts=attempts+1,work_phase='settled',wait_reason=NULL WHERE event_id=%(event)s"""
_RETRY = """UPDATE public.websub_events SET attempts=attempts+1,outcome=%(outcome)s,
 status=CASE WHEN attempts+1>=%(max)s THEN 'failed' ELSE 'pending' END,
 work_phase=CASE WHEN attempts+1>=%(max)s THEN 'settled' ELSE work_phase END,
 processed_at=CASE WHEN attempts+1>=%(max)s THEN clock_timestamp() END,
 next_try_at=clock_timestamp()+make_interval(secs=>least(3600,60*power(2,attempts)::integer))
 WHERE event_id=ANY(%(events)s::bigint[])"""
_DELTA = """UPDATE public.websub_subscriptions SET pending_delta=greatest(-100000,least(100000,pending_delta+%(delta)s)),
 pending_since=coalesce(pending_since,clock_timestamp()),updated_at=clock_timestamp() WHERE channel_id=%(channel)s"""


def _after(moment, changed_at):
    return moment is not None and (changed_at is None or moment > changed_at)


def classify(events, known, announced, eligible, count_changed=None):
    """Decide each event's effect. Returns (discovery_channels, refresh_ids, deltas, outcomes).

    known maps a video already in the catalogue to its channel; announced holds
    unknown videos whose discovery an earlier event already requested.
    count_changed maps a channel to when its last video-count change was seen.
    The count tally only takes an upload or removal YouTube dates after that
    moment: an older one may already be part of the count, and a tally that
    explains a later, unannounced change would skip a needed playlist check.
    """
    count_changed = count_changed or {}
    discovery, refresh, deltas, outcomes, requested = set(), set(), {}, {}, set(announced)
    for event in events:
        channel, video, kind = event['channel_id'], event['video_id'], event['kind']
        inventory_channel = known.get(video)
        if channel not in eligible:
            outcomes[event['event_id']] = ('skipped', 'channel_not_eligible')
        elif inventory_channel is None and kind == 'entry':
            if video in requested:
                outcomes[event['event_id']] = ('done', 'already_requested')
            else:
                requested.add(video)
                discovery.add(channel)
                if _after(event.get('provider_published_at'), count_changed.get(channel)):
                    deltas[channel] = deltas.get(channel, 0) + 1
                outcomes[event['event_id']] = ('done', 'discovery_requested')
        elif inventory_channel is None:
            outcomes[event['event_id']] = ('skipped', 'unknown_video')
        elif inventory_channel != channel:
            outcomes[event['event_id']] = ('skipped', 'channel_mismatch')
        else:
            refresh.add(video)
            if kind == 'deleted' and _after(event.get('provider_updated_at'), count_changed.get(channel)):
                deltas[channel] = deltas.get(channel, 0) - 1
            outcomes[event['event_id']] = ('done', 'refresh_requested' if kind == 'entry' else 'deletion_check_requested')
    return discovery, refresh, deltas, outcomes


def _error_code(error):
    code = str(getattr(error, 'message', '') or 'effect_failed').lower()
    return code if code.isascii() and code.replace('_', '').isalnum() and len(code) <= 40 else 'effect_failed'


class WebSubEventRepository(WebSubRepository):
    def process(self, limit=50):
        """Turn due events into earlier discovery or refresh, in one transaction. Returns events handled."""
        from .inventory.inventory_priority import prioritize_typed_inventory_channels
        from .videos.video_queue_maintenance import enqueue_video_collection_ids
        with self.database.transaction(_op('websub_process', statement_ms=15000, total_ms=30000, lock_ms=2000)) as tx:
            events = tx.fetch_all(_CLAIM, (limit,), max_rows=limit)
            if not events:
                return 0
            control = tx.fetch_one(_NOT_ENABLED)
            if control is None or control['enabled']:
                return 0        # activation came first: the claimed rows are released untouched
            ids = sorted({e['video_id'] for e in events})
            channels = sorted({e['channel_id'] for e in events})
            known = {r['video_id']: r['channel_id'] for r in tx.fetch_all(_KNOWN, (ids,), max_rows=limit)}
            announced = {r['video_id'] for r in tx.fetch_all(_ANNOUNCED, (ids,), max_rows=limit)}
            eligible = {r['channel_id'] for r in tx.fetch_all(_ELIGIBLE, (channels,), max_rows=limit)}
            changed = {r['channel_id']: r['changed_at'] for r in tx.fetch_all(_COUNT_CHANGED, (channels,), max_rows=limit)}
            discovery, refresh, deltas, outcomes = classify(events, known, announced, eligible, changed)
            try:
                with tx.savepoint():
                    # Subscription rows before playlist rows, in channel order: the
                    # metadata writer's count rule locks them in the same order.
                    for channel in sorted(deltas):
                        if deltas[channel]:
                            tx.execute(_DELTA, dict(channel=channel, delta=deltas[channel]))
                    if discovery:
                        prioritize_typed_inventory_channels(self.database, sorted(discovery), None, transaction=tx)
                    if refresh:
                        refresh_ids = sorted(refresh)
                        scopes = tx.fetch_one(_SCOPES, (refresh_ids,))['scopes']
                        enqueue_video_collection_ids(self.database, refresh_ids, scopes, transaction=tx)
            except DatabaseError as error:
                tx.execute(_RETRY, dict(outcome=_error_code(error), max=MAX_ATTEMPTS,
                                        events=[e['event_id'] for e in events]))
                return len(events)
            for event_id, (status, outcome) in outcomes.items():
                tx.execute(_DONE, dict(status=status, outcome=outcome, event=event_id))
            return len(events)


# ---------------------------------------------------------------- collector hook

# SKIP LOCKED: a subscription row another transaction holds is "not explained"
# at once. The metadata writer never waits for the handler above.
_EXPLAIN = """SELECT pending_delta,coalesce(state='verified' AND expires_at>clock_timestamp()
 AND pending_since>clock_timestamp()-make_interval(hours=>%s),false) AS fresh
 FROM public.websub_subscriptions WHERE channel_id=%s FOR UPDATE SKIP LOCKED"""
_CONSUME = """UPDATE public.websub_subscriptions SET pending_delta=0,pending_since=NULL,updated_at=clock_timestamp()
 WHERE channel_id=%s AND (pending_delta<>0 OR pending_since IS NOT NULL)"""


def explained_increase(tx, channel_id, increase):
    """A video-count increase fully announced by WebSub needs no playlist read.

    The announced uploads are consumed on any count increase. A mismatch clears
    the tally and the normal discovery that follows reconciles everything.
    Called by the count writer (db/discovery_counts.py) only while the
    discovery runtime is not enabled.
    """
    if increase <= 0:
        return False
    row = tx.fetch_one(_EXPLAIN, (PENDING_DELTA_HOURS, channel_id))
    if row is None:
        return False
    tx.execute(_CONSUME, (channel_id,))
    return bool(row['fresh']) and row['pending_delta'] == increase


# ---------------------------------------------------------------- health

# Health runs inside an HTTP request, where every read is lic_reader. That role
# reads no public.websub_* table; readable.websub_health gives it these totals only.
_HEALTH = """SELECT subscriptions,expiring_24h,last_delivery_at,deliveries_24h,events_24h,events_pending,
 hub_requests_24h,hold_until,breaker_open,ramp_stage FROM readable.websub_health"""


def health(database):
    return database.read_row('websub_health', _HEALTH)
