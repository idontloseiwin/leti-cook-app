"""Durable automatic playlist admission on the existing database owner thread.

A full page always consumes full capacity; count/event work also consumes event
capacity in the same reservation. The provider adapter retains sole quota-ledger
ownership. Disabled preparation spends full capacity without activating signals.
"""
from threading import get_ident
from discovery_admission import DiscoveryAdmissionDenied, DiscoveryAdmissionUnsafe


class InventoryDiscoveryAdmission:
    def __init__(self, budget):
        self.budget, self.owner, self.failure = budget, get_ident(), None

    def _check(self):
        if get_ident() != self.owner:
            raise RuntimeError('inventory_admission_owner_thread_required')
        if self.failure is not None:
            raise self.failure

    def admit(self, repository, claim, begin, transition_id):
        self._check()
        try:
            flags = repository.discovery_admission_scope(claim, scan_id=begin['scan_id'])
        except Exception as error:
            self.failure = DiscoveryAdmissionUnsafe('inventory_admission_outcome_unknown')
            raise self.failure from error
        return self.admit_flags(flags, transition_id)

    def admit_flags(self, flags, transition_id):
        self._check()
        try:
            if set(flags) != {'full', 'event'} or any(type(v) is not bool for v in flags.values()):
                raise RuntimeError('inventory_discovery_scope_unknown')
            if not (flags['full'] or flags['event']):return
            decision = self.budget.reserve(transition_id, bucket='general', **flags)
            allowed = decision['allowed'] and self.budget.begin_dispatch(transition_id)
        except Exception as error:
            self.failure = DiscoveryAdmissionUnsafe('inventory_admission_outcome_unknown')
            raise self.failure from error
        if not allowed:
            raise DiscoveryAdmissionDenied(decision if not decision['allowed'] else {'reason':'grant_not_dispatchable'})
        return transition_id

    def provider(self, token, callback):
        if token is None:
            return callback
        def admitted(*args, **kwargs):
            from quota_buckets import discovery_grant
            with discovery_grant(self.budget, token):
                return callback(*args, **kwargs)
        return admitted

    def exhausted(self):
        self._check()
        from quota_buckets import control_active
        if control_active():
            return
        try:
            return self.budget.note_exhausted('general')
        except Exception as error:
            self.failure = DiscoveryAdmissionUnsafe('inventory_quota_ban_unknown', http_attempted=True)
            raise self.failure from error
