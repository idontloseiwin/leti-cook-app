"""Hostile pagination through the supported scanner and shared page owner; offline."""
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from typed_playlist import (PlaylistTraversalError, checked_next_page_token,
                            playlist_page_video_items, scan_typed_playlist_window)
from collection.inventory import run_typed_inventory_claim
from test_shared_inventory_engine import Repository, claim, NOW, CHANNEL

FETCH_BUDGET = 200
BAD_ITEMS = ({'contentDetails': {'videoId': 'a,b'}},
             {'contentDetails': {'videoId': 'x' * 11, 'videoPublishedAt': 'not a date'}},
             {'contentDetails': {'videoId': ' spaced '}}, {'contentDetails': {}},
             {'snippet': {}}, 'not-an-object')


def item(index):
    return {'contentDetails': {'videoId': f'vid{index:08d}', 'videoPublishedAt': '2026-01-01T00:00:00Z'}}


class Provider:
    def __init__(self, pages):
        self.pages, self.calls = pages, []
    def __call__(self, playlist_id, page_token=None, **options):
        self.calls.append(page_token)
        if len(self.calls) > FETCH_BUDGET:
            raise AssertionError('fetch budget exceeded')
        return self.pages(page_token)


def chain(length):
    return lambda token: {'items': [item(int(token or 0) * 50 + i) for i in range(50)],
        **({'nextPageToken': str(int(token or 0) + 1)} if int(token or 0) + 1 < length else {})}


class PageWindowTests(unittest.TestCase):
    def window(self, pages, **options):
        provider = Provider(pages)
        return scan_typed_playlist_window(CHANNEL, 'long', fetch_page=provider, **options), provider

    def test_normal_playlist_completes(self):
        result, provider = self.window(chain(3))
        self.assertEqual((result.status, len(provider.calls), len(result.items)), ('complete', 3, 150))
        self.assertTrue(result.scan_complete)

    def test_cycling_tokens_stop_immediately(self):
        pages = {None: 'A', 'A': 'B', 'B': 'A'}
        result, provider = self.window(lambda token: {'items': [item(len(token or ''))], 'nextPageToken': pages[token]})
        self.assertEqual((result.status, result.error_type, len(provider.calls)), ('error', 'repeated_page_token', 3))
        self.assertFalse(result.scan_complete)

    def test_window_returns_a_continuation_at_max_pages(self):
        result, provider = self.window(chain(10_000), max_pages=3)
        self.assertEqual((len(result.items), result.next_page_token, result.pages_processed, len(provider.calls)), (150, '3', 3, 3))
        self.assertEqual(result.status, 'continuation')
        self.assertFalse(result.scan_complete)

    def test_repeated_resume_token_is_refused(self):
        result, provider = self.window(lambda token: {'items': [item(1)], 'nextPageToken': 'R'}, page_token='R')
        self.assertEqual((result.status, result.error_type, len(provider.calls)), ('error', 'repeated_page_token', 1))

    def test_error_code_carries_no_provider_data(self):
        result, _ = self.window(lambda token: {'items': [item(1)], 'nextPageToken': 'secret-token'}, page_token='secret-token')
        self.assertEqual(result.error_type, 'repeated_page_token')
        self.assertNotIn('secret-token', str(PlaylistTraversalError(result.error_type)))

    def test_invalid_item_is_refused(self):
        result, _ = self.window(lambda token: {'items': [item(1), BAD_ITEMS[1]]})
        self.assertEqual((result.status, result.error_type, result.items), ('error', 'invalid_playlist_item', ()))


