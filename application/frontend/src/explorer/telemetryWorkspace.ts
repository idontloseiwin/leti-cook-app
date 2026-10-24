import { eventIdentity, eventTimestamp, mergeTelemetryEvents, normalizeTelemetryText } from '../telemetryMapping';
import type { TelemetryEvent } from '../types';
import { formatExactLocal } from './localTime';
import { captureSourceLifetime, transferSourceLifetime } from '../sourceLifetime';
import { collectionReasonText, parseCollectionExplanation } from '../collectionExplanation';
import type { CollectionExplanation } from '../collectionExplanation';

export const TELEMETRY_VIEW_LIMIT = 240;
export const TELEMETRY_EVENT_BYTES = 16384; // Maximum configurable producer envelope, not an unbounded JSON payload.
const textFields = ['id', 'ts', 'kind', 'source', 'target', 'operation', 'phase', 'direction', 'status', 'detail'] as const;
const numberFields = ['count', 'quota', 'duration_ms', 'http_status'] as const;
const traceText = ['trace_id', 'span_id', 'parent_span_id', 'operation_id', 'video_id', 'channel_id', 'batch_id', 'request_attempt_id', 'retry_of_request_attempt_id', 'reason', 'due_at', 'observed_at', 'quota_bucket', 'video_type', 'endpoint', 'writer_id', 'error_code', 'error_type'] as const;
const traceNumber = ['sync_run_id', 'requested_count', 'received_count', 'missing_count', 'attempt', 'inflight', 'item_index', 'sequence', 'dropped_total', 'invalid_total', 'sink_errors_total', 'monotonic_ns', 'retry_after_seconds'] as const;
const allowed = new Set<string>(['schema_version', ...textFields, ...numberFields, ...traceText, ...traceNumber, 'job_id', 'video_ids']);
const required = ['id', 'ts', 'kind', 'source', 'target', 'operation'] as const;

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Accept bounded legacy and correlated envelopes. Unknown kinds retain their literal labels. */
export function parseTelemetryEvent(value: unknown): TelemetryEvent | null {
  if (!record(value) || ![1, 2].includes(value.schema_version as number) || Object.keys(value).some((key) => !allowed.has(key))) return null;
  if (required.some((key) => typeof value[key] !== 'string' || !(value[key] as string).trim())) return null;
  for (const key of textFields) {
    const text = value[key];
    if (text === undefined) continue;
    if (typeof text !== 'string' || [...text].length > (key === 'detail' ? 640 : 160) || /[\r\n\t]/.test(text)) return null;
  }
  if (!Number.isFinite(Date.parse(value.ts as string)) || !/(Z|[+-]\d{2}:\d{2})$/i.test(value.ts as string)) return null;
  for (const key of numberFields) {
    const number = value[key];
    if (number === undefined) continue;
    if (typeof number !== 'number' || !Number.isFinite(number) || Math.abs(number) > 1e15) return null;
    if (key === 'http_status' && (!Number.isInteger(number) || number < 100 || number > 599)) return null;
  }
  for (const key of traceText) {
    const text = value[key];
    if (text === undefined) continue;
    if (typeof text !== 'string' || !text || text.length > 96 || /[\r\n\t]/.test(text)) return null;
    if (['trace_id', 'span_id', 'parent_span_id', 'writer_id'].includes(key) && !/^[a-f0-9]{32}$/.test(text)) return null;
    if (key === 'video_id' && !/^[A-Za-z0-9_-]{11}$/.test(text)) return null;
    if (key === 'channel_id' && !/^UC[A-Za-z0-9_-]{22}$/.test(text)) return null;
    if (['due_at', 'observed_at'].includes(key) && (!Number.isFinite(Date.parse(text)) || !/(Z|[+-]\d{2}:\d{2})$/i.test(text))) return null;
  }
  for (const key of traceNumber) {
    const number = value[key];
    const maximum = key === 'retry_after_seconds' ? Number.MAX_SAFE_INTEGER : 2 ** 63 - 1;
    if (number !== undefined && (typeof number !== 'number' || !Number.isInteger(number) || number < 0 || number > maximum)) return null;
  }
  // Monotonic nanoseconds are diagnostic only; the UI never computes exact durations from them.
  if (value.job_id !== undefined && !(typeof value.job_id === 'number' && Number.isInteger(value.job_id) && value.job_id >= 0)
      && !(typeof value.job_id === 'string' && /^(?:[0-9]{1,18}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})$/.test(value.job_id))) return null;
  if (value.video_ids !== undefined && (!Array.isArray(value.video_ids) || value.video_ids.length > 50
      || value.video_ids.some((item: unknown) => typeof item !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(item)))) return null;
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > TELEMETRY_EVENT_BYTES) return null;
  return value as TelemetryEvent;
}

