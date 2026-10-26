-- Workspace limits are checked after an insert, by counting. Two writers at a
-- limit could both count before either committed and both pass, after which a
-- backup could no longer be exported. The check now takes a per-table
-- transaction lock first.
CREATE OR REPLACE FUNCTION public.enforce_user_work_backup_limits() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
-- Ordinary writes stay within the limits that export_user_work_backup and
-- import_user_work_backup accept, so using the app can never make a backup
-- fail. A restore item bound in this transaction is counted once: its channel
-- already has the item's id as user_work_id before the item is removed.
DECLARE exceeded boolean;
BEGIN
 -- Two writers at a limit must not both see room: the count is serialized per
 -- table until commit, so a later writer counts an earlier writer's rows.
 PERFORM pg_advisory_xact_lock(hashtextextended('user_work_limits:'||TG_TABLE_NAME,0));
 exceeded:=CASE TG_TABLE_NAME
  WHEN 'channels' THEN
   (SELECT count(*) FROM public.channels)
   +(SELECT count(*) FROM public.user_channel_restore_items p
     WHERE NOT EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=p.id))>10000
  WHEN 'saved_objects' THEN
   (SELECT count(*) FROM public.saved_objects WHERE deleted_at IS NULL)
   +coalesce((SELECT sum(jsonb_array_length(p.saved_views)) FROM public.user_channel_restore_items p
     WHERE NOT EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=p.id)),0)>1000
  WHEN 'folders' THEN (SELECT count(*) FROM(SELECT 1 FROM public.folders LIMIT 1001)x)>1000
  WHEN 'channel_labels' THEN (SELECT count(*) FROM(SELECT 1 FROM public.channel_labels LIMIT 1002)x)>1001
  WHEN 'channel_label_assignments' THEN
   (SELECT count(*) FROM public.channel_label_assignments)
   +coalesce((SELECT sum(cardinality(p.label_keys)) FROM public.user_channel_restore_items p
     WHERE NOT EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=p.id)),0)>100000
  WHEN 'saved_collection_members' THEN
   (SELECT count(*) FROM(SELECT 1 FROM public.saved_collection_members LIMIT 100001)x)>100000
  -- Every decision adds history, so both review limits are checked here.
  WHEN 'video_type_review_history' THEN
   (SELECT count(*) FROM(SELECT 1 FROM public.video_type_review_history LIMIT 10001)x)>10000
   OR (SELECT count(*) FROM(SELECT 1 FROM public.video_type_review_cases c WHERE c.decision_type IS NOT NULL
       OR EXISTS(SELECT 1 FROM public.video_type_review_history h WHERE h.case_id=c.case_id) LIMIT 1001)x)>1000
  ELSE false
 END;
 IF exceeded THEN
  RAISE EXCEPTION 'user_work_limit_reached' USING DETAIL=TG_TABLE_NAME;
 END IF;
 RETURN NULL;
END $$;
