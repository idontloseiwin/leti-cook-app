// Videos adapter: frozen pages of the
// shared backend → the cards the Library grid draws today, for Long, Shorts
// and Streams alike. One module for the page, the "Select all matching" id
// export, the group counts (facets) and the capabilities the Videos tab reads.
//
// Strict: an unknown key, a wrong type or a broken invariant
// is an ExplorerContractError; nothing is shown partially. Counters stay
// decimal strings. Growth is parsed only by `growthFromCells` (inspector.ts):
// one parser of the growth evidence for the card and the Inspector.
// Pure TypeScript: no React, window or network.
import type { ChannelExplorerCategory, ChannelExplorerVideo } from '../../types.ts';
import { transferDiscoveryReceipt } from '../../discoveryJournal.ts';
import { transferSourceLifetime } from '../../sourceLifetime.ts';
import {
  FIELDS, LIBRARY_VIDEO_TYPES, OPERATIONS, VIDEO_SOURCE, operationRequest,
  type LibraryQueryRequest, type LibraryVideoType, type TranslatedQuery,
} from './wire.ts';
import {
  array, closedObject, contractError, count, enumValue, instantMilliseconds, openObject, text, uintString, utcInstant,
  videoId, CELL_STATES, type JsonRecord,
} from './strict.ts';
import {
  coverageNotice, frozenPagePosition, frozenSessionIdentity, frozenSessionKey, requireSameFrozenSession,
  type CoverageNotice, type FrozenSessionIdentity,
} from './envelope.ts';
import { GROWTH_FIELDS, growthFromCells, type InspectorGrowth } from './inspector.ts';
import type { LibraryGates, ResearchCapabilitiesInput } from './capabilities.ts';
import { presetsFor, type LibraryPresetGroup } from './presets.ts';

// ---------------------------------------------------------------------------
// Switches (one place each)

/**
 * Group counts over the frozen list itself (server-side support, on
 * by the server): true sends `population: {kind: "frozen", frozen_ref}`, and the list
 * freezes every field of the groups the panel asks for (`libraryFrozenFields`);
 * false counts the current population with the same base query and group conditions.
 */
export const FROZEN_FACETS = true;

/**
 * The frozen list selects `video.tags` only when the session stays
 * within 64 MiB at 20,000 rows. Measured on the 20,000-video fixture
 * (temporary PostgreSQL 17): 61.5 MiB with 15 tags of 13 characters,
 * 67.5 MiB at YouTube's 500-character tag limit (over the server's 64 MiB per
 * result). Off: the Inspector shows the current tags with their current count
 * (the server takes the count from the same current read as the tags,
 * although the list froze `video.tag_count` for its group counts).
 */
export const FROZEN_TAGS = false;

// ---------------------------------------------------------------------------
// Capabilities

export type LibraryCapabilities = ResearchCapabilitiesInput & { presets?: unknown };

/** `/research/capabilities` → what the Library gates read. Unknown extra keys are ignored. */
export function parseLibraryCapabilities(value: unknown): LibraryCapabilities {
  const root = openObject(value, 'capabilities');
  if (root.query_version !== 1 || root.source_id !== VIDEO_SOURCE) contractError('capabilities', 'not the video source.');
  const fields = array(root.fields, 'capabilities.fields', 500).map((item, index) => {
    const field = openObject(item, `capabilities.fields.${index}`);
    const id = text(field.id, `capabilities.fields.${index}.id`, 96);
    const types = field.enabled_video_types === undefined ? undefined
      : array(field.enabled_video_types, `capabilities.fields.${index}.enabled_video_types`, 3)
        .map((type, at) => enumValue(type, `capabilities.fields.${index}.enabled_video_types.${at}`, LIBRARY_VIDEO_TYPES));
    return { id, ...(types ? { enabled_video_types: types } : {}) };
  });
  const words = (item: unknown, path: string) => (item === undefined ? undefined
    : array(item, path, 200).map((entry, index) => text(entry, `${path}.${index}`, 96)));
  const frozen = root.frozen === undefined ? undefined : openObject(root.frozen, 'capabilities.frozen');
  return {
    fields,
    operations: words(root.operations, 'capabilities.operations'),
    consistency_modes: words(root.consistency_modes, 'capabilities.consistency_modes'),
    ...(frozen ? { frozen: { ttl_seconds: frozen.ttl_seconds as number, max_rows: frozen.max_rows as number, max_page_size: frozen.max_page_size as number } } : {}),
    ...(root.presets !== undefined ? { presets: root.presets } : {}),
  };
}

