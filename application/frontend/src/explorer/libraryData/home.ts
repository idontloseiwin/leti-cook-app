// Latest videos across visible channels, ordered by publication time and ID.
// Each page reads current data. Relative pace uses the median over the full period.
import type { ExplorerRecentWindow } from '../model.ts';
import { sourceLifetimeParser } from '../../sourceLifetime.ts';
import { FIELDS, OPERATIONS, QUERY_VERSION, ROUTES, VIDEO_SOURCE, operationRequest, type LibraryVideoType } from './wire.ts';
import {
  array, bool, cellPresentation, closedObject, contractError, count, enumValue, instantMilliseconds, openObject,
  signedDecimal, text, uintString, utcInstant, videoId,
} from './strict.ts';
import { coverageNotice } from './envelope.ts';
import { utcTimestampMicroseconds } from '../contracts.ts';

/** Whole hours of each period (exact durations). */
export const HOME_WINDOW_HOURS: Readonly<Record<ExplorerRecentWindow, number>> = { '24h': 24, '7d': 168, '28d': 672 };
export const HOME_PAGE_SIZE = 20;
/** At most this many already shown videos may share the last publication instant (server list limit). */
export const HOME_MAX_TIED = 1000;
export const HOME_SELECT = [
  FIELDS.id, 'video.channel_id', FIELDS.title, FIELDS.thumbnailUrl, FIELDS.publishedAt,
  FIELDS.views, FIELDS.likes, FIELDS.comments, FIELDS.lifetimeViewsPerDay,
] as const;
export const HOME_PACE_HELP = 'Views per day since publication (age at the last counter check, at least one hour), '
  + 'compared with the median of all videos in this period.';

export type HomeFeedVideo = {
  video_id: string;
  channel_id: string;
  title: string | null;
  thumbnail_url: string | null;
  published_at: string;
  checked_at: string | null;
  views: string | null;
  like_count: string | null;
  comment_count: string | null;
  /** Server pace (video.lifetime_views_per_day), decimal text; null when not known. */
  views_per_day: string | null;
  /** The row's own data deadline (earliest expiry of its known values). */
  source_expires_at: string;
};

export type HomeFeedPage = {
  period: ExplorerRecentWindow;
  video_type: LibraryVideoType;
  generated_at: string;
  published_from: string;
  items: HomeFeedVideo[];
  has_more: boolean;
  /** Opaque key of the next page ("Load 20 more"); never expires. */
  next_cursor: string | null;
  partial: boolean;
};

export type HomePaceMedian = {
  period: ExplorerRecentWindow;
  median: string | null;
  population: number;
  known: number;
  evaluated_at: string;
  valid_before: string;
};

type Key = { published_at: string; tied: string[] };
const KEY_PREFIX = 'home-key-v1:';
const THUMBNAIL = (id: string) => `https://img.youtube.com/vi/${id}/mqdefault.jpg`;

function windowHours(period: ExplorerRecentWindow): number {
  const hours = HOME_WINDOW_HOURS[period];
  if (!hours) throw new Error('Unsupported Latest videos period.');
  return hours;
}

function videoType(value: LibraryVideoType): LibraryVideoType {
  if (!['long', 'short', 'stream'].includes(value)) throw new Error('Unsupported video type.');
  return value;
}

/** Encode the continuation after the last shown row (and every shown row at its instant). */
export function encodeHomeKey(key: Key): string {
  return KEY_PREFIX + JSON.stringify({ p: key.published_at, t: key.tied });
}

export function decodeHomeKey(value: string): Key {
  if (typeof value !== 'string' || !value.startsWith(KEY_PREFIX) || value.length > 64 * 1024) contractError('cursor', 'unknown continuation.');
  let parsed: unknown;
  try { parsed = JSON.parse(value.slice(KEY_PREFIX.length)); } catch { contractError('cursor', 'unknown continuation.'); }
  const key = closedObject(parsed, 'cursor', ['p', 't']);
  const tied = array(key.t, 'cursor.t', HOME_MAX_TIED).map((id, index) => videoId(id, `cursor.t.${index}`));
  if (!tied.length || new Set(tied).size !== tied.length) contractError('cursor.t', 'expected the shown videos.');
  return { published_at: utcInstant(key.p, 'cursor.p'), tied };
}

function windowPredicate(period: ExplorerRecentWindow) {
  return { field: FIELDS.publishedAt, op: 'within_last_hours_v1', value: String(windowHours(period)) };
}

