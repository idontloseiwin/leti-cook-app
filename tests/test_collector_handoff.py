"""Offline handoff regressions: real collector functions, fake DB/provider, simulated time."""
import ast
from functools import partial
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from collection.settings import CollectionSettings
from collection.context import CollectionContext
import collection_shutdown as shutdown
from collection_outcome import collection_outcome_fields, copy_collection_stop, sync_completion_fields
from collection_schedule import CollectionSchedules, ScheduleError
from runtime.collection_schedules import require_schedule_model
from self_hosted_collection import CollectionSupervisor, TERMINATE_GRACE_SECONDS
from self_hosted_settings import browser_settings, preset, read_settings_file, settings_ini, SettingsError
from db import VideoCollectionClaim
from db.base.postgres import DatabaseServerError, DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied
import operator_jobs
from video_collection_service import VideoCollectionService, run_claimed_common_video_collection
from ops.self_hosted_collect import operator_arguments
from collection import inventory as inventory_service


def settings():
    return dict(preset="standard", limits=preset("standard"), automatic_collection=True,
                interval_minutes=30, manual_stats_quota_units=200, handoff_grace_seconds=120)


class Clock:
    now = 1_000
    def __call__(self):
        return self.now


class Pipe:
    closed = False
    def __init__(self):
        self.commands = []
    def write(self, value):
        self.commands.append(value)
    def close(self):
        self.closed = True


