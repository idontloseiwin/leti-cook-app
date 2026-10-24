"""Bounded metadata candidates; the existing metadata unit owns every write."""
from datetime import datetime, timezone
from threading import get_ident
from uuid import uuid4

from channel_collection_scope import validate_channel_scopes
from collection.dispatch import Coordinator, Skipped, Work
from collection.metadata_dispatch import MetadataBatchUnit
from collection_outcome import copy_collection_stop
from db.channel.channel_sync import MetadataCandidateCursor


_COUNTERS = ('requested', 'processed', 'updated', 'video_count_changed', 'video_count_increased',
    'video_count_decreased', 'channel_discovery_prioritized', 'typed_inventory_prioritized',
    'no_long_reactivated', 'no_long_reactivation_deferred', 'still_no_long_playlist',
    'manual_required', 'missing', 'batches', 'errors', 'provider_steps')
_LISTS = ('video_count_changed_channel_ids', 'video_count_changed_channel_scopes',
    'video_count_increased_channel_ids', 'video_count_increased_channel_scopes', 'video_count_decreased_channel_ids',
    'video_count_decreased_channel_scopes', 'metadata_persisted_channel_scopes')


class AutoMetadataCursor:
    """Physical due position only; no channel payload, running marker or budget."""
    def __init__(self):
        self.after = self.due_ceiling = None
        self.sweep_exhausted = False
        self._owner, self._active = get_ident(), None

    def _check(self):
        if get_ident() != self._owner:
            raise RuntimeError('metadata_cursor_owner_thread_required')

    def _bind(self, feeder):
        self._check()
        if self._active is not None:
            raise RuntimeError('metadata_cursor_pass_active')
        if self.sweep_exhausted:
            self.after = self.due_ceiling = None
            self.sweep_exhausted = False
        self._active = feeder


