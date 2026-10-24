"""Collection membership and bounded current source projections."""
from __future__ import annotations

from hot_storage import iso_utc, utc_instant

from ..reads.analytics_read_queries import read_saved_collection_sources
from .saved_objects import SavedObjectsRepository
from .saved_mutations import change_saved_collection_members

SOURCE_BATCH_SIZE = 500


class SavedCollectionsRepository(SavedObjectsRepository):
    kind = "collection"

    def list_members(self, collection_id, limit, after=None):
        limit = self._page_limit(limit)
        parameters = [collection_id]
        statement = "SELECT video_id,added_at FROM public.saved_collection_members WHERE collection_id=%s::uuid"
        if after is not None:
            at, video_id = after
            statement += " AND (added_at,video_id)<(%s::timestamptz,%s)"
            parameters.extend((at, video_id))
        statement += " ORDER BY added_at DESC,video_id DESC LIMIT %s"
        parameters.append(limit)
        return self._rows(self.database.read_rows("saved_collection_members", statement, parameters, max_rows=limit))

    def load_sources(self, video_ids, *, as_of):
        # Caller as-of can narrow the upper endpoint but cannot move DB/server time.
        as_of = iso_utc(min(utc_instant(as_of), self.read_now()))
        result = []
        for start in range(0, len(video_ids), SOURCE_BATCH_SIZE):
            batch = list(video_ids[start:start + SOURCE_BATCH_SIZE])
            rows = self._rows(read_saved_collection_sources(self.database, video_ids=batch, as_of=as_of))
            seen = set()
            for row in rows:
                video_id = row.get("video_id")
                if not isinstance(video_id, str) or video_id not in batch or video_id in seen:
                    raise RuntimeError("Invalid Collection source identity coverage")
                seen.add(video_id)
            if seen != set(batch) or len(rows) != len(batch):
                raise RuntimeError("Incomplete Collection source response")
            indexed = {row["video_id"]: row for row in rows}
            result.extend(indexed[video_id] for video_id in batch)
        return result

    def change_members(self, collection_id, request_id, action, video_ids, expected_revision, fingerprint):
        return self._mutation_result(change_saved_collection_members(
            self.database, collection_id=collection_id, request_id=request_id, action=action,
            video_ids=video_ids, expected_revision=expected_revision, fingerprint=fingerprint))
