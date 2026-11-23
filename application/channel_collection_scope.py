"""Captured channel authorization for writes following external work.

An epoch comes from a canonical database projection or the original job claim.
Never reload it while acknowledging an already started provider operation.
"""
from collections.abc import Mapping
from typing import Any


def require_control_epoch(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 9223372036854775807:
        raise ValueError("channel_collection_scope_invalid")
    return value


def channel_scope(channel: Mapping[str, Any]) -> dict[str, Any]:
    identity = channel.get("channel_id")
    if not isinstance(identity, str) or not identity or identity.strip() != identity or len(identity) > 128:
        raise ValueError("channel_collection_scope_invalid")
    return {"channel_id": identity,
            "control_epoch": require_control_epoch(channel.get("collection_control_epoch"))}


def validate_channel_scopes(scopes, *, channel_ids) -> list[dict[str, Any]]:
    """Copy one exact caller-captured scope for every requested channel."""
    if not isinstance(scopes, (list, tuple)) or len(scopes) > 1000:
        raise ValueError("channel_collection_scope_invalid")
    requested = set(channel_ids)
    if any(not isinstance(value, str) or not value or value.strip() != value
           or len(value) > 128 for value in requested):
        raise ValueError("channel_collection_scope_invalid")
    captured = {}
    for scope in scopes:
        if not isinstance(scope, Mapping) or set(scope) != {"channel_id", "control_epoch"}:
            raise ValueError("channel_collection_scope_invalid")
        identity = scope["channel_id"]
        if not isinstance(identity, str) or identity not in requested or identity in captured:
            raise ValueError("channel_collection_scope_invalid")
        captured[identity] = {"channel_id": identity,
                              "control_epoch": require_control_epoch(scope["control_epoch"])}
    if set(captured) != requested:
        raise ValueError("channel_collection_scope_invalid")
    return list(captured.values())
