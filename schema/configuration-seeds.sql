-- Review reference only: these statements are already embedded in schema.sql.
-- Configuration only. No API rows, user work, or prior readiness seals.
INSERT INTO public.channel_labels(label_key,name,label_type)
VALUES ('monetized','Monetized','preset');
INSERT INTO public.typed_video_collection_runtime(video_type,runtime_enabled)
VALUES ('short',false),('stream',false);
INSERT INTO public.video_collection_control(singleton,reconciliation_required)
VALUES (true,true);
INSERT INTO public.channel_discovery_queue_control(singleton,reconciliation_required)
VALUES (true,true);
INSERT INTO public.video_collection_cadence(video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds) VALUES
('long','counters','public',0,86400,3600,1,true,NULL,0),
('long','counters','public',86400,604800,7200,1,true,NULL,0),
('long','counters','public',604800,2419200,21600,1,true,NULL,0),
('long','counters','public',2419200,7776000,43200,1,true,NULL,0),
('long','counters','public',7776000,31536000,86400,1,true,NULL,0),
('long','counters','public',31536000,NULL,604800,1,true,NULL,0),
('long','counters','unlisted',0,NULL,2419200,1,true,NULL,0),
('long','metadata','public',0,604800,21600,1,true,NULL,0),
('long','metadata','public',604800,2419200,86400,1,true,NULL,0),
('long','metadata','public',2419200,31536000,604800,1,true,NULL,0),
('long','metadata','public',31536000,NULL,2419200,1,true,NULL,0),
('long','metadata','unlisted',0,NULL,2419200,1,true,NULL,0),
('short','metadata','all',0,7776000,604800,1,true,'age_0_90d',21600),
('short','metadata','all',7776000,NULL,2419200,1,true,'age_91d_plus',21600),
('stream','metadata','all',0,7776000,604800,1,true,'age_0_90d',21600),
('stream','metadata','all',7776000,NULL,2419200,1,true,'age_91d_plus',21600);
