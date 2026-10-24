"""Actual local schedule preflight, management check and bundle contents; no Docker or network."""
import builtins
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('F11_SOURCE_ROOT', ROOT))
sys.path[:0] = [str(ROOT / 'application'), str(ROOT), str(ROOT / 'tests')]
import collection_schedule as schedule
import install
import manage
from ops import self_hosted_container

TEMPLATE = (ROOT / 'config/collection-schedules.ini').read_bytes()
CONFIG = (ROOT / 'config/config.ini').read_bytes()
CUSTOM = TEMPLATE.replace(b'age_0_1d_seconds = 3600', b'age_0_1d_seconds = 1800') + b'# owner comment\r\n'
INVALID = TEMPLATE.replace(b'age_0_1d_seconds = 3600', b'age_0_1d_seconds = confidential_invalid_value')


class FileValidationTests(unittest.TestCase):
    def test_fresh_schedule_import_has_no_runtime_database_or_timezone_dependency(self):
        script = """
import builtins
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'collection_policy', 'zoneinfo', 'db', 'hot_runtime', 'runtime', 'uvicorn'}:
        raise AssertionError('schedule validation imported runtime dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from collection_schedule import parse_schedule_file
value = parse_schedule_file(Path(sys.argv[2]).read_bytes())
assert value.rechecks.identity_seconds == 2419200
"""
        result = subprocess.run([sys.executable, '-B', '-c', script,
                                 str(ROOT / 'application'), str(ROOT / 'config/collection-schedules.ini')],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_regular_file_is_bounded_read_only_and_returns_immutable_model(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'collection-schedules.ini'
            path.write_bytes(CUSTOM)
            value = schedule.read_schedule_file(path)
            self.assertEqual(value.long_counters.age_0_1d_seconds, 1800)
            self.assertEqual(path.read_bytes(), CUSTOM)
            path.write_bytes(TEMPLATE)
            self.assertEqual(value.long_counters.age_0_1d_seconds, 1800)
            path.write_bytes(b'#' + b'x' * schedule.MAX_SCHEDULE_BYTES)
            with self.assertRaisesRegex(schedule.ScheduleError, '^schedule_bytes_invalid$'):
                schedule.read_schedule_file(path)

    def test_missing_directory_and_hardlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'collection-schedules.ini'
            with self.assertRaises(FileNotFoundError): schedule.read_schedule_file(path)
            with self.assertRaises((OSError, schedule.ScheduleError)): schedule.read_schedule_file(folder)
            path.write_bytes(TEMPLATE)
            os.link(path, Path(folder) / 'second-name.ini')
            with self.assertRaisesRegex(schedule.ScheduleError, '^schedule_file_unsafe$'):
                schedule.read_schedule_file(path)

    def test_link_rejection_precedes_open_and_messages_never_echo_values(self):
        with patch.object(Path, 'is_symlink', return_value=True), patch('os.open') as opened:
            with self.assertRaisesRegex(schedule.ScheduleError, '^schedule_link_refused$'):
                schedule.read_schedule_file(__file__)
            opened.assert_not_called()
        for code in ('confidential_invalid_value', 'schedule_interval_invalid', 'schedule_jitter_invalid'):
            self.assertNotIn('confidential_invalid_value', schedule.schedule_problem(code))
        self.assertIn('2419200', schedule.schedule_problem('schedule_interval_invalid'))


class ManagementScheduleTests(unittest.TestCase):
    def run_check(self, folder):
        out = io.StringIO()
        with patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE':'self-hosted', 'LIC_SETTINGS_FILE':str(Path(folder)/'config.ini')}), \
             patch.object(sys, 'argv', ['self_hosted_container.py','check-settings']), contextlib.redirect_stdout(out):
            result = self_hosted_container.main()
        return result, out.getvalue()

    def test_container_check_uses_sibling_and_never_imports_runtime_or_provider(self):
        original_import = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name.split('.')[0] in {'uvicorn','db','hot_runtime','runtime','collection_policy','self_hosted_configuration'}:
                raise AssertionError('unexpected runtime dependency')
            return original_import(name,*args,**kwargs)
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'config.ini').write_bytes(CONFIG)
            Path(folder,'collection-schedules.ini').write_bytes(CUSTOM)
            with patch('builtins.__import__', side_effect=guarded):
                self.assertEqual(self.run_check(folder), (0,''))
            Path(folder,'collection-schedules.ini').write_bytes(INVALID)
            code, output = self.run_check(folder)
            self.assertEqual(code,1)
            self.assertIn('collection-schedules.ini:',output)
            self.assertNotIn('confidential_invalid_value',output)
            Path(folder,'collection-schedules.ini').unlink()
            self.assertEqual(self.run_check(folder),(1,'config/collection-schedules.ini: The file could not be read.\n'))

    def test_check_settings_validates_rechecks_without_loading_policy_or_writing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'config.ini').write_bytes(CONFIG)
            path = Path(folder, 'collection-schedules.ini')
            raw = TEMPLATE.replace(b'unavailable_long_max_seconds = 2419200',
                                   b'unavailable_long_max_seconds = 21600')
            path.write_bytes(raw)
            self.assertEqual(self.run_check(folder), (0, ''))
            self.assertEqual(path.read_bytes(), raw)
            for bad in (raw.replace(b'identity_seconds = 2419200', b'identity_seconds = 2419201'),
                        raw.split(b'[schedule.rechecks]', 1)[0],
                        raw.replace(b'version = 4', b'version = 1')):
                path.write_bytes(bad)
                result, output = self.run_check(folder)
                self.assertEqual(result, 1)
                self.assertIn('collection-schedules.ini:', output)
                self.assertEqual(path.read_bytes(), bad)

    def test_restart_actual_preflight_stops_invalid_then_accepts_fixed_file(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'config.ini').write_bytes(CONFIG)
            path = Path(folder,'collection-schedules.ini'); path.write_bytes(INVALID)
            def run(*args, **kwargs):
                code,out = self.run_check(folder)
                return SimpleNamespace(returncode=code,stdout=out)
            with patch.object(manage.subprocess,'run',side_effect=run), patch.object(manage,'command') as command, \
                 contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(manage.restart(Path(folder),['docker','compose']),1)
                command.assert_not_called()
                path.write_bytes(CUSTOM)
                self.assertEqual(manage.restart(Path(folder),['docker','compose']),0)
                self.assertEqual([c.args[0][2] for c in command.call_args_list],['restart','up'])

    def test_scriptable_check_settings_checks_without_restart_or_write(self):
        from test_manage_terminal import run
        result,calls,out,err=run('check-settings')
        self.assertEqual((result,err),(0,''))
        self.assertIn('not restarted',out)
        # The existing test harness records command(), so an empty list also
        # proves this command never dispatches restart/up or secret management.
        self.assertEqual(calls,[])


