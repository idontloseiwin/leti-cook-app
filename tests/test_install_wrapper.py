"""The source-checkout install.sh removes or keeps its temporary bundle; fake packager and installer.

Runs the real install.sh with bash on POSIX only. Nothing is installed: a fake
tools/package_release.py writes a fake release whose install.sh stands in for
install.py, and Ctrl+C is a SIGINT to the whole process group, as a terminal sends it.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

PACKAGER = r'''
import os, sys, time
from pathlib import Path
output = Path(sys.argv[sys.argv.index('--output') + 1])
output.mkdir(parents=True)
behaviour = os.environ['FAKE_PACKAGE']
if behaviour == 'fail':
    sys.exit(1)
if behaviour == 'wait':
    Path(os.environ['FAKE_READY']).touch()
    time.sleep(30)
    sys.exit(0)
(output / 'install.sh').write_text('exec python3 -B "$(dirname "$0")/install.py" "$@"\n', encoding='utf-8')
(output / 'install.py').write_text(os.environ['FAKE_INSTALLER'], encoding='utf-8')
'''

# Exit codes as install.py returns them: 0, 1 (failure), 130 (Ctrl+C or end of
# input, after "Stopped by user."), and 2 as argparse does for bad arguments.
INSTALLER = r'''
import os, sys, time
from pathlib import Path
behaviour = os.environ['FAKE_INSTALL']
if behaviour == 'wait':
    Path(os.environ['FAKE_READY']).touch()
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        print('Stopped by user. Any partial installation is retained.', file=sys.stderr)
        sys.exit(130)
    sys.exit(0)
if behaviour == 'end_of_input':
    print('Stopped by user. Any partial installation is retained.', file=sys.stderr)
    sys.exit(130)
sys.exit({'ok': 0, 'fail': 1, 'usage': 2}[behaviour])
'''


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'install.sh runs with bash on Linux')
class InstallWrapperBundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.checkout = self.base / 'checkout'
        (self.checkout / 'tools').mkdir(parents=True)
        shutil.copyfile(ROOT / 'install.sh', self.checkout / 'install.sh')
        (self.checkout / 'tools/package_release.py').write_text(PACKAGER, encoding='utf-8')
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.ready = self.base / 'ready'

    def bundles(self):
        return sorted(path.name for path in self.tmp.iterdir() if path.name.startswith('leti-cook-release.'))

    def start(self, *, package='write', install='ok'):
        env = {'PATH': f'{self.bin}{os.pathsep}{os.environ.get("PATH", "/usr/bin:/bin")}',
               'TMPDIR': str(self.tmp), 'FAKE_PACKAGE': package, 'FAKE_INSTALL': install,
               'FAKE_INSTALLER': INSTALLER, 'FAKE_READY': str(self.ready), 'PYTHONDONTWRITEBYTECODE': '1'}
        # A new process group stands for the terminal's foreground job.
        return subprocess.Popen(['bash', str(self.checkout / 'install.sh')], env=env, cwd=self.base,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    def finish(self, process, signal_number=None):
        if signal_number is not None:
            deadline = time.monotonic() + 20
            while not self.ready.exists():
                self.assertIsNone(process.poll(), 'the fake step ended before the signal')
                self.assertLess(time.monotonic(), deadline, 'the fake step never started')
                time.sleep(0.02)
            self.assertEqual(len(self.bundles()), 1)
            os.killpg(process.pid, signal_number)
        try:
            _, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return process.returncode, stderr

    def assert_removed(self, status, stderr, expected):
        self.assertEqual(status, expected, stderr)
        self.assertEqual(self.bundles(), [])
        self.assertNotIn('Release bundle kept', stderr)

    def assert_kept(self, status, stderr, expected):
        self.assertEqual(status, expected, stderr)
        kept = self.bundles()
        self.assertEqual(len(kept), 1)
        self.assertIn(f'Release bundle kept for diagnosis: {self.tmp / kept[0]}\n', stderr)

    def test_success_and_rejected_arguments_remove_the_bundle(self):
        self.assert_removed(*self.finish(self.start(install='ok')), 0)
        self.assert_removed(*self.finish(self.start(install='usage')), 2)

    def test_ctrl_c_at_an_installer_prompt_removes_the_bundle(self):
        status, stderr = self.finish(self.start(install='wait'), signal.SIGINT)
        self.assertIn('Stopped by user.', stderr)
        self.assert_removed(status, stderr, 130)

    def test_end_of_input_at_an_installer_prompt_removes_the_bundle(self):
        status, stderr = self.finish(self.start(install='end_of_input'))
        self.assertIn('Stopped by user.', stderr)
        self.assert_removed(status, stderr, 130)

    def test_ctrl_c_while_packaging_removes_the_partial_bundle(self):
        self.assert_removed(*self.finish(self.start(package='wait'), signal.SIGINT), 130)

    def test_failures_keep_the_bundle_for_diagnosis(self):
        self.assert_kept(*self.finish(self.start(install='fail')), 1)

    def test_packaging_failure_keeps_the_bundle(self):
        self.assert_kept(*self.finish(self.start(package='fail')), 1)

    def test_terminate_and_hang_up_keep_the_bundle_as_before(self):
        self.assert_kept(*self.finish(self.start(install='wait'), signal.SIGTERM), 143)
        self.ready.unlink()
        for name in self.bundles():
            shutil.rmtree(self.tmp / name)
        self.assert_kept(*self.finish(self.start(install='wait'), signal.SIGHUP), 129)


if __name__ == '__main__':
    unittest.main()
