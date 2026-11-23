// Inspector adapter. The four per-video
// operations for the Inspector — static details, observed history, launch comparison and
// lifetime baseline — become the presentation the existing Inspector panels
// draw, for Long, Shorts and Streams alike.
//
// Opening: from a frozen Library page the population is that frozen
// result (`population: {kind: "frozen", frozen_ref}`): membership, values and
// the clock are the browsed ones. From a saved collection (or any place without
// a browse session) it is the current query (`population: {kind: "query"}`).
// Launch and baseline always send the channel query without conditions.
//
// Strict: an unknown key, enumeration value or broken
// invariant is an ExplorerContractError; nothing is shown partially. Counters
// stay decimal strings (BigInt arithmetic only for checks). Pure TypeScript:
// no React, window or network — the caller passes the request function.
import type { ChannelVideoViewGrowth } from '../../types.ts';
import { ExplorerContractError, utcTimestampMicroseconds } from '../contracts.ts';
import { transferSourceLifetime } from '../../sourceLifetime.ts';
import type { HistoryIntervalDefinition, HistoryViewSeries, VideoHistoryView } from '../videoHistory.ts';
import type { LaunchComparisonView, LaunchHorizonView, VideoLaunchView } from '../videoLaunch.ts';
import type { LifetimeBaselineView } from '../videoLifetimeBaseline.ts';
import { FIELDS, LIBRARY_VIDEO_TYPES, OPERATIONS, ROUTES, VIDEO_SOURCE, type LibraryVideoType } from './wire.ts';
import {
  array, closedObject, contractError, count, enumValue, openObject, text, uintString, utcInstant, videoId, CELL_STATES,
  type CellState, type JsonRecord,
} from './strict.ts';
import { coverageNotice, type CoverageNotice, type InspectorContext } from './envelope.ts';
import { libraryErrorState, PREPARING_DELAY_MS, PREPARING_RETRIES } from './errors.ts';
import type { LibraryGates } from './capabilities.ts';

/** Operation ids (history and launch are the working names already in wire.ts). */
export const INSPECTOR_OPERATIONS = {
  staticDetails: 'video.static_details.v1',
  history: OPERATIONS.observedHistory,
  launch: OPERATIONS.launchComparison,
  baseline: 'video.lifetime_baseline.v1',
} as const;
export type InspectorOperation = typeof INSPECTOR_OPERATIONS[keyof typeof INSPECTOR_OPERATIONS];

/**
 * Fields a frozen Library page must select so that every Inspector value is
 * the browsed one. A field missing from the frozen row is read
 * current instead (the result names the frozen ones in `inspection.frozen_fields`).
 */
export const INSPECTOR_FROZEN_FIELDS = [
  FIELDS.id, FIELDS.title, FIELDS.thumbnailUrl, FIELDS.durationSeconds, FIELDS.views, FIELDS.likes,
  FIELDS.comments, FIELDS.publishedAt, FIELDS.categoryId, 'video.tags',
] as const;

// ---------------------------------------------------------------------------
// Opening context, gates, requests

/** A frozen Library page; `pending`: the session is being prepared. */
export type FrozenInspectorOpening = InspectorContext & { kind?: 'frozen'; pending?: boolean; timeZone?: string };
/** No browse session: a saved collection or the channel's current population. */
export type QueryInspectorOpening = {
  kind: 'query';
  channelId: string;
  videoType: LibraryVideoType;
  scope: { kind: 'channel'; id: string } | { kind: 'collection'; id: string };
  checkedAt: string | null;
  active: boolean;
  pending?: boolean;
  timeZone?: string;
};
export type InspectorOpening = FrozenInspectorOpening | QueryInspectorOpening;

/** Which Inspector panels this server allows for the opened type. */
export type InspectorGates = {
  staticDetails: boolean;
  baseline: boolean;
  /** LAUNCH ∧ BASELINE ∧ DERIVED; hide the panel when the gate is closed. */
  launch: boolean;
  history: boolean;
  /** `video.description` enabled for the type; read with static details. */
  description: boolean;
  growth: boolean;
};

export function inspectorGates(gates: LibraryGates): InspectorGates {
  const staticDetails = gates.operation(INSPECTOR_OPERATIONS.staticDetails);
  return {
    staticDetails,
    baseline: gates.operation(INSPECTOR_OPERATIONS.baseline),
    launch: gates.operation(INSPECTOR_OPERATIONS.launch),
    history: gates.operation(INSPECTOR_OPERATIONS.history),
    description: staticDetails && gates.field(FIELDS.description),
    growth: gates.growth,
  };
}

const CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/;
const COLLECTION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const FROZEN_REF = /^[A-Za-z0-9_-]{1,4096}$/;

function isQueryOpening(opening: InspectorOpening): opening is QueryInspectorOpening {
  return opening.kind === 'query';
}

export function inspectorMode(opening: InspectorOpening): 'frozen' | 'query' {
  return isQueryOpening(opening) ? 'query' : 'frozen';
}

function checkedOpening(opening: InspectorOpening): InspectorOpening {
  if (!CHANNEL_ID.test(opening.channelId)) throw new Error('The Inspector needs the channel of the video.');
  if (!LIBRARY_VIDEO_TYPES.includes(opening.videoType)) throw new Error('Unsupported video type.');
  if (isQueryOpening(opening)) {
    const scope = opening.scope;
    if (scope.kind === 'channel' ? scope.id !== opening.channelId : !COLLECTION_ID.test(scope.id)) {
      throw new Error('Unsupported Inspector scope.');
    }
  } else if (!FROZEN_REF.test(opening.frozenRef)) {
    throw new Error('A frozen reference is required.');
  }
  return opening;
}

/** The population query of one operation: no conditions, no paging. */
function populationQuery(opening: InspectorOpening, channelOnly: boolean) {
  const scope = !channelOnly && isQueryOpening(opening) ? opening.scope : { kind: 'channel' as const, id: opening.channelId };
  return {
    query_version: 1 as const,
    source: { id: VIDEO_SOURCE },
    scope: { kind: scope.kind, id: scope.id },
    video_types: [opening.videoType],
    select: [FIELDS.id],
    order_by: [{ field: FIELDS.id, direction: 'asc' as const }],
  };
}

export type InspectorRequestBody = {
  version: 1;
  operation: InspectorOperation;
  query: ReturnType<typeof populationQuery>;
  parameters: { video_id: string; population: { kind: 'query' } | { kind: 'frozen'; frozen_ref: string }; description?: boolean };
};

/**
 * One Inspector operation request. Launch and baseline read the channel's
 * whole type population (never the Library conditions). A frozen opening
 * uses the channel query too: the server checks that the frozen result has
 * this query's source, scope and type (else `cursor_context_changed`).
 */
export function inspectorRequest(
  operation: InspectorOperation,
  opening: InspectorOpening,
  video: string,
  options: { description?: boolean } = {},
): InspectorRequestBody {
  checkedOpening(opening);
  videoId(video, 'video_id');
  const channelOnly = operation === INSPECTOR_OPERATIONS.launch || operation === INSPECTOR_OPERATIONS.baseline;
  const population = isQueryOpening(opening)
    ? { kind: 'query' as const }
    : { kind: 'frozen' as const, frozen_ref: opening.frozenRef };
  if (options.description !== undefined && operation !== INSPECTOR_OPERATIONS.staticDetails) {
    throw new Error('Only static details read the description.');
  }
  return {
    version: 1,
    operation,
    query: populationQuery(opening, channelOnly),
    parameters: { video_id: video, population, ...(options.description ? { description: true } : {}) },
  };
}

// ---------------------------------------------------------------------------
// Exact arithmetic (checks only; nothing is calculated for display here)

const MICROSECOND = 1_000_000n;
const WINDOW_MICROSECONDS = 672n * 3600n * MICROSECOND;

function micros(value: string): bigint {
  const parsed = utcTimestampMicroseconds(value);
  if (parsed === null) return contractError('instant', 'expected a UTC timestamp.');
  return parsed;
}

function roundHalfEven(numerator: bigint, denominator: bigint): bigint {
  const negative = numerator < 0n;
  const magnitude = negative ? -numerator : numerator;
  let quotient = magnitude / denominator;
  const remainder = magnitude % denominator;
  if (remainder * 2n > denominator || (remainder * 2n === denominator && quotient % 2n === 1n)) quotient += 1n;
  return negative ? -quotient : quotient;
}

function decimalText(scaled: bigint, places: number): string {
  const unit = 10n ** BigInt(places);
  const negative = scaled < 0n;
  const magnitude = negative ? -scaled : scaled;
  const fraction = (magnitude % unit).toString().padStart(places, '0').replace(/0+$/, '');
  return `${negative ? '-' : ''}${magnitude / unit}${fraction ? `.${fraction}` : ''}`;
}

/** Signed delta per exact elapsed time × one day, 2 places, ties to even (server `per_day`). */
export function exactPerDay(delta: bigint, elapsedMicroseconds: bigint): string {
  return decimalText(roundHalfEven(delta * 86_400n * MICROSECOND * 100n, elapsedMicroseconds), 2);
}

/** counter × 100 / views, 6 places, ties to even (server `_rate`). */
export function exactRate(counter: bigint, views: bigint): string {
  return decimalText(roundHalfEven(counter * 100n * 1_000_000n, views), 6);
}

/** Exact seconds from microseconds ('3600', '3600.5'), as the server writes them. */
export function secondsText(value: bigint): string {
  return decimalText(value, 6);
}

