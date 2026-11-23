"""Operation intents survive failures and abrupt process death at every boundary."""
import errno
import json
from pathlib import Path
import subprocess
import unittest

from application.update_copy_protocol import UpdateCopyError
from install import InstallError
from ops import update_journal, update_messages as messages
from ops.release_manifest import ReleaseError
from ops.schema_manifest import SchemaError
from ops.update_engine import UpdateEngine, UpdateError
from ops.update_journal import JournalError


class PowerLoss(BaseException):
    pass


class Host:
    def __init__(self, failure=None, crash=False):
        self.events, self.saved = [], None
        self.failure, self.crash = failure, crash

    def save(self, value):
        self.saved = dict(value)

    def __getattr__(self, action):
        def effect(*args, **kwargs):
            self.events.append(action)
            if self.failure == action:
                self.failure = None
                raise PowerLoss() if self.crash else ValueError('injected')
            return {'prepare': {'new_tree': 'new', 'new_images': 'images'},
                    'drain': 'epoch', 'copy_parameters': {'expires_at': 200},
                    'restore': 'restored'}.get(action)
        return effect


class EngineTests(unittest.TestCase):
    def engine(self, host, journal=None):
        return UpdateEngine(host, journal or {'phase': 'preparing', 'stopped_at': None},
                            clock=lambda: 100, emit=lambda value: None)

    def test_success_order_and_cleanup(self):
        host = Host()
        self.assertEqual(self.engine(host).update(), 'updated')
        self.assertEqual(host.saved['phase'], 'complete')
        self.assertLess(host.events.index('stop_backend'), host.events.index('window'))
        self.assertLess(host.events.index('copy'), host.events.index('exchange'))
        self.assertLess(host.events.index('health'), host.events.index('cleanup'))

    def test_every_forward_effect_failure_recovers(self):
        actions = ['prepare', 'hold', 'drain', 'stop_backend', 'window', 'stop_database',
                   'copy_parameters', 'copy', 'exchange', 'select_runtime', 'start_database',
                   'schema', 'start_backend', 'health', 'cleanup']
        for action in actions:
            with self.subTest(action=action):
                host = Host(action)
                if action == 'cleanup':
                    self.assertEqual(self.engine(host).update(), 'updated')
                else:
                    with self.assertRaises(UpdateError):
                        self.engine(host).update()
                self.assertEqual(host.saved['phase'], 'rolled_back' if action != 'cleanup' else 'complete')
                if actions.index(action) < actions.index('exchange'):
                    self.assertNotIn('restore', host.events)
                elif action != 'cleanup':
                    self.assertIn('restore', host.events)

    def test_kill_at_every_forward_boundary_recovers_from_saved_intent(self):
        baseline = Host()
        self.engine(baseline).update()
        for action in baseline.events:
            with self.subTest(action=action):
                host = Host(action, crash=True)
                with self.assertRaises(PowerLoss):
                    self.engine(host).update()
                self.engine(host, host.saved).recover()
                self.assertIn(host.saved['phase'], {'complete', 'rolled_back'})

    def test_failed_recovery_retains_exact_intent_and_copy(self):
        host = Host('restore')
        with self.assertRaisesRegex(UpdateError, 'deadline'):
            self.engine(host, {'phase': 'schema_intent', 'stopped_at': 50}).recover()
        self.assertEqual(host.saved['phase'], 'rollback_restore_intent')
        self.assertNotIn('cleanup', host.events)

    def test_rollback_interruption_can_be_recovered_again(self):
        for action in ['stop_all', 'exchange', 'restore', 'select_runtime', 'start_database',
                       'start_backend', 'health', 'cleanup']:
            with self.subTest(action=action):
                host = Host(action, crash=True)
                with self.assertRaises(PowerLoss):
                    self.engine(host, {'phase': 'schema_intent', 'stopped_at': 50}).recover()
                self.engine(host, host.saved).recover()
                self.assertEqual(host.saved['phase'], 'rolled_back')

    def test_terminal_operation_is_not_repeated(self):
        for phase in ('complete', 'rolled_back'):
            host = Host()
            self.assertFalse(self.engine(host, {'phase': phase}).recover())
            self.assertEqual(host.events, [])


SECRET = 'AIzaSyD-private-key-material /opt/private/path'


class CauseHost:
    """Fake host that fails one action with a given error and keeps every saved journal."""

    def __init__(self, action, error, *, recovery_error=None, save_errors=0):
        self.action, self.error, self.recovery_error = action, error, recovery_error
        self.saves, self.events, self.save_errors = [], [], save_errors
        self.failed = False

    def save(self, value):
        if self.failed and self.save_errors:
            self.save_errors -= 1
            raise OSError(5, SECRET)
        self.saves.append(dict(value))

    def __getattr__(self, action):
        def effect(*args, **kwargs):
            self.events.append(action)
            if action == self.action and not self.failed:
                self.failed = True
                raise self.error
            if action == 'restore' and self.recovery_error is not None:
                raise self.recovery_error
            return {'prepare': {'new_tree': {'device': 1, 'inode': 3}, 'new_images': 'images'},
                    'drain': 'e' * 32, 'copy_parameters': {'schema_version': 47},
                    'restore': 'leti-cook-0123456789ab_restore'}.get(action)
        return effect


