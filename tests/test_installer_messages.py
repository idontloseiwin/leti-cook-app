"""Release-readiness CLI diagnostics with local fakes only; no Docker/provider I/O."""
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT)]
import install
import manage
from ops import release_install, self_hosted_owner, updater
from db.self_hosted_auth import AuthStoreError, OwnerAuthRepository


class DockerDiagnosticTests(unittest.TestCase):
    def test_exhausted_pool_has_fixed_reason_without_raw_output_or_extra_commands(self):
        secret = 'private-command-output-do-not-print'
        result = subprocess.CompletedProcess(['docker'], 1, secret,
            'Error response from daemon: all predefined address pools have been fully subnetted\n' + secret)
        output, error = StringIO(), StringIO()
        with patch.object(install.subprocess, 'run', return_value=result) as run, \
                redirect_stdout(output), redirect_stderr(error):
            with self.assertRaisesRegex(install.InstallError, '^docker_network_address_pool_exhausted$'):
                install.command(['docker', '--host', 'unix:///run/docker.sock', 'compose', 'up', '-d'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(output.getvalue() + error.getvalue(), '')
        hint = install.REASON_HINTS['docker_network_address_pool_exhausted']
        self.assertIn('Review unused Docker networks', hint)
        self.assertIn('does not remove networks automatically', hint)
        self.assertNotIn(secret, hint)

    def test_other_failures_are_generic_and_success_is_not_reclassified(self):
        phrase = 'all predefined address pools have been fully subnetted'
        for arguments, stderr in ((['docker', 'info'], 'private failure'), (['other'], phrase)):
            with self.subTest(arguments=arguments), patch.object(install.subprocess, 'run',
                    return_value=subprocess.CompletedProcess(arguments, 2, 'private', stderr)):
                with self.assertRaisesRegex(install.InstallError, '^command_failed_2$'):
                    install.command(arguments)
        with patch.object(install.subprocess, 'run',
                return_value=subprocess.CompletedProcess(['docker'], 0, 'ok', phrase)):
            self.assertEqual(install.command(['docker', 'info']), 'ok')


MISSING = object()
REQUIRED, UNKNOWN = 'docker_engine_28_3_3_required', 'docker_engine_version_unknown'


def docker_answers(server_version=MISSING, compose='2.24.4'):
    """A local Docker whose `docker info` reports this Engine version."""
    info = {'OSType': 'linux'}
    if server_version is not MISSING:
        info['ServerVersion'] = server_version

    def run(args, **options):
        if args == ['docker', 'compose', 'version', '--short']:
            return compose + '\n'
        if args[:3] == ['docker', 'context', 'inspect']:
            return 'unix:///var/run/docker.sock\n'
        if args == ['docker', 'info', '--format', '{{json .}}']:
            return json.dumps(info)
        raise AssertionError(args)
    return run


def local_docker():
    stack = ExitStack()
    stack.enter_context(patch.object(install.sys, 'platform', 'linux'))
    stack.enter_context(patch.dict(install.os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock'}))
    install.os.environ.pop('DOCKER_CONTEXT', None)
    return stack


class DockerEngineVersionTests(unittest.TestCase):
    """Older Engines let the local network reach ports published on 127.0.0.1."""

    def check(self, version, *, engine='require', compose='2.24.4'):
        error = StringIO()
        with local_docker(), redirect_stderr(error):
            try:
                info = install.check_docker(docker_answers(version, compose), engine=engine)
            except install.InstallError as refusal:
                return str(refusal), error.getvalue()
        self.assertEqual(info.get('ServerVersion', MISSING), version)
        return None, error.getvalue()

    def test_engine_older_than_28_3_3_is_refused_and_odd_versions_are_read_safely(self):
        for version, expected in (
                ('27.5.1', REQUIRED), ('28.3.2', REQUIRED), ('28.0.4', REQUIRED), ('20.10.24+dfsg1', REQUIRED),
                ('1.13.1', REQUIRED), ('28.3.3', None), ('29.6.1', None), ('28.5.1+dfsg1', None), ('28.10.0', None),
                ('v29.0.0', None), ('29.0.0-rc.1', None), (' 28.3.3\n', None), ('', UNKNOWN), ('dev', UNKNOWN),
                ('28.3', UNKNOWN), (MISSING, UNKNOWN), (None, UNKNOWN), (2833, UNKNOWN), (['28.3.3'], UNKNOWN),
                ('٢٨.٣.٣', UNKNOWN), ('9' * 5000 + '.0.0', UNKNOWN), ('28.3.1234567', UNKNOWN)):
            with self.subTest(version=str(version)[:20]):
                reason, error = self.check(version)
                self.assertEqual(reason, expected)
                if expected is None:
                    self.assertEqual(error, '')
                    continue
                self.assertEqual(error.count('\n'), 1, error)
                self.assertIn('; Leti Cook needs Docker Engine 28.3.3 or newer. Older Engines let other computers on '
                              'the local network reach ports published on 127.0.0.1', error)
                if expected == REQUIRED:
                    self.assertTrue(error.startswith('Docker Engine ' + version.strip() + ' is installed;'), error)

    def test_the_terminal_shows_only_a_plain_version_text(self):
        for version, shown in (('27.0.0\x1b[2J', 'Docker Engine 27.0.0 is installed;'),
                               ('nightly', 'Docker reports Engine version "nightly", which has no version number;'),
                               ('dev\x1b]0;title\x07', 'Docker did not report a readable Engine version;'),
                               ('x' * 65, 'Docker did not report a readable Engine version;')):
            with self.subTest(version=version[:20]):
                _, error = self.check(version)
                self.assertTrue(error.startswith(shown), error)
                self.assertNotIn('\x1b', error)
                self.assertNotIn('x' * 65, error)

    def test_warn_mode_names_the_version_and_lets_the_command_run(self):
        for version, found in (('27.5.1', 'Docker Engine 27.5.1 is installed;'),
                               ('dev', 'Docker reports Engine version "dev"')):
            with self.subTest(version=version):
                reason, error = self.check(version, engine='warn')
                self.assertIsNone(reason)
                self.assertTrue(error.startswith('Warning: ' + found), error)
                self.assertEqual(error.count('\n'), 1, error)
                self.assertIn('127.0.0.1', error)
                self.assertTrue(error.endswith(install.DOCKER_ENGINE_INSTALL + ' Updates are refused until then.\n'))
        self.assertEqual(self.check('29.6.1', engine='warn'), (None, ''))
        # Only the Engine version is relaxed; every other Docker check still refuses.
        self.assertEqual(self.check('29.6.1', engine='warn', compose='2.20.0'), ('compose_2_24_4_required', ''))

    def test_each_refusal_says_where_to_get_a_current_engine(self):
        for reason in (REQUIRED, UNKNOWN):
            self.assertIn('https://docs.docker.com/engine/install/', install.REASON_HINTS[reason])
            self.assertIn('run the installer again', install.REASON_HINTS[reason])

    def run_entry(self, signed):
        real = install.check_docker
        stdout, stderr = StringIO(), StringIO()
        with ExitStack() as stack:
            stack.enter_context(local_docker())
            stack.enter_context(patch.object(install.os, 'geteuid', return_value=0, create=True))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(install, 'check_docker',
                side_effect=lambda *args, **options: real(docker_answers('27.5.1'), **options)))
            untouched = [stack.enter_context(patch.object(install, name)) for name in
                         ('check_time_sync', 'choose_plan', 'hidden_api_key')]
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            if signed:
                verified = SimpleNamespace(manifest={'version': '1.0.0'}, files={})
                stack.enter_context(patch.object(release_install, 'load_source', return_value=object()))
                stack.enter_context(patch.object(release_install, 'verify_release', return_value=verified))
                untouched.append(stack.enter_context(patch.object(release_install, 'install_verified_release')))
                status = release_install.main(['--package', 'unused'])
            else:
                stack.enter_context(patch.object(sys, 'argv', ['install.py', '--install']))
                stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {})))
                untouched.append(stack.enter_context(patch.object(install, 'install')))
                status = install.main()
        return status, stdout.getvalue(), stderr.getvalue(), untouched

    def test_both_installers_stop_on_an_old_engine_before_any_question(self):
        for signed in (False, True):
            with self.subTest(signed=signed):
                status, stdout, stderr, untouched = self.run_entry(signed)
                self.assertEqual(status, 1)
                self.assertNotIn('Docker checks passed.', stdout)
                lines = stderr.splitlines()
                self.assertTrue(lines[0].startswith('Docker Engine 27.5.1 is installed; Leti Cook needs Docker '
                                                    'Engine 28.3.3 or newer.'), stderr)
                self.assertEqual(lines[1:3], ['reason=' + REQUIRED, install.REASON_HINTS[REQUIRED]])
                self.assertTrue(lines[3].startswith('Installation stopped.'))
                for mock in untouched:
                    mock.assert_not_called()


