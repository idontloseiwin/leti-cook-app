-- Long videos only, as with Shorts and Streams off by default: no queue row
-- carries typed fields. Every video has a fresh membership.
insert into public.video_inventory(video_id, channel_id, video_type, published_at, published_acquired_at)
select 'q' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', timestamptz '2025-01-01' + v * interval '1 minute',
       now() - interval '1 day'
from generate_series(1, 150) v;
insert into public.video_inventory_memberships(video_id, channel_id, video_type, playlist_id, source_published_at,
  first_seen_at, last_seen_at, last_present_at)
select 'q' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', 'UULFprobeprobeprobeprobe01',
       timestamptz '2025-01-01' + v * interval '1 minute', now() - interval '2 days', now() - interval '1 day', now() - interval '1 day'
from generate_series(1, 150) v;
insert into public.video_collection_state(video_id, channel_id, video_type)
select 'q' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long'
from generate_series(1, 150) v;
