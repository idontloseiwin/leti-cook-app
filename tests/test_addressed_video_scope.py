"""Offline addressed-action scope tests with real repository/runner methods.

Reads use a bounded direct database recorder. The test RPC adapter
checks captured epochs and returns fixed claims; PostgreSQL admission itself
is covered by the separate inert video-sql contract scripts. No core bootstrap,
provider, server, SQL or external HTTP is used here. HOT readiness is a
synthetic fixture; these tests do not assert the HOT runtime contract.
"""
from copy import deepcopy
from pathlib import Path
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from db.videos.video_collection import VideoCollectionRepository
from video_collection_service import run_claimed_video_collection, _common_empty_summary

A = "UC" + "a" * 22
B = "UC" + "b" * 22
NOW = "2026-09-27T10:00:00+00:00"


class MemoryScopeDatabase:
    def __init__(self, video_channels, epochs):
        self.video_channels = dict(video_channels)
        self.epochs = dict(epochs)
        self.reads = []
        self.hidden = set()
        self.omit_channels = set()
        self.gateway = self

    def read_rows(self, operation, statement, parameters=(), *, max_rows):
        tables = {'collection_addressed_video_channels': 'video_inventory',
                  'collection_addressed_channel_scopes': 'channels_with_control'}
        table = tables[operation]
        ids, limit = parameters
        assert limit == max_rows == len(ids) <= 1000
        assert 'ANY(%s::text[])' in statement
        self.reads.append((table, tuple(ids)))
        if table == "video_inventory":
            rows = [{"video_id": identity, "channel_id": self.video_channels[identity]}
                    for identity in ids if identity in self.video_channels]
        elif table == "channels_with_control":
            rows = [{"channel_id": identity, "collection_control_epoch": self.epochs[identity],
                     "collection_permission": "accepted", "is_hidden": identity in self.hidden}
                    for identity in ids if identity in self.epochs and identity not in self.omit_channels]
        else:
            raise AssertionError("unexpected table " + table)
        return rows


class ScopeRepository(VideoCollectionRepository):
    def __init__(self, database):
        super().__init__(database.gateway)
        self.memory = database
        self.calls = []

    def _rpc(self, name, params=None, **kwargs):
        params = deepcopy(params)
        self.calls.append((name, params))
        ids = params["p_video_ids"]
        scopes = params["p_channel_scopes"]
        expected = {self.memory.video_channels[value] for value in ids if value in self.memory.video_channels}
        if {row["channel_id"] for row in scopes} != expected:
            raise ValueError("channel_collection_scope_invalid")
        if any(row["control_epoch"] != self.memory.epochs[row["channel_id"]] for row in scopes):
            raise ValueError("channel_collection_scope_lost")
        if name in {"ensure_video_collection_state", "enqueue_video_collection_ids"}:
            return len(ids)
        if name not in {"claim_video_collection_ids", "claim_video_metadata_collection_ids"}:
            raise AssertionError(name)
        return [{"video_id": identity, "channel_id": self.memory.video_channels[identity],
                 "collection_mode": "metadata", "lease_token": "fixed-" + identity,
                 "lease_expires_at": NOW, "next_due_at": NOW}
                for identity in ids if identity in self.memory.video_channels]


