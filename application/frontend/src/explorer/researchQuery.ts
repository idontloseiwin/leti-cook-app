import { requestJson, ExplorerApiError } from './api';
import { sourceExpired, sourceLifetime } from '../sourceLifetime';

// Labels are presentation only. Supported fields/operators come from the server.
export const researchLabels = {
  'observation.views_delta': 'Observed view change', 'observation.views_per_hour': 'Average views/hour over measured interval',
  'observation.views_at_age': 'Views observed near age 24 hours',
  // 28-day growth (video.observed_growth_28d.v1): Library's sort and filters; one evidence object per row.
  'observation.growth_28d_status': 'Growth status (28 days)', 'observation.views_gained_28d': 'Views gained (28 days)',
  'observation.views_per_day_28d': 'Views per day (28 days)', 'observation.growth_28d_interval_seconds': 'Growth interval',
  'observation.growth_28d_tracked_days': 'Growth tracked days',
  'channel.id': 'Channel ID (tracked)',
  'video.id': 'Video ID', 'video.channel_id': 'Channel ID', 'video.type': 'Format',
  'video.description': 'Description', 'video.paid_promotion': 'Declared paid promotion',
  'video.title': 'Title', 'video.published_at': 'Published (UTC)', 'video.duration_seconds': 'Duration',
  'video.views': 'Views', 'video.likes': 'Likes', 'video.comments': 'Comments', 'video.tags': 'Tags',
  'video.topic_urls': 'YouTube topics', 'video.category_id': 'Category ID', 'video.audio_language': 'Audio language',
  'video.metadata_language': 'Metadata language', 'video.caption_available': 'Captions',
  'video.scheduled_start_at': 'Scheduled start (UTC)', 'video.actual_end_at': 'Stream ended (UTC)',
  'video.thumbnail_url': 'Thumbnail',
  // Calculated from the stored row (Library's Baseline calculations, the Inspector's labels) and the
  // publication date and year in the study's time zone; Home's lifetime pace.
  'video.title_length': 'Title length', 'video.tag_count': 'Tag count', 'video.like_rate': 'Like rate',
  'video.comment_rate': 'Comment rate', 'video.published_date': 'Published date (study zone)',
  'video.published_year': 'Published year (study zone)', 'video.lifetime_views_per_day': 'Views per day since publication',
  'channel.title': 'Channel', 'channel.avatar_url': 'Channel image',
  'channel.subscribers': 'Subscribers', 'channel.views': 'Channel views', 'channel.video_count': 'Channel videos',
  'channel.country_code': 'Channel country', 'channel.created_at': 'Channel created (UTC)',
} as const;
export type ResearchField = keyof typeof researchLabels;
export const researchFields = Object.keys(researchLabels) as ResearchField[];
export const videoSource = 'public.videos.current.v1' as const;
export const channelSource = 'public.channels.current.v1' as const;
export type ResearchSource = typeof videoSource | typeof channelSource;
export type ResearchType = 'long' | 'short' | 'stream';
export const researchTypeNames = { long: 'Long-form', short: 'Shorts', stream: 'Streams' };
export type ScopeKind = 'channel' | 'folder' | 'label' | 'collection';
export type ResearchScope = { kind: ScopeKind; id: string };
export type FieldDefinition = { id: ResearchField; value_type: string; sortable: boolean; operators: string[]; enabled_video_types?: ResearchType[] };
export type ResearchCapabilities = { source_id:ResearchSource; fields: FieldDefinition[]; video_types: ResearchType[]; scopes: ScopeKind[]; operations:string[];
  // Oldest video age in seconds offered per observed change window, from the collection
  // schedule: null means every age, 0 means the window is unavailable under that schedule.
  observation_age_limits:Partial<Record<ResearchType,Record<string,number|null>>>;
  // Formats the owner turned off in config.ini; empty while all are on.
  turned_off_video_types: ResearchType[] };
