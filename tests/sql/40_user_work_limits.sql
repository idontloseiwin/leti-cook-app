-- Ordinary writes stop at the limits a user-work backup exports (audit S3-2,
-- S5-5): up to the limit succeeds, one more is refused.
begin;
do $$
declare pending uuid := 'a0000000-0000-4000-8000-000000000001';
begin
  insert into public.folders(id, name) select gen_random_uuid(), 'Folder ' || i from generate_series(1, 1000) i;
  begin
    insert into public.folders(id, name) values (gen_random_uuid(), 'One too many');
    assert false, 'folder 1001 was accepted';
  exception when raise_exception then
    assert sqlerrm = 'user_work_limit_reached', sqlerrm;
  end;

  -- The seed label counts too: 1,001 labels in all.
  insert into public.channel_labels(label_key, name, label_type)
  select 'custom:' || gen_random_uuid(), 'Label ' || i, 'custom'
  from generate_series(1, 1001 - (select count(*) from public.channel_labels)) i;
  begin
    insert into public.channel_labels(label_key, name, label_type) values ('custom:' || gen_random_uuid(), 'One too many', 'custom');
    assert false, 'label 1002 was accepted';
  exception when raise_exception then
    assert sqlerrm = 'user_work_limit_reached', sqlerrm;
  end;

  insert into public.saved_objects(kind, id, name, create_fingerprint, revision, member_count)
  select 'collection', gen_random_uuid(), 'Collection ' || i, repeat('0', 64), 1, 0 from generate_series(1, 1000) i;
  begin
    insert into public.saved_objects(kind, id, name, create_fingerprint, revision, member_count)
    values ('collection', gen_random_uuid(), 'One too many', repeat('0', 64), 1, 0);
    assert false, 'saved object 1001 was accepted';
  exception when raise_exception then
    assert sqlerrm = 'user_work_limit_reached', sqlerrm;
  end;

  -- A waiting restore item counts as a channel until it is bound.
  insert into public.user_channel_restore_items(id, user_input, is_hidden)
  values (pending, 'https://www.youtube.com/@pending', false);
  insert into public.channels(channel_id, title)
  select 'UC' || lpad(i::text, 22, '0'), 'Channel ' || i from generate_series(1, 9999) i;
  begin
    insert into public.channels(channel_id, title) values ('UCoverlimitoverlimitove1', 'One too many');
    assert false, 'channel 10001 was accepted';
  exception when raise_exception then
    assert sqlerrm = 'user_work_limit_reached', sqlerrm;
  end;
  -- Binding that item creates its channel before the item is removed: counted once.
  insert into public.channels(channel_id, title, user_work_id) values ('UCboundboundboundboundb1', 'Bound', pending);
end $$;
rollback;
