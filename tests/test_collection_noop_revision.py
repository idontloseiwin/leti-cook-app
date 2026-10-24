"""A membership command that changes nothing keeps the collection revision (tester finding T-08). No database."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from saved_collections import SavedCollectionsService  # noqa: E402
from saved_work import SavedWorkError  # noqa: E402

CID = 'c0000000-0000-4000-8000-000000000001'
REQUEST = 'd0000000-0000-4000-8000-000000000001'
AT = '2026-09-26T10:00:00+00:00'


class Repository:
    def __init__(self, revision, changed, applied):
        self.result = {'outcome': 'ready',
                       'collection': {'id': CID, 'kind': 'collection', 'name': 'Mix', 'revision': revision,
                                      'member_count': 2, 'created_at': AT, 'updated_at': AT, 'deleted_at': None},
                       'mutation': {'request_id': REQUEST, 'replayed': False, 'requested_count': 2,
                                    'changed_count': changed, 'applied_revision': applied}}

    def change_members(self, *args):
        return self.result


def change(revision, changed, applied):
    service = SavedCollectionsService(Repository(revision, changed, applied))
    return service.change_members(CID, 'add', {'request_id': REQUEST, 'expected_revision': 2,
                                               'video_ids': ['aaaaaaaaaaa', 'bbbbbbbbbbb']})


class NoopRevisionTests(unittest.TestCase):
    def test_a_command_that_changed_nothing_keeps_the_expected_revision(self):
        self.assertEqual(change(revision=2, changed=0, applied=2)['mutation']['applied_revision'], 2)

    def test_a_real_change_still_moves_one_revision_on(self):
        self.assertEqual(change(revision=3, changed=2, applied=3)['mutation']['applied_revision'], 3)

    def test_receipts_that_do_not_match_the_change_are_refused(self):
        for revision, changed, applied in ((3, 0, 3), (2, 2, 2), (4, 1, 4)):
            with self.assertRaises(SavedWorkError, msg=(revision, changed, applied)):
                change(revision, changed, applied)


if __name__ == '__main__':
    unittest.main()