// ---------------------------------------------------------------------------
// The frozen page request

/**
 * Calculated fields frozen next to their inputs: the Inspector shows the browsed
 * values and the title length and tag groups count the frozen list.
 * `video.tag_count` is a number per row (negligible beside the 64 MiB limit).
 */
const FROZEN_CALCULATIONS = [FIELDS.titleLength, FIELDS.tagCount, FIELDS.likeRate, FIELDS.commentRate] as const;

/**
 * Fields the frozen list selects besides the card fields (INSPECTOR_FROZEN_FIELDS):
 * calculations when the server grants them, tags only under
 * FROZEN_TAGS. Growth fields travel separately (`growthFields`).
 */
export function libraryFrozenFields(gates: LibraryGates | null, tags = FROZEN_TAGS): string[] {
  if (!gates) return [];
  const fields: string[] = gates.derivedFilters ? [...FROZEN_CALCULATIONS] : [];
  if (tags && gates.field(FIELDS.tags)) fields.push(FIELDS.tags);
  return fields;
}

/** The 28-day growth fields when growth is allowed for this attempt (growth order: the status carries the evidence). */
export function libraryGrowthFields(allowed: boolean): string[] {
  return allowed ? [...GROWTH_FIELDS] : [];
}

// ---------------------------------------------------------------------------
// One frozen page → the grid's cards

/** Card values shown as "—": `expired` (needs refresh) or `unavailable`. */
export type LibraryValueState = 'expired' | 'unavailable';
export type LibraryCardField = 'title' | 'published_at' | 'duration' | 'views' | 'like_count' | 'comment_count' | 'category_id' | 'thumbnail_url';
export type LibraryVideo = ChannelExplorerVideo & {
  video_id: string;
  video_type: LibraryVideoType;
  view_growth?: InspectorGrowth;
  value_states?: Partial<Record<LibraryCardField, LibraryValueState>>;
  /** At least one selected value is stale: the row stays, the value shows "—". */
  stale?: true;
  /** The row lives as long as its own values, not only the session. */
  source_expires_at?: string;
};

export type LibraryBrowse = {
  session_key: string;
  query_fingerprint: string;
  /** Latest token of this session (tokens differ on every page; any one works). */
  frozen_ref: string;
  created_at: string;
  expires_at: string;
  next_cursor: string | null;
  previous_cursor: string | null;
  start_index: number;
  total_count: number;
  anchor_video_id: string | null;
  consistency: 'frozen';
};

export type LibraryVideosPayload = {
  contract_version: 1;
  video_type: LibraryVideoType;
  channel: { channel_id: string };
  videos: LibraryVideo[];
  browse: LibraryBrowse;
  pagination: { matched_count: number; offset: number; returned_count: number; has_more_in_scan: boolean };
  coverage: { complete: boolean; notice: CoverageNotice };
  /** Whether the selection includes growth fields. */
  growth: boolean;
  columns: string[];
  time_zone: string;
};

export type LibraryPageExpectation = { request: LibraryQueryRequest; channelId: string };

const ROOT = ['query_version', 'query_fingerprint', 'context', 'columns', 'provenance', 'rows', 'coverage', 'page'];
const ROOT_OPTIONAL = ['warnings', 'source_read_at', 'source_expires_at'];
const CONTEXT = ['source_id', 'evaluated_at', 'valid_before', 'consistency', 'time_zone', 'frozen_ref'];
const CONTEXT_OPTIONAL = ['source_schema_version', 'registry_version', 'lifecycle_profile', 'preferences_revision', 'type_policy',
  'observation_age_limit_seconds'];
