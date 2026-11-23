"""Settle disabled-format notifications only with current canonical evidence."""
from .discovery import _claims, _update, locked_claim, operation
from .videos.typed_video_read_policy import TYPED_CANDIDATE_INVENTORY_IDS_SQL


def settle_disabled(database, claims, *, enabled_types):
    enabled = frozenset(enabled_types)
    if not {'long', 'short'} <= enabled <= {'long', 'short', 'stream'}:
        raise ValueError('discovery_formats_invalid')
    settled = []
    with database.transaction(operation('discovery_settle_disabled')) as tx:
        for claim in _claims(claims):
            row = locked_claim(tx, claim, fresh=True)
            if row['work_phase'] == 'owner_wait':
                continue
            inventory = tx.fetch_one('SELECT video_type,channel_id FROM public.video_inventory '
                                     'WHERE video_id=%s FOR SHARE', (row['video_id'],))
            if (not inventory or inventory['channel_id'] != row['channel_id']
                    or inventory['video_type'] in enabled):
                continue
            proof = tx.fetch_one(TYPED_CANDIDATE_INVENTORY_IDS_SQL,
                                 {'inventory_ids': [row['video_id']]})
            if not proof or proof['video_type'] in enabled or proof['channel_id'] != row['channel_id']:
                continue
            from .discovery_observations import terminal
            terminal(tx,claim,'format_off')
            settled.append(claim['work_id'])
    return settled
