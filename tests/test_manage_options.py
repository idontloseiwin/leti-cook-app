"""manage.py options: update options belong to the update action only."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage


class ManageOptionTests(unittest.TestCase):
    def test_update_options_require_the_update_action(self):
        for action in ('check-settings', 'restart'):
            err = io.StringIO()
            with self.subTest(action=action), patch.object(sys, 'argv', ['manage.py', action, '--check']),                     contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                manage.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertIn('update options require the update action', err.getvalue())


if __name__ == '__main__': unittest.main()
