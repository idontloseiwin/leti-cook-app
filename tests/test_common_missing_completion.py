"""Shared missing owner: bounded exact receipts; no database or provider I/O."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from db.videos.video_collection import VideoCollectionClaim, VideoCollectionRepository
from hot_runtime import HotDatabaseAdmissionDenied
from video_collection_service import VideoCollectionService

STAMP = "2026-09-27T12:00:00+00:00"

def row(index, kind="long"):
    return dict(video_id=f"v{index:010d}", channel_id="UC" + "a" * 22,
                video_type=kind, lease_token="lease", collection_attempt_id="attempt",
                recorded_at=STAMP, availability_status="unknown_unavailable",
                availability_source="caller_reason")

def claim(index, kind):
    return VideoCollectionClaim(video_id=f"v{index:010d}", channel_id="UC" + "a" * 22,
        video_type=kind, collection_mode="metadata", lease_token="lease",
        lease_expires_at=STAMP, next_due_at=STAMP)

class CommonMissingRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = VideoCollectionRepository(object())
        self.repo._rpc = Mock(side_effect=lambda _rpc, params, **_kw: len(params["p_rows"]))
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()

    def test_one_existing_rpc_accepts_all_three_types_without_payload_rewrite(self):
        rows = [row(i, kind) for i, kind in enumerate(("long", "short", "stream"))]
        self.assertEqual(self.repo.complete_missing(rows), 3)
        self.repo._rpc.assert_called_once_with("complete_typed_video_missing", {"p_rows": rows})
        self.assertEqual(rows[0]["availability_source"], "caller_reason")

    def test_transactions_remain_at_most_fifty_rows(self):
        rows = [row(i, ("long", "short", "stream")[i % 3]) for i in range(101)]
        self.assertEqual(self.repo.complete_missing(rows), 101)
        self.assertEqual([len(c.args[1]["p_rows"]) for c in self.repo._rpc.call_args_list], [50, 50, 1])
        self.assertEqual([r for c in self.repo._rpc.call_args_list for r in c.args[1]["p_rows"]], rows)

    def test_lost_ack_replays_the_same_batch_object_once(self):
        self.repo._rpc.side_effect = [ConnectionError("lost"), 1]
        self.assertEqual(self.repo.complete_missing([row(1)]), 1)
        first, second = self.repo._rpc.call_args_list
        self.assertIs(first.args[1]["p_rows"], second.args[1]["p_rows"])
        self.assertEqual(first.kwargs, {})
        self.assertEqual(second.kwargs, {"abort_on_denied": False})

    def test_hot_denial_never_replays(self):
        self.repo._rpc.side_effect = HotDatabaseAdmissionDenied()
        with self.assertRaises(HotDatabaseAdmissionDenied): self.repo.complete_missing([row(1)])
        self.assertEqual(self.repo._rpc.call_count, 1)

    def test_unknown_second_ack_does_not_send_later_chunks(self):
        self.repo._rpc.side_effect = [ConnectionError("lost"), ConnectionError("still unknown")]
        with self.assertRaises(ConnectionError): self.repo.complete_missing([row(i) for i in range(51)])
        self.assertEqual(self.repo._rpc.call_count, 2)
        self.assertEqual(len(self.repo._rpc.call_args.args[1]["p_rows"]), 50)

    def test_exact_count_mismatch_is_not_success(self):
        self.repo._rpc.side_effect = None
        self.repo._rpc.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "completed 0 of 1"):
            self.repo.complete_missing([row(1)])
        self.assertEqual(self.repo._rpc.call_count, 1)

    def test_compatibility_count_boundaries_remain_unchanged_for_large_calls(self):
        # Long checked each 50-row receipt; typed checked its final aggregate.
        # Keep that legacy distinction while normal service groups stay <=50.
        self.repo._rpc.side_effect = [49, 2]
        with self.assertRaisesRegex(RuntimeError, "completed 49 of 50"):
            self.repo.complete_long_missing([row(i) for i in range(51)])
        self.assertEqual(self.repo._rpc.call_count, 1)
        self.repo._rpc.reset_mock()
        self.repo._rpc.side_effect = [49, 2]
        self.assertEqual(self.repo.complete_typed_missing([row(i, "short") for i in range(51)]), 51)
        self.assertEqual(self.repo._rpc.call_count, 2)

    def test_compatibility_entrypoints_keep_their_former_type_contract(self):
        self.assertEqual(self.repo.complete_long_missing([row(1)]), 1)
        self.assertEqual(self.repo.complete_typed_missing([row(2, "short"), row(3, "stream")]), 2)
        for method, rows in ((self.repo.complete_long_missing, [row(1, "short")]),
                             (self.repo.complete_typed_missing, [row(1)]),
                             (self.repo.complete_missing, [row(1, "invalid")])):
            self.repo._rpc.reset_mock()
            with self.assertRaises(ValueError): method(rows)
            self.repo._rpc.assert_not_called()
        self.assertEqual(self.repo.complete_missing([]), 0)

class CommonMissingServiceTests(unittest.TestCase):
    def service(self, repository):
        self.metadata = Mock(return_value=[])
        self.counters = Mock(side_effect=AssertionError("no counters for omitted metadata"))
        return VideoCollectionService(repository, fetch_full=Mock(side_effect=AssertionError("no full fallback")),
            fetch_metadata=self.metadata, fetch_counters=self.counters,
            build_full_payload=Mock(side_effect=AssertionError("no payload for omitted metadata")),
            build_counter_payload=Mock(side_effect=AssertionError("no counters payload")))

    def test_existing_group_order_and_shared_attempt_clock_are_unchanged(self):
        repository = Mock(spec=["complete_missing", "fail_batch"])
        repository.complete_missing.side_effect = lambda rows: len(rows)
        service = self.service(repository)
        result = service.process_common_claims([claim(3, "stream"), claim(1, "long"), claim(2, "short")], reason="caller_reason")
        groups = [c.args[0] for c in repository.complete_missing.call_args_list]
        self.assertEqual([[r["video_type"] for r in group] for group in groups], [["long"], ["short"], ["stream"]])
        self.assertEqual(len({r["collection_attempt_id"] for group in groups for r in group}), 1)
        self.assertEqual(len({r["recorded_at"] for group in groups for r in group}), 1)
        self.assertEqual({r["availability_source"] for group in groups for r in group}, {"caller_reason"})
        self.assertEqual(result["unavailable_marked"], 3)
        self.metadata.assert_called_once()
        self.counters.assert_not_called()
        repository.fail_batch.assert_not_called()

    def test_unknown_ack_stops_remaining_type_groups_without_failure_write(self):
        repository = Mock(spec=["complete_missing", "fail_batch"])
        repository.complete_missing.side_effect = ConnectionError("unknown acknowledgement")
        result = self.service(repository).process_common_claims([claim(1, "long"), claim(2, "short"), claim(3, "stream")])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["unavailable_marked"], 0)
        self.assertEqual(repository.complete_missing.call_count, 1)
        repository.fail_batch.assert_not_called()

if __name__ == "__main__": unittest.main()