/** Quartile/median strings of integer counters: multiples of 0.25 (`quarters`, `round_summary`). */
function quarterHundredths(value: unknown, path: string): bigint {
  const match = typeof value === 'string' ? /^(0|[1-9][0-9]{0,19})(?:\.([0-9]{1,2}))?$/.exec(value) : null;
  if (!match) return contractError(path, 'expected a quartile.');
  const hundredths = BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0'));
  if (hundredths % 25n !== 0n) contractError(path, 'expected a multiple of 0.25.');
  return hundredths;
}

// ---------------------------------------------------------------------------
// Envelope and shared result parts

const ENVELOPE = ['version', 'operation', 'operation_fingerprint', 'context', 'coverage', 'result', 'provenance', 'warnings'];
const CONTEXT = ['source_id', 'registry_version', 'evaluated_at', 'valid_before', 'preferences_revision', 'consistency',
  'observation_age_limit_seconds'];

type Envelope = { evaluatedAt: string; validBefore: string; coverage: CoverageNotice; result: unknown };

function operationEnvelope(value: unknown, operation: InspectorOperation): Envelope {
  const root = closedObject(value, 'operation', ENVELOPE, ['source_read_at', 'source_expires_at']);
  if (root.version !== 1) contractError('operation.version', 'expected 1.');
  if (root.operation !== operation) contractError('operation.operation', `expected ${operation}.`);
  if (!/^[a-f0-9]{64}$/.test(text(root.operation_fingerprint, 'operation.operation_fingerprint', 64))) {
    contractError('operation.operation_fingerprint', 'expected a fingerprint.');
  }
  const context = closedObject(root.context, 'operation.context', CONTEXT, ['time_zone']);
  if (context.source_id !== VIDEO_SOURCE) contractError('operation.context.source_id', 'unexpected source.');
  if (count(context.registry_version, 'operation.context.registry_version') < 1) contractError('operation.context.registry_version', 'expected a version.');
  if (!text(context.preferences_revision, 'operation.context.preferences_revision', 96)) contractError('operation.context.preferences_revision', 'expected a revision.');
  enumValue(context.consistency, 'operation.context.consistency', ['current']);
  if (context.observation_age_limit_seconds !== null) count(context.observation_age_limit_seconds, 'operation.context.observation_age_limit_seconds');
  if (context.time_zone !== undefined) text(context.time_zone, 'operation.context.time_zone', 64);
  const evaluatedAt = utcInstant(context.evaluated_at, 'operation.context.evaluated_at');
  const validBefore = utcInstant(context.valid_before, 'operation.context.valid_before');
  if (micros(validBefore) <= micros(evaluatedAt)) contractError('operation.context.valid_before', 'the result must end after it was evaluated.');
  // The HTTP envelope (middleware) may add the read clock; it never outlives the result.
  if (root.source_read_at !== undefined && root.source_read_at !== null) utcInstant(root.source_read_at, 'operation.source_read_at');
  if (root.source_expires_at !== undefined && root.source_expires_at !== null
    && micros(utcInstant(root.source_expires_at, 'operation.source_expires_at')) > micros(validBefore)) {
    contractError('operation.source_expires_at', 'the response outlives its result.');
  }
  const coverageValue = closedObject(root.coverage, 'operation.coverage', ['status', 'channels_in_scope', 'channels_complete', 'reasons'],
    ['basis', 'eligible_count']);
  const inScope = count(coverageValue.channels_in_scope, 'operation.coverage.channels_in_scope');
  if (count(coverageValue.channels_complete, 'operation.coverage.channels_complete') > inScope) {
    contractError('operation.coverage.channels_complete', 'more complete channels than channels in scope.');
  }
  if (coverageValue.basis !== undefined) {
    enumValue(coverageValue.basis, 'operation.coverage.basis', ['channel_inventory', 'collection_members', 'channel_profiles']);
  }
  if (coverageValue.eligible_count !== undefined && coverageValue.eligible_count !== null) count(coverageValue.eligible_count, 'operation.coverage.eligible_count');
  const coverage = coverageNotice(coverageValue);
  if (!coverage.partial && (coverage.withheldRows || coverage.staleValueRows || coverage.inventoryIncomplete || coverage.other.length)) {
    contractError('operation.coverage', 'a complete scope has no limitations.');
  }
  const provenance = closedObject(root.provenance, 'operation.provenance',
    ['origin', 'method', 'population', 'missing_values', 'definition_epoch'], ['source_paths', 'type_rule']);
  enumValue(provenance.origin, 'operation.provenance.origin', ['calculated']);
  if (provenance.method !== operation) contractError('operation.provenance.method', `expected ${operation}.`);
  text(provenance.population, 'operation.provenance.population', 96);
  text(provenance.missing_values, 'operation.provenance.missing_values', 96);
  if (provenance.definition_epoch !== null) text(provenance.definition_epoch, 'operation.provenance.definition_epoch', 96);
  array(root.warnings, 'operation.warnings', 32).forEach((item, index) => text(item, `operation.warnings.${index}`, 96));
  return { evaluatedAt, validBefore, coverage, result: root.result };
}

/** What the caller expects back: the opened video, its type and the opening mode. */
export type InspectorExpectation = { videoId: string; videoType: LibraryVideoType; mode: 'frozen' | 'query'; description?: boolean };

type Head = {
  membership: 'member' | 'not_in_population';
  /** Inspection clock: the frozen evaluation time, else the statement's. */
  clock: string;
  frozenFields: string[] | null;
};

const HEAD = ['video_id', 'video_type', 'membership', 'inspection'];

function resultHead(result: JsonRecord, path: string, expect: InspectorExpectation, envelope: Envelope): Head {
  if (videoId(result.video_id, `${path}.video_id`) !== expect.videoId) contractError(`${path}.video_id`, 'another video.');
  if (result.video_type !== expect.videoType) contractError(`${path}.video_type`, 'another video type.');
  const membership = enumValue(result.membership, `${path}.membership`, ['member', 'not_in_population']);
  // The answer must come from the population the panel asked for (frozen page or current query).
  if (openObject(result.inspection, `${path}.inspection`).mode !== expect.mode) contractError(`${path}.inspection.mode`, `expected ${expect.mode}.`);
  if (expect.mode === 'query') {
    closedObject(result.inspection, `${path}.inspection`, ['mode']);
    return { membership, clock: envelope.evaluatedAt, frozenFields: null };
  }
  const inspection = closedObject(result.inspection, `${path}.inspection`,
    ['mode', 'evaluated_at', 'valid_before', 'position', 'total_count', 'frozen_fields']);
  const evaluated = utcInstant(inspection.evaluated_at, `${path}.inspection.evaluated_at`);
  const valid = utcInstant(inspection.valid_before, `${path}.inspection.valid_before`);
  if (micros(valid) <= micros(evaluated)) contractError(`${path}.inspection.valid_before`, 'the frozen result must end after it was evaluated.');
  // The answer lives no longer than the frozen result it was read from.
  if (micros(envelope.validBefore) > micros(valid)) contractError('operation.context.valid_before', 'outlives the frozen result.');
  const total = count(inspection.total_count, `${path}.inspection.total_count`, 50_000);
  if (count(inspection.position, `${path}.inspection.position`) >= total) contractError(`${path}.inspection.position`, 'outside the frozen result.');
  const fields = array(inspection.frozen_fields, `${path}.inspection.frozen_fields`, 50)
    .map((item, index) => text(item, `${path}.inspection.frozen_fields.${index}`, 96));
  if (fields.some((item, index) => !/^[a-z][a-z0-9_.]*$/.test(item) || (index > 0 && item <= fields[index - 1]))) {
    contractError(`${path}.inspection.frozen_fields`, 'expected sorted field ids.');
  }
  return { membership, clock: evaluated, frozenFields: fields };
}

/** One value of the panel: only a known cell carries a value. */
export type InspectorValue<T> = { state: 'known'; value: T } | { state: Exclude<CellState, 'known'> };

type ParsedCell<T> = { value: InspectorValue<T>; observedAt: string | null; expiresAt: string | null };

function inspectorCell<T>(value: unknown, path: string, read: (item: unknown, at: string) => T): ParsedCell<T> {
  const cell = closedObject(value, path, ['value', 'state', 'observed_at', 'expires_at']);
  const state = enumValue(cell.state, `${path}.state`, CELL_STATES);
  const observedAt = cell.observed_at === null ? null : utcInstant(cell.observed_at, `${path}.observed_at`);
  const expiresAt = cell.expires_at === null ? null : utcInstant(cell.expires_at, `${path}.expires_at`);
  if (observedAt !== null && expiresAt !== null && micros(observedAt) >= micros(expiresAt)) {
    contractError(path, 'the value expires before it was observed.');
  }
  if (state === 'known') {
    if (cell.value === null || observedAt === null || expiresAt === null) contractError(path, 'a known value needs its value and clocks.');
    return { value: { state, value: read(cell.value, `${path}.value`) }, observedAt, expiresAt };
  }
  if (cell.value !== null) contractError(`${path}.value`, `a ${state} value carries no value.`);
  return { value: { state }, observedAt, expiresAt };
}

const uintNumber = (maximum: number) => (item: unknown, at: string): number => {
  const parsed = BigInt(uintString(item, at));
  if (parsed > BigInt(maximum)) contractError(at, 'out of range.');
  return Number(parsed);
};
const anyText = (maximum: number) => (item: unknown, at: string) => text(item, at, maximum);

