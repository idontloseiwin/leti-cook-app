import { requestJson, ExplorerApiError } from './api';
import { sourceExpired, sourceLifetime } from '../sourceLifetime';

export const researchFields = ['video.id', 'video.channel_id', 'video.type', 'video.title', 'video.published_at', 'video.duration_seconds', 'video.views'] as const;
export type ResearchField = typeof researchFields[number];
export type ResearchType = 'long' | 'short' | 'stream';
export type ResearchFilters = { folder: string; type: ResearchType; title: string; minimumViews: string; sort: ResearchField; direction: 'asc' | 'desc' };
export type ResearchCell = { value: string | null; state: 'known' | 'unknown' | 'expired' | 'unavailable'; provenance_ref: string; observed_at: string | null; expires_at: string | null };
export type ResearchPage = {
  query_version: 1; query_fingerprint: string; columns: ResearchField[];
  context: { source_id: string; evaluated_at: string; valid_before: string; consistency: 'current' };
  rows: { id: string; cells: Record<ResearchField, ResearchCell> }[];
  provenance: Record<string, { origin: string; source_paths: string[]; method: string }>;
  coverage: { status: 'complete' | 'partial' | 'unknown'; channels_in_scope: number; channels_complete: number; reasons: Record<string, number> };
  page: { has_more: boolean; next_cursor: string | null };
};

function invalid(): never { throw new Error('The catalogue response could not be verified. Refresh to try again.'); }
function record(value: unknown): Record<string, unknown> { if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid(); return value as Record<string, unknown>; }
function stamp(value: unknown): number {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value)) return invalid();
  const time = Date.parse(value);
  if (!Number.isFinite(time) || new Date(time).toISOString().slice(0, 19) !== value.slice(0, 19)) return invalid();
  return time;
}
function count(value: unknown): number { if (!Number.isSafeInteger(value) || (value as number) < 0) return invalid(); return value as number; }
export function uint(value: unknown): string {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(value) || BigInt(value) > 18446744073709551615n) return invalid();
  return value;
}
export function researchQuery(filters: ResearchFilters, after?: string) {
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(filters.folder)
      || !['long', 'short', 'stream'].includes(filters.type) || !researchFields.includes(filters.sort)
      || !['asc', 'desc'].includes(filters.direction) || [...filters.title].length > 1000) return invalid();
  const conditions: { field: string; op: string; value: string }[] = [];
  if (filters.title) conditions.push({ field: 'video.title', op: 'contains_casefold_v1', value: filters.title });
  if (filters.minimumViews) conditions.push({ field: 'video.views', op: 'gte', value: uint(filters.minimumViews) });
  return { query_version: 1, source: { id: 'public.videos.current.v1' },
    scope: { kind: 'folder', id: filters.folder, include_descendants: false }, video_types: [filters.type],
    select: [...researchFields], where: conditions.length ? { all: conditions } : null,
    order_by: [{ field: filters.sort, direction: filters.direction, nulls: 'last' }],
    consistency: 'current', page: { size: 25, ...(after ? { after } : {}) } };
}

