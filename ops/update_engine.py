"""Durable update ordering; effects are owned by the checked local host adapter."""
from __future__ import annotations

import time


class UpdateError(ValueError):
    pass


EARLY = frozenset({'preparing', 'prepared', 'hold_requested', 'drained'})
OLD_DATABASE = EARLY | {'backend_stop_intent', 'backend_stopped', 'db_stop_intent',
                        'db_stopped', 'copy_intent', 'copy_verified'}
SUCCESS = frozenset({'verified', 'cleanup', 'complete'})
ROLLBACK_SUCCESS = frozenset({'rollback_verified', 'rollback_cleanup', 'rolled_back'})


class UpdateEngine:
    """Record intent before effects. An interrupted uncertain step is never replayed."""

    def __init__(self, host, journal, *, clock=time.time, emit=print):
        self.host, self.journal = host, dict(journal)
        self.clock, self.emit = clock, emit

    def phase(self, phase, **changes):
        self.journal.update(changes, phase=phase)
        self.host.save(self.journal)
        self.emit('stage=' + phase)

    def update(self):
        try:
            self.phase('preparing')
            prepared = self.host.prepare(self.journal)
            self.phase('prepared', **prepared)
            self.phase('hold_requested')
            self.host.hold(self.journal)
            epoch = self.host.drain(self.journal)
            self.phase('drained', runtime_epoch=epoch)
            self.phase('backend_stop_intent', stopped_at=int(self.clock()))
            self.host.stop_backend(self.journal)
            # The source window is authoritative only after all writers stopped.
            window = self.host.window(self.journal)
            copy = self.host.copy_parameters(self.journal, window)
            self.phase('backend_stopped', copy=copy)
            self.phase('db_stop_intent')
            self.host.stop_database(self.journal)
            self.phase('db_stopped')
            self.phase('copy_intent', copy=copy)
            self.host.copy(self.journal)
            self.phase('copy_verified')
            self.phase('swap_intent')
            self.host.exchange(self.journal, desired='new')
            self.phase('swapped')
            self.host.select_runtime(self.journal, new=True)
            self.phase('schema_intent')
            self.host.start_database(self.journal, new=True)
            # The adapter reconciles an unknown SQL acknowledgement using the ledger.
            self.host.schema(self.journal)
            self.phase('schema_applied')
            self.phase('new_start_intent')
            self.host.start_backend(self.journal, new=True)
            self.phase('new_started')
            self.host.health(self.journal, new=True)
            self.phase('verified')
            self.finish(new=True)
            return 'updated'
        except (Exception, KeyboardInterrupt):
            # Do not record exception text: it may contain credentials or source data.
            self.emit('Update was interrupted. Recovering its recorded state.')
            self.recover()
            if self.journal['phase'] == 'complete':
                return 'updated'
            raise UpdateError('update_failed_previous_version_recovered') from None

    def finish(self, *, new):
        self.phase('cleanup' if new else 'rollback_cleanup')
        self.host.cleanup(self.journal, new=new)
        self.phase('complete' if new else 'rolled_back')
        stopped = self.journal['stopped_at']
        seconds = max(0, int(self.clock()) - stopped) if stopped is not None else 0
        self.emit(('Update completed.' if new else 'Previous version recovered.')
                  + ' Application pause: ' + str(seconds) + ' seconds.')

    def recover(self):
        phase = self.journal['phase']
        if phase in {'complete', 'rolled_back'}:
            return False
        self.host.stop_helpers(self.journal)
        if phase in SUCCESS:
            # Health was durably acknowledged. Finish cleanup, not an unneeded restore.
            self.finish(new=True)
            return True
        if phase in ROLLBACK_SUCCESS:
            self.finish(new=False)
            return True
        try:
            if phase in OLD_DATABASE:
                # No candidate schema was permitted yet; preserve the original volume.
                self.host.recover_original(self.journal)
                self.host.cleanup_preparation(self.journal)
                self.phase('rollback_verified')
                self.finish(new=False)
                return True
            self.phase('rollback_intent')
            self.host.stop_all(self.journal)
            self.host.exchange(self.journal, desired='old')
            self.phase('rollback_restore_intent')
            # A prior uncertain/partial restore is never started or overwritten.
            volume = self.host.restore(self.journal)
            self.phase('rollback_restored', restore_volume=volume)
            self.host.select_runtime(self.journal, new=False)
            self.phase('rollback_start_intent')
            self.host.start_database(self.journal, new=False)
            self.host.start_backend(self.journal, new=False)
            self.phase('rollback_started')
            self.host.health(self.journal, new=False)
            self.phase('rollback_verified')
            self.finish(new=False)
            return True
        except (Exception, KeyboardInterrupt):
            # Keep the last durable intent. It identifies what recovery may do;
            # replacing it with an undifferentiated 'failed' would lose that proof.
            raise UpdateError('update_recovery_failed_copy_deadline_still_applies') from None
