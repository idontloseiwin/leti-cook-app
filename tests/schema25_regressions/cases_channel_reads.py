"""Actual retired-view readers on empty, live and malformed source rows."""
import json
from db.reads.frontend import FrontendRepository
from db.channel.channel_sync import ChannelSyncRepository
from db.channel.channel_discovery import ChannelDiscoveryRepository
from db.inventory.inventory_maintenance import InventoryMaintenanceRepository
from db.reads.system_health import SystemHealthRepository,CHANNEL_HEALTH_COLUMNS,CHANNEL_HEALTH_ORDER
from db.channel.channel_discovery_projection import COLUMNS
from db.base.postgres import DatabaseError
from .fixtures import CHANNEL

BAD='UCzzzzzzzzzzzzzzzzzzzzzz'

def channel_readers(f):
    front=FrontendRepository(f.database);sync=ChannelSyncRepository(f.database)
    discovery=ChannelDiscoveryRepository(f.database);maintenance=InventoryMaintenanceRepository(f.database)
    health=SystemHealthRepository(f.database)
    def owners():
        return (front.load_channel_rows('channel_id,is_hidden'),front.load_channel(CHANNEL),
          sync.load_channel_record(CHANNEL),sync.load_running_status_page(channel_id=CHANNEL,page=0,page_size=2),
          discovery.load_timing(CHANNEL),maintenance.load_channels('channel_id'),
          maintenance.load_channels_page('channel_id',offset=0,page_size=2),
          maintenance.fetch_all_rows('channels','channel_id','title',page_size=2),
          health.fetch_paginated('channels',CHANNEL_HEALTH_COLUMNS,operations=CHANNEL_HEALTH_ORDER,page_size=2,max_rows=2))
    before=(f.snapshot(),f.sequences(),f.catalog())
    f.check(f.scalar("SELECT to_regclass('public.channels_with_discovery') IS NULL"),'channel_read_retired_view_absent')
    f.check(owners()==([],[],None,[],{},[],[],[],([],False)),'channel_read_empty_nine_owners')
    f.check((f.snapshot(),f.sequences(),f.catalog())==before,'channel_read_empty_no_write')
    try:
        f.channel()
        estimate={'scope':'typed','video_types':['long'],'counts':{'long':12},'statuses':{'long':'ok'},'checked_at_by_type':{'long':f.now.isoformat()}}
        f.rows("UPDATE public.channels SET channel_metadata_status='ok',channel_metadata_checked_at=%s,initial_inventory_estimate=%s::jsonb WHERE channel_id=%s",(f.now,json.dumps(estimate),CHANNEL))
        f.rows("INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,source_checked_at,last_result) VALUES(%s,'long',%s,%s,'no_new_videos'),(%s,'short',%s,%s,'different_short') ON CONFLICT(channel_id,video_type) DO UPDATE SET source_checked_at=excluded.source_checked_at,last_result=excluded.last_result",(CHANNEL,'UULF'+CHANNEL[2:],f.now,CHANNEL,'UUSH'+CHANNEL[2:],f.now))
        before_reads=(f.snapshot(),f.sequences(),f.catalog())
        values=owners();record=values[2]
        f.check(list(record)==list(COLUMNS),'channel_read_fifty_column_order')
        f.check(record['channel_id']==CHANNEL and record['collection_mode']=='active' and record['collection_permission']=='accepted' and record['is_hidden'] is False and record['collection_control_epoch']==f.epoch(),'channel_read_control_projection')
        f.check(record['deep_inventory_page_token'] is None and record['long_video_count']==12 and record['channel_discovery_status']=='no_new_videos','channel_read_native_source_projection')
        f.check(len(values[0])==len(values[1])==len(values[3])==len(values[5])==len(values[6])==len(values[7])==len(values[8][0])==1 and values[8][1] is False,'channel_read_populated_nine_owners')
        f.check(values[4]['channel_discovery_checked_at']==record['channel_discovery_checked_at'] and values[1][0]['title']=='Schema CI synthetic' and values[1][0]['long_video_count']==12,'channel_read_public_source_admission')
        f.check((f.snapshot(),f.sequences(),f.catalog())==before_reads,'channel_read_populated_no_write')
        f.rows('DELETE FROM public.channel_collection_state WHERE channel_id=%s',(CHANNEL,))
        absent=sync.load_channel_record(CHANNEL)
        f.check(absent['is_hidden'] is True and absent['hidden_at'] is None and absent['collection_permission']=='legacy_hold' and absent['collection_control_epoch'] is None and absent['collection_mode']=='operator_hold','channel_read_missing_control_fail_closed')
        # Unselected count must not force a legacy malformed clock through its
        # failing native cast, nor may another addressed row be evaluated.
        f.channel(BAD)
        invalid='2026-10-07 12:00:00+16:00'
        f.check(f.scalar("SELECT (pg_input_error_info(%s,'timestamptz')).sql_error_code",(invalid,))=='22009','channel_read_malformed_fixture_native_code')
        malformed={**estimate,'checked_at_by_type':{'long':invalid}}
        f.rows('UPDATE public.channels SET initial_inventory_estimate=%s::jsonb WHERE channel_id=%s',(json.dumps(malformed),BAD))
        f.check(len(front.load_channel_rows('channel_id,is_hidden'))==2,'channel_read_unselected_count_lazy')
        f.check(sync.load_channel_record(CHANNEL)['long_video_count']==12,'channel_read_addressed_count_lazy')
        f.check(maintenance.load_channels_page('channel_id,long_video_count',offset=0,page_size=1,order_column='channel_id')==[{'channel_id':CHANNEL,'long_video_count':12}],'channel_read_limited_count_lazy')
        try:sync.load_channel_record(BAD)
        except DatabaseError as error:f.check(error.code=='22009','channel_read_selected_native_error')
        else:raise AssertionError('channel_read_selected_native_error_missing')
        f.gate.ready=False
        try:
            import hot_runtime
            try:front.load_channel_rows('channel_id')
            except hot_runtime.HotRuntimeUnavailable:pass
            else:raise AssertionError('channel_read_admission_bypassed')
        finally:f.gate.ready=True
        f.check(f.observe()<=1000,'channel_read_fixture_bound')
    finally:f.reset()
    f.check((f.snapshot(),f.sequences(),f.catalog())==before,'channel_read_exact_cleanup')