const CELL = ['value', 'state', 'provenance_ref', 'observed_at', 'expires_at'];
const FROZEN_REF = /^[A-Za-z0-9_-]{1,4096}$/;
const RATE = /^(0|[1-9][0-9]{0,24})(?:\.[0-9]{1,6})?$/;
const CARD_FIELD: Partial<Record<string, LibraryCardField>> = {
  [FIELDS.title]: 'title', [FIELDS.publishedAt]: 'published_at', [FIELDS.durationSeconds]: 'duration',
  [FIELDS.views]: 'views', [FIELDS.likes]: 'like_count', [FIELDS.comments]: 'comment_count',
  [FIELDS.categoryId]: 'category_id', [FIELDS.thumbnailUrl]: 'thumbnail_url',
};
const GROWTH = new Set<string>(GROWTH_FIELDS);

/** The value reader of every field a Library page may select. */
function fieldValue(field: string, value: unknown, path: string, id: string): unknown {
  switch (field) {
    case FIELDS.id: if (videoId(value, path) !== id) contractError(path, 'another video.'); return value;
    case FIELDS.title: return text(value, path, 4096);
    case FIELDS.thumbnailUrl:
      if (value !== `https://img.youtube.com/vi/${id}/mqdefault.jpg`) contractError(path, 'unexpected thumbnail.');
      return value;
    case FIELDS.publishedAt: return utcInstant(value, path);
    case FIELDS.durationSeconds: case FIELDS.views: case FIELDS.likes: case FIELDS.comments:
    case FIELDS.titleLength: case FIELDS.tagCount: return uintString(value, path);
    case FIELDS.likeRate: case FIELDS.commentRate:
      if (typeof value !== 'string' || !RATE.test(value)) contractError(path, 'expected a rate.');
      return value;
    case FIELDS.categoryId: return text(value, path, 64);
    case FIELDS.tags: return array(value, path, 500).map((tag, index) => text(tag, `${path}.${index}`, 25_600));
    default: return contractError(path, 'not a Library field.');
  }
}

function libraryRow(raw: unknown, path: string, columns: readonly string[], provenance: JsonRecord, type: LibraryVideoType,
  validBefore: number, growth: boolean): LibraryVideo {
  const row = closedObject(raw, path, ['id', 'cells']);
  const id = videoId(row.id, `${path}.id`);
  const cells = closedObject(row.cells, `${path}.cells`, columns);
  const video: LibraryVideo = { video_id: id, video_type: type };
  const states: Partial<Record<LibraryCardField, LibraryValueState>> = {};
  let expiry = Infinity, expiryText: string | undefined, stale = false;
  for (const field of columns) {
    const at = `${path}.cells.${field}`;
    if (GROWTH.has(field)) {
      // Shape and agreement with the evidence: growthFromCells (below); provenance here.
      const reference = openObject(cells[field], at).provenance_ref;
      if (typeof reference !== 'string' || !Object.hasOwn(provenance, reference)) contractError(`${at}.provenance_ref`, 'unknown provenance.');
      continue;
    }
    const cell = closedObject(cells[field], at, CELL, ['observation']);
    const state = enumValue(cell.state, `${at}.state`, CELL_STATES);
    const reference = text(cell.provenance_ref, `${at}.provenance_ref`, 128);
    if (!Object.hasOwn(provenance, reference)) contractError(`${at}.provenance_ref`, 'unknown provenance.');
    if (cell.observation !== undefined && cell.observation !== null) contractError(`${at}.observation`, 'only growth carries evidence.');
    const observed = cell.observed_at === null ? null : utcInstant(cell.observed_at, `${at}.observed_at`);
    const expires = cell.expires_at === null ? null : utcInstant(cell.expires_at, `${at}.expires_at`);
    if (field === FIELDS.views && observed) video.checked_at = observed;
    if (state !== 'known') {
      if (cell.value !== null) contractError(`${at}.value`, `a ${state} cell carries no value.`);
      if (field === FIELDS.id) contractError(at, 'the id is always known.');
      if (state === 'expired') stale = true;
      const card = CARD_FIELD[field];
      if (card && state !== 'unknown') states[card] = state;
      continue;
    }
    if (cell.value === null) contractError(`${at}.value`, 'a known cell needs a value.');
    const value = fieldValue(field, cell.value, `${at}.value`, id);
    if (expires) {
      const milliseconds = instantMilliseconds(expires);
      // A known value stays readable for the whole session.
      if (milliseconds < validBefore) contractError(`${at}.expires_at`, 'expires inside the session.');
      if (milliseconds < expiry) { expiry = milliseconds; expiryText = expires; }
    }
    switch (field) {
      case FIELDS.title: video.title = value as string; break;
      case FIELDS.thumbnailUrl: video.thumbnail_url = value as string; break;
      case FIELDS.publishedAt: video.published_at = value as string; break;
      case FIELDS.durationSeconds: {
        // Only an exact number of seconds is shown (beyond 2^53 it is unknown).
        const seconds = Number(value);
        if (Number.isSafeInteger(seconds)) video.duration = seconds;
        break;
      }
      case FIELDS.views: video.views = value as string; break;
      case FIELDS.likes: video.like_count = value as string; break;
      case FIELDS.comments: video.comment_count = value as string; break;
      case FIELDS.categoryId: video.category_id = value as string; break;
      case FIELDS.tags: video.tags = value as string[]; break;
      default: break;
    }
  }
  if (growth) {
    const parsed = growthFromCells(cells, `${path}.cells`);
    if (!parsed) contractError(`${path}.cells`, 'growth fields were selected.');
    video.view_growth = parsed;
  }
  if (Object.keys(states).length) video.value_states = states;
  if (stale) video.stale = true;
  if (expiryText) video.source_expires_at = expiryText;
  transferDiscoveryReceipt(raw, video);
  return video;
}

