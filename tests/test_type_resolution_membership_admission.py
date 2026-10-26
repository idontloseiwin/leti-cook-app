"""Type resolution admits memberships exactly as their protected view does. No database.

Schema 37 reads memberships from the base table inside the type-resolution
view, so channel-scoped reads can use the memberships index. Its admission
filter and source deadline are copies of readable.video_inventory_memberships;
this test keeps the two identical. A native check on 250,000 videos found no
row difference (reviews/db-audit-fixes-20261026).
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def current_view(schema, name):
    """The last definition of a view in the fresh schema (steps replace earlier ones)."""
    starts = [schema.rfind(prefix + name + ' WITH') for prefix in ('CREATE VIEW ', 'CREATE OR REPLACE VIEW ')]
    start = max(starts)
    return schema[start:schema.index(';\n', start)]


class TypeResolutionMembershipAdmissionTests(unittest.TestCase):
    def setUp(self):
        schema = (ROOT / 'schema/schema.sql').read_text(encoding='utf-8')
        self.membership = current_view(schema, 'readable.video_inventory_memberships')
        self.resolution = current_view(schema, 'readable.video_inventory_type_resolution')
        self.hot = current_view(schema, 'readable.hot_video_inventory')

    def test_inline_filter_and_deadline_are_the_membership_views_own(self):
        def words(text):  # SQL text compared regardless of line breaks and indentation
            return ' '.join(text.split())
        where = self.membership[self.membership.index('WHERE '):]
        deadline = self.membership[self.membership.index('least(s.last_seen_at'):self.membership.index(' AS source_expires_at')]
        resolution = words(self.resolution)
        self.assertIn('FROM public.video_inventory_memberships s', resolution)
        self.assertIn(words(where), resolution)
        self.assertIn(words(deadline) + ' AS source_expires_at', resolution)
        self.assertNotIn('readable.video_inventory_memberships', resolution)

    def test_hot_inventory_passes_its_type_to_the_resolution_group_key(self):
        self.assertIn('canonical_video_type AS inventory_video_type', self.resolution)
        self.assertIn('(resolution.inventory_video_type = inv.video_type)', self.hot)
        self.assertIn('(resolution.canonical_video_type = inv.video_type)', self.hot)


if __name__ == '__main__':
    unittest.main()