export function parseResearchPage(value: unknown, type: ResearchType): ResearchPage {
  const root = record(value), context = record(root.context), coverage = record(root.coverage), page = record(root.page), provenance = record(root.provenance);
  if (root.query_version !== 1 || typeof root.query_fingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(root.query_fingerprint)
      || JSON.stringify(root.columns) !== JSON.stringify(researchFields) || context.source_id !== 'public.videos.current.v1'
      || context.consistency !== 'current' || !Array.isArray(root.rows) || root.rows.length > 25) return invalid();
  const readAt = stamp(context.evaluated_at), deadline = stamp(context.valid_before);
  if (deadline <= readAt || !sourceLifetime(value) || sourceExpired(value)
      || stamp(root.source_expires_at) !== deadline || stamp(root.source_read_at) < readAt) return invalid();
  if (!['complete', 'partial', 'unknown'].includes(String(coverage.status))
      || count(coverage.channels_complete) > count(coverage.channels_in_scope)) return invalid();
  for (const number of Object.values(record(coverage.reasons))) count(number);
  if (typeof page.has_more !== 'boolean' || (page.has_more
    ? typeof page.next_cursor !== 'string' || page.next_cursor.length < 1 || page.next_cursor.length > 16384
    : page.next_cursor !== null)) return invalid();
  const seen = new Set<string>();
  for (const item of root.rows) {
    const row = record(item), cells = record(row.cells);
    if (typeof row.id !== 'string' || !/^[\w-]{11}$/.test(row.id) || seen.has(row.id)
        || Object.keys(cells).length !== researchFields.length) return invalid();
    seen.add(row.id);
    for (const field of researchFields) {
      const cell = record(cells[field]), origin = record(provenance[String(cell.provenance_ref)]);
      if (!['youtube_reported', 'normalized', 'operational'].includes(String(origin.origin))
          || !Array.isArray(origin.source_paths) || origin.source_paths.length > 8
          || origin.source_paths.some(path => typeof path !== 'string') || typeof origin.method !== 'string'
          || !['known', 'unknown', 'expired', 'unavailable'].includes(String(cell.state))) return invalid();
      if (cell.state !== 'known') {
        if (cell.value !== null || ['video.id', 'video.channel_id', 'video.type'].includes(field)) return invalid();
        continue;
      }
      if (typeof cell.value !== 'string' || stamp(cell.observed_at) > readAt || stamp(cell.expires_at) < deadline) return invalid();
      if (field === 'video.id' && cell.value !== row.id || field === 'video.type' && cell.value !== type
          || field === 'video.channel_id' && !/^UC[\w-]{22}$/.test(cell.value)
          || field === 'video.title' && [...cell.value].length > 1000) return invalid();
      if (field === 'video.views' || field === 'video.duration_seconds') uint(cell.value);
      if (field === 'video.published_at') stamp(cell.value);
    }
  }
  return value as ResearchPage;
}

export async function loadResearchCapabilities(signal: AbortSignal): Promise<ResearchType[]> {
  const root = record(await requestJson('/research/capabilities', signal));
  if (root.query_version !== 1 || root.source_id !== 'public.videos.current.v1' || root.max_video_types !== 1
      || !Array.isArray(root.video_types) || !root.video_types.length
      || root.video_types.some(type => !['long', 'short', 'stream'].includes(type)) || !Array.isArray(root.fields)) return invalid();
  const fields = root.fields.map(record);
  if (researchFields.some(id => !fields.some(field => field.id === id && field.sortable === true))) return invalid();
  return root.video_types as ResearchType[];
}
function preparationDelay(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 2000);
    signal.addEventListener('abort', abort, { once: true });
  });
}
export async function loadResearchPage(filters: ResearchFilters, signal: AbortSignal, after?: string, onPreparing = () => {}): Promise<ResearchPage> {
  const body = JSON.stringify(researchQuery(filters, after));
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    try {
      return parseResearchPage(await requestJson('/research/queries/execute', signal, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
      }), filters.type);
    } catch (error) {
      // Only this read-only, explicitly unexecuted request can retry. Never
      // retry access errors, query timeouts, writes, or an unknown response.
      if (!(error instanceof ExplorerApiError) || error.code !== 'catalogue_preparing' || attempt >= 2) throw error;
      onPreparing();
      await preparationDelay(signal);
    }
  }
}
export function researchError(error: unknown): string {
  if (error instanceof ExplorerApiError) {
    if (error.code === 'policy_denied') return 'This preview is not enabled, or your access has ended.';
    if (error.code === 'cursor_expired' || error.code === 'cursor_context_changed') return 'The page has expired or its source changed. Refresh from the first page.';
    if (error.code === 'source_unavailable') return 'This folder is no longer available. Choose a folder and refresh.';
    if (error.code === 'catalogue_preparing') return 'The catalogue is still being prepared in the background. Try Refresh shortly.';
    if (error.code === 'query_busy') return 'The catalogue is busy. Try Refresh shortly.';
    if (error.code === 'query_budget_exceeded') return 'The query could not finish within its limits. Narrow the selection and try again.';
  }
  return 'The catalogue could not be loaded. Check access and connection, then refresh.';
}
