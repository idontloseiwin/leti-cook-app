"""Whether the server clock agrees with Google's clock.

The age of YouTube data is measured with the server's wall clock, so a wrong
clock could keep data past its window or delete it early. This module compares
that clock with the HTTP ``Date`` header of Google's answers:

* every answer of the YouTube Data API (``youtube_client`` passes the header);
* one asynchronous ``HEAD https://www.googleapis.com/`` without any key or
  parameter when the backend starts, then after an hour without a conclusive
  answer in that process. It uses no quota and never delays startup.

States, served under ``time`` at ``/system/hot-runtime``:

``trusted``
    The latest check agrees with Google within 60 seconds and is at most six
    hours old.
``mismatch``
    A check proved a difference of more than 60 seconds, or the clock was set
    by more than that since an agreeing check. It lasts until the two clocks
    agree again; a failed check never clears or causes it.
``unverified``
    No conclusive check in the last six hours, for example without internet
    access. This is a warning, not a mismatch: the server clock stays in use.

Google stamps ``Date`` in whole seconds at some moment while our request is in
flight, so one answer bounds the difference between the clocks to an interval.
Only an interval entirely beyond 60 seconds proves a mismatch; one that crosses
the limit settles nothing. The elapsed clock (``CLOCK_BOOTTIME`` on Linux)
carries a check forward: it ignores clock changes and counts through suspend,
so a later change of the wall clock moves the interval by the same amount.

Only the ``Date`` header string reaches this module, never a URL, request,
response or key. Each process keeps its own state in memory; only backend
startup and its data-maintenance loop schedule probes. Importing this module,
constructing a monitor and reading status send nothing. The runtime controller consumes mismatch
as a reason to pause API reading, collection and cleanup; unverified warns only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import threading
import time

MAX_OFFSET_SECONDS = 60
# A check verifies the clock for this long; then the state becomes unverified.
VERIFIED_FOR_SECONDS = 6 * 3600
# After the initial check, probe when no conclusive answer arrived for this long.
PROBE_IDLE_SECONDS = 3600
# A failed probe is retried after this pause, doubling up to the maximum. A
# mismatch or a clock change is checked again after the first pause.
PROBE_RETRY_SECONDS = 300
PROBE_RETRY_MAX_SECONDS = 3600
PROBE_URL = 'https://www.googleapis.com/'
PROBE_TIMEOUT = (3.05, 5)  # connect, read
# The wall clock moved this much against elapsed time: a change, not drift.
STEP_SECONDS = 2
SOURCES = frozenset({'youtube_api', 'google_probe'})
_DATE_MAX_CHARS = 40
_WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_IMF_FIXDATE = re.compile(
    r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([0-9]{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) '
    r'([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) GMT')


if hasattr(time, 'CLOCK_BOOTTIME'):
    def elapsed_seconds():
        """Linux: unaffected by clock changes and keeps counting through suspend."""
        return time.clock_gettime(time.CLOCK_BOOTTIME)
else:
    elapsed_seconds = time.monotonic


def parse_http_date(value):
    """Seconds since the epoch of an IMF-fixdate (RFC 9110), otherwise None.

    Google sends this form, such as ``Thu, 08 Oct 2026 12:00:00 GMT``. Other
    forms, a wrong weekday and impossible dates are refused, never guessed.
    """
    if type(value) is not str or len(value) > _DATE_MAX_CHARS:
        return None
    match = _IMF_FIXDATE.fullmatch(value)
    if match is None:
        return None
    weekday, day, month, year, hour, minute, second = match.groups()
    try:
        moment = datetime(int(year), _MONTHS.index(month) + 1, int(day), int(hour), int(minute),
                          int(second), tzinfo=timezone.utc)
    except ValueError:
        return None
    if _WEEKDAYS[moment.weekday()] != weekday:
        return None
    return moment.timestamp()


def fetch_google_date(timeout=PROBE_TIMEOUT):
    """Send one keyless HEAD request to Google's API host; return its Date header.

    No key, parameter, cookie or body is sent, so no quota is used. Any HTTP
    answer carries the header. Redirects are not followed.
    """
    import requests
    response = requests.head(PROBE_URL, headers={'Cache-Control': 'no-cache'},
                             timeout=timeout, allow_redirects=False)
    try:
        value = response.headers.get('Date')
    finally:
        response.close()
    return value if type(value) is str else None


def _start_daemon(target):
    threading.Thread(target=target, name='time-trust-probe', daemon=True).start()


def _iso(seconds):
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None  # a wall clock outside the calendar still gets a state


def _verdict(low, high):
    """'trusted' or 'mismatch' when the interval proves it, otherwise None."""
    if -MAX_OFFSET_SECONDS <= low and high <= MAX_OFFSET_SECONDS:
        return 'trusted'
    if high < -MAX_OFFSET_SECONDS or low > MAX_OFFSET_SECONDS:
        return 'mismatch'
    return None


class _Sample:
    """One conclusive answer: Google's clock minus ours lies within [low, high]."""
    __slots__ = ('low', 'high', 'wall', 'elapsed', 'source', 'mismatch')

    def __init__(self, low, high, wall, elapsed, source, mismatch):
        self.low, self.high, self.wall, self.elapsed = low, high, wall, elapsed
        self.source, self.mismatch = source, mismatch


