-- Research catalogue read model. One row per inventory video keeps the type
-- proof of the protected views, duration seconds and the casefold title key as
-- of its last refresh, valid until the first source clock at which those views
-- could answer differently. A write to a proof source queues the video; readers
-- use a row only while it is current and unqueued and otherwise read the
-- protected views, so a late refresh costs speed, never results. The backend
-- refreshes rows with ordinary statements; these hooks only queue and track.
CREATE TABLE public.video_search_projection (
    video_id text PRIMARY KEY REFERENCES public.video_inventory(video_id) ON UPDATE CASCADE ON DELETE CASCADE,
    channel_id text NOT NULL,
    inventory_type text,
    hot_present boolean NOT NULL DEFAULT false,
    identity_clock timestamptz,
    identity_expiry timestamptz,
    title_key text,
    duration_seconds numeric,
    valid_until timestamptz NOT NULL DEFAULT '-infinity',
    refreshed_at timestamptz
);
CREATE INDEX video_search_projection_channel_idx ON public.video_search_projection (channel_id);
CREATE INDEX video_search_projection_valid_idx ON public.video_search_projection (valid_until);

-- One marker per video; a write bumps its generation, so a refresh that read an
-- older generation cannot remove the marker of a later write.
CREATE TABLE public.video_search_projection_queue (
    video_id text PRIMARY KEY,
    generation bigint NOT NULL DEFAULT 1
);

CREATE FUNCTION public.queue_video_search_projection() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 -- Detached review cases have no video and queue nothing.
 IF TG_OP IN ('UPDATE','DELETE') AND OLD.video_id IS NOT NULL THEN
  INSERT INTO public.video_search_projection_queue AS q(video_id) VALUES (OLD.video_id)
  ON CONFLICT (video_id) DO UPDATE SET generation=q.generation+1;
 END IF;
 IF TG_OP IN ('INSERT','UPDATE') AND NEW.video_id IS NOT NULL THEN
  IF TG_OP = 'INSERT' OR NEW.video_id IS DISTINCT FROM OLD.video_id THEN
   INSERT INTO public.video_search_projection_queue AS q(video_id) VALUES (NEW.video_id)
   ON CONFLICT (video_id) DO UPDATE SET generation=q.generation+1;
  END IF;
 END IF;
 -- Derived search fields never outlive their source columns: a cleanup that
 -- removes a title, a duration or the video removes them in the same write.
 IF TG_TABLE_NAME = 'videos' THEN
  IF TG_OP = 'DELETE' THEN
   UPDATE public.video_search_projection SET title_key=NULL,duration_seconds=NULL WHERE video_id=OLD.video_id;
  ELSE
   IF TG_OP = 'UPDATE' AND NEW.video_id IS DISTINCT FROM OLD.video_id THEN
    UPDATE public.video_search_projection SET title_key=NULL,duration_seconds=NULL WHERE video_id=OLD.video_id;
   END IF;
   IF NEW.title IS NULL OR NEW.duration IS NULL THEN
    UPDATE public.video_search_projection
    SET title_key=CASE WHEN NEW.title IS NULL THEN NULL ELSE title_key END,
        duration_seconds=CASE WHEN NEW.duration IS NULL THEN NULL ELSE duration_seconds END
    WHERE video_id=NEW.video_id
      AND ((NEW.title IS NULL AND title_key IS NOT NULL) OR (NEW.duration IS NULL AND duration_seconds IS NOT NULL));
   END IF;
  END IF;
 END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION public.track_video_search_inventory() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF TG_OP = 'INSERT' THEN
  INSERT INTO public.video_search_projection AS p(video_id,channel_id) VALUES (NEW.video_id,NEW.channel_id)
  ON CONFLICT (video_id) DO NOTHING;
 ELSE
  UPDATE public.video_search_projection SET channel_id=NEW.channel_id
  WHERE video_id=NEW.video_id AND channel_id IS DISTINCT FROM NEW.channel_id;
 END IF;
 RETURN NULL;
END $$;

ALTER TABLE public.video_search_projection OWNER TO postgres;
ALTER TABLE public.video_search_projection_queue OWNER TO postgres;
ALTER FUNCTION public.queue_video_search_projection() OWNER TO postgres;
ALTER FUNCTION public.track_video_search_inventory() OWNER TO postgres;
REVOKE ALL ON FUNCTION public.queue_video_search_projection() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.track_video_search_inventory() FROM PUBLIC;
GRANT SELECT,UPDATE ON TABLE public.video_search_projection TO service_role;
GRANT SELECT,DELETE ON TABLE public.video_search_projection_queue TO service_role;

-- Title key and duration seconds are exposed only with their sources.
CREATE VIEW readable.video_search_projection WITH (security_barrier=true, security_invoker=false) AS
SELECT p.video_id,p.channel_id,
 (p.valid_until>statement_timestamp() AND p.refreshed_at<=statement_timestamp()
  AND NOT EXISTS (SELECT 1 FROM public.video_search_projection_queue q WHERE q.video_id=p.video_id)) AS proof_current,
 p.inventory_type,p.hot_present,p.identity_clock,p.identity_expiry,
 CASE WHEN v.title IS NOT NULL THEN p.title_key END AS title_key,
 CASE WHEN v.duration IS NOT NULL THEN p.duration_seconds END AS duration_seconds,
 v.video_id AS v_video_id,v.channel_id AS v_channel_id,v.title,v.published_at,v.duration,v.views,v.checked_at,
 v.metadata_checked_at,v.availability_checked_at,v.source_expires_at AS video_expiry,v.availability_status,
 v.publication_status,v.stream_actual_end_at
FROM public.video_search_projection p LEFT JOIN readable.videos v ON v.video_id=p.video_id;
ALTER VIEW readable.video_search_projection OWNER TO postgres;
GRANT SELECT ON readable.video_search_projection TO lic_reader;

CREATE TRIGGER video_inventory_search_projection AFTER INSERT OR UPDATE OF channel_id ON public.video_inventory
 FOR EACH ROW EXECUTE FUNCTION public.track_video_search_inventory();
CREATE TRIGGER video_inventory_search_queue AFTER INSERT OR UPDATE OR DELETE ON public.video_inventory
 FOR EACH ROW EXECUTE FUNCTION public.queue_video_search_projection();
CREATE TRIGGER video_membership_search_queue AFTER INSERT OR UPDATE OR DELETE ON public.video_inventory_memberships
 FOR EACH ROW EXECUTE FUNCTION public.queue_video_search_projection();
CREATE TRIGGER video_review_search_queue AFTER INSERT OR UPDATE OR DELETE ON public.video_type_review_cases
 FOR EACH ROW EXECUTE FUNCTION public.queue_video_search_projection();
-- Counters and availability are read live; they do not queue a video.
CREATE TRIGGER videos_search_queue AFTER INSERT OR DELETE
 OR UPDATE OF video_id,channel_id,title,published_at,metadata_checked_at,duration,publication_status
 ON public.videos FOR EACH ROW EXECUTE FUNCTION public.queue_video_search_projection();

-- Existing rows start past their deadline and are read live until refreshed.
INSERT INTO public.video_search_projection(video_id,channel_id)
SELECT video_id,channel_id FROM public.video_inventory;
