"""Offline trust handoff and source-reader boundaries; no Docker/database/network."""
from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ops.release_verification import VerifiedRelease
from ops.update_candidate import CandidateError
from ops.update_host import Host, UpdateError
from ops.update_reader import validate_reader_source
from ops import updater

APP = {'ops/update_copy_window.py': b'read only', 'VERSION': b'0.2.0\n',
       'requirements.lock': b'pinned-dependencies', 'Dockerfile': b'pinned-image'}
RELEASE = VerifiedRelease({'version': '0.2.0', 'schema_version': 31,
                          'minimum_upgrade_version': '0.1.0'},
                         {'application/' + k: v for k, v in APP.items()})
PLAN = SimpleNamespace(current_version='0.1.0', target_version='0.2.0',
                       installed_schema=SimpleNamespace(schema_version=31),
                       schema=SimpleNamespace(schema_version=31))
IMAGE = 'sha256:' + 'a' * 64
PROJECT = 'leti-cook-0123456789ab'


class ReaderSourceTests(unittest.TestCase):
    def test_exact_captured_application_bytes_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            # The captured tree and installed inputs have private parents;
            # the shared system temporary directory is only an ancestor.
            base = Path(temporary)
            root = base / 'captured' / 'application'
            installed = base / 'installed'
            for name, data in APP.items():
                target = root / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(data)
            for name in ('requirements.lock', 'Dockerfile'):
                target = installed / 'application' / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(APP[name])
            plan = SimpleNamespace(**vars(PLAN), installed=installed)
            self.assertEqual(validate_reader_source(root, RELEASE, plan), root)
            (root / 'VERSION').write_bytes(b'changed')
            with self.assertRaisesRegex(UpdateError, 'source_changed'):
                validate_reader_source(root, RELEASE, plan)

    @unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0,
                         'requires native Linux ownership checks as root')
    def test_captured_source_under_shared_writable_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            shared = Path(temporary) / 'shared'
            shared.mkdir()
            shared.chmod(0o777)
            root = shared / 'application'
            root.mkdir(mode=0o700)
            with self.assertRaisesRegex(CandidateError, 'update_path_permissions'):
                validate_reader_source(root, RELEASE, PLAN)

    def test_different_old_image_dependency_inputs_are_rejected(self):
        with patch('ops.update_reader._capture_tree', return_value=(APP, (1, 2))), \
                patch('ops.update_reader._safe_path'), patch('ops.update_reader._file', return_value=b'changed'):
            plan = SimpleNamespace(**vars(PLAN), installed=Path.cwd())
            with self.assertRaisesRegex(UpdateError, 'runtime_incompatible'):
                validate_reader_source(Path.cwd(), RELEASE, plan)

    def test_extra_code_and_schema_change_are_rejected(self):
        with patch('ops.update_reader._capture_tree', return_value=({**APP, 'extra.py': b''}, (1, 2))):
            with self.assertRaisesRegex(UpdateError, 'source_changed'):
                validate_reader_source(Path.cwd(), RELEASE, PLAN)
        bad = SimpleNamespace(current_version='0.1.0', target_version='0.2.0',
                              installed_schema=SimpleNamespace(schema_version=31),
                              schema=SimpleNamespace(schema_version=32))
        with self.assertRaisesRegex(UpdateError, 'schema_unsupported'):
            validate_reader_source(Path.cwd(), RELEASE, bad)

    def test_bind_option_injection_path_is_rejected(self):
        with self.assertRaisesRegex(UpdateError, 'path_invalid'):
            validate_reader_source(Path.cwd() / 'reader,readonly=false', RELEASE, PLAN)


