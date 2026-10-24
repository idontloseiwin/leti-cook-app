"""Aged-out rows do not terminate a live recent-video page scan. No database."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from http_test_support import ROOT, cache_stub, library_stub, endpoint
from web.constants import EXPLORER_RECENT_VIDEO_SCAN_SIZE
from web.home import create_home_router
from youtube_source_fields import parse_dt


class PagingRawCountTests(unittest.TestCase):
    def test_an_expired_row_does_not_end_the_scan(self):
        page_size = EXPLORER_RECENT_VIDEO_SCAN_SIZE
        reads = []
        published = datetime.now(timezone.utc).isoformat()
        full = [{'video_id': f'a{i}', 'channel_id': 'hidden', 'published_at': published} for i in range(page_size)]
        # One row aged out between query and admission; the others are hidden.
        pages = [(full[1:], page_size), ([{'video_id': 'visible', 'channel_id': 'UC1', 'published_at': published}], 1)]
        class Repository:
            def __init__(self, database): pass
            def load_recent_long_inventory_page(self, *, offset, page_size, **kwargs):
                reads.append(offset)
                return pages[offset // page_size]
            def load_videos_by_ids(self, ids, **kwargs):
                return [{'video_id': value, 'title': value} for value in ids]
        router = create_home_router(cache=cache_stub(), library=library_stub(load_visible_channel_ids=lambda: {'UC1'}),
                                    core=SimpleNamespace(parse_dt=parse_dt), database=None)
        with patch('web.home.FrontendRepository', Repository):
            result = endpoint(router, 'explorer_recent_videos')()
        self.assertEqual(reads, [0, page_size])
        self.assertEqual([row['video_id'] for row in result['items']], ['visible'])
        self.assertFalse(result['has_more'])

    def test_both_repository_pages_report_their_raw_size(self):
        source = (ROOT / 'application/db/reads/frontend.py').read_bytes().decode('utf-8')
        self.assertEqual(source.count('return self._admitted_inventory(rows), len(rows)'), 2)


if __name__ == '__main__': unittest.main()