class AddressedVideoScopeTests(unittest.TestCase):
    def setUp(self):
        for fixture in (
            patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"}),
            patch("hot_runtime.require_hot_runtime_ready", return_value=None),
            patch("collection_shutdown.yield_requested", return_value=False),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    def fixture(self, mapping, epochs=None):
        database = MemoryScopeDatabase(mapping, epochs or {A: 7, B: 11})
        return database, ScopeRepository(database)

    def provider(self, after_batch=None):
        calls = []
        def process(claims, *, reason, summary=None):
            yield from ()
            calls.append([claim.video_id for claim in claims])
            if after_batch:
                after_batch(len(calls))
            summary = {} if summary is None else summary
            summary.update(_common_empty_summary(reason))
            summary.update(requested=len(claims), updated=len(claims), batches=1,
                           video_ids=[claim.video_id for claim in claims])
            return summary
        return SimpleNamespace(common_claim_steps=process,
                               finish_common_claims=lambda claims, result: result), calls

    def test_new_action_captures_once_and_subsets_each_actual_claim_batch(self):
        ids = [f"v{i:010d}" for i in range(51)]
        database, repository = self.fixture({identity: A if i < 50 else B for i, identity in enumerate(ids)})
        service, calls = self.provider()
        result = run_claimed_video_collection(repository, service, limit=51, worker_id="test", explicit_video_ids=ids)
        self.assertEqual(result["updated"], 51)
        self.assertEqual([len(batch) for batch in calls], [50, 1])
        self.assertEqual([table for table, _ in database.reads], ["video_inventory", "channels_with_control"])
        self.assertEqual([params["p_channel_scopes"] for name, params in repository.calls if name.startswith("claim_")],
                         [[{"channel_id": A, "control_epoch": 7}], [{"channel_id": B, "control_epoch": 11}]])

    def test_remove_restore_between_provider_batches_never_reloads_epoch(self):
        ids = [f"v{i:010d}" for i in range(51)]
        database, repository = self.fixture(dict.fromkeys(ids, A))
        service, calls = self.provider(lambda number: database.epochs.update({A: 8}))
        result = run_claimed_video_collection(repository, service, limit=51, worker_id="test", explicit_video_ids=ids)
        self.assertEqual([len(batch) for batch in calls], [50])
        self.assertEqual(result["stopped_reason"], "claim_failed")
        self.assertEqual([params["p_channel_scopes"][0]["control_epoch"] for _, params in repository.calls], [7, 7, 7])
        self.assertEqual(sum(table == "channels_with_control" for table, _ in database.reads), 1)

    def test_nested_initial_scope_is_not_reauthorized_before_prepare(self):
        database, repository = self.fixture({"video000001": A}, {A: 8})
        service, calls = self.provider()
        with patch.object(repository, "load_channel_scopes", side_effect=AssertionError("parent scope refreshed")):
            result = run_claimed_video_collection(repository, service, limit=1, worker_id="test",
                explicit_video_ids=["video000001"], channel_scopes=[{"channel_id": A, "control_epoch": 7}])
        self.assertEqual(calls, [])
        self.assertEqual(result["stopped_reason"], "queue_prepare_failed")
        self.assertEqual(repository.calls[0][1]["p_channel_scopes"], [{"channel_id": A, "control_epoch": 7}])
        self.assertEqual([table for table, _ in database.reads], ["video_inventory"])

    def test_parent_scope_is_copied_before_provider_can_mutate_the_input(self):
        ids = [f"v{i:010d}" for i in range(51)]
        database, repository = self.fixture(dict.fromkeys(ids, A))
        parent = [{"channel_id": A, "control_epoch": 7}]
        service, calls = self.provider(lambda number: parent[0].update(control_epoch=999))
        result = run_claimed_video_collection(repository, service, limit=51, worker_id="test",
            explicit_video_ids=ids, channel_scopes=parent)
        self.assertEqual(result["updated"], 51)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(params["p_channel_scopes"] == [{"channel_id": A, "control_epoch": 7}]
                            for _, params in repository.calls))
        self.assertFalse(any(table == "channels_with_control" for table, _ in database.reads))

    def test_repository_chunks_use_exact_scope_without_queries(self):
        ids = [f"v{i:010d}" for i in range(1001)]
        mapping = {identity: A if i < 1000 else B for i, identity in enumerate(ids)}
        database, repository = self.fixture(mapping)
        scopes = [{"channel_id": A, "control_epoch": 7}, {"channel_id": B, "control_epoch": 11}]
        self.assertEqual(repository.ensure_video_ids(ids, channel_scopes=scopes, video_channels=mapping), 1001)
        for claim in (repository.claim_video_ids, repository.claim_video_metadata_ids):
            self.assertEqual(len(claim(ids, channel_scopes=scopes, video_channels=mapping, worker_id="test", run_id="run")), 1001)
        self.assertEqual(database.reads, [])
        self.assertEqual([len(params["p_video_ids"]) for _, params in repository.calls], [1000, 1] * 3)
        self.assertEqual([params["p_channel_scopes"] for _, params in repository.calls], [[scopes[0]], [scopes[1]]] * 3)

    def test_capture_reads_are_bounded_and_missing_canonical_row_fails_closed(self):
        mapping = {f"v{i:010d}": "UC" + f"{i:022d}" for i in range(1001)}
        database, repository = self.fixture(mapping, dict.fromkeys(mapping.values(), 3))
        captured = repository.load_video_channel_map(mapping)
        self.assertEqual(captured, mapping)
        self.assertEqual(len(repository.load_channel_scopes(captured.values())), 1001)
        self.assertEqual([len(ids) for _, ids in database.reads], [1000, 1, 1000, 1])
        database.omit_channels.add(next(iter(mapping.values())))
        with self.assertRaisesRegex(ValueError, "channel_collection_scope_lost"):
            repository.load_channel_scopes(mapping.values())

    def test_repository_requires_supplied_scope_and_validates_before_first_rpc(self):
        _, repository = self.fixture({"video000001": A})
        with self.assertRaises(TypeError): repository.ensure_video_ids(["video000001"])
        for claim in (repository.claim_video_ids, repository.claim_video_metadata_ids):
            with self.assertRaises(TypeError): claim(["video000001"], worker_id="test", run_id="run")
        for scopes in ([], [{"channel_id": A, "control_epoch": True}],
                       [{"channel_id": A, "control_epoch": 7}] * 2):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                repository.ensure_video_ids(["video000001"], channel_scopes=scopes, video_channels={"video000001": A})
        self.assertEqual(repository.calls, [])

    def test_hidden_new_action_stops_before_preparation_or_provider(self):
        database, repository = self.fixture({"video000001": A})
        database.hidden.add(A)
        service, calls = self.provider()
        result = run_claimed_video_collection(repository, service, limit=1, worker_id="test", explicit_video_ids=["video000001"])
        self.assertEqual(result["stopped_reason"], "queue_prepare_failed")
        self.assertEqual(repository.calls, [])
        self.assertEqual(calls, [])

    def test_unknown_inventory_ids_remain_noop_and_force_metadata_preserves_scope(self):
        _, repository = self.fixture({"video000001": A})
        service, calls = self.provider()
        result = run_claimed_video_collection(repository, service, limit=2, worker_id="test",
            explicit_video_ids=["unknown0001", "video000001"], force_metadata=True)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(calls, [["video000001"]])
        self.assertEqual(repository.calls[-1][0], "claim_video_metadata_collection_ids")
        self.assertEqual(repository.calls[-1][1]["p_channel_scopes"], [{"channel_id": A, "control_epoch": 7}])

    def test_discovery_enqueue_keeps_parent_epoch_without_reauthorization(self):
        database, repository = self.fixture({"video000001": A}, {A: 8})
        with self.assertRaises(TypeError): repository.enqueue_video_ids(["video000001"])
        with self.assertRaisesRegex(ValueError, "channel_collection_scope_lost"):
            repository.enqueue_video_ids(["video000001"], channel_scopes=[{"channel_id": A, "control_epoch": 7}])
        self.assertEqual(database.reads, [])
        self.assertEqual(repository.calls, [("enqueue_video_collection_ids", {
            "p_video_ids": ["video000001"], "p_channel_scopes": [{"channel_id": A, "control_epoch": 7}],
        })])

    def test_empty_forced_metadata_preflight_delegates_without_resolving_scope(self):
        database, repository = self.fixture({})
        self.assertFalse(hasattr(repository, "_telemetry_rpc"))
        with patch('db.readiness.collection_preflight.assert_forced_metadata_claim_ready', return_value=[]) as ready:
            self.assertEqual(repository.assert_forced_metadata_claim_ready(worker_id="test", run_id="run"), [])
        ready.assert_called_once_with(database, worker_id="test", run_id="run", lease_seconds=300)
        self.assertEqual(database.reads, [])
        self.assertEqual(repository.calls, [])


if __name__ == "__main__":
    unittest.main()
