insert into public.channels(channel_id, title, initial_long_inventory_status)
values ('UCprobeprobeprobeprobe01', 'Probe', 'completed');
insert into public.video_inventory(video_id, channel_id, video_type, published_at, published_acquired_at)
select 'v' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', timestamptz '2025-01-01' + v * interval '1 minute',
       now() - case when v % 300 = 0 then interval '40 days' else interval '1 day' end
from generate_series(1, 1500) v;
-- Every 300th membership is 40 days stale; the rest are current.
insert into public.video_inventory_memberships(video_id, channel_id, video_type, playlist_id, source_published_at,
  first_seen_at, last_seen_at, last_present_at)
select 'v' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', 'UULFprobeprobeprobeprobe01',
       timestamptz '2025-01-01' + v * interval '1 minute',
       now() - case when v % 300 = 0 then interval '45 days' else interval '2 days' end,
       now() - case when v % 300 = 0 then interval '40 days' else interval '1 day' end,
       now() - case when v % 300 = 0 then interval '40 days' else interval '1 day' end
from generate_series(1, 1500) v;