class Process:
    def __init__(self):
        self.stdin, self.code, self.signals = Pipe(), None, []
    def poll(self):
        return self.code
    def terminate(self):
        self.signals.append("terminate")
    def kill(self):
        self.signals.append("kill")  # deliberately not reaped yet


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.wall = Clock(), Clock()
        self.value = {"prepared": True, "phase": "ready", "settings": settings()}
        manager = types.SimpleNamespace(lock=threading.RLock(),
                                        store=types.SimpleNamespace(read=lambda: self.value))
        self.starts = []
        def spawn(args, **kw):
            process = Process()
            self.starts.append((args[-1], kw, process))
            return process
        self.supervisor = CollectionSupervisor(manager, ready=lambda: None, schedules=CollectionSchedules(), spawn=spawn,
                                               clock=self.clock, wall_clock=self.wall)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {"OPERATOR_JOB_WAKE_PATH": self.directory.name + "/wake"})
        env.start()
        self.addCleanup(env.stop)
        self.supervisor.tick()
        self.process = self.starts[0][2]

    def test_collection_pause_finishes_owned_batch_once_and_waits_before_replacement(self):
        self.supervisor.require_ready = Mock(side_effect=HotDatabaseAdmissionDenied('hot_runtime_not_ready'))
        self.supervisor.tick()
        self.supervisor.tick()
        self.assertEqual(self.process.stdin.commands, [b"stop\n"])
        self.assertEqual(self.process.signals, [])
        self.assertEqual(len(self.starts), 1)
        self.process.code = 0
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 1)
        self.supervisor.require_ready = lambda: None
        self.supervisor.tick()
        self.assertEqual([kind for kind, _, _ in self.starts], ['auto', 'operator'])

    def test_initial_load_concurrency_changes_only_after_supervisor_restart(self):
        self.assertEqual(self.starts[0][1]['env']['LIC_YOUTUBE_REQUEST_CONCURRENCY'], '8')
        self.value['settings']['youtube_request_concurrency'] = 1
        self.process.code = 0
        self.clock.now += 30
        self.supervisor.tick()
        self.assertEqual(self.starts[-1][1]['env']['LIC_YOUTUBE_REQUEST_CONCURRENCY'], '8')
        manager = self.supervisor.manager
        restarted = CollectionSupervisor(manager, ready=lambda: None, schedules=CollectionSchedules(),
            spawn=self.supervisor.spawn, clock=self.clock, wall_clock=self.wall)
        restarted.tick()
        self.assertEqual(self.starts[-1][1]['env']['LIC_YOUTUBE_REQUEST_CONCURRENCY'], '1')

    def test_auto_uses_full_interval_after_actual_start(self):
        self.assertEqual(self.supervisor.deadline, 2800)
        self.clock.now = 2799
        self.supervisor.tick()
        self.assertEqual(self.process.stdin.commands, [])
        self.clock.now = 2800
        self.supervisor.tick()
        self.assertEqual(self.process.stdin.commands, [b"stop\n"])
        self.assertEqual(self.process.signals, [])
        self.assertEqual(self.supervisor.status()["state"], "finishing_batch")
        self.assertEqual(len(self.starts), 1)

    def test_cooperative_exit_needs_no_terminate_and_allows_next_worker(self):
        self.clock.now = 2800
        self.supervisor.tick()
        self.process.code = 0
        self.clock.now += 3
        self.supervisor.tick()
        self.assertEqual(self.process.signals, [])
        self.assertEqual(len(self.starts), 2)
        self.assertEqual(self.supervisor.status()["timeouts"], 0)

    def test_hung_worker_escalates_once_and_replacement_waits_for_exit(self):
        self.clock.now = 2800
        self.supervisor.tick()
        self.clock.now += 119
        self.supervisor.tick()
        self.assertEqual(self.process.signals, [])
        self.clock.now += 1
        self.supervisor.tick()
        self.assertEqual(self.process.signals, ["terminate"])
        self.clock.now += TERMINATE_GRACE_SECONDS
        self.supervisor.tick()
        self.supervisor.tick()
        self.assertEqual(self.process.signals, ["terminate", "kill"])
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.supervisor.status()["timeouts"], 1)
        self.process.code = -9
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 2)

    def test_wall_clock_jumps_do_not_change_deadlines(self):
        self.wall.now = 10**12
        self.supervisor.tick()
        self.wall.now = -10**12
        self.supervisor.tick()
        self.assertEqual(self.process.stdin.commands, [])
        self.assertEqual(self.supervisor.status()["elapsed_seconds"], 0)

    def test_short_auto_does_not_delay_operator_and_operator_has_a_budget(self):
        self.process.code = 0
        self.clock.now += 30
        self.supervisor.tick()
        kind, options, operator = self.starts[-1]
        self.assertEqual(kind, "operator")
        self.assertEqual(options["env"]["LIC_OPERATOR_SLICE_SECONDS"], "900")
        self.assertEqual(self.supervisor.deadline, self.clock.now + 900)
        self.clock.now += 900
        self.supervisor.tick()
        self.assertEqual(operator.stdin.commands, [b"stop\n"])

    def test_overrun_does_not_starve_operator_or_replay_missed_ticks(self):
        self.clock.now = 10000
        self.supervisor.tick()
        self.process.code = 0
        self.supervisor.tick()
        self.assertEqual([kind for kind, _, _ in self.starts], ["auto", "operator"])
        self.starts[-1][2].code = 0
        self.clock.now += 10
        self.supervisor.tick()
        self.assertEqual([kind for kind, _, _ in self.starts], ["auto", "operator", "auto"])
        self.assertEqual(self.supervisor.next_auto, self.clock.now + 1800)

    def test_failed_control_write_does_not_start_another_worker(self):
        self.process.stdin.write = Mock(side_effect=BrokenPipeError)
        self.clock.now = 2800
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.supervisor.status()["state"], "handoff_unconfirmed")
        self.clock.now += 120
        self.supervisor.tick()
        self.assertEqual(self.process.signals, ["terminate"])

    def test_paused_collection_does_not_restart_after_current_exit(self):
        self.value["settings"]["automatic_collection"] = False
        self.process.code = 0
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.supervisor.status()["state"], "paused")

    def test_config_enable_resumes_same_supervisor_without_resetting_auto_due(self):
        config = Path(self.directory.name, "config.ini")
        enabled = settings_ini(self.value["settings"])
        config.write_text(enabled.replace("automatic_collection = true",
                                          "automatic_collection = false"), encoding="utf-8")
        self.supervisor.manager.store.read = lambda: {
            **self.value, "settings": read_settings_file(config)}
        auto_due = self.supervisor.next_auto
        self.process.code = 0
        self.clock.now += 30
        self.supervisor.tick()
        self.clock.now += 3
        self.supervisor.tick()
        self.assertEqual(self.supervisor.status()["state"], "paused")
        self.assertIsNone(self.supervisor.process)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(self.supervisor.next_auto, auto_due)

        config.write_text(enabled, encoding="utf-8")
        self.supervisor.tick()
        self.assertEqual([kind for kind, _, _ in self.starts], ["auto", "operator"])
        self.assertEqual(self.supervisor.status()["state"], "running")
        self.assertEqual(self.supervisor.next_auto, auto_due)
        self.starts[-1][2].code = 0
        self.clock.now = auto_due - 1
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 2)
        self.clock.now = auto_due
        self.supervisor.tick()
        self.assertEqual([kind for kind, _, _ in self.starts], ["auto", "operator", "auto"])
        self.assertEqual(self.supervisor.next_auto, auto_due + 30 * 60)


