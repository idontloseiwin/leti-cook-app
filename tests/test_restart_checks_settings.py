"""manage.py restart refuses a config.ini the backend would refuse (tester finding T-16). No Docker."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'application'))
import manage  # noqa: E402
from install import InstallError  # noqa: E402
from ops import self_hosted_container  # noqa: E402
from self_hosted_settings import SettingsError, read_settings_file, settings_problem  # noqa: E402

CONFIG = (ROOT / 'config/config.ini').read_text(encoding='utf-8')


def edited(key, value):
    text, count = re.subn(rf'(?m)^{key} = .*$', f'{key} = {value}', CONFIG)
    assert count == 1, key
    return text


@contextlib.contextmanager
def config_file(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'config.ini'
        path.write_text(text, encoding='utf-8')
        path.with_name('collection-schedules.ini').write_bytes((ROOT / 'config/collection-schedules.ini').read_bytes())
        yield path


class SettingsProblemTests(unittest.TestCase):
    def problem(self, text):
        with config_file(text) as path, self.assertRaises(SettingsError) as caught:
            read_settings_file(path)
        return settings_problem(str(caught.exception))

    def test_each_refused_value_names_its_field_and_the_allowed_values(self):
        above_stats_limit = str(int(re.search(r'(?m)^stats_limit = (\d+)$', CONFIG).group(1)) + 1)
        cases = {
            ('interval_minutes', '7'): 'interval_minutes must be 6, 10, 15, 20 or 30.',
            ('interval_minutes', 'soon'): 'interval_minutes must be 6, 10, 15, 20 or 30.',
            ('automatic_collection', 'yes'): 'automatic_collection must be true or false.',
            ('stats_limit', '0'): 'stats_limit must be a whole number from 1 to 100000.',
            ('stats_limit', 'many'): 'stats_limit must be a whole number from 1 to 100000.',
            ('handoff_grace_seconds', '5'): 'handoff_grace_seconds must be a whole number from 10 to 600.',
            ('new_video_stats_limit', above_stats_limit): 'new_video_stats_limit must not exceed stats_limit.',
        }
        for (key, value), message in cases.items():
            self.assertEqual(self.problem(edited(key, value)), message, (key, value))

    def test_a_broken_layout_gets_the_layout_rule_and_no_file_content(self):
        for text in (CONFIG + '\nstats_limit = abc123\n', CONFIG.replace('[limits]', '[limitz]'), '[x'):
            message = self.problem(text)
            self.assertIn('[collection] and [limits] sections', message)
            self.assertNotIn('abc123', message)

    def test_unrelated_keys_are_accepted_without_rewriting_the_file(self):
        with config_file(CONFIG) as path:
            expected = read_settings_file(path)
        with config_file(CONFIG + '\ncustom_extension = abc123\n') as path:
            before = path.read_bytes()
            self.assertEqual(read_settings_file(path), expected)
            self.assertEqual(path.read_bytes(), before)

    def test_the_shipped_file_is_accepted(self):
        with config_file(CONFIG) as path:
            self.assertEqual(read_settings_file(path)['interval_minutes'], 30)


class ContainerCheckTests(unittest.TestCase):
    def check(self, text):
        out = io.StringIO()
        with config_file(text) as path, \
                patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETTINGS_FILE': str(path)}), \
                patch.object(sys, 'argv', ['self_hosted_container.py', 'check-settings']), \
                contextlib.redirect_stdout(out):
            code = self_hosted_container.main()
        return code, out.getvalue()

    def test_the_check_prints_only_the_problem(self):
        self.assertEqual(self.check(edited('interval_minutes', '7')), (1, 'interval_minutes must be 6, 10, 15, 20 or 30.\n'))
        self.assertEqual(self.check(CONFIG), (0, ''))
        self.assertEqual(self.check(CONFIG + '\ncustom_extension = abc123\n'), (0, ''))


class RestartTests(unittest.TestCase):
    def restart(self, check, up_fails=False):
        calls, out, err = [], io.StringIO(), io.StringIO()

        def command(args, **kw):
            calls.append(args[2:])
            if up_fails and args[2] == 'up':
                raise InstallError('command_failed_1')
            return ''

        def run(args, **kw):
            calls.append(args[2:])
            return check
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
            with patch.object(manage, '__file__', str(Path(directory, 'manage.py'))), \
                    patch.object(manage, 'safe_path', Path), patch.object(manage, 'check_docker', lambda: None), \
                    patch.object(manage, 'compose_prefix', lambda *a: ['docker', 'compose']), \
                    patch.object(manage, 'command', command), patch.object(manage.subprocess, 'run', run), \
                    patch('ops.updater.management', lambda *a: contextlib.nullcontext((None, False))), \
                    patch.object(sys, 'argv', ['manage.py', 'restart']), patch.object(sys.stdin, 'isatty', lambda: True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = manage.main()
        return code, calls, out.getvalue(), err.getvalue()

    def test_a_refused_file_stops_before_anything_restarts(self):
        check = types.SimpleNamespace(returncode=1, stdout='Container starting\ninterval_minutes must be 6, 10, 15, 20 or 30.\n')
        code, calls, out, err = self.restart(check)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [manage.SETTINGS_CHECK])
        self.assertIn('Collection settings were not applied: interval_minutes must be 6, 10, 15, 20 or 30.', err)
        self.assertIn('The application was not restarted', err)
        self.assertEqual(out, '')

    def test_a_check_that_could_not_run_is_not_reported_as_success(self):
        code, calls, out, err = self.restart(types.SimpleNamespace(returncode=125, stdout=''))
        self.assertEqual((code, calls), (1, [manage.SETTINGS_CHECK]))
        self.assertIn('it could not be checked', err)

    def test_an_accepted_file_restarts_and_waits_for_the_backend(self):
        code, calls, out, err = self.restart(types.SimpleNamespace(returncode=0, stdout=''))
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in calls], ['run', 'restart', 'up'])
        self.assertIn('--wait', calls[2])
        self.assertIn('--no-recreate', calls[2])
        self.assertIn('Application restarted.', out)

    def test_a_backend_that_does_not_come_up_is_reported(self):
        code, calls, out, err = self.restart(types.SimpleNamespace(returncode=0, stdout=''), up_fails=True)
        self.assertEqual(code, 1)
        self.assertIn('did not become ready', err)
        self.assertNotIn('Application restarted.', out)


if __name__ == '__main__':
    unittest.main()
