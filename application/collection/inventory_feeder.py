"""Opt-in auto inventory producer for one shared coordinator, never a lease owner."""
from collections.abc import Mapping
from dataclasses import replace
from threading import get_ident
import time
from uuid import UUID, uuid4

from channel_collection_scope import require_control_epoch
from collection.budgets import PageBudget
from collection.dispatch import Ack, Coordinator, DuplicateWork, Prepared, Skipped
from collection.inventory import MAX_CONTINUATION_PROOFS, MAX_RESULT_EXAMPLES, _utc_now
from collection.inventory_dispatch import InventoryPageReservations, InventoryPageUnit
from db.inventory.inventory_candidates import InventoryCandidateCursor
from db.inventory.typed_inventory import TypedInventoryClaim
from saved_work import channel_id as canonical_channel_id

_TYPES=('long','short','stream')
_STATUS={'running','completed','playlist_not_found','error','quota_deferred','cancelled',
         'expired','scheduled_handoff','hot_runtime_interrupted'}


def _uuid(value):
    if not isinstance(value,str) or len(value)!=36 or str(UUID(value))!=value:
        raise ValueError('inventory_feeder_proof_invalid')
    return value


class AutoInventoryCursor:
    """Child-lifetime physical position; no budgets, proofs, tokens or leases."""
    def __init__(self):
        self._owner,self._active=get_ident(),None
        self._after=self._ceiling=None
        self._exhausted=False

    def _check(self):
        if get_ident()!=self._owner:
            raise RuntimeError('inventory_cursor_owner_thread_required')

    @property
    def sweep_exhausted(self):
        self._check()
        return self._exhausted

    def _reset(self):
        self._check()
        if not self._exhausted:
            raise RuntimeError('inventory_cursor_sweep_incomplete')
        self._after=self._ceiling=None
        self._exhausted=False

    def _bind(self, feeder):
        self._check()
        if self._active is not None:
            raise RuntimeError('inventory_cursor_pass_active')
        if self._exhausted:self._reset()
        self._active=feeder

    def _release(self, feeder):
        self._check()
        if self._active is not feeder:
            raise RuntimeError('inventory_cursor_pass_not_active')
        self._active=None


