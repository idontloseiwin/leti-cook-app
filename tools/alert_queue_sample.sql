-- One read-only operator sample. Both counters include all writers and
-- removals; compare samples from the same statistics lifetime.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '2s';
SELECT statement_timestamp() AS sampled_at,
       pg_postmaster_start_time() AS postgres_started_at,
       db.stats_reset AS database_stats_reset_at,
       q.relid AS queue_relation, n.relid AS inbox_relation,
       current_setting('track_counts')::boolean AS counts_enabled,
       q.n_tup_ins AS queue_inserts, q.n_tup_del AS queue_removals,
       n.n_tup_ins AS inbox_inserts, q.n_live_tup AS pending_estimate,
       pg_total_relation_size(q.relid) AS queue_bytes,
       extract(epoch FROM statement_timestamp() - (
           SELECT enqueued_at FROM public.alert_observations
           WHERE enqueued_at IS NOT NULL ORDER BY enqueued_at LIMIT 1
       )) AS oldest_known_wait_seconds,
       EXISTS (SELECT FROM public.alert_observations WHERE enqueued_at IS NULL)
           AS contains_jobs_with_unknown_wait
FROM pg_stat_user_tables q CROSS JOIN pg_stat_user_tables n
JOIN pg_stat_database db ON db.datname=current_database()
WHERE q.relid='public.alert_observations'::regclass
  AND n.relid='public.alert_inbox'::regclass;
COMMIT;
