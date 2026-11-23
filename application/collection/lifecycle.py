"""Persist channel lifecycle fields through the caller-owned database scope."""

from channel_collection_scope import require_control_epoch
from db import ChannelSyncRepository
from source_log import source_log_message


def update_channel_values(channel_id, values, *, repository, expected_epoch):
    expected_epoch = require_control_epoch(expected_epoch)
    if repository is None:
        raise TypeError("repository is required")
    complete = repository.update_process_state(channel_id, values, expected_epoch=expected_epoch)
    if not complete:
        print(
            source_log_message('lifecycle_columns_missing', lambda: ("Optional channel process columns unavailable; "
            f"updated {channel_id} without them")),
            flush=True,
        )


def update_channel_lifecycle_status(channel_id, values, *, database, expected_epoch):
    expected_epoch = require_control_epoch(expected_epoch)
    if database is None:
        raise TypeError("database is required")
    try:
        repository = ChannelSyncRepository(database)
        update_channel_values(channel_id, values, repository=repository, expected_epoch=expected_epoch)
        return True
    except Exception as error:
        print(
            source_log_message('lifecycle_update_error', lambda: (f"Failed to update channel lifecycle status for {channel_id}: {error}"), error=error),
            flush=True,
        )
        raise
