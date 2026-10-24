"""Signed fresh installs use captured bytes and exact images, with no Docker I/O."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
from ops import release_install
from ops.release_manifest import ReleaseError
from ops.release_verification import VerifiedRelease
from test_direct_pg_installation import PORT, composed
import test_release_verification as release_fixture


class ReleaseInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        release_fixture.ReleaseVerificationTests.setUpClass()

    def setUp(self):
        self.fixture = release_fixture.ReleaseVerificationTests('test_real_ed25519_verification_returns_immutable_captured_bytes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.verified = self.fixture.verify()
        self.destination = self.fixture.root / 'installation'
        self.calls = []

    def lifecycle(self, *, architecture='amd64', build_local=False, bad_image=None, fail_pull=False,
                  cached=False, wrong_override=False, cache_listing=None, list_failure=False, inspect_failure=False):
        # Once authenticated, changing or deleting the original package has no
        # influence on the installation or the code that drives it.
        self.fixture.archive.write_bytes(b'replaced after verification')
        directory = self.destination
        details_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(architecture, architecture)
        pulled = set()
        def run(command, **kwargs):
            self.calls.append((list(command), dict(kwargs)))
            args = command
            if args[:2] == ['docker', '--host']:
                self.assertEqual(args[2], 'unix:///var/run/docker.sock')
                args = ['docker', *args[3:]]
            if args[:3] == ['docker', 'context', 'inspect']:
                return 'unix:///var/run/docker.sock\n'
            if args == ['docker', 'compose', 'version', '--short']:
                return '2.24.4\n'
            if args[:2] == ['docker', 'info']:
                return json.dumps({'OSType': 'linux', 'Architecture': architecture})
            if args[:1] == ['timedatectl']:
                return 'yes\n'
            if args[-3:] == ['config', '--format', 'json']:
                value = composed(directory, args[args.index('--project-name') + 1])
                if (directory / 'runtime.compose.json').exists():
                    runtime = json.loads((directory / 'runtime.compose.json').read_bytes())
                    for service, data in runtime['services'].items():
                        value['services'][service].update(data)
                    value['volumes'].update(runtime['volumes'])
                    if wrong_override:
                        value['services']['db']['image'] = 'untrusted:tag'
                return json.dumps(value)
            if args[:2] == ['docker', 'pull'] and fail_pull:
                raise install.InstallError('command_failed_1')
            if args[:2] == ['docker', 'pull']:
                pulled.add(args[-1])
            if args[:2] == ['docker', 'build']:
                context = Path(args[-1])
                self.assertIn(context, (directory / 'database', directory / 'application'))
                self.assertNotIn('--env-file', args)
                for path in context.rglob('*'):
                    if path.is_file():
                        self.assertFalse(path.name.endswith('.env'))
                        self.assertNotIn(b'synthetic_private_secret', path.read_bytes())
                self.assertTrue((directory / 'backend.env').is_file())
            if args[:3] == ['docker', 'image', 'ls']:
                if list_failure:
                    raise install.InstallError('command_failed_1')
                return '' if cache_listing is None else json.dumps(cache_listing)
            if args[:3] == ['docker', 'image', 'inspect']:
                if inspect_failure or ('@' in args[-1] and not cached and args[-1] not in pulled):
                    raise install.InstallError('command_failed_1')
                service = 'db' if '-db' in args[-1] else 'backend'
                details = {'Id': 'sha256:' + ('a' if service == 'db' else 'b') * 64,
                           'Os': 'linux', 'Architecture': details_arch, 'RepoDigests': [args[-1]]}
                if bad_image:
                    details.update(bad_image)
                return json.dumps(details)
            return ''
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            stack.enter_context(patch.object(install.sys, 'platform', 'linux'))
            stack.enter_context(patch.object(install, 'verify_bundle', side_effect=AssertionError('Must not reread package')))
            stack.enter_context(patch.object(install, 'new_directory', return_value=directory))
            stack.enter_context(patch.object(install, 'port_available', return_value=True))
            stack.enter_context(patch.object(install, 'installation_settings', return_value=(
                {'AUTHENTICATOR_PASSWORD': 'synthetic_private_secret'},
                {'LIC_DATABASE_ID': '00000000-0000-4000-8000-000000000001', 'SECRET': 'synthetic_private_secret'})))
            stack.enter_context(patch.object(install, 'check_update_copy_storage'))
            stack.enter_context(patch.object(install, 'initialize_update_copy_storage'))
            opener = stack.enter_context(patch.object(install.urllib.request, 'build_opener'))
            opener.return_value.open.return_value = StringIO(json.dumps(
                dict(configured=False, authenticated=False, username=None, access_mode='pending')))
            stack.enter_context(redirect_stdout(StringIO()))
            return release_install.install_verified_release(self.verified, directory, PORT, run=run,
                                                             build_local=build_local)

    def test_signed_amd64_pulls_exact_digests_pins_ids_and_preserves_captured_files(self):
        result = self.lifecycle(architecture='x86_64')
        commands = [item[0] for item in self.calls]
        pulls = [args for args in commands if 'pull' in args and 'up' not in args and 'run' not in args]
        self.assertEqual([args[-1] for args in pulls], [
            self.verified.manifest['images'][name]['repository'] + '@' + self.verified.manifest['images'][name]['digest']
            for name in ('db', 'backend')])
        self.assertTrue(all('--platform=linux/amd64' in args for args in pulls))
        self.assertFalse(any('build' in args for args in commands))
        for name, data in self.verified.files.items():
            self.assertEqual((self.destination / name).read_bytes(), data, name)
        override = json.loads((self.destination / 'runtime.compose.json').read_bytes())
        self.assertEqual(override, {'services': {'db': {'image': 'sha256:' + 'a' * 64},
                                               'backend': {'image': 'sha256:' + 'b' * 64}},
                                    'volumes': {'database-data': {'name': result['project'] + '_database-data'}}})
        for name in ('installation.json', 'installed.json'):
            receipt = json.loads((self.destination / name).read_bytes())
            self.assertEqual(receipt['source'], 'release')
            self.assertEqual(receipt['version'], self.verified.manifest['version'])
        for args in commands:
            if 'up' in args:
                self.assertIn('--no-build', args)
                self.assertEqual(args[args.index('--pull') + 1], 'never')
        self.assertEqual(result['source'], 'release')

    def test_build_local_uses_verified_contexts_and_still_records_release_and_ids(self):
        result = self.lifecycle(build_local=True)
        builds = [args for args, _ in self.calls if 'build' in args]
        self.assertEqual([Path(args[-1]).name for args in builds], ['database', 'application'])
        self.assertFalse(any('--platform=linux/amd64' in args for args, _ in self.calls))
        self.assertEqual(result['source'], 'release')
        self.assertTrue((self.destination / 'runtime.compose.json').is_file())

    def test_cached_exact_images_allow_offline_package_install_without_registry(self):
        self.lifecycle(cached=True)
        self.assertFalse(any('--platform=linux/amd64' in args for args, _ in self.calls))
        self.assertFalse(any('build' in args for args, _ in self.calls))
        self.assertTrue((self.destination / 'runtime.compose.json').is_file())

    def test_bad_cached_platform_digest_and_id_never_trigger_pull(self):
        for index, bad in enumerate(({'Os': 'windows'}, {'Architecture': 'arm64'}, {'RepoDigests': []},
                                     {'Id': 'mutable:tag'})):
            with self.subTest(bad=bad):
                self.destination = self.fixture.root / ('cached-failure-' + str(index))
                self.calls.clear()
                with self.assertRaisesRegex(install.InstallError, 'release_image_identity_invalid'):
                    self.lifecycle(cached=True, bad_image=bad)
                self.assertFalse(any('pull' in args or 'up' in args for args, _ in self.calls))

    def test_failed_local_inspection_does_not_guess_that_cached_image_is_absent(self):
        image = self.verified.manifest['images']['db']
        for index, options in enumerate(({'list_failure': True},
                    {'cache_listing': {'Repository': image['repository'], 'Digest': image['digest']}})):
            with self.subTest(options=options):
                self.destination = self.fixture.root / ('daemon-failure-' + str(index))
                self.calls.clear()
                with self.assertRaises(install.InstallError):
                    self.lifecycle(inspect_failure=True, **options)
                self.assertFalse(any('pull' in args or 'up' in args for args, _ in self.calls))

    def test_non_amd64_automatically_builds_verified_sources(self):
        self.lifecycle(architecture='aarch64')
        self.assertEqual(sum('build' in args for args, _ in self.calls), 2)
        self.assertFalse(any('--platform=linux/amd64' in args for args, _ in self.calls))

    def test_wrong_digest_platform_or_identity_refuses_before_any_service_starts(self):
        for index, details in enumerate(({'RepoDigests': []}, {'Architecture': 'arm64'}, {'Os': 'windows'}, {'Id': 'untrusted:tag'})):
            with self.subTest(details=details):
                self.destination = self.fixture.root / ('failure-' + str(index))
                self.calls.clear()
                with self.assertRaisesRegex(install.InstallError, 'release_image_identity_invalid'):
                    self.lifecycle(bad_image=details)
                self.assertFalse(any('up' in args or 'run' in args or 'exec' in args for args, _ in self.calls))
                self.assertFalse((self.destination / 'runtime.compose.json').exists())
                # Deliberate partial install remains available to inspect.
                self.assertTrue((self.destination / 'installation.json').is_file())

    def test_pull_failure_does_not_start_or_delete_partial_installation(self):
        with self.assertRaisesRegex(install.InstallError, 'command_failed_1'):
            self.lifecycle(fail_pull=True)
        self.assertFalse(any('up' in args or 'run' in args for args, _ in self.calls))
        self.assertTrue((self.destination / 'installation.json').exists())

    def test_compose_must_resolve_the_exact_pinned_image_before_start(self):
        with self.assertRaisesRegex(install.InstallError, 'release_image_override_mismatch'):
            self.lifecycle(wrong_override=True)
        self.assertFalse(any('up' in args or 'run' in args for args, _ in self.calls))

    def test_remote_daemon_is_refused_before_local_files_are_written(self):
        run = Mock()
        with patch.object(install.sys, 'platform', 'linux'), \
                patch.dict(os.environ, {'DOCKER_HOST': 'ssh://other'}, clear=True):
            with self.assertRaisesRegex(install.InstallError, 'local_docker_socket_required'):
                release_install.install_verified_release(self.verified, self.destination, PORT, run=run)
        run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_context_precedence_is_captured_once_then_all_commands_use_fixed_host(self):
        run = Mock(return_value='unix:///run/chosen.sock\n')
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://other', 'DOCKER_CONTEXT': 'chosen'}, clear=True):
            pinned = release_install._pinned_runner(run)
            pinned(['docker', 'info'])
            pinned(['docker', 'pull', 'fixed'])
        self.assertEqual(run.call_args_list[1].args[0], ['docker', '--host', 'unix:///run/chosen.sock', 'info'])
        self.assertEqual(run.call_args_list[2].args[0], ['docker', '--host', 'unix:///run/chosen.sock', 'pull', 'fixed'])

    def test_check_only_verifies_with_trusted_source_key_and_never_uses_docker(self):
        with patch.object(release_install, 'load_source', return_value=self.fixture.source), \
                patch.object(release_install, 'verify_release', return_value=self.verified) as verify, \
                patch.object(release_install, 'install_verified_release') as create, \
                patch.object(install, 'check_docker', side_effect=AssertionError('check must not call Docker')), \
                redirect_stdout(StringIO()) as output:
            status = release_install.main(['--check', '--package', str(self.fixture.archive)])
        self.assertEqual(status, 0)
        self.assertEqual(verify.call_args.kwargs, {'source': self.fixture.source,
                         'public_key_path': release_install.ROOT / 'application/release-signing.pub'})
        create.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['changes'], False)

    def test_signature_failure_never_calls_prompts_docker_or_install(self):
        with patch.object(release_install, 'load_source', return_value=self.fixture.source), \
                patch.object(release_install, 'verify_release', side_effect=ReleaseError('release_signature_invalid')), \
                patch.object(install, 'check_docker') as docker, patch.object(install, 'hidden_api_key') as key, \
                patch.object(release_install, 'install_verified_release') as create, redirect_stderr(StringIO()):
            self.assertEqual(release_install.main(['--package', str(self.fixture.archive)]), 1)
        docker.assert_not_called()
        key.assert_not_called()
        create.assert_not_called()

    def test_exact_online_arguments_and_prerelease_requires_opt_in_offline_too(self):
        prerelease = VerifiedRelease(MappingProxyType({**self.verified.manifest, 'version': '1.0.0-rc.1'}),
                                    self.verified.files)
        with patch.object(release_install, 'load_source', return_value=self.fixture.source), \
                patch.object(release_install, 'fetch_release', return_value=prerelease) as fetch, \
                patch.object(release_install, 'verify_release', return_value=prerelease), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(release_install.main(['--check', '--version', '1.0.0-rc.1', '--pre']), 0)
            self.assertEqual(fetch.call_args.kwargs['version'], '1.0.0-rc.1')
            self.assertTrue(fetch.call_args.kwargs['include_prereleases'])
            self.assertEqual(release_install.main(['--check', '--package', str(self.fixture.archive)]), 1)
            self.assertEqual(release_install.main(['--check', '--package', str(self.fixture.archive), '--pre']), 0)

    def test_no_cli_source_key_url_or_signature_bypass(self):
        for option in ('--source', '--public-key', '--url', '--base-url', '--skip-signature'):
            with self.subTest(option=option), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
                release_install.main([option, 'anything'])
            self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
