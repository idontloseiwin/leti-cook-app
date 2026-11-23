"""Saved view channel availability; object lifecycle uses shared kind-scoped storage."""
from __future__ import annotations

from .saved_objects import SavedObjectsRepository


class SavedViewsRepository(SavedObjectsRepository):
    kind = "view"

    def load_available_channels(self, channel_ids):
        ids = sorted({value for value in channel_ids if value is not None})
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

    def update_research(self,object_id,name,conditions,expected_revision):
        from .saved_mutations import update_research_definition
        return self._mutation_result(update_research_definition(self.database,object_id=object_id,name=name,conditions=conditions,expected_revision=expected_revision))

    def load_available_scopes(self, scopes):
        from psycopg.types.json import Jsonb
        if not scopes: return set()
        if len(scopes)>100: raise ValueError('Saved scope read exceeds page limit')
        statement = """SELECT kind||':'||id AS key FROM jsonb_to_recordset(%s::jsonb) AS s(kind text,id text)
          WHERE CASE kind
           WHEN 'folder' THEN EXISTS(SELECT 1 FROM public.folders WHERE id=s.id::uuid)
           WHEN 'label' THEN EXISTS(SELECT 1 FROM public.channel_labels WHERE label_key=s.id)
           WHEN 'collection' THEN EXISTS(SELECT 1 FROM public.saved_objects WHERE kind='collection' AND id=s.id::uuid AND deleted_at IS NULL)
           ELSE false END"""
        rows=self._rows(self.database.read_rows('saved_query_sources',statement,(Jsonb(scopes),),max_rows=len(scopes)))
        return {row['key'] for row in rows}