class ControlTestCase(unittest.TestCase):
    def setUp(self):
        shutdown._requested.clear()
        self.addCleanup(shutdown._requested.clear)


class ShutdownTests(ControlTestCase):
    def test_pipe_request_is_deferred_until_operator_chunk_acknowledgement(self):
        with shutdown.finish_current_chunk():
            shutdown.read_control(io.BytesIO(b"stop\n"))
            self.assertFalse(shutdown.stop_at_boundary({}))
        summary = {}
        self.assertTrue(shutdown.stop_at_boundary(summary))
        self.assertEqual(summary["stopped_reason"], "scheduled_handoff")

    def test_eof_requests_cooperative_exit(self):
        shutdown.read_control(io.BytesIO())
        self.assertTrue(shutdown.yield_requested())

    def test_unknown_command_does_not_change_control(self):
        shutdown.read_control(io.BytesIO(b"bad\n"))
        self.assertFalse(shutdown.yield_requested())

    def test_release_error_is_not_retried_or_hidden(self):
        summary = {}
        with patch("db.hot.hot_collection_control.abort_pending_playlists", side_effect=RuntimeError) as abort:
            shutdown.release_unstarted_playlists(types.SimpleNamespace(database=object()), ["pending"], summary)
        self.assertEqual(abort.call_count, 1)
        self.assertTrue(summary["settlement_unknown"])
        self.assertEqual(summary["errors"], 1)

    def test_real_child_finishes_current_unit_before_observing_prequeued_command(self):
        # Bounded <5 s subprocess with no imports of core, credentials, DB or HTTP.
        source = """
import sys,time
sys.path.insert(0,sys.argv[1])
from collection_shutdown import start_control_reader,yield_requested
print('started',flush=True)
start_control_reader(sys.stdin.buffer)
time.sleep(0.05)
print('unit_committed',flush=True)
deadline=time.monotonic()+2
while not yield_requested() and time.monotonic()<deadline:time.sleep(0.005)
print('yielded' if yield_requested() else 'missing',flush=True)
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", source, str(ROOT/"application")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = child.communicate(b"stop\n", timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(out.decode().splitlines(), ["started", "unit_committed", "yielded"])

    def test_real_child_exits_cleanly_while_the_reader_still_waits(self):
        # The supervisor keeps the pipe open and sends nothing; the worker ends its
        # work and exits. A reader on the buffered stdin made Python abort here.
        source = """
import sys
sys.path.insert(0,sys.argv[1])
from collection_shutdown import start_control_reader
start_control_reader(sys.stdin.buffer)
print('work_done',flush=True)
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", source, str(ROOT/"application")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            code = child.wait(timeout=5)
            out, err = child.stdout.read(), child.stderr.read()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()
        self.assertNotIn(b"Fatal Python error", err)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.decode().splitlines(), ["work_done"])


