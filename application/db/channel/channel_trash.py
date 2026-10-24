"""Self-hosted channel trash; all query construction stays in this repository."""
from ..base.repository import DatabaseRepository
from .channel_control import set_channel_trash_state
from hot_storage_fields import channel_source_fields

class ChannelTrashRepository(DatabaseRepository):
    def removed_titles(self, identities):
        rows = self.database.read_rows("channel_trash_titles", """SELECT user_work_id,title,
            channel_metadata_status,channel_metadata_checked_at,source_expires_at
            FROM readable.channels_with_control WHERE is_hidden IS TRUE AND user_work_id=ANY(%s::uuid[])
            ORDER BY user_work_id LIMIT 100""", (identities,), max_rows=100) or []
        return [channel_source_fields(row, now=self.read_now()) for row in self.admit_source_deadlines(rows)]

    def list_removed(self, after=None):
        statement = "SELECT user_work_id,user_input,hidden_at FROM readable.channel_user_work WHERE is_hidden IS TRUE"
        parameters = ()
        if after is not None:
            statement += " AND user_work_id>%s::uuid"
            parameters = (after,)
        return self.database.read_rows("channel_trash_list", statement + " ORDER BY user_work_id LIMIT 101", parameters, max_rows=101) or []

    def remove(self, channel_id):
        return set_channel_trash_state(self.database, channel_id=channel_id, restore=False)

    def restore(self, identity, removed_at):
        return set_channel_trash_state(self.database, user_work_id=identity,
                                       expected_hidden_at=removed_at, restore=True)

    def restore_channel(self, channel_id, removed_at):
        return set_channel_trash_state(self.database, channel_id=channel_id,
                                       expected_hidden_at=removed_at, restore=True)
