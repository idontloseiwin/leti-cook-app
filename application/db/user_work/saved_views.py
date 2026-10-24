"""Saved view channel availability; object lifecycle uses shared kind-scoped storage."""
from __future__ import annotations

from .saved_objects import SavedObjectsRepository


class SavedViewsRepository(SavedObjectsRepository):
    kind = "view"

    def load_available_channels(self, channel_ids):
        ids = sorted(set(channel_ids))
        if not ids:
            return set()
        if len(ids) > 100:
            raise ValueError("Saved view channel read exceeds page limit")
        rows = self._rows(self.database.read_rows(
            "saved_view_channels", "SELECT channel_id,is_hidden FROM readable.channel_user_work WHERE channel_id=ANY(%s::text[]) LIMIT %s",
            (ids, len(ids)), max_rows=len(ids),
        ))
        if any(row.get("channel_id") not in ids for row in rows):
            raise RuntimeError("Invalid Saved view channel response")
        return {row["channel_id"] for row in rows if row.get("is_hidden") is not True}