/** The format_turned_off refusal in words (the same words as formatsOff.ts; this module imports nothing new). */
export const FORMAT_TURNED_OFF_TEXT = 'This video format is turned off in config/config.ini (formats). '
  + 'Its stored videos are kept but not shown; add it to formats to show it again.';
export type Predicate = { field: ResearchField; op: string; value?: string | boolean | string[] };
export type Expression = Predicate | { all: Expression[] } | { any: Expression[] } | { not: Expression };
export type ResearchSort = { field: ResearchField; direction: 'asc' | 'desc' };
export type ResearchFilters = { source?:ResearchSource; scope: ResearchScope; type: ResearchType; columns: ResearchField[];
  where: Expression | null; order: ResearchSort[]; complete: boolean; requiredFields?: ResearchField[]; pageSize?: number; exactProjection?: boolean;
  observations?: { version: 1; window_seconds: 3600 | 86400; tolerance_seconds: number; age_seconds: 86400; age_tolerance_seconds: number } };
export const defaultColumns: ResearchField[] = ['video.title', 'channel.title', 'video.published_at', 'video.duration_seconds', 'video.views', 'video.likes', 'video.comments'];
export const identityFields: ResearchField[] = ['video.id', 'video.channel_id', 'video.type'];
export const requiredPresentation: ResearchField[] = [...identityFields, 'video.title', 'video.thumbnail_url'];
export type ObservationEvidence = { method: 'video.observed_change.v1' | 'video.observed_age.v1'; version: 1;
  from_at: string | null; to_at: string | null; actual_seconds: string | null; definition_epoch: string | null; reason: string };
export type GrowthEvidence = { method: 'video.observed_growth_28d.v1'; version: 1; status: typeof growthStatuses[number]; reason: string | null;
  from_at: string | null; to_at: string | null; actual_interval_seconds: string | null; tracked_days: string | null; time_zone: string };
/** Fields measured between two saved snapshots (each cell carries its own evidence). */
export const observedFields: ResearchField[] = ['observation.views_delta', 'observation.views_per_hour', 'observation.views_at_age'];
/** 28-day growth fields: one evidence object per row, on one of its cells. */
export const growthFields: ResearchField[] = ['observation.growth_28d_status', 'observation.views_gained_28d', 'observation.views_per_day_28d',
  'observation.growth_28d_interval_seconds', 'observation.growth_28d_tracked_days'];
export const growthStatuses = ['ready', 'partial', 'started_today', 'waiting_next_snapshot', 'unavailable'] as const;
export type ResearchCell = { observation?: ObservationEvidence | GrowthEvidence | null; value: string | boolean | string[] | null; state: 'known' | 'unknown' | 'expired' | 'unavailable'; provenance_ref: string; observed_at: string | null; expires_at: string | null };
export type ResearchProvenance = { origin: string; source_paths: string[]; method: string; definition_epoch?: string | null;
  type_rule?: { id: 'long_short_overlap_duration'; version: number; max_short_seconds: number } | null };
export type ResearchRow = { id: string; cells: Partial<Record<ResearchField, ResearchCell>> };
export type ResearchPage = {
  query_version: 1; query_fingerprint: string; columns: ResearchField[];
  context: { source_id: string; evaluated_at: string; valid_before: string; consistency: 'current';
    // Oldest admitted video age when an observed change window narrowed the query.
    observation_age_limit_seconds?: number | null;
    // The request's time.timezone (IANA name; UTC unless the viewer named one).
    time_zone?: string };
  rows: ResearchRow[]; provenance: Record<string, ResearchProvenance>;
  coverage: { status: 'complete' | 'partial' | 'unknown'; basis: 'channel_inventory' | 'collection_members' | 'channel_profiles'; channels_in_scope: number; channels_complete: number; reasons: Record<string, number> };
  page: { has_more: boolean; next_cursor: string | null };
};
// Wire shape of consistency 'frozen' (no consumer yet): the whole matching population is
// read once and kept by the server for at most capabilities.frozen.ttl_seconds. Pages come
// from that copy; page.after takes next_cursor or previous_cursor, page.anchor_id picks the
// first page. Extra coverage reasons: source_rows_expiring, rows_with_expired_values.
export type ResearchFrozenPage = Omit<ResearchPage, 'context' | 'page'> & {
  context: Omit<ResearchPage['context'], 'consistency' | 'time_zone'> & { consistency: 'frozen'; time_zone: string;
    // Opaque reference to the whole frozen result: POST /research/queries/frozen-ids.
    frozen_ref: string };
  page: { has_more: boolean; next_cursor: string | null; previous_cursor: string | null;
    start_index: number; total_count: number; anchor_id: string | null };
};
function invalid(): never { throw new Error('The server’s answer could not be verified. Try again.'); }
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
/** Every field an expression refers to, at any depth of AND / OR / NOT. */
export function expressionFields(expression: Expression | null | undefined): ResearchField[] {
  if (!expression) return [];
  if ('all' in expression) return expression.all.flatMap(expressionFields);
  if ('any' in expression) return expression.any.flatMap(expressionFields);
  if ('not' in expression) return expressionFields(expression.not);
  return [expression.field];
}

