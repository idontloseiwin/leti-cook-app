"""Kind-scoped Saved object storage and atomic lifecycle functions."""
from __future__ import annotations

from psycopg import sql

from ..base.repository import DatabaseRepository
from ..base.sql_values import projection
from . import saved_mutations

OBJECT_COLUMNS = "kind,id,name,channel_id,conditions,member_count,revision,created_at,updated_at,deleted_at"
MAX_SAVED_PAGE_ROWS = 101


class SavedObjectsRepository(DatabaseRepository):
    kind: str

    def _rows(self, rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Invalid Saved database response")
        return rows

    def _kind(self):
        if self.kind not in {"view", "collection"}:
            raise ValueError("Unsupported Saved object kind")
        return self.kind

    @staticmethod
    def _page_limit(limit):
        if type(limit) is not int or not 1 <= limit <= MAX_SAVED_PAGE_ROWS:
            raise ValueError("Saved object read exceeds page limit")
        return limit

    def get_object(self, object_id):
        rows = self._rows(self.database.read_rows(
            "saved_object_get", sql.SQL("SELECT {} FROM public.saved_objects WHERE kind=%s AND id=%s::uuid LIMIT 1").format(projection(OBJECT_COLUMNS)),
            (self._kind(), object_id), max_rows=1,
        ))
        return rows[0] if rows else None

    def list_objects(self, limit, after=None):
        limit = self._page_limit(limit)
        parameters = [self._kind()]
        where = sql.SQL("kind=%s AND deleted_at IS NULL")
        if after is not None:
            created_at, object_id = after
            where += sql.SQL(" AND (created_at,id)<(%s::timestamptz,%s::uuid)")
            parameters.extend((created_at, object_id))
        parameters.append(limit)
        return self._rows(self.database.read_rows(
            "saved_object_list", sql.SQL("SELECT {} FROM public.saved_objects WHERE {} ORDER BY created_at DESC,id DESC LIMIT %s").format(projection(OBJECT_COLUMNS), where),
            parameters, max_rows=limit,
        ))

    @staticmethod
    def _mutation_result(result):
        if not isinstance(result, dict):
            raise RuntimeError("Invalid Saved mutation response")
        return result

    def create_object(self, object_id, name, fingerprint, channel_id=None, conditions=None):
        return self._mutation_result(saved_mutations.create_saved_object(
            self.database, kind=self.kind, object_id=object_id, name=name,
            fingerprint=fingerprint, channel_id=channel_id, conditions=conditions))

    def rename_object(self, object_id, name, expected_revision):
        return self._mutation_result(saved_mutations.rename_saved_object(
            self.database, kind=self.kind, object_id=object_id, name=name,
            expected_revision=expected_revision))

    def delete_object(self, object_id, expected_revision):
        return self._mutation_result(saved_mutations.delete_saved_object(
            self.database, kind=self.kind, object_id=object_id, expected_revision=expected_revision))