class AutoMetadataFeeder:
    """One resident <=50-channel chunk, recaptured only at prepare.

    The channel budget charges selected candidates once, including stale captures.
    A physical scan cap ends this pass, not its sweep. Only known drainage may
    release the cursor for another logical pass; the composition owns its lifetime.
    """
    def __init__(self, repository, context, *, channel_limit, candidate_scan_limit,
                 cursor, require_ready, should_stop=lambda: False, on_settled=None,
                 unit_factory=MetadataBatchUnit):
        if (type(channel_limit) is not int or not 0 <= channel_limit <= 100000
                or type(candidate_scan_limit) is not int or not 0 <= candidate_scan_limit < 2**63
                or not isinstance(cursor, AutoMetadataCursor)
                or any(not callable(value) for value in (require_ready, should_stop, unit_factory))
                or (on_settled is not None and not callable(on_settled))):
            raise ValueError('metadata_feeder_config_invalid')
        self.repository, self.context, self.cursor = repository, context, cursor
        self.channel_limit, self.candidate_scan_limit = channel_limit, candidate_scan_limit
        self.require_ready, self.should_stop, self.on_settled = require_ready, should_stop, on_settled
        self.unit_factory = unit_factory
        self.selected = self.inspected = 0
        self.paused = self.exhausted = False
        self.summary = {name: 0 for name in _COUNTERS}
        self.summary.update({name: [] for name in _LISTS})
        self.summary.update(error_messages=[], skipped_missing_columns=False, candidate_inspected=0)
        self._owner, self._coordinator = get_ident(), None
        self._key, self._unit, self._closed = None, None, False
        self._identity, self._sequence = str(uuid4()), 0
        cursor._bind(self)

    def _check(self, coordinator=None):
        if get_ident() != self._owner or self._closed:
            raise RuntimeError('metadata_feeder_owner_required')
        if coordinator is not None:
            if not isinstance(coordinator, Coordinator) or self._coordinator not in (None, coordinator):
                raise ValueError('metadata_feeder_coordinator_mismatch')
            self._coordinator = coordinator
            if self._key is not None and not coordinator.contains(self._key):
                self._key = self._unit = None

    def pause(self, reason):
        self._check()
        self.paused = True
        self.summary['stopped_reason'] = reason

    def finish_pass(self, coordinator):
        self._check(coordinator)
        if self._key is not None or coordinator.snapshot().blocked or not (self.exhausted or self.paused):
            raise RuntimeError('metadata_feeder_pass_release_unconfirmed')
        self.cursor._check()
        self.cursor._active = None
        self._closed = True

    def _page(self, page, limit):
        if not isinstance(page, dict) or set(page) != {'rows', 'next_after', 'due_ceiling', 'inspected_count', 'scan_exhausted'}:
            raise ValueError('metadata_feeder_page_invalid')
        rows, count, after = page['rows'], page['inspected_count'], page['next_after']
        ceiling, exhausted = page['due_ceiling'], page['scan_exhausted']
        if (not isinstance(rows, list) or type(count) is not int or not 0 <= count <= limit
                or len(rows) > count or type(exhausted) is not bool or exhausted != (count < limit)
                or (count and type(after) is not MetadataCandidateCursor)
                or (not count and after != self.cursor.after)
                or (count and after == self.cursor.after)
                or not isinstance(ceiling, str)
                or (self.cursor.due_ceiling is not None and ceiling != self.cursor.due_ceiling)):
            raise ValueError('metadata_feeder_page_invalid')
        parsed = datetime.fromisoformat(ceiling)
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError('metadata_feeder_clock_invalid')
        scopes = validate_channel_scopes(rows, channel_ids=[row.get('channel_id') for row in rows]) if rows else []
        if any(set(row) != {'channel_id', 'control_epoch'} for row in rows) or len(scopes) != len(rows):
            raise ValueError('metadata_feeder_page_invalid')
        self.cursor.after, self.cursor.due_ceiling, self.cursor.sweep_exhausted = after, ceiling, exhausted
        self.inspected += count
        self.summary['candidate_inspected'] = self.inspected
        return scopes

    def _settled(self, summary):
        self._check()
        if summary.get('settlement_unknown'):
            raise RuntimeError('metadata_feeder_settlement_unconfirmed')
        for name in _COUNTERS:
            self.summary[name] += int(summary.get(name) or 0)
        for name in _LISTS:
            values = summary.get(name) or []
            if len(values) > 50 or len(self.summary[name]) + len(values) > self.channel_limit:
                raise RuntimeError('metadata_feeder_handoff_bound_exceeded')
            self.summary[name].extend(values)
        # Source-bearing diagnostic examples remain bounded independently of
        # the complete private ID/epoch handoff required by later phases.
        self.summary['error_messages'].extend((summary.get('error_messages') or [])[:max(0, 50-len(self.summary['error_messages']))])
        if copy_collection_stop(self.summary, summary):
            self.paused = True
        if self.on_settled is not None:
            self.on_settled(summary)

    def offer_to(self, coordinator, *, limit=None):
        self._check(coordinator)
        if limit is not None and (type(limit) is not int or not 0 <= limit <= 100):
            raise ValueError('metadata_feeder_offer_limit_invalid')
        if limit == 0:
            return 0
        current = coordinator.snapshot()
        if self.paused or self.should_stop() or current.blocked or current.paused or current.stopped:
            return 0
        if self._key is not None:
            return 0
        if (self.selected >= self.channel_limit or self.inspected >= self.candidate_scan_limit
                or self.cursor.sweep_exhausted):
            self.exhausted = True
            return 0
        if current.ready >= 2 * coordinator.concurrency:
            return 0
        count = min(50, self.channel_limit-self.selected, self.candidate_scan_limit-self.inspected)
        self.require_ready()
        scopes = self._page(self.repository.metadata_candidate_page(limit=count,
            after=self.cursor.after, due_ceiling=self.cursor.due_ceiling), count)
        if not scopes:
            return 0
        self.selected += len(scopes)
        self._sequence += 1
        key = f'auto-metadata:{self._identity}:{self._sequence}'
        def prepare():
            self._check()
            if self.paused or self.should_stop():
                return Skipped()
            self.require_ready()
            channels = self.repository.capture_metadata_candidates(scopes, due_ceiling=self.cursor.due_ceiling)
            expected = {(scope['channel_id'], scope['control_epoch']) for scope in scopes}
            from channel_collection_scope import channel_scope
            actual = [channel_scope(row) for row in channels]
            if (len(channels) > len(scopes) or len({row['channel_id'] for row in actual}) != len(actual)
                    or any((row['channel_id'], row['control_epoch']) not in expected for row in actual)):
                raise RuntimeError('metadata_feeder_capture_scope_mismatch')
            if not channels:
                return Skipped()
            self._unit = self.unit_factory(self.context, key=key, lane='auto', channels=channels,
                require_ready=self.require_ready, on_settled=self._settled, should_stop=self.should_stop)
            return self._unit.prepare()
        def settle(completion):
            self._check()
            if self._unit is None:
                raise RuntimeError('metadata_feeder_unit_missing')
            return self._unit.settle(completion)
        coordinator.offer(Work(key, 'auto', scopes[0]['channel_id'], 'channel', prepare, settle,
                               exclusion_key='channel-metadata'))
        self._key = key
        return 1