/**
 * One frozen page of `expect.request` → the Library's page presentation.
 * Checks: the frozen source, scope and type, the zone, the exact columns, one
 * session identity, page bounds, the anchor echo and one row per id.
 */
export function parseLibraryPage(raw: unknown, expect: LibraryPageExpectation): LibraryVideosPayload {
  const { request } = expect;
  const root = closedObject(raw, 'result', ROOT, ROOT_OPTIONAL);
  if (root.query_version !== 1) contractError('result.query_version', 'unsupported version.');
  const identity: FrozenSessionIdentity = frozenSessionIdentity(root as never);
  const context = closedObject(root.context, 'result.context', CONTEXT, CONTEXT_OPTIONAL);
  if (context.source_id !== VIDEO_SOURCE || context.consistency !== 'frozen') contractError('result.context', 'expected a frozen video result.');
  if (context.time_zone !== request.time.timezone) contractError('result.context.time_zone', 'another time zone.');
  const frozenRef = text(context.frozen_ref, 'result.context.frozen_ref', 4096);
  if (!FROZEN_REF.test(frozenRef)) contractError('result.context.frozen_ref', 'expected a reference.');
  const columns = array(root.columns, 'result.columns', 64).map((item, index) => text(item, `result.columns.${index}`, 96));
  if (columns.length !== request.select.length || columns.some((item, index) => item !== request.select[index])) {
    contractError('result.columns', 'not the requested fields.');
  }
  const provenance = openObject(root.provenance, 'result.provenance');
  if (root.warnings !== undefined) array(root.warnings, 'result.warnings', 32).forEach((item, index) => text(item, `result.warnings.${index}`, 96));
  const notice = coverageNotice(root.coverage);
  const rows = array(root.rows, 'result.rows', request.page.size);
  const position = frozenPagePosition(root.page, rows.length);
  if (position.totalCount !== identity.total_count) contractError('result.page.total_count', 'not the session total.');
  if (position.totalCount > 0 && rows.length === 0) contractError('result.rows', 'an empty page of a non-empty result.');
  if (position.startIndex % request.page.size) contractError('result.page.start_index', 'not a page boundary.');
  const validBefore = instantMilliseconds(identity.valid_before);
  const growth = request.select.some((field) => GROWTH.has(field));
  const type = request.video_types[0];
  const videos = rows.map((row, index) => libraryRow(row, `result.rows.${index}`, columns, provenance, type, validBefore, growth));
  if (new Set(videos.map((video) => video.video_id)).size !== videos.length) contractError('result.rows', 'a repeated video.');
  const anchor = request.page.anchor_id;
  if (position.anchorId !== null) {
    if (position.anchorId !== anchor || !videos.some((video) => video.video_id === anchor)) contractError('result.page.anchor_id', 'not the requested anchor on this page.');
  } else if (!request.page.after && position.startIndex !== 0) {
    contractError('result.page.start_index', 'a first page without its anchor starts at zero.');
  }
  return transferSourceLifetime(raw, {
    contract_version: 1,
    video_type: type,
    channel: { channel_id: expect.channelId },
    videos,
    browse: {
      session_key: frozenSessionKey(identity),
      query_fingerprint: identity.query_fingerprint,
      frozen_ref: frozenRef,
      created_at: identity.evaluated_at,
      expires_at: identity.valid_before,
      next_cursor: position.nextCursor,
      previous_cursor: position.previousCursor,
      start_index: position.startIndex,
      total_count: position.totalCount,
      anchor_video_id: position.anchorId,
      consistency: 'frozen',
    },
    pagination: { matched_count: position.totalCount, offset: position.startIndex, returned_count: videos.length, has_more_in_scan: position.hasMore },
    coverage: { complete: !notice.partial, notice },
    growth,
    columns,
    time_zone: request.time.timezone,
  } satisfies LibraryVideosPayload);
}

