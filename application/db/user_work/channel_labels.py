"""Persistence boundary for manual channel labels."""
from __future__ import annotations

from typing import Any
from psycopg import sql

from ..base.repository import DatabaseRepository
from ..base.sql_values import insert_values

# The durable user-work limits include 1,000 custom labels plus the preset.
MAX_LABEL_ROWS = 1001
MAX_ASSIGNMENT_ROWS = 100000


class ChannelLabelRepository(DatabaseRepository):
    def load_labels(self) -> list[dict[str, Any]]:
        return list(self.database.read_rows(
            "channel_labels_list", "SELECT label_key,name,label_type,created_at FROM public.channel_labels ORDER BY label_type DESC,name,label_key LIMIT %s",
            (MAX_LABEL_ROWS,), max_rows=MAX_LABEL_ROWS,
        ) or [])

    def load_assignments(self) -> list[dict[str, Any]]:
        return list(self.database.read_rows(
            "channel_labels_assignments", "SELECT channel_id,label_key FROM public.channel_label_assignments ORDER BY channel_id,label_key LIMIT %s",
            (MAX_ASSIGNMENT_ROWS,), max_rows=MAX_ASSIGNMENT_ROWS,
        ) or [])

    def create_label(self, row: dict[str, Any]) -> dict[str, Any]:
        columns, values, parameters = insert_values([row])
        result = self.database.write_rows("channel_label_create",
            sql.SQL("INSERT INTO public.channel_labels ({}) VALUES {} RETURNING label_key,name,label_type,created_at").format(columns, values),
            parameters, max_rows=1, error_codes=frozenset({"user_work_limit_reached"}),
        ) or []
        return dict(result[0]) if result else dict(row)

    def assign_label(self, channel_id: str, label_key: str) -> None:
        self.database.write("channel_label_assign",
            "INSERT INTO public.channel_label_assignments(channel_id,label_key) VALUES (%s,%s) "
            "ON CONFLICT (channel_id,label_key) DO UPDATE SET channel_id=EXCLUDED.channel_id,label_key=EXCLUDED.label_key",
            (channel_id, label_key), error_codes=frozenset({"user_work_limit_reached"}))

    def unassign_label(self, channel_id: str, label_key: str) -> None:
        self.database.write("channel_label_unassign", "DELETE FROM public.channel_label_assignments WHERE channel_id=%s AND label_key=%s", (channel_id, label_key))

    def rename_label(self, label_key: str, name: str) -> None:
        self.database.write("channel_label_rename", "UPDATE public.channel_labels SET name=%s WHERE label_key=%s", (name, label_key))

    def delete_label(self, label_key: str) -> None:
        # Its assignments go with it (ON DELETE CASCADE), the preset included.
        self.database.write("channel_label_delete", "DELETE FROM public.channel_labels WHERE label_key=%s", (label_key,))
