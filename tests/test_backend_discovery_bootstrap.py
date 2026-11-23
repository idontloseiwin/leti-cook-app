"""Real launcher/core/ASGI composition without starting I/O or a server.

Collector-only admission must not require a child's interval in the backend.
The native delivery test separately exercises the actual application lifespan.
"""
from pathlib import Path
import os
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def probe(profile):
    from contextlib import ExitStack
    import socket
    import sqlite3
    import tempfile
    from threading import get_ident
    from unittest.mock import Mock, patch

    sys.path.insert(0, str(ROOT / 'application'))
    from collection_schedule import upgrade_schedule_file_for_discovery
    from self_hosted_settings import read_settings_file
    format4 = (ROOT / 'tests/fixtures/schedule_format4_example.ini').read_bytes()
    schedule = (format4 if profile == 'v4' else
                upgrade_schedule_file_for_discovery(format4) if profile == 'migrated' else
                (ROOT / 'config/collection-schedules.ini').read_bytes())
    schedule = schedule.replace(b'age_0_1d_seconds = 3600', b'age_0_1d_seconds = 3601', 1)
    with tempfile.TemporaryDirectory(prefix='lic-discovery-bootstrap-') as temporary:
        directory = Path(temporary)
        settings = directory / 'config.ini'
        owner = (ROOT / 'config/config.ini').read_bytes().replace(
            b'interval_minutes = 10', b'interval_minutes = 30')
        settings.write_bytes(owner)
        settings.chmod(0o644)
        schedule_file = directory / 'collection-schedules.ini'
        schedule_file.write_bytes(schedule)
        schedule_file.chmod(0o644)
        system_environment = {key: value for key, value in os.environ.items()
                              if key.upper() in ('SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE')}
        os.environ.clear()
        os.environ.update(system_environment)
        os.environ.update({
            'HOME': temporary, 'USERPROFILE': temporary,
            'PYTHON_DOTENV_DISABLED': '1', 'LIC_DEPLOYMENT_PROFILE': 'self-hosted',
            'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_DSN': 'postgresql://authenticator:synthetic@127.0.0.1:1/offline',
            'LIC_DATABASE_ID': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
            'LIC_HOT_RUNTIME_SECRET': 'synthetic-bootstrap-not-a-secret-000000',
            'LIC_HOT_RUNTIME_URL': 'http://127.0.0.1:19999/internal/hot-runtime',
            'LIC_PUBLIC_ORIGIN': 'http://127.0.0.1:19999',
            'LIC_AUTH_DATABASE': str(directory / 'owner.sqlite3'),
            'LIC_SETUP_DIRECTORY': str(directory / 'configuration'),
            'LIC_SETTINGS_FILE': str(settings),
            'YOUTUBE_QUOTA_PROJECT_ID': 'offline-bootstrap',
            'YOUTUBE_QUOTA_JOURNAL_PATH': str(directory / 'quota.sqlite3'),
            'TELEMETRY_ENABLED': 'false', 'TELEMETRY_LEGACY_HTTP_ENABLED': 'false',
            'OPERATOR_JOBS_MODE': 'durable',
        })

        def forbidden(*args, **kwargs):
            raise AssertionError('bootstrap_external_effect')

        def compose(config):
            # Uvicorn loads the real server entrypoint, core and route wiring.
            config.load()
            import server
            import core
            import quota_buckets
            from runtime.process_collection import process_collection_scope
            assert server.COLLECTION_CONTEXT.inventory_discovery_admission is None
            assert not quota_buckets.control_active()
            assert 'LIC_COLLECTION_INTERVAL_MINUTES' not in os.environ
            assert core.COLLECTION_SCHEDULES.long_counters.age_0_1d_seconds == 3601
            assert read_settings_file(settings)['interval_minutes'] == 30
            assert settings.read_bytes() == owner
            assert schedule_file.read_bytes() == schedule
            # The retained legacy callback cannot execute in a real backend:
            # its mode resolver maps legacy to durable, and drain only queues.
            from web.jobs import create_channel_jobs
            from channels import service
            jobs = create_channel_jobs(COLLECTION_POLICY=server.COLLECTION_POLICY,
                COLLECTION_SETTINGS=server.COLLECTION_SETTINGS, database=core.database)
            channel = dict(channel_id='UC' + 'a' * 22, collection_control_epoch=7,
                           initial_long_inventory_status='pending')
            for mode in ('durable', 'drain', 'legacy'):
                background = Mock()
                with patch.dict(os.environ, {'OPERATOR_JOBS_MODE': mode}), \
                        patch.object(service, 'require_full_hydration_schema'), \
                        patch.object(service, 'load_initial_hydration_context',
                                     return_value=({'items': 1}, {'status': 'queued'})) as load, \
                        patch.object(service, 'normalized_initial_hydration_estimate', return_value={'items': 1}), \
                        patch.object(service, 'prepare_initial_typed_inventory'), \
                        patch.object(service, 'initial_hydration_payload',
                                     side_effect=lambda channel, **values: {'mode': values['mode']}), \
                        patch.object(service, 'with_initial_format_readiness',
                                     side_effect=lambda channel, payload, **values: payload), \
                        patch('web.jobs.signal_operator_job_worker'):
                    result = jobs._prepare_channel_add_hydration(channel, background, ensure_auto=True)
                assert result == {'mode': 'drain' if mode == 'drain' else 'durable'}
                load.assert_called_once()
                background.add_task.assert_not_called()
            arguments = dict(settings=server.COLLECTION_SETTINGS, policy=server.COLLECTION_POLICY)
            if profile != 'v4':
                for interval in (None, 'broken'):
                    if interval is not None:
                        os.environ['LIC_COLLECTION_INTERVAL_MINUTES'] = interval
                    try:
                        with process_collection_scope(**arguments):
                            raise AssertionError('missing_child_interval_admitted')
                    except (KeyError, ValueError):
                        pass
                    assert not quota_buckets.control_active()
            # The supervisor captures the explicit owner interval for a child.
            os.environ['LIC_COLLECTION_INTERVAL_MINUTES'] = str(read_settings_file(settings)['interval_minutes'])
            with process_collection_scope(**arguments) as context:
                admission = context.inventory_discovery_admission
                if profile == 'v4':
                    assert admission is None and not quota_buckets.control_active()
                else:
                    assert admission.budget.database is core.database
                    assert admission.budget.interval == 30
                    assert admission.budget.project == 'offline-bootstrap'
                    assert admission.budget.settings == core.COLLECTION_SCHEDULES.discovery
                    assert admission.owner == get_ident() and quota_buckets.control_active()
            assert not quota_buckets.control_active()
            return 0

        with ExitStack() as guards:
            guards.enter_context(patch.object(socket.socket, 'connect', forbidden))
            guards.enter_context(patch.object(socket, 'getaddrinfo', forbidden))
            guards.enter_context(patch.object(sqlite3, 'connect', forbidden))
            import psycopg
            import psycopg_pool
            guards.enter_context(patch.object(psycopg, 'connect', forbidden))
            guards.enter_context(patch.object(psycopg.Connection, 'connect', forbidden))
            guards.enter_context(patch.object(psycopg_pool.ConnectionPool, 'open', forbidden))
            from ops import self_hosted_container
            guards.enter_context(patch.object(self_hosted_container, 'run_backend', compose))
            guards.enter_context(patch.object(sys, 'argv', ['self_hosted_container.py']))
            assert self_hosted_container.main() == 0
    print('bootstrap_and_collector_scope_ok')


class BackendDiscoveryBootstrapTests(unittest.TestCase):
    def test_parent_bootstrap_and_strict_child_scope_for_v4_and_both_v5_paths(self):
        for profile in ('v4', 'fresh', 'migrated'):
            with self.subTest(profile=profile):
                result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                         '--probe', profile], cwd=ROOT, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('bootstrap_and_collector_scope_ok', result.stdout)


if __name__ == '__main__':
    if '--probe' in sys.argv:
        import traceback
        try:
            probe(sys.argv[-1])
        except BaseException:
            traceback.print_exc()
            raise SystemExit(1)
    else:
        unittest.main()
