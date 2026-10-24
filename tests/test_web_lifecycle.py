"""Offline resource-lifecycle checks against the real FastAPI lifespan machinery."""
from contextlib import ExitStack, contextmanager
from email.utils import formatdate
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web.lifecycle import create_lifespan


class WebLifespanTests(unittest.TestCase):
    @contextmanager
    def application(self, *, self_hosted=True, setup=False, failure=None, blocked=False,
                    clock_monitor=None, on_initialize=None):
        events = []

        def record(name):
            events.append(name)
            if failure == name:
                raise RuntimeError(name)

        database = SimpleNamespace(
            open=lambda: record("database.open"),
            close=lambda: record("database.close"), cadence_snapshot="cadence",
        )
        recorder = SimpleNamespace(
            start=lambda: record("recorder.start"), close=lambda: record("recorder.close"),
        )
        core = SimpleNamespace(database=database, DATABASE_CONFIG=object(),
                               HOT_ADMISSION_CONFIG=object(), COLLECTION_SCHEDULES=object())
        provider_snapshot = {'LIC_PROVIDER_ADMISSION_SNAPSHOT': 'immutable-provider-snapshot',
                             'LIC_YOUTUBE_REQUEST_CONCURRENCY': '8'}
        provider = SimpleNamespace(environment=lambda: dict(provider_snapshot))
        def start_provider(environ, *, concurrency):
            assert concurrency == 8
            assert 'owner.enter' in events
            record('provider.start')
            return provider
        def stop_provider(value):
            if value is not None:
                assert value is provider
                record('provider.close')
        maintenance = SimpleNamespace(close=lambda: record("maintenance.close"))
        hold_guard = SimpleNamespace(check=lambda: record("update_hold.check"))
        copy_guard = SimpleNamespace(check=lambda *, force=False: record("update_copy.check"))
        owner_lock = SimpleNamespace()

        class OwnerLock:
            def __init__(self, *, admission):
                self.assert_admission = admission
                assert admission is core.HOT_ADMISSION_CONFIG
            def __enter__(self):
                record("owner.enter")
                return owner_lock
            def __exit__(self, *args):
                record("owner.exit")

        class Runtime:
            def __init__(self, repository, *, local_cleanup, clear_caches, on_ready, update_copy_guard, update_hold_guard):
                assert repository is maintenance
                assert update_hold_guard is hold_guard
                assert update_copy_guard is copy_guard
                self.update_copy_guard = update_copy_guard
                self.local_cleanup = local_cleanup
                self.clear_caches = clear_caches
                self.on_ready = on_ready
                self.time_trust = clock_monitor if clock_monitor is not None else SimpleNamespace(
                    probe_if_due=lambda: record("clock.probe"))
                record("runtime.construct")
            def initialize(self):
                self.initialized_thread = threading.get_ident()
                record("runtime.initialize")
                if on_initialize is not None:
                    on_initialize()
                self.blocked = blocked
            def require_collection_ready(self):
                if self.blocked:
                    from hot_runtime import HotRuntimeUnavailable
                    raise HotRuntimeUnavailable()
                return None
            def start(self):
                self.started_thread = threading.get_ident()
                record("runtime.start")
            def close(self):
                record("runtime.close")

        class Manager:
            def __init__(self, store):
                assert store == "configuration-store"
                record("onboarding.construct")
            def close(self):
                record("onboarding.close")

        def create_maintenance(config, *, cadence_snapshot):
            assert config is core.DATABASE_CONFIG
            assert cadence_snapshot == "cadence"
            record("maintenance.construct")
            return maintenance

        def create_supervisor(manager, *, ready, schedules, update_hold_guard, provider_snapshot):
            assert provider_snapshot == provider.environment()
            assert update_hold_guard is hold_guard
            assert ready.__name__ == "require_collection_ready"
            assert schedules is core.COLLECTION_SCHEDULES
            record("supervisor.construct")
            return SimpleNamespace(start=lambda: record("supervisor.start"))

        reference_store = SimpleNamespace(
            purge_expired=lambda: record("reference.purge"),
        )

        def create_reference(repository):
            assert repository.database is core.database
            return reference_store

        cache = SimpleNamespace(
            clear_read_cache=lambda: record("cache.read.clear"),
            catalogue=SimpleNamespace(invalidate_all=lambda: record("cache.catalogue.clear")),
        )
        with ExitStack() as stack:
            stack.enter_context(patch("update_hold.UpdateHoldGuard.from_environment", return_value=hold_guard))
            stack.enter_context(patch("update_copy_guard.UpdateCopyGuard.from_environment", return_value=copy_guard))
            stack.enter_context(patch("web.lifecycle.selected_deployment_profile", return_value="self-hosted" if self_hosted else "operator"))
            stack.enter_context(patch.dict("os.environ", {"LIC_SETUP_DIRECTORY": "offline-setup" if setup else "", "LIC_YOUTUBE_REQUEST_CONCURRENCY": "8"}))
            stack.enter_context(patch("provider_runtime.start_backend", side_effect=start_provider))
            stack.enter_context(patch("provider_runtime.stop_runtime", side_effect=stop_provider))
            stack.enter_context(patch("hot_runtime.RuntimeOwnerLock", OwnerLock))
            stack.enter_context(patch("hot_runtime.HotRuntimeController", Runtime))
            stack.enter_context(patch("db.hot.hot_runtime.maintenance_repository", side_effect=create_maintenance))
            stack.enter_context(patch("hot_runtime.install_local_runtime", side_effect=lambda runtime: record("runtime.install")))
            stack.enter_context(patch("hot_runtime.remove_local_runtime", side_effect=lambda runtime: record("runtime.remove")))
            stack.enter_context(patch("youtube_category_reference.CategoryReferenceStore", side_effect=create_reference))
            stack.enter_context(patch("youtube_category_reference.install_managed_reference_store",
                                      side_effect=lambda store: self.assertIs(store, reference_store)))
            stack.enter_context(patch("youtube_category_reference.remove_managed_reference_store",
                                      side_effect=lambda store: record("reference.close")))
            stack.enter_context(patch("self_hosted_configuration.ConfigurationStore", return_value="configuration-store"))
            stack.enter_context(patch("self_hosted_onboarding.SetupManager", Manager))
            stack.enter_context(patch("self_hosted_onboarding.install_manager", side_effect=lambda manager: record("onboarding.install" if manager else "onboarding.remove")))
            stack.enter_context(patch("self_hosted_collection.CollectionSupervisor", side_effect=create_supervisor))
            app = FastAPI(lifespan=create_lifespan(core=core, cache=cache, YOUTUBE_QUOTA_ATTEMPT_RECORDER=recorder))
            yield app, events

    def test_self_hosted_initializes_database_gate_without_waiting_for_full_cleanup(self):
        with self.application(setup=True) as (app, events):
            with TestClient(app):
                self.assertEqual(events, ["update_hold.check", "update_copy.check", "database.open", "owner.enter", "provider.start", "reference.purge", "maintenance.construct",
                    "runtime.construct", "runtime.install", "clock.probe", "runtime.initialize", "runtime.start", "onboarding.construct",
                    "supervisor.construct", "onboarding.install", "supervisor.start"])
                runtime = app.state.hot_runtime
                self.assertNotEqual(runtime.initialized_thread, runtime.started_thread)
                runtime.local_cleanup()
                runtime.clear_caches()
                runtime.on_ready()
                self.assertEqual(events[-4:], ["reference.purge", "cache.read.clear", "cache.catalogue.clear", "recorder.start"])
            self.assertEqual(events[-10:], ["onboarding.close", "onboarding.remove", "provider.close", "recorder.close",
                "runtime.close", "runtime.remove", "maintenance.close", "reference.close", "database.close", "owner.exit"])


    def test_startup_clock_head_does_not_wait_for_the_network_or_a_database_step(self):
        from time_trust import TimeTrust
        entered, release, trusted = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def head(url, **options):
            calls.append((url, options))
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release the clock response')
            return SimpleNamespace(headers={'Date': formatdate(time.time(), usegmt=True)}, close=lambda: None)
        monitor = TimeTrust(on_change=lambda state, reason: trusted.set() if state == 'trusted' else None)
        def before_database_check():
            self.assertTrue(entered.wait(2), 'the startup probe must already be scheduled')
            self.assertFalse(release.is_set(), 'startup must not wait for the HEAD response')
        with patch('requests.sessions.Session.request', side_effect=AssertionError('network is forbidden')), \
                patch('requests.head', side_effect=head):
            try:
                with self.application(clock_monitor=monitor, on_initialize=before_database_check) as (app, events):
                    @app.get('/clock-state')
                    def state():
                        return app.state.hot_runtime.time_trust.status()
                    with TestClient(app) as client:
                        self.assertEqual(client.get('/clock-state').json()['reason'], 'not_checked_yet')
                        self.assertFalse(monitor.probe_if_due())
                        self.assertEqual(len(calls), 1)
                        release.set()
                        self.assertTrue(trusted.wait(2))
                        self.assertEqual(client.get('/clock-state').json()['state'], 'trusted')
                        self.assertFalse(monitor.probe_if_due())
            finally:
                release.set()
        self.assertEqual(calls, [('https://www.googleapis.com/', {
            'headers': {'Cache-Control': 'no-cache'}, 'timeout': (3.05, 5), 'allow_redirects': False})])

    def test_known_database_outage_keeps_application_and_runtime_retry_owner_alive(self):
        with self.application(blocked=True) as (app, events):
            @app.get('/shell')
            def shell(): return {'available': True}
            with TestClient(app) as client:
                self.assertEqual(client.get('/shell').json(), {'available': True})
                self.assertTrue(app.state.hot_runtime.blocked)
                self.assertIn('runtime.start', events)
                self.assertNotIn('recorder.start', events)
            self.assertIn('runtime.close', events)

    def test_without_setup_no_collection_supervisor_is_created(self):
        with self.application() as (app, events):
            with TestClient(app):
                self.assertEqual(events[-1], "runtime.start")
                self.assertNotIn("recorder.start", events)
            self.assertFalse(any(event.startswith("onboarding.") or event.startswith("supervisor.") for event in events))
            self.assertEqual(events[-8:], ["provider.close", "recorder.close", "runtime.close", "runtime.remove", "maintenance.close", "reference.close", "database.close", "owner.exit"])

    def test_partial_startup_failure_releases_database_and_owner(self):
        with self.application(failure="maintenance.construct") as (app, events):
            with self.assertRaisesRegex(RuntimeError, "maintenance.construct"):
                with TestClient(app):
                    self.fail("Startup failure must prevent entering the application")
            self.assertEqual(events, ["update_hold.check", "update_copy.check", "database.open", "owner.enter", "provider.start", "reference.purge", "maintenance.construct", "provider.close", "recorder.close", "reference.close", "database.close", "owner.exit"])

    def test_failed_initialization_never_starts_consumers_and_releases_resources(self):
        with self.application(setup=True, failure="runtime.initialize") as (app, events):
            with self.assertRaisesRegex(RuntimeError, "runtime.initialize"):
                with TestClient(app):
                    self.fail("Failed initialization cannot expose the application")
            self.assertNotIn("runtime.start", events)
            self.assertNotIn("onboarding.construct", events)
            self.assertEqual(events[-8:], ["provider.close", "recorder.close", "runtime.close", "runtime.remove",
                "maintenance.close", "reference.close", "database.close", "owner.exit"])

    def test_failed_onboarding_close_does_not_skip_remaining_cleanup(self):
        with self.application(setup=True, failure="onboarding.close") as (app, events):
            with self.assertRaisesRegex(RuntimeError, "onboarding.close"):
                with TestClient(app):
                    pass
            self.assertEqual(events[-10:], ["onboarding.close", "onboarding.remove", "provider.close", "recorder.close", "runtime.close", "runtime.remove", "maintenance.close", "reference.close", "database.close", "owner.exit"])

    def test_failed_runtime_close_still_removes_runtime_and_releases_owner(self):
        with self.application(failure="runtime.close") as (app, events):
            with self.assertRaisesRegex(RuntimeError, "runtime.close"):
                with TestClient(app):
                    pass
            self.assertEqual(events[-8:], ["provider.close", "recorder.close", "runtime.close", "runtime.remove", "maintenance.close", "reference.close", "database.close", "owner.exit"])

    def test_owner_lock_failure_never_initializes_provider_or_consumers(self):
        with self.application(setup=True, failure='owner.enter') as (app, events):
            with self.assertRaisesRegex(RuntimeError, 'owner.enter'), TestClient(app):
                self.fail('owner refusal must prevent startup')
            self.assertNotIn('provider.start', events)
            self.assertNotIn('supervisor.construct', events)
            self.assertIn('database.close', events)

    def test_provider_start_failure_unwinds_owner_without_consumers(self):
        with self.application(setup=True, failure='provider.start') as (app, events):
            with self.assertRaisesRegex(RuntimeError, 'provider.start'), TestClient(app):
                self.fail('provider refusal must prevent startup')
            self.assertEqual(events, ['update_hold.check', 'update_copy.check', 'database.open',
                'owner.enter', 'provider.start', 'recorder.close', 'database.close', 'owner.exit'])

    def test_provider_close_failure_does_not_skip_other_owned_resources(self):
        with self.application(setup=True, failure='provider.close') as (app, events):
            with self.assertRaisesRegex(RuntimeError, 'provider.close'), TestClient(app):
                pass
            self.assertEqual(events[-10:], ['onboarding.close', 'onboarding.remove', 'provider.close',
                'recorder.close', 'runtime.close', 'runtime.remove', 'maintenance.close',
                'reference.close', 'database.close', 'owner.exit'])

    def test_other_profile_only_owns_database_and_quota_recorder(self):
        with self.application(self_hosted=False) as (app, events):
            with TestClient(app):
                self.assertEqual(events, ["update_hold.check", "update_copy.check", "database.open", "recorder.start"])
            self.assertEqual(events, ["update_hold.check", "update_copy.check", "database.open", "recorder.start", "recorder.close", "database.close"])


if __name__ == "__main__":
    unittest.main()