class ReaderHostTests(unittest.TestCase):
    def setUp(self):
        self.layout = SimpleNamespace(installation=Path('/opt/example'), project=PROJECT)
        self.host = Host(self.layout, plan=PLAN, release=RELEASE)
        self.host.reader_source_root = Path('/opt/captured/application')
        self.current = {
            'backend': {'Id': 'b' * 64, 'State': {'Running': False}},
            'db': {'Id': 'd' * 64, 'State': {'Running': True},
                   'NetworkSettings': {'Networks': {'private': {}}}},
        }
        self.host.containers = Mock(return_value=self.current)
        self.host.inspect = Mock(return_value={'Internal': True,
            'Labels': {'com.docker.compose.project': PROJECT},
            'Containers': {entry['Id']: {} for entry in self.current.values()}})
        self.host.helper = Mock(return_value='{"snapshot":"bounded"}')
        patcher = patch('ops.update_reader.validate_reader_source', return_value=self.host.reader_source_root)
        self.validation = patcher.start()
        self.addCleanup(patcher.stop)
        self.value = {'operation_id': 'f' * 32, 'old_images': {'backend': IMAGE}}

    def test_stopped_writer_read_uses_old_image_signed_code_and_readonly_mounts(self):
        self.assertEqual(self.host.window(self.value), {'snapshot': 'bounded'})
        args, kwargs = self.host.helper.call_args
        self.assertEqual(args[1], IMAGE)
        self.assertEqual(args[2], ['-B', '/verified-reader/ops/update_copy_window.py'])
        self.assertEqual(kwargs['user'], '10001:10001')
        self.assertEqual(kwargs['capabilities'], ())
        self.assertTrue(kwargs['auto_remove'])
        self.assertEqual(kwargs['network'], 'private')
        self.assertEqual(len(kwargs['mounts']), 2)
        self.assertTrue(all(mount.startswith('type=bind,') and mount.endswith(',readonly')
                            for mount in kwargs['mounts']))
        self.assertNotIn('application-state', str(kwargs))
        self.validation.assert_called_once_with(self.host.reader_source_root, RELEASE, PLAN)

    def test_running_writer_blocks_authoritative_read(self):
        self.current['backend']['State']['Running'] = True
        with self.assertRaisesRegex(UpdateError, 'writer_still_running'):
            self.host.window(self.value)
        self.host.helper.assert_not_called()

    def test_changed_source_or_foreign_network_cannot_query_database(self):
        self.validation.side_effect = UpdateError('update_reader_source_changed')
        with self.assertRaisesRegex(UpdateError, 'source_changed'):
            self.host.window(self.value)
        self.host.helper.assert_not_called()
        self.validation.side_effect = None
        self.host.inspect.return_value['Containers']['foreign'] = {}
        with self.assertRaisesRegex(UpdateError, 'network_shared'):
            self.host.window(self.value)
        self.host.helper.assert_not_called()

    def test_reader_helper_is_removed_by_docker_even_if_parent_disappears(self):
        host = Host(self.layout, run=Mock(return_value=''))
        host.stop_helpers = Mock()
        host.helper(self.value, IMAGE, ['reader'], auto_remove=True, capabilities=())
        command = host.run.call_args.args[0]
        self.assertIn('--rm', command)
        self.assertIn('--read-only', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertFalse(any(part.startswith('--cap-add=') for part in command))

    def test_readiness_also_uses_captured_reader_before_any_stop(self):
        from test_update_host import HostReadinessTests
        fixture = HostReadinessTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        host = fixture.host
        host.reader_source_root = Path('/opt/captured/application')
        host._source_window = Mock(return_value=fixture.window)
        host.check_readiness()
        host._source_window.assert_called_once()
        call = host._source_window.call_args
        self.assertEqual(len(call.args[0]['operation_id']), 32)
        self.assertEqual(call.args[2], host.old_images['backend'])
        self.assertEqual([item.args[0] for item in host.compose.call_args_list], [['config', '--format', 'json']])
        fixture.exchange.assert_not_called()


class VerifiedUpdaterTests(unittest.TestCase):
    def test_captured_entry_does_not_reopen_package_or_change_trust(self):
        layout = SimpleNamespace(installation=Path('/opt/example'))
        with patch.object(updater, 'read_version', return_value='0.1.0'), \
                patch('ops.update_candidate.preflight_candidate', return_value=PLAN), \
                patch.object(updater, 'Host') as host, \
                patch.object(updater, 'verify_release') as verify, \
                patch.object(updater, 'fetch_release') as fetch:
            self.assertEqual(updater.update_verified(layout, RELEASE, check=True,
                reader_source_root=Path('/opt/captured/application'), emit=lambda text: None), 0)
            verify.assert_not_called()
            fetch.assert_not_called()
            host.assert_called_once_with(layout, plan=PLAN, release=RELEASE, build_local=False,
                reader_source_root=Path('/opt/captured/application'))
            host.return_value.check_readiness.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
