-- Paused long videos are selected for a recheck (audit S7-6, S5-4): all of
-- them after a channel count change, and only old enough misses otherwise.
insert into public.channels(channel_id, title, initial_long_inventory_status)
values ('UCprobeprobeprobeprobe01', 'Probe', 'completed');
insert into public.video_inventory(video_id, channel_id, video_type, published_at, published_acquired_at)
select id, 'UCprobeprobeprobeprobe01', 'long', now() - interval '100 days', now()
from unnest(array['vid_fresh01','vid_day1_02','vid_att3_03','vid_att3_04','vid_old_005','vid_priv_06','vid_futur07']) id;
insert into public.video_availability_incidents(video_id, channel_id, first_status, latest_status, first_seen_at, last_seen_at, attempt_count)
values
 ('vid_fresh01','UCprobeprobeprobeprobe01','unknown_unavailable','unknown_unavailable', now()-interval '2 hours', now()-interval '2 hours', 1),
 ('vid_day1_02','UCprobeprobeprobeprobe01','unknown_unavailable','unknown_unavailable', now()-interval '25 hours', now()-interval '25 hours', 1),
 ('vid_att3_03','UCprobeprobeprobeprobe01','unknown_unavailable','unknown_unavailable', now()-interval '9 days', now()-interval '3 days', 3),
 ('vid_att3_04','UCprobeprobeprobeprobe01','unknown_unavailable','unknown_unavailable', now()-interval '9 days', now()-interval '5 days', 3),
 -- Retention scrubbed this incident's status; it still counts as unavailable.
 ('vid_old_005','UCprobeprobeprobeprobe01',null,null, now()-interval '60 days', now()-interval '29 days', 9),
 ('vid_priv_06','UCprobeprobeprobeprobe01','private','private', now()-interval '5 days', now()-interval '1 day', 3),
 ('vid_futur07','UCprobeprobeprobeprobe01','unknown_unavailable','unknown_unavailable', now()-interval '1 day', now()+interval '2 days', 1);
