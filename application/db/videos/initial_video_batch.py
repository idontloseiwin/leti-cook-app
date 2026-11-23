"""One atomic initial-load response across all existing video write owners.

The provider has already returned and normalization is complete. A batch owns
at most 50 distinct videos. Queue locks are acquired in one stable order before
any child writer; metadata, snapshots, receipts and queue settlement either all
commit or all roll back. No transport exception is automatically retried.
"""
from collections.abc import Mapping

from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError
from . import video_failures, video_missing_completion, video_profile_completion
from . import video_stream_completion, video_typed_metadata_completion, video_identity_recovery

MAX_INITIAL_BATCH_ROWS = 50
_WRITERS = {
    "complete_metadata": video_profile_completion.complete_video_profile_metadata_collection,
    "complete_typed_metadata": video_typed_metadata_completion.complete_typed_video_metadata_collection,
    "complete_stream_lifecycle": video_stream_completion.complete_stream_lifecycle_probe,
    "complete_missing": video_missing_completion.complete_typed_video_missing,
    "fail_batch": video_failures.fail_video_collection_batch,
    "complete_identity_refresh": video_identity_recovery.complete_video_identity_refresh,
}
_LOCK = """SELECT video_id FROM public.video_collection_state
 WHERE video_id=ANY(%s::text[]) ORDER BY video_id FOR UPDATE"""


def validate_commands(commands):
    """Copy the small normalized command list before taking a connection."""
    if not isinstance(commands, (list, tuple)) or len(commands) > MAX_INITIAL_BATCH_ROWS:
        raise ValueError("initial_video_batch_invalid")
    result = []
    seen = set()
    for command in commands:
        if not isinstance(command, Mapping) or set(command) != {"operation", "rows"}:
            raise ValueError("initial_video_batch_invalid")
        operation = command["operation"]
        if not isinstance(operation, str) or operation not in _WRITERS:
            raise ValueError("initial_video_batch_invalid")
        if not isinstance(command["rows"], (list, tuple)):
            raise ValueError("initial_video_batch_invalid")
        rows = []
        kinds = set()
        for source in command["rows"]:
            if not isinstance(source, Mapping):
                raise ValueError("initial_video_batch_invalid")
            row = dict(source)
            video_id = row.get("video_id")
            if (not isinstance(video_id, str) or not video_id or video_id != video_id.strip()
                    or video_id in seen or not row.get("lease_token")):
                raise ValueError("initial_video_batch_invalid")
            seen.add(video_id)
            if len(seen) > MAX_INITIAL_BATCH_ROWS:
                raise ValueError("initial_video_batch_invalid")
            kind = row.get("video_type") or ("long" if operation == "complete_metadata" else None)
            if kind not in {"long", "short", "stream"} and not (
                    kind == "unknown" and operation in {"complete_stream_lifecycle", "complete_identity_refresh"}):
                raise ValueError("initial_video_batch_invalid")
            if operation == "complete_metadata" and kind != "long":
                raise ValueError("initial_video_batch_invalid")
            if operation == "complete_typed_metadata" and kind not in {"short", "stream"}:
                raise ValueError("initial_video_batch_invalid")
            if operation != "fail_batch" and (not row.get("collection_attempt_id") or not row.get("channel_id")):
                raise ValueError("initial_video_batch_invalid")
            kinds.add(kind)
            rows.append(row)
        if operation == "complete_typed_metadata" and len(kinds) > 1:
            raise ValueError("initial_video_batch_invalid")
        if rows:
            result.append({"operation": operation, "rows": rows})
    return result


def complete_initial_video_batch(database, commands):
    """Apply an already normalized response once; unknown commit stays unknown."""
    commands = validate_commands(commands)
    rows = [row for command in commands for row in command["rows"]]
    if not rows:
        return 0
    with database.transaction(get_operation("complete_initial_video_batch")) as tx:
        tx.fetch_all(_LOCK, (sorted(row["video_id"] for row in rows),), max_rows=MAX_INITIAL_BATCH_ROWS)
        completed = 0
        for command in commands:
            count = _WRITERS[command["operation"]](None, command["rows"], transaction=tx)
            if type(count) is not int or count != len(command["rows"]):
                raise DatabaseServerError("initial_video_batch_incomplete", code="P0001")
            completed += count
        return completed
