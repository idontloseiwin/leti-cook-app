"""A channel as large as YouTube lists (20,000 long videos) fits a full catalogue browse (tester finding T-43).

The browse runs the same way as the server: the browse cache, the recording repository and the
catalogue builder, with the default limits. Rows have the shape and size of BBC News rows."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import catalogue_browse  # noqa: E402
from catalogue_browse import CatalogueBrowseCache, RecordingCatalogueRepository, normalize_browse_query  # noqa: E402
import catalogue_scope  # noqa: E402
from catalogue_scope import CatalogueReadBudget  # noqa: E402
from channel_explorer import build_channel_explorer  # noqa: E402

CHANNEL = 'UC' + 'b' * 22
YOUTUBE_UPLOADS_LIST_LIMIT = 20_000
TAGS = ['bbc news', 'news', 'world', 'politics', 'uk', 'live', 'report', 'bbc', 'breaking', 'today']


class LargeChannel:
    """Stored rows of a channel with `count` current long videos."""

    def __init__(self, count, now):
        self.now = now
        self.ids = [f'{n:011d}'.replace('0', 'a') for n in range(count)]
        self.checked = (now - timedelta(hours=5)).isoformat()

    def load_channel(self, channel_id, *, columns):
        return {'channel_id': CHANNEL, 'title': 'BBC News', 'subscribers': 19_000_000,
                'views': 5_000_000_000, 'video_count': 33_334, 'country_code': 'GB',
                'channel_created_at': '2006-04-08T00:00:00+00:00',
                'channel_metadata_checked_at': self.checked, 'channel_metadata_status': 'ok', 'is_hidden': False}

    def iter_long_inventory_pages(self, channel_id):
        for start in range(0, len(self.ids), 500):
            yield [{'video_id': video_id, 'published_at': (self.now - timedelta(hours=3 * (start + n))).isoformat(),
                    'source_checked_at': self.checked}
                   for n, video_id in enumerate(self.ids[start:start + 500])]

    def load_videos_by_ids(self, video_ids, *, columns):
        rows = []
        for n, video_id in enumerate(video_ids):
            row = {'video_id': video_id,
                   'title': 'Why this headline is exactly seventy-four characters long for a test %04d' % (n % 10_000),
                   'published_at': (self.now - timedelta(days=n % 3000)).isoformat(), 'checked_at': self.checked,
                   'metadata_checked_at': self.checked, 'duration': 'PT12M34S', 'views': 123_456 + n,
                   'like_count': 2345, 'comment_count': 321, 'category_id': '25', 'availability_status': 'public',
                   'availability_checked_at': self.checked}
            if 'tags' in columns.split(','):
                row['tags'] = list(TAGS)
            rows.append(row)
        return rows


class LargeCatalogueTests(unittest.TestCase):
    def test_the_largest_listable_channel_fits_with_room_to_spare(self):
        now = datetime.now(timezone.utc)
        cache = CatalogueBrowseCache()
        # The same default limit the server uses, observed through the budget it creates.
        budget = CatalogueReadBudget()

        def build(created_at):
            valid_until = created_at + timedelta(seconds=cache.ttl_seconds)
            repository = RecordingCatalogueRepository(LargeChannel(YOUTUBE_UPLOADS_LIST_LIMIT, created_at),
                                                      capture_growth=False, now=created_at,
                                                      source_valid_until=valid_until)
            payload = build_channel_explorer(repository, CHANNEL, now=created_at, source_valid_until=valid_until,
                                             full_scope=True, all_rows=True, catalogue_filters='{"version":1}',
                                             read_budget=budget)
            return payload, repository.snapshot

        payload = cache.begin(CHANNEL, 'raw', normalize_browse_query('raw', {}), build)

        self.assertEqual(payload['coverage']['scan_mode'], 'full_catalogue')
        self.assertEqual(payload['browse']['total_count'], YOUTUBE_UPLOADS_LIST_LIMIT)
        # Real rows vary; keep at least 40% of each limit free.
        self.assertLess(budget.retained_bytes, 0.6 * catalogue_scope.DEFAULT_CATALOGUE_MAX_BYTES)
        self.assertLess(cache._bytes, 0.6 * catalogue_browse.MAX_SESSION_BYTES)
        self.assertLessEqual(2 * catalogue_browse.MAX_SESSION_BYTES, catalogue_browse.MAX_CACHE_BYTES)


if __name__ == '__main__':
    unittest.main()
