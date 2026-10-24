"""Bounded, protected reads of publication stages, independent of video type."""

from ..base.client import execute_recoverable_read
from ..base.repository import DatabaseRepository


PUBLICATION_PAGE_SIZE = 50

_PAGE = """WITH waiting AS MATERIALIZED (
 SELECT v.video_id,v.channel_id,i.video_type AS inventory_type,v.title,v.publication_status,
 v.scheduled_start_at,v.metadata_checked_at AS checked_at,
 v.source_expires_at AS video_expires_at,i.source_expires_at AS inventory_expires_at
 FROM readable.videos v
 JOIN readable.video_inventory i ON i.video_id=v.video_id AND i.channel_id=v.channel_id
 WHERE v.channel_id=%s AND v.publication_status IN ('upcoming','live','awaiting_completion')
 AND v.availability_status IN ('public','unlisted')
 AND (%s::text IS NULL OR v.video_id>%s)
 ORDER BY v.video_id LIMIT %s)
 SELECT waiting.video_id,
 CASE WHEN proof.resolution_status='resolved' AND proof.canonical_video_type=waiting.inventory_type
 THEN waiting.inventory_type END AS video_type,
 waiting.title,waiting.publication_status,waiting.scheduled_start_at,waiting.checked_at,
 least(waiting.video_expires_at,
 CASE WHEN proof.resolution_status='resolved' AND proof.canonical_video_type=waiting.inventory_type
 THEN least(waiting.inventory_expires_at,proof.source_expires_at) END) AS source_expires_at
 FROM waiting LEFT JOIN LATERAL (
 SELECT resolution.canonical_video_type,resolution.resolution_status,resolution.source_expires_at
 FROM readable.video_inventory_type_resolution resolution
 WHERE resolution.video_id=waiting.video_id AND resolution.canonical_channel_id=waiting.channel_id
 OFFSET 0) proof ON true
 ORDER BY waiting.video_id"""


class ChannelPublicationsRepository(DatabaseRepository):
    def load_page(self, channel_id, *, cursor=None):
        rows = execute_recoverable_read(
            self.database,
            lambda database: database.read_rows(
                "channel_publications_page", _PAGE,
                (channel_id, cursor, cursor, PUBLICATION_PAGE_SIZE + 1),
                max_rows=PUBLICATION_PAGE_SIZE + 1,
            ),
            operation="channel_publications.page",
        )
        return self.admit_source_deadlines([dict(row) for row in rows])