export type TelemetryRecentSnapshot = { events: TelemetryEvent[]; rejected: number };
export function parseTelemetryRecent(value: unknown): TelemetryRecentSnapshot {
  if (!record(value) || value.status === 'unavailable' || !Array.isArray(value.events) || value.events.length > TELEMETRY_VIEW_LIMIT) {
    throw new Error('Telemetry recent response unavailable');
  }
  const events: TelemetryEvent[] = [];
  let rejected = 0;
  for (const item of value.events) {
    const event = parseTelemetryEvent(item);
    if (event) events.push(event); else rejected += 1;
  }
  return { events: mergeTelemetryEvents([], events, TELEMETRY_VIEW_LIMIT), rejected };
}

async function read(path: string, signal: AbortSignal): Promise<unknown> {
  const requestStarted = performance.now();
  const response = await fetch(path, { signal, cache: 'no-store', headers: { Accept: 'application/json' } });
  if (!response.ok || response.headers.get('X-Telemetry-Status') === 'unavailable') throw new Error('Telemetry read unavailable');
  return captureSourceLifetime(await response.json(), response.headers, requestStarted);
}
export async function loadTelemetryRecent(signal: AbortSignal) {
  return parseTelemetryRecent(await read(`/telemetry/recent?limit=${TELEMETRY_VIEW_LIMIT}`, signal));
}