def _sample(started, arrived, date, source):
    if source not in SOURCES or date is None:
        return None
    if (type(started) is not tuple or len(started) != 2
            or not all(type(value) in (int, float) and math.isfinite(value) for value in started + arrived)):
        return None
    (wall_sent, sent), (wall_arrived, arrived_at) = started, arrived
    duration = arrived_at - sent
    if duration < 0 or abs((wall_arrived - wall_sent) - duration) > STEP_SECONDS:
        return None  # Our clock was changed while the request was in flight.
    # Google stamped [date, date + 1) while our clock read [wall_sent, wall_arrived].
    low, high = date - wall_arrived, date + 1 - wall_sent
    verdict = _verdict(low, high)
    if verdict is None:
        return None
    return _Sample(low, high, wall_arrived, arrived_at, source, verdict == 'mismatch')


def _assess(sample, failures, wall, now):
    """The bounded view, the clock change since the check and the check's age."""
    view = {'state': 'unverified', 'reason': 'check_failed' if failures else 'not_checked_yet',
            'offset_seconds': None, 'checked_at': None, 'source': None,
            'max_offset_seconds': MAX_OFFSET_SECONDS}
    if sample is None:
        return view, 0.0, None
    age = max(0.0, now - sample.elapsed)
    shift = (wall - sample.wall) - (now - sample.elapsed)
    low, high = sample.low - shift, sample.high - shift
    verdict = _verdict(low, high)
    if verdict == 'mismatch':
        state, reason = 'mismatch', 'offset_too_large' if sample.mismatch else 'clock_jumped'
    elif verdict == 'trusted' and age <= VERIFIED_FOR_SECONDS:
        state, reason = 'trusted', 'agrees_with_google'
    elif verdict is None:
        state, reason = 'unverified', 'clock_jumped'
    else:
        state, reason = 'unverified', 'check_failed' if failures else 'check_expired'
    view.update(state=state, reason=reason, offset_seconds=round((low + high) / 2, 1),
                checked_at=_iso(wall - age), source=sample.source)
    return view, shift, age


class TimeTrust:
    """Thread-safe owner of one process's clock check. No method raises."""

    def __init__(self, *, clock=time.time, elapsed=elapsed_seconds, fetch_date=fetch_google_date,
                 spawn=_start_daemon, on_change=None):
        self.clock, self.elapsed = clock, elapsed
        self._fetch_date, self._spawn, self._on_change = fetch_date, spawn, on_change
        self._lock = threading.Lock()
        self._sample = None
        self._failures = 0
        self._attempted = None
        self._probing = False
        self._reported = None

    def request_started(self):
        """Wall and elapsed clocks just before a request leaves; pass to record_date."""
        try:
            return (self.clock(), self.elapsed())
        except Exception:
            return None

    def record_date(self, started, value, *, source):
        """Compare one Date header with our clock. True when the answer settled it."""
        try:
            sample = _sample(started, (self.clock(), self.elapsed()), parse_http_date(value), source)
            if sample is None:
                return False
            with self._lock:
                self._sample, self._failures = sample, 0
        except Exception:
            return False
        self.status()
        return True

    def status(self):
        """state, reason, offset_seconds (Google minus server), checked_at, source."""
        try:
            with self._lock:
                sample, failures = self._sample, self._failures
            view = _assess(sample, failures, self.clock(), self.elapsed())[0]
        except Exception:
            view = _assess(None, 1, 0.0, 0.0)[0]  # unreadable clocks: unverified, never a mismatch
        self._report(view['state'], view['reason'])
        return view

    def probe_now(self):
        """One blocking keyless probe, bounded by its timeouts."""
        started = self.request_started()
        try:
            value = self._fetch_date()
        except Exception:
            value = None
        if not self.record_date(started, value, source='google_probe'):
            with self._lock:
                self._failures += 1
            self.status()

    def probe_if_due(self):
        """Start one background probe when due and return at once. True if started."""
        try:
            with self._lock:
                now = self.elapsed()
                if self._probing or not self._probe_due(now):
                    return False
                self._probing, self._attempted = True, now
            try:
                self._spawn(self._probe_in_background)
            except Exception:
                with self._lock:
                    self._probing = False
                    self._failures += 1
                return False
            return True
        except Exception:
            return False

    def _probe_in_background(self):
        try:
            self.probe_now()
        finally:
            with self._lock:
                self._probing = False

    def _probe_due(self, now):
        since = math.inf if self._attempted is None else now - self._attempted
        if self._failures:
            pause = PROBE_RETRY_SECONDS * 2 ** min(self._failures - 1, 8)
            return since >= min(PROBE_RETRY_MAX_SECONDS, pause)
        view, shift, age = _assess(self._sample, 0, self.clock(), now)
        if age is None or view['state'] != 'trusted' or abs(shift) > STEP_SECONDS:
            return since >= PROBE_RETRY_SECONDS
        return age >= PROBE_IDLE_SECONDS and since >= PROBE_RETRY_SECONDS

    def _report(self, state, reason):
        if self._on_change is None:
            return
        with self._lock:
            if self._reported == (state, reason):
                return
            self._reported = (state, reason)
        try:
            self._on_change(state, reason)
        except Exception:
            pass


def _emit_change(state, reason):
    """One bounded telemetry event per change of state or reason."""
    from telemetry import emit_event
    emit_event('runtime', 'time_trust', 'google', 'time_check', phase=reason, direction='internal',
               status=state)


_MONITOR = TimeTrust(on_change=_emit_change)


def monitor():
    """This process's clock check."""
    return _MONITOR


def request_started():
    return _MONITOR.request_started()


def record_date(started, value, *, source):
    return _MONITOR.record_date(started, value, source=source)


def status():
    return _MONITOR.status()
