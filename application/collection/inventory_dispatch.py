"""Opt-in one-page inventory adapter; durable owners remain in inventory/db."""
import threading
import time
import uuid

from collection.budgets import PageBudget
from collection.dispatch import Ack, Prepared, Skipped, Work
from collection.inventory import (prepare_typed_inventory_claim, settle_typed_inventory_claim,
    _TypedInventoryDailyQuotaError, _utc_now)
from collection.inventory_steps import InventoryPageResponse
from collection_shutdown import handoff_requested
from db.hot.hot_collection_control import HotCollectionControlRepository
from db.inventory.typed_inventory import TypedInventoryClaim
from hot_runtime import HotRuntimeUnavailable
from provider_admission import AdmissionCancelled


class InventoryPageReservations:
    """One pass's owner-only reservations, separate from actual attempt charges.

    Use this budget exclusively through this object while any unit is reserved.
    Unknown pre-dispatch/submit outcomes retain their reservation. Confirmed
    attempts are charged once even if the subsequent database write is unknown.
    A quota stop prevents new claims; the caller must explicitly cancel/drain
    its already prepared independent siblings. It does not acknowledge them.
    """
    def __init__(self, budget):
        if not isinstance(budget, PageBudget):
            raise TypeError('inventory_dispatch_budget_required')
        self.budget, self.quota = budget, None
        self._owner_thread, self._reserved = threading.get_ident(), set()

    def _owner(self):
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError('inventory_budget_owner_thread_required')

    @property
    def reserved(self):
        self._owner()
        return len(self._reserved)

    def reserve(self, key):
        self._owner()
        if key in self._reserved:
            raise RuntimeError('inventory_page_already_reserved')
        if self.quota is not None or len(self._reserved) >= min(self.budget.remaining, 50):
            return False
        self._reserved.add(key)
        return True

    def release(self, key):
        self._owner()
        self._reserved.remove(key)

    def charge(self, key):
        self._owner()
        if key not in self._reserved or not self.budget.try_debit():
            raise RuntimeError('inventory_attempt_reservation_lost')
        self._reserved.remove(key)

    def stop_for_quota(self, error):
        self._owner()
        if self.quota is None:
            self.quota = error