/** The parse result when the inspected video is not in the population (a state, not an error). */
export class InspectorNotInPopulation extends Error {
  constructor() {
    super('This video is not in the current results.');
    this.name = 'InspectorNotInPopulation';
  }
}

// ---------------------------------------------------------------------------
// video.static_details.v1

export type StaticDetailsView = {
  video_type: LibraryVideoType;
  like_rate: InspectorValue<string>;
  comment_rate: InspectorValue<string>;
  title_length: InspectorValue<number>;
  tag_count: InspectorValue<number>;
  tags: InspectorValue<string[]>;
  metadata_checked_at: string | null;
  /** Null when the description was not requested (field off for the type). */
  description: InspectorValue<string> | null;
};

const STATIC_FIELDS = ['video.id', 'video.title', 'video.thumbnail_url', 'video.duration_seconds', 'video.views',
  'video.likes', 'video.comments', 'video.published_at', 'video.category_id', 'video.tags'];
const STATIC_METHODS = {
  rates: 'counter_times_100_divided_by_views_6dp_half_even', zero_views: 'unavailable', title_length: 'unicode_code_points',
  tag_count: 'stored_array_length', duration: 'youtube_duration_seconds_v1_integer', stale_values: 'unknown_or_expired_row_kept',
};
const RATE = /^(0|[1-9][0-9]{0,24})(?:\.[0-9]{1,6})?$/;

function exactMethods(value: unknown, path: string, expected: Record<string, string>): void {
  const methods = closedObject(value, path, Object.keys(expected));
  for (const [key, literal] of Object.entries(expected)) {
    if (methods[key] !== literal) contractError(`${path}.${key}`, `expected ${literal}.`);
  }
}

function sameState<T>(derived: InspectorValue<T>, source: InspectorValue<unknown>, path: string): void {
  if (derived.state !== source.state) contractError(path, 'its state differs from its source value.');
}

export function parseStaticDetails(raw: unknown, expect: InspectorExpectation): StaticDetailsView {
  const envelope = operationEnvelope(raw, INSPECTOR_OPERATIONS.staticDetails);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, [...HEAD, 'cells', 'derived', 'category', 'clocks', 'methods']);
  const head = resultHead(result, path, expect, envelope);
  exactMethods(result.methods, `${path}.methods`, STATIC_METHODS);
  if (result.cells === null) {
    // Without a frozen row a non-member has nothing to show.
    if (head.membership !== 'not_in_population' || expect.mode !== 'query'
      || result.derived !== null || result.category !== null || result.clocks !== null) contractError(path, 'missing values.');
    throw new InspectorNotInPopulation();
  }
  const fields = expect.description ? [...STATIC_FIELDS, FIELDS.description] : STATIC_FIELDS;
  const cells = closedObject(result.cells, `${path}.cells`, fields);
  const read = <T>(field: string, reader: (item: unknown, at: string) => T) => inspectorCell(cells[field], `${path}.cells.${field}`, reader);
  const id = read('video.id', (item, at) => videoId(item, at));
  if (id.value.state === 'known' && id.value.value !== expect.videoId) contractError(`${path}.cells.video.id`, 'another video.');
  const title = read('video.title', anyText(4096));
  read('video.thumbnail_url', anyText(2048));
  read('video.duration_seconds', (item, at) => uintString(item, at));
  const views = read('video.views', (item, at) => uintString(item, at));
  const likes = read('video.likes', (item, at) => uintString(item, at));
  const comments = read('video.comments', (item, at) => uintString(item, at));
  read('video.published_at', (item, at) => utcInstant(item, at));
  const category = read('video.category_id', anyText(64));
  const tags = read('video.tags', (item, at) => array(item, at, 500).map((tag, index) => text(tag, `${at}.${index}`, 25_600)));
  const description = expect.description ? read(FIELDS.description, anyText(5000)) : null;

  const derived = closedObject(result.derived, `${path}.derived`, ['video.like_rate', 'video.comment_rate', 'video.title_length', 'video.tag_count']);
  const rate = (item: unknown, at: string) => {
    if (typeof item !== 'string' || !RATE.test(item)) return contractError(at, 'expected a rate.');
    return item;
  };
  const likeRate = inspectorCell(derived['video.like_rate'], `${path}.derived.video.like_rate`, rate);
  const commentRate = inspectorCell(derived['video.comment_rate'], `${path}.derived.video.comment_rate`, rate);
  const titleLength = inspectorCell(derived['video.title_length'], `${path}.derived.video.title_length`, uintNumber(100_000));
  const tagCount = inspectorCell(derived['video.tag_count'], `${path}.derived.video.tag_count`, uintNumber(500));
  sameState(titleLength.value, title.value, `${path}.derived.video.title_length`);
  sameState(tagCount.value, tags.value, `${path}.derived.video.tag_count`);
  if (titleLength.value.state === 'known' && title.value.state === 'known'
    && titleLength.value.value !== [...title.value.value].length) contractError(`${path}.derived.video.title_length`, 'not the title length.');
  if (tagCount.value.state === 'known' && tags.value.state === 'known'
    && tagCount.value.value !== tags.value.value.length) contractError(`${path}.derived.video.tag_count`, 'not the tag count.');
  // The count comes from the same read as the shown tags (both frozen, or both current
  // when the list froze only the count for its group counts), so it carries the tags' clock.
  if (tagCount.observedAt !== tags.observedAt) contractError(`${path}.derived.video.tag_count`, 'not read with the tags.');
  // The rates are calculated from the shown counters when they are all frozen,
  // or all current (query mode); a mixed frozen row is never mixed in one rate.
  const countersShown = expect.mode === 'query'
    || ['video.views', 'video.likes', 'video.comments'].every((field) => head.frozenFields?.includes(field));
  if (countersShown) {
    for (const [name, value, counter] of [['like_rate', likeRate.value, likes.value], ['comment_rate', commentRate.value, comments.value]] as const) {
      const at = `${path}.derived.video.${name}`;
      if (counter.state === 'known' && views.value.state === 'known') {
        const total = BigInt(views.value.value);
        if (total === 0n ? value.state !== 'unavailable'
          : value.state !== 'known' || value.value !== exactRate(BigInt(counter.value), total)) contractError(at, 'not the stored rate.');
      } else if (value.state === 'known') {
        contractError(at, 'a rate needs known counters.');
      }
    }
  }
  const categoryValue = closedObject(result.category, `${path}.category`, ['id', 'label']);
  if (categoryValue.id !== (category.value.state === 'known' ? category.value.value : null)) contractError(`${path}.category.id`, 'another category.');
  if (categoryValue.label !== null) text(categoryValue.label, `${path}.category.label`, 200);
  const clocks = closedObject(result.clocks, `${path}.clocks`, ['counters_checked_at', 'metadata_checked_at']);
  if (clocks.counters_checked_at !== views.observedAt) contractError(`${path}.clocks.counters_checked_at`, 'not the counter clock.');
  if (clocks.metadata_checked_at !== title.observedAt) contractError(`${path}.clocks.metadata_checked_at`, 'not the metadata clock.');
  return transferSourceLifetime(raw, {
    video_type: expect.videoType,
    like_rate: likeRate.value,
    comment_rate: commentRate.value,
    title_length: titleLength.value,
    tag_count: tagCount.value,
    tags: tags.value,
    metadata_checked_at: title.observedAt,
    description: description ? description.value : null,
  });
}

// ---------------------------------------------------------------------------
// video.observed_history.v1

const HISTORY_METHODS = {
  ordering: 'recorded_at_ascending', same_timestamp: 'identical_values_collapsed_conflicts_unavailable',
  gaps: 'no_interpolation_or_daily_allocation', rate: 'signed_delta_per_exact_elapsed_second_times_86400_rounded_2dp_half_even',
  window: 'evaluated_at_minus_28_days_through_counter_observation', views_definition: 'interval_requires_one_known_definition_epoch',
  streams: 'observations_from_actual_end', interaction_metrics: 'independent_stored_counters_same_observation_window',
};
export const HISTORY_UNAVAILABLE_REASONS = ['endpoint_snapshot_missing', 'endpoint_counter_mismatch', 'invalid_catalogue_endpoint',
  'catalogue_endpoint_outside_hot_window', 'conflicting_simultaneous_observations', 'observation_limit_exceeded',
  'video_not_in_population'] as const;
const HISTORY_ROW_LIMIT = 500;
const HISTORY_METRICS = ['views', 'likes', 'comments'] as const;
type HistoryMetric = typeof HISTORY_METRICS[number];

type WirePoint = { at: bigint; recorded_at: string; values: Record<HistoryMetric, string | null>; epoch: string | null };