class InstallerPathOrderTests(unittest.TestCase):
    def test_interactive_folder_storage_refusal_reprompts_with_existing_hint(self):
        ask = Mock(side_effect=['/tmp/new', '/opt/new', ''])
        lines = []
        with patch.object(install, 'new_directory', side_effect=Path), \
                patch.object(install, 'check_update_copy_storage',
                    side_effect=[install.InstallError('update_copy_storage_unavailable'), None]) as storage:
            plan = install.choose_plan(SimpleNamespace(directory=None, port=None),
                                       ask=ask, emit=lines.append, available=lambda port: True)
        self.assertEqual(plan['directory'], Path('/opt/new'))
        self.assertEqual(storage.call_count, 2)
        self.assertIn('Invalid choice: update_copy_storage_unavailable', lines)
        self.assertIn(install.REASON_HINTS['update_copy_storage_unavailable'], lines)
        self.assertEqual(ask.call_count, 3)

    def entry(self, signed, storage_error=None):
        order = []
        stdout, stderr = StringIO(), StringIO()
        def validate_storage(directory):
            order.append('storage')
            if storage_error is not None:
                raise storage_error
        with ExitStack() as stack:
            stack.enter_context(patch.object(install.os, 'geteuid', return_value=0, create=True))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(install, 'check_docker', return_value={}))
            stack.enter_context(patch.object(install, 'check_time_sync'))
            stack.enter_context(patch.object(install, 'new_directory', side_effect=Path))
            storage = stack.enter_context(patch('ops.update_copy_store.validate_new_layout',
                side_effect=validate_storage))
            stack.enter_context(patch.object(install, 'print_plan'))
            stack.enter_context(patch.object(install.os, 'umask'))
            key = stack.enter_context(patch.object(install, 'hidden_api_key',
                side_effect=lambda: order.append('key') or 'synthetic_key_123456789012345'))
            # Folder checks are exercised here; separate tests own port selection.
            stack.enter_context(patch.object(install, 'available_port', return_value=8002))
            stack.enter_context(patch.object(install, 'ask_value', return_value=8002))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            if signed:
                verified = SimpleNamespace(manifest={'version': '1.0.0'}, files={})
                stack.enter_context(patch.object(release_install, 'load_source', return_value=object()))
                stack.enter_context(patch.object(release_install, 'verify_release', return_value=verified))
                create = stack.enter_context(patch.object(release_install, 'install_verified_release', return_value={}))
                status = release_install.main(['--package', 'unused', '--directory', '/tmp/new'])
            else:
                stack.enter_context(patch.object(sys, 'argv', ['install.py', '--install', '--directory', '/tmp/new']))
                stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {})))
                create = stack.enter_context(patch.object(install, 'install', return_value={}))
                status = install.main()
        return status, order, stderr.getvalue(), key, create, storage

    def test_both_entrypoints_refuse_private_path_error_before_key_or_installation(self):
        for signed in (False, True):
            with self.subTest(signed=signed):
                status, order, error, key, create, storage = self.entry(signed, ValueError('update_copy_parent_untrusted'))
                self.assertEqual((status, order), (1, ['storage']))
                self.assertIn('reason=update_copy_storage_unavailable', error)
                self.assertIn('root-owned directory', error)
                key.assert_not_called()
                create.assert_not_called()
                storage.assert_called_once_with(Path('/tmp/new'))

    def test_folder_question_stops_when_no_folder_can_pass_without_root(self):
        # The probe's endless "Invalid choice" loop for every folder.
        ask = Mock(side_effect=['/opt/a', '/opt/b'])
        lines = []
        with patch.object(install, 'new_directory', side_effect=Path), \
                patch('ops.update_copy_store.validate_new_layout', side_effect=ValueError('update_copy_root_required')), \
                self.assertRaisesRegex(install.InstallError, '^root_required_run_with_sudo$'):
            install.choose_plan(SimpleNamespace(directory=None, port=None),
                                ask=ask, emit=lines.append, available=lambda port: True)
        self.assertEqual((ask.call_count, lines), (1, []))

    def run_without_root(self, signed, argv=None):
        stdout, stderr = StringIO(), StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(install.os, 'geteuid', return_value=1000, create=True))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            untouched = [stack.enter_context(patch.object(install, name)) for name in
                         ('check_docker', 'check_time_sync', 'choose_plan', 'hidden_api_key')]
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            if signed:
                verified = SimpleNamespace(manifest={'version': '1.0.0'}, files={})
                untouched += [stack.enter_context(patch.object(release_install, 'load_source')),
                              stack.enter_context(patch.object(release_install, 'fetch_release', return_value=verified)),
                              stack.enter_context(patch.object(release_install, 'verify_release', return_value=verified)),
                              stack.enter_context(patch.object(release_install, 'install_verified_release'))]
                status = release_install.main(argv or ['--package', 'unused'])
            else:
                stack.enter_context(patch.object(sys, 'argv', ['install.py', '--install']))
                stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {})))
                untouched.append(stack.enter_context(patch.object(install, 'install')))
                status = install.main()
        return status, stdout.getvalue(), stderr.getvalue(), untouched

    def test_both_entrypoints_stop_without_root_before_any_check_or_question(self):
        hint = install.REASON_HINTS['root_required_run_with_sudo']
        self.assertIn('sudo bash install.sh', hint)
        self.assertNotIn('root-owned directory', hint)
        for signed in (False, True):
            with self.subTest(signed=signed):
                status, stdout, stderr, untouched = self.run_without_root(signed)
                self.assertEqual((status, stdout), (1, ''))
                self.assertTrue(stderr.startswith('reason=root_required_run_with_sudo\n' + hint + '\n'), stderr)
                self.assertIn('Installation stopped.', stderr)
                self.assertNotIn('Invalid choice', stderr)
                for mock in untouched:
                    mock.assert_not_called()

    def test_release_check_still_needs_no_root(self):
        status, stdout, _, untouched = self.run_without_root(True, ['--check', '--package', 'unused'])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout)['status'], 'verified')
        for mock in untouched[:4]:
            mock.assert_not_called()

    def test_both_entrypoints_validate_storage_before_key_on_valid_path(self):
        for signed in (False, True):
            with self.subTest(signed=signed):
                status, order, _, key, create, _ = self.entry(signed)
                self.assertEqual((status, order), (0, ['storage', 'key']))
                key.assert_called_once()
                create.assert_called_once()