export function validScope(scope: ResearchScope): boolean {
  const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  const v4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  return scope.kind === 'channel' ? /^UC[\w-]{22}$/.test(scope.id)
    : scope.kind === 'folder' ? uuid.test(scope.id)
    : scope.kind === 'collection' ? v4.test(scope.id)
    : scope.kind === 'label' && (scope.id === 'monetized' || scope.id.startsWith('custom:') && v4.test(scope.id.slice(7)));
}
export function researchQuery(filters: ResearchFilters, after?: string) {
  const source=filters.source??videoSource, channels=source===channelSource;
  if (![videoSource,channelSource].includes(source) || channels && (filters.scope.kind==='collection'||filters.observations)) return invalid();
  if (!validScope(filters.scope) || !['long', 'short', 'stream'].includes(filters.type)
      || !filters.columns.length || filters.columns.some(id => !researchFields.includes(id))
      || !filters.order.length || filters.order.length > 3
      || filters.order.some(item => !researchFields.includes(item.field) || !['asc', 'desc'].includes(item.direction))
      || new Set(filters.order.map(item => item.field)).size !== filters.order.length) return invalid();
  return { query_version: 1, source: { id: source },
    scope: { ...filters.scope, ...(filters.scope.kind === 'folder' ? { include_descendants: false } : {}) }, ...(!channels?{video_types:[filters.type]}:{}),
    select: filters.exactProjection ? [...filters.columns] : [...new Set([...(channels?['channel.id' as ResearchField]:requiredPresentation), ...filters.columns])], where: filters.where,
    order_by: filters.order.map(item => ({ ...item, nulls: 'last' })), coverage: { mode: filters.complete ? 'require_complete' : 'available', ...(filters.requiredFields ? { required_fields: filters.requiredFields } : {}) },
    ...(filters.observations ? { observations: filters.observations } : {}),
    consistency: 'current', page: { size: filters.pageSize ?? 25, ...(after ? { after } : {}) } };
}

