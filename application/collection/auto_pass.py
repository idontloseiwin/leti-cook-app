"""Opt-in common-video pass bookkeeping, independent of process composition.

No queue, lease, SQL, provider call or automatic retry lives here. One owner
thread retains the original stats/1000-row scan budgets and run UUID across
interleaved CommonBatchUnits. Inventory/metadata phase conversion is separate.
"""
from collections import deque
from collections.abc import Mapping
from threading import get_ident
from uuid import UUID, uuid4

from saved_work import SavedWorkError, channel_id as _channel_id

_TYPES = ('long', 'short', 'stream', 'unknown')
_OPERATIONS = ('metadata', 'counters')
_FRONTIERS = tuple((operation, kind) for operation in _OPERATIONS for kind in _TYPES)
# Every confirmed type can use Stats; unfinished streams remain General work
# in the database mode projection. An unknown type never has a Stats frontier.
COUNTERS_EMPTY_FRONTIERS = frozenset({('counters', 'unknown')})


def channel_id(value):
    try:
        return _channel_id(value)
    except SavedWorkError:
        raise ValueError('auto_pass_candidate_result_invalid') from None


class CommonCandidateCursor:
    """In-child physical positions only; one active pass, no budget or leases."""
    def __init__(self):
        self._owner, self._active = get_ident(), None
        self._formats = deque(_TYPES)
        self._after = dict.fromkeys(_FRONTIERS)
        self._exhausted = set(COUNTERS_EMPTY_FRONTIERS)
        self._revisit = False

    def _check(self):
        if get_ident() != self._owner:
            raise RuntimeError('common_candidate_cursor_owner_thread_required')

    @property
    def sweep_exhausted(self):
        self._check()
        return len(self._exhausted) == len(_FRONTIERS)

    def _reset_sweep(self):
        self._check()
        if not self.sweep_exhausted:
            raise RuntimeError('common_candidate_sweep_incomplete')
        self._after = dict.fromkeys(_FRONTIERS)
        self._exhausted = set(COUNTERS_EMPTY_FRONTIERS)
        self._revisit = False

    def _bind(self, state):
        self._check()
        if self._active is not None:
            raise RuntimeError('common_candidate_cursor_pass_active')
        if self.sweep_exhausted:
            self._reset_sweep()
        self._active = state

    def _release(self, state):
        self._check()
        if (self._active is not state or state.held or state._page is not None
                or state.stop_reason in ('claim_unknown', 'settlement_unknown',
                    'candidate_read_failed', 'candidate_offer_failed')):
            raise RuntimeError('common_candidate_cursor_release_unconfirmed')
        # Only never-claimed candidates may remain after the feeder confirms
        # all its descriptors drained. Their frontier has not advanced.
        state._candidate_pages.clear()
        self._active = None