class InstallationLockTests(unittest.TestCase):
    """The installer holds the lock manage.py commands take while the installation is unfinished."""
    PROJECT, DATABASE = 'leti-cook-0123456789ab', '9a2fc4bc-9d4a-4bc4-b4be-69c7d37f2d23'

    def test_installer_takes_the_manage_lock_of_its_own_store(self):
        layout, events = object(), []

        @contextmanager
        def lock(value):
            events.append(('enter', value))
            try:
                yield
            finally:
                events.append('exit')
        directory = Path('/opt/leti-cook-test')
        with patch('ops.update_copy_store.load_layout', return_value=layout) as load, \
                patch('ops.update_journal.update_lock', lock):
            with install.installation_lock(directory, self.PROJECT, self.DATABASE):
                events.append('body')
            # A failure inside the held section passes unchanged and releases the lock.
            with self.assertRaisesRegex(install.InstallError, '^command_failed_1$'):
                with install.installation_lock(directory, self.PROJECT, self.DATABASE):
                    raise install.InstallError('command_failed_1')
        load.assert_called_with(directory, project=self.PROJECT, database_id=self.DATABASE)
        self.assertEqual(events, [('enter', layout), 'body', 'exit', ('enter', layout), 'exit'])

    def test_a_lock_that_cannot_be_taken_stops_before_any_docker_work(self):
        for failure in (ValueError('update_lock_busy'), ValueError('update_copy_scope_invalid'), OSError(13, 'denied')):
            with self.subTest(failure=repr(failure)), \
                    patch('ops.update_copy_store.load_layout', return_value=object()), \
                    patch('ops.update_journal.update_lock', side_effect=failure):
                with self.assertRaisesRegex(install.InstallError, '^update_copy_storage_unavailable$'):
                    with install.installation_lock(Path('/opt/x'), self.PROJECT, self.DATABASE):
                        self.fail('nothing may run without the lock')