class VideoBoundaryTests(ControlTestCase):
    def run_collection(self, *, unknown_ack=False):
        calls = []
        claim = VideoCollectionClaim(video_id="vid00000001", channel_id="UC"+"c"*22,
            collection_mode="counters", lease_token="token", lease_expires_at="2026-09-24T10:00:00Z",
            next_due_at="2026-09-24T00:00:00Z")
        class Repository:
            def reconcile_typed(self, *a, **kw):
                return {"affected_count": 0}
            def claim_due_batch(self, **kw):
                calls.append("claim")
                return dict(rows=[claim], inspected_count=1, paused_count=0, scan_exhausted=False)
            def load_counter_presence(self, ids):
                return {video_id: True for video_id in ids}
            def complete_counters(self, rows):
                calls.append("commit")
                if unknown_ack:
                    raise DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
                return len(rows)
            def fail_batch(self, rows):
                calls.append("fail")
        repository = Repository()
        def counters(ids, phase):
            calls.append("provider")
            shutdown.read_control(io.BytesIO(b"stop\n"))
            return {"items": [{"id": ids[0], "views": 10}], "summary": {"failed_ids": []}}
        service = VideoCollectionService(repository, fetch_full=lambda *a: [],
            fetch_metadata=lambda *a: [], fetch_counters=counters, build_full_payload=lambda *a: {},
            build_counter_payload=lambda row, at: {"snapshot": {"views": row["views"], "recorded_at": at}})
        result = run_claimed_common_video_collection(repository, service, limit=100, worker_id="offline")
        return result, calls

    def test_request_during_provider_call_still_commits_then_takes_no_new_claim(self):
        result, calls = self.run_collection()
        self.assertEqual(calls, ["claim", "provider", "commit"])
        self.assertEqual((result["updated"], result["errors"]), (1, 0))
        self.assertEqual((result["completion_outcome"], result["stopped_reason"]),
                         ("partial", "scheduled_handoff"))

    def test_unknown_commit_ack_does_not_release_or_retry_whole_batch(self):
        result, calls = self.run_collection(unknown_ack=True)
        self.assertEqual(calls, ["claim", "provider", "commit"])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["completion_outcome"], "error")


def functions_from_file(name, names, namespace):
    path = ROOT/"application"/name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class PhaseBoundaryTests(ControlTestCase):
    def test_auto_cycle_stops_before_channel_phase_and_keeps_partial_counts(self):
        def collect(**kw):
            shutdown.read_control(io.BytesIO(b"stop\n"))
            return dict(updated=7, errors=0, yield_requested=True, stopped_reason="scheduled_handoff")
        ns = dict(CollectionContext=CollectionContext,time=time, datetime=datetime, timezone=timezone,
                  _copy_collection_stop=copy_collection_stop, sync_completion_fields=sync_completion_fields,
                  stop_at_boundary=shutdown.stop_at_boundary,
                  emit_phase=lambda *a: None, event=lambda *a,**kw: None,
                  scope=__import__("pipeline_trace").scope, update_sync_run_progress=lambda *a,**kw: None,
                  validate_required_auto_sync_rpcs=lambda *,context: {}, recover_stale_running_statuses=lambda *,context: {},
                  run_video_stats_sync_batch=collect, run_channel_metadata_sync_batch=Mock())
        functions_from_file("collection/cycle.py", {"run_auto_sync_cycle", "_run_auto_sync_cycle"}, ns)
        with patch("sys.stdout", io.StringIO()):
            result = ns["run_auto_sync_cycle"](context=types.SimpleNamespace(), inventory_page_budget=12)
        ns["run_channel_metadata_sync_batch"].assert_not_called()
        self.assertEqual(result["video_collection"]["updated"], 7)
        self.assertEqual((result["errors"], result["completion_outcome"]), (0, "partial"))
        self.assertIsNotNone(result["finished_at"])

    def test_typed_page_finishes_and_only_unstarted_claims_are_released(self):
        first, second = types.SimpleNamespace(channel_id="first", video_type="short", lease_channel_epoch=7), types.SimpleNamespace(channel_id="second", video_type="short")
        repository = types.SimpleNamespace(database=object(), claim_pass=lambda **kw: [first, second])
        def finish_page(**kw):
            self.assertIs(kw["claim"], first)
            shutdown.read_control(io.BytesIO(b"stop\n"))
            return {"status": "running", "inserted": 3, "scan_id": "scan", "transition_id": "page-ack"}
        with patch.object(inventory_service, "run_typed_inventory_claim", side_effect=finish_page) as run:
            with patch("db.hot.hot_collection_control.abort_pending_playlists") as release:
                result = inventory_service.run_typed_inventory_batch(worker_id="test-worker",
                    repository=repository, fetch_page=Mock(), limit=10, prepare=False)
        self.assertEqual(run.call_count, 1)
        release.assert_called_once_with(repository.database, [second])
        self.assertEqual(result["inserted"], 3)
        self.assertTrue(result["yield_requested"])