function historySeries(value: unknown, path: string, metric: HistoryMetric, points: WirePoint[], scanned: number): HistoryViewSeries {
  const series = closedObject(value, path, ['status', 'reason', 'intervals', 'latest_interval', 'coverage']);
  const status = enumValue(series.status, `${path}.status`, ['ready', 'unavailable']);
  const coverage = closedObject(series.coverage, `${path}.coverage`, ['point_count', 'valid_count', 'missing_count', 'duplicate_timestamp_count']);
  const pointCount = count(coverage.point_count, `${path}.coverage.point_count`, HISTORY_ROW_LIMIT);
  const valid = count(coverage.valid_count, `${path}.coverage.valid_count`, HISTORY_ROW_LIMIT);
  const missing = count(coverage.missing_count, `${path}.coverage.missing_count`, HISTORY_ROW_LIMIT);
  const duplicates = count(coverage.duplicate_timestamp_count, `${path}.coverage.duplicate_timestamp_count`, HISTORY_ROW_LIMIT + 1);
  const intervalsValue = array(series.intervals, `${path}.intervals`, HISTORY_ROW_LIMIT - 1);
  const shape = { point_count: pointCount, valid_count: valid, missing_count: missing, duplicate_timestamp_count: duplicates };
  if (status === 'unavailable') {
    const reason = enumValue(series.reason, `${path}.reason`, HISTORY_UNAVAILABLE_REASONS);
    if (intervalsValue.length || series.latest_interval !== null || pointCount || valid || missing
      || duplicates > Math.max(0, scanned - points.length)) contractError(path, 'an unavailable series carries no values.');
    if (points.some((point) => point.values[metric] !== null)) contractError(path, 'an unavailable series has no points.');
    return { status, reason, points: [], intervals: [], latest_interval: null, coverage: shape };
  }
  if (series.reason !== null) contractError(`${path}.reason`, 'a ready series has no reason.');
  const known = points.filter((point) => point.values[metric] !== null).length;
  if (!points.length || pointCount !== points.length || valid !== known || missing !== points.length - known
    || duplicates !== scanned - points.length || intervalsValue.length !== points.length - 1) contractError(`${path}.coverage`, 'does not match the points.');
  const intervals = intervalsValue.map((item, index) => {
    const at = `${path}.intervals.${index}`;
    const interval = closedObject(item, at, ['from_at', 'to_at', 'actual_interval_seconds', 'delta', 'per_day'],
      metric === 'views' ? ['definition'] : []);
    const left = points[index], right = points[index + 1];
    if (interval.from_at !== left.recorded_at || interval.to_at !== right.recorded_at) contractError(at, 'not adjacent real points.');
    const elapsed = right.at - left.at;
    if (interval.actual_interval_seconds !== secondsText(elapsed)) contractError(`${at}.actual_interval_seconds`, 'not the exact interval.');
    let definition: HistoryIntervalDefinition | undefined;
    if (metric === 'views') {
      definition = enumValue(interval.definition, `${at}.definition`, ['known', 'unknown_definition', 'incompatible_definition']);
      const expected = left.epoch === null || right.epoch === null ? 'unknown_definition'
        : left.epoch !== right.epoch ? 'incompatible_definition' : 'known';
      if (definition !== expected) contractError(`${at}.definition`, 'does not match the points.');
    }
    const from = left.values[metric], to = right.values[metric];
    if (from === null || to === null || (definition !== undefined && definition !== 'known')) {
      if (interval.delta !== null || interval.per_day !== null) contractError(at, 'a delta needs two comparable values.');
    } else {
      const delta = BigInt(to) - BigInt(from);
      if (interval.delta !== delta.toString() || interval.per_day !== exactPerDay(delta, elapsed)) contractError(at, 'not the exact change.');
    }
    return {
      from_at: left.recorded_at, to_at: right.recorded_at, actual_interval_seconds: interval.actual_interval_seconds as string,
      delta: interval.delta as string | null, per_day: interval.per_day as string | null, ...(definition ? { definition } : {}),
    };
  });
  // The latest interval is the last one when it has a change (server: same object).
  const last = intervals.at(-1);
  const latest = last && last.delta !== null ? last : null;
  if (latest === null) {
    if (series.latest_interval !== null) contractError(`${path}.latest_interval`, 'only an interval with a change.');
  } else {
    const copy = closedObject(series.latest_interval, `${path}.latest_interval`, Object.keys(latest));
    if (Object.keys(latest).some((key) => copy[key] !== (latest as JsonRecord)[key])) contractError(`${path}.latest_interval`, 'not the last interval.');
  }
  return {
    status, reason: null,
    points: points.map((point) => ({ recorded_at: point.recorded_at, value: point.values[metric] })),
    intervals,
    latest_interval: latest ? {
      from_at: latest.from_at, to_at: latest.to_at, actual_interval_seconds: latest.actual_interval_seconds,
      delta: latest.delta, per_day: latest.per_day,
    } : null,
    coverage: shape,
  };
}

export function parseObservedHistory(raw: unknown, expect: InspectorExpectation): VideoHistoryView {
  const envelope = operationEnvelope(raw, INSPECTOR_OPERATIONS.history);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path,
    [...HEAD, 'status', 'reason', 'window', 'row_limit', 'scanned_count', 'points', 'series', 'methods']);
  const head = resultHead(result, path, expect, envelope);
  exactMethods(result.methods, `${path}.methods`, HISTORY_METHODS);
  if (result.row_limit !== HISTORY_ROW_LIMIT) contractError(`${path}.row_limit`, 'expected 500.');
  const scanned = count(result.scanned_count, `${path}.scanned_count`, HISTORY_ROW_LIMIT + 1);
  const span = closedObject(result.window, `${path}.window`, ['days', 'hours', 'from_at', 'to_at', 'rule', 'endpoint', 'start']);
  if (span.days !== 28 || span.hours !== 672 || span.rule !== HISTORY_METHODS.window) contractError(`${path}.window`, 'unexpected window.');
  enumValue(span.endpoint, `${path}.window.endpoint`, expect.mode === 'frozen'
    ? ['frozen_counter_observation', 'counter_observation'] : ['counter_observation']);
  if (span.start !== (expect.videoType === 'stream' ? 'actual_end_at_if_later' : 'window')) contractError(`${path}.window.start`, 'unexpected start.');
  const from = span.from_at === null ? null : utcInstant(span.from_at, `${path}.window.from_at`);
  const to = span.to_at === null ? null : utcInstant(span.to_at, `${path}.window.to_at`);
  if ((from === null) !== (to === null)) contractError(`${path}.window`, 'both ends or none.');
  if (from !== null && to !== null) {
    // From the evaluation time minus 28 days (Streams: not before their end) to the counter observation.
    const start = micros(envelope.evaluatedAt) - WINDOW_MICROSECONDS;
    if (expect.videoType === 'stream' ? micros(from) < start : micros(from) !== start) contractError(`${path}.window.from_at`, 'not the 28-day window.');
    if (micros(to) < micros(from) || micros(to) > micros(head.clock)) contractError(`${path}.window.to_at`, 'outside the inspection clock.');
  }
  let previous: bigint | null = null;
  const points: WirePoint[] = array(result.points, `${path}.points`, HISTORY_ROW_LIMIT).map((item, index) => {
    const at = `${path}.points.${index}`;
    const point = closedObject(item, at, ['recorded_at', 'views', 'likes', 'comments', 'views_definition_epoch']);
    const recorded = utcInstant(point.recorded_at, `${at}.recorded_at`);
    const time = micros(recorded);
    if (from === null || to === null || time < micros(from) || time > micros(to) || (previous !== null && time <= previous)) {
      contractError(`${at}.recorded_at`, 'outside the window or out of order.');
    }
    previous = time;
    const values = Object.fromEntries(HISTORY_METRICS.map((metric) => [metric,
      point[metric] === null ? null : uintString(point[metric], `${at}.${metric}`)])) as Record<HistoryMetric, string | null>;
    const epoch = point.views_definition_epoch === null ? null : text(point.views_definition_epoch, `${at}.views_definition_epoch`, 96);
    return { at: time, recorded_at: recorded, values, epoch };
  });
  if (scanned < points.length) contractError(`${path}.scanned_count`, 'fewer rows than points.');
  if (points.length && micros(points[points.length - 1].recorded_at) !== micros(to as string)) {
    contractError(`${path}.points`, 'the series must end at the counter observation.');
  }
  const seriesValue = closedObject(result.series, `${path}.series`, [...HISTORY_METRICS]);
  const series = Object.fromEntries(HISTORY_METRICS.map((metric) => [metric,
    historySeries(seriesValue[metric], `${path}.series.${metric}`, metric, points, scanned)])) as Record<HistoryMetric, HistoryViewSeries>;
  const ready = HISTORY_METRICS.some((metric) => series[metric].status === 'ready');
  if (ready !== (points.length > 0)) contractError(`${path}.points`, 'points exist exactly when a series is ready.');
  if (series.views.status !== 'ready' && points.some((point) => point.epoch !== null)) contractError(`${path}.points`, 'epochs belong to views.');
  if (result.status !== series.views.status || result.reason !== series.views.reason) contractError(`${path}.status`, 'must be the views series state.');
  const notMember = HISTORY_METRICS.every((metric) => series[metric].reason === 'video_not_in_population');
  if ((head.membership === 'not_in_population') !== notMember
    || (!notMember && HISTORY_METRICS.some((metric) => series[metric].reason === 'video_not_in_population'))) {
    contractError(`${path}.membership`, 'does not match the series.');
  }
  if (notMember) throw new InspectorNotInPopulation();
  const views = series.views;
  const shared = { from_at: from, to_at: to };
  return transferSourceLifetime(raw, {
    status: views.status,
    reason: views.reason,
    window: shared,
    points: views.points.map((point) => ({ recorded_at: point.recorded_at, views: point.value })),
    intervals: views.intervals.map((item) => ({
      from_at: item.from_at, to_at: item.to_at, actual_interval_seconds: item.actual_interval_seconds,
      delta_views: item.delta, views_per_day: item.per_day, ...(item.definition ? { definition: item.definition } : {}),
    })),
    latest_interval: views.latest_interval ? {
      from_at: views.latest_interval.from_at, to_at: views.latest_interval.to_at,
      actual_interval_seconds: views.latest_interval.actual_interval_seconds,
      delta_views: views.latest_interval.delta, views_per_day: views.latest_interval.per_day,
    } : null,
    coverage: {
      point_count: views.coverage.point_count, valid_view_count: views.coverage.valid_count,
      missing_view_count: views.coverage.missing_count, duplicate_timestamp_count: views.coverage.duplicate_timestamp_count,
    },
    interaction_metrics: { window: { ...shared }, likes: series.likes, comments: series.comments },
  } satisfies VideoHistoryView);
}

