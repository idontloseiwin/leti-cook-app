"""Addressed evidence removal on the parent's disposable PG17 fixture."""
import os
from uuid import uuid4

from psycopg.types.json import Jsonb

from db.base.postgres import Operation
from db.retention import deleted_channel_retention as subject


def run_checks(admin, database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
        raise RuntimeError('disposable_cluster_required')
    channel = 'UC' + 'fxdelete'.ljust(22, '0')
    foreign = 'UC' + 'fxforeign'.ljust(22, '0')
    receipts = [str(uuid4()) for _ in range(4)]
    campaign = str(uuid4())
    checks = []

    def check(value, name):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    for offset, identity in enumerate(receipts):
        # No channel or scan row: this is exactly the evidence that the former
        # scan_id-only deletion missed. Only fixture receipt identities exist.
        admin.execute('''INSERT INTO public.typed_inventory_receipts
            (receipt_scope,transition_id,source_checked_at,result)
            VALUES('addressed_membership',%s::uuid,clock_timestamp(),%s)''',
            (identity, Jsonb(dict(channel_id=channel if offset < 3 else foreign,
                                 video_type='short', video_ids=['fxretention']))))
    params = dict(channel=channel, owner=str(uuid4()), limit=1)
    try:
        with database.transaction(Operation('discovery_retention_test')) as tx:
            check(tx.fetch_one(subject._CHILDREN, params)['remaining'],
                  'addressed_without_scan_keeps_channel_pending')
            check(tx.execute(subject._ADDRESSED_RECEIPTS_DELETE, params) == 1,
                  'addressed_deletion_is_page_bounded')
            check(tx.fetch_one(subject._CHILDREN, params)['remaining'],
                  'remaining_addressed_page_blocks_channel_removal')
            params['limit'] = 2
            check(tx.execute(subject._ADDRESSED_RECEIPTS_DELETE, params) == 2,
                  'next_page_removes_remaining_channel_receipts')
            check(not tx.fetch_one(subject._CHILDREN, params)['remaining'],
                  'completed_addressed_deletion_releases_channel')
            check(tx.fetch_one('SELECT count(*) AS n FROM public.typed_inventory_receipts '
                'WHERE transition_id=%s::uuid', (receipts[-1],))['n'] == 1,
                'unrelated_channel_receipt_untouched')
        admin.execute('''INSERT INTO public.discovery_repair_campaigns
            (campaign_id,definition_digest,definition_version,window_start,window_end,
             target_limit,general_limit,statistics_limit,target_count)
            VALUES(%s::uuid,%s,1,clock_timestamp()-interval '7 days',clock_timestamp(),4,4,4,4)''',
            (campaign,uuid4().hex+uuid4().hex))
        for offset in range(4):
            admin.execute('''INSERT INTO public.discovery_repair_targets
                (campaign_id,channel_id,video_id,source_event_ids,received_at)
                VALUES(%s::uuid,%s,%s,ARRAY[1]::bigint[],clock_timestamp())''',
                (campaign,channel if offset<3 else foreign,f'fxrepair{offset:03d}'))
        params['limit']=1
        with database.transaction(Operation('discovery_retention_test')) as tx:
            check(tx.fetch_one(subject._CHILDREN,params)['remaining'],
                  'pending_repair_target_keeps_channel_removal_pending')
            check(tx.execute(subject._DISCOVERY_REPAIR_DELETE,params)==1,
                  'repair_target_deletion_is_page_bounded')
            check(tx.fetch_one(subject._CHILDREN,params)['remaining'],
                  'remaining_repair_targets_prevent_early_channel_removal')
            params['limit']=2
            check(tx.execute(subject._DISCOVERY_REPAIR_DELETE,params)==2,
                  'last_channel_repair_targets_removed')
            check(not tx.fetch_one(subject._CHILDREN,params)['remaining'],
                  'removed_repair_targets_release_channel')
            check(tx.fetch_one('SELECT count(*) AS n FROM public.discovery_repair_targets '
                'WHERE campaign_id=%s::uuid AND channel_id=%s',(campaign,foreign))['n']==1,
                'unrelated_repair_target_untouched')
            check(tx.fetch_one('SELECT target_count FROM public.discovery_repair_campaigns '
                'WHERE campaign_id=%s::uuid',(campaign,))['target_count']==4,
                'original_campaign_cohort_count_preserved')
    finally:
        admin.execute('DELETE FROM public.typed_inventory_receipts '
                      'WHERE transition_id=ANY(%s::uuid[])', (receipts,))
        admin.execute('DELETE FROM public.discovery_repair_campaigns WHERE campaign_id=%s::uuid',(campaign,))
    return checks
