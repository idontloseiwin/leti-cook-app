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
import { ownSourceLifetime, transferSourceLifetime } from '../../sourceLifetime.ts';
import {
  FIELDS, LIBRARY_VIDEO_TYPES, OPERATIONS, VIDEO_SOURCE, operationRequest,
  type LibraryQueryRequest, type LibraryVideoType, type TranslatedQuery,
} from './wire.ts';
import {
  array, closedObject, contractError, count, enumValue, instantMilliseconds, openObject, text, uintString, utcInstant,
  videoId, CELL_STATES, type JsonRecord,
} from './strict.ts';
import {
  coverageNotice, pageClocks, frozenPagePosition, frozenSessionIdentity, frozenSessionKey, requireSameFrozenSession,
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
  /** The earliest end of the row's own values (`valueDeadlines` keeps each value's). */
  source_expires_at?: string;
};

/** A shown value of a row with a lifetime of its own (the cell's `expires_at`). */
type TimedValue = LibraryCardField | 'tags' | 'view_growth';
/**
 * When the values of a parsed row end, as wall-clock milliseconds: `row` is the video's identity (after it
 * no value of the row is current), `values` each shown value's own end. Kept beside the row, never in JSON.
 */
type ValueDeadlines = { row: number; values: Partial<Record<TimedValue, number>> };
const valueDeadlines = new WeakMap<LibraryVideo, ValueDeadlines>();

/**
 * The ordered list of a current Library list (`page.order`): every matching video once, in the order of
 * the page it came with (equal values by video ID, unknown values last), read in the same statement.
 */
export type LibraryOrder = {
  /** Every matching video in list order, or null when the list has more than `limit` videos (withheld, never cut). */
  ids: readonly string[] | null;
  total: number;
  limit: number;
};

export type LibraryBrowse = {
  session_key: string;
  query_fingerprint: string;
  /** Latest token of this session (tokens differ on every page; any one works). */
  created_at: string;
  expires_at: string;
  next_cursor: string | null;
  previous_cursor: string | null;
  /** The loaded window of the list: [start_index, end_index). */
  start_index: number;
  end_index: number;
  anchor_video_id: string | null;
  /** Videos of the loaded window that no longer match or are no longer available (not shown, never replaced). */
  left_results: number;
} & ({ consistency: 'frozen'; total_count: number; frozen_ref: string; order?: undefined }
  | { consistency: 'current'; total_count?: undefined; frozen_ref?: undefined; order?: LibraryOrder });

export type LibraryVideosPayload = {
  contract_version: 1;
  video_type: LibraryVideoType;
  channel: { channel_id: string };
  videos: LibraryVideo[];
  browse: LibraryBrowse;
  pagination: { matched_count?: number; offset: number; returned_count: number; has_more_in_scan: boolean };
  coverage: { complete: boolean; notice: CoverageNotice };
  /** Whether the selection includes growth fields. */
  growth: boolean;
  columns: string[];
  time_zone: string;
};

export type LibraryPageExpectation = { request: LibraryQueryRequest; channelId: string };