// ---------------------------------------------------------------------------
// video.launch_comparison.v1

const LAUNCH_HORIZONS = [['24h', 86_400, 7_200], ['7d', 604_800, 86_400], ['28d', 2_419_200, 86_400]] as const;
const LAUNCH_STATUSES = ['ready', 'not_reached', 'missing_publication', 'outside_observation_window', 'missing_snapshot',
  'invalid_snapshot', 'conflicting_snapshots', 'ineligible_video'] as const;
const quality = (size: number) => (size < 4 ? 'insufficient_data' : size < 12 ? 'preliminary' : 'ready');

/** Excluded rows: the population's withheld rows and other row reasons (not channel inventory). */
function excludedRows(coverage: CoverageNotice): number {
  return coverage.withheldRows + coverage.other.reduce((sum, item) => sum + item.count, 0);
}

function launchComparison(value: unknown, path: string, target: { status: string; views: string | null; epoch: string | null },
  cohort: number, state: string, epoch: string | null): LaunchComparisonView {
  const comparison = closedObject(value, path,
    ['status', 'reference_count', 'median', 'p25', 'p75', 'classification', 'multiplier', 'multiplier_reason']);
  const status = enumValue(comparison.status, `${path}.status`,
    ['unavailable', 'unknown_definition', 'mixed_definition_epochs', 'no_known_values', 'insufficient_data', 'preliminary', 'ready']);
  const references = count(comparison.reference_count, `${path}.reference_count`);
  const empty = () => {
    if (['median', 'p25', 'p75', 'classification', 'multiplier', 'multiplier_reason'].some((key) => comparison[key] !== null)) {
      contractError(path, 'no comparison values without an assessment.');
    }
  };
  let expected: string, expectedReferences: number;
  if (target.status !== 'ready') { expected = 'unavailable'; expectedReferences = cohort; }
  else if (target.epoch === null) { expected = 'unknown_definition'; expectedReferences = cohort; }
  else if (state !== 'known') { expected = state; expectedReferences = 0; }
  else if (cohort < 4) { expected = 'insufficient_data'; expectedReferences = cohort - 1; }
  else { expected = quality(cohort); expectedReferences = cohort - 1; }
  if (status !== expected || references !== expectedReferences) contractError(path, 'not the comparison of this horizon.');
  if (target.status === 'ready' && target.epoch !== null && state === 'known' && target.epoch !== epoch) {
    contractError(path, 'the video is outside the cohort epoch.');
  }
  if (status !== 'preliminary' && status !== 'ready') {
    empty();
    return { status, reference_count: references, median: null, p25: null, p75: null, classification: null, multiplier: null, multiplier_reason: null };
  }
  const median = quarterHundredths(comparison.median, `${path}.median`);
  const low = quarterHundredths(comparison.p25, `${path}.p25`);
  const high = quarterHundredths(comparison.p75, `${path}.p75`);
  if (low > median || median > high) contractError(path, 'quartiles out of order.');
  const observed = BigInt(target.views as string) * 100n;
  const classification = observed < low ? 'below_typical' : observed > high ? 'above_typical' : 'typical';
  if (comparison.classification !== classification) contractError(`${path}.classification`, 'not this video against the quartiles.');
  if (median === 0n) {
    if (comparison.multiplier !== null || comparison.multiplier_reason !== 'zero_reference_median') contractError(`${path}.multiplier`, 'no ratio over a zero median.');
  } else {
    const rounded = roundHalfEven(observed * 100n, median);
    if (comparison.multiplier_reason !== null || comparison.multiplier !== `${rounded / 100n}.${(rounded % 100n).toString().padStart(2, '0')}`) {
      contractError(`${path}.multiplier`, 'not the exact ratio.');
    }
  }
  return {
    status, reference_count: references, median: comparison.median as string, p25: comparison.p25 as string, p75: comparison.p75 as string,
    classification, multiplier: comparison.multiplier as string | null,
    multiplier_reason: comparison.multiplier_reason as 'zero_reference_median' | null,
  };
}

export function parseLaunchComparison(raw: unknown, expect: InspectorExpectation): VideoLaunchView {
  const envelope = operationEnvelope(raw, INSPECTOR_OPERATIONS.launch);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, [...HEAD, 'method', 'window', 'reference', 'coverage', 'horizons']);
  const head = resultHead(result, path, expect, envelope);
  if (result.method !== 'measured_launch_before_horizon_v1') contractError(`${path}.method`, 'unexpected method.');
  const span = closedObject(result.window, `${path}.window`, ['days', 'hours', 'from_at', 'to_at']);
  const from = utcInstant(span.from_at, `${path}.window.from_at`), to = utcInstant(span.to_at, `${path}.window.to_at`);
  if (span.days !== 28 || span.hours !== 672 || micros(from) !== micros(envelope.evaluatedAt) - WINDOW_MICROSECONDS
    || micros(to) !== micros(head.clock) || micros(from) >= micros(to)) contractError(`${path}.window`, 'not the 28-day window to the inspection clock.');
  const reference = closedObject(result.reference, `${path}.reference`,
    ['video_type', 'publication_period', 'reference_point', 'query_conditions_applied', 'leave_one_out']);
  if (reference.video_type !== expect.videoType || reference.publication_period !== 'all_time'
    || reference.reference_point !== (expect.videoType === 'stream' ? 'actual_end_at' : 'published_at')
    || reference.query_conditions_applied !== false || reference.leave_one_out !== true) contractError(`${path}.reference`, 'unexpected reference.');
  const coverage = closedObject(result.coverage, `${path}.coverage`,
    ['population_count', 'candidate_targets', 'snapshot_rows', 'reads_per_video_limit', 'complete']);
  const population = count(coverage.population_count, `${path}.coverage.population_count`);
  if (coverage.reads_per_video_limit !== 3 || coverage.complete !== true
    || count(coverage.candidate_targets, `${path}.coverage.candidate_targets`) > 3 * population) contractError(`${path}.coverage`, 'unexpected coverage.');
  count(coverage.snapshot_rows, `${path}.coverage.snapshot_rows`);
  const horizons = array(result.horizons, `${path}.horizons`, 3);
  if (horizons.length !== 3) contractError(`${path}.horizons`, 'expected three horizons.');
  let reference_at: bigint | null = null;
  const member = head.membership === 'member';
  const parsed = horizons.map((item, index): LaunchHorizonView | null => {
    const at = `${path}.horizons.${index}`;
    const [key, seconds, tolerance] = LAUNCH_HORIZONS[index];
    const horizon = closedObject(item, at,
      ['key', 'horizon_seconds', 'tolerance_before_seconds', 'eligible_count', 'quality', 'counts', 'ready_count', 'definition', 'target']);
    if (horizon.key !== key || horizon.horizon_seconds !== seconds || horizon.tolerance_before_seconds !== tolerance) contractError(at, 'unexpected horizon.');
    const cohort = count(horizon.eligible_count, `${at}.eligible_count`);
    const ready = count(horizon.ready_count, `${at}.ready_count`);
    if (cohort > ready || ready > population || horizon.quality !== quality(cohort)) contractError(at, 'unexpected cohort.');
    const counts = closedObject(horizon.counts, `${at}.counts`, ['below_typical', 'typical', 'above_typical']);
    const sum = ['below_typical', 'typical', 'above_typical'].reduce((total, name) => total + count(counts[name], `${at}.counts.${name}`), 0);
    if (sum !== (cohort >= 4 ? cohort : 0)) contractError(`${at}.counts`, 'do not add up to the cohort.');
    const definition = closedObject(horizon.definition, `${at}.definition`, ['state', 'epoch', 'unknown_definition_count']);
    const state = enumValue(definition.state, `${at}.definition.state`, ['known', 'mixed_definition_epochs', 'no_known_values']);
    const epoch = state === 'known' ? text(definition.epoch, `${at}.definition.epoch`, 96) : null;
    if (state !== 'known' && definition.epoch !== null) contractError(`${at}.definition.epoch`, 'only a known definition has an epoch.');
    const unknown = count(definition.unknown_definition_count, `${at}.definition.unknown_definition_count`);
    if (unknown > ready || (state === 'known') !== (cohort > 0 && cohort === ready - unknown)) contractError(`${at}.definition`, 'does not match the cohort.');
    if (!member) {
      if (horizon.target !== null) contractError(`${at}.target`, 'a non-member has no target.');
      return null;
    }
    const target = closedObject(horizon.target, `${at}.target`,
      ['status', 'target_at', 'observed_at', 'actual_age_seconds', 'views', 'definition_epoch', 'comparison']);
    const status = enumValue(target.status, `${at}.target.status`, LAUNCH_STATUSES);
    const noTarget = status === 'missing_publication' || status === 'ineligible_video';
    const targetAt = target.target_at === null ? null : utcInstant(target.target_at, `${at}.target.target_at`);
    if (noTarget !== (targetAt === null)) contractError(`${at}.target.target_at`, 'unexpected target time.');
    if (targetAt !== null) {
      const start = micros(targetAt) - BigInt(seconds) * MICROSECOND;
      if (reference_at !== null && reference_at !== start) contractError(`${at}.target.target_at`, 'another reference point.');
      reference_at = start;
    }
    const target_view = { status, target_at: targetAt, observed_at: null as string | null, actual_age_seconds: null as string | null,
      views: null as string | null };
    let targetEpoch: string | null = null;
    if (status === 'ready') {
      const observed = utcInstant(target.observed_at, `${at}.target.observed_at`);
      const age = text(target.actual_age_seconds, `${at}.target.actual_age_seconds`, 32);
      const views = uintString(target.views, `${at}.target.views`);
      targetEpoch = target.definition_epoch === null ? null : text(target.definition_epoch, `${at}.target.definition_epoch`, 96);
      const gap = micros(targetAt as string) - micros(observed);
      if (cohort > ready || gap < 0n || gap > BigInt(tolerance) * MICROSECOND || micros(observed) < micros(from) || micros(observed) > micros(to)
        || age !== secondsText(BigInt(seconds) * MICROSECOND - gap)) contractError(`${at}.target`, 'not a real snapshot before the target.');
      Object.assign(target_view, { observed_at: observed, actual_age_seconds: age, views });
    } else {
      if (target.observed_at !== null || target.actual_age_seconds !== null || target.views !== null || target.definition_epoch !== null) {
        contractError(`${at}.target`, 'an unmeasured target has no observation.');
      }
      const time = targetAt === null ? null : micros(targetAt);
      if ((status === 'not_reached' && (time === null || time <= micros(to)))
        || (status === 'outside_observation_window' && (time === null || time >= micros(from)))) contractError(`${at}.target`, 'status does not match the window.');
    }
    const comparison = launchComparison(target.comparison, `${at}.target.comparison`, { status, views: target_view.views, epoch: targetEpoch },
      cohort, state, epoch);
    return {
      key, horizon_seconds: seconds, tolerance_before_seconds: tolerance, eligible_count: cohort, quality: quality(cohort),
      counts: { below_typical: counts.below_typical as number, typical: counts.typical as number, above_typical: counts.above_typical as number },
      definition_state: state, unknown_definition_count: unknown,
      target: { ...target_view, comparison },
    };
  });
  if (!member) throw new InspectorNotInPopulation();
  return transferSourceLifetime(raw, {
    video_type: expect.videoType,
    window: { from_at: from, to_at: to },
    coverage: { eligible_video_count: population, excluded_video_count: excludedRows(envelope.coverage) },
    horizons: parsed as LaunchHorizonView[],
  } satisfies VideoLaunchView);
}