class OperatorBoundaryTests(ControlTestCase):
    def worker_namespace(self, repository, chunk, factory, lock_handle):
        @contextmanager
        def fresh():
            yield repository
        ns = dict(DEFAULT_AUTO_SYNC_INTERVAL_MINUTES=30,
            COLLECTION_SCHEDULES=CollectionSchedules(), require_schedule_model=require_schedule_model,
            Callable=__import__("typing").Callable, datetime=datetime, time=time, timezone=timezone,
            os=os, sys=sys, json=__import__("json"),
            operator_jobs_mode=lambda: "durable", partial=partial,
            load_collection_settings=lambda env: CollectionSettings(True, True, True),
            _auto_sync_interval_minutes=lambda value: value,
            _bounded_positive_int=lambda value, **kwargs: value if value is not None else kwargs["default"],
            DRAIN_MAX_CHUNKS_ENV="OFFLINE_UNUSED_CHUNKS", DRAIN_MAX_SECONDS_ENV="OFFLINE_UNUSED_SECONDS",
            DEFAULT_DRAIN_MAX_CHUNKS=3, DEFAULT_DRAIN_MAX_SECONDS=900,
            in_auto_sync_reserved_window=Mock(side_effect=AssertionError("supervised drain used wall clock")),
            acquire_shared_sync_lock=lambda: lock_handle,
            fresh_operator_job_repository=fresh, _operator_jobs_schema_version=lambda row: row["schema_version"],
            REQUIRED_OPERATOR_JOBS_SCHEMA_VERSION=7, load_operator_job_settings=lambda **kwargs: {},
            run_one_operator_job_chunk=chunk, _collection_callbacks=factory,
            fcntl=types.SimpleNamespace(flock=Mock(), LOCK_UN=8),
            DatabaseUnavailable=DatabaseUnavailable, operator_collection_stop_reason=lambda row: None,
            _public_result=lambda result, **kwargs: dict(result, **kwargs))
        def dispatch(repo, callbacks, options, **bounds):
            result = chunk(repo, callbacks['run_stats'], settings=options,
                run_initial_inventory_chunk=callbacks['run_initial_inventory_chunk'],
                run_initial_stats=callbacks['run_initial_stats'])
            return dict(result, chunks_processed=1, stop_reason='scheduled_handoff')
        ns['_run_dispatch_slice'] = dispatch
        return functions_from_file("scripts/run_operator_job_worker.py", {"main"}, ns)

    def test_operator_refuses_missing_schedule_before_repository_or_provider(self):
        repository, chunk, factory, lock = Mock(), Mock(), Mock(), Mock()
        ns = self.worker_namespace(repository, chunk, factory, lock)
        ns['COLLECTION_SCHEDULES'] = None
        with self.assertRaisesRegex(ScheduleError, 'schedule_snapshot_required'):
            ns['main'](drain=True, supervised=True)
        self.assertEqual(repository.mock_calls, [])
        chunk.assert_not_called()
        factory.assert_not_called()
        self.assertEqual(lock.mock_calls, [])

    def test_drain_acknowledges_current_job_chunk_before_yield(self):
        events = []
        callbacks = {name: Mock() for name in ("run_stats", "run_initial_inventory_chunk", "run_initial_stats")}
        factory = Mock(return_value=callbacks)
        repository = types.SimpleNamespace(database=object(), schema_status=lambda: {
            "ready": True, "schema_version": 7, "initial_quota_limit_enforced": False})
        def chunk(*args, **kwargs):
            self.assertIs(args[1], callbacks["run_stats"])
            self.assertIs(kwargs["run_initial_inventory_chunk"], callbacks["run_initial_inventory_chunk"])
            self.assertIs(kwargs["run_initial_stats"], callbacks["run_initial_stats"])
            events.append("provider")
            shutdown.read_control(io.BytesIO(b"stop\n"))
            self.assertFalse(shutdown.yield_requested())
            events.extend(["video_commit", "job_ack"])
            return {"status": "processed", "processed_video_ids": 1}
        lock_handle = Mock()
        ns = self.worker_namespace(repository, chunk, factory, lock_handle)
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = ns["main"](drain=True, supervised=True, monotonic=lambda: 0)
        self.assertEqual(code, 0)
        self.assertEqual(events, ["provider", "video_commit", "job_ack"])
        factory.assert_called_once()
        self.assertIs(factory.call_args.args[0], repository.database)
        result = __import__("json").loads(output.getvalue())
        self.assertEqual((result["chunks_processed"], result["stop_reason"]), (1, "scheduled_handoff"))
        ns['fcntl'].flock.assert_called_once_with(lock_handle, 8)
        lock_handle.close.assert_called_once_with()

    def test_supervised_worker_delegates_one_finite_slice_to_shared_dispatch(self):
        repository = types.SimpleNamespace(database=object(), schema_status=lambda: {
            'ready': True, 'schema_version': 7, 'initial_quota_limit_enforced': False})
        callbacks, chunk, lock_handle = {}, Mock(), Mock()
        ns = self.worker_namespace(repository, chunk, Mock(return_value=callbacks), lock_handle)
        driver = ns['_run_dispatch_slice'] = Mock(return_value={
            'status': 'processed', 'chunks_processed': 7, 'stop_reason': 'max_chunks'})
        clock = lambda: 0
        with patch('sys.stdout', io.StringIO()):
            self.assertEqual(ns['main'](drain=True, supervised=True, max_chunks=7,
                                       max_seconds=25, monotonic=clock), 0)
        driver.assert_called_once_with(repository, callbacks, {}, max_chunks=7, max_seconds=25, clock=clock)
        chunk.assert_not_called()
        lock_handle.close.assert_called_once()

    def test_direct_database_unavailability_stops_worker_without_replaying_chunk(self):
        for error in (DatabaseUnavailable('database_connection_failed', outcome_unknown=True),
                      DatabaseUnavailable('database_pool_unavailable')):
            with self.subTest(error=error.message):
                repository = types.SimpleNamespace(database=object(), schema_status=lambda: {
                    'ready': True, 'schema_version': 7, 'initial_quota_limit_enforced': False})
                callbacks = {name: Mock() for name in ('run_stats', 'run_initial_inventory_chunk', 'run_initial_stats')}
                chunk, lock_handle = Mock(side_effect=error), Mock()
                ns = self.worker_namespace(repository, chunk, Mock(return_value=callbacks), lock_handle)
                output = io.StringIO()
                with patch('sys.stderr', output):
                    self.assertEqual(ns['main'](drain=True, supervised=True, monotonic=lambda: 0), 1)
                chunk.assert_called_once()
                self.assertTrue(all(not callback.called for callback in callbacks.values()))
                self.assertEqual(__import__('json').loads(output.getvalue()), {
                    'status': 'error', 'reason': 'database_transport_error', 'error_type': 'DatabaseUnavailable',
                })
                ns['fcntl'].flock.assert_called_once_with(lock_handle, 8)
                lock_handle.close.assert_called_once_with()


class HydrationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.claimed = {'id': 'job', 'channel_id': 'channel', 'lease_token': 'lease', 'phase': 'verify'}
        self.database, self.repositories, self.verify_calls = object(), [], []
        self.completed = {**self.claimed, 'status': 'completed'}
        self.provider = Mock(side_effect=AssertionError('verification requested provider work'))
        output = patch('sys.stdout', io.StringIO())
        output.start(); self.addCleanup(output.stop)

    def run_verification(self, outcomes, *, current=None):
        pending = iter(outcomes)
        def verify(job_id, lease):
            self.verify_calls.append((job_id, lease))
            outcome = next(pending)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        def repository():
            value = Mock(spec_set=['database', 'verify_initial_hydration', 'get_initial_inventory_job',
                                   'release', 'prepare_initial_hydration_chunk', 'settle_initial_hydration_chunk'])
            value.database = self.database
            value.verify_initial_hydration.side_effect = verify
            value.get_initial_inventory_job.return_value = current
            self.repositories.append(value)
            return value
        @contextmanager
        def fresh():
            yield repository()
        return operator_jobs._run_initial_full_hydration_chunk(
            repository(), self.provider, self.claimed, settings=object(), fresh_repository_factory=fresh,
        )

    def assert_no_provider_or_hydration_write(self):
        self.provider.assert_not_called()
        for repository in self.repositories:
            self.assertIs(repository.database, self.database)
            repository.prepare_initial_hydration_chunk.assert_not_called()
            repository.settle_initial_hydration_chunk.assert_not_called()

    def test_unknown_ack_replays_only_same_verification_identity(self):
        result = self.run_verification([
            DatabaseUnavailable('database_connection_failed', outcome_unknown=True), self.completed,
        ])
        self.assertEqual((result['status'], result['processed_video_ids'], result['processed_pages']), ('completed', 0, 0))
        self.assertEqual(self.verify_calls, [('job', 'lease')] * 2)
        self.assertEqual(len(self.repositories), 2)
        self.assert_no_provider_or_hydration_write()
        for repository in self.repositories:
            repository.get_initial_inventory_job.assert_not_called()
            repository.release.assert_not_called()

    def test_exhausted_unknown_ack_reconciles_committed_receipt_without_provider(self):
        error = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        result = self.run_verification([error] * 3, current=self.completed)
        self.assertEqual(result['job'], self.completed)
        self.assertEqual(self.verify_calls, [('job', 'lease')] * 3)
        self.assertEqual(len(self.repositories), 4)
        self.repositories[-1].get_initial_inventory_job.assert_called_once_with('channel')
        self.assert_no_provider_or_hydration_write()
        self.assertTrue(all(not repository.release.called for repository in self.repositories))

    def test_pool_pressure_preserves_bounded_existing_receipt_recovery(self):
        error = DatabaseUnavailable('database_pool_unavailable')
        result = self.run_verification([error] * 3, current=self.completed)
        self.assertEqual(result['job'], self.completed)
        self.assertEqual(self.verify_calls, [('job', 'lease')] * 3)
        self.assertEqual(len(self.repositories), 4)
        self.repositories[-1].get_initial_inventory_job.assert_called_once_with('channel')
        self.assert_no_provider_or_hydration_write()
        self.assertTrue(all(not repository.release.called for repository in self.repositories))

    def test_confirmed_sql_error_is_not_retried_as_network_failure(self):
        error = DatabaseServerError('operator_job_lease_invalid', code='P0001')
        with self.assertRaises(DatabaseServerError) as caught:
            self.run_verification([error])
        self.assertIs(caught.exception, error)
        self.assertEqual(self.verify_calls, [('job', 'lease')])
        self.assertEqual(len(self.repositories), 2)
        self.repositories[-1].release.assert_called_once_with(
            'job', 'lease', error_code='initial_hydration_databaseservererror',
        )
        self.assertTrue(all(not repository.get_initial_inventory_job.called for repository in self.repositories))
        self.assert_no_provider_or_hydration_write()

    def test_admission_denial_after_unknown_ack_does_not_release_uncertain_lease(self):
        error = HotDatabaseAdmissionDenied('cleanup_unavailable')
        with self.assertRaises(HotDatabaseAdmissionDenied) as caught:
            self.run_verification([DatabaseUnavailable('database_connection_failed', outcome_unknown=True), error])
        self.assertIs(caught.exception, error)
        self.assertTrue(error.outcome_may_be_committed)
        self.assertEqual(self.verify_calls, [('job', 'lease')] * 2)
        self.assert_no_provider_or_hydration_write()
        self.assertTrue(all(not repository.release.called for repository in self.repositories))


