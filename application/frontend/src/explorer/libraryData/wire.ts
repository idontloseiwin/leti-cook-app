// Every shared-backend identifier the Library uses, and the request builders.
// The one place where names change between the API response and the browser model.
// Names marked "working" are not final until the named package reports them.
// Pure TypeScript: no React, window or network.
import type { VideoType } from '../../types';

export type LibraryVideoType = VideoType;
export const LIBRARY_VIDEO_TYPES: readonly LibraryVideoType[] = ['long', 'short', 'stream'];

export const VIDEO_SOURCE = 'public.videos.current.v1';
export const QUERY_VERSION = 1;
/** Library pages: 50 rows. */
export const LIBRARY_PAGE_SIZE = 50;
/** Frozen session lifetime and limits for the current session (capabilities report the live values). */
export const FROZEN_TTL_SECONDS = 600;
export const FROZEN_MAX_ROWS = 50_000;
/** Card fields; equal to `LIBRARY_SELECT`. Tags and description are never selected. */
export const LIBRARY_SELECT = [
  'video.id', 'video.title', 'video.thumbnail_url', 'video.published_at', 'video.duration_seconds',
  'video.views', 'video.likes', 'video.comments', 'video.category_id',
] as const;

export const FIELDS = {
  id: 'video.id',
  title: 'video.title',
  thumbnailUrl: 'video.thumbnail_url',
  publishedAt: 'video.published_at',
  durationSeconds: 'video.duration_seconds',
  views: 'video.views',
  likes: 'video.likes',
  comments: 'video.comments',
  categoryId: 'video.category_id',
  description: 'video.description',
  /** Working: publication date and year in `time.timezone`. */
  publishedDate: 'video.published_date',
  publishedYear: 'video.published_year',
  /** Working. */
  titleLength: 'video.title_length',
  tagCount: 'video.tag_count',
  likeRate: 'video.like_rate',
  commentRate: 'video.comment_rate',
  /** Working: 28-day growth. */
  viewsGained28d: 'observation.views_gained_28d',
  /** Home pace, views per day at the counter check (at least one hour old). */
  lifetimeViewsPerDay: 'video.lifetime_views_per_day',
  /** Library's further filters: captions, declared paid promotion, a stream's actual end. */
  captionAvailable: 'video.caption_available',
  paidPromotion: 'video.paid_promotion',
  actualEndAt: 'video.actual_end_at',
  tags: 'video.tags',
} as const;
export const DERIVED_FIELDS = [FIELDS.titleLength, FIELDS.tagCount, FIELDS.likeRate, FIELDS.commentRate] as const;

/** Operation ids, as registered in `research_query/operation_kinds`. */
export const OPERATIONS = {
  /** Counts by facet for the current selection. */
  facets: 'video.facets.v1',
  /** The workshop's summary block; the Library Overview reads the four operations below. */
  summary: 'video.summary.v1',
  /** Overview requires descriptive_analytics. */
  fingerprint: 'video.fingerprint.v1',
  rhythm: 'video.publication_rhythm.v1',
  years: 'video.publication_years.v1',
  breakdown: 'video.content_breakdown.v1',
  /** The workshop's calendar block (its zone is the query's `time.timezone`). */
  publicationCalendar: 'video.publication_calendar.v1',
  /** Channel header (never gated). */
  counterChange: 'channel.counter_change.v1',
  formatTotals: 'channel.format_totals.v1',
  /** Observed history and launch comparisons for one video. */
  observedHistory: 'video.observed_history.v1',
  launchComparison: 'video.launch_comparison.v1',
  /** Median Home pace over every video of the period. */
  paceMedian: 'video.view_pace_median.v1',
} as const;

export const ROUTES = {
  execute: '/research/queries/execute',
  frozenIds: '/research/queries/frozen-ids',
  operations: '/research/operations/execute',
  capabilities: '/research/capabilities',
  /** Library v1 conditions → one shared query. */
  translate: '/saved/views/translate',
} as const;

/** Sorts are always stable on the video id (the backend adds `video.id asc`). */
export type SortItem = { field: string; direction: 'asc' | 'desc'; nulls?: 'last' };

/** The query a server translation returns (only the parts the Library keeps). */
export type TranslatedQuery = {
  query_version: 1;
  source: { id: string };
  scope: { kind: 'channel'; id: string };
  video_types: [LibraryVideoType];
  where?: unknown;
  order_by: SortItem[];
  time?: { mode?: string; timezone?: string };
};

export type LibraryPage =
  | { size?: number; anchorId?: string }
  | { size?: number; after: string };

export type LibraryQueryRequest = {
  query_version: 1;
  source: { id: string };
  scope: { kind: 'channel'; id: string };
  video_types: [LibraryVideoType];
  select: string[];
  where?: unknown;
  order_by: SortItem[];
  time: { mode: 'current'; timezone: string };
  coverage: { mode: 'available'; required_fields: string[] };
  consistency: 'frozen';
  page: { size: number; anchor_id?: string; after?: string };
};

