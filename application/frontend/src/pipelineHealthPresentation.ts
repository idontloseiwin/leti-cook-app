import type { PipelineHealthReport, PipelineHealthQueue } from './types';
import { parseCollectionExplanation } from './collectionExplanation';

export type HealthTone = 'ok' | 'warning' | 'critical' | 'unknown';
export type HealthSource = 'stats' | 'discovery' | 'metadata' | 'channels' | 'sync' | 'quota';

// Unknown is not zero. Health values are aggregate counters, never booleans.
export function healthNumber(value: unknown): number | null {
  if (typeof value === 'string' && value.trim()) value = Number(value);
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function healthCount(value: unknown): string {
  const number = healthNumber(value);
  return number === null ? '—' : new Intl.NumberFormat('en').format(number);
}

export function healthSourceError(report: PipelineHealthReport | undefined, source: HealthSource): string | undefined {
  const components: Record<HealthSource, string[]> = {
    stats: ['stats_queue'],
    discovery: report?.runtime?.database_channel_discovery_enabled === true
      ? ['channel_discovery_queue'] : ['channels', 'channel_discovery_queue'],
    metadata: ['channels'], channels: ['channels'], sync: ['sync_runs'],
    quota: ['youtube_api_request_attempts'],
  };
  const error = report?.collection_errors?.find((item) => components[source].includes(item.component ?? ''));
  if (!error) return undefined;
  // Only expose the collector's bounded class name, never a provider/DB body.
  const type = /^[a-zA-Z][a-zA-Z0-9_]{0,79}$/.test(error.error_type ?? '') ? error.error_type : 'Read failed';
  return `${error.component}: ${type}`;
}

export function healthSnapshotStale(report: PipelineHealthReport | undefined, readError: string | undefined, now = Date.now()): boolean {
  if (!report) return false;
  if (readError) return true;
  const generated = Date.parse(report.generated_at ?? '');
  // Three missed 30-second reads: a UI freshness marker, not a worker alarm.
  return !Number.isFinite(generated) || generated > now + 60_000 || now - generated > 90_000;
}

export function healthAssessment(report: PipelineHealthReport | undefined, stale: boolean): HealthTone {
  if (!report || stale) return 'unknown';
  if (report.status === 'critical') return 'critical';
  if (report.status === 'warning' || (report.collection_errors?.length ?? 0) > 0) return 'warning';
  return report.status === 'ok' ? 'ok' : 'unknown';
}

export function healthQueueState(queue: PipelineHealthQueue | undefined, kind: 'stats' | 'discovery' | 'metadata', error?: string, stale = false): { tone: HealthTone; label: string } {
  if (error) return { tone: 'unknown', label: 'Unavailable' };
  const due = healthNumber(kind === 'stats' ? queue?.all_due : queue?.due);
  const leased = healthNumber(kind === 'stats' ? queue?.leased : queue?.running);
  if (stale) return { tone: 'unknown', label: 'Last reading' };
  if (due === null) return { tone: 'unknown', label: 'Unavailable' };
  if ((healthNumber(queue?.expired_leases) ?? 0) > 0) return { tone: 'warning', label: `${healthCount(queue?.expired_leases)} expired leases` };
  if ((healthNumber(queue?.failed) ?? 0) > 0) return { tone: 'warning', label: `${healthCount(queue?.failed)} failed` };
  if (due > 0) return { tone: 'warning', label: `${healthCount(due)} due` };
  if (leased === null) return { tone: 'unknown', label: '0 due · leases unknown' };
  return { tone: 'ok', label: leased > 0 ? `${healthCount(leased)} leased` : 'No work due' };
}

export function healthCountTone(value: unknown, stale = false): HealthTone {
  const count = healthNumber(value);
  return stale || count === null ? 'unknown' : count > 0 ? 'warning' : 'ok';
}

// This legacy endpoint is additive rather than versioned. Validate only the
// fields we render, allow omitted sections, and reject malformed values instead
// of claiming an all-clear from an invalid response.
export function parsePipelineHealth(value: unknown): PipelineHealthReport {
  const fail = (): never => { throw new Error('Invalid pipeline health response'); };
  const record = (input: unknown): Record<string, unknown> => {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return fail();
    return input as Record<string, unknown>;
  };
  const numericFields = (input: Record<string, unknown>, fields: string[]) => {
    for (const field of fields) {
      if (input[field] !== undefined && input[field] !== null && healthNumber(input[field]) === null) fail();
    }
  };
  const counts = (input: unknown) => {
    if (input === undefined) return;
    for (const [key, count] of Object.entries(record(input))) {
      if (key.length > 200 || typeof count !== 'number' || healthNumber(count) === null) fail();
    }
  };
  const report = record(value);
  if (typeof report.status !== 'string' || report.status.length > 40) fail();
  if (report.generated_at !== undefined && (typeof report.generated_at !== 'string' || !Number.isFinite(Date.parse(report.generated_at)))) fail();
  numericFields(report, ['window_hours']);
  for (const field of ['warnings', 'critical']) {
    const list = report[field];
    if (list !== undefined && (!Array.isArray(list) || list.some((item) => typeof item !== 'string' || item.length > 1000))) fail();
  }
  if (report.collection_errors !== undefined) {
    if (!Array.isArray(report.collection_errors)) fail();
    for (const item of report.collection_errors as unknown[]) {
      const error = record(item);
      for (const field of ['component', 'error_type']) {
        if (error[field] !== undefined && (typeof error[field] !== 'string' || !/^[a-zA-Z][a-zA-Z0-9_]{0,79}$/.test(error[field] as string))) fail();
      }
    }
  }
  if (report.runtime !== undefined) {
    const runtime = record(report.runtime);
    for (const field of ['batch_stats_enabled', 'database_video_collection_enabled', 'database_channel_discovery_enabled']) {
      if (runtime[field] !== undefined && typeof runtime[field] !== 'boolean') fail();
    }
  }
  if (report.channels !== undefined) numericFields(record(report.channels), ['total', 'visible']);
  if (report.queues !== undefined) {
    const queues = record(report.queues);
    for (const field of ['stats', 'channel_discovery', 'channel_metadata']) {
      if (queues[field] === undefined) continue;
      const queue = record(queues[field]);
      const queueFields = ['all_due', 'total', 'total_channels', 'due', 'null_schedule', 'running', 'leased', 'expired_leases', 'paused', 'hydration_due', 'metadata_due', 'counter_due', 'failed', 'excluded_failed', 'runtime_disabled', 'playlist_disabled', 'initial_owned', 'playlist_not_found', 'policy_paused', 'oldest_overdue_minutes', 'oldest_running_minutes', 'active_resume_states', 'oldest_resume_age_minutes'];
      numericFields(queue, queueFields);
      if (queue.by_type !== undefined) {
        const buckets = record(queue.by_type);
        for (const [kind, bucket] of Object.entries(buckets)) {
          if (!['long', 'short', 'stream', 'unknown'].includes(kind)) fail();
          numericFields(record(bucket), queueFields);
        }
        if (queue.scope !== 'all_types' || !['videos', 'playlists'].includes(queue.unit as string)) fail();
        if (['long', 'short', 'stream'].some((kind) => !buckets[kind])) fail();
      }
      counts(queue.status_counts); counts(queue.streak_buckets);
    }
  }
  for (const field of ['latest_sync', 'last_successful_sync', 'recent_syncs']) {
    if (report[field] === undefined || report[field] === null) continue;
    const sync = record(report[field]);
    numericFields(sync, ['count', 'error_total', 'age_minutes', 'errors', 'metadata_processed', 'metadata_updated', 'discovery_processed', 'new_videos', 'stats_updated', 'regular_stats_requested', 'unavailable_marked', 'missing']);
    counts(sync.status_counts);
    if (field === 'recent_syncs') {
      if (sync.duration_seconds !== undefined) numericFields(record(sync.duration_seconds), ['median', 'max']);
    } else {
      numericFields(sync, ['duration_seconds']);
      if (sync.explanation !== undefined && !parseCollectionExplanation(sync.explanation)) fail();
      for (const time of ['started_at', 'finished_at']) {
        if (sync[time] !== undefined && sync[time] !== null && (typeof sync[time] !== 'string' || !Number.isFinite(Date.parse(sync[time] as string)))) fail();
      }
      if (sync.status !== undefined && (typeof sync.status !== 'string' || sync.status.length > 40)) fail();
    }
  }
  if (report.history !== undefined) {
    const history = record(report.history);
    numericFields(history, ['sync_errors', 'transport_errors']);
    if (history.recovery !== 'not_correlated' || !['ok', 'partial', 'unavailable'].includes(history.transport_read_status as string)) fail();
    if (typeof history.quota_day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(history.quota_day)) fail();
  }
  if (report.youtube_quota !== undefined) {
    const quota = record(report.youtube_quota);
    if (quota.read_status !== undefined && !['ok', 'partial', 'unavailable'].includes(quota.read_status as string)) fail();
    numericFields(quota, ['attempts', 'minutes_until_reset']);
    for (const field of ['status_counts', 'endpoint_attempt_counts', 'bucket_units', 'configured_bucket_limits']) counts(quota[field]);
    if (quota.estimated_bucket_remaining !== undefined) numericFields(record(quota.estimated_bucket_remaining), ['general', 'video_batch_get_stats']);
    if (quota.unfinished !== undefined) numericFields(record(quota.unfinished), ['count', 'stale_count']);
    if (quota.quota_day !== undefined && (typeof quota.quota_day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(quota.quota_day))) fail();
  }
  return value as PipelineHealthReport;
}