// ---------------------------------------------------------------------------
// video.lifetime_baseline.v1

export function parseLifetimeBaseline(raw: unknown, expect: InspectorExpectation): LifetimeBaselineView {
  const envelope = operationEnvelope(raw, INSPECTOR_OPERATIONS.baseline);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, [...HEAD, 'method', 'status', 'sample_size', 'missing_views', 'median', 'p25', 'p75',
    'definition', 'population_count', 'reference', 'target']);
  const head = resultHead(result, path, expect, envelope);
  if (result.method !== 'current_lifetime_views_median_iqr_v1') contractError(`${path}.method`, 'unexpected method.');
  const population = count(result.population_count, `${path}.population_count`);
  const sample = count(result.sample_size, `${path}.sample_size`);
  if (count(result.missing_views, `${path}.missing_views`) !== population - sample || sample > population) contractError(path, 'sample does not add up.');
  const status = enumValue(result.status, `${path}.status`, ['insufficient_data', 'preliminary', 'ready']);
  if (status !== quality(sample)) contractError(`${path}.status`, 'not the status of this sample size.');
  if (status === 'insufficient_data') {
    if (result.median !== null || result.p25 !== null || result.p75 !== null) contractError(path, 'no statistics below 4 videos.');
  } else {
    const median = quarterHundredths(result.median, `${path}.median`), low = quarterHundredths(result.p25, `${path}.p25`);
    const high = quarterHundredths(result.p75, `${path}.p75`);
    if (low > median || median > high) contractError(path, 'quartiles out of order.');
  }
  // A stored value without a recorded views definition is left out on its own and counted.
  const definition = closedObject(result.definition, `${path}.definition`, ['state', 'epoch', 'unknown_definition_epoch_count']);
  const state = enumValue(definition.state, `${path}.definition.state`,
    ['known', 'mixed_definition_epochs', 'unknown_definition_epoch', 'no_known_values']);
  if ((state === 'known') !== (sample > 0)) contractError(`${path}.definition`, 'known exactly when views are counted.');
  if (state === 'known' ? !text(definition.epoch, `${path}.definition.epoch`, 96) : definition.epoch !== null) {
    contractError(`${path}.definition.epoch`, 'only a known definition has an epoch.');
  }
  const undefinedValues = count(definition.unknown_definition_epoch_count, `${path}.definition.unknown_definition_epoch_count`);
  if (undefinedValues > population - sample) contractError(`${path}.definition.unknown_definition_epoch_count`, 'more than the missing views.');
  // Every stored value lacks a definition (unknown_definition_epoch), or none is stored (no_known_values).
  if (state === 'unknown_definition_epoch' ? undefinedValues === 0 : state === 'no_known_values' && undefinedValues !== 0) {
    contractError(`${path}.definition.unknown_definition_epoch_count`, 'does not match the definition state.');
  }
  const reference = closedObject(result.reference, `${path}.reference`,
    ['video_type', 'publication_period', 'query_conditions_applied', 'includes_inspected_video']);
  if (reference.video_type !== expect.videoType || reference.publication_period !== 'all_time'
    || reference.query_conditions_applied !== false || reference.includes_inspected_video !== true) contractError(`${path}.reference`, 'unexpected reference.');
  // The inspected video's own views beside the sample (frozen: the browsed value).
  const frozenViews = head.frozenFields?.includes('video.views') ?? false;
  if (result.target === null) {
    if (frozenViews || head.membership === 'member') contractError(`${path}.target`, 'missing the video views.');
  } else {
    if (!frozenViews && head.membership !== 'member') contractError(`${path}.target`, 'a non-member has no views here.');
    const target = closedObject(result.target, `${path}.target`, ['views']);
    inspectorCell(target.views, `${path}.target.views`, (item, at) => uintString(item, at));
  }
  return transferSourceLifetime(raw, {
    video_type: expect.videoType,
    status,
    sample_size: sample,
    missing_views: population - sample,
    median: result.median as string | null,
    p25: result.p25 as string | null,
    p75: result.p75 as string | null,
    excluded_count: excludedRows(envelope.coverage),
    definition: state,
    unknown_definition_count: undefinedValues,
  } satisfies LifetimeBaselineView);
}

// ---------------------------------------------------------------------------
// 28-day growth evidence → the "Observed views" section

/** The five growth fields in growth order; the first selected one carries the evidence. */
export const GROWTH_FIELDS = ['observation.growth_28d_status', 'observation.views_gained_28d', 'observation.views_per_day_28d',
  'observation.growth_28d_interval_seconds', 'observation.growth_28d_tracked_days'] as const;
const GROWTH_STATUSES = ['ready', 'partial', 'started_today', 'waiting_next_snapshot', 'unavailable'] as const;
const GROWTH_EPOCH_REASONS = ['unknown_definition', 'incompatible_definition'];
const GROWTH_REASONS: Record<typeof GROWTH_STATUSES[number], readonly (string | null)[]> = {
  ready: [null],
  partial: ['full_window_history_unavailable'],
  started_today: ['tracking_started_today'],
  waiting_next_snapshot: ['waiting_for_next_snapshot'],
  unavailable: ['video_not_in_scope', 'current_data_stale', 'current_counter_unavailable', 'current_snapshot_missing', ...GROWTH_EPOCH_REASONS],
};
const GROWTH_WINDOW_SECONDS = 672n * 3600n;
const GROWTH_EVIDENCE = ['method', 'version', 'status', 'reason', 'from_at', 'to_at', 'from_views', 'to_views', 'actual_interval_seconds',
  'boundary_distance_seconds', 'tracked_days', 'definition_epoch', 'from_definition_epoch', 'to_definition_epoch', 'time_zone'];

/** The Inspector's growth: the card's `view_growth` plus the zone of "today". */
export type InspectorGrowth = ChannelVideoViewGrowth & { time_zone: string };

function nullable<T>(value: unknown, read: (item: unknown) => T): T | null {
  return value === null ? null : read(value);
}

