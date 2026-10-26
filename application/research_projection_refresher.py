"""Keeps the research catalogue read model current in the background.

Runs only while collection is admitted, so an update hold or a database
outage simply pauses it; readers then use the protected views directly.
While a batch finds work the next one follows shortly; an empty batch idles.

Initial preparation ends with the first batch that took all the work it found.
Collection keeps queueing videos, so an empty batch may never come while it
writes; what arrives later is read from the protected views until refreshed.
"""
import threading

from db.inventory.search_projection import REFRESH_LIMIT

BUSY_SECONDS = 1
IDLE_SECONDS = 10
RETRY_SECONDS = 60
# Before initial preparation ends a failed batch is retried sooner, and only
# this many failures in a row make the preview report the catalogue unavailable.
PREPARING_RETRY_SECONDS = 5
FAILURES_BEFORE_UNAVAILABLE = 3


class SearchProjectionRefresher:
    def __init__(self, repository, *, ready, limit=REFRESH_LIMIT):
        self.repository, self.ready, self.limit = repository, ready, limit
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None
        self._failures = 0
        self._initial_fill_complete = threading.Event()

    @property
    def initial_fill_complete(self):
        # A performance admission signal, never evidence of row freshness.
        # Once admitted, protected source fallback handles subsequent changes.
        return self._initial_fill_complete.is_set()

    @property
    def preparation_failing(self):
        # One failed batch is a transient state of preparation, not an outage.
        return self._failures >= FAILURES_BEFORE_UNAVAILABLE

    def step(self):
        """One bounded refresh; returns the seconds to wait before the next."""
        try:
            self.ready()
            taken, refreshed = self.repository.refresh_counts(self.limit)
        except Exception as error:
            # A fixed code only: no SQL, row values or connection details.
            self.last_error = type(error).__name__
            self._failures += 1
            return RETRY_SECONDS if self.initial_fill_complete else PREPARING_RETRY_SECONDS
        self.last_error = None
        self._failures = 0
        if taken < self.limit and refreshed < self.limit:
            self._initial_fill_complete.set()
        return BUSY_SECONDS if taken or refreshed else IDLE_SECONDS

    def start(self):
        if self._thread is not None:
            raise RuntimeError('research_projection_refresher_running')
        def run():
            while not self._stop.is_set():
                self._stop.wait(self.step())
        self._thread = threading.Thread(target=run, name='research-projection-refresh', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
            self._thread = None
