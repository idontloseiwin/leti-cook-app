-- A single channel refresh timestamps every snapshot in its batch alike.
-- Identify the snapshot by both fields of its primary key. Epoch seconds retain
-- microseconds and have the same representation in every session time zone.
DROP TRIGGER alert_channel_observations ON public.channel_snapshots;
CREATE TRIGGER alert_channel_observations AFTER INSERT ON public.channel_snapshots
REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION public.capture_alert_observations(
'channel.subscribers',
'SELECT ''channel.subscribers'' AS event,s.channel_id AS subject,s.channel_id,
        s.channel_id || '':'' || extract(epoch FROM s.recorded_at)::text AS source_key,s.recorded_at AS at,NULL::text AS video_type,
        NULL::timestamptz AS published_at,NULL::text AS duration,NULL::timestamptz AS evidence_expires_at,
        NULL::timestamptz AS duration_since,NULL::timestamptz AS metadata_expires_at
 FROM inserted s WHERE s.source=''youtube_channel_metadata_refresh'' AND s.subscribers IS NOT NULL');

-- A replay under a different time zone could spell the old timestamp key
-- differently. Keep one pending delivery per snapshot.
WITH repeated AS (
    SELECT id, row_number() OVER (
        PARTITION BY rule_id, version, channel_id, observed_at ORDER BY id
    ) AS occurrence
    FROM public.alert_observations WHERE event = 'channel.subscribers'
)
DELETE FROM public.alert_observations AS observation USING repeated
WHERE observation.id = repeated.id AND repeated.occurrence > 1;

UPDATE public.alert_observations
SET source_key = channel_id || ':' || extract(epoch FROM observed_at)::text
WHERE event = 'channel.subscribers';

-- Keep every existing inbox entry and read flag, even if a replay already
-- produced two messages. The longest-lived entry guards retries until all
-- evidence of that notification has expired.
UPDATE public.alert_inbox AS message
SET source_key = message.channel_id || ':' || extract(epoch FROM message.observed_at)::text
WHERE message.id IN (
    SELECT DISTINCT ON (rule_id, version, channel_id, observed_at) id
    FROM public.alert_inbox WHERE event = 'channel.subscribers'
    ORDER BY rule_id, version, channel_id, observed_at, expires_at DESC, id
);
