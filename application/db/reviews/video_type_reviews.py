"""Bounded private manual video-type review persistence; no transport retries."""
from __future__ import annotations

from ..base.repository import DatabaseRepository
from . import video_type_review_queries as queries, video_type_review_decisions as decisions


class VideoTypeReviewsRepository(DatabaseRepository):
    def hidden_identities(self, *, channel_id=None, after_id=None, limit=50):
        return queries.read_hidden_video_identities(self.database, channel_id=channel_id, after_id=after_id, limit=limit)

    def _rows(self, rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Invalid video type review response")
        return rows

    def list_cases(self, state, limit, after_case_id=None):
        """Page by the review's own identity, including detached cases."""
        return self._rows(queries.list_video_type_review_cases(
            self.database, state=state, limit=limit, after_case_id=after_case_id,
        ))

    def get_case(self, case_id):
        result = queries.get_video_type_review_case(self.database, case_id=case_id)
        if result is not None and not isinstance(result, dict):
            raise RuntimeError("Invalid video type review response")
        return result

    def list_history(self, case_id, limit, before_revision=None):
        return self._rows(queries.list_video_type_review_history(
            self.database, case_id=case_id, limit=limit, before_revision=before_revision,
        ))

    def has_open(self):
        result = queries.has_open_video_type_reviews(self.database)
        if not isinstance(result, bool):
            raise RuntimeError("Invalid video type review response")
        return result

    def decide(self, case_id, request_id, expected_revision, video_type, reason):
        result = decisions.decide_video_type_review(
            self.database, case_id=case_id, request_id=request_id,
            expected_revision=expected_revision, video_type=video_type, reason=reason,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Invalid video type review response")
        return result
