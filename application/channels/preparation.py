"""Pure channel-add policy and row construction shared by source adapters."""
from channel_inventory_estimate import initial_inventory_count_bounds
from operator_jobs import INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT


def initial_channel_status(channel, estimate):
    bounds = initial_inventory_count_bounds(estimate)
    upper = bounds["items_upper_bound"]
    # The typed-list sum and the provider's channel total both guard auto work.
    total = channel["statistics"].get("videoCount")
    channel_total = int(total) if isinstance(total, str) and total.isdecimal() else None
    return (
        "pending" if upper is not None and upper <= INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT
        and (channel_total is None or channel_total <= INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT)
        else "manual_required"
    )


def prepared_channel_row(channel, estimate, status, *, observed_at, metadata_update_values):
    """metadata_update_values is the existing pure metadata projection."""
    return {
        **metadata_update_values(channel, observed_at),
        "channel_id": channel["id"],
        "initial_inventory_estimate": estimate,
        "initial_long_inventory_status": status,
        "initial_28d_stats_status": (
            "pending" if status == "pending"
            else "skipped" if status == "skipped_no_long_playlist" else None
        ),
        "deep_inventory_error": None,
        "added_at": observed_at.isoformat(),
        "is_hidden": False,
        "hidden_at": None,
    }