class FailureCauseTests(unittest.TestCase):
    """The journal and the owner get one fixed cause, never exception text."""

    def run_update(self, host):
        lines = []
        engine = UpdateEngine(host, {'phase': 'preparing', 'stopped_at': None, 'failure_code': None},
                              clock=lambda: 100, emit=lines.append)
        try:
            outcome = engine.update()
        except UpdateError as error:
            outcome = str(error)
        return outcome, lines

    def assert_cause(self, host, code, *, paused):
        outcome, lines = self.run_update(host)
        self.assertEqual(outcome, 'update_failed_previous_version_recovered')
        self.assertEqual(host.saves[-1]['phase'], 'rolled_back')
        self.assertEqual(host.saves[-1]['failure_code'], code)
        self.assertEqual(lines[-1], messages.cause_line(code))
        self.assertIn(messages.HINTS[code], lines[-1])
        text = '\n'.join(lines) + json.dumps(host.saves)
        self.assertNotIn('AIza', text)
        self.assertNotIn('/opt/private', text)
        if paused:
            self.assertIn('Update was interrupted. Recovering its recorded state.', lines)
            self.assertIn('Previous version recovered. Application pause: 0 seconds.', lines)
        else:
            self.assertIn('Update stopped before the application was paused. Removing what it prepared.', lines)
            self.assertIn('The installed version kept running.', lines)
            self.assertFalse(any('interrupted' in line or 'Application pause' in line for line in lines))
        return lines

    def test_owner_sees_and_journal_keeps_each_audited_cause(self):
        # Each rejection must have an actionable cause in the response and journal.
        cases = (
            ('prepare', UpdateError('update_insufficient_disk_space'), 'update_insufficient_disk_space', False),
            ('drain', UpdateError('update_collection_stop_unconfirmed'), 'update_collection_stop_unconfirmed', False),
            ('stop_backend', UpdateError('update_backend_stop_not_clean'), 'update_backend_stop_not_clean', True),
            ('schema', UpdateError('update_schema_not_completed'), 'update_schema_not_completed', True),
            ('health', UpdateError('update_runtime_health_failed'), 'update_runtime_health_failed', True),
        )
        for action, error, code, paused in cases:
            with self.subTest(action=action):
                self.assert_cause(CauseHost(action, error), code, paused=paused)

    def test_command_failures_without_own_code_are_named_by_their_step(self):
        # A taken port makes `compose up` fail with only an exit status (LIVE-OPS 5b).
        cases = (
            ('prepare', InstallError('command_failed_1'), 'update_preparation_failed', False),
            ('start_backend', InstallError('command_failed_1'), 'update_backend_start_failed', True),
            ('start_database', InstallError('command_failed_125'), 'update_schema_step_failed', True),
            ('schema', subprocess.TimeoutExpired(['docker', SECRET], 240), 'update_schema_step_failed', True),
            ('health', subprocess.TimeoutExpired(['docker', SECRET], 15), 'update_runtime_health_failed', True),
            ('drain', subprocess.CalledProcessError(1, ['docker', SECRET]), 'update_collection_pause_failed', False),
            ('copy', OSError(5, SECRET), 'update_copy_failed', True),
            ('exchange', OSError(1, SECRET), 'update_file_swap_failed', True),
            ('prepare', OSError(errno.ENOSPC, SECRET), 'update_insufficient_disk_space', False),
            ('copy', OSError(errno.ENOSPC, SECRET), 'update_insufficient_disk_space', True),
        )
        for action, error, code, paused in cases:
            with self.subTest(action=action, error=type(error).__name__):
                self.assert_cause(CauseHost(action, error), code, paused=paused)

    def test_fixed_codes_of_other_product_errors_are_kept(self):
        for error in (SchemaError('schema_result_invalid'), ReleaseError('release_image_pull_failed'),
                      JournalError('update_lock_changed'), UpdateCopyError('update_copy_window_unavailable'),
                      InstallError('docker_network_address_pool_exhausted')):
            with self.subTest(error=type(error).__name__):
                host = CauseHost('copy', error)
                self.run_update(host)
                self.assertEqual(host.saves[-1]['failure_code'], str(error))

    def test_unknown_errors_and_interrupts_get_generic_codes_without_their_text(self):
        cases = ((ValueError(SECRET), messages.UNEXPECTED), (TypeError(SECRET), messages.UNEXPECTED),
                 # Shaped like a code, but not from a product error class: never recorded.
                 (ValueError('aiza_private_token'), messages.UNEXPECTED),
                 (RuntimeError('command_failed_1'), messages.UNEXPECTED),
                 (KeyboardInterrupt(), messages.STOPPED_BY_USER))
        for error, code in cases:
            with self.subTest(error=repr(error)):
                host = CauseHost('start_backend', error)
                lines = self.assert_cause(host, code, paused=True)
                self.assertNotIn('aiza_private_token', '\n'.join(lines) + json.dumps(host.saves))

    def test_cause_is_saved_before_any_recovery_effect(self):
        host = CauseHost('start_backend', InstallError('command_failed_1'))
        self.run_update(host)
        first = next(index for index, value in enumerate(host.saves) if value['failure_code'])
        self.assertEqual(host.saves[first]['phase'], 'new_start_intent')
        self.assertEqual(host.saves[first - 1], dict(host.saves[first], failure_code=None))
        self.assertEqual(host.events[host.events.index('start_backend') + 1], 'stop_helpers')

    def test_unsaved_cause_does_not_block_recovery(self):
        host = CauseHost('copy', UpdateError('update_copy_verification_mismatch'), save_errors=1)
        outcome, lines = self.run_update(host)
        self.assertEqual(outcome, 'update_failed_previous_version_recovered')
        self.assertEqual(host.saves[-1], dict(host.saves[-1], phase='rolled_back',
                                              failure_code='update_copy_verification_mismatch'))
        self.assertEqual(lines[-1], 'Cause: update_copy_verification_mismatch.')

    def test_failed_recovery_still_names_the_cause(self):
        host = CauseHost('start_backend', InstallError('command_failed_1'),
                         recovery_error=UpdateError('update_restore_failed'))
        outcome, lines = self.run_update(host)
        self.assertEqual(outcome, 'update_recovery_failed_copy_deadline_still_applies')
        self.assertEqual(host.saves[-1]['phase'], 'rollback_restore_intent')
        self.assertEqual(host.saves[-1]['failure_code'], 'update_backend_start_failed')
        self.assertEqual(lines[-1], messages.cause_line('update_backend_start_failed'))

    def test_later_recovery_of_an_interrupted_update_prints_its_saved_cause(self):
        lines = []
        host = CauseHost(None, None)
        journal = {'phase': 'rollback_restore_intent', 'stopped_at': 50, 'failure_code': 'update_backend_start_failed'}
        self.assertTrue(UpdateEngine(host, journal, clock=lambda: 100, emit=lines.append).recover())
        self.assertEqual(host.saves[-1]['failure_code'], 'update_backend_start_failed')
        self.assertEqual(lines[-2:], ['Previous version recovered. Application pause: 50 seconds.',
                                      messages.cause_line('update_backend_start_failed')])
        # Without a recorded cause (older journal, killed process) nothing is claimed.
        lines.clear()
        UpdateEngine(CauseHost(None, None), {'phase': 'copy_intent', 'stopped_at': 50, 'failure_code': None},
                     clock=lambda: 100, emit=lines.append).recover()
        self.assertFalse(any(line.startswith('Cause:') for line in lines))
        self.assertFalse(UpdateEngine(CauseHost(None, None), dict(journal, phase='rolled_back'),
                                      emit=lines.append).recover())

    def test_success_and_failed_cleanup_after_health_record_no_cause(self):
        for action in (None, 'cleanup'):
            with self.subTest(action=action):
                host = CauseHost(action, InstallError('command_failed_1'))
                outcome, lines = self.run_update(host)
                self.assertEqual(outcome, 'updated')
                self.assertEqual(host.saves[-1]['phase'], 'complete')
                self.assertIsNone(host.saves[-1]['failure_code'])
                self.assertFalse(any(line.startswith('Cause:') for line in lines))
                self.assertEqual(lines[-1], 'Update completed. Application pause: 0 seconds.')

    def test_every_cause_code_fits_the_journal_and_every_step_has_a_hint(self):
        value = {'format': 'leti-cook-update', 'version': 1, 'operation_id': 'a' * 32,
                 'project': 'leti-cook-0123456789ab', 'database_id': '9a2fc4bc-9d4a-4bc4-b4be-69c7d37f2d23',
                 'phase': 'preparing', 'old_version': '0.1.0', 'new_version': '0.2.0',
                 'old_tree': {'device': 1, 'inode': 2}, 'new_tree': None,
                 'old_images': {'db': 'sha256:' + '1' * 64, 'backend': 'sha256:' + '2' * 64},
                 'new_images': None, 'old_volume': 'leti-cook-0123456789ab_database-data',
                 'restore_volume': None, 'runtime_epoch': None, 'started_at': 2000000000,
                 'stopped_at': None, 'copy': None, 'failure_code': None}
        codes = set(messages.HINTS) | set(messages.STEP_CAUSES.values())
        self.assertEqual(set(messages.STEP_CAUSES.values()) - set(messages.HINTS), set())
        for code in codes:
            with self.subTest(code=code):
                self.assertEqual(update_journal.validate_journal(dict(value, failure_code=code))['failure_code'], code)
                self.assertTrue(messages.HINTS[code].isascii())
        self.assertEqual(messages.cause_line('update_lock_changed'), 'Cause: update_lock_changed.')

    def test_engine_does_not_import_messages_after_the_tree_exchange(self):
        source = (Path(__file__).resolve().parents[1] / 'ops/update_engine.py').read_text()
        body = source.split('class UpdateEngine', 1)[1]
        self.assertNotIn('import ', body)


if __name__ == '__main__':
    unittest.main()
