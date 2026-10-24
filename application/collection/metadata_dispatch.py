"""Opt-in captured metadata chunk; provider threads never resume domain steps."""
from copy import deepcopy
from datetime import datetime, timezone
import threading
import time

from channel_collection_scope import channel_scope
from collection import channel_metadata as metadata
from collection.dispatch import Ack, Prepared, Skipped, Work
from collection.provider_steps import ProviderResult, ProviderStep
from collection_shutdown import handoff_requested
from db import ChannelSyncRepository
from hot_runtime import HotRuntimeUnavailable


class MetadataBatchUnit:
    """One <=50-channel captured chunk and one credit across provider continuations.

    One process-local exclusion key serializes these units until composition can
    prove disjoint channel ownership. No DB lease is invented for metadata. The
    caller owns candidate freshness/paging and daily-quota sibling cancellation.
    Unknown writes/progress require explicit external reconciliation, never retry.
    """
    def __init__(self, context, *, key, lane, channels, require_ready, on_settled,
                 lease_seconds=900, clock=time.monotonic, now=None, should_stop=lambda: False):
        if (not isinstance(channels, (list, tuple)) or not 1 <= len(channels) <= 50
                or type(lease_seconds) is not int or not 30 <= lease_seconds <= 900
                or any(not callable(value) for value in (require_ready, on_settled, clock, should_stop))
                or (now is not None and not callable(now))):
            raise ValueError('metadata_dispatch_config_invalid')
        self.channels = tuple(deepcopy(channel) for channel in channels)
        scopes = [channel_scope(channel) for channel in self.channels]
        if len({scope['channel_id'] for scope in scopes}) != len(scopes):
            raise ValueError('metadata_dispatch_duplicate_channel')
        self.context, self.require_ready, self.on_settled = context, require_ready, on_settled
        self.lease_seconds, self.clock, self.should_stop = lease_seconds, clock, should_stop
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.state = metadata.MetadataSettlement()
        self.summary = self.state.summary
        self.phase, self.steps, self.current_step, self.deadline = 'candidate', None, None, None
        self._thread = threading.get_ident()
        self.work = Work(key, lane, scopes[0]['channel_id'], 'channel', self.prepare, self.settle,
                         exclusion_key='channel-metadata')

    def _owner(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('metadata_dispatch_owner_thread_required')

    def prepare(self):
        self._owner()
        if self.phase != 'candidate':
            raise RuntimeError('metadata_dispatch_prepare_repeated')
        if handoff_requested() or self.should_stop():
            self.phase = 'skipped'
            return Skipped()
        self.require_ready()
        started = self.now()
        if not isinstance(started, datetime) or started.tzinfo is None or started.utcoffset() is None:
            raise ValueError('metadata_dispatch_start_clock_invalid')
        channels = tuple(channel for channel in self.channels
            if metadata.channel_sync_eligibility(channel).allow_channel_metadata
            and channel.get('channel_metadata_status') != metadata.CHANNEL_METADATA_STATUS_RUNNING
            and metadata.channel_metadata_is_due(channel, started, parse_dt=self.context.sources.parse_dt))
        if not channels:
            self.phase = 'skipped'
            return Skipped()
        self.deadline = self.clock() + self.lease_seconds - 1
        self.steps = metadata.channel_metadata_steps(context=self.context, captured_channels=channels,
            settlement=self.state, started_at=started)
        outcome = self._advance()
        return Skipped() if isinstance(outcome, Ack) else outcome

    def _ack(self):
        if self.state.pending or self.summary.get('settlement_unknown'):
            self.state.unknown()
        self.phase = 'progress'
        self.on_settled(self.summary)
        self.phase = 'acknowledged'
        return Ack()

    def _advance(self, response=None):
        self.phase = 'settling'
        try:
            self.current_step = next(self.steps) if response is None else self.steps.send(response)
        except StopIteration:
            return self._ack()
        except metadata.MetadataSettlementDenied:
            return self._drain('hot_runtime_interrupted')
        except metadata.MetadataSettlementUnknown:
            self.phase = 'settlement_unknown'
            raise
        if not isinstance(self.current_step, ProviderStep):
            raise TypeError('metadata_dispatch_provider_step_required')
        if self.should_stop():
            return self._drain('collection_stopped')
        try:
            self.require_ready()
        except HotRuntimeUnavailable:
            return self._drain('hot_runtime_interrupted')
        self.phase = 'prepared'
        return Prepared(self.fetch, self.deadline, len(self.current_step.identities))

    def _drain(self, reason):
        if self.summary.get('settlement_unknown'):
            self.state.unknown()
        self.phase = 'releasing'
        markers = list(self.state.pending.values())
        if markers:
            # This owner validates the exact result, with no SKIP-LOCKED absence.
            ChannelSyncRepository(self.context.database).drain_metadata_running(markers)
            self.state.pending.clear()
        self.steps.close()
        self.summary.update(yield_requested=True, incomplete=True, stopped_reason=reason,
                            hot_runtime_interrupted=reason == 'hot_runtime_interrupted')
        return self._ack()

    def fetch(self, cancelled):
        # Only a captured source callback/materialization/timing runs here.
        return self.current_step.execute()

    def settle(self, completion):
        self._owner()
        if self.phase != 'prepared':
            raise RuntimeError('metadata_dispatch_settle_repeated')
        if completion.phase in ('cancelled', 'expired') and not completion.fetch_started:
            return self._drain('scheduled_handoff' if handoff_requested() else completion.phase)
        if completion.phase != 'response' or not isinstance(completion.value, ProviderResult):
            raise RuntimeError('metadata_dispatch_response_unknown')
        self.summary['provider_steps'] = self.summary.get('provider_steps', 0) + 1
        return self._advance(completion.value)
