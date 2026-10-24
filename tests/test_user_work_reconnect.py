"""Pure snapshot authorization tests: zero DB/provider and no route wiring."""
from pathlib import Path
import sys
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from user_work_backup import UserWorkError
from user_work_reconnect import authorize_reconnect, reconnect_snapshot


def item(number, *, hidden=False, revision=1):
    return {'id': str(UUID(int=number, version=4)), 'revision': revision,
            'is_hidden': hidden, 'user_input': 'a saved locator'}


class ReconnectAuthorizationTests(unittest.TestCase):
    def test_full_set_beyond_page_and_hidden_exclusion(self):
        pending = [item(i, hidden=i == 110) for i in range(1, 151)]
        result = authorize_reconnect(pending, {'expected_snapshot': reconnect_snapshot(pending)})
        self.assertEqual(len(result.items), 149)
        self.assertEqual(result.excluded_hidden, 1)
        self.assertIn((item(150)['id'], 1), result.items)
        self.assertNotIn((item(110)['id'], 1), result.items)

    def test_order_and_unselected_data_do_not_change_token(self):
        pending = [item(1), item(2)]
        before = reconnect_snapshot(pending)
        pending[0]['user_input'] = 'not logged or hashed; edits must increment revision'
        self.assertEqual(reconnect_snapshot(list(reversed(pending))), before)
        pending[0]['revision'] += 1
        self.assertNotEqual(reconnect_snapshot(pending), before)

    def test_duplicate_request_is_identical_authorization(self):
        pending = [item(1)]
        body = {'expected_snapshot': reconnect_snapshot(pending)}
        self.assertEqual(authorize_reconnect(pending, body), authorize_reconnect(pending, body))
        self.assertNotIn('reconnect_state', pending[0])

    def test_unknown_ack_consumed_or_edited_set_conflicts_without_new_plan(self):
        pending = [item(1), item(2)]
        body = {'expected_snapshot': reconnect_snapshot(pending)}
        for current in ([item(2)], [item(1, revision=2), item(2)], [item(1), item(2), item(3)]):
            with self.subTest(current=current), self.assertRaises(UserWorkError) as caught:
                authorize_reconnect(current, body)
            self.assertEqual((caught.exception.code, caught.exception.status), ('conflict', 409))

    def test_empty_set_has_no_implicit_work(self):
        self.assertEqual(authorize_reconnect([], {'expected_snapshot': reconnect_snapshot([])}).items, ())

    def test_request_shape_and_set_limits_are_strict(self):
        for body in ({}, {'expected_snapshot': 1}, {'expected_snapshot': 'wrong'},
                     {'expected_snapshot': reconnect_snapshot([]), 'confirmed': True}):
            with self.subTest(body=body), self.assertRaises(UserWorkError):
                authorize_reconnect([], body)
        for pending in ([item(1), item(1)], [item(1)] * 10001, [{**item(1), 'is_hidden': 0}],
                        [{'is_hidden': False}], [{**item(1), 'revision': True}], [{**item(1), 'id': 'bad'}]):
            with self.assertRaises(UserWorkError):
                reconnect_snapshot(pending)


if __name__ == '__main__':
    unittest.main()
