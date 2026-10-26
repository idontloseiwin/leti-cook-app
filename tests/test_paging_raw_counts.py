"""Recent-video scans continue by key over visible channels, within a page budget. No database."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from http_test_support import ROOT, cache_stub, library_stub, endpoint
from web.constants import EXPLORER_RECENT_VIDEO_MAX_PAGES, EXPLORER_RECENT_VIDEO_SCAN_SIZE
from web.home import create_home_router
from youtube_source_fields import parse_dt


def router_with(pages, reads, visible=frozenset({'UC1'})):
    class Repository:
        def __init__(self, database): pass
        def load_recent_long_inventory_page(self, *, published_from, channel_ids, before, page_size):
            reads.append((tuple(channel_ids), before, page_size))
            return pages[len(reads) - 1]
        def load_videos_by_ids(self, ids, **kwargs):
            return [{'video_id': value, 'title': value} for value in ids]
    router = create_home_router(cache=cache_stub(), library=library_stub(load_visible_channel_ids=lambda: set(visible)),
                                core=SimpleNamespace(parse_dt=parse_dt), database=None)
    return router, Repository


class PagingRawCountTests(unittest.TestCase):
    def test_an_expired_row_does_not_end_the_scan_and_the_next_page_follows_its_key(self):
        size = EXPLORER_RECENT_VIDEO_SCAN_SIZE
        published = datetime.now(timezone.utc).isoformat()
        # Every row of a full page aged out between query and admission.
        pages = [([], size, (published, 'a0')), ([{'video_id': 'visible', 'channel_id': 'UC1', 'published_at': published}], 1, None)]
        reads = []
        router, Repository = router_with(pages, reads)
        with patch('web.home.FrontendRepository', Repository):
            result = endpoint(router, 'explorer_recent_videos')()
        self.assertEqual(reads, [(('UC1',), None, size), (('UC1',), (published, 'a0'), size)])
        self.assertEqual([row['video_id'] for row in result['items']], ['visible'])
        self.assertFalse(result['has_more'])
        self.assertIsNone(result['next_cursor'])

    def test_page_budget_returns_a_cursor_after_the_last_row_read(self):
        size = EXPLORER_RECENT_VIDEO_SCAN_SIZE
        start = datetime.now(timezone.utc)
        keys = [((start - timedelta(minutes=page)).isoformat(), f'k{page}') for page in range(EXPLORER_RECENT_VIDEO_MAX_PAGES)]
        # Every row of every page is unusable (no current video), so the budget ends the scan.
        pages = [([], size, key) for key in keys]
        reads = []
        router, Repository = router_with(pages, reads)
        with patch('web.home.FrontendRepository', Repository):
            result = endpoint(router, 'explorer_recent_videos')()
        self.assertEqual(len(reads), EXPLORER_RECENT_VIDEO_MAX_PAGES)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['has_more'])
        self.assertEqual(result['next_cursor'], f"{keys[-1][0]}|{keys[-1][1]}")

    def test_both_repository_pages_report_their_raw_size(self):
        source = (ROOT / 'application/db/reads/frontend.py').read_bytes().decode('utf-8')
        self.assertEqual(source.count('return self._admitted_inventory(rows), len(rows)'), 2)


if __name__ == '__main__': unittest.main()