class PackageScheduleTests(unittest.TestCase):
    def test_actual_bundle_requires_build_and_schedule_components(self):
        selection=json.loads((ROOT/'packaging/files.json').read_text())
        added={'application/collection_schedule.py','application/collection_schedule_model.py',
               'application/runtime/collection_schedules.py',
               'config/collection-schedules.ini','database/Dockerfile',
               'application/VERSION','application/release_version.py','application/release-source.json',
               'application/requirements.lock','application/db/base/operation_contracts.py',
               'ops/bundle_contract.py','ops/release_manifest.py','ops/release_verification.py'}
        for name in added:self.assertEqual(selection[name],name)
        # Assemble exactly the selected candidate overlay onto its read-only base.
        # The real verifier checks every member/hash and new required-file guards.
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory); files={}
            for name,source in selection.items():
                path=ROOT/source if (ROOT/source).is_file() else SOURCE/source
                data=path.read_bytes();files[name]=hashlib.sha256(data).hexdigest()
                target=folder/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            manifest={'format':install.FORMAT,'version':1,'fresh_install_only':True,'files':files}
            (folder/'bundle.json').write_text(json.dumps(manifest))
            verified,contents=install.verify_bundle(folder)
            self.assertEqual(set(contents),set(selection))
            for name in added:
                data=(folder/name).read_bytes();saved=files.pop(name);(folder/name).unlink()
                (folder/'bundle.json').write_text(json.dumps(manifest))
                with self.assertRaisesRegex(install.InstallError,'^incomplete_bundle$'):
                    install.verify_bundle(folder)
                files[name]=saved;(folder/name).write_bytes(data)
            self.assertEqual(contents['config/collection-schedules.ini'],TEMPLATE)


if __name__ == '__main__': unittest.main()
