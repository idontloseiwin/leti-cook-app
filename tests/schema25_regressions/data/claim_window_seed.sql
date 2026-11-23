-- A due queue whose 1000 earliest rows are held by live leases (an abandoned
-- worker) and five later rows are free.
insert into public.video_inventory(video_id, channel_id, video_type, published_at, published_acquired_at)
select 'w' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', now() - interval '100 days', now() - interval '1 hour'
from generate_series(1, 1005) v;
insert into public.video_collection_state(video_id, channel_id, video_type, next_due_at,
  lease_token, lease_owner, lease_started_at, lease_expires_at, lease_channel_epoch)
select 'w' || lpad(v::text, 10, '0'), 'UCprobeprobeprobeprobe01', 'long', now() - interval '2 hours' + v * interval '1 second',
       case when v <= 1000 then md5(v::text)::uuid end, case when v <= 1000 then 'abandoned-worker' end,
       case when v <= 1000 then now() - interval '1 minute' end, case when v <= 1000 then now() + interval '4 minutes' end,
       case when v <= 1000 then 1 end
from generate_series(1, 1005) v;
