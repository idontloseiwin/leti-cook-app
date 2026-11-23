"""The bounded read of Saved collection members (every video type).

Members are read through the shared research collection scope
(db/reads/research_sql.py), which owns their admission. SQL owns freshness;
this module checks the result's shape and identities and admits each row's
deadline. The caller still owns freshness validation after transport and
cache admission.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from hot_storage import iso_utc, utc_now, utc_instant, register_read_deadline, HotSourceReadExpired
from ..base.postgres import DatabaseError

SOURCE_COLUMNS = frozenset(("video_id", "channel_id", "source_status", "source_reason", "video_row"))
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_COLLECTION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class AnalyticsReadResultError(DatabaseError):
    """A query result failed the local shape/identity contract."""
    def __init__(self):
        super().__init__("database_result_shape_invalid")


def _invalid(state="P0001"):
    # Local domain validation has the retained RPC's public SQLSTATE. No server
    # message, invalid input, SQL or provider payload is attached to the error.
    raise DatabaseError("database_operation_failed", code=state)


def _instant(value, *, nullable=False, state="P0001"):
    if value is None:
        if nullable:
            return None
        _invalid(state)
    # Repository callers supply aware datetime / ISO-8601 strings.
    # Do not implement PostgreSQL's open-ended timestamp text grammar in Python.
    if not isinstance(value, (datetime, str)):
        raise TypeError("analytics_timestamp_type_invalid")
    try:
        return iso_utc(value)
    except (ValueError, TypeError, OverflowError):
        _invalid(state)


def _rows(data, columns, maximum, *, identities=None):
    if not isinstance(data, list):
        raise AnalyticsReadResultError()
    if len(data) > maximum:
        raise DatabaseError("database_result_limit_exceeded")
    if any(not isinstance(row, dict) or set(row) != columns for row in data):
        raise AnalyticsReadResultError()
    if identities is not None:
        found = [row["video_id"] for row in data]
        if (any(not isinstance(value, str) for value in found)
                or len(found) != len(identities) or len(set(found)) != len(found)
                or set(found) != set(identities)):
            raise AnalyticsReadResultError()
        indexed = {row["video_id"]: row for row in data}
        return [indexed[value] for value in identities]
    return data


def _admit_deadlines(rows):
    """Preserve item witnesses; admit and discard only the aggregate scan clock."""
    now = utc_now()
    for row in rows:
        for value in (row["source_expires_at"], row.pop("response_source_expires_at", None)):
            if value is not None:
                deadline = utc_instant(value)
                if deadline <= now:
                    raise HotSourceReadExpired("Analytical source expired during read")
                register_read_deadline(deadline)
        if row["source_expires_at"] is not None:
            row["source_expires_at"] = iso_utc(row["source_expires_at"])
    return rows


def read_saved_collection_sources(database, *, collection_id, video_ids, as_of) -> list[dict[str, Any]]:
    """Exact requested member IDs of one collection, every video type, read
    through the shared collection scope with one reason per hidden member."""
    from ..base.read_context import http_read_scope
    from .research_sql import COLLECTION_MEMBER_REASONS, compile_collection_members
    as_of = _instant(as_of, state="22023")
    if (not isinstance(collection_id, str) or not _COLLECTION_ID.fullmatch(collection_id)
            or not isinstance(video_ids, (list, tuple)) or not 1 <= len(video_ids) <= 500
            or any(not isinstance(value, str) or not _VIDEO_ID.fullmatch(value) for value in video_ids)
            or len(set(video_ids)) != len(video_ids)):
        _invalid("22023")
    ids = list(video_ids)
    statement, parameters = compile_collection_members(collection_id, ids, as_of)
    # The same restricted reader role as every shared research read, also
    # outside an HTTP request; it never widens the caller's access.
    with http_read_scope():
        data = database.read_rows("analytics_saved_sources", statement, parameters, max_rows=len(ids))
    rows = _admit_deadlines(_rows(data, SOURCE_COLUMNS | {"source_expires_at"}, len(ids), identities=ids))
    for row in rows:
        available = row["source_status"] == "available"
        if (row["source_status"] not in {"available", "unavailable"}
                or available != (row["source_reason"] is None) or available != isinstance(row["video_row"], dict)
                or (not available and (row["video_row"] is not None or row["source_reason"] not in {*COLLECTION_MEMBER_REASONS, "not_saved"}))):
            raise AnalyticsReadResultError()
    return rows
