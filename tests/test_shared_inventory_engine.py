"""Offline behavioral checks for the shared page owner; no real API or DB."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection.budgets import PageBudget
from collection.inventory import run_typed_inventory_claim, run_typed_inventory_batch
from db.inventory.typed_inventory import TypedInventoryClaim, TypedInventoryRepository, InventoryTokenCycleError
from db.base.postgres import DatabaseUnavailable
from typed_playlist import typed_playlist_id

CHANNEL = "UC" + "a" * 22
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
VIDEO = "vid00000001"

def claim(kind="long", **changes):
    values = dict(channel_id=CHANNEL, video_type=kind, playlist_id=typed_playlist_id(CHANNEL, kind),
        lease_token=str(uuid.uuid4()), lease_channel_epoch=7, lease_expires_at=NOW.isoformat(),
        no_new_streak=0, active_scan_id=None, resume_page_token=None,
        scan_kind="initial_inventory", scan_boundary_at=None)
    values.update(changes)
    return TypedInventoryClaim(**values)

def page(next_token=None):
    value = {"items": [{"contentDetails": {"videoId": VIDEO, "videoPublishedAt": NOW.isoformat()}}]}
    if next_token:
        value["nextPageToken"] = next_token
    return value

class Repository:
    def __init__(self):
        self.database = object()
        self.events = []
        self.receipts = []
        self.existing_ids = set()
    def begin_scan(self, owned, **kw):
        self.events.append("begin")
        return dict(status="running", scan_id=kw["scan_id"],
            resume_page_token=owned.resume_page_token, scan_boundary_at=NOW.isoformat())
    def known_membership_ids(self, *args, **kw):
        return set()
    def settle_page(self, owned, **kw):
        self.events.append("settle")
        ids = [item["video_id"] for item in kw["items"]]
        new_ids = [video_id for video_id in ids if video_id not in self.existing_ids]
        self.existing_ids.update(ids)
        receipt = dict(channel_id=owned.channel_id, video_type=owned.video_type,
            lease_token=owned.lease_token, scan_id=kw["scan_id"], transition_id=kw["transition_id"],
            status="running" if kw["next_page_token"] else "completed",
            coverage="partial" if kw["page_status"] in {"continuation", "boundary"} else "complete",
            page_items=len(kw["items"]), inserted=len(new_ids), already_matching=len(ids)-len(new_ids), conflicts=0,
            items_seen=len(kw["items"]), canonical_video_ids=[item["video_id"] for item in kw["items"]],
            new_video_ids=new_ids,
            resume_page_token=kw["next_page_token"], provider_attempts=1)
        self.receipts.append(receipt)
        return receipt
    def fail_scan(self, owned, **kw):
        self.events.append(("fail", kw))
        return {"items_seen": 0}
    def defer_claims_after_daily_quota(self, claims, **kw):
        self.events.append(("defer", claims, kw))
        return dict(status="deferred", active_scan_state_preserved=True,
                    requested=len(claims), released=len(claims), rejected=0)

class SharedPageTests(unittest.TestCase):
    def run_page(self, repository=None, provider=None, **kw):
        return run_typed_inventory_claim(repository=repository or Repository(),
            claim=kw.pop("claim", claim()), fetch_page=provider or (lambda *a, **k: page()),
            now=lambda: NOW, **kw)

    def test_all_three_types_use_same_begin_fetch_settlement(self):
        for kind, prefix in (("long", "UULF"), ("short", "UUSH"), ("stream", "UULV")):
            with self.subTest(kind=kind):
                repository, budget = Repository(), PageBudget(1)
                def provider(playlist, **kw):
                    self.assertTrue(playlist.startswith(prefix))
                    repository.events.append("fetch")
                    self.assertEqual(budget.attempts, 1)
                    return page()
                result = self.run_page(repository, provider, claim=claim(kind), page_budget=budget)
                self.assertEqual(repository.events, ["begin", "fetch", "settle"])
                self.assertEqual(result["pages_processed"], 1)
                self.assertEqual(result["canonical_video_ids"], [VIDEO])
                self.assertEqual(budget.attempts, 1)

    def test_zero_budget_does_not_begin_bind_or_fetch(self):
        repository, provider, bind = Mock(), Mock(), Mock()
        result = self.run_page(repository, provider, page_budget=PageBudget(0), before_dispatch=bind)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertFalse(repository.mock_calls)
        provider.assert_not_called(); bind.assert_not_called()

    def test_failed_begin_or_bind_spends_no_request(self):
        for where in ("begin", "bind"):
            with self.subTest(where=where):
                repository, provider, budget = Repository(), Mock(), PageBudget(1)
                if where == "begin":
                    repository.begin_scan = Mock(side_effect=DatabaseUnavailable('database_connection_failed', outcome_unknown=True))
                    bind = None
                else:
                    bind = Mock(side_effect=DatabaseUnavailable('database_connection_failed', outcome_unknown=True))
                with self.assertRaises(DatabaseUnavailable):
                    self.run_page(repository, provider, page_budget=budget, before_dispatch=bind)
                provider.assert_not_called()
                self.assertEqual(budget.attempts, 0)

    def test_replayed_bind_never_repeats_provider(self):
        repository, provider, budget = Repository(), Mock(), PageBudget(1)
        result = self.run_page(repository, provider, page_budget=budget,
                              before_dispatch=lambda *args: False)
        self.assertEqual(result["status"], "await_receipt")
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(repository.events, ["begin"])
        self.assertEqual(budget.attempts, 0); provider.assert_not_called()

    def test_transition_is_planned_before_bind_and_reused_in_receipt(self):
        repository, budget, planned = Repository(), PageBudget(1), str(uuid.uuid4())
        def bind(owned, begin, transition):
            self.assertEqual(transition, planned)
            self.assertNotEqual(begin["scan_id"], planned)
            self.assertEqual(budget.attempts, 0)
            repository.events.append("bind")
            return True
        result = self.run_page(repository, page_budget=budget, transition_id=planned, before_dispatch=bind)
        self.assertEqual(repository.events, ["begin", "bind", "settle"])
        self.assertEqual(result["receipt"]["transition_id"], planned)

    def test_provider_error_missing_and_empty_each_spend_one(self):
        class Missing(RuntimeError): pass
        for response in (TimeoutError(), Missing(), {"items": []}):
            with self.subTest(response=type(response).__name__):
                repository, budget = Repository(), PageBudget(1)
                provider = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                result = self.run_page(repository, provider, page_budget=budget, playlist_not_found_errors=(Missing,))
                self.assertEqual(budget.attempts, 1)
                self.assertEqual(result["provider_attempts"], 1)
                self.assertEqual(result["pages_processed"], int(not isinstance(response, Exception)))

    def test_confirmed_token_cycle_uses_same_transition_failure(self):
        repository, budget, planned = Repository(), PageBudget(1), str(uuid.uuid4())
        repository.settle_page = Mock(side_effect=InventoryTokenCycleError("inventory_token_cycle"))
        result = self.run_page(repository, page_budget=budget, transition_id=planned)
        self.assertEqual(result["error_type"], "inventory_token_cycle")
        self.assertEqual(repository.events[-1][1]["transition_id"], planned)
        self.assertEqual(budget.attempts, 1)

    def test_unknown_page_ack_does_not_issue_failure(self):
        repository, budget = Repository(), PageBudget(1)
        repository.settle_page = Mock(side_effect=DatabaseUnavailable('database_connection_failed', outcome_unknown=True))
        repository.fail_scan = Mock()
        with self.assertRaises(DatabaseUnavailable):
            self.run_page(repository, page_budget=budget)
        repository.fail_scan.assert_not_called()
        self.assertEqual(budget.attempts, 1)

    def test_discovery_boundary_stops_pagination_but_observes_known_page_items(self):
        repository = Repository()
        repository.existing_ids.add(VIDEO)
        repository.known_membership_ids = Mock(return_value={VIDEO})
        result = self.run_page(repository, claim=claim(scan_kind="discovery"))
        self.assertTrue(result["boundary_found"])
        self.assertTrue(result["latest_uulf_known"])
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["page_items"], 1)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["new_video_ids"], [])

    def test_discovery_continuation_keeps_cumulative_new_count_without_known_head(self):
        repository = Repository()
        repository.existing_ids.add(VIDEO)
        repository.known_membership_ids = Mock(return_value={VIDEO})
        original = repository.settle_page
        def settle(*args, **kw):
            receipt = original(*args, **kw)
            receipt["new_memberships_found"] = sum(row["inserted"] for row in repository.receipts)
            return receipt
        repository.settle_page = settle
        first_page = {"items": [{"contentDetails": {"videoId": f"new{index:08d}",
            "videoPublishedAt": NOW.isoformat()}} for index in range(50)], "nextPageToken": "after-new"}
        provider = Mock(side_effect=[first_page, page()])
        first = self.run_page(repository, provider, claim=claim(scan_kind="discovery"))
        self.assertEqual(first["status"], "running")
        self.assertEqual(first["inserted"], 50)
        self.assertFalse(first["latest_uulf_known"])
        # A new claim/call also models a later worker pass: only the durable
        # scan cursor and receipt counter carry the earlier page's discoveries.
        final = self.run_page(repository, provider, claim=claim(scan_kind="discovery",
            active_scan_id=first["scan_id"], resume_page_token="after-new"))
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["inserted"], 0)
        self.assertEqual(final["full_generation_new_count"], 50)
        self.assertTrue(final["boundary_found"])
        self.assertFalse(final["latest_uulf_known"])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(provider.call_args.kwargs["page_token"], "after-new")

    def test_returning_id_after_known_boundary_is_settled_without_another_request(self):
        returning_id = 'return00001'
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind):
                repository = Repository()
                repository.existing_ids.update((VIDEO, returning_id))
                repository.known_membership_ids = Mock(return_value={VIDEO})
                observed = Mock(wraps=repository.settle_page)
                repository.settle_page = observed
                response = page('must-not-be-fetched')
                response['items'].append({'contentDetails': {'videoId': returning_id,
                    'videoPublishedAt': NOW.isoformat()}})
                provider = Mock(return_value=response)
                result = self.run_page(repository, provider, claim=claim(kind, scan_kind='discovery'))
                provider.assert_called_once()
                self.assertTrue(result['boundary_found'])
                self.assertEqual(result['status'], 'completed')
                self.assertIsNone(observed.call_args.kwargs['next_page_token'])
                self.assertEqual([item['video_id'] for item in observed.call_args.kwargs['items']],
                                 [VIDEO, returning_id])
                self.assertEqual(result['new_video_ids'], [])
                self.assertEqual(result['page_items'], 2)

    def test_receipt_identity_mismatch_is_not_invented_success(self):
        repository = Repository()
        original = repository.settle_page
        def forged(*args, **kw):
            return {**original(*args, **kw), "channel_id": "foreign"}
        repository.settle_page = forged
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            self.run_page(repository)

class SharedPassTests(unittest.TestCase):
    def run_pass(self, repository, provider, limit, **kw):
        return run_typed_inventory_batch(repository=repository, fetch_page=provider,
            limit=limit, worker_id="offline-test", prepare=False, now=lambda: NOW, **kw)

    def test_zero_does_not_claim_or_prepare(self):
        repository, provider = Mock(), Mock()
        result = self.run_pass(repository, provider, 0)
        self.assertEqual(result["provider_attempts"], 0)
        self.assertFalse(repository.mock_calls); provider.assert_not_called()

    def test_round_robin_is_one_page_each_not_five_before_next(self):
        repository, initial = Repository(), [claim(kind) for kind in ("long", "short", "stream")]
        calls, seen = [], []
        def claims(**kw):
            calls.append(kw)
            if len(calls) == 1:
                return initial
            if len(calls) == 2:
                self.assertEqual([p["video_type"] for p in kw["continuations"]], ["long", "short", "stream"])
                return [claim("long", active_scan_id=repository.receipts[0]["scan_id"], resume_page_token="next")]
            return []
        repository.claim_pass = claims
        def fetch(playlist, **kw):
            seen.append(playlist[:4])
            return page("next" if not kw["page_token"] else "later")
        result = self.run_pass(repository, fetch, 4)
        self.assertEqual(seen, ["UULF", "UUSH", "UULV", "UULF"])
        self.assertEqual(result["provider_attempts"], 4)
        self.assertEqual(result["errors"], 0)
        self.assertTrue(result["budget_exhausted"])
        self.assertTrue(all(call["include_due"] for call in calls))

    def test_partial_format_request_is_not_silently_widened_to_all_channels(self):
        repository = Repository()
        repository.claim_pass = Mock()
        provider = Mock()
        with self.assertRaisesRegex(ValueError, "requires explicit channels"):
            self.run_pass(repository, provider, 1, video_types=("short",))
        repository.claim_pass.assert_not_called()
        provider.assert_not_called()

    def test_addressed_continuation_excludes_global_due(self):
        repository, seen = Repository(), []
        repository.claim_channels = Mock(return_value=[claim("short")])
        def continuation(**kw):
            seen.append(kw)
            proof = kw["continuations"][0]
            return [claim("short", active_scan_id=proof["scan_id"], resume_page_token="next")]
        repository.claim_pass = continuation
        provider = Mock(side_effect=[page("next"), page()])
        result = self.run_pass(repository, provider, 2,
            explicit_channel_ids=[CHANNEL], video_types=("short",),
            expected_channel_scopes=[dict(channel_id=CHANNEL, control_epoch=7)])
        self.assertEqual(result["errors"], 0)
        self.assertFalse(seen[0]["include_due"])
        self.assertEqual(seen[0]["continuations"][0]["control_epoch"], 7)
        self.assertEqual(provider.call_count, 2)

    def test_quota_uses_planned_receipt_and_does_not_request_pending(self):
        class YouTubeDailyQuotaExceededError(RuntimeError): pass
        repository, owned = Repository(), [claim("long"), claim("short")]
        repository.claim_pass = Mock(return_value=owned)
        provider = Mock(side_effect=YouTubeDailyQuotaExceededError())
        result = self.run_pass(repository, provider, 2)
        self.assertTrue(result["quota_exhausted"])
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["provider_attempts"], 1)
        self.assertEqual(result["quota_deferred"], 2)
        self.assertEqual(repository.events[-1][1], owned)
        self.assertEqual(repository.events[-1][2]["transition_id"], result["results"][0]["transition_id"])
        provider.assert_called_once()

    def test_uncertain_ack_stops_and_only_releases_unstarted(self):
        repository, owned = Repository(), [claim("long"), claim("short")]
        repository.claim_pass = Mock(return_value=owned)
        repository.settle_page = Mock(side_effect=DatabaseUnavailable('database_connection_failed', outcome_unknown=True))
        with patch("db.hot.hot_collection_control.abort_pending_playlists") as release:
            result = self.run_pass(repository, Mock(return_value=page()), 2)
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["provider_attempts"], 1)
        release.assert_called_once_with(repository.database, owned[1:])

    def test_more_than_1000_attempts_remain_bounded_and_all_callbacks_run(self):
        repository, observed = Repository(), []
        def next_claim(**kw):
            if repository.receipts:
                previous = repository.receipts[-1]
                return [claim("long", active_scan_id=previous["scan_id"],
                              resume_page_token=previous["resume_page_token"])]
            return [claim("long")]
        repository.claim_pass = next_claim
        calls = 0
        def fetch(*args, **kwargs):
            nonlocal calls
            calls += 1
            return page("page-" + str(calls))
        result = self.run_pass(repository, fetch, 1001, on_page=lambda item: observed.append(item["transition_id"]))
        self.assertEqual(calls, 1001)
        self.assertEqual(result["provider_attempts"], 1001)
        self.assertEqual(len(set(observed)), 1001)
        self.assertEqual(len(result["results"]), 500)
        self.assertEqual(result["results_omitted"], 501)
        self.assertEqual(result["errors"], 0)

    def test_no_silent_1000_page_clamp(self):
        repository = Repository()
        repository.claim_pass = Mock(return_value=[])
        result = self.run_pass(repository, Mock(), 6000)
        self.assertFalse(result["budget_exhausted"])
        self.assertEqual(repository.claim_pass.call_args.kwargs["limit"], 50)

class PageRepositoryAckTests(unittest.TestCase):
    def test_transport_retry_replays_receipt_without_another_provider_page(self):
        owned, observed, saved = claim(), [], []
        provider = Mock(return_value=page())
        fixture = Repository()
        def begin(database, **params):
            observed.append(("prepared_begin", dict(params)))
            return dict(status="running", scan_id=params["scan_id"],
                resume_page_token=None, scan_boundary_at=NOW.isoformat())
        def settle(database, **params):
            self.assertIs(database, repository.database)
            observed.append(("prepared_settle", dict(params)))
            if not saved:
                saved.append(fixture.settle_page(owned, scan_id=params["scan_id"],
                    transition_id=params["transition_id"], items=params["items"],
                    page_status=params["page_status"], next_page_token=params["next_page_token"]))
                raise DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
            return {**saved[0], "replayed": True}
        repository = TypedInventoryRepository(SimpleNamespace(call=Mock(side_effect=AssertionError('RPC forbidden'))))
        budget = PageBudget(1)
        with patch("db.inventory.typed_inventory.emit_event"), patch("db.inventory.typed_inventory.inventory_scans.begin_typed_inventory_scan", side_effect=begin), patch("db.inventory.inventory_scan_pages.settle_typed_inventory_page", side_effect=settle):
            result = run_typed_inventory_claim(repository=repository, claim=owned,
                fetch_page=provider, page_budget=budget, now=lambda: NOW)
        self.assertEqual([name for name, _ in observed],
                         ["prepared_begin", "prepared_settle", "prepared_settle"])
        self.assertEqual(observed[1][1], observed[2][1])
        self.assertTrue(result["receipt"]["replayed"])
        self.assertEqual(budget.attempts, 1)
        provider.assert_called_once()

    def test_old_or_partial_shared_contract_rejected_before_claim(self):
        good = dict(status="ok", schema_version=2, runtime_video_types=["long", "short", "stream"],
                    durable_fairness=True, product_exposure_independent_of_active_scan=True, initial_chunk_pages=1)
        repository = TypedInventoryRepository(object())
        repository.assert_quota_defer_ready = Mock(return_value={})
        for bad in ({"status": "ok", "schema_version": 1}, {**good, "durable_fairness": False},
                    {**good, "runtime_video_types": ["short", "stream"]}):
            with patch("db.inventory.typed_inventory.capabilities.assert_typed_inventory_runtime_schema", return_value=bad) as check:
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    repository.assert_ready()
            check.assert_called_once_with(repository.database)
        repository.assert_quota_defer_ready.assert_not_called()
        with patch("db.inventory.typed_inventory.capabilities.assert_typed_inventory_runtime_schema", return_value=good) as check:
            self.assertEqual(repository.assert_ready(), good)
        check.assert_called_once_with(repository.database)
        repository.assert_quota_defer_ready.assert_called_once()

class PassRepositoryTests(unittest.TestCase):
    def test_closed_exact_pair_rpc_and_zero_no_request(self):
        repository = TypedInventoryRepository(object())
        repository._rpc = Mock(return_value=[])
        proof = dict(channel_id=CHANNEL, video_type="long", scan_id=str(uuid.uuid4()),
                     control_epoch=7, last_transition_id=str(uuid.uuid4()))
        repository.claim_pass(continuations=[proof], limit=2, include_due=False, worker_id="w", run_id="r")
        params = repository._rpc.call_args.args[1]
        self.assertEqual(params["p_continuations"], [proof])
        self.assertIs(params["p_include_due"], False)
        repository._rpc.reset_mock()
        repository.claim_pass(continuations=[], limit=0, include_due=True, worker_id="w", run_id="r")
        repository._rpc.assert_not_called()
        for invalid in ({**proof, "page_token": "not-allowed"}, {**proof, "control_epoch": True}):
            with self.assertRaises(ValueError):
                repository.claim_pass(continuations=[invalid], limit=1, include_due=True, worker_id="w", run_id="r")
        repository._rpc.assert_not_called()

if __name__ == "__main__": unittest.main()
