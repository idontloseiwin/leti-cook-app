"""User-authored work snapshots and atomic restore boundaries; no API calls."""
from __future__ import annotations
from psycopg import sql

from ..base.repository import DatabaseRepository
from ..base.sql_values import projection
from .user_work_read_queries import export_snapshot, restore_status
from .user_work_restore import import_user_work_backup, update_user_channel_restore_item
from .user_work_restore_bind import bind_user_channel_restore_item
from .user_work_reconnect import enqueue_reconnect, reconnect_candidates, reconnect_candidate_page, claim_reconnect, defer_reconnect, bind_reconnect

PENDING_FIELDS = "id,user_input,folder_id,parent_ref,is_hidden,added_at,label_keys,saved_views,revision,reconnect_state"


class UserWorkBackupRepository(DatabaseRepository):
    @staticmethod
    def _result(result):
        if not isinstance(result, dict):
            raise RuntimeError("user_work_response_invalid")
        return result

    @staticmethod
    def _rows(rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("user_work_response_invalid")
        return rows

    def snapshot(self):
        return export_snapshot(self.database)

    def import_document(self, document):
        return self._result(import_user_work_backup(self.database, document))

    def pending_item(self, identity):
        rows = self._rows(self.database.read_rows("user_work_pending_item",
            sql.SQL("SELECT {} FROM public.user_channel_restore_items WHERE id=%s::uuid LIMIT 1").format(projection(PENDING_FIELDS)),
            (identity,), max_rows=1))
        return rows[0] if rows else None

    def update_pending(self, identity, expected_revision, user_input, parent_ref):
        return self._result(update_user_channel_restore_item(
            self.database, identity=identity, expected_revision=expected_revision,
            user_input=user_input, parent_ref=parent_ref))

    def bind_pending(self, identity, expected_revision, channel_id, channel_data=None):
        return self._result(bind_user_channel_restore_item(
            self.database, identity=identity, expected_revision=expected_revision,
            channel_id=channel_id, channel_data=channel_data))

    def status(self, offset=0):
        # Advisory UI state; import rechecks emptiness under table locks.
        return restore_status(self.database, offset)

    def enqueue_reconnect(self, body, item_id=None):
        return self._result(enqueue_reconnect(self.database, body, item_id=item_id))

    def reconnect_candidates(self, *, limit=16):
        return self._rows(reconnect_candidates(self.database,limit=limit))

    def reconnect_candidate_page(self, *, limit=16, after=None, ceiling=None):
        return self._result(reconnect_candidate_page(self.database,limit=limit,after=after,ceiling=ceiling))

    def claim_reconnect(self, *, limit=1, lease_seconds=180, identity=None):
        return self._rows(claim_reconnect(self.database,limit=limit,lease_seconds=lease_seconds,identity=identity))

    def defer_reconnect(self, identity, expected_revision, lease_token, *, reason, retry_seconds=None):
        return self._result(defer_reconnect(self.database,identity,expected_revision,lease_token,
                                          reason=reason,retry_seconds=retry_seconds))

    def bind_reconnect(self, identity, expected_revision, lease_token, channel_data, *, max_attempts=3):
        return self._result(bind_reconnect(self.database,identity,expected_revision,lease_token,channel_data,
                                          max_attempts=max_attempts))

    def abort_reconnect(self, identity, expected_revision, lease_token):
        from hot_runtime import collection_control_access
        with collection_control_access():
            value = self.database.abort_hot_reconnect_claim(
                identity=identity, expected_revision=expected_revision, lease_token=lease_token)
        if type(value) is not dict or set(value) != {'outcome'} or value['outcome'] not in ('ready', 'lease_lost'):
            raise RuntimeError('hot_reconnect_abort_unconfirmed')
        return value