const ROOT = ['query_version', 'query_fingerprint', 'context', 'columns', 'provenance', 'rows', 'coverage', 'page'];
const ROOT_OPTIONAL = ['warnings', 'source_read_at', 'source_expires_at'];
const CONTEXT = ['source_id', 'evaluated_at', 'valid_before', 'consistency', 'time_zone'];
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
      if (value !== `https://img.youtube.com/vi/${id}/hqdefault.jpg`) contractError(path, 'unexpected thumbnail.');
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
  const ends: ValueDeadlines = { row: Infinity, values: {} };
  let expiry = Infinity, expiryText: string | undefined, stale = false;
  for (const field of columns) {
    const at = `${path}.cells.${field}`;
    if (GROWTH.has(field)) {
      // Shape and agreement with the evidence: growthFromCells (below); provenance here.
      const cell = openObject(cells[field], at);
      const reference = cell.provenance_ref;
      if (cell.expires_at !== null) {
        const expires = utcInstant(cell.expires_at, at + '.expires_at');
        const milliseconds = instantMilliseconds(expires);
        if (milliseconds < validBefore) contractError(at + '.expires_at', 'expires inside the session.');
        if (milliseconds < expiry) { expiry = milliseconds; expiryText = expires; }
        ends.values.view_growth = Math.min(ends.values.view_growth ?? Infinity, milliseconds);
      }
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
      const timed: TimedValue | undefined = field === FIELDS.tags ? 'tags' : CARD_FIELD[field];
      if (field === FIELDS.id) ends.row = milliseconds;
      else if (timed) ends.values[timed] = milliseconds;
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
  if (expiryText) valueDeadlines.set(video, ends);
  transferDiscoveryReceipt(raw, video);
  return video;
}

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

/** `page.order`: the total and the 11-character IDs joined in list order (null beyond the server's limit). */
function parseOrder(value: unknown): LibraryOrder {
  const path = 'result.page.order';
  const order = closedObject(value, path, ['total_count', 'ids', 'limit']);
  const total = count(order.total_count, `${path}.total_count`);
  const limit = count(order.limit, `${path}.limit`);
  if (order.ids === null) {
    if (total <= limit) contractError(`${path}.ids`, 'withheld although the list is within the limit.');
    return { ids: null, total, limit };
  }
  if (typeof order.ids !== 'string' || total > limit || order.ids.length !== total * 11) contractError(`${path}.ids`, 'not every video once.');
  const joined = order.ids as string;
  const ids = new Array<string>(total);
  for (let index = 0; index < total; index += 1) {
    const id = joined.slice(index * 11, index * 11 + 11);
    if (!VIDEO_ID.test(id)) contractError(`${path}.ids`, 'expected video ids.');
    ids[index] = id;
  }
  const result: LibraryOrder = { ids, total, limit };
  if (positions(result).size !== total) contractError(`${path}.ids`, 'a repeated video.');
  return result;
}

const orderPositions = new WeakMap<LibraryOrder, Map<string, number>>();

function positions(order: LibraryOrder): Map<string, number> {
  let known = orderPositions.get(order);
  if (!known) {
    known = new Map((order.ids ?? []).map((id, index) => [id, index]));
    orderPositions.set(order, known);
  }
  return known;
}

/** The place of a video in its list, or -1 (not in the list, or the list has no IDs). */
export function listPosition(order: LibraryOrder | undefined, videoId: string): number {
  return order?.ids ? positions(order).get(videoId) ?? -1 : -1;
}

/**
 * One current or frozen page of `expect.request` → the Library's page presentation.
 * Checks: the frozen source, scope and type, the zone, the exact columns, one
 * session identity, page bounds, the anchor echo and one row per id.
 */
export function parseLibraryPage(raw: unknown, expect: LibraryPageExpectation): LibraryVideosPayload {
  const { request } = expect;
  const root = closedObject(raw, 'result', ROOT, ROOT_OPTIONAL);
  if (root.query_version !== 1) contractError('result.query_version', 'unsupported version.');
  const fixed = request.consistency === 'frozen';
  const context = closedObject(root.context, 'result.context', fixed ? [...CONTEXT, 'frozen_ref'] : CONTEXT, CONTEXT_OPTIONAL);
  if (context.source_id !== VIDEO_SOURCE || context.consistency !== request.consistency) contractError('result.context', 'unexpected source or consistency.');
  if (context.time_zone !== request.time.timezone || request.scope.id !== expect.channelId) contractError('result.context', 'another scope or time zone.');
  const clocks = pageClocks(root);
  const columns = array(root.columns, 'result.columns', 64).map((item, index) => text(item, `result.columns.${index}`, 96));
  if (columns.length !== request.select.length || columns.some((item, index) => item !== request.select[index])) {
    contractError('result.columns', 'not the requested fields.');
  }
  const provenance = openObject(root.provenance, 'result.provenance');
  if (root.warnings !== undefined) array(root.warnings, 'result.warnings', 32).forEach((item, index) => text(item, `result.warnings.${index}`, 96));
  const notice = coverageNotice(root.coverage);
  const rows = array(root.rows, 'result.rows', request.page.size);
  let browse: LibraryBrowse;
  if (fixed) {
    const identity = frozenSessionIdentity(root);
    const position = frozenPagePosition(root.page, rows.length);
    const ref = text(context.frozen_ref, 'result.context.frozen_ref', 4096);
    if (!FROZEN_REF.test(ref)) contractError('result.context.frozen_ref', 'expected a reference.');
    if (position.totalCount > 0 && !rows.length) contractError('result.rows', 'an empty page of a non-empty result.');
    if (position.startIndex % request.page.size) contractError('result.page.start_index', 'not a page boundary.');
    browse = { consistency: 'frozen', session_key: frozenSessionKey(identity), frozen_ref: ref,
      total_count: position.totalCount, start_index: position.startIndex, end_index: position.startIndex + rows.length,
      previous_cursor: position.previousCursor, left_results: 0,
      next_cursor: position.nextCursor, anchor_video_id: position.anchorId, query_fingerprint: clocks.query_fingerprint,
      created_at: clocks.evaluated_at, expires_at: clocks.valid_before };
  } else {
    const listed = request.page.order === 'ids';
    const page = closedObject(root.page, 'result.page', listed ? ['has_more', 'next_cursor', 'order'] : ['has_more', 'next_cursor']);
    const next = page.next_cursor === null ? null : text(page.next_cursor, 'result.page.next_cursor', 4096);
    if (page.has_more !== (next !== null) || (next && (rows.length !== request.page.size || next === request.page.after))) {
      contractError('result.page', 'invalid continuation.');
    }
    browse = { consistency: 'current', session_key: clocks.query_fingerprint + '\n' + clocks.evaluated_at,
      query_fingerprint: clocks.query_fingerprint, created_at: clocks.evaluated_at, expires_at: clocks.valid_before,
      start_index: 0, end_index: rows.length, previous_cursor: null, next_cursor: next, anchor_video_id: null, left_results: 0,
      ...(listed ? { order: parseOrder(page.order) } : {}) };
  }
  const validBefore = instantMilliseconds(clocks.valid_before);
  const growth = request.select.some((field) => GROWTH.has(field));
  const type = request.video_types[0];
  const videos = rows.map((row, index) => libraryRow(row, `result.rows.${index}`, columns, provenance, type, validBefore, growth));
  if (new Set(videos.map((video) => video.video_id)).size !== videos.length) contractError('result.rows', 'a repeated video.');
  if (!fixed && request.page.anchor_id && videos[0]?.video_id === request.page.anchor_id) browse.anchor_video_id = request.page.anchor_id;
  if (browse.anchor_video_id !== null) {
    if (browse.anchor_video_id !== request.page.anchor_id || !videos.some(video => video.video_id === browse.anchor_video_id)) {
      contractError('result.page.anchor_id', 'not the requested anchor on this page.');
    }
  } else if (!request.page.after && !request.page.ids && browse.start_index !== 0) {
    contractError('result.page.start_index', 'a first page without its anchor starts at zero.');
  }
  if (browse.order?.ids) {
    // The page is the list's slice at its first row: the start, or the requested anchor.
    const start = videos.length ? listPosition(browse.order, videos[0].video_id) : 0;
    if (start < 0 || (start > 0 && browse.anchor_video_id === null) || (!videos.length && browse.order.total > 0)
      || videos.some((video, index) => browse.order!.ids![start + index] !== video.video_id)
      || (browse.next_cursor !== null) !== (start + videos.length < browse.order.total)) {
      contractError('result.page.order', 'the page is not a slice of its list.');
    }
    browse.start_index = start;
    browse.end_index = start + videos.length;
  }
  if (request.page.ids) {
    // A slice: requested videos only, in the requested order; the others left the results.
    const wanted = new Map(request.page.ids.map((id, index) => [id, index]));
    let previous = -1;
    for (const video of videos) {
      const at = wanted.get(video.video_id);
      if (at === undefined || at <= previous) contractError('result.rows', 'not the requested videos in their order.');
      previous = at;
    }
    if (browse.next_cursor !== null) contractError('result.page', 'a slice has no continuation.');
    browse.end_index = request.page.ids.length;
    browse.left_results = request.page.ids.length - videos.length;
  }
  return transferSourceLifetime(raw, {
    contract_version: 1,
    video_type: type,
    channel: { channel_id: expect.channelId },
    videos, browse,
    pagination: { matched_count: browse.total_count ?? browse.order?.total, offset: browse.start_index, returned_count: videos.length,
      has_more_in_scan: browse.next_cursor !== null },
    coverage: { complete: !notice.partial, notice },
    growth,
    columns,
    time_zone: request.time.timezone,
  } satisfies LibraryVideosPayload);
}

/** Identity of a parsed page (`session_key` is its key). */
export function pageIdentity(payload: LibraryVideosPayload): FrozenSessionIdentity {
  if (payload.browse.consistency !== 'frozen') return contractError('result.context', 'expected a fixed list.');
  return {
    query_fingerprint: payload.browse.query_fingerprint, evaluated_at: payload.browse.created_at,
    valid_before: payload.browse.expires_at, total_count: payload.browse.total_count,
  };
}

// Keep the membership index on the current end of a page chain. Branching from
// an earlier page reconstructs that branch's index and leaves both results independent.
const pageIds = new WeakMap<LibraryVideosPayload, Set<string>>();

// What the grid shows as time passes: every loaded row stays in its place.

/**
 * The monotonic end of a wall-clock end of this row's values: the read's source clock (request start on the
 * monotonic clock minus the server's read time) travels in the row's own lifetime. Null: no source clock,
 * the wall clock alone decides. -Infinity: the clock could not be proven, every timed value has ended.
 */
function clockOffset(video: LibraryVideo): number | null {
  const life = ownSourceLifetime(video);
  if (!life) return null;
  return life.expiresAt > 0 ? life.monotonicDeadline - life.expiresAt : -Infinity;
}

/** Either clock passing the end ends the value: a computer that slept, or a clock that was changed. */
function ended(end: number, offset: number | null, now: number, monotonic: number): boolean {
  return now >= end || (offset !== null && monotonic >= end + offset);
}

/** The row with its ended values taken out and marked `expired` ("—", Needs refresh); everything else as read. */
function expiredView(video: LibraryVideo, names: readonly TimedValue[]): LibraryVideo {
  const view: LibraryVideo = { ...video, stale: true };
  const states = { ...video.value_states };
  for (const name of names) {
    delete (view as Record<string, unknown>)[name];
    if (name !== 'tags' && name !== 'view_growth') states[name] = 'expired';
  }
  if (Object.keys(states).length) view.value_states = states;
  transferDiscoveryReceipt(video, view);
  return view;
}

type ShownRows = { videos: LibraryVideo[]; untilNow: number; untilMonotonic: number };
const shownRows = new WeakMap<LibraryVideosPayload, ShownRows>();
const expiredViews = new WeakMap<LibraryVideo, { ended: number; view: LibraryVideo }>();

function shown(payload: LibraryVideosPayload, now: number, monotonic: number): ShownRows {
  const known = shownRows.get(payload);
  if (known && now < known.untilNow && monotonic < known.untilMonotonic) return known;
  let untilNow = Infinity, untilMonotonic = Infinity, changed = false;
  const videos = payload.videos.map((video) => {
    const ends = valueDeadlines.get(video);
    if (!ends) return video;
    const offset = clockOffset(video);
    const names: TimedValue[] = [];
    for (const [name, own] of Object.entries(ends.values) as Array<[TimedValue, number]>) {
      const end = Math.min(own, ends.row);
      if (ended(end, offset, now, monotonic)) names.push(name);
      else {
        untilNow = Math.min(untilNow, end);
        if (offset !== null) untilMonotonic = Math.min(untilMonotonic, end + offset);
      }
    }
    if (!names.length) return video;
    changed = true;
    const cached = expiredViews.get(video);
    if (cached?.ended === names.length) return cached.view;
    const view = expiredView(video, names);
    expiredViews.set(video, { ended: names.length, view });
    return view;
  });
  const result = { videos: changed ? videos : payload.videos, untilNow, untilMonotonic };
  shownRows.set(payload, result);
  return result;
}

/**
 * The rows the grid shows now: every loaded row, in its place. Time alone removes nothing. A value whose own
 * lifetime has ended, and that no later read has replaced, is shown as expired ("—", Needs refresh), never as
 * a current number; the other values of the row stay as read.
 */
export function shownCatalogueVideos(payload: LibraryVideosPayload, now = Date.now(), monotonic = performance.now()): LibraryVideo[] {
  return shown(payload, now, monotonic).videos;
}

/** Milliseconds until the next value of the loaded rows ends (Infinity when none will). */
export function nextCatalogueExpiry(payload: LibraryVideosPayload, now = Date.now(), monotonic = performance.now()): number {
  const state = shown(payload, now, monotonic);
  return Math.max(0, Math.min(state.untilNow - now, state.untilMonotonic - monotonic));
}

/** Loaded rows with a value that has ended or ends within `ahead` milliseconds: the first to be read again. */
export function expiringCatalogueIds(payload: LibraryVideosPayload, ahead: number, now = Date.now(), monotonic = performance.now()): Set<string> {
  const ids = new Set<string>();
  for (const video of payload.videos) {
    const ends = valueDeadlines.get(video);
    if (!ends) continue;
    const first = Math.min(ends.row, ...Object.values(ends.values));
    if (ended(first - ahead, clockOffset(video), now, monotonic)) ids.add(video.video_id);
  }
  return ids;
}

function existingVideoIds(payload: LibraryVideosPayload) {
  return pageIds.get(payload) ?? new Set(payload.videos.map(video => video.video_id));
}

function indexMergedPage(current: LibraryVideosPayload, result: LibraryVideosPayload, loaded: Set<string>, additions: LibraryVideo[]) {
  pageIds.delete(current);
  for (const video of additions) loaded.add(video.video_id);
  pageIds.set(result, loaded);
  return result;
}

/** Merge adjacent pages of one session. Never deduplicate around a gap or mix sessions. */
export function mergeLibraryPages(current: LibraryVideosPayload, page: LibraryVideosPayload, direction: 'next' | 'previous'): LibraryVideosPayload {
  if (current.channel.channel_id !== page.channel.channel_id || current.video_type !== page.video_type
    || current.growth !== page.growth || current.columns.join('\n') !== page.columns.join('\n') || current.time_zone !== page.time_zone) {
    contractError('result', 'the page belongs to another query.');
  }
  const before = current.browse, next = page.browse;
  if (before.consistency === 'current' && next.consistency === 'current') {
    if (direction !== 'next' || !before.next_cursor || before.query_fingerprint !== next.query_fingerprint) {
      contractError('result.page', 'not a continuation of the current query.');
    }
    const loaded = existingVideoIds(current);
    const additions = page.videos.filter(video => !loaded.has(video.video_id));
    const videos = current.videos.concat(additions);
    return indexMergedPage(current, { ...page, videos, browse: { ...before, expires_at: next.expires_at, next_cursor: next.next_cursor,
      end_index: before.end_index + additions.length },
      pagination: { ...page.pagination, matched_count: current.pagination.matched_count, returned_count: videos.length } }, loaded, additions);
  }
  requireSameFrozenSession(pageIdentity(current), pageIdentity(page));
  if (before.consistency !== 'frozen' || next.consistency !== 'frozen') return contractError('result.context', 'incompatible pages.');
  if (direction === 'next'
    ? next.start_index !== before.start_index + current.videos.length
    : next.start_index + page.videos.length !== before.start_index) contractError('result.page', 'not an adjacent page.');
  const ids = existingVideoIds(current);
  if (page.videos.some((video) => ids.has(video.video_id))) contractError('result.rows', 'a video repeated across pages.');
  const videos = direction === 'next' ? current.videos.concat(page.videos) : page.videos.concat(current.videos);
  const browse: LibraryBrowse = {
    ...before,
    frozen_ref: next.frozen_ref,
    start_index: direction === 'previous' ? next.start_index : before.start_index,
    end_index: direction === 'next' ? next.end_index : before.end_index,
    previous_cursor: direction === 'previous' ? next.previous_cursor : before.previous_cursor,
    next_cursor: direction === 'next' ? next.next_cursor : before.next_cursor,
  };
  return indexMergedPage(current, {
    ...current, videos, browse,
    pagination: { ...current.pagination, offset: browse.start_index, returned_count: videos.length, has_more_in_scan: browse.next_cursor !== null },
  }, ids, page.videos);
}

/** The IDs of the list's next or previous slice of `size` around the loaded window (empty at that end). */
export function listSlice(payload: LibraryVideosPayload, direction: 'next' | 'previous', size: number): string[] {
  const { order, start_index: start, end_index: end } = payload.browse;
  if (!order?.ids) return [];
  return direction === 'next' ? order.ids.slice(end, end + size) : order.ids.slice(Math.max(0, start - size), start);
}

/** Whether the list has videos after (next) or before (previous) the loaded window. */
export function listHasMore(payload: LibraryVideosPayload | undefined, direction: 'next' | 'previous'): boolean {
  const browse = payload?.browse;
  if (!browse) return false;
  if (browse.order?.ids) return direction === 'next' ? browse.end_index < browse.order.ids.length : browse.start_index > 0;
  return Boolean(direction === 'next' ? browse.next_cursor : browse.previous_cursor);
}

/**
 * Add one slice of the list (`requested`, read with `page.ids`) next to the loaded window. Nothing is
 * skipped or repeated: the window moves by exactly the requested IDs, and a requested video that is
 * absent (no longer matching or available) is counted, never replaced by another.
 */
export function mergeLibrarySlice(current: LibraryVideosPayload, slice: LibraryVideosPayload, direction: 'next' | 'previous',
  requested: readonly string[]): LibraryVideosPayload {
  if (current.channel.channel_id !== slice.channel.channel_id || current.video_type !== slice.video_type
    || current.growth !== slice.growth || current.columns.join('\n') !== slice.columns.join('\n') || current.time_zone !== slice.time_zone
    || current.browse.query_fingerprint !== slice.browse.query_fingerprint || slice.browse.consistency !== 'current') {
    contractError('result', 'the page belongs to another query.');
  }
  const before = current.browse;
  const expected = listSlice(current, direction, requested.length);
  if (!requested.length || expected.length !== requested.length || expected.some((id, index) => id !== requested[index])) {
    contractError('result.page', 'not the next slice of the list.');
  }
  const loaded = existingVideoIds(current);
  if (slice.videos.some((video) => loaded.has(video.video_id))) contractError('result.rows', 'a video repeated across pages.');
  const videos = direction === 'next' ? current.videos.concat(slice.videos) : slice.videos.concat(current.videos);
  const browse: LibraryBrowse = {
    ...before,
    start_index: direction === 'previous' ? before.start_index - requested.length : before.start_index,
    end_index: direction === 'next' ? before.end_index + requested.length : before.end_index,
    left_results: before.left_results + requested.length - slice.videos.length,
  };
  // The newest read's coverage and lifetime describe the list now; the list itself stays the first page's.
  return indexMergedPage(current, {
    ...slice, videos, browse,
    pagination: { ...slice.pagination, matched_count: before.order?.total, offset: browse.start_index, returned_count: videos.length,
      has_more_in_scan: browse.end_index < (before.order?.ids?.length ?? 0) },
  }, loaded, slice.videos);
}

/**
 * Put a later read of loaded rows (`requested`, read with `page.ids`) in their places: newer values replace
 * older ones, a value the program has nothing newer for arrives as expired. The loaded window, its order and
 * the list are unchanged. A requested row the read no longer has (it stopped matching these filters or is no
 * longer available) keeps its card as it was and is named in `absent`.
 */
export function refreshLibraryRows(current: LibraryVideosPayload, read: LibraryVideosPayload, requested: readonly string[]): {
  payload: LibraryVideosPayload; absent: string[];
} {
  if (current.channel.channel_id !== read.channel.channel_id || current.video_type !== read.video_type
    || current.growth !== read.growth || current.columns.join('\n') !== read.columns.join('\n') || current.time_zone !== read.time_zone
    || current.browse.consistency !== 'current' || read.browse.consistency !== 'current') {
    contractError('result', 'the page belongs to another query.');
  }
  const wanted = new Set(requested);
  const fresh = new Map<string, LibraryVideo>();
  for (const video of read.videos) {
    if (!wanted.has(video.video_id)) contractError('result.rows', 'not the requested videos.');
    fresh.set(video.video_id, video);
  }
  const loaded = existingVideoIds(current);
  const payload: LibraryVideosPayload = { ...current, videos: current.videos.map((video) => fresh.get(video.video_id) ?? video) };
  const index = pageIds.get(current);
  if (index) { pageIds.delete(current); pageIds.set(payload, index); }
  return { payload, absent: requested.filter((id) => loaded.has(id) && !fresh.has(id)) };
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
  const maximum = kind === 'frozen' ? 50_000 : Number.MAX_SAFE_INTEGER;
  const bucketCount = (value: unknown, path: string) => count(value, path, maximum);
  const total = bucketCount(population.count, 'facets.result.population.count');
  const matched = bucketCount(population.matched_count, 'facets.result.population.matched_count');
  if (matched > total) contractError('facets.result.population', 'more matched rows than the population.');
  if (expect.frozenTotal !== undefined && matched !== expect.frozenTotal) contractError('facets.result.population.matched_count', 'not the frozen total.');
  const excluded = openObject(population.excluded, 'facets.result.population.excluded');
  Object.entries(excluded).forEach(([code, value]) => count(value, `facets.result.population.excluded.${code}`));
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
