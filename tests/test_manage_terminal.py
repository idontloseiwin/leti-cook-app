"""manage.py needs a terminal only where it asks or shows a secret (tester finding T-15). No Docker."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage  # noqa: E402


def run(action):
    calls, out, err = [], io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
        with patch.object(manage, '__file__', str(Path(directory, 'manage.py'))), \
                patch.object(manage, 'safe_path', Path), patch.object(manage, 'check_docker', lambda: None), \
                patch.object(manage, 'compose_prefix', lambda *a: ['docker', 'compose']), \
                patch.object(manage, 'command', lambda args, **kw: calls.append(args[2:]) or ''), \
                patch('ops.updater.management', lambda *a: contextlib.nullcontext((None, False))), \
                patch.object(manage.subprocess, 'run', lambda args, **kw: types.SimpleNamespace(returncode=0, stdout='')), \
                patch.object(sys, 'argv', ['manage.py', action]), patch.object(sys.stdin, 'isatty', lambda: False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = manage.main()
    return code, calls, out.getvalue(), err.getvalue()


class TerminalTests(unittest.TestCase):
    def test_restart_runs_from_a_script(self):
        code, calls, out, err = run('restart')
        self.assertEqual(code, 0, err)
        self.assertIn(['restart', 'backend'], calls)
        self.assertIn('Application restarted.', out)

    def test_commands_that_ask_or_show_a_secret_say_they_need_a_terminal(self):
        for action in ('setup-code', 'set-key', 'redact-secrets', 'uninstall'):
            code, calls, out, err = run(action)
            self.assertEqual((code, calls), (1, []), action)
            self.assertEqual(err, f'{action} needs an interactive terminal. Run it in a terminal; over SSH, use ssh -t.\n')


if __name__ == '__main__':
    unittest.main()
