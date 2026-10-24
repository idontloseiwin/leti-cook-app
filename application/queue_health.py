"""Validated all-format queue observations, never collection authorization.

Counts describe persisted queue candidates after current channel/pause filters.
Legacy runtime and playlist flags are diagnostic: the shared video owner can
still collect or recover identity while those switches are off. Claim owners
verify identity, source freshness and leases. Missing observations raise instead
of turning an unavailable queue into an empty one.
"""
from collections.abc import Mapping
from datetime import datetime


COMMON_COUNTS = (
    "total", "due", "failed", "excluded_failed", "paused", "policy_paused",
    "runtime_disabled", "leased", "expired_leases",
)
VIDEO_COUNTS = COMMON_COUNTS + (
    "hydration_due", "metadata_due", "counter_due", "pending", "hydrated",
    "awaiting_completion", "unavailable", "identity_pending", "playlist_disabled",
)
DISCOVERY_COUNTS = COMMON_COUNTS + (
    "ready", "active_resume_states", "playlist_not_found", "initial_owned",
)


def validate_queue_health(value, *, unit):
    """Reject partial envelopes rather than fabricating successful zero counts."""
    if unit not in {"videos", "playlists"}:
        raise ValueError("queue_health_unit_invalid")
    counts = VIDEO_COUNTS if unit == "videos" else DISCOVERY_COUNTS
    types = ("long", "short", "stream", "unknown") if unit == "videos" else ("long", "short", "stream")
    if (not isinstance(value, Mapping) or value.get("schema_version") != 2
            or value.get("scope") != "all_types" or value.get("unit") != unit
            or value.get("channel_eligibility_checked") is not True
            or value.get("operational_eligibility_checked") is not False
            or not isinstance(value.get("by_type"), Mapping)
            or set(value["by_type"]) != set(types)):
        raise ValueError("queue_health_observation_invalid")

    def bucket(source):
        if not isinstance(source, Mapping):
            raise ValueError("queue_health_observation_invalid")
        result = {}
        for key in counts:
            number = source.get(key)
            if type(number) is not int or number < 0:
                raise ValueError("queue_health_observation_invalid")
            result[key] = number
        stamp = source.get("oldest_overdue_at")
        if stamp is not None:
            if not isinstance(stamp, str):
                raise ValueError("queue_health_observation_invalid")
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("queue_health_observation_invalid") from None
            if parsed.tzinfo is None:
                raise ValueError("queue_health_observation_invalid")
        if "oldest_overdue_at" not in source or (result["due"] == 0 and stamp is not None):
            raise ValueError("queue_health_observation_invalid")
        if any(result[key] > result["total"] for key in counts if key != "total"):
            raise ValueError("queue_health_observation_invalid")
        if result["failed"] + result["excluded_failed"] > result["total"]:
            raise ValueError("queue_health_observation_invalid")
        result["oldest_overdue_at"] = stamp
        return result

    result = bucket(value)
    by_type = {kind: bucket(value["by_type"][kind]) for kind in types}
    if any(result[key] != sum(row[key] for row in by_type.values()) for key in counts):
        raise ValueError("queue_health_observation_invalid")
    if unit == "playlists":
        channels = value.get("total_channels")
        if type(channels) is not int or not 0 <= channels <= result["total"]:
            raise ValueError("queue_health_observation_invalid")
        result["total_channels"] = channels
    return dict(result, schema_version=2, scope="all_types", unit=unit,
                channel_eligibility_checked=True, operational_eligibility_checked=False,
                by_type=by_type)


def validate_metadata_queue_health(value):
    """Channel profile work is shared by types, not three copies of a channel."""
    if (not isinstance(value, Mapping) or value.get("schema_version") != 1
            or value.get("scope") != "channel_profiles" or value.get("unit") != "channels"):
        raise ValueError("queue_health_observation_invalid")
    result = {}
    for key in ("total", "eligible_channels", "policy_paused_channels", "due", "failed",
                "excluded_failed", "running", "null_schedule"):
        number = value.get(key)
        if type(number) is not int or number < 0:
            raise ValueError("queue_health_observation_invalid")
        result[key] = number
    if (result["total"] != result["eligible_channels"] + result["policy_paused_channels"]
            or any(result[key] > result["eligible_channels"] for key in ("due", "failed", "running", "null_schedule"))
            or result["excluded_failed"] > result["policy_paused_channels"]):
        raise ValueError("queue_health_observation_invalid")
    counts = value.get("status_counts")
    if (not isinstance(counts, Mapping) or not set(counts) <= {"ok", "error", "running", "pending", "unknown"}
            or any(type(n) is not int or n < 0 for n in counts.values())
            or sum(counts.values()) != result["eligible_channels"]
            or counts.get("error", 0) != result["failed"]):
        raise ValueError("queue_health_observation_invalid")
    for key in ("oldest_overdue_at", "oldest_running_at"):
        stamp = value.get(key)
        if key not in value:
            raise ValueError("queue_health_observation_invalid")
        if stamp is not None:
            try:
                if not isinstance(stamp, str) or datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise ValueError("queue_health_observation_invalid") from None
        result[key] = stamp
    return dict(result, schema_version=1, scope="channel_profiles", unit="channels", status_counts=dict(counts))
