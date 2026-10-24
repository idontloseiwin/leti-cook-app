"""Fresh installation names are coherent; old installation identity is not adopted."""
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path, PurePosixPath
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
import manage
from ops.self_hosted_credentials import schema_settings


class ReleaseNameTests(unittest.TestCase):
    def test_fresh_settings_match_compose_and_database_identity(self):
        project = 'leti-cook-0123456789ab'
        with patch.object(install, 'safe_path', return_value=PurePosixPath('/opt/leti-cook')):
            settings, backend = install.installation_settings('/opt/leti-cook', 8010, 'a' * 64, project)
        self.assertEqual(install.DEFAULT_DIRECTORY, '/opt/leti-cook')
        self.assertEqual(install.FORMAT, 'leti-cook-owner-install')
        self.assertEqual(settings['SELF_HOSTED_DB_PROJECT'], project)
        self.assertEqual(settings['SELF_HOSTED_SCHEMA_PATH'], '/opt/leti-cook/schema/schema.sql')
        self.assertEqual(urlsplit(backend['LIC_DATABASE_DSN']).path, '/leti-cook')
        self.assertEqual(backend['LIC_AUTH_DATABASE'], '/var/lib/leti-cook/access/owner.sqlite3')
        self.assertEqual(backend['LIC_SETUP_DIRECTORY'], '/var/lib/leti-cook/configuration')
        self.assertEqual(backend['LIC_SETTINGS_FILE'], '/etc/leti-cook/config.ini')
        self.assertEqual(backend['OPERATOR_JOB_WAKE_PATH'], '/var/lib/leti-cook/operator-jobs.wake')
        compose = (ROOT / 'application.compose.yaml').read_text(encoding='utf-8')
        self.assertIn('application-state:/var/lib/leti-cook', compose)
        self.assertIn('../config:/etc/leti-cook:ro', compose)
        self.assertNotIn('let-it-cook', compose)

    def test_new_installer_refuses_old_project_identity(self):
        with self.assertRaisesRegex(install.InstallError, '^isolated_project_required$'):
            install.installation_settings('/opt/leti-cook', 8010, 'a' * 64, 'lic-0123456789ab')

    def test_management_refuses_old_project_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'installed.json').write_text(json.dumps({'project': 'lic-0123456789ab'}), encoding='utf-8')
            with patch.object(manage, '__file__', str(folder / 'manage.py')), \
                    patch.object(manage, 'safe_path', Path), \
                    patch.object(manage, 'check_docker') as docker, \
                    patch.object(manage, 'command') as command, \
                    patch.object(sys, 'argv', ['manage.py', 'check-settings']), \
                    redirect_stderr(StringIO()):
                self.assertEqual(manage.main(), 1)
            docker.assert_not_called()
            command.assert_not_called()

    def test_old_bundle_format_is_not_a_new_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'bundle.json').write_text(json.dumps({
                'format': 'let-it-cook-owner-install', 'version': 1,
                'fresh_install_only': True, 'files': {},
            }), encoding='utf-8')
            with self.assertRaisesRegex(install.InstallError, '^invalid_bundle_manifest$'):
                install.verify_bundle(folder)

    def test_old_schema_manifest_is_not_silently_relabelled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            manifest = json.loads((ROOT / 'schema/manifest.json').read_text(encoding='utf-8'))
            manifest['format'] = 'let-it-cook-self-hosted-schema'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, '^fresh_schema_manifest_required$'):
                schema_settings(path, 'leti-cook-0123456789ab')
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
