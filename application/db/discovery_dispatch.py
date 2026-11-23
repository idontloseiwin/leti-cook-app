"""Release only this interrupted dispatcher's exact event-processing leases."""
from .discovery import _claims, _TABLES, operation


def release_claims(database, claims):
    if not claims:return
    with database.transaction(operation('discovery_dispatch_release')) as tx:
        for row in _claims(list(claims)):
            table,key,cast=_TABLES[row['origin']]
            tx.execute(f'''UPDATE public.{table} SET claim_token=NULL,claim_expires_at=NULL
                WHERE {key}=%s::{cast} AND status='pending' AND claim_token=%s::uuid
                AND claim_channel_epoch=%s''',
                (row['work_id'],row['claim_token'],row['claim_channel_epoch']))