class UnfinishedInstallationMessageTests(unittest.TestCase):
    """An installer that stops after creating the folder prints the exact removal command."""

    def run_entry(self, signed, failure, *, receipt=True, installed=False, existing=False):
        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as base, ExitStack() as stack:
            directory = Path(base, 'leti-cook')
            if existing:
                directory.mkdir()
                (directory / 'installation.json').write_text('{}')

            def create(*args, **kwargs):
                if not existing:
                    directory.mkdir()
                    if receipt:
                        (directory / 'installation.json').write_text('{}')
                    if installed:
                        (directory / 'installed.json').write_text('{}')
                raise failure
            stack.enter_context(patch.object(install.os, 'geteuid', return_value=0, create=True))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(install, 'check_docker', return_value={}))
            stack.enter_context(patch.object(install, 'check_time_sync'))
            stack.enter_context(patch.object(install, 'choose_plan', return_value={'directory': directory, 'port': 8002}))
            stack.enter_context(patch.object(install, 'print_plan'))
            stack.enter_context(patch.object(install.os, 'umask'))
            stack.enter_context(patch.object(install, 'hidden_api_key', return_value='synthetic_key_123456789012345'))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            if signed:
                verified = SimpleNamespace(manifest={'version': '1.0.0'}, files={})
                stack.enter_context(patch.object(release_install, 'load_source', return_value=object()))
                stack.enter_context(patch.object(release_install, 'verify_release', return_value=verified))
                stack.enter_context(patch.object(release_install, 'install_verified_release', side_effect=create))
                status = release_install.main(['--package', 'unused'])
            else:
                stack.enter_context(patch.object(sys, 'argv', ['install.py', '--install']))
                stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {})))
                stack.enter_context(patch.object(install, 'install', side_effect=create))
                status = install.main()
        return status, stderr.getvalue(), directory

    def test_stop_after_the_folder_was_created_prints_its_uninstall_command(self):
        for signed in (False, True):
            for failure, code in ((KeyboardInterrupt(), 130), (install.InstallError('command_failed_1'), 1),
                                  (OSError('private'), 1)):
                with self.subTest(signed=signed, failure=type(failure).__name__):
                    status, error, directory = self.run_entry(signed, failure)
                    self.assertEqual(status, code)
                    self.assertTrue(error.endswith('The unfinished installation may keep running. Remove it with: '
                                                   'sudo python3 ' + str(directory / 'manage.py') + ' uninstall\n'),
                                    error)

    def test_no_command_when_this_run_left_no_unfinished_installation(self):
        for signed in (False, True):
            for case in ({'receipt': False}, {'installed': True}, {'existing': True}):
                with self.subTest(signed=signed, case=case):
                    status, error, _ = self.run_entry(signed, install.InstallError('command_failed_1'), **case)
                    self.assertEqual(status, 1)
                    self.assertNotIn('uninstall', error)


