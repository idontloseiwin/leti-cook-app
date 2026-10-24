"""The derived image must fail closed on changed upstream privilege handling."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECIPE = (ROOT / 'database/Dockerfile').read_text(encoding='utf-8')
CALL = 'exec gosu postgres "$BASH_SOURCE" "$@"'


class PostgresImageTests(unittest.TestCase):
    def test_keeps_official_postgres_lifecycle_and_removes_gosu(self):
        self.assertRegex(RECIPE.splitlines()[0], r'^FROM postgres:17\.[0-9]+-bookworm@sha256:[0-9a-f]{64}$')
        self.assertIsNone(re.search(r'(?m)^(USER|ENTRYPOINT|CMD|COPY|ADD)\b', RECIPE))
        self.assertIn('rm /usr/local/bin/gosu', RECIPE)
        self.assertIn('--reuid=postgres --regid=postgres --init-groups', RECIPE)
        self.assertNotIn('--reset-env', RECIPE)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and Path('/usr/bin/setpriv').is_file(),
                         'the actual Debian build commands require Linux; exercised in CI')
    def test_actual_build_commands_accept_only_the_expected_upstream_call(self):
        for calls, accepted in ((CALL, True), ('# no privilege switch', False),
                                (CALL + '\n' + CALL, False), (CALL + ' extra', False),
                                (CALL + '\n# another gosu reference', False)):
            with self.subTest(calls=calls), tempfile.TemporaryDirectory(prefix='pg-image-test-') as temp:
                root = Path(temp)
                entry = root / 'entrypoint.sh'
                binary = root / 'gosu'
                entry.write_text('#!/bin/bash\n' + calls + '\n', encoding='utf-8')
                binary.write_text('fixture', encoding='utf-8')
                command = RECIPE.split('\nRUN ', 1)[1].replace('\\\n', '\n')
                command = command.replace('/usr/local/bin/docker-entrypoint.sh', str(entry))
                command = command.replace('/usr/local/bin/gosu', str(binary))
                result = subprocess.run(['bash', '-c', command], text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    updated = entry.read_text(encoding='utf-8')
                    self.assertNotIn('gosu', updated)
                    self.assertFalse(binary.exists())
                    self.assertIn('export HOME="$(getent passwd postgres | cut -d: -f6)"', updated)
                    self.assertIn('test -n "$HOME"', updated)
                    self.assertIn('exec /usr/bin/setpriv', updated)
                    self.assertTrue(updated.rstrip().endswith('"$BASH_SOURCE" "$@"'))
                else:
                    self.assertTrue(binary.exists(), 'unexpected upstream scripts must fail before removal')


if __name__ == '__main__':
    unittest.main()