/** Identity of a parsed page (`session_key` is its key). */
export function pageIdentity(payload: LibraryVideosPayload): FrozenSessionIdentity {
  return {
    query_fingerprint: payload.browse.query_fingerprint, evaluated_at: payload.browse.created_at,
    valid_before: payload.browse.expires_at, total_count: payload.browse.total_count,
  };
}

/** Merge adjacent pages of one session. Never deduplicate around a gap or mix sessions. */
export function mergeLibraryPages(current: LibraryVideosPayload, page: LibraryVideosPayload, direction: 'next' | 'previous'): LibraryVideosPayload {
  requireSameFrozenSession(pageIdentity(current), pageIdentity(page));
  if (current.channel.channel_id !== page.channel.channel_id || current.video_type !== page.video_type
    || current.growth !== page.growth || current.columns.join('\n') !== page.columns.join('\n') || current.time_zone !== page.time_zone) {
    contractError('result', 'the page belongs to another query.');
  }
  const before = current.browse, next = page.browse;
  if (direction === 'next'
    ? next.start_index !== before.start_index + current.videos.length
    : next.start_index + page.videos.length !== before.start_index) contractError('result.page', 'not an adjacent page.');
  const ids = new Set(current.videos.map((video) => video.video_id));
  if (page.videos.some((video) => ids.has(video.video_id))) contractError('result.rows', 'a video repeated across pages.');
  const videos = direction === 'next' ? [...current.videos, ...page.videos] : [...page.videos, ...current.videos];
  const browse: LibraryBrowse = {
    ...before,
    frozen_ref: next.frozen_ref,
    start_index: direction === 'previous' ? next.start_index : before.start_index,
    previous_cursor: direction === 'previous' ? next.previous_cursor : before.previous_cursor,
    next_cursor: direction === 'next' ? next.next_cursor : before.next_cursor,
  };
  return {
    ...current, videos, browse,
    pagination: { ...current.pagination, offset: browse.start_index, returned_count: videos.length, has_more_in_scan: browse.next_cursor !== null },
  };
}

// ---------------------------------------------------------------------------
// "Select all matching": every id of the session in one reply

const IDS_ROOT = ['version', 'query_fingerprint', 'context', 'ids', 'total_count'];
const IDS_CONTEXT = ['source_id', 'evaluated_at', 'valid_before', 'consistency', 'time_zone'];

/** The ids of the session this page belongs to, in result order. */
export function parseFrozenIds(raw: unknown, page: LibraryVideosPayload): string[] {
  const root = closedObject(raw, 'frozen_ids', IDS_ROOT, ROOT_OPTIONAL);
  if (root.version !== 1) contractError('frozen_ids.version', 'unsupported version.');
  const context = closedObject(root.context, 'frozen_ids.context', IDS_CONTEXT);
  if (context.source_id !== VIDEO_SOURCE || context.consistency !== 'frozen' || context.time_zone !== page.time_zone) {
    contractError('frozen_ids.context', 'not this frozen result.');
  }
  requireSameFrozenSession(pageIdentity(page), frozenSessionIdentity(root as never));
  const ids = array(root.ids, 'frozen_ids.ids', 50_000).map((item, index) => videoId(item, `frozen_ids.ids.${index}`));
  if (ids.length !== root.total_count || new Set(ids).size !== ids.length) contractError('frozen_ids.ids', 'not every id once.');
  const position = new Map(ids.map((id, index) => [id, index]));
  page.videos.forEach((video, index) => {
    if (position.get(video.video_id) !== page.browse.start_index + index) contractError('frozen_ids.ids', 'another order than the pages.');
  });
  return ids;
}