/** `growth.evidence_problem`: the integrity rules of one growth row. */
function growthEvidence(value: unknown, path: string) {
  const item = closedObject(value, path, GROWTH_EVIDENCE);
  if (item.method !== 'video.observed_growth_28d.v1' || item.version !== 1) contractError(path, 'unexpected growth method.');
  const status = enumValue(item.status, `${path}.status`, GROWTH_STATUSES);
  const reason = item.reason === null ? null : text(item.reason, `${path}.reason`, 64);
  if (!GROWTH_REASONS[status].includes(reason)) contractError(`${path}.reason`, 'not a reason of this status.');
  const at = (key: string) => nullable(item[key], (raw) => utcInstant(raw, `${path}.${key}`));
  const uint = (key: string) => nullable(item[key], (raw) => BigInt(uintString(raw, `${path}.${key}`)));
  const epoch = (key: string) => nullable(item[key], (raw) => {
    const name = text(raw, `${path}.${key}`, 96);
    if (!/^[a-zA-Z0-9_.:-]{1,96}$/.test(name)) contractError(`${path}.${key}`, 'expected an epoch.');
    return name;
  });
  const start = at('from_at'), end = at('to_at');
  const fromViews = uint('from_views'), toViews = uint('to_views'), interval = uint('actual_interval_seconds'), tracked = uint('tracked_days');
  const boundary = nullable(item.boundary_distance_seconds, (raw) => {
    if (typeof raw !== 'string' || !/^-?(?:0|[1-9][0-9]{0,6})$/.test(raw)) return contractError(`${path}.boundary_distance_seconds`, 'expected seconds.');
    return BigInt(raw);
  });
  const definition = epoch('definition_epoch'), fromEpoch = epoch('from_definition_epoch'), toEpoch = epoch('to_definition_epoch');
  const timeZone = text(item.time_zone, `${path}.time_zone`, 64);
  if (!timeZone) contractError(`${path}.time_zone`, 'expected a zone.');
  // Whole seconds of the measured span (Python timedelta days * 86400 + seconds).
  let measured: bigint | null = null;
  if (start !== null && end !== null) {
    const span = micros(end) - micros(start);
    measured = span >= 0n ? span / MICROSECOND : -((-span + MICROSECOND - 1n) / MICROSECOND);
  }
  const invalid = () => contractError(path, `inconsistent ${status} growth evidence.`);
  if (status === 'ready' || status === 'partial') {
    if (start === null || end === null || interval === null || fromViews === null || toViews === null || measured !== interval
      || definition === null || fromEpoch !== definition || toEpoch !== definition) invalid();
    if (status === 'ready') {
      if (boundary === null || tracked !== null || interval !== GROWTH_WINDOW_SECONDS + boundary || boundary < -10_800n || boundary > 86_400n) invalid();
    } else if (boundary !== null || interval === null || interval < 86_400n || interval >= GROWTH_WINDOW_SECONDS
      || tracked !== interval / 86_400n || (tracked ?? 0n) < 1n) invalid();
  } else if (status !== 'unavailable') {
    if (start === null || end === null || interval === null || measured !== interval || interval >= 86_400n
      || boundary !== null || tracked !== null) invalid();
  } else {
    if (interval !== null || boundary !== null || tracked !== null) invalid();
    if (reason !== null && GROWTH_EPOCH_REASONS.includes(reason)) {
      if (start === null || end === null || definition !== null || (reason === 'unknown_definition') !== (fromEpoch === null || toEpoch === null)
        || (reason === 'incompatible_definition' && fromEpoch === toEpoch)) invalid();
    } else if (start !== null || fromViews !== null) invalid();
  }
  return { status, reason, start, end, fromViews, toViews, interval, tracked, boundary, timeZone };
}

/**
 * Growth cells of one row (a frozen page row or a current one-video query)
 * → the existing `view_growth` presentation. Null when no growth field was
 * selected. Every selected cell must equal what the evidence implies
 * (`cells_problem`); one row carries one evidence object.
 */
export function growthFromCells(cells: JsonRecord, path: string): InspectorGrowth | null {
  const selected = GROWTH_FIELDS.filter((field) => Object.hasOwn(cells, field));
  if (!selected.length) return null;
  const parsed = selected.map((field) => {
    const at = `${path}.${field}`;
    const cell = closedObject(cells[field], at, ['value', 'state', 'provenance_ref', 'observed_at', 'expires_at'], ['observation']);
    const state = enumValue(cell.state, `${at}.state`, CELL_STATES);
    text(cell.provenance_ref, `${at}.provenance_ref`, 96);
    if ((state === 'known') !== (cell.value !== null)) contractError(at, 'a value exactly when known.');
    if (field !== GROWTH_FIELDS[0] && state !== 'known' && state !== 'unavailable') contractError(`${at}.state`, 'expected known or unavailable.');
    if (cell.value !== null && typeof cell.value !== 'string') contractError(`${at}.value`, 'expected text.');
    const carrier = field === selected[0];
    if (carrier ? cell.observation === null || cell.observation === undefined : cell.observation !== null && cell.observation !== undefined) {
      contractError(`${at}.observation`, 'one evidence object per row, on the first growth field.');
    }
    return { field, value: cell.value as string | null, observation: carrier ? cell.observation : null };
  });
  const evidence = growthEvidence(parsed[0].observation, `${path}.${selected[0]}.observation`);
  const calculable = evidence.status === 'ready' || evidence.status === 'partial';
  const gained = calculable ? (evidence.toViews as bigint) - (evidence.fromViews as bigint) : null;
  const perDay = gained === null ? null : exactPerDay(gained, (evidence.interval as bigint) * MICROSECOND);
  const expected: Record<string, string | null> = {
    [GROWTH_FIELDS[0]]: evidence.status,
    [GROWTH_FIELDS[1]]: gained === null ? null : gained.toString(),
    [GROWTH_FIELDS[2]]: perDay,
    [GROWTH_FIELDS[3]]: evidence.interval === null ? null : evidence.interval.toString(),
    [GROWTH_FIELDS[4]]: evidence.tracked === null ? null : evidence.tracked.toString(),
  };
  for (const item of parsed) {
    if (item.value !== expected[item.field]) contractError(`${path}.${item.field}`, 'does not match the growth evidence.');
  }
  return {
    status: evidence.status,
    delta_views: gained === null ? null : gained.toString(),
    tracked_days: evidence.status === 'partial' ? Number(evidence.tracked) : null,
    observed_start_at: evidence.status === 'unavailable' ? null : evidence.start,
    observed_end_at: evidence.end,
    actual_interval_seconds: evidence.interval === null ? null : Number(evidence.interval),
    average_views_per_day: perDay,
    boundary_distance_seconds: evidence.status === 'ready' ? Number(evidence.boundary) : null,
    reason: evidence.reason,
    time_zone: evidence.timeZone,
  };
}

/** One current video with its growth fields (query opening; `video.id eq`). */
export function inspectorGrowthQuery(opening: QueryInspectorOpening, video: string) {
  checkedOpening(opening);
  videoId(video, 'video_id');
  const timezone = opening.timeZone ?? 'UTC';
  text(timezone, 'time.timezone', 64);
  return {
    query_version: 1 as const,
    source: { id: VIDEO_SOURCE },
    scope: { kind: opening.scope.kind, id: opening.scope.id },
    video_types: [opening.videoType],
    select: [FIELDS.id, ...GROWTH_FIELDS],
    where: { field: FIELDS.id, op: 'eq' as const, value: video },
    order_by: [{ field: FIELDS.id, direction: 'asc' as const }],
    time: { mode: 'current' as const, timezone },
    page: { size: 1 },
  };
}

const QUERY_CONTEXT_OPTIONAL = ['source_schema_version', 'registry_version', 'lifecycle_profile', 'preferences_revision', 'type_policy',
  'observation_age_limit_seconds', 'time_zone'];

/** The one-row current result of `inspectorGrowthQuery`. No row: the video is not in the population. */
export function parseInspectorGrowth(raw: unknown, expect: { videoId: string; videoType: LibraryVideoType; timeZone: string }): InspectorGrowth {
  const root = closedObject(raw, 'result', ['query_version', 'query_fingerprint', 'context', 'columns', 'provenance', 'rows', 'coverage', 'page'],
    ['warnings', 'source_read_at', 'source_expires_at']);
  if (root.query_version !== 1 || !/^[a-f0-9]{64}$/.test(text(root.query_fingerprint, 'result.query_fingerprint', 64))) {
    contractError('result', 'unexpected query result.');
  }
  const context = closedObject(root.context, 'result.context', ['source_id', 'evaluated_at', 'valid_before', 'consistency'], QUERY_CONTEXT_OPTIONAL);
  if (context.source_id !== VIDEO_SOURCE || context.consistency !== 'current') contractError('result.context', 'expected the current video source.');
  if (context.time_zone !== undefined && context.time_zone !== expect.timeZone) contractError('result.context.time_zone', 'another zone.');
  if (micros(utcInstant(context.valid_before, 'result.context.valid_before')) <= micros(utcInstant(context.evaluated_at, 'result.context.evaluated_at'))) {
    contractError('result.context.valid_before', 'the result must end after it was evaluated.');
  }
  const columns = [FIELDS.id, ...GROWTH_FIELDS];
  const shown = array(root.columns, 'result.columns', 50);
  if (shown.length !== columns.length || shown.some((item, index) => item !== columns[index])) contractError('result.columns', 'unexpected columns.');
  const provenance = openObject(root.provenance, 'result.provenance');
  const page = closedObject(root.page, 'result.page', ['has_more', 'next_cursor']);
  if (page.has_more !== false || page.next_cursor !== null) contractError('result.page', 'one video has one page.');
  coverageNotice(root.coverage);
  if (root.warnings !== undefined) array(root.warnings, 'result.warnings', 32).forEach((item, index) => text(item, `result.warnings.${index}`, 96));
  const rows = array(root.rows, 'result.rows', 1);
  if (!rows.length) throw new InspectorNotInPopulation();
  const row = closedObject(rows[0], 'result.rows.0', ['id', 'cells']);
  if (videoId(row.id, 'result.rows.0.id') !== expect.videoId) contractError('result.rows.0.id', 'another video.');
  const cells = closedObject(row.cells, 'result.rows.0.cells', columns);
  for (const field of columns) {
    const reference = (cells[field] as JsonRecord | null)?.provenance_ref;
    if (typeof reference !== 'string' || !Object.hasOwn(provenance, reference)) contractError(`result.rows.0.cells.${field}`, 'unknown provenance.');
  }
  const id = closedObject(cells[FIELDS.id], `result.rows.0.cells.${FIELDS.id}`, ['value', 'state', 'provenance_ref', 'observed_at', 'expires_at'], ['observation']);
  if (id.state !== 'known' || id.value !== expect.videoId) contractError(`result.rows.0.cells.${FIELDS.id}`, 'another video.');
  const growth = growthFromCells(cells, 'result.rows.0.cells') as InspectorGrowth;
  if (growth.time_zone !== expect.timeZone) contractError('result.rows.0.cells', 'growth evidence of another zone.');
  return transferSourceLifetime(raw, growth);
}

