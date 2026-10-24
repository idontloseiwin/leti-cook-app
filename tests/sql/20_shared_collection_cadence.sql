-- Fixed-clock cadence regression; isolated PostgreSQL CI only.
-- Long policy and typed 90-day edges, early jitter, HOT deadline and Stream end anchor.
begin;
DO $long_seed_pin$ DECLARE changed boolean;n integer;BEGIN
 WITH expected(video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,
  cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds) AS (VALUES
('long','counters','public',0::bigint,86400::bigint,3600,1,true,NULL::text,0),
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
('long','metadata','unlisted',0,NULL,2419200,1,true,NULL,0)), actual AS (
 SELECT video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,
  cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds
 FROM public.video_collection_cadence WHERE video_type='long'
 ), difference AS (
 (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
 UNION ALL
 (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual))
 SELECT EXISTS(SELECT 1 FROM difference) INTO changed;
 SELECT count(*) INTO n FROM public.video_collection_cadence WHERE video_type='long';
 IF changed OR n<>12 THEN RAISE EXCEPTION 'phase4b_long_seed_pin_mismatch'; END IF;
END $long_seed_pin$;
DO $$ BEGIN
 assert (SELECT count(*) FROM public.video_collection_cadence)=16, 'cadence_seed_count';
 assert (SELECT count(*) FROM public.video_collection_cadence WHERE video_type='short')=2, 'short_seed_count';
 assert (SELECT count(*) FROM public.video_collection_cadence WHERE video_type='stream')=2, 'stream_seed_count';
END $$;
-- Fresh-schema bounds must accept 28 days and refuse even one second more.
DO $cadence_cap$ DECLARE violated text; BEGIN
 assert (SELECT count(*) FROM public.video_collection_cadence
  WHERE video_type IN ('short','stream') AND age_bucket='age_91d_plus'
    AND cadence_seconds=2419200)=2, 'typed_old_default_28_days';
 UPDATE public.video_collection_cadence SET cadence_seconds=2419200
 WHERE video_type='long' AND collection_kind='counters' AND availability_class='public'
   AND min_age_seconds=0 AND policy_version=1;
 assert (SELECT cadence_seconds FROM public.video_collection_cadence
  WHERE video_type='long' AND collection_kind='counters' AND availability_class='public'
    AND min_age_seconds=0 AND policy_version=1)=2419200, 'cadence_cap_28_days_accepted';
 BEGIN
  UPDATE public.video_collection_cadence SET cadence_seconds=2419201
  WHERE video_type='long' AND collection_kind='counters' AND availability_class='public'
    AND min_age_seconds=0 AND policy_version=1;
  RAISE EXCEPTION 'cadence_above_28_days_was_accepted';
 EXCEPTION WHEN check_violation THEN
  GET STACKED DIAGNOSTICS violated=CONSTRAINT_NAME;
  assert violated='video_collection_cadence_hot_maximum', 'cadence_maximum_constraint_expected';
 END;
END $cadence_cap$;
rollback;
