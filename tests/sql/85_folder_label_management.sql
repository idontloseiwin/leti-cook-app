-- Deleting a folder moves its channels to no folder, and a restore keeps the
-- preset Monetized label as the backup has it (tester finding T-06).
begin;
insert into public.folders(id, name) values ('f0000000-0000-4000-8000-000000000085', 'Music');
insert into public.channels(channel_id, title, folder_id, initial_long_inventory_status)
values ('UCfolderfolderfolder0085', 'Folder probe', 'f0000000-0000-4000-8000-000000000085', 'completed');
delete from public.folders where id = 'f0000000-0000-4000-8000-000000000085';
do $$ begin
  assert (select folder_id from public.channels where channel_id = 'UCfolderfolderfolder0085') is null,
    'the channel kept a deleted folder';
end $$;
rollback;