// ---------------------------------------------------------------------------
// Failures, retries, client and panel sources

/** How a panel shows a failed read (Inspector column). */
export type InspectorFailure = { kind: 'unavailable' | 'inactive' | 'error'; notInPopulation?: true };

export function inspectorFailure(error: unknown): InspectorFailure | null {
  if (error instanceof InspectorNotInPopulation) return { kind: 'unavailable', notInPopulation: true };
  const state = libraryErrorState(error, 'inspector');
  if (state.action === 'aborted') return null;
  if (state.action === 'inactive') return { kind: 'inactive' };
  if (state.action === 'unavailable') return { kind: 'unavailable' };
  return { kind: 'error' };
}

/** Busy reads are retried, not shown: the page's first burst of reads shares two read slots. */
export const INSPECTOR_BUSY_DELAYS_MS = [500, 1000, 2000, 4000] as const;
/** A response that expired during the read (503 without a code, "retry") is repeated once. */
export const INSPECTOR_EXPIRED_READ_DELAY_MS = 500;

function errorStatus(error: unknown): number | undefined {
  const status = error && typeof error === 'object' ? (error as { status?: unknown }).status : undefined;
  return typeof status === 'number' ? status : undefined;
}

/** The delay before the next attempt, or null when the error is final. */
export function inspectorRetryDelay(error: unknown, attempts: { busy: number; preparing: number; expired: number }): number | null {
  if (error instanceof ExplorerContractError || error instanceof InspectorNotInPopulation) return null;
  const state = libraryErrorState(error, 'inspector');
  if (state.code === 'query_busy') return attempts.busy < INSPECTOR_BUSY_DELAYS_MS.length ? INSPECTOR_BUSY_DELAYS_MS[attempts.busy] : null;
  if (state.action === 'preparing') return attempts.preparing < PREPARING_RETRIES ? PREPARING_DELAY_MS : null;
  if (state.code === 'unknown' && errorStatus(error) === 503) return attempts.expired < 1 ? INSPECTOR_EXPIRED_READ_DELAY_MS : null;
  return null;
}

function abortError(): Error {
  const error = new Error('The read was cancelled.');
  error.name = 'AbortError';
  return error;
}

export type InspectorWait = (milliseconds: number, signal: AbortSignal) => Promise<void>;

const defaultWait: InspectorWait = (milliseconds, signal) => new Promise((resolve, reject) => {
  if (signal.aborted) { reject(abortError()); return; }
  const timer = setTimeout(() => { signal.removeEventListener('abort', cancel); resolve(); }, milliseconds);
  const cancel = () => { clearTimeout(timer); reject(abortError()); };
  signal.addEventListener('abort', cancel, { once: true });
});

/** The application passes `requestJson` (POST, JSON body); tests pass a fake. */
export type InspectorRequest = (path: string, body: unknown, signal: AbortSignal) => Promise<unknown>;

export type InspectorClient = {
  staticDetails(opening: InspectorOpening, video: string, options: { description: boolean }, signal: AbortSignal): Promise<StaticDetailsView>;
  history(opening: InspectorOpening, video: string, signal: AbortSignal): Promise<VideoHistoryView>;
  launch(opening: InspectorOpening, video: string, signal: AbortSignal): Promise<VideoLaunchView>;
  baseline(opening: InspectorOpening, video: string, signal: AbortSignal): Promise<LifetimeBaselineView>;
  /** Without a frozen row (query opening): the one video's growth evidence, current. */
  growth(opening: QueryInspectorOpening, video: string, signal: AbortSignal): Promise<InspectorGrowth>;
};

export function createInspectorClient(request: InspectorRequest, options: { wait?: InspectorWait } = {}): InspectorClient {
  const wait = options.wait ?? defaultWait;
  async function send(path: string, body: unknown, signal: AbortSignal): Promise<unknown> {
    const attempts = { busy: 0, preparing: 0, expired: 0 };
    for (;;) {
      if (signal.aborted) throw abortError();
      try {
        return await request(path, body, signal);
      } catch (error) {
        if (signal.aborted) throw abortError();
        const delay = inspectorRetryDelay(error, attempts);
        if (delay === null) throw error;
        const state = libraryErrorState(error, 'inspector');
        if (state.code === 'query_busy') attempts.busy += 1;
        else if (state.action === 'preparing') attempts.preparing += 1;
        else attempts.expired += 1;
        await wait(delay, signal);
      }
    }
  }
  const post = (body: InspectorRequestBody, signal: AbortSignal) => send(ROUTES.operations, body, signal);
  const expectation = (opening: InspectorOpening, video: string, description?: boolean): InspectorExpectation => ({
    videoId: video, videoType: opening.videoType, mode: inspectorMode(opening), ...(description ? { description } : {}),
  });
  return {
    async staticDetails(opening, video, { description }, signal) {
      const raw = await post(inspectorRequest(INSPECTOR_OPERATIONS.staticDetails, opening, video, description ? { description } : {}), signal);
      return parseStaticDetails(raw, expectation(opening, video, description));
    },
    async history(opening, video, signal) {
      return parseObservedHistory(await post(inspectorRequest(INSPECTOR_OPERATIONS.history, opening, video), signal), expectation(opening, video));
    },
    async launch(opening, video, signal) {
      return parseLaunchComparison(await post(inspectorRequest(INSPECTOR_OPERATIONS.launch, opening, video), signal), expectation(opening, video));
    },
    async baseline(opening, video, signal) {
      return parseLifetimeBaseline(await post(inspectorRequest(INSPECTOR_OPERATIONS.baseline, opening, video), signal), expectation(opening, video));
    },
    async growth(opening, video, signal) {
      const query = inspectorGrowthQuery(opening, video);
      return parseInspectorGrowth(await send(ROUTES.execute, query, signal), { videoId: video, videoType: opening.videoType, timeZone: query.time.timezone });
    },
  };
}

/**
 * What one panel needs: an identity (a new key resets the panel), whether the
 * opening is still the current session (`active`), whether the session is
 * still being prepared (`pending`: show loading and defer the read), the read
 * itself and how a failure is shown. The panels' state machines stay as they were.
 */
export type InspectorPanelSource<T> = {
  key: string;
  active: boolean;
  pending: boolean;
  videoType: LibraryVideoType;
  /** Frozen: a Library page; query: no browse session (texts differ only where they would mislead). */
  mode: 'frozen' | 'query';
  load: (signal: AbortSignal) => Promise<T>;
  failure: (error: unknown) => InspectorFailure | null;
};

export type InspectorSources = {
  staticDetails?: InspectorPanelSource<StaticDetailsView>;
  baseline?: InspectorPanelSource<LifetimeBaselineView>;
  launch?: InspectorPanelSource<VideoLaunchView>;
  history?: InspectorPanelSource<VideoHistoryView>;
  /** Query opening only; a frozen page passes its row's growth to the Inspector instead. */
  growth?: InspectorPanelSource<InspectorGrowth>;
  /** The description is requested with static details (field enabled for the type). */
  description: boolean;
};

/** Panel sources of one opened video. A frozen reference is read at call time (tokens change per page). */
export function inspectorSources(
  opening: InspectorOpening,
  video: string,
  gates: InspectorGates,
  client: InspectorClient,
): InspectorSources {
  const mode = inspectorMode(opening);
  const session = isQueryOpening(opening) ? `${opening.scope.kind}:${opening.scope.id}` : opening.sessionKey;
  const identity = [mode, opening.channelId, opening.videoType, session, video];
  const pending = opening.pending === true || (!isQueryOpening(opening) && !opening.frozenRef);
  const source = <T>(name: string, load: (signal: AbortSignal) => Promise<T>, extra: unknown[] = []): InspectorPanelSource<T> => ({
    key: JSON.stringify([name, ...identity, ...extra]), active: opening.active, pending, videoType: opening.videoType, mode,
    load, failure: inspectorFailure,
  });
  const description = gates.staticDetails && gates.description;
  return {
    ...(gates.staticDetails ? { staticDetails: source('static', (signal) => client.staticDetails(opening, video, { description }, signal), [description]) } : {}),
    ...(gates.baseline ? { baseline: source('baseline', (signal) => client.baseline(opening, video, signal)) } : {}),
    ...(gates.launch ? { launch: source('launch', (signal) => client.launch(opening, video, signal)) } : {}),
    ...(gates.history ? { history: source('history', (signal) => client.history(opening, video, signal), [opening.checkedAt]) } : {}),
    ...(gates.growth && isQueryOpening(opening)
      ? { growth: source('growth', (signal) => client.growth(opening, video, signal), [opening.timeZone ?? 'UTC']) } : {}),
    description,
  };
}
