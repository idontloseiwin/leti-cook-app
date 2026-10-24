"""Persistence for user-defined folders; restore alone changes parent links."""
from __future__ import annotations

from typing import Any, Mapping
from psycopg import sql

from ..base.repository import DatabaseRepository
from ..base.sql_values import insert_values


class ChannelOrganizationRepository(DatabaseRepository):
    def create_folder(self, values: Mapping[str, Any]) -> None:
        columns, rows, parameters = insert_values([dict(values)])
        self.database.write("channel_folder_create", sql.SQL("INSERT INTO public.folders ({}) VALUES {}").format(columns, rows), parameters, error_codes=frozenset({"user_work_limit_reached"}))

    def assign_folder(self, channel_id: str, folder_id: str | None) -> None:
        self.database.write("channel_folder_assign", "UPDATE public.channels SET folder_id=%s::uuid WHERE channel_id=%s", (folder_id, channel_id))

    def rename_folder(self, folder_id: str, name: str) -> None:
        self.database.write("channel_folder_rename", "UPDATE public.folders SET name=%s WHERE id=%s::uuid", (name, folder_id))

    def delete_folder(self, folder_id: str) -> None:
        # channels.folder_id is ON DELETE SET NULL.
        self.database.write("channel_folder_delete", "DELETE FROM public.folders WHERE id=%s::uuid", (folder_id,))
