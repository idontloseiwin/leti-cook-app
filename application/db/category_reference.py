"""The installation's bounded category reference and its source clock."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from hot_storage import HOT_DATA_WINDOW, utc_instant

from .base.repository import DatabaseRepository


_READ = """SELECT document, source_expires_at
FROM readable.youtube_category_reference WHERE singleton"""

_REPLACE = """INSERT INTO public.youtube_category_reference
    (singleton, retrieved_at, document)
SELECT true, %(source)s::timestamptz, %(document)s::jsonb
WHERE %(source)s::timestamptz > statement_timestamp()-interval '696 hours'
  AND %(source)s::timestamptz <= statement_timestamp()
ON CONFLICT (singleton) DO UPDATE
SET retrieved_at=excluded.retrieved_at, document=excluded.document
WHERE NOT isfinite(public.youtube_category_reference.retrieved_at)
   OR public.youtube_category_reference.retrieved_at > statement_timestamp()
   OR public.youtube_category_reference.retrieved_at <= excluded.retrieved_at
RETURNING singleton"""


class CategoryReferenceRepository(DatabaseRepository):
    def load_reference(self) -> dict | None:
        row = self.database.read_row("category_reference_read", _READ)
        if row is None:
            return None
        self.admit_source_deadlines((row,))
        return row["document"]

    def replace_reference(self, value: dict) -> None:
        # Full document validation belongs to the reference application service.
        # This boundary also rejects an invalid clock before borrowing a pool.
        if not isinstance(value, dict) or not isinstance(value.get("retrieved_at"), str):
            raise ValueError("category_reference_invalid")
        source = utc_instant(value["retrieved_at"])
        now = self.read_now()
        if not source <= now < source + HOT_DATA_WINDOW:
            raise ValueError("category_reference_expired_or_invalid")
        rows = self.database.write_rows(
            "category_reference_replace", _REPLACE,
            {"source": value["retrieved_at"], "document": Jsonb(value)}, max_rows=1,
        )
        if not rows:
            if self.read_now() >= source + HOT_DATA_WINDOW:
                raise ValueError("category_reference_expired_or_invalid")
            raise ValueError("category_reference_newer_copy_exists")