class SharedPageTraversalTests(unittest.TestCase):
    def run_page(self, repository, provider, **changes):
        return run_typed_inventory_claim(repository=repository, claim=claim(**changes),
                                        fetch_page=provider, now=lambda: NOW)

    def test_empty_page_with_continuation_fails_without_page_settlement(self):
        repository = Repository()
        provider = Provider(lambda token: {'items': [], 'nextPageToken': 'next'})
        result = self.run_page(repository, provider)
        self.assertEqual((result['status'], result['error_type'], len(provider.calls)),
                         ('error', 'empty_page_with_continuation', 1))
        self.assertEqual(repository.receipts, [])
        self.assertEqual(repository.events[-1][1]['error_code'], 'empty_page_with_continuation')

    def test_an_invalid_item_fails_whole_page_instead_of_being_skipped(self):
        for bad in BAD_ITEMS:
            with self.subTest(item=bad):
                repository = Repository()
                result = self.run_page(repository, Provider(lambda token: {'items': [item(1), bad]}))
                self.assertEqual((result['status'], result['error_type'], repository.receipts),
                                 ('error', 'invalid_playlist_item', []))

    def test_pages_before_an_invalid_one_stay_committed(self):
        repository = Repository()
        provider = Provider(lambda token: {'items': [item(1)], 'nextPageToken': '2'} if token is None
                            else {'items': [BAD_ITEMS[1]]})
        first = self.run_page(repository, provider)
        committed = list(repository.receipts)
        second = self.run_page(repository, provider, active_scan_id=first['scan_id'], resume_page_token='2')
        self.assertEqual((second['status'], second['error_type'], provider.calls), ('error', 'invalid_playlist_item', [None, '2']))
        self.assertEqual(repository.receipts, committed)
        self.assertEqual(committed[0]['canonical_video_ids'], ['vid00000001'])

    def test_private_videos_without_a_publication_time_are_kept(self):
        repository = Repository()
        repository.settle_page = Mock(wraps=repository.settle_page)
        result = self.run_page(repository, Provider(lambda token: {'items': [{'contentDetails': {'videoId': 'p' * 11}}]}))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(repository.settle_page.call_args.kwargs['items'], ({'video_id': 'p' * 11, 'published_at': None},))

    def test_duplicate_items_keep_first_value_and_use_canonical_receipt_counts(self):
        repository = Repository()
        original = repository.settle_page
        def settle(*args, **kw):
            reply = original(*args, **kw)
            return {**reply, 'inserted': 1, 'already_matching': 1, 'new_video_ids': ['vid00000002']}
        repository.settle_page = Mock(side_effect=settle)
        duplicate = {'contentDetails': {'videoId': 'vid00000001', 'videoPublishedAt': '2026-02-01T00:00:00Z'}}
        result = self.run_page(repository, Provider(lambda token: {'items': [item(1), duplicate, item(2)]}))
        self.assertEqual(repository.settle_page.call_args.kwargs['items'],
            ({'video_id': 'vid00000001', 'published_at': '2026-01-01T00:00:00Z'},
             {'video_id': 'vid00000002', 'published_at': '2026-01-01T00:00:00Z'}))
        self.assertEqual((result['inserted'], result['already_matching'], result['canonical_video_ids']),
                         (1, 1, ['vid00000001', 'vid00000002']))

    def test_discovery_observes_the_whole_received_page_but_stops_at_original_boundary(self):
        repository = Repository()
        repository.existing_ids.add('vid00000002')
        repository.known_membership_ids = Mock(return_value={'vid00000002'})
        repository.settle_page = Mock(wraps=repository.settle_page)
        provider = Provider(lambda token: {'items': [item(1), item(2), item(3)], 'nextPageToken': 'next'})
        result = self.run_page(repository, provider, scan_kind='discovery')
        self.assertEqual(result['canonical_video_ids'], ['vid00000001', 'vid00000002', 'vid00000003'])
        self.assertEqual(result['new_video_ids'], ['vid00000001', 'vid00000003'])
        self.assertEqual((result['inserted'], result['already_matching']), (2, 1))
        self.assertEqual(result['coverage'], 'partial')
        self.assertTrue(result['boundary_found'])
        self.assertIsNone(result['resume_page_token'])
        self.assertEqual(repository.known_membership_ids.call_args.kwargs['scan_boundary_at'], NOW)
        self.assertEqual(provider.calls, [None])
        repository.settle_page.assert_called_once()
        self.assertIsNone(repository.settle_page.call_args.kwargs['next_page_token'])
        self.assertEqual(repository.settle_page.call_args.kwargs['page_status'], 'boundary')

    def test_new_videos_are_found_by_shared_discovery(self):
        repository = Repository()
        result = self.run_page(repository, Provider(lambda token: {'items': [item(1), item(2)]}), scan_kind='discovery')
        self.assertEqual((result['status'], result['inserted'], result['canonical_video_ids']),
                         ('completed', 2, ['vid00000001', 'vid00000002']))

    def test_invalid_discovery_page_is_not_partially_saved(self):
        repository = Repository()
        result = self.run_page(repository, Provider(lambda token: {'items': [item(1), BAD_ITEMS[1]]}), scan_kind='discovery')
        self.assertEqual((result['status'], result['error_type'], repository.receipts), ('error', 'invalid_playlist_item', []))


class CheckedNextPageTokenTests(unittest.TestCase):
    def test_page_items_contract(self):
        self.assertEqual(playlist_page_video_items({}), [])
        self.assertEqual(playlist_page_video_items({'items': [item(1)]}), [{'video_id': 'vid00000001', 'published_at': '2026-01-01T00:00:00Z'}])
        for bad in BAD_ITEMS:
            with self.assertRaises(PlaylistTraversalError) as caught:
                playlist_page_video_items({'items': [item(1), bad]})
            self.assertEqual(caught.exception.code, 'invalid_playlist_item')
        with self.assertRaises(PlaylistTraversalError) as caught:
            playlist_page_video_items({'items': None})
        self.assertEqual(caught.exception.code, 'invalid_items')

    def test_contract(self):
        seen = set()
        self.assertIsNone(checked_next_page_token({}, 1, seen))
        self.assertIsNone(checked_next_page_token({'nextPageToken': ''}, 1, seen))
        self.assertEqual((checked_next_page_token({'nextPageToken': 'T1'}, 1, seen), seen), ('T1', {'T1'}))
        for response, count, code in (({'nextPageToken': 'T1'}, 1, 'repeated_page_token'),
                ({'nextPageToken': 'T2'}, 0, 'empty_page_with_continuation'),
                ({'nextPageToken': ' T3'}, 1, 'invalid_next_page_token'),
                ({'nextPageToken': 7}, 1, 'invalid_next_page_token'),
                ({'nextPageToken': 'x' * 2049}, 1, 'invalid_next_page_token')):
            with self.assertRaises(PlaylistTraversalError) as caught:
                checked_next_page_token(response, count, seen)
            self.assertEqual(caught.exception.code, code)

    def test_shared_owner_uses_the_supported_whole_page_scanner(self):
        path = ROOT / 'application/collection/inventory.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        imported = {alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                    and node.module == 'typed_playlist' for alias in node.names}
        self.assertIn('scan_typed_playlist_window', imported)
        self.assertNotIn('playlist_video_item', imported)
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == 'playlist_video_item' for node in tree.body))


if __name__ == '__main__': unittest.main()