/** Selection records for the exported ids: loaded rows keep their values, the others only their id. */
export function selectionRecords(ids: readonly string[], loaded: readonly LibraryVideo[], type: LibraryVideoType): LibraryVideo[] {
  const byId = new Map(loaded.map((video) => [video.video_id, video]));
  return ids.map((id) => byId.get(id) ?? { video_id: id, video_type: type });
}

// ---------------------------------------------------------------------------
// Group counts (video.facets.v1)

const GROUP_FIELDS: Record<string, readonly string[]> = {
  views: [FIELDS.views], duration: [FIELDS.durationSeconds], likes: [FIELDS.likes], comments: [FIELDS.comments],
  title_length: [FIELDS.titleLength], tag_count: [FIELDS.tagCount],
  published: [FIELDS.publishedDate, FIELDS.publishedYear], category: [FIELDS.categoryId],
};
const BUCKET_GROUPS = ['views', 'duration', 'likes', 'comments', 'title_length', 'tag_count'] as const;
export type FacetGroup = typeof BUCKET_GROUPS[number] | 'published' | 'category';

/** Groups the filter panel shows: title length and tags only with the Baseline calculations. */
export function facetGroups(gates: LibraryGates): FacetGroup[] {
  return ['views', 'duration', 'likes', 'comments', ...(gates.derivedFilters ? ['title_length', 'tag_count'] as const : []), 'published', 'category'];
}

function nodeFields(node: unknown, found = new Set<string>()): Set<string> {
  if (!node || typeof node !== 'object') return found;
  const item = node as JsonRecord;
  if (typeof item.field === 'string') found.add(item.field);
  for (const key of ['all', 'any'] as const) if (Array.isArray(item[key])) (item[key] as unknown[]).forEach((child) => nodeFields(child, found));
  if (item.not) nodeFields(item.not, found);
  return found;
}

/**
 * The translated `where` split into the base conditions (search, cohort,
 * rates, growth, further filters) and one condition per group, so each group
 * is counted without its own condition. A node belongs to a group
 * when every field it reads is that group's; the cohort (`video.published_at`)
 * stays in the base, like the search and rate conditions.
 */
export function splitFacetConditions(where: unknown, groups: readonly FacetGroup[]): { base: unknown | null; conditions: Partial<Record<FacetGroup, unknown>> } {
  const items = where === undefined || where === null ? []
    : (where as JsonRecord).all && Object.keys(where as JsonRecord).length === 1 ? (where as JsonRecord).all as unknown[] : [where];
  const base: unknown[] = [];
  const grouped: Partial<Record<FacetGroup, unknown[]>> = {};
  for (const item of items) {
    const fields = [...nodeFields(item)];
    const group = groups.find((name) => fields.length > 0 && fields.every((field) => GROUP_FIELDS[name].includes(field)));
    if (group) (grouped[group] ??= []).push(item); else base.push(item);
  }
  const join = (nodes: unknown[]) => (nodes.length === 1 ? nodes[0] : { all: nodes });
  return {
    base: base.length ? join(base) : null,
    conditions: Object.fromEntries(Object.entries(grouped).map(([name, nodes]) => [name, join(nodes!)])) as Partial<Record<FacetGroup, unknown>>,
  };
}

/** The facets request of one Library query (the translated membership), current or over the frozen list. */
export function facetsRequest(translated: TranslatedQuery, options: {
  timeZone: string; groups: readonly FacetGroup[]; frozenRef?: string | null; frozen?: boolean;
}) {
  const { base, conditions } = splitFacetConditions(translated.where, options.groups);
  const query = {
    query_version: 1 as const,
    source: { id: VIDEO_SOURCE },
    scope: { kind: 'channel' as const, id: translated.scope.id },
    video_types: [translated.video_types[0]],
    select: [FIELDS.id],
    ...(base === null ? {} : { where: base }),
    order_by: translated.order_by.map((item) => ({ field: item.field, direction: item.direction, nulls: 'last' as const })),
    time: { mode: 'current' as const, timezone: options.timeZone },
  };
  const frozen = (options.frozen ?? FROZEN_FACETS) && options.frozenRef ? options.frozenRef : undefined;
  const parameters = { groups: [...options.groups], ...(Object.keys(conditions).length ? { conditions } : {}) };
  // One switch: the same base query and group conditions over the frozen list or the current population.
  const request = operationRequest(OPERATIONS.facets, query, parameters, frozen);
  return frozen ? request : { ...request, parameters: { ...request.parameters, population: { kind: 'query' as const } } };
}