class AutoInventoryFeeder:
    """One preflighted auto pass; all counters/proofs stay on its owner thread.

    candidate_scan_limit is explicit composition input, not a product default.
    The fresh caller-owned PageBudget is exclusive to this automatic phase.
    cancel_ready is the explicit hook for dropping this feeder's unsent work.
    """
    def __init__(self, repository, *, page_budget, cursor, candidate_scan_limit, run_id,
                 worker_id, fetch_page, require_ready, concurrency=8, lease_seconds=900,
                 playlist_not_found_errors=(), now=_utc_now, clock=time.monotonic,
                 should_stop=lambda: False, on_page=None, on_settled=None):
        if (not isinstance(page_budget,PageBudget) or page_budget.attempts!=0
                or not isinstance(cursor,AutoInventoryCursor)
                or type(candidate_scan_limit) is not int or not 0<=candidate_scan_limit<2**63
                or not isinstance(run_id,str) or not run_id.strip() or len(run_id)>200
                or not isinstance(worker_id,str) or not worker_id.strip() or len(worker_id)>200
                or type(concurrency) is not int or not 1<=concurrency<=50
                or type(lease_seconds) is not int or not 60<=lease_seconds<=3600
                or any(not callable(value) for value in (fetch_page,require_ready,now,clock,should_stop))
                or any(value is not None and not callable(value) for value in (on_page,on_settled))
                or any(not callable(getattr(repository,name,None)) for name in
                       ('candidate_page','claim_channels','claim_pass'))):
            raise ValueError('inventory_feeder_config_invalid')
        self.repository,self.budget=repository,page_budget
        self.reservations=InventoryPageReservations(page_budget)
        self.candidate_scan_limit,self.run_id=candidate_scan_limit,run_id
        self.worker_id,self.concurrency,self.lease_seconds=worker_id,concurrency,lease_seconds
        self.fetch_page,self.require_ready,self.now,self.clock=fetch_page,require_ready,now,clock
        self.playlist_not_found_errors=playlist_not_found_errors
        self.should_stop,self.on_page,self.on_settled=should_stop,on_page,on_settled
        self.claims_limit=page_budget.limit
        self.claims_processed=self.candidate_inspected=0
        self.paused=self.pass_finished=False
        self.pass_stop_reason=None
        self._closed=False
        self._owner=get_ident();self._coordinator=None
        self._identity=str(uuid4());self._sequence=0
        self.cursor=cursor
        self._sweep_claimed=0
        self._owned={};self._proofs={};self._offered=set()
        cursor._bind(self)
        self.summary=dict(enabled=True,claimed=0,processed=0,completed=0,continuations=0,
            playlist_not_found=0,errors=0,inserted=0,already_matching=0,conflicts=0,
            provider_attempts=0,quota_exhausted=False,results=[],results_omitted=0,
            candidate_inspected=0,by_type={kind:dict(processed=0,inserted=0,errors=0) for kind in _TYPES})

    def _check(self):
        if get_ident()!=self._owner:
            raise RuntimeError('inventory_feeder_owner_thread_required')
        if self._closed:
            raise RuntimeError('inventory_feeder_pass_closed')

    @property
    def exhausted(self):
        """Per-pass completion only; physical EOF lives on the shared cursor."""
        return self.pass_finished

    def _prune(self, coordinator):
        self._offered={key for key in self._offered if coordinator.contains(key)}
        for scope,record in list(self._owned.items()):
            if record['phase']=='candidate' and record['key'] not in self._offered:
                del self._owned[scope]

    def finish_pass(self, coordinator):
        """Release the physical cursor after known drain, never on uncertainty."""
        self._coordinated(coordinator)
        self._prune(coordinator)
        unknown={'inventory_claim_unknown','inventory_prepare_unconfirmed',
            'inventory_settlement_unconfirmed','inventory_candidate_read_failed',
            'inventory_continuation_scope_changed'}
        if (self._owned or self._offered or self.reservations.reserved
                or coordinator.snapshot().blocked
                or self.summary.get('stopped_reason') in unknown
                or not (self.pass_finished or self.paused)):
            raise RuntimeError('inventory_feeder_pass_release_unconfirmed')
        self.cursor._release(self)
        self._proofs.clear()
        self._closed=True

    def _sync(self):
        self.summary.update(claimed=self.claims_processed,provider_attempts=self.budget.attempts,
                            candidate_inspected=self.candidate_inspected)

    @property
    def claims_remaining(self):
        self._check()
        reserved=sum(row['phase'] in ('claiming','claim_unknown') for row in self._owned.values())
        return max(0,self.claims_limit-self.claims_processed-reserved)

    def pause(self, reason):
        self._check()
        self.paused=True
        self.summary['stopped_reason']=reason
        self._sync()

    def _coordinated(self, coordinator):
        self._check()
        if (not isinstance(coordinator,Coordinator) or coordinator.concurrency!=self.concurrency
                or (self._coordinator is not None and self._coordinator is not coordinator)):
            raise ValueError('inventory_feeder_coordinator_mismatch')
        self._coordinator=coordinator

    def cancel_ready(self, coordinator):
        """Drop only descriptors whose prepare has never entered; no DB/lease work."""
        self._coordinated(coordinator)
        removed=0
        for scope,record in list(self._owned.items()):
            if record['phase']=='candidate':
                coordinator.cancel(record['key'])
                del self._owned[scope]
                removed+=1
        return removed

    def _accept_page(self, page, limit):
        fields={'rows','next_after','ceiling','inspected_count','scan_exhausted'}
        if not isinstance(page,Mapping) or set(page)!=fields:
            raise ValueError('inventory_feeder_candidate_invalid')
        rows,after,ceiling,inspected,exhausted=(page[name] for name in
            ('rows','next_after','ceiling','inspected_count','scan_exhausted'))
        if (not isinstance(rows,list) or type(inspected) is not int or not 0<=inspected<=limit
                or len(rows)>inspected or type(exhausted) is not bool or exhausted!=(inspected<limit)
                or type(ceiling) is not int or not 0<=ceiling<2**63
                or (self.cursor._ceiling is not None and ceiling!=self.cursor._ceiling)
                or (inspected==0 and after!=self.cursor._after)
                or (inspected>0 and (type(after) is not InventoryCandidateCursor
                    or after.dispatch_ordinal>ceiling or (self.cursor._after is not None and after.key<=self.cursor._after.key)))):
            raise ValueError('inventory_feeder_candidate_invalid')
        clean=[];seen=set()
        for row in rows:
            if not isinstance(row,Mapping) or set(row)!={'channel_id','video_type','control_epoch','due'}:
                raise ValueError('inventory_feeder_candidate_invalid')
            row=dict(row);canonical_channel_id(row['channel_id']);require_control_epoch(row['control_epoch'])
            if row['video_type'] not in _TYPES or type(row['due']) is not bool:
                raise ValueError('inventory_feeder_candidate_invalid')
            scope=(row['channel_id'],row['video_type'])
            if scope in seen:
                raise ValueError('inventory_feeder_candidate_invalid')
            seen.add(scope);clean.append(row)
        self.cursor._after,self.cursor._ceiling,self.cursor._exhausted=after,ceiling,exhausted
        self.candidate_inspected+=inspected
        self._sync()
        return clean

    def _work(self, scope, row, proof):
        self._sequence+=1
        key=f'auto-inventory:{self._identity}:{self._sequence}'
        record=dict(key=key,phase='candidate',claim=None,followup=None)

        def claim():
            self._check();record['phase']='claiming'
            try:
                options=dict(worker_id=self.worker_id,run_id=self.run_id,lease_seconds=self.lease_seconds)
                if proof is not None:
                    rows=self.repository.claim_pass(continuations=[dict(proof)],limit=1,include_due=False,**options)
                else:
                    rows=self.repository.claim_channels([row['channel_id']],video_types=[row['video_type']],
                        expected_channel_scopes=[dict(channel_id=row['channel_id'],control_epoch=row['control_epoch'])],
                        only_due=True,**options)
                if not isinstance(rows,list) or len(rows)>1:
                    raise RuntimeError('inventory_feeder_claim_scope_mismatch')
                value=rows[0] if rows else None
                if value is not None:
                    if (not isinstance(value,TypedInventoryClaim) or (value.channel_id,value.video_type)!=scope
                            or require_control_epoch(value.lease_channel_epoch)!=row['control_epoch']
                            or (proof is not None and value.active_scan_id!=proof['scan_id'])):
                        raise RuntimeError('inventory_feeder_claim_scope_mismatch')
                return value
            except Exception:
                record['phase']='claim_unknown'
                self.pause('inventory_claim_unknown')
                raise

        def claimed(value):
            self._check()
            if record['phase']!='claiming':
                raise RuntimeError('inventory_feeder_claim_repeated')
            record['claim']=value
            record['phase']='claimed' if value is not None else 'empty'
            if value is not None:
                self.claims_processed+=1
                self._proofs.pop(scope,None)
            self._sync()

        def settled(summary):
            self._settled(scope,record,summary)

        unit=InventoryPageUnit(self.repository,key=key,lane='auto',channel=row['channel_id'],
            video_type=row['video_type'],claim_page=claim,fetch_page=self.fetch_page,
            reservations=self.reservations,require_ready=self.require_ready,on_claimed=claimed,
            on_settled=settled,lease_seconds=self.lease_seconds,now=self.now,clock=self.clock,
            playlist_not_found_errors=self.playlist_not_found_errors,should_stop=self.should_stop)

        def prepare():
            self._check()
            if self.paused or self.should_stop() or not self.claims_remaining or not self.budget.remaining:
                self._owned.pop(scope,None)
                return Skipped()
            record['phase']='preparing'
            try:
                result=unit.prepare()
                if isinstance(result,Skipped):self._owned.pop(scope,None)
                return result
            except Exception:
                self.pause('inventory_prepare_unconfirmed')
                raise

        def settle(completion):
            self._check()
            try:
                if record['followup'] is not None:
                    return record['followup'].settle(completion)
                outcome=unit.settle(completion)
                if isinstance(outcome,Ack) and record['followup'] is not None:
                    prepared=record['followup'].prepare()
                    return Ack() if isinstance(prepared,Skipped) else prepared
                return outcome
            except Exception:
                self.pause('inventory_settlement_unconfirmed')
                raise

        return replace(unit.work,prepare=prepare,settle=settle),record

    def _settled(self, scope, record, summary):
        self._check()
        if record['phase']!='claimed' or not isinstance(summary,Mapping) or summary.get('settlement_unknown'):
            raise RuntimeError('inventory_feeder_settlement_unconfirmed')
        status=summary.get('status');attempts=summary.get('provider_attempts')
        if (status not in _STATUS or type(attempts) is not int or attempts not in (0,1)
                or (summary.get('channel_id'),summary.get('video_type'))!=scope):
            raise RuntimeError('inventory_feeder_settlement_unconfirmed')
        counts={name:summary.get(name,0) for name in ('page_items','inserted','already_matching','conflicts')}
        if any(type(value) is not int or not 0<=value<=50 for value in counts.values()):
            raise RuntimeError('inventory_feeder_settlement_unconfirmed')
        proof=None
        if status=='running':
            if attempts!=1:
                raise RuntimeError('inventory_feeder_settlement_unconfirmed')
            proof=dict(channel_id=scope[0],video_type=scope[1],control_epoch=record['claim'].lease_channel_epoch,
                       scan_id=_uuid(summary.get('scan_id')),last_transition_id=_uuid(summary.get('transition_id')))
        # Only bounded scalar summaries leave the private receipt/proof owner.
        sample=dict(video_type=scope[1],status=status,provider_attempts=attempts,page_items=counts['page_items'])
        if len(self.summary['results'])<MAX_RESULT_EXAMPLES:self.summary['results'].append(sample)
        else:self.summary['results_omitted']+=1
        self.summary['processed']+=attempts
        for name in ('inserted','already_matching','conflicts'):self.summary[name]+=counts[name]
        key={'running':'continuations','completed':'completed','playlist_not_found':'playlist_not_found','error':'errors'}.get(status)
        if key:self.summary[key]+=1
        by_type=self.summary['by_type'][scope[1]]
        by_type['processed']+=attempts;by_type['inserted']+=counts['inserted'];by_type['errors']+=int(status=='error')
        if proof is not None:
            if len(self._proofs)>=MAX_CONTINUATION_PROOFS:self._proofs.pop(next(iter(self._proofs)))
            self._proofs[scope]=proof
        if summary.get('quota_exhausted'):self.summary['quota_exhausted']=True
        if summary.get('stopped_reason') or summary.get('yield_requested'):
            reason=summary.get('stopped_reason')
            self.pause(reason if reason in _STATUS|{'provider_rate_limited','youtube_quota_exhausted'} else 'inventory_stopped')
        # Full durable page evidence is a private owner-thread handoff, never a
        # telemetry sample. A failed observer retains the descriptor; no replay.
        record['phase']='progress'
        if self.on_page is not None and attempts and status in ('running','completed','playlist_not_found','error'):
            followup=self.on_page(summary)
            if followup is not None:
                from collection.availability import AddressedAvailabilityUnit
                if (not isinstance(followup,AddressedAvailabilityUnit) or status!='completed'
                        or scope[1]!='long' or followup.kind!='recheck'
                        or followup.scope!=summary.get('channel_scope')):
                    raise RuntimeError('inventory_feeder_followup_invalid')
                record['followup']=followup
        if self.on_settled is not None:
            self.on_settled(summary)
        record['phase']='acknowledged'
        del self._owned[scope]
        self._sync()

    def offer_to(self, coordinator, *, limit=None):
        """One physical candidate page, never a private descriptor backlog."""
        self._coordinated(coordinator)
        if limit is not None and (type(limit) is not int or not 0<=limit<=100):
            raise ValueError('inventory_feeder_offer_limit_invalid')
        if limit == 0:return 0
        self._prune(coordinator)
        current=coordinator.snapshot()
        if current.stopped:
            self.cancel_ready(coordinator)
        self._sync()
        capacity=2*self.concurrency-current.ready
        if limit is not None:capacity=min(capacity,limit)
        if current.blocked or current.paused or current.stopped or self.paused or self.should_stop():
            return 0
        if not self.claims_remaining or not self.budget.remaining or self.candidate_inspected>=self.candidate_scan_limit:
            self.pass_stop_reason=('claims_limit' if not self.claims_remaining else
                'page_limit' if not self.budget.remaining else 'candidate_scan_limit')
            self.pass_finished=not self._owned and not self._offered
            return 0
        if self.cursor._exhausted:
            if self._owned:return 0
            if self.claims_processed==self._sweep_claimed:
                self.pass_finished=True
                self.pass_stop_reason='sweep_exhausted'
                return 0
            if not capacity:return 0
            self.cursor._reset()
            self._sweep_claimed=self.claims_processed
        if not capacity:return 0
        limit=min(100,capacity,self.candidate_scan_limit-self.candidate_inspected)
        try:
            self.require_ready()
            page=self.repository.candidate_page(limit=limit,after=self.cursor._after,ceiling=self.cursor._ceiling,
                continuations=[dict(value) for value in self._proofs.values()])
            rows=self._accept_page(page,limit)
        except Exception:
            self.pause('inventory_candidate_read_failed')
            raise
        self.pass_finished=False
        offered=0
        for row in rows:
            scope=(row['channel_id'],row['video_type'])
            if scope in self._owned:continue
            proof=self._proofs.get(scope)
            if (proof is None and not row['due']) or (proof is not None and proof['control_epoch']!=row['control_epoch']):
                self.pause('inventory_continuation_scope_changed')
                raise RuntimeError('inventory_feeder_continuation_unconfirmed')
            work,record=self._work(scope,row,proof)
            try:coordinator.offer(work)
            except DuplicateWork:continue  # Another lane owns this exact playlist; no claim occurred.
            self._owned[scope]=record
            self._offered.add(work.key)
            offered+=1
        return offered