/** The current Home query: first page, or the page after `cursor`. */
export function homeFeedQuery(period: ExplorerRecentWindow, cursor?: string, type: LibraryVideoType = 'long') {
  const key = cursor ? decodeHomeKey(cursor) : undefined;
  const range = windowPredicate(period);
  return {
    query_version: QUERY_VERSION,
    source: { id: VIDEO_SOURCE },
    scope: { kind: 'library' },
    video_types: [videoType(type)],
    select: [...HOME_SELECT],
    where: key
      ? { all: [range, { field: FIELDS.publishedAt, op: 'lte', value: key.published_at },
        { not: { field: FIELDS.id, op: 'in', value: [...key.tied].sort() } }] }
      : range,
    order_by: [{ field: FIELDS.publishedAt, direction: 'desc', nulls: 'last' }, { field: FIELDS.id, direction: 'desc', nulls: 'last' }],
    time: { mode: 'current', timezone: 'UTC' },
    coverage: { mode: 'available', required_fields: [] },
    consistency: 'current',
    page: { size: HOME_PAGE_SIZE },
  };
}

/** The pace median of the whole period (the first page's query without continuation). */
export function homePaceRequest(period: ExplorerRecentWindow, type: LibraryVideoType = 'long') {
  return operationRequest(OPERATIONS.paceMedian, homeFeedQuery(period, undefined, type));
}

/** Byte order of video ids, as the server's `COLLATE "C"`. */
/**
 * The HTTP read envelope (web/middleware.py) the server adds to a research reply
 * whose read registered a deadline: the read clock and an expiry that never
 * outlives the result (as the Inspector and Overview adapters read it).
 */
function readEnvelope(root: Record<string, unknown>, validBefore: string, path: string): void {
  if (root.source_read_at !== undefined && root.source_read_at !== null) utcInstant(root.source_read_at, `${path}.source_read_at`);
  if (root.source_expires_at !== undefined && root.source_expires_at !== null) {
    const expires = utcTimestampMicroseconds(utcInstant(root.source_expires_at, `${path}.source_expires_at`));
    if (expires === null || expires > (utcTimestampMicroseconds(validBefore) ?? -1n)) {
      contractError(`${path}.source_expires_at`, 'the response outlives its result.');
    }
  }
}

function before(left: string, right: string): boolean {
  return left < right;
}

function known(cells: Record<string, unknown>, field: string, path: string) {
  return cellPresentation(cells[field], `${path}.cells.${field}`);
}