export type LibraryFacetRow = { key: string; label: string; min: number | null; max: number | null; count: number };
/** What the filter panel reads, for any type. */
export type LibraryFacets = {
  matched_count: number;
  population: 'query' | 'frozen';
  facets: Partial<Record<typeof BUCKET_GROUPS[number], LibraryFacetRow[]>> & {
    published: { years: Array<{ year: number; count: number }>; older: { before_year: number; count: number } | null };
  };
  /** Every category of the admitted rows with its count, without the category condition. */
  categories: Array<ChannelExplorerCategory & { count: number }>;
  coverage: { complete: boolean };
};

const OPERATION_ROOT = ['version', 'operation', 'operation_fingerprint', 'context', 'coverage', 'result', 'provenance', 'warnings'];
const OPERATION_CONTEXT = ['source_id', 'evaluated_at', 'valid_before', 'consistency'];
const OPERATION_CONTEXT_OPTIONAL = ['registry_version', 'preferences_revision', 'observation_age_limit_seconds', 'source_schema_version',
  'lifecycle_profile', 'type_policy', 'time_zone'];

function bucketCount(value: unknown, path: string): number {
  return count(value, path, 50_000);
}

/** The facets reply for these groups of this type, strictly (presets of the type, years, categories). */
export function parseFacets(raw: unknown, expect: {
  videoType: LibraryVideoType; groups: readonly FacetGroup[]; timeZone: string; frozenTotal?: number;
}): LibraryFacets {
  const root = closedObject(raw, 'facets', OPERATION_ROOT, ['source_read_at', 'source_expires_at']);
  if (root.version !== 1 || root.operation !== OPERATIONS.facets) contractError('facets', 'not the facets operation.');
  if (!/^[a-f0-9]{64}$/.test(text(root.operation_fingerprint, 'facets.operation_fingerprint', 64))) contractError('facets.operation_fingerprint', 'expected a fingerprint.');
  const context = closedObject(root.context, 'facets.context', OPERATION_CONTEXT, OPERATION_CONTEXT_OPTIONAL);
  if (context.source_id !== VIDEO_SOURCE || context.consistency !== 'current') contractError('facets.context', 'expected the video source.');
  if (instantMilliseconds(utcInstant(context.valid_before, 'facets.context.valid_before'))
    <= instantMilliseconds(utcInstant(context.evaluated_at, 'facets.context.evaluated_at'))) contractError('facets.context.valid_before', 'must end after it was evaluated.');
  const notice = coverageNotice(root.coverage);
  openObject(root.provenance, 'facets.provenance');
  array(root.warnings, 'facets.warnings', 32).forEach((item, index) => text(item, `facets.warnings.${index}`, 96));
  const result = closedObject(root.result, 'facets.result', ['groups', 'time_zone', 'population', 'bucket_registry_version']);
  if (result.time_zone !== expect.timeZone) contractError('facets.result.time_zone', 'another time zone.');
  const population = closedObject(result.population, 'facets.result.population',
    ['kind', 'count', 'matched_count', 'expired_counters', 'expired_metadata', 'excluded']);
  const kind = enumValue(population.kind, 'facets.result.population.kind', ['query', 'frozen'] as const);
  if ((kind === 'frozen') !== (expect.frozenTotal !== undefined)) contractError('facets.result.population.kind', 'not the requested population.');
  const total = count(population.count, 'facets.result.population.count', 50_000);
  const matched = count(population.matched_count, 'facets.result.population.matched_count', 50_000);
  if (matched > total) contractError('facets.result.population', 'more matched rows than the population.');
  if (expect.frozenTotal !== undefined && matched !== expect.frozenTotal) contractError('facets.result.population.matched_count', 'not the frozen total.');
  const excluded = openObject(population.excluded, 'facets.result.population.excluded');
  Object.entries(excluded).forEach(([code, value]) => count(value, `facets.result.population.excluded.${code}`, 50_000));
  const groups = closedObject(result.groups, 'facets.result.groups', [...expect.groups]);
  const facets = { published: { years: [], older: null } } as unknown as LibraryFacets['facets'];
  let categories: LibraryFacets['categories'] = [];
  for (const name of expect.groups) {
    const path = `facets.result.groups.${name}`;
    const shape = name === 'published' ? 'years' : name === 'category' ? 'categories' : 'buckets';
    const group = closedObject(groups[name], path, ['field', 'count', 'unknown_count', shape, ...(name === 'published' ? ['older'] : [])]);
    const size = bucketCount(group.count, `${path}.count`);
    bucketCount(group.unknown_count, `${path}.unknown_count`);
    if (name === 'published') {
      if (group.field !== FIELDS.publishedYear) contractError(`${path}.field`, 'unexpected field.');
      let previous = 10_000, sum = 0;
      const years = array(group.years, `${path}.years`, 3).map((item, index) => {
        const row = closedObject(item, `${path}.years.${index}`, ['year', 'count']);
        const year = count(row.year, `${path}.years.${index}.year`, 9999);
        if (year < 1 || year >= previous) contractError(`${path}.years`, 'years descend.');
        previous = year;
        const value = bucketCount(row.count, `${path}.years.${index}.count`);
        sum += value;
        return { year, count: value };
      });
      let older: { before_year: number; count: number } | null = null;
      if (group.older !== null) {
        const row = closedObject(group.older, `${path}.older`, ['before_year', 'count']);
        if (years.length !== 3 || row.before_year !== previous) contractError(`${path}.older`, 'older is below the third year.');
        older = { before_year: previous, count: bucketCount(row.count, `${path}.older.count`) };
        sum += older.count;
      }
      if (sum > size) contractError(path, 'more rows than the group.');
      facets.published = { years, older };
    } else if (name === 'category') {
      if (group.field !== FIELDS.categoryId) contractError(`${path}.field`, 'unexpected field.');
      let sum = 0;
      categories = array(group.categories, `${path}.categories`, 1000).map((item, index) => {
        const row = closedObject(item, `${path}.categories.${index}`, ['id', 'label', 'count']);
        const value = bucketCount(row.count, `${path}.categories.${index}.count`);
        sum += value;
        return { category_id: text(row.id, `${path}.categories.${index}.id`, 64), category_name: text(row.label, `${path}.categories.${index}.label`, 200), count: value };
      });
      if (new Set(categories.map((row) => row.category_id)).size !== categories.length) contractError(path, 'a repeated category.');
      if (sum > size) contractError(path, 'more rows than the group.');
    } else {
      const presets = presetsFor(expect.videoType, name as LibraryPresetGroup);
      if (!presets) contractError(path, 'no presets for this type.');
      if (group.field !== GROUP_FIELDS[name][0]) contractError(`${path}.field`, 'unexpected field.');
      const rows = array(group.buckets, `${path}.buckets`, 50);
      if (rows.length !== presets.length) contractError(`${path}.buckets`, 'not the presets of this type.');
      let sum = 0;
      facets[name as typeof BUCKET_GROUPS[number]] = rows.map((item, index) => {
        const row = closedObject(item, `${path}.buckets.${index}`, ['key', 'label', 'min', 'max', 'count']);
        const preset = presets[index];
        if (row.key !== preset.key || row.label !== preset.label || row.min !== preset.min || row.max !== preset.max) {
          contractError(`${path}.buckets.${index}`, 'not the preset of this type.');
        }
        const value = bucketCount(row.count, `${path}.buckets.${index}.count`);
        sum += value;
        return { key: preset.key, label: preset.label, min: preset.min, max: preset.max, count: value };
      });
      if (sum > size) contractError(path, 'more rows than the group.');
    }
  }
  return transferSourceLifetime(raw, {
    matched_count: matched, population: kind, facets, categories, coverage: { complete: !notice.partial },
  } satisfies LibraryFacets);
}

/** Category labels of the admitted rows (for chips, cards and the Inspector line). */
export function categoryLabels(facets: LibraryFacets | undefined): Map<string, string> {
  return new Map((facets?.categories ?? []).map((row) => [row.category_id, row.category_name]));
}
