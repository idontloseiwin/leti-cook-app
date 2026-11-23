"""Evidence withdrawal preserves decisions and cannot create a review task."""
from contextlib import nullcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'application'))
from db.reviews import video_review_evidence as review


class Transaction:
    def __init__(self, *, present, inventory):
        self.present = present
        self.inventory = inventory
        self.calls = []

    def borrow(self):
        return nullcontext()

    def fetch_one(self, query, params):
        self.calls.append((query, params))
        if query == review._ADVISORY:
            return {'acquired': True}
        if query == review._PRESENT:
            return {'present': self.present}
        if query == review._INVENTORY:
            return self.inventory
        raise AssertionError('Unexpected read during evidence withdrawal')

    def execute(self, query, params):
        self.calls.append((query, params))


class WithdrawalTests(unittest.TestCase):
    def test_no_case_stops_under_identity_lock_without_inventory_or_mutation(self):
        tx = Transaction(present=False, inventory={'channel_id': 'channel', 'video_type': 'long'})
        review.refresh_review_case(tx, 'video', source_removed=True)
        self.assertEqual(tx.calls, [(review._ADVISORY, ('video',)), (review._PRESENT, ('video',))])

    def test_orphaned_case_is_unchanged(self):
        tx = Transaction(present=True, inventory=None)
        review.refresh_review_case(tx, 'video', source_removed=True)
        self.assertEqual([query for query, _ in tx.calls], [review._ADVISORY, review._PRESENT, review._INVENTORY])

    def test_existing_case_with_identity_only_withdraws_evidence(self):
        tx = Transaction(present=True, inventory={'channel_id': 'channel', 'video_type': None})
        review.refresh_review_case(tx, 'video', source_removed=True)
        self.assertEqual([query for query, _ in tx.calls], [review._ADVISORY, review._PRESENT, review._INVENTORY, review._WITHDRAW])
        self.assertTrue(all(params == ('video',) for _, params in tx.calls))
        self.assertNotIn('decision_type=', review._WITHDRAW)


if __name__ == '__main__':
    unittest.main()
