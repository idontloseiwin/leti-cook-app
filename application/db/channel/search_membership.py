"""One bounded, owner-only lookup for a page of externally discovered channels."""
from db.base.postgres import DatabaseError


def library_membership(database, ids):
    try:
        rows = database.read_rows("channel_search_membership",
                                  "SELECT channel_id,is_hidden FROM readable.channel_control WHERE channel_id=ANY(%s::text[])",
                                  (ids,), max_rows=len(ids))
    except DatabaseError:
        return {key: "unknown" for key in ids}
    states = {key: "new" for key in ids}
    states.update({row["channel_id"]: "removed" if row["is_hidden"] else "tracked" for row in rows})
    return states
