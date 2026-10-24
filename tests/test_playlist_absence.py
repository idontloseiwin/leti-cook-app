"""A channel without Shorts or streams has no such list: the size estimate says so and stays known."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from channels import sources
import youtube_errors

CHANNEL = 'UC' + 'b' * 22
NOT_FOUND_MESSAGE = "The playlist identified with the request's <code>playlistId</code> parameter cannot be found."


class CopiedNotFound(RuntimeError):
    """Same name as the real class but a different object, as after a second module load."""


CopiedNotFound.__name__ = 'YouTubePlaylistNotFoundError'


def estimate(refusals):
    requested = []

    def fetch(playlist_id, **_):
        requested.append(playlist_id[:4])
        refusal = refusals.get(playlist_id[:4])
        if refusal is not None:
            raise refusal
        return {'items': [], 'pageInfo': {'totalResults': 758, 'resultsPerPage': 1}}

    result = sources.estimate_initial_inventory(CHANNEL, fetch_playlist_items_page=fetch,
                                                clock=lambda: datetime.now(timezone.utc))
    return result, requested


class PlaylistAbsenceTests(unittest.TestCase):
    def assert_absent_lists_known(self, refusal):
        result, requested = estimate({'UUSH': refusal, 'UULV': refusal})
        self.assertEqual(result['statuses'], {'long': 'ok', 'short': 'playlist_not_found',
                                              'stream': 'playlist_not_found'})
        self.assertEqual(result['counts'], {'long': 758, 'short': None, 'stream': None})
        self.assertIsNotNone(result['checked_at'])
        self.assertEqual(requested, ['UULF', 'UUSH', 'UULV'])

    def test_a_missing_list_is_absent_not_unknown(self):
        self.assert_absent_lists_known(youtube_errors.YouTubePlaylistNotFoundError(NOT_FOUND_MESSAGE))

    def test_the_answer_decides_even_when_the_class_object_differs(self):
        self.assert_absent_lists_known(CopiedNotFound(NOT_FOUND_MESSAGE))

    def test_a_generic_refusal_naming_the_missing_list_is_absent(self):
        rendered = f'{NOT_FOUND_MESSAGE} | reason=playlistNotFound'
        self.assert_absent_lists_known(youtube_errors.YouTubeAPIError(rendered))

    def test_other_failures_stay_unknown(self):
        result, _ = estimate({'UUSH': youtube_errors.YouTubeAPIError('Backend Error | reason=backendError'),
                              'UULV': TimeoutError('read timed out')})
        self.assertEqual(result['statuses'], {'long': 'ok', 'short': 'error', 'stream': 'error'})
        self.assertIsNone(result['checked_at'])

    def test_an_exhausted_quota_stops_the_remaining_probes(self):
        for refusal in (youtube_errors.YouTubeDailyQuotaExceededError('quota | reason=quotaExceeded'),
                        type('YouTubeDailyQuotaExceededError', (RuntimeError,), {})('quota')):
            with self.subTest(refusal=type(refusal)):
                result, requested = estimate({'UUSH': refusal})
                self.assertEqual(requested, ['UULF', 'UUSH'])
                self.assertEqual(result['statuses'], {'long': 'ok', 'short': 'error', 'stream': 'error'})

    def test_a_small_channel_without_shorts_can_start_by_itself(self):
        from channel_inventory_estimate import initial_inventory_count_bounds
        result, _ = estimate({'UUSH': CopiedNotFound(NOT_FOUND_MESSAGE),
                              'UULV': youtube_errors.YouTubePlaylistNotFoundError(NOT_FOUND_MESSAGE)})
        self.assertEqual(initial_inventory_count_bounds(result)['items_upper_bound'], 758)


if __name__ == '__main__':
    unittest.main()
