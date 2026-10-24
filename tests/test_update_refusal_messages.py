"""RR-24: an update refused at the source-age check explains what to wait for."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'application')]
from install import InstallError  # noqa: E402
import manage  # noqa: E402
from ops import update_host  # noqa: E402
from update_copy_protocol import UpdateCopyError, marker_from_window  # noqa: E402

NOW = 1791547200


def window(**changes):
    stamp = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    value = {'as_of': stamp(NOW), 'oldest_source_at': stamp(NOW - 3600),
             'expired': False, 'invalid_source_clock': False}
    value.update(changes)
    return value


class RefusalCodeTests(unittest.TestCase):
    def test_unconfirmed_and_expired_sources_have_their_own_codes(self):
        with self.assertRaisesRegex(UpdateCopyError, '^update_copy_window_unconfirmed$'):
            marker_from_window(window(invalid_source_clock=True), created_at=NOW)
        with self.assertRaisesRegex(UpdateCopyError, '^update_copy_window_expired$'):
            marker_from_window(window(expired=True), created_at=NOW)
        with self.assertRaisesRegex(UpdateCopyError, '^update_copy_window_unconfirmed$'):
            marker_from_window(window(expired=True, invalid_source_clock=True), created_at=NOW)

    def test_a_failed_source_age_read_is_named_not_a_generic_container_failure(self):
        def failed():
            raise InstallError('command_failed_1')
        # The host imports the protocol as application.update_copy_protocol;
        # both module copies raise a ValueError with the fixed code.
        with self.assertRaisesRegex(ValueError, '^update_copy_window_unavailable$'):
            update_host._window_read(failed)
        self.assertEqual(update_host._window_read(lambda: '{"ok": 1}'), '{"ok": 1}')

    def test_every_source_age_refusal_has_a_plain_hint(self):
        for code in ('update_copy_window_unconfirmed', 'update_copy_window_expired',
                     'update_copy_window_unavailable'):
            with self.subTest(code=code):
                hint = manage.UPDATE_STOP_HINTS[code]
                self.assertIn('current version keeps running', hint)
                self.assertIn('run the update again', hint)


if __name__ == '__main__':
    unittest.main()
