"""Single-process backend launcher and non-browser health check for the installer."""
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


class LifespanCompletion:
    """Observe acknowledged teardown; returning from Uvicorn alone is not proof."""
    def __init__(self, app):
        self.app = app
        self.started = self.stopped = self.failed = self.returned = False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'lifespan':
            return await self.app(scope, receive, send)

        async def observe(message):
            await send(message)
            if message['type'] == 'lifespan.startup.complete':
                self.started = True
            elif message['type'] == 'lifespan.shutdown.complete':
                self.stopped = True
            elif message['type'] in ('lifespan.startup.failed', 'lifespan.shutdown.failed'):
                self.failed = True
        try:
            await self.app(scope, receive, observe)
        except BaseException:
            self.failed = True
            raise
        else:
            self.returned = True


def run_backend(config, *, sockets=None):
    """Single-process clean exit, preserving forced/error exits for the updater.

    Pinned Uvicorn 0.53 replays SIGTERM after graceful teardown. Suppress only
    that replay when the application actually acknowledged and completed its
    lifespan; the host must still require exit status zero before copying data.
    """
    from contextlib import contextmanager
    import uvicorn
    if config.lifespan != 'on':
        raise ValueError('self_hosted_lifespan_required')
    if not config.loaded:
        config.load()
    witness = LifespanCompletion(config.loaded_app)
    config.loaded_app = witness

    class BackendServer(uvicorn.Server):
        shutdown_returned = False

        def clean_shutdown(self):
            lifecycle = getattr(self, 'lifespan', None)
            return (self.started and self.shutdown_returned and not self.force_exit
                    and witness.started and witness.stopped and witness.returned and not witness.failed
                    and lifecycle is not None and not lifecycle.startup_failed
                    and not lifecycle.shutdown_failed and not lifecycle.error_occurred
                    and not self.server_state.tasks and not self.server_state.connections)

        async def shutdown(self, sockets=None):
            await super().shutdown(sockets=sockets)
            self.shutdown_returned = True

        @contextmanager
        def capture_signals(self):
            with super().capture_signals():
                yield
                if self.clean_shutdown():
                    # Keep Uvicorn's handlers and escalation; remove only the
                    # final replay after successful shutdown, not a live signal.
                    self._captured_signals.clear()

    server = BackendServer(config)
    server.run(sockets=sockets)
    return 0 if server.clean_shutdown() else 1


def main():
    if os.getenv('LIC_DEPLOYMENT_PROFILE')!='self-hosted':raise RuntimeError('self_hosted_profile_required')
    if sys.argv[1:]==['health']:
        try:
            from hot_runtime import RemoteAdmission, load_hot_admission_config
            RemoteAdmission(os.environ['LIC_HOT_RUNTIME_URL'], admission=load_hot_admission_config()).require_database_ready()
            from db.self_hosted_auth import OwnerAuthRepository
            OwnerAuthRepository(os.environ['LIC_AUTH_DATABASE']).configured()
            from self_hosted_configuration import ConfigurationStore
            ConfigurationStore(os.environ['LIC_SETUP_DIRECTORY']).read()
            return 0
        except Exception:return 1
    if sys.argv[1:]==['check-settings']:
        # manage.py restart asks first: a refused value would stop the backend from starting.
        from self_hosted_settings import SETTINGS_ENV,SettingsError,read_settings_file,settings_problem
        try:read_settings_file(os.environ[SETTINGS_ENV])
        except SettingsError as error:
            print(settings_problem(str(error)))
            return 1
        except OSError:
            print('The file could not be read.')
            return 1
        from collection_schedule import ScheduleError,read_schedule_file,schedule_problem
        try:read_schedule_file(Path(os.environ[SETTINGS_ENV]).with_name('collection-schedules.ini'))
        except ScheduleError as error:
            print('config/collection-schedules.ini: ' + schedule_problem(str(error)))
            return 1
        except OSError:
            print('config/collection-schedules.ini: The file could not be read.')
            return 1
        return 0
    if sys.argv[1:]:raise RuntimeError('unknown_container_command')
    os.umask(0o077)
    from runtime.collection_schedules import backend_schedule_environment
    os.environ.update(backend_schedule_environment(os.environ))
    import uvicorn
    # No proxy-derived client addresses. The installer publishes loopback only.
    config=uvicorn.Config('server:app',host='0.0.0.0',port=8002,workers=1,access_log=False,
                proxy_headers=False,limit_concurrency=64,timeout_keep_alive=5,lifespan='on')
    return run_backend(config)

if __name__=='__main__':raise SystemExit(main())
