"""A malformed user-work request is not called a malformed backup (tester finding T-18). No database."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from user_work_backup import UserWorkError  # noqa: E402
from user_work_service import INVALID_REQUEST, UserWorkService  # noqa: E402

ITEM = 'b212c9c9-882a-4fae-8734-98aef46f6980'


class RequestCodeTests(unittest.TestCase):
    def code(self, call):
        with self.assertRaises(UserWorkError) as raised:
            call(UserWorkService(repository=None))
        return raised.exception.code, raised.exception.status

    def test_extra_or_missing_fields_get_the_request_code(self):
        for call in (lambda s: s.prepare_resolution(ITEM, {'expected_revision': 1, 'x': 1}),
                     lambda s: s.prepare_resolution(ITEM, {}),
                     lambda s: s.edit_pending(ITEM, {'expected_revision': 1, 'user_input': None}),
                     lambda s: s.export({'browser_preferences': {}, 'extra': True}),
                     lambda s: s.export([])):
            self.assertEqual(self.code(call), (INVALID_REQUEST, 422))
        self.assertEqual(INVALID_REQUEST, 'invalid_user_work_request')

    def test_the_page_has_a_message_for_it(self):
        api = (ROOT / 'application/frontend/src/explorer/userWorkBackupApi.ts').read_text(encoding='utf-8')
        self.assertIn("invalid_user_work_request: 'The request was not accepted.", api)


if __name__ == '__main__':
    unittest.main()