export type TelemetryCollectionOutcome = Partial<Record<
  'claimed' | 'updated' | 'unavailable_marked' | 'metadata_deferred' | 'unresolved' | 'publication_waiting' | 'stream_lifecycle_probed', number>> & {
  completion_outcome?: 'completed' | 'partial' | 'error'; incomplete?: boolean; yield_requested?: boolean;
};
// Sequential cycle phases. regular_video_stats_seconds is an alias of the
// common video phase and must not appear as another cost.
export const telemetryRunPhases = [
  ['preflight_seconds', 'Preflight'], ['lifecycle_recovery_seconds', 'Recovery'],
  ['video_collection_seconds', 'Video collection'], ['channel_metadata_seconds', 'Channel profiles'],
  ['typed_inventory_seconds', 'Playlist inventory'],
] as const;
type RunPhaseTimings = Partial<Record<typeof telemetryRunPhases[number][0], number>>;
export type TelemetryRun = {
  id?: string; status?: string; current_phase?: string; phase_state?: string;
  started_at?: string; finished_at?: string; progress_updated_at?: string;
  duration_seconds?: number; stats_updated?: number; new_videos?: number; errors?: number;
  collection?: TelemetryCollectionOutcome; yield_requested?: boolean;
  explanation?: CollectionExplanation;
  timings?: RunPhaseTimings;
};
export type TelemetrySystemSummary = { now: string; latest_run: TelemetryRun | null; recent_runs: TelemetryRun[] };
/** Validate only the owned, bounded run projection; unrelated summary fields remain additive. */
export function parseTelemetrySystemSummary(value: unknown): TelemetrySystemSummary {
  const fail = (): never => { throw new Error('Invalid run history response'); };
  const date = (value: unknown): string => {
    if (typeof value !== 'string' || value.length > 160 || !/(Z|[+-]\d{2}:\d{2})$/i.test(value) || !Number.isFinite(Date.parse(value))) return fail();
    return new Date(value).toISOString();
  };
  const run = (value: unknown): TelemetryRun => {
    if (!record(value)) return fail();
    const result: TelemetryRun = {};
    if (value.id != null) {
      if (typeof value.id === 'number' && Number.isSafeInteger(value.id) && value.id > 0) result.id = String(value.id);
      else if (typeof value.id === 'string' && telemetrySummaryText(value.id)) result.id = value.id;
      else return fail();
    }
    if (value.summary_json != null && !record(value.summary_json)) return fail();
    const summary = record(value.summary_json) ? value.summary_json : {};
    // Current sync_runs stores progress in summary_json; retain older top-level compatibility.
    const phase = (key: string) => Object.hasOwn(summary, key) ? summary[key] : value[key];
    for (const key of ['status', 'current_phase', 'phase_state'] as const) {
      const field = key === 'status' ? value[key] : phase(key);
      if (field == null) continue;
      if (!telemetrySummaryText(field)) return fail();
      result[key] = field as string;
    }
    for (const key of ['started_at', 'finished_at', 'progress_updated_at'] as const) {
      const field = key === 'progress_updated_at' ? phase(key) : value[key];
      if (field != null) result[key] = date(field);
    }
    for (const key of ['duration_seconds', 'stats_updated', 'new_videos', 'errors'] as const) {
      if (value[key] == null) continue;
      if (typeof value[key] !== 'number' || !Number.isFinite(value[key]) || value[key] < 0 || value[key] > 1e15) return fail();
      result[key] = value[key];
    }
    if (summary.timings != null) {
      if (!record(summary.timings)) return fail();
      const timings: RunPhaseTimings = {};
      for (const [key] of telemetryRunPhases) {
        const seconds = summary.timings[key];
        if (seconds == null) continue;
        if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0 || seconds > 1e15) return fail();
        timings[key] = seconds;
      }
      result.timings = timings;
    }
    if (summary.yield_requested != null) {
      if (typeof summary.yield_requested !== 'boolean') return fail();
      result.yield_requested = summary.yield_requested;
    }
    if (summary.video_collection != null) {
      if (!record(summary.video_collection)) return fail();
      const collection: TelemetryCollectionOutcome = {};
      for (const key of ['claimed', 'updated', 'unavailable_marked', 'metadata_deferred', 'unresolved', 'publication_waiting', 'stream_lifecycle_probed'] as const) {
        const field = summary.video_collection[key];
        if (field == null) continue;
        if (typeof field !== 'number' || !Number.isSafeInteger(field) || field < 0) return fail();
        collection[key] = field;
      }
      for (const key of ['incomplete', 'yield_requested'] as const) {
        const field = summary.video_collection[key];
        if (field == null) continue;
        if (typeof field !== 'boolean') return fail();
        collection[key] = field;
      }
      const outcome = summary.video_collection.completion_outcome;
      if (outcome != null) {
        if (outcome !== 'completed' && outcome !== 'partial' && outcome !== 'error') return fail();
        collection.completion_outcome = outcome;
      }
      result.collection = collection;
    }
    if (value.explanation != null) {
      const explanation = parseCollectionExplanation(value.explanation);
      if (!explanation) return fail();
      result.explanation = explanation;
    }
    return result;
  };
  if (!record(value) || !Array.isArray(value.recent_runs) || value.recent_runs.length > 10
    || (value.recent_runs_read_status !== undefined && value.recent_runs_read_status !== 'ok')) return fail();
  return transferSourceLifetime(value, {
    now: date(value.now), latest_run: value.latest_run === null ? null : run(value.latest_run), recent_runs: value.recent_runs.map(run),
  });
}
export async function loadTelemetrySystem(signal: AbortSignal) {
  return parseTelemetrySystemSummary(await read('/system/run-history', signal));
}

export function telemetryFailed(event: TelemetryEvent): boolean {
  const status = normalizeTelemetryText(event.status);
  // Admission refusals are normal bounded work; an unknown ACK is not a known rollback.
  return ['error', 'failed'].includes(status)
    || (event.operation === 'video_write' && status === 'rejected')
    || (['video_provider_manifest', 'video_provider_item'].includes(event.operation ?? '') && status === 'invalid');
}

