"""One unavailable episode policy shared by all completion paths.

The queue owns this operational clock. It is not an API-data acquisition clock
and never renews metadata, counter, or membership retention.
"""
from collection_policy import clock, unavailable_retry_policy
from datetime import datetime


def observation_clock(value):
    """Normalize native PostgreSQL JSON timestamps as well as driver datetimes."""
    return clock(datetime.fromisoformat(value) if isinstance(value, str) else value)


def missing_schedule(snapshot, *, video_type, first_missing_at, observed_at):
    observed = observation_clock(observed_at)
    first = observation_clock(first_missing_at) if first_missing_at is not None else observed
    policy_type = video_type if video_type in ("long", "short", "stream") else None
    policy = unavailable_retry_policy(snapshot, policy_type, first, observed)
    return {"first_missing_at": first.isoformat(), "schedule_type": policy_type,
            "next_due_at": policy.next_due_at.isoformat() if policy.next_due_at is not None else None,
            "stopped": policy.stopped}