class SetupCodeMessageTests(unittest.TestCase):
    def test_completed_password_and_local_modes_issue_no_code_or_access(self):
        for mode in ('password', 'local'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'owner.sqlite3'
                repository = OwnerAuthRepository.initialize(path, 'dummy', 1800000000)
                with repository.transaction() as database:
                    database.execute('UPDATE owner SET access_mode=? WHERE singleton=1', (mode,))
                    repository._session(database, 'kept-session', 1800000000)
                    before = tuple(database.execute('SELECT * FROM owner').fetchone())
                    sessions = [tuple(row) for row in database.execute('SELECT * FROM sessions')]
                terminal = StringIO()
                terminal.isatty = lambda: True
                terminal.close = lambda: None
                output, error = StringIO(), StringIO()
                with patch.object(sys, 'argv', ['owner.py', '--database', str(path), 'setup-code']), \
                        patch.object(sys.stdin, 'isatty', return_value=True), \
                        patch.object(self_hosted_owner, 'open', return_value=terminal, create=True), \
                        redirect_stdout(output), redirect_stderr(error):
                    # manage.py names this status owner_already_created.
                    self.assertEqual(self_hosted_owner.main(), manage.HELPER_REFUSED_STATUS)
                self.assertEqual(self_hosted_owner.EXIT_OWNER_ALREADY_CREATED, manage.HELPER_REFUSED_STATUS)
                self.assertIn('Access setup is already complete.', error.getvalue())
                self.assertIn('No new setup code was issued.', error.getvalue())
                self.assertEqual(output.getvalue() + terminal.getvalue(), '')
                with repository.transaction() as database:
                    self.assertEqual(tuple(database.execute('SELECT * FROM owner').fetchone()), before)
                    self.assertEqual([tuple(row) for row in database.execute('SELECT * FROM sessions')], sessions)

    def test_other_owner_errors_remain_generic(self):
        error = StringIO()
        with patch.object(sys, 'argv', ['owner.py', '--database', 'unused', 'setup-code']), \
                patch.object(self_hosted_owner, 'OwnerAuthRepository', side_effect=AuthStoreError('private-error')), \
                redirect_stderr(error):
            self.assertEqual(self_hosted_owner.main(), 1)
        self.assertIn('Owner operation refused', error.getvalue())
        self.assertNotIn('private-error', error.getvalue())


class UpdateCheckMessageTests(unittest.TestCase):
    def invoke(self, failure, *, recovery=False, check=True):
        output, error = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            Path(directory, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
            stack.enter_context(patch.object(manage, '__file__', str(Path(directory, 'manage.py'))))
            stack.enter_context(patch('ops.uninstall.continue_removal', return_value=False))
            if recovery:
                stack.enter_context(patch.object(updater, 'layout_for', return_value=object()))
                stack.enter_context(patch.object(updater.journal, 'update_lock', side_effect=lambda layout: nullcontext()))
                stack.enter_context(patch.object(updater.journal, 'load_journal', return_value={'phase': 'prepared'}))
                stack.enter_context(patch.object(updater, 'Host'))
                engine = stack.enter_context(patch.object(updater, 'UpdateEngine'))
                engine.return_value.recover.side_effect = ValueError(failure)
            else:
                stack.enter_context(patch.object(updater, 'management', side_effect=lambda *args: nullcontext((None, False))))
            update = stack.enter_context(patch.object(updater, 'update', side_effect=ValueError(failure)))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(sys, 'argv', ['manage.py', 'update'] + (['--check'] if check else [])))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(error))
            status = manage.main()
        if recovery:
            self.assertIn('An interrupted update was found. Recovering it first.', output.getvalue())
        return status, error.getvalue(), update

    def test_missing_release_and_old_candidate_check_do_not_claim_a_journal(self):
        for code in ('release_not_found', 'update_version_not_newer'):
            with self.subTest(code=code):
                status, error, update = self.invoke(code)
                self.assertEqual(status, 1)
                self.assertIn('Command stopped: ' + code, error)
                self.assertIn('No update was started.', error)
                self.assertNotIn('journal', error)
                self.assertNotIn('recovery', error)
                update.assert_called_once_with(None, None, check=True, pre=False, build_local=False)

    def test_actual_recovery_failure_still_warns_and_does_not_run_check(self):
        status, error, update = self.invoke('update_recovery_unavailable', recovery=True)
        self.assertEqual(status, 1)
        self.assertIn('journal records any recovery still needed', error)
        self.assertNotIn('No update was started', error)
        update.assert_not_called()

    def test_non_domain_errors_stay_generic(self):
        _, error, _ = self.invoke('private-secret-error')
        self.assertNotIn('private-secret-error', error)
        self.assertIn('Command failed.', error)