function parseHomeFeedPageRaw(value: unknown, period: ExplorerRecentWindow, cursor?: string, type: LibraryVideoType = 'long'): HomeFeedPage {
  const root = closedObject(value, 'result', ['query_version', 'query_fingerprint', 'context', 'columns', 'provenance',
    'rows', 'coverage', 'page'], ['warnings', 'source_read_at', 'source_expires_at']);
  if (root.query_version !== 1) contractError('result.query_version', 'expected 1.');
  const context = openObject(root.context, 'result.context');
  if (context.source_id !== VIDEO_SOURCE) contractError('result.context.source_id', 'expected the video source.');
  enumValue(context.consistency, 'result.context.consistency', ['current'] as const);
  const evaluated = utcInstant(context.evaluated_at, 'result.context.evaluated_at');
  const validBefore = utcInstant(context.valid_before, 'result.context.valid_before');
  if (instantMilliseconds(validBefore) <= instantMilliseconds(evaluated)) contractError('result.context.valid_before', 'already expired.');
  readEnvelope(root, validBefore, 'result');
  const columns = array(root.columns, 'result.columns', 50);
  if (columns.length !== HOME_SELECT.length || HOME_SELECT.some((field, index) => columns[index] !== field)) {
    contractError('result.columns', 'unexpected columns.');
  }
  openObject(root.provenance, 'result.provenance');
  const page = closedObject(root.page, 'result.page', ['has_more', 'next_cursor']);
  const hasMore = bool(page.has_more, 'result.page.has_more');
  if (hasMore !== (page.next_cursor !== null)) contractError('result.page', 'has_more and next_cursor disagree.');
  const partial = coverageNotice(root.coverage).partial;
  const previous = cursor ? decodeHomeKey(cursor) : undefined;
  const hours = windowHours(period);
  const from = new Date(instantMilliseconds(evaluated) - hours * 3_600_000).toISOString();
  const items: HomeFeedVideo[] = [];
  const rows = array(root.rows, 'result.rows', HOME_PAGE_SIZE);
  rows.forEach((value, index) => {
    const path = `result.rows.${index}`;
    const row = closedObject(value, path, ['id', 'cells']);
    const id = videoId(row.id, `${path}.id`);
    const cells = closedObject(row.cells, `${path}.cells`, HOME_SELECT);
    const identity = known(cells, FIELDS.id, path);
    if (identity.state !== 'known' || identity.value !== id) contractError(`${path}.cells.video.id`, 'expected the row id.');
    const channel = known(cells, 'video.channel_id', path);
    if (channel.state !== 'known' || typeof channel.value !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(channel.value)) {
      contractError(`${path}.cells.video.channel_id`, 'expected a channel id.');
    }
    const published = known(cells, FIELDS.publishedAt, path);
    if (published.state !== 'known') contractError(`${path}.cells.video.published_at`, 'a windowed video has a publication time.');
    const publishedAt = utcInstant(published.value, `${path}.cells.video.published_at`);
    if (instantMilliseconds(publishedAt) < instantMilliseconds(from) - 1) contractError(`${path}.cells.video.published_at`, 'outside the period.');
    const title = known(cells, FIELDS.title, path);
    const thumbnail = known(cells, FIELDS.thumbnailUrl, path);
    if (thumbnail.state === 'known' && thumbnail.value !== THUMBNAIL(id)) contractError(`${path}.cells.video.thumbnail_url`, 'unexpected image.');
    const counter = (field: string) => {
      const cell = known(cells, field, path);
      return cell.state === 'known' ? uintString(cell.value, `${path}.cells.${field}`) : null;
    };
    const pace = known(cells, FIELDS.lifetimeViewsPerDay, path);
    const paceValue = pace.state === 'known' ? signedDecimal(pace.value, `${path}.cells.${FIELDS.lifetimeViewsPerDay}`) : null;
    if (paceValue?.startsWith('-')) contractError(`${path}.cells.${FIELDS.lifetimeViewsPerDay}`, 'a pace is never negative.');
    // The row lives by its own data clocks.
    let expires: string | null = null;
    let checkedAt: string | null = null;
    for (const field of HOME_SELECT) {
      const cell = openObject(cells[field], `${path}.cells.${field}`);
      if (cell.state !== 'known') continue;
      const expiry = utcInstant(cell.expires_at, `${path}.cells.${field}.expires_at`);
      if (expires === null || instantMilliseconds(expiry) < instantMilliseconds(expires)) expires = expiry;
      if (field === FIELDS.views) checkedAt = utcInstant(cell.observed_at, `${path}.cells.${field}.observed_at`);
    }
    if (expires === null) contractError(path, 'a row without known values.');
    const last = items[items.length - 1];
    if (last && (last.video_id === id || instantMilliseconds(publishedAt) > instantMilliseconds(last.published_at)
      || (publishedAt === last.published_at && !before(id, last.video_id)))) {
      contractError(path, 'rows out of order.');
    }
    if (!last && previous && (instantMilliseconds(publishedAt) > instantMilliseconds(previous.published_at)
      || previous.tied.includes(id))) contractError(path, 'the page repeats shown videos.');
    items.push({
      video_id: id,
      channel_id: channel.value as string,
      title: title.state === 'known' ? text(title.value, `${path}.cells.video.title`, 1000) : null,
      thumbnail_url: thumbnail.state === 'known' ? THUMBNAIL(id) : null,
      published_at: publishedAt,
      checked_at: checkedAt,
      views: counter(FIELDS.views),
      like_count: counter(FIELDS.likes),
      comment_count: counter(FIELDS.comments),
      views_per_day: paceValue,
      source_expires_at: expires as string,
    });
  });
  if (hasMore && !items.length) contractError('result.page', 'more rows without a page.');
  let nextCursor: string | null = null;
  if (hasMore) {
    const tail = items[items.length - 1];
    const tied = items.filter((item) => item.published_at === tail.published_at).map((item) => item.video_id);
    if (previous && previous.published_at === tail.published_at) tied.push(...previous.tied);
    if (tied.length > HOME_MAX_TIED) contractError('result.page', 'too many videos share one publication time.');
    nextCursor = encodeHomeKey({ published_at: tail.published_at, tied });
  }
  return {
    period, video_type: type, generated_at: evaluated, published_from: from, items, has_more: hasMore,
    next_cursor: nextCursor, partial,
  };
}

