"""Operation intents survive failures and abrupt process death at every boundary."""
import unittest

from ops.update_engine import UpdateEngine, UpdateError


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


if __name__ == '__main__':
    unittest.main()
