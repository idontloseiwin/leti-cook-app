"""Keeps the research catalogue read model current in the background.

Runs only while collection is admitted, so an update hold or a database
outage simply pauses it; readers then use the protected views directly.
While a batch finds work the next one follows shortly; an empty batch idles.
"""
import threading

from db.inventory.search_projection import REFRESH_LIMIT

BUSY_SECONDS = 1
IDLE_SECONDS = 10
RETRY_SECONDS = 60


class SearchProjectionRefresher:
    def __init__(self, repository, *, ready, limit=REFRESH_LIMIT):
        self.repository, self.ready, self.limit = repository, ready, limit
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def step(self):
        """One bounded refresh; returns the seconds to wait before the next."""
        try:
            self.ready()
            refreshed = self.repository.refresh(self.limit)
        except Exception as error:
            # A fixed code only: no SQL, row values or connection details.
            self.last_error = type(error).__name__
            return RETRY_SECONDS
        self.last_error = None
        return BUSY_SECONDS if refreshed else IDLE_SECONDS

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