class SettingsTests(unittest.TestCase):
    def test_old_ini_preserves_interval_and_uses_default_grace(self):
        value = settings()
        value["interval_minutes"] = 10
        text = settings_ini(value)
        text = "\n".join(line for line in text.splitlines() if not line.startswith("handoff_grace_seconds"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"config.ini"
            path.write_text(text, encoding="utf-8")
            got = read_settings_file(path)
        self.assertEqual((got["interval_minutes"], got["handoff_grace_seconds"]), (10, 120))

    def test_configurable_grace_and_invalid_values(self):
        for value in (10, 120, 600):
            self.assertEqual(browser_settings({**settings(), "handoff_grace_seconds": value})["handoff_grace_seconds"], value)
        for value in (0, 9, 601, True, "120"):
            with self.assertRaises(SettingsError):
                browser_settings({**settings(), "handoff_grace_seconds": value})

    def test_fresh_configuration_uses_thirty_minutes(self):
        value = read_settings_file(ROOT/"config/config.ini")
        self.assertEqual(value["interval_minutes"], 30)

    def test_operator_keeps_configured_budget(self):
        result = operator_arguments(30, {"LIC_OPERATOR_SLICE_SECONDS": "300", "OPERATOR_JOB_DRAIN_MAX_SECONDS": "100"})
        self.assertEqual(result[-1], "100")


if __name__ == "__main__":
    unittest.main()