class JournalNoteTests(unittest.TestCase):
    """A refusal names a journal only when one on disk still records unfinished work."""
    PENDING = {'phase': 'copy_intent'}
    FINISHED = {'phase': 'rolled_back'}  # An earlier update's journal stays after it ends.
    RECOVERY = 'The update journal records any recovery still needed.'

    def invoke(self, action, failure, *, journal=None, removal=False, admission=None):
        output, error = StringIO(), StringIO()
        def management(*args):
            if admission is not None:
                raise ValueError(admission)
            return nullcontext((None, False))
        with tempfile.TemporaryDirectory() as base, ExitStack() as stack:
            directory = Path(base, 'installation')
            directory.mkdir()
            (directory / 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
            if removal:
                owner = Path(base, 'installation.update', 'owner')
                owner.mkdir(parents=True)
                (owner / 'uninstall-journal.json').write_text('{}')
            stack.enter_context(patch.object(manage, '__file__', str(directory / 'manage.py')))
            stack.enter_context(patch('ops.uninstall.continue_removal', return_value=False))
            stack.enter_context(patch('ops.uninstall.uninstall', side_effect=ValueError(failure)))
            stack.enter_context(patch.object(updater, 'management', side_effect=management))
            stack.enter_context(patch.object(updater, 'update', side_effect=ValueError(failure)))
            stack.enter_context(patch.object(updater, 'restore_update_copy', side_effect=ValueError(failure)))
            stack.enter_context(patch.object(updater, 'layout_for', return_value=object()))
            stack.enter_context(patch.object(updater.journal, 'update_lock', side_effect=lambda layout: nullcontext()))
            stack.enter_context(patch.object(updater.journal, 'load_journal', return_value=journal))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(sys, 'argv', ['manage.py', *action.split()]))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(error))
            status = manage.main()
        self.assertEqual(status, 1)
        return error.getvalue()

    def test_update_refused_before_its_journal_says_no_update_was_started(self):
        for code in ('update_services_must_be_running', 'update_unexpected_project_container',
                     'release_download_timeout', 'update_unknown_config_key'):
            for journal in (None, self.FINISHED):
                with self.subTest(code=code, journal=journal):
                    self.assertEqual(self.invoke('update', code, journal=journal),
                                     f'Command stopped: {code}. No update was started.\n')

    def test_busy_lock_says_what_holds_it_not_a_journal(self):
        expected = 'Command stopped: update_lock_busy. ' + manage.UPDATE_STOP_HINTS['update_lock_busy'] + '\n'
        for action in ('update', 'update --check', 'restart'):
            with self.subTest(action=action):
                self.assertEqual(self.invoke(action, 'unused', admission='update_lock_busy', journal=self.PENDING),
                                 expected)
        self.assertEqual(self.invoke('uninstall', 'update_lock_busy', journal=self.PENDING), expected)
        self.assertNotIn('journal', expected)

    def test_unfinished_update_journal_is_named_wherever_the_command_stops(self):
        for action, failure, admission in (
                ('update', 'update_recovery_failed_copy_deadline_still_applies', None),
                ('update --check', 'unused', 'update_recovery_failed_copy_deadline_still_applies'),
                ('restart', 'unused', 'update_backend_stop_not_clean'),
                ('uninstall', 'uninstall_container_not_owned', None)):
            with self.subTest(action=action):
                code = admission or failure
                self.assertEqual(self.invoke(action, failure, admission=admission, journal=self.PENDING),
                                 f'Command stopped: {code}. {self.RECOVERY}\n')

    def test_recovered_update_claims_neither_recovery_nor_no_start(self):
        code = 'update_failed_previous_version_recovered'
        self.assertEqual(self.invoke('update', code, journal=self.FINISHED), f'Command stopped: {code}.\n')

    def test_removal_journal_is_named_only_once_a_confirmed_removal_saved_it(self):
        code = 'uninstall_container_not_owned'
        for journal in (None, self.FINISHED):
            with self.subTest(journal=journal):
                self.assertEqual(self.invoke('uninstall', code, journal=journal), f'Command stopped: {code}.\n')
        self.assertEqual(self.invoke('uninstall', 'uninstall_tree_changed', removal=True),
                         'Command stopped: uninstall_tree_changed. '
                         'The saved removal journal records the remaining work.\n')

    def test_restore_without_an_interrupted_update_names_no_journal(self):
        self.assertEqual(self.invoke('restore-update-copy', 'update_restore_unavailable', journal=self.FINISHED),
                         'Command stopped: update_restore_unavailable.\n')


if __name__ == '__main__':
    unittest.main()
