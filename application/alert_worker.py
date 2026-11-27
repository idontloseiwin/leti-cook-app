"""Bounded local observation delivery independent of browser sessions."""
from threading import Event, Thread
from time import monotonic

from collection_formats import shown_formats
from db.user_work.alerts import RETRY_SECONDS


MAX_BATCHES_PER_BURST = 8
BURST_SECONDS = 0.25
YIELD_SECONDS = 0.05
IDLE_SECONDS = 5


class AlertWorker:
    def __init__(self, repository, ready):
        self.repository = repository
        self.ready = ready
        self.failure = None
        self._batches = 0
        self._burst_started = 0.0
        self._stop = Event()
        self._thread = Thread(target=self._run, name="owner-alerts", daemon=True)

    def step(self):
        if self._batches == 0:
            self._burst_started = monotonic()
        try:
            self.ready()
            taken = self.repository.process(shown_formats())
        except Exception as error:
            self._batches = 0
            self.failure = type(error).__name__
            return RETRY_SECONDS
        self.failure = None
        if not taken:
            self._batches = 0
            return IDLE_SECONDS
        self._batches += 1
        if self._batches >= MAX_BATCHES_PER_BURST or monotonic()-self._burst_started >= BURST_SECONDS:
            self._batches = 0
            return YIELD_SECONDS
        return 0

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self.step())

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=18)
