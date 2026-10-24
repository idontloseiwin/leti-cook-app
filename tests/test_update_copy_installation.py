"""Installer mount isolation and management expiry cleanup, without services."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
import manage
from ops import update_copy_store as store

PROJECT = 'leti-cook-0123456789ab'
DATABASE = '00000000-0000-4000-8000-000000000001'


class UpdateCopyInstallationTests(unittest.TestCase):
    def test_backend_gets_only_fixed_keyless_guard_paths(self):
        with patch.object(install, 'safe_path', return_value=PurePosixPath('/opt/leti-cook')):
            settings, backend = install.installation_settings('/opt/leti-cook', 8010, 'a' * 64, PROJECT)
        self.assertEqual(settings['LIC_UPDATE_MARKER_HOST_DIRECTORY'], '/opt/leti-cook.update/marker')
        self.assertEqual(settings['LIC_UPDATE_CIPHER_HOST_DIRECTORY'], '/opt/leti-cook.update/cipher')
        self.assertEqual({key: value for key, value in backend.items() if key.startswith('LIC_UPDATE_')}, {
            'LIC_UPDATE_MARKER_DIRECTORY': '/run/leti-cook-update/marker',
            'LIC_UPDATE_CIPHER_DIRECTORY': '/run/leti-cook-update/cipher',
        })

    def invoke(self, *, project=PROJECT, removed=True, failure=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'installed.json').write_text(json.dumps({'project': project, 'database_id': DATABASE}), encoding='utf-8')
            out, err = StringIO(), StringIO()
            with patch.object(manage, '__file__', str(root / 'manage.py')), \
                    patch.object(sys, 'argv', ['manage.py', 'purge-update-copy']), \
                    patch.object(manage.sys.stdin, 'isatty', side_effect=AssertionError('TTY must not be needed')), \
                    patch.object(manage, 'check_docker', side_effect=AssertionError('Docker must not be needed')), \
                    patch.object(manage, 'compose_prefix', side_effect=AssertionError('Compose must not be needed')), \
                    patch.object(store, 'load_layout', return_value='owned-layout') as load, \
                    patch.object(store, 'purge_copy', return_value=removed, side_effect=failure) as purge, \
                    redirect_stdout(out), redirect_stderr(err):
                result = manage.main()
            return result, out.getvalue(), err.getvalue(), load, purge

    def test_expiry_cleanup_works_when_database_and_docker_are_unavailable(self):
        result, out, err, load, purge = self.invoke()
        self.assertEqual(result, 0)
        self.assertIn('Expired update copy removed.', out)
        self.assertEqual(err, '')
        self.assertEqual(load.call_args.kwargs, {'project': PROJECT, 'database_id': DATABASE})
        purge.assert_called_once_with('owned-layout')

    def test_unexpired_copy_is_not_forced_from_public_cleanup_command(self):
        result, out, _, _, purge = self.invoke(removed=False)
        self.assertEqual(result, 0)
        self.assertIn('No expired update copy', out)
        purge.assert_called_once_with('owned-layout')

    def test_foreign_project_is_rejected_before_opening_update_storage(self):
        result, _, _, load, purge = self.invoke(project='foreign')
        self.assertEqual(result, 1)
        load.assert_not_called()
        purge.assert_not_called()

    def test_cleanup_failure_does_not_publish_details_or_claim_success(self):
        result, out, err, _, _ = self.invoke(failure=ValueError('private-path-and-key-details'))
        self.assertEqual(result, 1)
        self.assertEqual(out, '')
        self.assertNotIn('private-path', err)
        self.assertIn('could not be cleaned', err)


if __name__ == '__main__':
    unittest.main()
