"""A missing list is confirmed through the receipt kinds that can record it."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(Path(__file__).parent)]
from db.inventory import inventory_heads  # noqa: E402

WRITERS = {
    'scan_transition': ROOT / 'application/db/inventory/inventory_scan_steps.py',
    'addressed_membership': ROOT / 'application/db/inventory/inventory_heads.py',
}


class MissingListReceiptLookupTests(unittest.TestCase):
    def test_lookup_names_its_receipt_kinds_so_the_primary_key_applies(self):
        lookup = inventory_heads.ABSENT_UNTIL_RECHECK_SQL
        match = re.search(r"FROM public\.typed_inventory_receipts r WHERE r\.receipt_scope IN\(([^)]*)\)\s+AND r\.transition_id=", lookup)
        self.assertIsNotNone(match, 'the receipt lookup must restrict receipt_scope before transition_id')
        kinds = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        self.assertEqual(kinds, set(WRITERS))

    def test_each_named_kind_is_written_with_a_missing_list_outcome(self):
        for kind, path in WRITERS.items():
            text = path.read_text(encoding='utf-8')
            self.assertIn('typed_playlist_not_found', text, path.name)
            self.assertRegex(text, r"INSERT INTO public\.typed_inventory_receipts\s*\(receipt_scope", path.name)
            self.assertIn("'" + kind + "'", text, path.name)

    def test_quota_deferrals_never_record_a_missing_list(self):
        text = (ROOT / 'application/db/inventory/inventory_quota_deferral.py').read_text(encoding='utf-8')
        self.assertNotIn('typed_playlist_not_found', text)


if __name__ == '__main__':
    unittest.main()
