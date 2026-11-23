-- Current physical token storage boundary; workflow scenarios are mandatory Python cases.
begin;
insert into public.channels(channel_id,title) values('UCprobeprobeprobeprobe01','Probe');
insert into public.channel_collection_state(channel_id,user_work_id,owner_permission) select channel_id,user_work_id,'accepted' from public.channels where channel_id='UCprobeprobeprobeprobe01';
set local role service_role;
do $$ begin
  assert not exists (
    select 1 from pg_catalog.pg_attribute
    where attrelid = 'public.channels'::regclass
      and attname = 'deep_inventory_page_token' and not attisdropped
  ), 'retired physical cursor column still exists';
  assert (select deep_inventory_page_token is null
          from public.channels_with_control
          where channel_id = 'UCprobeprobeprobeprobe01'),
         'compatibility projection is not NULL';
  begin
    execute 'update public.channels set deep_inventory_page_token = $1 where channel_id = $2'
      using 'EAAaBlBUOkNBVQ', 'UCprobeprobeprobeprobe01';
    assert false, 'a page token was stored';
  exception when undefined_column then
    null;
  end;
end $$;
rollback;