/** Counter names belong to each producer boundary, not to the envelope field name. */
export function telemetryCountLabels(event: TelemetryEvent): { requested: string; received: string; count?: string } {
  switch (event.operation) {
    case 'collection_phase': return { requested: 'Requested count', received: 'Received count',
      count: event.phase === 'video_collection' ? 'Confirmed video updates'
        : event.phase === 'channel_metadata' ? 'Processed channels'
        : event.phase === 'typed_inventory' ? 'Playlist page attempts' : 'Count' };
    case 'video_provider_manifest':
    case 'video_general_observation':
    case 'video_stats_observation': return { requested: 'Requested IDs', received: 'Returned IDs' };
    case 'video_batch': return { requested: 'Selected videos', received: 'Confirmed updates' };
    case 'common_pass': return { requested: 'Claimed videos', received: 'Confirmed updates' };
    case 'video_claim': return { requested: 'Requested IDs', received: 'Claimed videos' };
    case 'common_claim': return { requested: 'Claim limit', received: 'Claimed videos' };
    case 'inventory_write': return { requested: 'Submitted page items', received: 'Acknowledged page items' };
    case 'video_provider_selection': return { requested: 'Selected IDs', received: 'Received count' };
    case 'video_batch_manifest': return { requested: 'Claimed IDs', received: 'Received count' };
    default: return { requested: 'Requested count', received: 'Received count' };
  }
}

/** Interpret only explicit observations, never infer why YouTube omitted an ID. */
export function telemetryVideoObservation(event: TelemetryEvent): string | undefined {
  if (event.operation === 'video_provider_item' && event.status === 'observed' && event.reason === 'id_omitted') {
    if (event.endpoint === 'videos:batchGetStats') return 'This video was not returned in the statistics response. This alone does not establish availability; a metadata check is needed.';
    if (event.endpoint === 'videos') return 'This video was not returned in the metadata response. The API did not report a reason for its absence.';
  }
  if (event.operation !== 'video_write') return undefined;
  if (event.status === 'unknown') return 'The write was not acknowledged. Its result is unknown; this event does not confirm a saved change.';
  if (event.status !== 'confirmed') return undefined;
  if (event.reason === 'youtube_stats_counters_unavailable') return 'The need for a metadata check was saved. The retry delay is a requested minimum, not a promised check time. Search the same Video ID for later retained observations.';
  if (event.reason === 'unavailable') return 'The API absence was saved as unavailable. This does not identify deletion, privacy or another cause, and does not establish current availability.';
  return undefined;
}

/** Existing Stream count is a subset of the all-type publication count, never an extra total. */
export function telemetryWaitingPublications(collection?: TelemetryCollectionOutcome): number | undefined {
  if (collection?.publication_waiting === undefined && collection?.stream_lifecycle_probed === undefined) return undefined;
  return Math.max(collection?.publication_waiting ?? 0, collection?.stream_lifecycle_probed ?? 0);
}

export function telemetryRunExplanation(run: TelemetryRun): string | undefined {
  if (run.explanation?.reasons.length) {
    return [...run.explanation.reasons.map(collectionReasonText),
      'These observations describe this run. Current queue state determines the next check; later recovery is not assessed here.'].join(' ');
  }
  if (run.status === 'error') return 'One or more operations failed; confirmed changes are saved. Check the recorded errors and affected phase. This status does not by itself mean the server stopped.';
  if (run.status !== 'partial') return undefined;
  const collection = run.collection;
  const notes = ['Selected work was left unfinished; confirmed changes are saved.'];
  if ((collection?.metadata_deferred ?? 0) > 0) {
    const count = collection!.metadata_deferred!;
    notes.push(`${count.toLocaleString('en')} video${count === 1 ? '' : 's'} recorded as needing additional metadata checks. A missing counter alone does not confirm that a video is unavailable.`);
  }
  if ((collection?.unresolved ?? 0) > 0) {
    notes.push(`Unresolved items in video collection: ${collection!.unresolved!.toLocaleString('en')}.`);
  }
  if (run.yield_requested || collection?.yield_requested) notes.push('Work yielded to a later pass.');
  if (notes.length === 1) notes.push('A more specific reason was not recorded in this summary.');
  notes.push('These counts describe this run, not the current queue.');
  return notes.join(' ');
}

