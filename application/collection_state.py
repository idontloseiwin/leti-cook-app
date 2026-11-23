"""The current collection state shown by Health and Home.

Pure rules over stored sync-run rows, the backend start time and the clock, so
Health and Home cannot disagree. A partial sync without errors is normal work:
it carries the rest over. Only a failure is red; a YouTube quota or rate-limit
stop waits in yellow. Time while the server was off is not counted: ages run
from the later of the event and this backend's start, the one process value
kept here.
"""
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from collection_outcome import BUCKET_STOP_FLAGS, COLLECTION_STOP_SCOPES, FAILURE_STOP_FLAGS


FULL_PASS_WARNING = timedelta(hours=24)
MIN_COLLECTION_SILENCE = timedelta(minutes=90)
GAP_NOTE = timedelta(hours=1)

QUOTA_STOP_FLAGS = ("quota_exhausted", "general_quota_exhausted", "batch_quota_exhausted", "daily_quota_exhausted")
RATE_STOP_FLAGS = ("rate_limited",)
# Every other recorded stop is a failure, including a stop flag added later
# (collection_outcome.FAILURE_STOP_FLAGS).
RUN_KINDS = ("running", "complete", "carried_over", "quota", "rate_limited", "failed")
# The quota stop's notice: the bucket that ran out and the work that goes on.
# Both buckets, or quota without a named bucket, keep the general notice.
QUOTA_WAIT_SIGNALS = {
    None: ("collection_waiting_for_quota",
           "Collection is waiting for YouTube quota; it resumes after the daily reset at midnight Pacific."),
    "general": ("collection_waiting_for_general_quota",
                "General quota is exhausted; counters keep updating through Batch statistics. "
                "It resumes at midnight Pacific."),
    "stats": ("collection_waiting_for_stats_quota",
              "Batch statistics quota is exhausted; video details, channel profiles and playlist checks "
              "continue through General. It resumes at midnight Pacific."),
}

_server_started_at = None


def note_server_started(now=None):
    """Record this backend's start; warnings do not count time before it."""
    global _server_started_at
    _server_started_at = moment(now) or datetime.now(timezone.utc)
    return _server_started_at


def server_started_at():
    return _server_started_at


def moment(value):
    """An aware UTC datetime from a datetime or ISO text; None otherwise."""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            value = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _summary(row):
    summary = row.get("summary_json") if isinstance(row, Mapping) else None
    return summary if isinstance(summary, Mapping) else {}


def run_kind(row):
    """How one stored run ended. A stop is yellow only when its recorded flags
    are YouTube quota or rate limit and every recorded error is a refusal they
    explain: the summary counts those as ``limit_errors``. Any other
    error, such as a failed channel-profile read, makes the run a failure.

    A run recorded before that count cannot show which errors the stop
    explains; it waits in yellow only when it recorded no error at all.
    """
    row = row if isinstance(row, Mapping) else {}
    status = row.get("status")
    if status == "running":
        return "running"
    try:
        errors = int(row.get("errors") or 0)
    except (TypeError, ValueError):
        return "failed"
    if errors == 0 and status == "completed":
        return "complete"
    if errors == 0 and status == "partial":
        return "carried_over"
    summary = _summary(row)

    def flagged(names):
        return any(summary.get(name) is True for name in names)

    if status != "error" or flagged(FAILURE_STOP_FLAGS):
        return "failed"
    if not (flagged(QUOTA_STOP_FLAGS) or flagged(RATE_STOP_FLAGS)):
        return "failed"
    explained = summary.get("limit_errors")
    if type(explained) is not int or explained < 0:
        explained = 0
    if errors != explained:
        return "failed"
    return "quota" if flagged(QUOTA_STOP_FLAGS) else "rate_limited"


def quota_wait_bucket(row):
    """The one quota bucket a stored run reports exhausted: "general" or
    "stats"; None for both buckets or for quota without a named bucket."""
    summary = _summary(row)
    named = [COLLECTION_STOP_SCOPES[name] for name in BUCKET_STOP_FLAGS if summary.get(name) is True]
    return named[0] if len(named) == 1 else None


def run_moment(row):
    """When a stored run ended, or started if no end was recorded."""
    row = row if isinstance(row, Mapping) else {}
    return moment(row.get("finished_at")) or moment(row.get("started_at"))


def _span(delta):
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _signal(code, level, message):
    return {"code": code, "level": level, "message": message}


def collection_signals(*, latest_kind, latest_finished_at, last_full_at, now, server_started_at=None,
                       interval_minutes=None, automatic=None, full_pass_known=True, min_silence_minutes=None,
                       quota_wait_bucket=None):
    """Collection health signals in display order; a partial run without errors adds none.

    A failed run is critical. A quota or rate-limit stop shows a warning; a quota
    stop of one bucket (``quota_wait_bucket``) names it and the work that
    goes on. Warn when no run has finished for max(3 intervals, 90 min) of running time
    while automatic collection is on. Also warn after 24 h of running
    time; skipped when the last full pass could not be read.
    """
    silence = MIN_COLLECTION_SILENCE
    if type(min_silence_minutes) is int and min_silence_minutes > 0:
        silence = timedelta(minutes=min_silence_minutes)
    now = moment(now) or datetime.now(timezone.utc)
    started = moment(server_started_at)

    def running_since(value):
        values = [item for item in (moment(value), started) if item]
        return max(values) if values else None

    signals = []
    if latest_kind == "failed":
        signals.append(_signal("latest_run_failed", "critical", "latest sync run failed"))
    elif latest_kind == "quota":
        code, message = QUOTA_WAIT_SIGNALS.get(quota_wait_bucket, QUOTA_WAIT_SIGNALS[None])
        signals.append(_signal(code, "warning", message))
    elif latest_kind == "rate_limited":
        signals.append(_signal("collection_rate_limited", "warning",
            "YouTube asked to slow down; collection resumes on the next sync."))
    if latest_kind in RUN_KINDS and latest_kind != "running" and automatic is not False:
        limit = silence
        if type(interval_minutes) is int and interval_minutes > 0:
            limit = max(limit, timedelta(minutes=3 * interval_minutes))
        since = running_since(latest_finished_at)
        if since and now - since >= limit:
            signals.append(_signal("collection_not_finishing", "warning",
                f"No sync has finished for {_span(now - since)} while the server was running. "
                "Check that automatic collection is running."))
    if full_pass_known:
        since = running_since(last_full_at)
        if since and now - since >= FULL_PASS_WARNING:
            signals.append(_signal("no_full_pass", "warning",
                "No full pass in 24 h of running time; work is accumulating. "
                "Check quota use and the number of channels."))
    return signals


def collection_gap(server_started_at, last_run_before_start):
    """How long collection did not run before this start, when over an hour.

    A run cut off by the stop ends after the start once it is recovered; its
    start is then the last time collection is known to have run.
    """
    started = moment(server_started_at)
    if not started or not isinstance(last_run_before_start, Mapping):
        return None
    ran = moment(last_run_before_start.get("finished_at"))
    if not ran or ran > started:
        ran = moment(last_run_before_start.get("started_at"))
    if not ran or ran > started or started - ran < GAP_NOTE:
        return None
    return {
        "server_started_at": started.isoformat(),
        "collection_last_ran_at": ran.isoformat(),
        "hours": round((started - ran).total_seconds() / 3600, 1),
    }
