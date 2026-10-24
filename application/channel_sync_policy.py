"""Canonical channel synchronization eligibility policy.

The policy is intentionally pure: callers can use it before any database or
YouTube work.  Native facts project control and linked-job values; the finite rules below own mode selection.
Legacy progress fields never grant permission in this Python read boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from channel_collection_scope import require_control_epoch
from typing import Any, Mapping


CHANNEL_SYNC_ACTIVE = "active"
CHANNEL_SYNC_INITIAL_INVENTORY = "initial_inventory"
CHANNEL_SYNC_METADATA_ONLY = "metadata_only"
CHANNEL_SYNC_OPERATOR_HOLD = "operator_hold"
CHANNEL_SYNC_HIDDEN = "hidden"

INITIAL_INVENTORY_COMPLETED = "completed"
INITIAL_INVENTORY_MANUAL_REQUIRED = "manual_required"
INITIAL_INVENTORY_ERROR = "error"
INITIAL_INVENTORY_PENDING = "pending"
INITIAL_INVENTORY_RUNNING = "running"
INITIAL_INVENTORY_NO_LONG_PLAYLIST = "skipped_no_long_playlist"


@dataclass(frozen=True)
class ChannelSyncEligibility:
    mode: str
    reason: str
    allow_channel_metadata: bool
    allow_initial_inventory: bool
    allow_discovery: bool
    allow_video_collection: bool
    allow_current_stats_job: bool
    allow_typed_inventory: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "allow_channel_metadata": self.allow_channel_metadata,
            "allow_initial_inventory": self.allow_initial_inventory,
            "allow_discovery": self.allow_discovery,
            "allow_video_collection": self.allow_video_collection,
            "allow_current_stats_job": self.allow_current_stats_job,
            "allow_typed_inventory": self.allow_typed_inventory,
        }


def channel_sync_eligibility(channel: Mapping[str, Any]) -> ChannelSyncEligibility:
    """Return the accepted fail-closed synchronization policy for a channel."""

    mode = str(channel.get("collection_mode") or "")
    if bool(channel.get("is_hidden")) or mode == CHANNEL_SYNC_HIDDEN:
        return ChannelSyncEligibility(
            mode=CHANNEL_SYNC_HIDDEN,
            reason="channel_hidden",
            allow_channel_metadata=False,
            allow_initial_inventory=False,
            allow_discovery=False,
            allow_video_collection=False,
            allow_current_stats_job=False,
            allow_typed_inventory=False,
        )

    permission = channel.get("collection_permission")
    try:
        require_control_epoch(channel.get("collection_control_epoch"))
    except ValueError:
        permission = None
    if permission != "accepted":
        mode = CHANNEL_SYNC_OPERATOR_HOLD
    if mode == CHANNEL_SYNC_ACTIVE:
        return ChannelSyncEligibility(
            mode=CHANNEL_SYNC_ACTIVE,
            reason="inventory_completed",
            allow_channel_metadata=True,
            allow_initial_inventory=False,
            allow_discovery=True,
            allow_video_collection=True,
            allow_current_stats_job=True,
            allow_typed_inventory=True,
        )
    if mode == CHANNEL_SYNC_METADATA_ONLY:
        return ChannelSyncEligibility(
            mode=CHANNEL_SYNC_METADATA_ONLY,
            reason="no_long_playlist",
            allow_channel_metadata=True,
            allow_initial_inventory=False,
            allow_discovery=False,
            allow_video_collection=False,
            allow_current_stats_job=False,
            allow_typed_inventory=True,
        )
    if mode == CHANNEL_SYNC_INITIAL_INVENTORY:
        return ChannelSyncEligibility(
            mode=CHANNEL_SYNC_INITIAL_INVENTORY,
            reason="inventory_not_ready",
            allow_channel_metadata=False,
            allow_initial_inventory=True,
            allow_discovery=False,
            allow_video_collection=False,
            allow_current_stats_job=False,
            allow_typed_inventory=False,
        )
    reason = ("inventory_manual_required" if permission == "manual_required"
              else "collection_control_unavailable")
    return ChannelSyncEligibility(
        mode=CHANNEL_SYNC_OPERATOR_HOLD,
        reason=reason,
        allow_channel_metadata=False,
        allow_initial_inventory=False,
        allow_discovery=False,
        allow_video_collection=False,
        allow_current_stats_job=False,
        allow_typed_inventory=False,
    )


def channel_allows_automatic_youtube(channel: Mapping[str, Any]) -> bool:
    eligibility = channel_sync_eligibility(channel)
    return eligibility.mode in {CHANNEL_SYNC_ACTIVE, CHANNEL_SYNC_METADATA_ONLY}


# Shared finite policy for native fact readers and fixed SQL CASE emission.
@dataclass(frozen=True)
class _ChannelPolicyRule:
    terms: tuple[tuple[str, bool], ...]
    result: str | bool | None

# Native fields retain NULL. Only a fact literally True/False satisfies a term.
# More than one row may intentionally have the same result: this flattens OR.
CHANNEL_MODE_RULES = (
    _ChannelPolicyRule((("control_present", False),), "operator_hold"),
    _ChannelPolicyRule((("hidden", True),), "hidden"),
    _ChannelPolicyRule((("permission_not_accepted", True),), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("job_missing", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("job_channel_distinct", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("job_type_distinct", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("epoch_null", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("errors_null", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("status_null", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("phase_null", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("types_long", False), ("types_all", False)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("scope_full", True), ("status_blocked", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("scope_full", True), ("status_error", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("scope_full", True), ("status_suspended", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("scope_full", True), ("status_completed", True), ("frozen_null", True)), "operator_hold"),
    _ChannelPolicyRule((("linked", True), ("types_long", True), ("status_completed", True), ("initial_skipped", True)), "active"),
    _ChannelPolicyRule((("linked", True), ("status_completed", True), ("errors_zero", True), ("type_count_three", True), ("phase_verify", True)), "active"),
    _ChannelPolicyRule((("linked", True), ("status_completed", True), ("errors_zero", True), ("types_long", True), ("phase_verify", True)), "active"),
    _ChannelPolicyRule((("linked", True), ("status_completed", True), ("errors_zero", True), ("types_long", True), ("phase_initial_stats", True)), "active"),
    _ChannelPolicyRule((("linked", True),), "initial_inventory"),
    _ChannelPolicyRule((("initial_completed", True),), "active"),
    _ChannelPolicyRule((("initial_skipped", True),), "active"),
)
CHANNEL_MODE_DEFAULT = "initial_inventory"
# Runtime typed permission intentionally differs from public UI eligibility.
CHANNEL_RUNTIME_TYPED_RULES = tuple(_ChannelPolicyRule((("control_present", True), ("hidden", False),
    ("permission_accepted", True), (name, True)), True)
    for name in ("mode_active", "mode_metadata", "mode_initial"))
CHANNEL_PAUSE_RULES = (
    _ChannelPolicyRule((("pause_hidden", True),), "channel_hidden"),
    _ChannelPolicyRule((("mode_active", True),), None),
    _ChannelPolicyRule((("mode_metadata", True),), "channel_no_long_playlist"),
    _ChannelPolicyRule((("mode_initial", True),), "channel_inventory_not_ready"),
    _ChannelPolicyRule((("permission_manual", True),), "channel_inventory_manual_required"),
)

def _choose_channel_policy(rules, default, facts):
    # SQL WHEN AND of (fact IS TRUE/FALSE), including unknown, exactly.
    return next((rule.result for rule in rules
        if all(facts[name] is required for name, required in rule.terms)), default)

def channel_policy_from_native_facts(facts):
    if any(value is not None and type(value) is not bool for value in facts.values()):
        raise ValueError("channel_policy_facts_invalid")
    mode = _choose_channel_policy(CHANNEL_MODE_RULES, CHANNEL_MODE_DEFAULT, facts)
    facts = dict(facts, mode_active=mode == "active", mode_metadata=mode == "metadata_only",
                 mode_initial=mode == "initial_inventory")
    return {"mode": mode, "typed_runtime_eligible": _choose_channel_policy(CHANNEL_RUNTIME_TYPED_RULES, False, facts),
            "pause_reason": _choose_channel_policy(CHANNEL_PAUSE_RULES, "channel_inventory_not_ready", facts)}
