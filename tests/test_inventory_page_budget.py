"""Offline page-budget and mechanical inventory ownership contracts."""
import ast
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
from collection.budgets import PageBudget


class PageBudgetTests(unittest.TestCase):
    def test_zero_never_admits_a_request(self):
        budget = PageBudget(0)
        request = Mock()
        for _ in range(3):
            if budget.try_debit():
                request()
        request.assert_not_called()
        self.assertEqual((budget.limit, budget.attempts, budget.remaining), (0, 0, 0))

    def test_exact_bound_is_reserved_before_the_request(self):
        budget = PageBudget(2)
        observed = []
        def request():
            observed.append((budget.attempts, budget.remaining))
        for _ in range(4):
            if budget.try_debit():
                request()
        self.assertEqual(observed, [(1, 1), (2, 0)])
        self.assertEqual((budget.limit, budget.attempts, budget.remaining), (2, 2, 0))

    def test_failed_provider_attempt_is_not_refunded(self):
        budget = PageBudget(1)
        def request():
            self.assertEqual((budget.attempts, budget.remaining), (1, 0))
            raise TimeoutError("synthetic provider timeout")
        self.assertTrue(budget.try_debit())
        with self.assertRaises(TimeoutError):
            request()
        self.assertFalse(budget.try_debit())
        self.assertEqual(budget.attempts, 1)

    def test_boolean_and_coercible_nonintegers_are_rejected(self):
        for value in (True, False, "2", 2.0, None, [], {}):
            with self.subTest(value=value), self.assertRaises(TypeError):
                PageBudget(value)
        class IntSubclass(int):
            pass
        with self.assertRaises(TypeError):
            PageBudget(IntSubclass(1))

    def test_negative_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            PageBudget(-1)

    def test_limits_are_not_silently_clamped_to_rpc_batch_sizes(self):
        budget = PageBudget(1001)
        for _ in range(1001):
            self.assertTrue(budget.try_debit())
        self.assertFalse(budget.try_debit())
        self.assertEqual((budget.attempts, budget.remaining), (1001, 0))

    def test_separate_passes_do_not_share_mutable_budget(self):
        first, second = PageBudget(1), PageBudget(1)
        self.assertTrue(first.try_debit())
        self.assertEqual((second.attempts, second.remaining), (0, 1))
        self.assertTrue(second.try_debit())

    def test_public_counters_are_read_only(self):
        budget = PageBudget(1)
        for name in ("limit", "attempts", "remaining"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                setattr(budget, name, 5)

    def test_budget_import_does_not_load_runtime_or_external_resources(self):
        code = r"""
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'db', 'requests', 'dotenv'}:
        raise AssertionError('runtime import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = checked_import
from collection.budgets import PageBudget
assert PageBudget(0).try_debit() is False
"""
        result = subprocess.run([sys.executable, "-B", "-c", code, str(APPLICATION)],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


class InventoryOwnershipTests(unittest.TestCase):
    def test_runtime_calls_the_new_owner_with_one_shared_budget(self):
        from collection import cycle, initial, inventory
        self.assertIs(cycle.run_typed_inventory_batch, inventory.run_typed_inventory_batch)
        self.assertIs(initial.run_typed_inventory_batch, inventory.run_typed_inventory_batch)
        tree = ast.parse((APPLICATION / "collection/inventory.py").read_text(encoding="utf-8"))
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "collection.budgets"
                             for node in ast.walk(tree)))
        self.assertIn("PageBudget", vars(inventory))


class InventoryWorkerIdentityTests(unittest.TestCase):
    def test_worker_identity_is_required_before_any_repository_or_provider_work(self):
        from collection.inventory import run_typed_inventory_batch
        repository, provider = Mock(), Mock()
        with self.assertRaises(TypeError):
            run_typed_inventory_batch(repository=repository, fetch_page=provider, limit=0)
        for value in (None, True, 12, object()):
            with self.subTest(value=value), self.assertRaises(TypeError):
                run_typed_inventory_batch(repository=repository, fetch_page=provider, limit=0, worker_id=value)
        for value in ("", "  ", "\t"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_typed_inventory_batch(repository=repository, fetch_page=provider, limit=0, worker_id=value)
        self.assertEqual(repository.mock_calls, [])
        provider.assert_not_called()

    def test_claims_receive_the_supplied_identity_unchanged(self):
        from collection.inventory import run_typed_inventory_batch
        for explicit in (None, ["UC" + "a" * 22]):
            with self.subTest(explicit=explicit):
                repository, provider = Mock(), Mock()
                repository.claim_pass.return_value = []
                repository.claim_channels.return_value = []
                result = run_typed_inventory_batch(repository=repository, fetch_page=provider,
                    limit=1, worker_id="owned-worker:typed-inventory", prepare=False,
                    explicit_channel_ids=explicit,
                    video_types=("long", "short", "stream") if explicit is None else ("short",))
                method = repository.claim_pass if explicit is None else repository.claim_channels
                self.assertEqual(method.call_args.kwargs["worker_id"], "owned-worker:typed-inventory")
                self.assertEqual(result["claimed"], 0)
                provider.assert_not_called()

    def test_context_entrypoints_supply_existing_worker_factory(self):
        from types import SimpleNamespace
        from collection import api, cycle, initial
        factory = Mock(return_value="scoped-worker")
        context = SimpleNamespace(database=object(), settings=SimpleNamespace(typed_playlist_inventory_enabled=True),
            video_worker_id=factory, sources=SimpleNamespace(fetch_playlist_items_page=Mock(),
                get_longform_playlist_id=Mock(), parse_dt=Mock()))
        with patch.object(cycle, "run_typed_inventory_batch", return_value={}) as batch:
            cycle.run_typed_playlist_inventory_batch(limit=1, context=context)
        self.assertEqual(batch.call_args.kwargs["worker_id"], "scoped-worker:typed-inventory")
        with patch.object(initial, "collect_initial_inventory_chunk", return_value={}) as chunk:
            api.collect_initial_inventory_chunk("channel", expected_epoch=7, context=context)
        self.assertEqual(chunk.call_args.kwargs["worker_id"], "scoped-worker:typed-inventory")
        self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