function parseHomePaceRaw(value: unknown, period: ExplorerRecentWindow): HomePaceMedian {
  const root = closedObject(value, 'operation', ['version', 'operation', 'operation_fingerprint', 'context', 'coverage',
    'result', 'provenance'], ['warnings', 'source_read_at', 'source_expires_at']);
  if (root.version !== 1 || root.operation !== OPERATIONS.paceMedian) contractError('operation', 'unexpected operation.');
  const context = openObject(root.context, 'operation.context');
  const evaluated = utcInstant(context.evaluated_at, 'operation.context.evaluated_at');
  const validBefore = utcInstant(context.valid_before, 'operation.context.valid_before');
  if (instantMilliseconds(validBefore) <= instantMilliseconds(evaluated)) contractError('operation.context.valid_before', 'already expired.');
  readEnvelope(root, validBefore, 'operation');
  coverageNotice(root.coverage);
  const result = closedObject(root.result, 'operation.result', ['field', 'method', 'population_count', 'known_count', 'missing', 'definition', 'median']);
  if (result.field !== FIELDS.lifetimeViewsPerDay) contractError('operation.result.field', 'unexpected field.');
  text(result.method, 'operation.result.method', 96);
  const population = count(result.population_count, 'operation.result.population_count');
  const knownCount = count(result.known_count, 'operation.result.known_count');
  const missing = closedObject(result.missing, 'operation.result.missing', ['unknown', 'expired', 'unavailable']);
  const missingCount = ['unknown', 'expired', 'unavailable'].reduce((sum, state) => sum + count(missing[state], `operation.result.missing.${state}`), 0);
  // Values whose views have no recorded definition (or two definitions in the period) are left out and counted.
  const definition = closedObject(result.definition, 'operation.result.definition', ['state', 'epoch', 'excluded_count']);
  const definitionState = enumValue(definition.state, 'operation.result.definition.state',
    ['known', 'mixed_definition_epochs', 'unknown_definition_epoch', 'no_known_values'] as const);
  if (definition.epoch !== null) text(definition.epoch, 'operation.result.definition.epoch', 128);
  const excluded = count(definition.excluded_count, 'operation.result.definition.excluded_count');
  if ((definitionState === 'known') !== (definition.epoch !== null) || (definitionState !== 'known' && knownCount > 0)) {
    contractError('operation.result.definition', 'unexpected definition state.');
  }
  if (knownCount + excluded + missingCount !== population) contractError('operation.result', 'counts disagree.');
  const median = result.median === null ? null : signedDecimal(result.median, 'operation.result.median');
  if ((median === null) !== (knownCount === 0) || median?.startsWith('-')) contractError('operation.result.median', 'unexpected median.');
  return { period, median, population, known: knownCount, evaluated_at: evaluated, valid_before: validBefore };
}

export const parseHomeFeedPage = sourceLifetimeParser(parseHomeFeedPageRaw);
export const parseHomePace = sourceLifetimeParser(parseHomePaceRaw);

type RequestJson = (path: string, signal?: AbortSignal, init?: RequestInit) => Promise<unknown>;

function post(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

/** One Home page: the first one, or the next after `cursor`. */
export async function loadHomeFeedPage(
  request: RequestJson, period: ExplorerRecentWindow, cursor: string | undefined, signal?: AbortSignal,
  type: LibraryVideoType = 'long',
): Promise<HomeFeedPage> {
  const body = homeFeedQuery(period, cursor, type);
  return parseHomeFeedPage(await request(ROUTES.execute, signal, post(body)), period, cursor, type);
}

/** The median pace of every video of the period. */
export async function loadHomePace(
  request: RequestJson, period: ExplorerRecentWindow, signal?: AbortSignal, type: LibraryVideoType = 'long',
): Promise<HomePaceMedian> {
  return parseHomePace(await request(ROUTES.operations, signal, post(homePaceRequest(period, type))), period);
}

/** "N× median": a row's pace over the period median; undefined without both. */
export function paceRatio(viewsPerDay: string | null | undefined, median: string | null | undefined): number | undefined {
  if (viewsPerDay == null || median == null) return undefined;
  const pace = Number(viewsPerDay), middle = Number(median);
  return Number.isFinite(pace) && Number.isFinite(middle) && pace >= 0 && middle > 0 ? pace / middle : undefined;
}
