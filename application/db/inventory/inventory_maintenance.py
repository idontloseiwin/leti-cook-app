"""Database operations shared by inventory maintenance and repair commands."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from psycopg import sql

from ..base.repository import DatabaseRepository
from ..channel.channel_discovery_projection import CHANNEL_DISCOVERY_RELATION
from ..base.sql_values import assignments, identifier, insert_values, projection


class InventoryMaintenanceRepository(DatabaseRepository):


    def load_long_inventory_by_ids(
        self, video_ids: Sequence[str], *, channel_id: str | None = None,
    ) -> list[dict[str, Any]]:
        video_ids = list(dict.fromkeys(video_ids))
        result = []
        for start in range(0, len(video_ids), 1000):
            batch = list(video_ids[start:start + 1000])
            query = ("SELECT video_id, channel_id, published_at FROM public.video_inventory "
                     "WHERE video_type = 'long' AND video_id = ANY(%s::text[]) ")
            params = (batch,)
            if channel_id:
                query += "AND channel_id = %s "
                params += (channel_id,)
            result.extend(self.database.read_rows(
                "maintenance_long_inventory_ids", query + "LIMIT %s",
                (*params, len(batch)), max_rows=len(batch),
            ))
        return result

    def load_long_inventory_page(
        self, *, channel_id: str | None, offset: int, page_size: int,
    ) -> list[dict[str, Any]]:
        query = ("SELECT video_id, channel_id, published_at FROM public.video_inventory "
                 "WHERE video_type = 'long' ")
        params = ()
        if channel_id:
            query += "AND channel_id = %s "
            params = (channel_id,)
        return self.database.read_rows(
            "maintenance_long_inventory_page", query + "ORDER BY video_id ASC LIMIT %s OFFSET %s",
            (*params, page_size, offset), max_rows=page_size,
        )

    def load_videos_by_ids(self, video_ids: Sequence[str], *, columns: str) -> list[dict[str, Any]]:
        video_ids = list(dict.fromkeys(video_ids))
        selected = projection(columns)
        result = []
        for start in range(0, len(video_ids), 1000):
            batch = list(video_ids[start:start + 1000])
            result.extend(self.database.read_rows(
                "maintenance_video_ids",
                sql.SQL('SELECT {} FROM public.videos WHERE video_id = ANY(%s::text[]) LIMIT %s').format(selected),
                (batch, len(batch)), max_rows=len(batch),
            ))
        return result

    def load_channels(self, columns: str) -> list[dict[str, Any]]:
        return self.database.read_rows(
            "maintenance_channels",
            sql.SQL('SELECT {} FROM {}').format(projection(columns), sql.SQL(CHANNEL_DISCOVERY_RELATION)),
            max_rows=10000,
        )

    def load_channels_page(
        self, columns: str, *, offset: int, page_size: int, order_column: str = "title",
    ) -> list[dict[str, Any]]:
        return self.database.read_rows(
            "maintenance_channels_page",
            sql.SQL('SELECT {} FROM {} '
                    'ORDER BY {} ASC NULLS LAST, channel_id ASC LIMIT %s OFFSET %s')
            .format(projection(columns), sql.SQL(CHANNEL_DISCOVERY_RELATION), identifier(order_column)),
            (page_size, offset), max_rows=page_size,
        )


    def fetch_all_rows(
        self, table_name: str, select_columns: str, order_column: str, *, page_size: int,
    ) -> list[dict[str, Any]]:
        if table_name not in {"channels", "videos", "video_inventory"}:
            raise ValueError(f"unsupported maintenance table: {table_name}")
        relation = (sql.SQL(CHANNEL_DISCOVERY_RELATION) if table_name == "channels"
                    else sql.Identifier("public", table_name))
        key = "channel_id" if table_name == "channels" else "video_id"
        query = sql.SQL('SELECT {} FROM {} ORDER BY {} ASC NULLS LAST, {} ASC LIMIT %s OFFSET %s').format(
            projection(select_columns), relation, identifier(order_column), identifier(key),
        )
        rows, page = [], 0
        while True:
            batch = self.database.read_rows(
                "maintenance_all_rows", query, (page_size, page * page_size), max_rows=page_size,
            )
            rows.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        return rows

    def select_recent_public_video_rows(self, candidate_limit: int) -> list[dict[str, Any]]:
        if candidate_limit <= 0:
            return []
        return self.database.read_rows(
            "maintenance_recent_video_rows",
            "SELECT video_id, availability_status, checked_at FROM public.videos "
            "ORDER BY checked_at DESC NULLS FIRST, video_id ASC LIMIT %s",
            (candidate_limit,), max_rows=candidate_limit,
        )
