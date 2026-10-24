insert into public.video_inventory(video_id, channel_id, video_type, published_at, published_acquired_at)
values ('vid_fresh01','UCprobeprobeprobeprobe01','long',now()-interval '100 days',now()-interval '1 hour'),
       ('vid_stale02','UCprobeprobeprobeprobe01','long',now()-interval '100 days',now()-interval '29 days'),
       ('vid_priv_03','UCprobeprobeprobeprobe01','long',now()-interval '10 days',now());
insert into public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,revision,first_seen_at,last_seen_at,evidence_expires_at)
values ('vid_fresh01','UCprobeprobeprobeprobe01','long',array['long','short'],array['UCprobeprobeprobeprobe01'],'open',4,now()-interval '60 days',now()-interval '1 hour',now()+interval '696 hours'-interval '1 hour'),
       ('vid_stale02','UCprobeprobeprobeprobe01','long',array['long','short'],array['UCprobeprobeprobeprobe01'],'open',4,now()-interval '60 days',now(),now());
insert into public.videos(video_id, channel_id, availability_status)
values ('vid_priv_03','UCprobeprobeprobeprobe01','private');
