"""Process-owned research read admission; waiting never borrows a DB connection."""
from collections import deque
from contextlib import contextmanager
from threading import Condition
import time

from .validation import QueryRejected


class ReadAdmission:
    def __init__(self, *, concurrency=2, max_waiting=16):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("invalid_read_concurrency")
        if type(max_waiting) is not int or max_waiting < 0:
            raise ValueError("invalid_read_queue")
        self.concurrency, self.max_waiting = concurrency, max_waiting
        self._condition, self._waiting, self._active = Condition(), deque(), 0

    @contextmanager
    def enter(self, *, deadline, cancelled):
        ticket = object()
        with self._condition:
            # A stopped caller (the client left, or a graph out of its own time) gets the bare code;
            # this wait's own deadline is query_busy.
            if cancelled():
                raise QueryRejected("query_budget_exceeded")
            if time.monotonic() >= deadline:
                raise QueryRejected("query_busy")
            if self._active < self.concurrency and not self._waiting:
                self._active += 1
            else:
                if len(self._waiting) >= self.max_waiting:
                    raise QueryRejected("query_busy")
                self._waiting.append(ticket)
                try:
                    while True:
                        if cancelled():
                            raise QueryRejected("query_budget_exceeded")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise QueryRejected("query_busy")
                        if self._waiting[0] is ticket and self._active < self.concurrency:
                            self._waiting.popleft()
                            self._active += 1
                            break
                        self._condition.wait(min(.05, remaining))
                finally:
                    if ticket in self._waiting:
                        self._waiting.remove(ticket)
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


class LimitedReadAdmission:
    """A caller group waits outside the shared queue and DB pool.

    One process-owned instance for all agents prevents a second agent from
    taking the owner's remaining shared slot. Both waits use the same deadline.
    """
    def __init__(self, shared, *, concurrency=1, max_waiting=16):
        self.shared = shared
        self.group = ReadAdmission(concurrency=concurrency, max_waiting=max_waiting)

    @contextmanager
    def enter(self, *, deadline, cancelled):
        with self.group.enter(deadline=deadline, cancelled=cancelled):
            with self.shared.enter(deadline=deadline, cancelled=cancelled):
                yield