class AutoPassState:
    """Finite counters for one logical pass borrowing an explicit physical cursor.

    A scope's empty/exhausted claim is not global queue exhaustion. Independent
    candidate scan accounting bounds DB-only sweeps without spending API quota
    or changing the original raw-video inspected counter.
    """
    def __init__(self, stats_limit, *, concurrency=8, run_id=None, sync_run_id=None):
        if (type(stats_limit) is not int or stats_limit < 0 or type(concurrency) is not int
                or not 1 <= concurrency <= 50
                or (sync_run_id is not None and (type(sync_run_id) is not int or sync_run_id < 1))):
            raise ValueError('auto_pass_config_invalid')
        run_id = str(uuid4()) if run_id is None else run_id
        if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
            raise ValueError('auto_pass_run_invalid')
        self.run_id, self.sync_run_id = run_id, sync_run_id
        self.limit, self.concurrency = stats_limit, concurrency
        self.scan_limit = max(1000, min(((stats_limit * 20 + 999)//1000)*1000, 2000000))
        self.claimed = self.inspected = self.paused_rows = self.settled_units = 0
        self.paused = False
        self.stop_reason = None
        self._owner = get_ident()
        self._claims = {}
        self._candidate_cursor = None
        self._page = None
        self._candidate_pages = {}
        self.candidate_inspected = 0
        self.candidate_scan_limit = self.scan_limit
        self._sweep_claimed = 0

    def _check(self):
        if get_ident() != self._owner:
            raise RuntimeError('auto_pass_owner_thread_required')

    def bind_candidate_cursor(self, cursor):
        self._check()
        if (not isinstance(cursor, CommonCandidateCursor) or self._candidate_cursor is not None
                or self.held or self.claimed or self.inspected or self.candidate_inspected or self.paused):
            raise ValueError('auto_pass_candidate_cursor_invalid')
        cursor._bind(self)
        self._candidate_cursor = cursor

    def _cursor(self):
        cursor = self._candidate_cursor
        if cursor is None or cursor._active is not self:
            raise RuntimeError('auto_pass_candidate_cursor_required')
        cursor._check()
        return cursor

    @property
    def _after(self):
        return self._cursor()._after

    @property
    def held(self):
        return len(self._claims)

    @property
    def remaining(self):
        reserved = sum(row['limit'] for row in self._claims.values() if row['phase'] in ('claiming','unknown'))
        return max(0, self.limit-self.claimed-reserved)

    def pause(self, reason):
        self._check()
        self.paused, self.stop_reason = True, reason

    def begin_claim(self, key):
        """Called exactly once immediately before the existing claim owner."""
        self._check()
        if self._candidate_cursor is not None:
            self._cursor()
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or key in self._claims:
            raise ValueError('auto_pass_claim_identity_invalid')
        if self.paused or not self.remaining or self.inspected >= self.scan_limit:
            return None
        if self.held >= self.concurrency:
            raise RuntimeError('auto_pass_credit_exhausted')
        if any(row['phase']=='claiming' for row in self._claims.values()):
            raise RuntimeError('auto_pass_claim_callback_pending')
        limit = min(self.remaining, 50)
        self._claims[key] = dict(phase='claiming', limit=limit)
        return dict(limit=limit, run_id=self.run_id)

    def claim_failed(self, key):
        """Retain reservation: a missing claim ACK cannot prove zero leases."""
        self._check()
        if self._claims[key]['phase'] != 'claiming':
            raise RuntimeError('auto_pass_claim_not_pending')
        self._claims[key]['phase'] = 'unknown'
        # Keep unknown reservations charged, rather than advertising capacity.
        self.pause('claim_unknown')

    def on_claimed(self, key, batch):
        self._check()
        record = self._claims[key]
        if record['phase'] != 'claiming':
            raise RuntimeError('auto_pass_claim_not_repeatable')
        if not isinstance(batch, Mapping):
            self.claim_failed(key)
            raise ValueError('auto_pass_claim_result_invalid')
        rows, inspected, paused, exhausted = (batch.get(name) for name in
            ('rows', 'inspected_count', 'paused_count', 'scan_exhausted'))
        if (not isinstance(rows, (tuple, list)) or len(rows)>record['limit']
                or type(inspected) is not int or not 0<=inspected<=1000
                or type(paused) is not int or not 0<=paused<=inspected
                or len(rows)+paused>inspected or type(exhausted) is not bool
                or (not inspected and not exhausted)):
            self.claim_failed(key)
            raise ValueError('auto_pass_claim_result_invalid')
        self.claimed += len(rows)
        self.inspected += inspected
        self.paused_rows += paused
        if rows:
            record.update(phase='claimed', limit=0, scan_exhausted=exhausted)
        else:
            if not exhausted and self._candidate_cursor is not None:
                self._cursor()._revisit = True
            del self._claims[key]

    def on_settled(self, key, summary):
        """The adapter calls this only after domain settlement is acknowledged."""
        self._check()
        record = self._claims[key]
        if record['phase'] != 'claimed':
            raise RuntimeError('auto_pass_settle_not_repeatable')
        if not isinstance(summary, Mapping) or summary.get('settlement_unknown'):
            self.settlement_failed(key)
            raise RuntimeError('auto_pass_settlement_unknown')
        if not record['scan_exhausted'] and self._candidate_cursor is not None:
            # This includes due rows omitted from an operation-specific batch.
            # Their original deadline stays in the queue; the next fair sweep
            # can choose them without a new provider lane or persistent queue.
            self._cursor()._revisit = True
        del self._claims[key]
        self.settled_units += 1
        if summary.get('stopped_reason') or any(summary.get(name) for name in (
                'yield_requested', 'infrastructure_error', 'quota_exhausted',
                'general_quota_exhausted', 'batch_quota_exhausted', 'batch_unavailable')):
            self.pause(summary.get('stopped_reason') or 'collection_stopped')

    def settlement_failed(self, key):
        """Keep a claimed unit charged after missing/uncertain durable ACK."""
        self._check()
        record = self._claims[key]
        if record['phase'] != 'claimed':
            raise RuntimeError('auto_pass_settle_not_repeatable')
        record['phase'] = 'unknown'
        self.pause('settlement_unknown')

    def operation_window_exhausted(self, operation):
        """Physical EOF of one advisory window, not its confirmed checkpoint."""
        self._check()
        cursor = self._cursor()
        return all((operation, kind) in cursor._exhausted or
                   self._candidate_pages.get((operation, kind), {}).get('exhausted', False)
                   for kind in _TYPES)

    def _candidate_capacity(self, capacity):
        self._check()
        if type(capacity) is not int or not 0 <= capacity <= 2*self.concurrency:
            raise ValueError('auto_pass_candidate_capacity_invalid')
        if self._page is not None:
            raise RuntimeError('auto_pass_candidate_read_pending')
        return not (self.paused or not capacity or not self.remaining or self.inspected >= self.scan_limit
                    or self.candidate_inspected >= self.candidate_scan_limit)

    def seed_request(self, capacity):
        """Peek at current fair-format heads without claiming or advancing them."""
        if not self._candidate_capacity(capacity):
            return None
        cursor = self._cursor()
        if cursor.sweep_exhausted:
            if self.claimed == self._sweep_claimed and (self.held or not cursor._revisit):
                return None
            self._sweep_claimed = self.claimed
            cursor._reset_sweep()
        for _ in _TYPES:
            kind = cursor._formats[0]
            cursor._formats.rotate(-1)
            keys = [(operation, kind) for operation in _OPERATIONS]
            # An offered descriptor owns its frontier until the claim ACK.
            if any(key in self._candidate_pages for key in keys):
                continue
            starts = [cursor._after[key] for key in keys if key not in cursor._exhausted]
            if not starts:
                continue
            after = min(starts, key=lambda value: value or '')
            self._page = dict(video_type=kind, run_id=self.run_id, after=after,
                limit=min(50, self.candidate_scan_limit-self.candidate_inspected), operation=None,
                frontiers={op:dict(after=cursor._after[(op,kind)],
                    exhausted=(op,kind) in cursor._exhausted) for op in _OPERATIONS})
            return dict(self._page)
        return None

    def candidate_request(self, capacity, *, scope_limit=None, extend_types=(), operation='metadata'):
        """One operation-filtered physical page with at most 50 new scopes."""
        if scope_limit is not None and (type(scope_limit) is not int or not 1 <= scope_limit <= 50):
            raise ValueError('auto_pass_candidate_scope_limit_invalid')
        if (not isinstance(extend_types, (tuple, set, frozenset))
                or any(kind not in _TYPES for kind in extend_types)
                or operation not in _OPERATIONS):
            raise ValueError('auto_pass_candidate_extension_invalid')
        if not self._candidate_capacity(capacity):
            return None
        cursor = self._cursor()
        for _ in _TYPES:
            kind = cursor._formats[0]
            cursor._formats.rotate(-1)
            key = (operation, kind)
            pending = self._candidate_pages.get(key)
            if key not in cursor._exhausted and (pending is None or
                    (kind in extend_types and not pending['exhausted'])):
                after = pending['after'] if pending is not None else cursor._after[key]
                self._page = dict(video_type=kind,run_id=self.run_id,after=after,
                    limit=min(capacity if scope_limit is None else scope_limit,50,
                        self.candidate_scan_limit-self.candidate_inspected), operation=operation)
                return dict(self._page)
        return None

    def _validated_page(self, page):
        request = self._page
        if request is None:
            raise RuntimeError('auto_pass_candidate_read_not_pending')
        rows, inspected, exhausted, after = (page.get(name) for name in
            ('rows','inspected_count','scan_exhausted','next_after'))
        if (not isinstance(rows,list) or type(inspected) is not int or not 0 <= inspected <= request['limit']
                or len(rows) > inspected or type(exhausted) is not bool
                or exhausted != (inspected < request['limit'])
                or (inspected == 0 and after != request['after'])
                or (inspected > 0 and channel_id(after) <= (request['after'] or ''))):
            raise ValueError('auto_pass_candidate_result_invalid')
        previous = request['after'] or ''
        for row in rows:
            if (set(row) != {'channel_id','video_type','operation','ready_count','due_at'}
                    or row['video_type'] != request['video_type']
                    or not previous < channel_id(row['channel_id']) <= after
                    or row['operation'] not in _OPERATIONS
                    or (request['operation'] is not None and row['operation'] != request['operation'])
                    or type(row['ready_count']) is not int or not 1 <= row['ready_count'] <= 50
                    or row['due_at'] is None):
                raise ValueError('auto_pass_candidate_result_invalid')
            previous = row['channel_id']
        self.candidate_inspected += inspected
        self._page = None
        return request, rows, exhausted, after

    def accept_seed_page(self, page):
        self._check()
        cursor = self._cursor()
        request, rows, exhausted, after = self._validated_page(page)
        if request['operation'] is not None:
            raise RuntimeError('auto_pass_seed_required')
        if rows:
            # Start the filtered pass at the just-selected fair format head.
            while cursor._formats[0] != request['video_type']:
                cursor._formats.rotate(-1)
            # Heads are compared in this fair round, not globally across the queue.
            return min(rows, key=lambda row: (row['due_at'], row['channel_id']))['operation']
        # A genuinely empty unfiltered page is safe for both operation frontiers.
        for operation in _OPERATIONS:
            key = (operation, request['video_type'])
            if key in COUNTERS_EMPTY_FRONTIERS:
                continue
            if (after or '') >= (cursor._after[key] or ''):
                cursor._after[key] = after
            if exhausted:
                cursor._exhausted.add(key)
        return None

    def accept_candidate_page(self, page):
        self._check()
        cursor = self._cursor()
        request, rows, exhausted, after = self._validated_page(page)
        key = (request['operation'], request['video_type'])
        if request['operation'] not in _OPERATIONS:
            raise RuntimeError('auto_pass_operation_required')
        fresh = rows
        if key in self._candidate_pages:
            pending = self._candidate_pages[key]
            # A single speculative EOF wrap may encounter an already buffered scope.
            fresh = [row for row in rows if row['channel_id'] not in pending['pending']]
            pending['pending'].update({row['channel_id']: False for row in fresh})
            pending.update(after=after, exhausted=exhausted)
        elif rows:
            self._candidate_pages[key] = dict(
                pending={row['channel_id']:False for row in rows}, after=after, exhausted=exhausted)
        else:
            cursor._after[key] = after
            if exhausted:
                cursor._exhausted.add(key)
        return fresh

    def wrap_candidate_window(self, operation, *, scopes):
        """One caller-bounded speculative wrap; never checkpoint unclaimed rows."""
        self._check()
        cursor = self._cursor()
        if not self.operation_window_exhausted(operation):
            raise RuntimeError('auto_pass_candidate_wrap_before_eof')
        if scopes is not None:
            owned = {(item['video_type'], item['channel_id']) for item in scopes}
            if any((kind, identity) not in owned for (mode,kind),pending in self._candidate_pages.items()
                   if mode == operation for identity in pending['pending']):
                return False
        for kind in _TYPES:
            key = (operation, kind)
            if key in COUNTERS_EMPTY_FRONTIERS:
                continue
            pending = self._candidate_pages.get(key)
            if pending is not None:
                pending.update(after=None, exhausted=False)
            else:
                cursor._after[key] = None
            cursor._exhausted.discard(key)
        return True

    def candidate_claimed(self, scope, *, operation='metadata'):
        """Only a known claim ACK advances one operation's ordered frontier."""
        self._check()
        cursor = self._cursor()
        key = (operation, scope['video_type'])
        identity = scope['channel_id']
        page = self._candidate_pages.get(key)
        if page is None or identity not in page['pending'] or page['pending'][identity]:
            raise RuntimeError('auto_pass_candidate_claim_unexpected')
        page['pending'][identity] = True
        while page['pending']:
            first = next(iter(page['pending']))
            if not page['pending'][first]:
                break
            cursor._after[key] = first
            del page['pending'][first]
        if not page['pending']:
            cursor._after[key] = page['after']
            if page['exhausted']:
                cursor._exhausted.add(key)
            del self._candidate_pages[key]