class InventoryPageUnit:
    """One claim/begin/bind -> raw provider page -> known receipt/progress ACK.

    A continuation page is a new descriptor after this unit's ACK; stable
    (channel, type) exclusion prevents overlap. claim_page returns one exact
    TypedInventoryClaim or None, on the owner thread. It must use the supplied
    lease_seconds. The caller owns durable candidate paging and sibling drains.
    """
    def __init__(self, repository, *, key, lane, channel, video_type, claim_page,
                 fetch_page, reservations, require_ready, on_claimed, on_settled,
                 lease_seconds=900, transition_id=None, before_dispatch=None,
                 require_observed_video_ids=False, playlist_not_found_errors=(),
                 now=_utc_now, uuid_factory=uuid.uuid4, clock=time.monotonic, should_stop=lambda: False):
        if (not isinstance(reservations, InventoryPageReservations)
                or not isinstance(channel, str) or len(channel) != 24 or not channel.startswith('UC')
                or video_type not in ('long', 'short', 'stream')
                or type(lease_seconds) is not int or not 60 <= lease_seconds <= 3600
                or any(not callable(v) for v in (claim_page, fetch_page, require_ready,
                    on_claimed, on_settled, now, uuid_factory, clock, should_stop))):
            raise ValueError('inventory_dispatch_config_invalid')
        self.repository, self.claim_page, self.fetch_page = repository, claim_page, fetch_page
        self.reservations, self.require_ready = reservations, require_ready
        self.on_claimed, self.on_settled = on_claimed, on_settled
        self.lease_seconds, self.clock, self.should_stop = lease_seconds, clock, should_stop
        self.options = dict(transition_id=transition_id, before_dispatch=before_dispatch,
            require_observed_video_ids=require_observed_video_ids,
            playlist_not_found_errors=playlist_not_found_errors, now=now, uuid_factory=uuid_factory)
        self.phase, self.claim, self.prepared, self.summary = 'candidate', None, None, None
        self.deadline = None
        self.work = Work(key, lane, channel, video_type, self.prepare, self.settle,
                         exclusion_key='playlist:' + channel + ':' + video_type)

    def _identity(self):
        if self.prepared is not None:
            return dict(self.prepared.identity, scan_id=self.prepared.scan_id)
        return dict(channel_id=self.work.channel, video_type=self.work.format)

    def _ack(self, summary):
        self.summary, self.phase = summary, 'progress'
        self.on_settled(summary)
        self.phase = 'acknowledged'
        return Ack()

    def prepare(self):
        if self.phase != 'candidate':
            raise RuntimeError('inventory_dispatch_prepare_repeated')
        if handoff_requested() or self.should_stop():
            self.phase = 'skipped'
            return Skipped()
        self.require_ready()
        if not self.reservations.reserve(self.work.key):
            self.phase = 'skipped'
            return Skipped()
        self.phase = 'claiming'
        self.deadline = self.clock() + self.lease_seconds - 1
        self.claim = self.claim_page()
        if self.claim is not None and (not isinstance(self.claim, TypedInventoryClaim)
                or self.claim.channel_id != self.work.channel or self.claim.video_type != self.work.format):
            raise RuntimeError('inventory_dispatch_claim_scope_mismatch')
        self.on_claimed(self.claim)
        if self.claim is None:
            self.reservations.release(self.work.key)
            self.phase = 'skipped'
            return Skipped()
        if handoff_requested() or self.should_stop():
            self._drain('scheduled_handoff')
            return Skipped()
        self.phase = 'preparing'
        planned = prepare_typed_inventory_claim(repository=self.repository, claim=self.claim,
            fetch_page=self.fetch_page, page_budget=self.reservations.budget, **self.options)
        if isinstance(planned, dict):
            self.summary = planned
            # A bind returning await_receipt explicitly lacks a durable ACK.
            # Never abort or refund that uncertain claim automatically.
            raise RuntimeError('inventory_dispatch_prepare_unconfirmed')
        self.prepared = planned
        try:
            self.require_ready()
        except HotRuntimeUnavailable:
            self._drain('hot_runtime_interrupted')
            return Skipped()
        self.phase = 'prepared'
        return Prepared(self.fetch, self.deadline)

    def fetch(self, cancelled):
        # Coordinator checks cancellation/deadline before callback entry. A
        # late handoff before actual fetch is also definitely unsent.
        if cancelled.is_set() or handoff_requested() or self.should_stop():
            return InventoryPageResponse(attempted=False)
        return self.prepared.step.execute()

    def _defer_quota(self, *, unsent=False):
        self.phase = 'quota_deferring'
        error = self.reservations.quota
        reply = self.repository.defer_claims_after_daily_quota([self.claim],
            transition_id=self.prepared.identity['transition_id'],
            source_checked_at=error.source_checked_at)
        if (reply.get('status') != 'deferred' or reply.get('active_scan_state_preserved') is not True
                or reply.get('requested') != 1 or reply.get('released') != 1 or reply.get('rejected') != 0):
            raise RuntimeError('inventory_quota_receipt_unconfirmed')
        if unsent:
            self.reservations.release(self.work.key)
        return self._ack({**self._identity(), 'status': 'quota_deferred',
            'error_type': error.provider_error_type, 'quota_exhausted': True,
            'stopped_reason': 'youtube_quota_exhausted', 'claim_release_status': 'quota_deferred',
            'provider_attempts': 0 if unsent else 1, 'pages_processed': 0, 'page_items': 0})

    def _drain(self, reason):
        if self.reservations.quota is not None and self.prepared is not None:
            return self._defer_quota(unsent=True)
        self.phase = 'releasing'
        # This closed owner verifies exact token absence in the release
        # transaction. A partial release count alone can never acknowledge it.
        HotCollectionControlRepository(self.repository.database).drain(playlists=[{
            'channel_id': self.claim.channel_id, 'video_type': self.claim.video_type,
            'lease_token': self.claim.lease_token}])
        self.reservations.release(self.work.key)
        return self._ack({**self._identity(), 'status': reason, 'stopped_reason': reason,
            'yield_requested': True, 'incomplete': True,
            'hot_runtime_interrupted': reason == 'hot_runtime_interrupted',
            'provider_attempts': 0, 'pages_processed': 0, 'page_items': 0})

    def settle(self, completion):
        if self.phase != 'prepared':
            raise RuntimeError('inventory_dispatch_settle_repeated')
        if completion.phase in ('cancelled', 'expired') and not completion.fetch_started:
            return self._drain('scheduled_handoff' if handoff_requested() else completion.phase)
        if completion.phase != 'response' or not isinstance(completion.value, InventoryPageResponse):
            # No response envelope means no trustworthy attempt/clock evidence.
            # Keep the reservation/credit; do not invent a source timestamp.
            raise RuntimeError('inventory_dispatch_response_unknown')
        response = completion.value
        if response.attempted is False:
            return self._drain('scheduled_handoff' if handoff_requested() else 'cancelled')
        if response.attempted is not True:
            raise RuntimeError('inventory_dispatch_attempt_unknown')
        if isinstance(response.error, AdmissionCancelled):
            # The request limiter withdrew this request (quota stop, pause or
            # handoff): no page was received, so the last confirmed page stays
            # the checkpoint. Defer or release; never record a list failure.
            return self._drain('scheduled_handoff' if handoff_requested() else 'cancelled')
        self.phase = 'settling'
        self.reservations.charge(self.work.key)
        try:
            summary = settle_typed_inventory_claim(repository=self.repository,
                prepared=self.prepared, response=response)
        except _TypedInventoryDailyQuotaError as error:
            self.reservations.stop_for_quota(error)
            return self._defer_quota()
        return self._ack(summary)