export function parseResearchPage(value: unknown, filters: ResearchFilters): ResearchPage {
  const columns = researchQuery(filters).select;
  const root = record(value), context = record(root.context), coverage = record(root.coverage), page = record(root.page), provenance = record(root.provenance);
  if (root.query_version !== 1 || typeof root.query_fingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(root.query_fingerprint)
      || JSON.stringify(root.columns) !== JSON.stringify(columns) || context.source_id !== (filters.source??videoSource)
      || context.consistency !== 'current' || !Array.isArray(root.rows) || root.rows.length > (filters.pageSize ?? 25)
      || context.observation_age_limit_seconds !== undefined && context.observation_age_limit_seconds !== null
         && !(Number.isSafeInteger(context.observation_age_limit_seconds) && Number(context.observation_age_limit_seconds) >= 0)) return invalid();
  const readAt = stamp(context.evaluated_at), deadline = stamp(context.valid_before);
  if (deadline <= readAt || !sourceLifetime(value) || sourceExpired(value)
      || stamp(root.source_expires_at) !== deadline || stamp(root.source_read_at) < readAt) return invalid();
  if (!['complete', 'partial', 'unknown'].includes(String(coverage.status))
      || coverage.basis !== (filters.source===channelSource?'channel_profiles':filters.scope.kind === 'collection' ? 'collection_members' : 'channel_inventory')
      || count(coverage.channels_complete) > count(coverage.channels_in_scope)) return invalid();
  for (const number of Object.values(record(coverage.reasons))) count(number);
  if (coverage.status === 'complete' && Object.values(record(coverage.reasons)).some(n => n !== 0)) return invalid();
  if (typeof page.has_more !== 'boolean' || (page.has_more
    ? typeof page.next_cursor !== 'string' || page.next_cursor.length < 1 || page.next_cursor.length > 16384
    : page.next_cursor !== null)) return invalid();
  const seen = new Set<string>();
  for (const item of root.rows) {
    const row = record(item), cells = record(row.cells);
    if (typeof row.id !== 'string' || !(filters.source===channelSource?/^UC[\w-]{22}$/:/^[\w-]{11}$/).test(row.id) || seen.has(row.id) || Object.keys(cells).length !== columns.length) return invalid();
    seen.add(row.id);
    for (const field of columns) {
      const cell = record(cells[field]), origin = record(provenance[String(cell.provenance_ref)]);
      if (!['youtube_reported', 'normalized', 'operational', 'calculated'].includes(String(origin.origin))
          || !Array.isArray(origin.source_paths) || origin.source_paths.length > 8
          || origin.source_paths.some(path => typeof path !== 'string') || typeof origin.method !== 'string'
          || !['known', 'unknown', 'expired', 'unavailable'].includes(String(cell.state))) return invalid();
      if (field === 'video.type') {
        if (origin.method === 'automatic_long_short_v1') {
          const rule = record(origin.type_rule);
          if (rule.id !== 'long_short_overlap_duration' || count(rule.version) < 1 || count(rule.max_short_seconds) < 1 || Number(rule.max_short_seconds) > 86400) return invalid();
        } else if (origin.type_rule !== undefined && origin.type_rule !== null) return invalid();
      }
      if (growthFields.includes(field)) {
        if (cell.observation !== undefined && cell.observation !== null) {
          const proof = record(cell.observation);
          if (proof.version !== 1 || proof.method !== 'video.observed_growth_28d.v1' || !growthStatuses.includes(proof.status as typeof growthStatuses[number])) return invalid();
          for (const key of ['from_at', 'to_at']) if (proof[key] !== null && proof[key] !== undefined) stamp(proof[key]);
        }
      } else if (observedFields.includes(field)) {
        const proof = record(cell.observation);
        if (proof.version !== 1 || proof.method !== (field === 'observation.views_at_age' ? 'video.observed_age.v1' : 'video.observed_change.v1')
            || !['known', 'missing_endpoint', 'missing_boundary', 'unknown_definition', 'incompatible_definition', 'missing_age_observation'].includes(String(proof.reason))) return invalid();
        for (const key of ['from_at', 'to_at']) if (proof[key] !== null) stamp(proof[key]);
        if (cell.state === 'known' && (proof.reason !== 'known' || proof.to_at === null || proof.from_at === null
            || stamp(proof.to_at) <= stamp(proof.from_at) || typeof proof.actual_seconds !== 'string'
            || !/^[0-9]+(?:\.[0-9]{1,6})?$/.test(proof.actual_seconds))) return invalid();
      }
      if (cell.state !== 'known') {
        if (cell.value !== null || identityFields.includes(field) || field==='channel.id') return invalid();
        if (cell.observed_at !== null) stamp(cell.observed_at);
        if (cell.expires_at !== null) stamp(cell.expires_at);
        continue;
      }
      if (stamp(cell.observed_at) > readAt || stamp(cell.expires_at) < deadline) return invalid();
      if (field === 'video.caption_available' || field === 'video.paid_promotion') { if (typeof cell.value !== 'boolean') return invalid(); continue; }
      if (field === 'video.tags' || field === 'video.topic_urls') {
        if (!Array.isArray(cell.value) || cell.value.length > (field === 'video.tags' ? 500 : 50)
            || cell.value.some(v => typeof v !== 'string') || new TextEncoder().encode((cell.value as string[]).join('')).length > 25600) return invalid();
        continue;
      }
      if (typeof cell.value !== 'string') return invalid();
      if ((field === 'video.id' || field === 'channel.id') && cell.value !== row.id || field === 'video.type' && cell.value !== filters.type
          || field === 'video.channel_id' && !/^UC[\w-]{22}$/.test(cell.value)
          || (field === 'video.title' || field === 'channel.title') && [...cell.value].length > 1000) return invalid();
      if (['video.views', 'video.likes', 'video.comments', 'video.duration_seconds', 'channel.subscribers', 'channel.views', 'channel.video_count',
        'video.title_length', 'video.tag_count', 'video.published_year'].includes(field)) uint(cell.value);
      if (['video.like_rate', 'video.comment_rate', 'video.lifetime_views_per_day'].includes(field)
          && !/^(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,6})?$/.test(cell.value)) return invalid();
      if (field === 'video.published_date' && !/^\d{4}-\d{2}-\d{2}$/.test(cell.value)) return invalid();
      if (field === 'observation.views_at_age' || field === 'observation.growth_28d_interval_seconds' || field === 'observation.growth_28d_tracked_days') uint(cell.value);
      if (field === 'observation.growth_28d_status' && !growthStatuses.includes(cell.value as typeof growthStatuses[number])) return invalid();
      if (field === 'observation.views_delta' || field === 'observation.views_per_hour' || field === 'observation.views_gained_28d' || field === 'observation.views_per_day_28d') {
        if (!/^-?(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,6})?$/.test(cell.value)
            || BigInt(cell.value.replace('-', '').split('.')[0]) > 18446744073709551615n) return invalid();
      }
      if (field.endsWith('_at')) stamp(cell.value);
      if (field === 'video.thumbnail_url' && cell.value !== `https://img.youtube.com/vi/${row.id}/hqdefault.jpg`) return invalid();
      if (field === 'channel.avatar_url' && (cell.value.length > 2048 || !httpsReference(cell.value))) return invalid();
    }
  }
  return value as ResearchPage;
}
function httpsReference(value: string): boolean {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password; } catch { return false; }
}
export function safeImage(value: string): boolean {
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password && !url.port
    && (url.hostname === 'yt3.ggpht.com' || url.hostname === 'yt3.googleusercontent.com' || url.hostname === 'img.youtube.com'); } catch { return false; }
}
export async function loadResearchCapabilities(signal: AbortSignal, source:ResearchSource=videoSource): Promise<ResearchCapabilities> {
  const root = record(await requestJson('/research/capabilities'+(source===videoSource?'':'?source='+encodeURIComponent(source)), signal));
  if (root.query_version !== 1 || root.source_id !== source || root.max_video_types !== (source===channelSource?0:1)
      || !Array.isArray(root.video_types) || root.video_types.some(type => !['long', 'short', 'stream'].includes(type))
      || !Array.isArray(root.fields) || !Array.isArray(root.scopes) || root.scopes.some(scope => !['channel', 'folder', 'label', 'collection', 'library'].includes(scope))) return invalid();
  const fields = root.fields.map(record).filter(field => researchFields.includes(field.id as ResearchField));
  if (fields.some(field => typeof field.sortable !== 'boolean' || typeof field.value_type !== 'string'
    || !Array.isArray(field.operators) || field.operators.some(op => typeof op !== 'string')
    || field.enabled_video_types !== undefined && (!Array.isArray(field.enabled_video_types) || field.enabled_video_types.some(kind => !['long','short','stream'].includes(String(kind)))))) return invalid();
  if ((source===channelSource?['channel.id']:identityFields).some(id => !fields.some(field => field.id === id))) return invalid();
  if(root.operations!==undefined&&(!Array.isArray(root.operations)||root.operations.some(op=>typeof op!=="string")))return invalid();
  const offered = root.video_types as unknown[];
  const turnedOff = root.turned_off_video_types ?? [];
  if (!Array.isArray(turnedOff) || turnedOff.length > 2 || new Set(turnedOff).size !== turnedOff.length
    || turnedOff.some(kind => !['long','short','stream'].includes(kind as string) || offered.includes(kind))) return invalid();
  const limits=root.observation_age_limits===undefined?{}:record(root.observation_age_limits);
  // 0: no video is refreshed that often under the current schedule (window unavailable); null: every age.
  if(Object.entries(limits).some(([kind,windows])=>!['long','short','stream'].includes(kind)||Object.values(record(windows)).some(v=>v!==null&&!(Number.isSafeInteger(v)&&Number(v)>=0))))return invalid();
  // The library scope (every visible channel) serves Home; the workshop offers the other four.
  return { source_id:source, fields, video_types: root.video_types, scopes: root.scopes.filter(scope => scope !== 'library'),operations:root.operations??[],observation_age_limits:limits,turned_off_video_types:turnedOff } as ResearchCapabilities;
}
/**
 * A failed workshop request in words: a known refusal has its own sentence;
 * otherwise the reason (HTTP status, lost connection) and `retry`, the action that
 * exists where the message is shown (Run research, Check connections, a reload).
 */
