"""Background WebSub work in the backend: subscription management and event processing.

Runs only while collection is admitted, so an update hold or a database outage
pauses it. Neither part makes a YouTube Data API request: events only make
existing discovery or refresh due earlier, and Hub requests use no API quota.
Events are processed here only until the discovery runtime is enabled; from
then on the collection child owns durable notifications and this step is idle.

Hub requests are careful, as in the September experiment, only faster: one at
a time, the answer awaited up to the timeout, each request recorded before it
is sent, Retry-After and a Hub-wide pause honoured, unanswered requests not
resent at once, a first-run ramp (1, then 10, then all) and a stop after
repeated failures. Shutdown does not wait for a slow Hub answer: the request
was recorded before sending and its outcome is settled by later verification.
"""
import threading
import time

import websub

KEY_FILE = 'websub/websub.key'


class WebSubKey:
    """The installation key, created on first use in the application state volume."""

    def __init__(self, path):
        self.path = path
        self._value = None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._value is None:
                self._value = websub.load_or_create_key(self.path)
            return self._value


def configured_websub(environ):
    """(callback base, WebSubKey) when [collection] websub_callback_url is set, else None.

    The state volume is the parent of LIC_SETUP_DIRECTORY. An invalid URL raises
    SettingsError, so the backend refuses to start with the setting named.
    """
    from pathlib import Path
    from self_hosted_settings import configured_settings_path, read_websub_callback_url
    directory = environ.get('LIC_SETUP_DIRECTORY')
    if not directory:
        return None
    path = configured_settings_path(environ)
    if not Path(path).exists():
        return None  # setup has not written config.ini yet
    base = read_websub_callback_url(path)
    return (base, WebSubKey(Path(directory).parent / KEY_FILE)) if base else None


PLAN_SECONDS = 600
CLEAN_SECONDS = 3600
PACE_SECONDS = 3.0
IDLE_SECONDS = 5.0
RETRY_SECONDS = 30.0
MAX_WAIT_SECONDS = 60.0


class WebSubService:
    def __init__(self, manager, events, *, base, key, ready, request=websub.hub_request,
                 hub=websub.HUB_URL, pace=PACE_SECONDS, monotonic=time.monotonic):
        self.manager, self.events, self.base, self.key = manager, events, base, key
        self.ready, self.request, self.hub, self.pace, self.monotonic = ready, request, hub, pace, monotonic
        self._stop = threading.Event()
        self._thread = None
        self._next_plan = self._next_clean = 0.0
        self.last_error = None
        self.stopped_reason = None

    def step(self):
        """One bounded unit of work; returns the seconds to wait before the next."""
        try:
            self.ready()
            processed = self.events.process()
            now = self.monotonic()
            if now >= self._next_plan:
                self.manager.plan(self.base)
                self._next_plan = now + PLAN_SECONDS
            if now >= self._next_clean:
                self.manager.clean()
                self._next_clean = now + CLEAN_SECONDS
            row, admission = self.manager.reserve()
            if row is None:
                self.stopped_reason = 'breaker_open' if admission.reason == 'breaker_open' else None
                wait = IDLE_SECONDS if admission.reason in ('idle', 'breaker_open') else admission.wait
                return 0.2 if processed else min(MAX_WAIT_SECONDS, max(0.5, wait))
            key = self.key()
            callback = websub.callback_url(row['callback_base'], key, row['channel_id'], row['generation'])
            secret = websub.signing_secret(key, row['channel_id'], row['generation'])
            # The reservation is already committed: an abrupt stop leaves a
            # recorded attempt that is re-asked only after the no-verification wait.
            result = self.request(row['mode'], row['channel_id'], callback, secret=secret, hub=self.hub)
            self.manager.settle(row, result)
        except Exception as error:
            # A fixed code only: no SQL, URLs, capabilities or provider text.
            self.last_error = type(error).__name__
            return RETRY_SECONDS
        self.last_error = None
        return self.pace

    def start(self):
        if self._thread is not None:
            raise RuntimeError('websub_service_running')

        def run():
            while not self._stop.is_set():
                self._stop.wait(self.step())

        self._thread = threading.Thread(target=run, name='websub', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            # Shutdown is not held for a slow Hub answer: the request in flight
            # was recorded before sending and is re-asked only if never verified.
            self._thread.join(timeout=5)
            self._thread = None