/** True when a condition tree reads a field evaluated in `time.timezone`. */
export function usesLocalDateFields(node: unknown): boolean {
  if (!node || typeof node !== 'object') return false;
  if (Array.isArray(node)) return node.some(usesLocalDateFields);
  const item = node as Record<string, unknown>;
  if (item.field === FIELDS.publishedDate || item.field === FIELDS.publishedYear) return true;
  return usesLocalDateFields(item.all) || usesLocalDateFields(item.any) || usesLocalDateFields(item.not);
}

function channelId(value: string): string {
  if (!/^UC[A-Za-z0-9_-]{22}$/.test(value)) throw new Error('A Library query needs one channel scope.');
  return value;
}

/**
 * A frozen Library page request from a server translation.
 * Membership (source, scope, type, where, order, zone) comes only from the
 * translation; this adds what does not change the matched set: the card
 * fields (+ `fields`: the frozen Inspector values, and growth fields when
 * allowed), available coverage, frozen consistency and the page (first page
 * with an anchor, or a cursor).
 */
export function libraryQuery(
  translated: TranslatedQuery,
  options: { timeZone: string; growthFields?: readonly string[]; fields?: readonly string[]; page?: LibraryPage },
): LibraryQueryRequest {
  if (translated.query_version !== QUERY_VERSION || translated.source?.id !== VIDEO_SOURCE
    || translated.scope?.kind !== 'channel' || !Array.isArray(translated.video_types)
    || translated.video_types.length !== 1 || !LIBRARY_VIDEO_TYPES.includes(translated.video_types[0])
    || !Array.isArray(translated.order_by) || !translated.order_by.length) {
    throw new Error('The translated Library query has an unexpected shape.');
  }
  // For v1 filters the translator resolves dates into UTC instants and leaves
  // `time.timezone` at UTC; the page still runs in the viewer zone (frozen
  // key, years in facets). Local-date fields must have been translated in
  // the viewer zone itself.
  const translatedZone = translated.time?.timezone ?? 'UTC';
  if (translatedZone !== options.timeZone
    && (translatedZone !== 'UTC' || usesLocalDateFields(translated.where))) {
    throw new Error('The translated query belongs to another time zone.');
  }
  const zone = options.timeZone;
  const page = options.page ?? {};
  const size = page.size ?? LIBRARY_PAGE_SIZE;
  if (!Number.isSafeInteger(size) || size < 1 || size > 200) throw new Error('Unsupported Library page size.');
  const select = [...LIBRARY_SELECT, ...(options.fields ?? []), ...(options.growthFields ?? [])];
  if (new Set(select).size !== select.length) throw new Error('Duplicate Library field.');
  return {
    query_version: QUERY_VERSION,
    source: { id: VIDEO_SOURCE },
    scope: { kind: 'channel', id: channelId(translated.scope.id) },
    video_types: [translated.video_types[0]],
    select,
    ...(translated.where === undefined || translated.where === null ? {} : { where: translated.where }),
    order_by: translated.order_by.map((item) => ({ field: item.field, direction: item.direction, nulls: 'last' as const })),
    time: { mode: 'current', timezone: zone },
    coverage: { mode: 'available', required_fields: [] },
    consistency: 'frozen',
    page: 'after' in page
      ? { size, after: page.after }
      : { size, ...(page.anchorId ? { anchor_id: page.anchorId } : {}) },
  };
}

/**
 * `parameters.population` of an operation: the whole query, or one frozen
 * result. The server's key is `frozen_ref` (`inspection.population_reference`).
 */
export type Population = { kind: 'query' } | { kind: 'frozen'; frozen_ref: string };

/**
 * One operation request. The top level is closed (`version, operation, query,
 * parameters`), so a frozen result travels only as `parameters.population`.
 */
export function operationRequest(
  operation: string,
  query: unknown,
  parameters: Record<string, unknown> = {},
  frozenRef?: string,
): { version: 1; operation: string; query: unknown; parameters: Record<string, unknown> } {
  if (Object.hasOwn(parameters, 'population')) throw new Error('Population is set from the frozen reference.');
  return {
    version: 1,
    operation,
    query,
    parameters: { ...parameters, ...(frozenRef ? { population: { kind: 'frozen', frozen_ref: frozenRef } satisfies Population } : {}) },
  };
}

/** "Select all matching": every id of one frozen result in one call. */
export function frozenIdsRequest(frozenRef: string): { version: 1; frozen_ref: string } {
  if (typeof frozenRef !== 'string' || !frozenRef || frozenRef.length > 4096 || /[\u0000- \u007f]/.test(frozenRef)) {
    throw new Error('A frozen reference is required.');
  }
  return { version: 1, frozen_ref: frozenRef };
}