export function researchError(error: unknown, retry = 'Reload the page to try again.'): string {
  if (error instanceof ExplorerApiError) {
    const messages: Record<string, string> = {
      grant_required: 'This calculation requires an accepted analytics use case and server permission.',
      policy_denied: 'Your access has ended, or this installation does not allow this request.',
      cursor_expired: 'This list expired. Run research to read it again.',
      cursor_context_changed: 'Data or access changed. Run research again.',
      source_unavailable: 'This source is no longer available. Choose another source.',
      catalogue_preparing: 'Stored data is being prepared in the background. Try again shortly.',
      query_busy: 'The server is busy with other reads or lists. Try again shortly.',
      query_budget_exceeded: 'The query reached its limits. Narrow the selection and try again.',
      field_disabled: 'A selected field is disabled. Remove it from the query or enable it.',
      dependency_disabled: 'Data needed by this query is disabled.',
      format_turned_off: FORMAT_TURNED_OFF_TEXT,
      incomplete_source: 'This source is incomplete. Turn off “Require complete data” to see available videos.',
      invalid_query: 'Check the conditions and values, then try again.',
      unsupported_field: 'A selected field is not supported by this server.',
      unsupported_operator: 'A condition is not supported by this server.',
    };
    if (error.code && messages[error.code]) return messages[error.code];
  }
  const reason = error instanceof Error && error.name === 'ExplorerNetworkError' ? error.message
    : !(error instanceof ExplorerApiError) ? 'The server’s answer could not be read.'
      : error.code === 'client_timeout' ? 'The request timed out.'
        : error.status >= 500 ? `The server could not complete the request (HTTP ${error.status}).`
          : `The request was not accepted (HTTP ${error.status}).`;
  return `${reason} ${retry}`;
}

// Presentation only: never persist source rows, clocks, filters or cursor tokens.
export type Presentation = { version: 1; view: 'table' | 'gallery'; columns: ResearchField[] };
export function parsePresentation(raw: string | null): Presentation {
  try {
    const value = JSON.parse(raw ?? 'null');
    if (value?.version === 1 && ['table', 'gallery'].includes(value.view) && Array.isArray(value.columns)
        && value.columns.length > 0 && value.columns.length <= researchFields.length
        && value.columns.every((id: unknown) => researchFields.includes(id as ResearchField))
        && new Set(value.columns).size === value.columns.length) return { version: 1, view: value.view, columns: value.columns };
  } catch { /* Broken presentation settings do not affect query access. */ }
  return { version: 1, view: 'table', columns: [...defaultColumns] };
}
