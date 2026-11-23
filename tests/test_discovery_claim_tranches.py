"""Shared inventory claims stay bounded and release unstarted leases on stop; offline."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from collection import inventory
from test_shared_inventory_engine import Repository, claim, page, NOW


def backlog(size):
    repository, calls = Repository(), []
    due = [claim(channel_id='UC' + f'{index:022d}') for index in range(size)]
    owned = list(due)
    def claim_pass(**options):
        calls.append(options['limit'])
        taken = due[:options['limit']]
        del due[:options['limit']]
        return taken
    repository.claim_pass = claim_pass
    return repository, calls, owned


class SharedInventoryClaimWindowTests(unittest.TestCase):
    def run_batch(self, repository, limit):
        return inventory.run_typed_inventory_batch(repository=repository, fetch_page=Mock(return_value=page()),
            worker_id='offline-test', limit=limit, prepare=False, now=lambda: NOW)

    def test_a_backlog_is_claimed_fifty_at_a_time(self):
        repository, calls, owned = backlog(130)
        summary = self.run_batch(repository, 1000)
        self.assertEqual(calls, [50, 50, 50, 50])  # an empty claim also confirms queue exhaustion
        self.assertEqual((summary['claimed'], summary['processed'], summary['provider_attempts']), (130, 130, 130))
        self.assertEqual([row['channel_scope'] for row in summary['results']],
                         [{'channel_id': row.channel_id, 'control_epoch': 7} for row in owned])

    def test_the_page_budget_still_bounds_the_run(self):
        repository, calls, _ = backlog(500)
        summary = self.run_batch(repository, 120)
        self.assertEqual(calls, [50, 50, 20])
        self.assertEqual((summary['processed'], summary['provider_attempts']), (120, 120))
        self.assertTrue(summary['budget_exhausted'])

    def test_a_stop_claims_nothing_more_and_releases_exact_unstarted_leases(self):
        repository, calls, owned = backlog(500)
        with patch.object(inventory, 'stop_at_boundary', side_effect=lambda summary: summary['processed'] >= 60), \
             patch.object(inventory, 'release_unstarted_playlists') as release:
            summary = self.run_batch(repository, 1000)
        self.assertEqual(calls, [50, 50])
        self.assertEqual((summary['processed'], summary['provider_attempts']), (60, 60))
        release.assert_called_once_with(repository, owned[60:100], summary)


if __name__ == '__main__': unittest.main()
