-- A channel's stored catalogue before it is moved to the trash: more videos
-- than one cleanup page, each with a membership, current fields and a snapshot.
insert into public.video_inventory(video_id, channel_id, video_type, published_at, published_acquired_at)
select 't' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', timestamptz '2025-01-01' + v * interval '1 minute',
       now() - interval '1 day'
from generate_series(1, 110) v;
insert into public.video_inventory_memberships(video_id, channel_id, video_type, playlist_id, source_published_at,
  first_seen_at, last_seen_at, last_present_at)
select 't' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', 'UULFprobeprobeprobeprobe01',
       timestamptz '2025-01-01' + v * interval '1 minute', now() - interval '2 days', now() - interval '1 day', now() - interval '1 day'
from generate_series(1, 110) v;
insert into public.videos(video_id, channel_id, title, views, checked_at, published_at, duration, metadata_checked_at)
select 't' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'Trashed ' || v, 1000 + v, now() - interval '1 day',
       timestamptz '2025-01-01' + v * interval '1 minute', 'PT5M', now() - interval '1 day'
from generate_series(1, 110) v;
insert into public.video_snapshots(video_id, views, recorded_at)
select 't' || lpad(v::text, 10, '0'), 1000 + v, now() - interval '1 day'
from generate_series(1, 110) v;
