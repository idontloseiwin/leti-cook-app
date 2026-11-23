"""Real interrupted rotation and cross-process exclusion; no service or database."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
import support_journal
import telemetry_sink
from telemetry_contract import build_event, encode_event
from telemetry_sink import JsonlTelemetryJournal


class RotationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/'events.jsonl'
        self.path.write_bytes(b'original')
        self.path.chmod(0o600)

    def orphan(self):
        with tempfile.NamedTemporaryFile('wb', dir=self.folder.name,
                prefix=f'.{self.path.name}.', suffix='.tmp', delete=False) as handle:
            handle.write(b'incomplete rotation')
            return Path(handle.name)

    def test_reopen_recovers_real_hard_exit_during_rotation(self):
        script = '''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from telemetry_sink import JsonlTelemetryJournal
import telemetry_sink
from telemetry_contract import build_event,encode_event
path=Path(sys.argv[2])
event=build_event('pipeline','collector','collector','rotation',status='confirmed')
path.write_bytes((encode_event(event)+b'\\n')*30)
path.chmod(0o600)
def crash(*args): os._exit(73)
telemetry_sink.os.replace=crash
JsonlTelemetryJournal(path,max_bytes=2048).append_batch([event])
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT/'application'), str(self.path)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors='replace'))
        orphans = list(self.path.parent.glob(f'.{self.path.name}.*.tmp'))
        self.assertEqual(len(orphans), 1)
        before = self.path.read_bytes()
        reopened = JsonlTelemetryJournal(self.path, max_bytes=2048)
        reopened.recover_rotation()
        self.assertFalse(orphans[0].exists())
        self.assertEqual(self.path.read_bytes(), before)  # Recovery never alters the journal.
        reopened.append_batch([build_event('pipeline','collector','collector','after_crash')])
        self.assertLessEqual(self.path.stat().st_size, 2048)
        self.assertEqual(list(self.path.parent.glob(f'.{self.path.name}.*.tmp')), [])

    def test_initialization_recovers_before_starting_optional_writer(self):
        orphan = self.orphan()
        support_journal.close()
        self.addCleanup(support_journal.close)
        support_journal.initialize(path=self.path, key=b'x'*32, environ={})
        self.assertFalse(orphan.exists())
        self.assertEqual(support_journal.snapshot()['status'], 'ok')
        self.assertIsNone(support_journal._emitter._thread)

    def test_recovery_preserves_foreign_files_links_and_directories(self):
        own = self.orphan()
        protected = []
        for name in ('.other.jsonl.abcdef12.tmp', f'.{self.path.name}.not-a-generated-suffix.tmp',
                     f'.{self.path.name}.abcdef12.log', 'unrelated.tmp'):
            path = self.path.parent/name
            path.write_bytes(b'foreign')
            path.chmod(0o600)
            protected.append(path)
        directory = self.path.parent/f'.{self.path.name}.dir00001.tmp'
        directory.mkdir()
        protected.append(directory)
        hardlink = self.path.parent/f'.{self.path.name}.hard0001.tmp'
        try:
            os.link(protected[0], hardlink)
        except PermissionError:
            if os.name != 'nt':
                raise
        else:
            protected.append(hardlink)
        symlink = self.path.parent/f'.{self.path.name}.link0001.tmp'
        try:
            symlink.symlink_to(protected[0])
        except (OSError, NotImplementedError):
            pass  # Windows may lack symlink privilege; Linux exercises it.
        else:
            protected.append(symlink)
        if os.name != 'nt':
            public = self.path.parent/f'.{self.path.name}.public01.tmp'
            public.write_bytes(b'not a private temporary')
            public.chmod(0o644)
            protected.append(public)
            if os.geteuid() == 0:
                foreign_owner = self.path.parent/f'.{self.path.name}.other001.tmp'
                foreign_owner.write_bytes(b'foreign owner')
                foreign_owner.chmod(0o600)
                os.chown(foreign_owner, 1, -1)
                protected.append(foreign_owner)
        JsonlTelemetryJournal(self.path).recover_rotation()
        self.assertFalse(own.exists())
        self.assertTrue(all(path.exists() for path in protected))
        self.assertEqual(protected[0].read_bytes(), b'foreign')
        self.assertEqual(self.path.read_bytes(), b'original')

    def test_live_other_process_rotation_is_never_cleaned(self):
        script = '''
import sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from telemetry_sink import journal_file_lock
path=Path(sys.argv[2])
with journal_file_lock(path,timeout=1):
    with tempfile.NamedTemporaryFile('wb',dir=path.parent,prefix='.'+path.name+'.',suffix='.tmp',delete=False) as f:
        f.write(b'live rotation')
    print('ready',flush=True)
    sys.stdin.read(1)
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', script, str(ROOT/'application'), str(self.path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            live, = self.path.parent.glob(f'.{self.path.name}.*.tmp')
            with self.assertRaises(TimeoutError):
                JsonlTelemetryJournal(self.path, lock_timeout=.05).recover_rotation()
            self.assertEqual(live.read_bytes(), b'live rotation')
        finally:
            try:
                _, stderr = process.communicate('x', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
                raise
        self.assertEqual(process.returncode, 0, stderr)
        JsonlTelemetryJournal(self.path).recover_rotation()
        self.assertFalse(live.exists())

    def test_matching_name_with_different_owner_is_preserved_on_every_platform(self):
        foreign = self.orphan()
        file_owner = telemetry_sink._file_owner
        def owner(path, info):
            return ('foreign owner',) if Path(path) == foreign else file_owner(path, info)
        with patch.object(telemetry_sink, '_file_owner', side_effect=owner):
            JsonlTelemetryJournal(self.path).recover_rotation()
        self.assertEqual(foreign.read_bytes(), b'incomplete rotation')

    def test_failed_cleanup_cannot_accumulate_another_rotation_file(self):
        event = build_event('pipeline','collector','collector','rotation')
        self.path.write_bytes((encode_event(event)+b'\n')*30)
        orphan = self.orphan()
        with patch.object(telemetry_sink.os, 'unlink', side_effect=PermissionError('synthetic refusal')), \
             patch.object(telemetry_sink.tempfile, 'NamedTemporaryFile', side_effect=AssertionError('new temp forbidden')):
            with self.assertRaises(PermissionError):
                JsonlTelemetryJournal(self.path, max_bytes=2048).append_batch([event])
        self.assertEqual(list(self.path.parent.glob(f'.{self.path.name}.*.tmp')), [orphan])


if __name__ == '__main__': unittest.main()
