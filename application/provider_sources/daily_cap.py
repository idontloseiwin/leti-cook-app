"""Daily caps of one-off acquisitions per quota bucket (search).

At most 500 general-bucket units per Pacific quota day across all one-off
acquisitions, so collection keeps its quota. search.list spends its own
Search Queries bucket (100 calls per day by default, 1 unit each; collection never
uses it): one-off acquisitions may spend at most 100 of it per Pacific day, so a
repeated search is refused locally with Retry-After instead of being sent into a
provider refusal. The count is the existing quota ledger (phase
``explicit_acquisition``, the cap's bucket) plus this process's own attempts that
the ledger has not received yet (the journal delivers in batches). The cap is
checked before every request; an unreadable or truncated ledger refuses the
acquisition (fail closed). Nothing new is stored: the ledger rows are the ones
every provider request already writes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import uuid
from zoneinfo import ZoneInfo

DAILY_GENERAL_CAP_UNITS = 500
CAPPED_BUCKET = "general"
DAILY_SEARCH_CAP_UNITS = 100
SEARCH_BUCKET = "search_queries"
PHASE = "explicit_acquisition"
PACIFIC = ZoneInfo("America/Los_Angeles")


class CapUnavailable(RuntimeError):
    code = "daily_cap_unavailable"


def quota_day(now):
    return now.astimezone(PACIFIC).date().isoformat()


def next_reset(now):
    local = now.astimezone(PACIFIC)
    midnight = datetime(local.year, local.month, local.day, tzinfo=PACIFIC) + timedelta(days=1)
    return midnight.astimezone(timezone.utc)


class DailyCap:
    def __init__(self, ledger_reader, *, limit=DAILY_GENERAL_CAP_UNITS, clock=lambda: datetime.now(timezone.utc),
                 bucket=CAPPED_BUCKET):
        """``ledger_reader(quota_day)`` returns ([(request_attempt_id, units), ...], truncated) of ``bucket``."""
        self._reader, self.limit, self.clock, self.bucket = ledger_reader, limit, clock, bucket
        self._lock = threading.Lock()
        self._local = {}   # quota day -> {attempt key: units}

    def _remember(self, day, key, units):
        with self._lock:
            self._local = {day: self._local.get(day, {})}   # older days are settled by the ledger
            self._local[day][key] = units

    def reading(self):
        """Units used today and what remains; never raises."""
        now = self.clock()
        day = quota_day(now)
        base = {"quota_day": day, "limit_units": self.limit, "bucket": self.bucket, "phase": PHASE,
                "resets_at": next_reset(now).isoformat().replace("+00:00", "Z")}
        try:
            rows, truncated = self._reader(day)
            ledger = {str(attempt): int(units) for attempt, units in rows}
        except Exception:
            return dict(base, read_status="unavailable", used_units=None, remaining_units=None,
                        local_unconfirmed_units=None)
        with self._lock:
            local = dict(self._local.get(day, {}))
        unconfirmed = sum(units for key, units in local.items() if key not in ledger)
        used = sum(ledger.values()) + unconfirmed
        return dict(base, read_status="truncated" if truncated else "ok", used_units=used,
                    remaining_units=None if truncated else max(0, self.limit - used),
                    local_unconfirmed_units=unconfirmed)

    def session(self):
        reading = self.reading()
        if reading["read_status"] != "ok":
            raise CapUnavailable()
        return CapSession(self, reading)


class CapSession:
    """The remaining allowance of one acquisition, counted before each request."""

    def __init__(self, cap, reading):
        self.cap, self.reading = cap, reading
        self.spent = 0

    @property
    def remaining(self):
        return self.reading["remaining_units"] - self.spent

    def allows(self, bucket, units):
        return bucket != self.cap.bucket or units <= self.remaining

    def record(self, bucket, units, attempt_id):
        if bucket != self.cap.bucket:
            return
        self.spent += units
        # An attempt without a ledger ID still spent quota: count it under a local key.
        self.cap._remember(self.reading["quota_day"], str(attempt_id or "local-" + uuid.uuid4().hex), units)


class CapSessions:
    """The sessions of every capped bucket one acquisition may spend; each counts only its bucket."""

    def __init__(self, sessions):
        self.sessions = tuple(sessions)

    def allows(self, bucket, units):
        return all(session.allows(bucket, units) for session in self.sessions)

    def record(self, bucket, units, attempt_id):
        for session in self.sessions:
            session.record(bucket, units, attempt_id)
