"""Checked Docker boundaries; no daemon, network, keys or real installation."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import manage
from install import InstallError
from ops.release_images import ensure_release_image, MAX_LIST_BYTES, MAX_LIST_ROWS
from ops.update_host import Host, UpdateError, runtime_override, healthy_admission
from ops.updater import management

PROJECT = 'leti-cook-0123456789ab'
OP = 'a' * 32
IMAGE = 'sha256:' + 'b' * 64


class HostTests(unittest.TestCase):
    def setUp(self):
        self.layout = SimpleNamespace(installation=Path('/opt/leti-cook-example'), project=PROJECT)
        self.host = Host(self.layout, pause=lambda delay: None)
        self.value = {'operation_id': OP, 'runtime_epoch': 'c' * 32}

    def status(self, *, observed=OP, active=False, outcome='idle', epoch='c' * 32, setup=False):
        return {'runtime_epoch': epoch, 'setup_active': setup,
                'hot': {name: {'allowed': True, 'reason': 'ready'} for name in ('user_work', 'collection', 'api_read')},
                'collector': {'observed_operation_id': observed, 'active': active, 'outcome': outcome}}

    def test_schema_ready_does_not_override_unsafe_clock_or_admission(self):
        for reason in ('time_mismatch', 'clock_changed', 'schema_invalid', 'database_unavailable', 'update_hold_invalid'):
            status = self.status()
            status['hot']['collection'] = {'allowed': False, 'reason': reason}
            with self.assertRaises(UpdateError):
                healthy_admission(status, held=True, startup=True)
        status = self.status()
        status['hot']['api_read'] = {'allowed': False, 'reason': 'startup_cleanup'}
        status['hot']['collection'] = {'allowed': False, 'reason': 'update_hold'}
        healthy_admission(status, held=True, startup=True)
        with self.assertRaises(UpdateError):
            healthy_admission(status, held=True)

    def test_drain_requires_supervisor_ack_not_just_hold(self):
        self.host.status = Mock(side_effect=[self.status(observed=None), self.status(active=True, outcome='draining'),
                                            self.status(outcome='normal')])
        self.assertEqual(self.host.drain(self.value), 'c' * 32)
        self.assertEqual(self.host.status.call_count, 3)

    def test_forced_exit_restart_and_active_setup_never_confirm_drain(self):
        for status in (self.status(outcome='forced'), self.status(outcome='unconfirmed'),
                       self.status(epoch='d' * 32), self.status(setup=True)):
            self.host.status = Mock(return_value=status)
            with self.assertRaises(UpdateError):
                self.host.drain(self.value)

    def test_stopping_disables_restart_before_docker_stop(self):
        self.host.containers = Mock(return_value={'db': {'Id': 'd' * 64}})
        self.host.docker = Mock(return_value='')
        self.host.inspect = Mock(return_value={'State': {'Running': False, 'ExitCode': 0}})
        self.host.stop_service('db')
        self.assertEqual(self.host.docker.call_args_list[0].args[0][:2], ['update', '--restart=no'])
        self.assertEqual(self.host.docker.call_args_list[1].args[0][0], 'stop')

    def test_unclean_stop_cannot_be_copied_but_can_be_stopped_for_restore(self):
        self.host.containers = Mock(return_value={'db': {'Id': 'd' * 64}})
        self.host.docker = Mock(return_value='')
        self.host.inspect = Mock(return_value={'State': {'Running': False, 'ExitCode': 137}})
        with self.assertRaises(UpdateError):
            self.host.stop_service('db')
        self.host.stop_service('db', clean=False)

    def test_foreign_helper_is_not_killed(self):
        self.host.docker = Mock(return_value='d' * 64)
        self.host.inspect = Mock(return_value={'Name': '/' + self.host.helper_name(self.value),
                                              'Config': {'Labels': {}}})
        with self.assertRaisesRegex(UpdateError, 'not_owned'):
            self.host.stop_helpers(self.value)
        self.assertFalse(any(c.args[0][0] == 'rm' for c in self.host.docker.call_args_list))

    def test_volume_needs_exact_project_logical_name_and_plain_local_driver(self):
        correct = {'Labels': {'com.docker.compose.project': PROJECT,
                              'com.docker.compose.volume': 'database-data'}, 'Driver': 'local', 'Options': None}
        self.host.inspect = Mock(return_value=correct)
        self.host.volume(PROJECT + '_database-data')
        for field, invalid in (('Driver', 'nfs'), ('Options', {'device': '/foreign'}), ('Labels', {})):
            self.host.inspect = Mock(return_value=dict(correct, **{field: invalid}))
            with self.assertRaises(UpdateError):
                self.host.volume(PROJECT + '_database-data')

    def test_volume_with_running_writer_cannot_be_restored_or_removed(self):
        self.host.volume = Mock()
        self.host.docker = Mock(return_value='container')
        with self.assertRaisesRegex(UpdateError, 'writer'):
            self.host.remove_volume(PROJECT + '_database-data')
        self.assertEqual(self.host.docker.call_count, 1)

    def test_override_never_accepts_mutable_image_tags(self):
        with self.assertRaises(UpdateError):
            runtime_override({'db': 'postgres:17', 'backend': IMAGE}, PROJECT + '_database-data')
        self.assertEqual(runtime_override({'db': IMAGE, 'backend': IMAGE}, 'volume')['services']['db']['image'], IMAGE)

    def test_authoritative_window_uses_captured_image_and_private_network(self):
        self.value['old_images'] = {'backend': IMAGE}
        self.host.containers = Mock(return_value={
            'backend': {'Id': 'backend', 'State': {'Running': False}},
            'db': {'Id': 'db', 'NetworkSettings': {'Networks': {'private': {}}}}})
        self.host.inspect = Mock(return_value={'Internal': True, 'Labels': {'com.docker.compose.project': PROJECT},
                                              'Containers': {'db': {}}})
        self.host.helper = Mock(return_value='{}')
        self.host.window(self.value)
        call = self.host.helper.call_args
        self.assertEqual(call.args[1], IMAGE)
        self.assertEqual(call.kwargs['network'], 'private')
        self.assertEqual(call.kwargs['environment_file'], self.layout.installation / 'backend.env')
        self.host.inspect.return_value['Containers']['foreign'] = {}
        with self.assertRaisesRegex(UpdateError, 'shared'):
            self.host.window(self.value)

    def test_runtime_version_and_baseline_are_checked_before_preparation(self):
        self.host.plan = SimpleNamespace(port=8101, current_version='0.1.0',
            installed_schema=SimpleNamespace(schema_version=25, baseline_id='leti-cook-release-01'))
        self.host.compose = Mock(return_value='{}')
        self.host.containers = Mock(return_value={
            'backend': {'Id': 'backend', 'State': {'Running': True}, 'Image': IMAGE},
            'db': {'Id': 'db', 'State': {'Running': True}, 'Image': IMAGE,
                   'Mounts': [{'Destination': '/var/lib/postgresql/data', 'Type': 'volume', 'Name': PROJECT + '_database-data'}]}})
        self.host.volume = Mock()
        self.host.docker = Mock(return_value='db')
        good = dict(self.status(), hold={'state': 'absent'}, release_version='0.1.0',
                    schema={'ready': True, 'version': 25, 'baseline_id': 'leti-cook-release-01'})
        for field in ('release_version', 'baseline_id'):
            bad = dict(good, schema=dict(good['schema']))
            if field == 'release_version':
                bad[field] = '9.9.9'
            else:
                bad['schema'][field] = 'foreign'
            self.host.status = Mock(return_value=bad)
            with patch('ops.update_host.os.name', 'posix'), patch('ops.update_host.os.geteuid', return_value=0, create=True), \
                    patch('ops.update_host.check_docker'), patch('ops.update_host.check_time_sync'), \
                    patch('ops.update_host.validate_composed'), self.assertRaisesRegex(UpdateError, 'not_ready'):
                self.host.preflight()


class HostReadinessTests(unittest.TestCase):
    def setUp(self):
        self.layout = SimpleNamespace(installation=Path('/opt/leti-cook-example'), project=PROJECT)
        plan = SimpleNamespace(port=8101, current_version='0.1.0',
            installed_schema=SimpleNamespace(schema_version=25, baseline_id='leti-cook-release-01'))
        self.host = Host(self.layout, plan=plan, clock=lambda: 1791547200)
        self.host.containers = Mock(return_value={
            'backend': {'Id': 'backend', 'State': {'Running': True}, 'Image': IMAGE},
            'db': {'Id': 'db', 'State': {'Running': True}, 'Image': IMAGE,
                   'Mounts': [{'Destination': '/var/lib/postgresql/data', 'Type': 'volume',
                               'Name': PROJECT + '_database-data'}]}})
        self.host.volume = Mock()
        self.host.docker = Mock(return_value='db')
        self.host.status = Mock(return_value={
            'runtime_epoch': 'c' * 32, 'setup_active': False, 'hold': {'state': 'absent'},
            'release_version': '0.1.0',
            'schema': {'ready': True, 'version': 25, 'baseline_id': 'leti-cook-release-01'},
            'hot': {name: {'allowed': True, 'reason': 'ready'}
                    for name in ('user_work', 'collection', 'api_read')}})
        self.window = {'as_of': '2026-10-09T12:00:00+00:00',
                       'oldest_source_at': '2026-10-09T11:00:00+00:00',
                       'expired': False, 'invalid_source_clock': False}
        self.host.compose = Mock(side_effect=lambda args, **kwargs:
            '{}' if args == ['config', '--format', 'json'] else json.dumps(self.window))
        for target, kwargs in (
                ('os.name', {'new': 'posix'}),
                ('os.geteuid', {'return_value': 0, 'create': True}),
                ('check_docker', {}), ('check_time_sync', {}), ('validate_composed', {})):
            patcher = patch('ops.update_host.' + target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('ops.update_host.journal.preflight_exchange')
        self.exchange = patcher.start()
        self.addCleanup(patcher.stop)

    def test_readiness_reads_runtime_and_copy_window_without_filesystem_probe(self):
        result = self.host.check_readiness()
        self.assertEqual(result['old_volume'], PROJECT + '_database-data')
        self.host.status.assert_called_once_with()
        self.assertEqual([call.args[0] for call in self.host.compose.call_args_list], [
            ['config', '--format', 'json'],
            ['exec', '-T', 'backend', 'python', 'ops/update_copy_window.py']])
        self.assertEqual(self.host.docker.call_args.args[0][:2], ['ps', '-aq'])
        self.exchange.assert_not_called()

    def test_unconfirmed_window_is_refused_during_readiness_without_exchange(self):
        self.window['invalid_source_clock'] = True
        with self.assertRaisesRegex(ValueError, '^update_copy_window_unconfirmed$'):
            self.host.check_readiness()
        self.exchange.assert_not_called()

    def test_actual_update_still_requires_exchange_after_readiness(self):
        result = self.host.preflight()
        self.assertEqual(result['runtime_epoch'], 'c' * 32)
        self.exchange.assert_called_once_with(self.layout)


class HostImagePreparationTests(unittest.TestCase):
    def setUp(self):
        self.layout = SimpleNamespace(installation=Path('/opt/leti-cook-example'),
                                      root=Path('/opt/leti-cook-example.update'), project=PROJECT)
        self.plan = SimpleNamespace(target_version='0.2.0', installed_fingerprint='captured')
        self.release = SimpleNamespace(manifest={'images': {
            name: {'repository': 'ghcr.io/example/leti-cook-' + name, 'digest': 'sha256:' + char * 64}
            for name, char in (('db', 'd'), ('backend', 'e'))}})
        self.references = [self.release.manifest['images'][name]['repository'] + '@' +
                           self.release.manifest['images'][name]['digest'] for name in ('db', 'backend')]

    def prepare(self, run):
        host = Host(self.layout, plan=self.plan, release=self.release, run=run)
        host.check_space = Mock()
        prepared = SimpleNamespace(path=self.layout.root / 'candidate')
        with patch('ops.update_candidate.prepare_candidate', return_value=prepared), \
                patch('ops.update_candidate.preflight_candidate', return_value=self.plan), \
                patch('ops.update_host.journal.tree_identity', return_value='identity'), \
                patch('ops.update_host.write_override') as write:
            result = host.prepare({'operation_id': OP, 'old_volume': PROJECT + '_database-data'})
            write.assert_called_once_with(prepared.path, result['new_images'], PROJECT + '_database-data')
            return result

    def details(self, reference, **changes):
        return json.dumps({'Id': IMAGE, 'Os': 'linux', 'Architecture': 'amd64',
                           'RepoDigests': [reference], **changes})

    def test_prepare_uses_both_exact_cached_digests_without_any_registry_call(self):
        def run(args, **kwargs):
            if args == ['docker', 'info', '--format', '{{json .Architecture}}']:
                return '"amd64"'
            self.assertEqual(args[:5], ['docker', 'image', 'inspect', '--format', '{{json .}}'])
            self.assertIn(args[-1], self.references)
            return self.details(args[-1])
        run = Mock(side_effect=run)
        self.assertEqual(self.prepare(run)['new_images'], {'db': IMAGE, 'backend': IMAGE})
        self.assertEqual(len(run.call_args_list), 3)

    def test_prepare_pulls_only_missing_digest_after_successful_local_absence_proof(self):
        pulled = set()
        missing = self.references[1]
        def run(args, **kwargs):
            if args[1] == 'info':
                return '"x86_64"'
            if args[1:3] == ['image', 'inspect']:
                if args[-1] == missing and missing not in pulled:
                    raise InstallError('command_failed_1')
                return self.details(args[-1])
            if args[1:3] == ['image', 'ls']:
                self.assertEqual(args[-1], missing.split('@')[0])
                self.assertIn('--all', args)
                return json.dumps({'Repository': args[-1], 'Digest': 'sha256:' + 'f' * 64})
            if args[1] == 'pull':
                self.assertEqual(args, ['docker', 'pull', '--platform=linux/amd64', missing])
                self.assertEqual(kwargs['timeout'], 900)
                pulled.add(missing)
                return ''
            self.fail(args)
        run = Mock(side_effect=run)
        self.assertEqual(self.prepare(run)['new_images'], {'db': IMAGE, 'backend': IMAGE})
        self.assertEqual(pulled, {missing})
        actions = [call.args[0][1:3] for call in run.call_args_list]
        self.assertEqual(actions, [['info', '--format'], ['image', 'inspect'], ['image', 'inspect'],
                                  ['image', 'ls'], ['pull', '--platform=linux/amd64'], ['image', 'inspect']])

    def test_bad_cached_identity_fails_without_pulling_or_repairing_cache(self):
        for changes in ({'Os': 'windows'}, {'Architecture': 'arm64'}, {'RepoDigests': []},
                        {'RepoDigests': self.references[0]}, {'Id': 'untrusted:latest'}):
            with self.subTest(changes=changes):
                run = Mock(side_effect=lambda args, **kw: '"amd64"' if args[1] == 'info'
                           else self.details(self.references[0], **changes))
                with self.assertRaisesRegex(UpdateError, 'update_image_identity_invalid'):
                    self.prepare(run)
                self.assertEqual([call.args[0][1] for call in run.call_args_list], ['info', 'image'])

    def test_failed_inspection_with_present_digest_never_pulls(self):
        reference = self.references[0]
        run = Mock(side_effect=[InstallError('command_failed_1'), json.dumps({
            'Repository': reference.split('@')[0], 'Digest': reference.split('@')[1]})])
        with self.assertRaisesRegex(InstallError, 'release_image_cache_unavailable'):
            ensure_release_image(reference, run)
        self.assertEqual([call.args[0][1:3] for call in run.call_args_list], [['image', 'inspect'], ['image', 'ls']])

    def test_daemon_failure_or_other_inspect_failure_is_not_absence(self):
        for errors, calls in (([InstallError('command_failed_1'), InstallError('command_failed_1')], 2),
                              ([InstallError('command_failed_125')], 1)):
            run = Mock(side_effect=errors)
            with self.assertRaises(InstallError):
                ensure_release_image(self.references[0], run)
            self.assertEqual(run.call_count, calls)
            self.assertFalse(any(call.args[0][1] == 'pull' for call in run.call_args_list))

    def test_malformed_or_excessive_local_listing_never_causes_registry_access(self):
        repo = self.references[0].split('@')[0]
        for raw in ('not json', '[]', json.dumps({'Repository': repo}),
                    json.dumps({'Repository': 'other', 'Digest': '<none>'}),
                    json.dumps({'Repository': repo, 'Digest': 'bad'}),
                    'x' * (MAX_LIST_BYTES + 1), '\n' * (MAX_LIST_ROWS + 1)):
            with self.subTest(size=len(raw)):
                run = Mock(side_effect=[InstallError('command_failed_1'), raw])
                with self.assertRaisesRegex(InstallError, 'release_image_cache_unavailable'):
                    ensure_release_image(self.references[0], run)
                self.assertEqual(run.call_count, 2)

    def test_newly_pulled_image_is_still_verified(self):
        run = Mock(side_effect=[InstallError('command_failed_1'), '', '',
                               self.details(self.references[0], RepoDigests=[])])
        with self.assertRaisesRegex(InstallError, 'release_image_identity_invalid'):
            ensure_release_image(self.references[0], run)
        self.assertEqual(run.call_args_list[2].args[0],
                         ['docker', 'pull', '--platform=linux/amd64', self.references[0]])


class ManageRecoveryTests(unittest.TestCase):
    def test_pending_recovery_happens_inside_lock_before_dispatch(self):
        events = []
        @contextlib.contextmanager
        def lock(layout):
            events.append('locked')
            yield
            events.append('unlocked')
        engine = Mock()
        engine.recover.side_effect = lambda: events.append('recovered') or True
        with patch('ops.updater.layout_for', return_value='layout'), patch('ops.updater.journal.update_lock', lock), \
                patch('ops.updater.journal.load_journal', return_value={'phase': 'swap_intent'}), \
                patch('ops.updater.Host'), patch('ops.updater.UpdateEngine', return_value=engine), contextlib.redirect_stdout(io.StringIO()):
            with management(Path('/opt/app'), {}) as (_, recovered):
                self.assertTrue(recovered)
                events.append('yield')
        self.assertEqual(events, ['locked', 'recovered', 'yield', 'unlocked'])

    def test_recovered_manage_does_not_dispatch_using_stale_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'installed.json').write_text(json.dumps({'project': PROJECT}))
            with patch.object(manage, '__file__', str(root / 'manage.py')), \
                    patch.object(sys, 'argv', ['manage.py', 'restart']), \
                    patch('ops.updater.management', lambda *a: contextlib.nullcontext((None, True))), \
                    patch.object(manage, '_dispatch') as dispatch, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(manage.main(), 0)
                dispatch.assert_not_called()

    def test_expiry_purge_is_not_blocked_by_long_update_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'installed.json').write_text(json.dumps({'project': PROJECT}))
            with patch.object(manage, '__file__', str(root / 'manage.py')), \
                    patch.object(sys, 'argv', ['manage.py', 'purge-update-copy']), \
                    patch('ops.updater.management', side_effect=AssertionError('must remain independent')), \
                    patch.object(manage, 'purge_update_copy', return_value=0):
                self.assertEqual(manage.main(), 0)


if __name__ == '__main__':
    unittest.main()