/** The legacy SystemSummary type is permissive; never render arbitrary nested values as React children. */
export function telemetrySummaryText(value: unknown): string | undefined {
  return typeof value === 'string' && value.length <= 160 && value.trim() ? value : undefined;
}
export function telemetryRunContext(value: unknown) {
  const summary = record(value) ? value : {};
  const run = record(summary.latest_run) ? summary.latest_run : {};
  return {
    status: telemetrySummaryText(run.status), phase: telemetrySummaryText(run.current_phase) || telemetrySummaryText(run.phase_state),
    startedAt: telemetrySummaryText(run.started_at), progressAt: telemetrySummaryText(run.progress_updated_at),
    finishedAt: telemetrySummaryText(run.finished_at), summaryAt: telemetrySummaryText(summary.now),
  };
}
const sourceLabels: Record<string, string> = {
  youtube_api: 'YouTube API', db_selectors: 'Database selectors', db_tables: 'Database tables',
  backend: 'Backend', ui: 'Browser', auto_sync: 'Auto-sync', sync_service: 'Sync service',
  sync_engine: 'Sync engine', supabase: 'Database API', video_collection_repository: 'Video collection',
  typed_inventory_repository: 'Typed inventory', channel_discovery_repository: 'Channel discovery',
  channel_sync_repository: 'Channel metadata',
};
export function telemetrySourceLabel(source: string): string { return Object.hasOwn(sourceLabels, source) ? sourceLabels[source] : source; }
export function telemetryDate(value?: string): string {
  return value ? formatExactLocal(value).slice(0, 10) : 'Not recorded';
}
export function telemetryRunDuration(seconds?: number): string {
  if (seconds === undefined) return 'Not recorded';
  if (seconds < 60) return `${Number(seconds.toFixed(1))}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
}
// Telemetry times are local like every other moment on the page.
export function telemetryExact(value?: string | null): string {
  return formatExactLocal(value);
}
export function telemetryTime(value?: string): string {
  return value ? formatExactLocal(value).slice(11) : 'Not recorded';
}
export function telemetryDuration(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? `${value.toLocaleString('en', { maximumFractionDigits: 2 })} ms` : 'Not recorded';
}

export type TelemetryFlow = { key: string; source: string; target: string; count: number; errors: number; latestAt: string };
export function telemetryFlows(events: TelemetryEvent[]): TelemetryFlow[] {
  const flows = new Map<string, TelemetryFlow>();
  for (const event of events) {
    const source = event.source!;
    const target = event.target!;
    const key = JSON.stringify([source, target]);
    const row = flows.get(key) ?? { key, source, target, count: 0, errors: 0, latestAt: event.ts! };
    row.count += 1;
    row.errors += Number(telemetryFailed(event));
    if (eventTimestamp(event) > Date.parse(row.latestAt)) row.latestAt = event.ts!;
    flows.set(key, row);
  }
  return [...flows.values()].sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

export type TelemetryFilter = { query: string; kind: string; failedOnly: boolean; flowKey?: string };
export function filterTelemetryEvents(events: TelemetryEvent[], filter: TelemetryFilter): TelemetryEvent[] {
  const query = filter.query.toLowerCase().trim();
  return events.filter((event) => (!filter.kind || event.kind === filter.kind)
    && (!filter.failedOnly || telemetryFailed(event))
    && (!filter.flowKey || JSON.stringify([event.source, event.target]) === filter.flowKey)
    && (!query || [event.operation, event.source, event.target, event.phase, event.status, event.detail,
      event.reason, event.video_id, event.channel_id, event.batch_id, event.sync_run_id?.toString(),
      ...(Array.isArray(event.video_ids) ? event.video_ids : [])].some((field) => typeof field === 'string' && field.toLowerCase().includes(query))));
}

/** Same dedupe/order owner as legacy mapping, with one bounded visible buffer. */
export function appendTelemetryEvents(current: TelemetryEvent[], incoming: TelemetryEvent[]) {
  return mergeTelemetryEvents(current, incoming, TELEMETRY_VIEW_LIMIT);
}
export { eventIdentity, eventTimestamp };
