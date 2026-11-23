// Overview and channel header adapter.
// Six operations produce the presentation the existing Overview and
// header components draw, for Long, Shorts and Streams alike:
//   video.fingerprint.v1, video.publication_rhythm.v1, video.publication_years.v1,
//   video.content_breakdown.v1 (Overview; descriptive_analytics = BASELINE gate);
// channel.counter_change.v1, channel.format_totals.v1 (header; never gated).
//
// Population: the server's one Overview population; a stale value
// is unknown and the video stays (its count is shown). Period, zone and
// buckets come from the server: the period moments of the reply, the viewer
// zone of `time.timezone`, the per-type bucket sets of
// `/research/capabilities`. The result lives as long as its data
// clocks (`context.valid_before`), never a fixed 120 s.
//
// Strict: an unknown key, enumeration value or broken
// invariant is an ExplorerContractError; nothing is shown partially. Counters
// stay decimal strings. Pure TypeScript: no React, window or network — the
// caller passes the request function (`requestJson` in the app).
import type {
  ChannelBaselineBreakdownSection, ChannelBaselineContentBreakdown, ChannelBaselinePublicationCalendar,
  ChannelExplorerCounterDynamicsMetric, ChannelExplorerCounterDynamicsPeriodKey, ChannelExplorerFormatStatus,
} from '../../types.ts';
import { ExplorerContractError, utcTimestampMicroseconds } from '../contracts.ts';
import { transferSourceLifetime } from '../../sourceLifetime.ts';
import { LIBRARY_VIDEO_TYPES, OPERATIONS, QUERY_VERSION, ROUTES, VIDEO_SOURCE, type LibraryVideoType } from './wire.ts';
import {
  array, closedObject, contractError, count, enumValue, openObject, text, uintString, utcInstant, type JsonRecord,
} from './strict.ts';
import { coverageNotice, type CoverageNotice } from './envelope.ts';
import { libraryErrorState, PREPARING_DELAY_MS, PREPARING_RETRIES } from './errors.ts';
import { PRESET_GROUPS, presetsFor, type LibraryPreset, type LibraryPresetGroup } from './presets.ts';
import {
  applyCataloguePreset, createInitialCatalogueConditions, normalizeCatalogueConditions, type CatalogueConditions,
} from '../catalogueQuery.ts';

export const OVERVIEW_OPERATIONS = {
  fingerprint: OPERATIONS.fingerprint,
  rhythm: OPERATIONS.rhythm,
  years: OPERATIONS.years,
  breakdown: OPERATIONS.breakdown,
} as const;
export const HEADER_OPERATIONS = {
  counterChange: OPERATIONS.counterChange,
  formatTotals: OPERATIONS.formatTotals,
} as const;
export type OverviewOperation = typeof OVERVIEW_OPERATIONS[keyof typeof OVERVIEW_OPERATIONS];
export type HeaderOperation = typeof HEADER_OPERATIONS[keyof typeof HEADER_OPERATIONS];
export const CHANNEL_SOURCE = 'public.channels.current.v1';

export type OverviewPeriodKey = '7d' | '28d' | '3m' | '1y' | 'max';
const PERIODS: Readonly<Record<OverviewPeriodKey, { label: string; days: number | null }>> = {
  '7d': { label: '7D', days: 7 }, '28d': { label: '28D', days: 28 }, '3m': { label: '3M', days: 90 },
  '1y': { label: '1Y', days: 365 }, max: { label: 'MAX', days: null },
};
const CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/;
const ZONE = /^(?:UTC|[A-Za-z]+(?:\/[A-Za-z0-9_+-]+){1,2})$/;

// ---------------------------------------------------------------------------
// Requests

/** The population query of every Overview operation: one channel, one type, the viewer zone. */
export function overviewQuery(channelId: string, type: LibraryVideoType, timeZone: string) {
  if (!CHANNEL_ID.test(channelId)) throw new Error('The Overview needs one channel.');
  if (!LIBRARY_VIDEO_TYPES.includes(type)) throw new Error('Unsupported video type.');
  if (!ZONE.test(timeZone)) throw new Error('Unsupported time zone.');
  return {
    query_version: QUERY_VERSION,
    source: { id: VIDEO_SOURCE },
    scope: { kind: 'channel' as const, id: channelId },
    video_types: [type],
    select: ['video.id'],
    order_by: [{ field: 'video.id', direction: 'asc' as const }],
    page: { size: 1 },
    time: { mode: 'current' as const, timezone: timeZone },
  };
}

export type OverviewRequestBody = {
  version: 1;
  operation: OverviewOperation;
  query: ReturnType<typeof overviewQuery>;
  parameters: { period?: OverviewPeriodKey; year?: number };
};

/** One Overview operation. The calendar takes no period; the others take the period key. */
export function overviewRequest(
  operation: OverviewOperation,
  channelId: string,
  type: LibraryVideoType,
  timeZone: string,
  options: { period?: OverviewPeriodKey; year?: number } = {},
): OverviewRequestBody {
  const query = overviewQuery(channelId, type, timeZone);
  if (operation === OVERVIEW_OPERATIONS.years) {
    if (options.period !== undefined) throw new Error('The publication calendar has no period.');
    if (options.year !== undefined && (!Number.isSafeInteger(options.year) || options.year < 1 || options.year > 9999)) {
      throw new Error('Unsupported calendar year.');
    }
    return { version: 1, operation, query, parameters: options.year === undefined ? {} : { year: options.year } };
  }
  if (options.year !== undefined) throw new Error('Only the publication calendar takes a year.');
  if (!options.period || !Object.hasOwn(PERIODS, options.period)) throw new Error('An Overview period is required.');
  return { version: 1, operation, query, parameters: { period: options.period } };
}

export type HeaderRequestBody = {
  version: 1;
  operation: HeaderOperation;
  query: {
    query_version: 1; source: { id: string }; scope: { kind: 'channel'; id: string };
    select: string[]; order_by: Array<{ field: string; direction: 'asc' }>; page: { size: 1 };
  };
  parameters: Record<string, never>;
};

/** A header operation: the channel source, one channel, no parameters. */
export function headerRequest(operation: HeaderOperation, channelId: string): HeaderRequestBody {
  if (!CHANNEL_ID.test(channelId)) throw new Error('The header needs one channel.');
  return {
    version: 1,
    operation,
    query: {
      query_version: QUERY_VERSION,
      source: { id: CHANNEL_SOURCE },
      scope: { kind: 'channel', id: channelId },
      select: ['channel.id'],
      order_by: [{ field: 'channel.id', direction: 'asc' }],
      page: { size: 1 },
    },
    parameters: {},
  };
}

// ---------------------------------------------------------------------------
// Capabilities: the Overview gate and the per-type bucket sets

export type OverviewCapabilities = {
  operations: ReadonlySet<string>;
  /** Server bucket sets by group and type; null when the server does not send `presets`. */
  presets: Partial<Record<LibraryPresetGroup, Record<LibraryVideoType, LibraryPreset[]>>> | null;
};

function presetList(value: unknown, path: string): LibraryPreset[] {
  const items = array(value, path, 64).map((item, index) => {
    const preset = closedObject(item, `${path}.${index}`, ['key', 'label', 'min', 'max']);
    const bound = (raw: unknown, at: string) => (raw === null ? null : count(raw, at));
    return {
      key: text(preset.key, `${path}.${index}.key`, 64),
      label: text(preset.label, `${path}.${index}.label`, 128),
      min: bound(preset.min, `${path}.${index}.min`),
      max: bound(preset.max, `${path}.${index}.max`),
    };
  });
  if (!items.length || new Set(items.map((item) => item.key)).size !== items.length) contractError(path, 'expected unique buckets.');
  return items;
}

/**
 * Only the parts of `/research/capabilities` the Overview reads: the operation
 * set (gates) and `presets` (bucket sets). The document itself stays open: other
 * parts belong to other readers.
 */
export function parseOverviewCapabilities(value: unknown): OverviewCapabilities {
  const root = openObject(value, 'capabilities');
  if (root.source_id !== VIDEO_SOURCE) contractError('capabilities.source_id', 'unexpected source.');
  const operations = new Set(array(root.operations ?? [], 'capabilities.operations', 256)
    .map((item, index) => text(item, `capabilities.operations.${index}`, 96)));
  if (root.presets === undefined) return { operations, presets: null };
  const presets = closedObject(root.presets, 'capabilities.presets', ['version', 'groups']);
  if (presets.version !== 1) contractError('capabilities.presets.version', 'expected 1.');
  const groups = openObject(presets.groups, 'capabilities.presets.groups');
  const result: Partial<Record<LibraryPresetGroup, Record<LibraryVideoType, LibraryPreset[]>>> = {};
  for (const group of PRESET_GROUPS) {
    if (!Object.hasOwn(groups, group)) continue;
    const item = closedObject(groups[group], `capabilities.presets.groups.${group}`, ['field', 'buckets']);
    text(item.field, `capabilities.presets.groups.${group}.field`, 96);
    const buckets = closedObject(item.buckets, `capabilities.presets.groups.${group}.buckets`, [...LIBRARY_VIDEO_TYPES]);
    result[group] = Object.fromEntries(LIBRARY_VIDEO_TYPES.map((type) => [type,
      presetList(buckets[type], `capabilities.presets.groups.${group}.buckets.${type}`)])) as Record<LibraryVideoType, LibraryPreset[]>;
  }
  return { operations, presets: result };
}

/** All four Overview operations allowed (descriptive_analytics: the old `baseline` gate). */
export function overviewAvailable(capabilities: OverviewCapabilities): boolean {
  return Object.values(OVERVIEW_OPERATIONS).every((operation) => capabilities.operations.has(operation));
}

/** Bucket set of one group and type: the server's (capabilities), else the bundled shared JSON. */
export function overviewPresets(
  capabilities: OverviewCapabilities | null | undefined, type: LibraryVideoType, group: LibraryPresetGroup,
): readonly LibraryPreset[] | null {
  return capabilities?.presets?.[group]?.[type] ?? presetsFor(type, group);
}

// ---------------------------------------------------------------------------
// Envelope

const ENVELOPE = ['version', 'operation', 'operation_fingerprint', 'context', 'coverage', 'result', 'provenance', 'warnings'];
const CONTEXT = ['source_id', 'registry_version', 'evaluated_at', 'valid_before', 'preferences_revision', 'consistency',
  'observation_age_limit_seconds'];
const MICROSECOND = 1_000_000n;

function micros(value: string, path: string): bigint {
  const parsed = utcTimestampMicroseconds(value);
  if (parsed === null) return contractError(path, 'expected a UTC timestamp.');
  return parsed;
}

type Envelope = {
  operationFingerprint: string;
  evaluatedAt: string;
  validBefore: string;
  coverage: CoverageNotice;
  result: JsonRecord;
};

function operationEnvelope(value: unknown, operation: OverviewOperation | HeaderOperation, source: string): Envelope {
  // The HTTP middleware may add the read clock to the body; it never outlives the result.
  const root = closedObject(value, 'operation', ENVELOPE, ['source_read_at', 'source_expires_at']);
  if (root.version !== 1) contractError('operation.version', 'expected 1.');
  if (root.operation !== operation) contractError('operation.operation', `expected ${operation}.`);
  const fingerprint = text(root.operation_fingerprint, 'operation.operation_fingerprint', 64);
  if (!/^[a-f0-9]{64}$/.test(fingerprint)) contractError('operation.operation_fingerprint', 'expected a fingerprint.');
  const context = closedObject(root.context, 'operation.context', CONTEXT, ['time_zone']);
  if (context.source_id !== source) contractError('operation.context.source_id', 'unexpected source.');
  if (count(context.registry_version, 'operation.context.registry_version') < 1) contractError('operation.context.registry_version', 'expected a version.');
  if (!text(context.preferences_revision, 'operation.context.preferences_revision', 96)) {
    contractError('operation.context.preferences_revision', 'expected a revision.');
  }
  enumValue(context.consistency, 'operation.context.consistency', ['current']);
  if (context.observation_age_limit_seconds !== null) count(context.observation_age_limit_seconds, 'operation.context.observation_age_limit_seconds');
  if (context.time_zone !== undefined) text(context.time_zone, 'operation.context.time_zone', 64);
  const evaluatedAt = utcInstant(context.evaluated_at, 'operation.context.evaluated_at');
  const validBefore = utcInstant(context.valid_before, 'operation.context.valid_before');
  if (micros(validBefore, 'operation.context.valid_before') <= micros(evaluatedAt, 'operation.context.evaluated_at')) {
    contractError('operation.context.valid_before', 'the result must end after it was evaluated.');
  }
  if (root.source_read_at !== undefined && root.source_read_at !== null) utcInstant(root.source_read_at, 'operation.source_read_at');
  if (root.source_expires_at !== undefined && root.source_expires_at !== null
    && micros(utcInstant(root.source_expires_at, 'operation.source_expires_at'), 'operation.source_expires_at')
      > micros(validBefore, 'operation.context.valid_before')) {
    contractError('operation.source_expires_at', 'the response outlives its result.');
  }
  const coverageValue = closedObject(root.coverage, 'operation.coverage', ['status', 'channels_in_scope', 'channels_complete', 'reasons'],
    ['basis', 'eligible_count']);
  const inScope = count(coverageValue.channels_in_scope, 'operation.coverage.channels_in_scope');
  if (count(coverageValue.channels_complete, 'operation.coverage.channels_complete') > inScope) {
    contractError('operation.coverage.channels_complete', 'more complete channels than channels in scope.');
  }
  if (coverageValue.basis !== undefined) {
    enumValue(coverageValue.basis, 'operation.coverage.basis', [source === CHANNEL_SOURCE ? 'channel_profiles' : 'channel_inventory']);
  }
  if (coverageValue.eligible_count !== undefined && coverageValue.eligible_count !== null) {
    count(coverageValue.eligible_count, 'operation.coverage.eligible_count');
  }
  const coverage = coverageNotice(coverageValue);
  const provenance = closedObject(root.provenance, 'operation.provenance', ['origin', 'method', 'population', 'missing_values', 'definition_epoch']);
  enumValue(provenance.origin, 'operation.provenance.origin', ['calculated']);
  if (provenance.method !== operation) contractError('operation.provenance.method', `expected ${operation}.`);
  text(provenance.population, 'operation.provenance.population', 96);
  text(provenance.missing_values, 'operation.provenance.missing_values', 96);
  if (provenance.definition_epoch !== null) text(provenance.definition_epoch, 'operation.provenance.definition_epoch', 96);
  array(root.warnings, 'operation.warnings', 32).forEach((item, index) => text(item, `operation.warnings.${index}`, 96));
  return {
    operationFingerprint: fingerprint, evaluatedAt, validBefore, coverage,
    result: openObject(root.result, 'operation.result'),
  };
}

// ---------------------------------------------------------------------------
// Shared result parts

export type OverviewPeriod = {
  key: OverviewPeriodKey;
  label: string;
  kind: 'rolling' | 'max';
  days: number | null;
  /** Exact moments of the server; null start for MAX (every video, unknown dates too). */
  start_at: string | null;
  end_at: string;
};

function period(value: unknown, path: string, expected: OverviewPeriodKey, evaluatedAt: string): OverviewPeriod {
  const item = closedObject(value, path, ['key', 'label', 'kind', 'days', 'start_at', 'end_at']);
  if (item.key !== expected) contractError(`${path}.key`, `expected ${expected}.`);
  const { label, days } = PERIODS[expected];
  if (item.label !== label) contractError(`${path}.label`, `expected ${label}.`);
  if (item.kind !== (days === null ? 'max' : 'rolling')) contractError(`${path}.kind`, 'unexpected period kind.');
  if (item.days !== days) contractError(`${path}.days`, 'unexpected period length.');
  const end = utcInstant(item.end_at, `${path}.end_at`);
  // The period ends at the statement's own evaluation instant.
  if (micros(end, `${path}.end_at`) !== micros(evaluatedAt, 'operation.context.evaluated_at')) {
    contractError(`${path}.end_at`, 'the period must end at the evaluation time.');
  }
  let start: string | null = null;
  if (days === null) {
    if (item.start_at !== null) contractError(`${path}.start_at`, 'MAX has no start.');
  } else {
    start = utcInstant(item.start_at, `${path}.start_at`);
    if (micros(end, `${path}.end_at`) - micros(start, `${path}.start_at`) !== BigInt(days) * 86_400n * MICROSECOND) {
      contractError(path, 'the period is not whole days of 24 hours.');
    }
  }
  return { key: expected, label, kind: days === null ? 'max' : 'rolling', days, start_at: start, end_at: end };
}

type BasePopulation = { admitted: number; matched: number; publication_unknown_excluded: number };

function basePopulation(value: JsonRecord, path: string, periodKey: OverviewPeriodKey): BasePopulation {
  const admitted = count(value.admitted, `${path}.admitted`);
  const matched = count(value.matched, `${path}.matched`);
  const excluded = count(value.publication_unknown_excluded, `${path}.publication_unknown_excluded`);
  if (matched + excluded > admitted) contractError(path, 'more rows than the population.');
  if (periodKey === 'max' && (excluded !== 0 || matched !== admitted)) contractError(path, 'MAX keeps every admitted video.');
  return { admitted, matched, publication_unknown_excluded: excluded };
}

/** Exact decimal text of a summary (half-even, at most two places) as hundredths. */
function hundredths(value: unknown, path: string): bigint {
  const match = typeof value === 'string' ? /^(0|[1-9][0-9]{0,39})(?:\.([0-9]{1,2}))?$/.exec(value) : null;
  if (!match) return contractError(path, 'expected a decimal summary.');
  return BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0'));
}

// ---------------------------------------------------------------------------
// video.fingerprint.v1

export const FINGERPRINT_METRICS = ['views', 'duration_seconds', 'likes', 'comments', 'title_length', 'tag_count'] as const;
export type FingerprintMetric = typeof FINGERPRINT_METRICS[number];
/** The component keys (`title_length` is shown as `title_length_characters`). */
export type OverviewMetricKey = 'views' | 'duration_seconds' | 'likes' | 'comments' | 'title_length_characters' | 'tag_count';
const METRIC_KEY: Record<FingerprintMetric, OverviewMetricKey> = {
  views: 'views', duration_seconds: 'duration_seconds', likes: 'likes', comments: 'comments',
  title_length: 'title_length_characters', tag_count: 'tag_count',
};
const METRIC_GROUP: Record<FingerprintMetric, LibraryPresetGroup> = {
  views: 'views', duration_seconds: 'duration', likes: 'likes', comments: 'comments', title_length: 'title_length', tag_count: 'tag_count',
};
export const METRIC_STATES = ['known', 'no_known_values', 'mixed_definition_epochs', 'unknown_definition_epoch'] as const;
export type MetricState = typeof METRIC_STATES[number];
/** Why a metric has no summary. */
export const METRIC_STATE_TEXT: Readonly<Record<Exclude<MetricState, 'known'>, string>> = {
  no_known_values: 'No known values',
  mixed_definition_epochs: 'Views definition changed in this period',
  unknown_definition_epoch: 'Views without a recorded definition',
};
export const REJECTIONS = ['identity', 'source_missing', 'source_mismatch', 'source_stale', 'source_unavailable',
  'source_unpublished', 'stream_incomplete'] as const;
export type Rejection = typeof REJECTIONS[number];
const SUMMARY_VALUES = ['total', 'mean', 'median', 'q1', 'q3', 'min', 'max'] as const;

export type OverviewMetricSummary = {
  state: MetricState;
  /** Null when known; else the reason text the row shows instead of a summary. */
  unavailable_reason: string | null;
  known_count: number;
  missing_count: number;
  excluded_count: number;
  definition_epoch: string | null;
  total: string | null;
  mean: string | null;
  median: string | null;
  q1: string | null;
  q3: string | null;
  min: string | null;
  max: string | null;
};
export type OverviewBucket = { key: string; label: string; count: number };
export type OverviewDistributionCoverage = { known_count: number; missing_count: number; excluded_count: number };

/** The Overview population (the menu's Coverage block) — scanned, admitted, why not admitted, stale. */
export type OverviewCoverage = {
  scanned: number;
  admitted: number;
  matched: number;
  publication_unknown_excluded: number;
  /** Admitted videos with stale values; they stay in the sample, the values count as unknown. */
  stale_count: number;
  stale_counter_count: number;
  stale_metadata_count: number;
  rejected: Record<Rejection, number>;
  /** `coverage.status` of the server (one completeness rule). */
  partial: boolean;
  inventory_incomplete: number;
};

export type FingerprintView = {
  operation_fingerprint: string;
  evaluated_at: string;
  video_type: LibraryVideoType;
  period: OverviewPeriod;
  sample: { matched_video_count: number; scanned_inventory_count: number; inventory_complete: boolean };
  coverage: OverviewCoverage;
  statistics: Record<OverviewMetricKey, OverviewMetricSummary>;
  distributions: Record<OverviewMetricKey, OverviewBucket[]>;
  distribution_coverage: Record<OverviewMetricKey, OverviewDistributionCoverage>;
  /** Uploads = videos of the period with a known publication time. */
  publication_cadence: { known_publication_count: number; unknown_publication_count: number };
};

export type FingerprintExpectation = {
  videoType: LibraryVideoType;
  period: OverviewPeriodKey;
  /** Bucket sets of the type (capabilities). */
  presets: (group: LibraryPresetGroup) => readonly LibraryPreset[] | null;
};

function metricSummary(
  value: unknown, path: string, name: FingerprintMetric, matched: number, presets: readonly LibraryPreset[] | null,
): { summary: OverviewMetricSummary; buckets: OverviewBucket[] } {
  const keys = ['known_count', 'missing_count', 'excluded_count', 'definition_epoch', 'state', ...SUMMARY_VALUES, 'buckets'];
  const metric = closedObject(value, path, name === 'views' ? [...keys, 'unknown_definition_epoch_count'] : keys);
  const known = count(metric.known_count, `${path}.known_count`);
  const missing = count(metric.missing_count, `${path}.missing_count`);
  const excluded = count(metric.excluded_count, `${path}.excluded_count`);
  if (known + missing + excluded !== matched) contractError(path, 'known, missing and excluded must cover the matched videos.');
  const state = enumValue(metric.state, `${path}.state`, name === 'views' ? METRIC_STATES : ['known', 'no_known_values'] as const);
  // A summary exists exactly when values are known (one undefined value is left out, not the metric).
  if ((state === 'known') !== (known > 0)) contractError(`${path}.state`, 'disagrees with the known count.');
  if (name !== 'views' && excluded !== 0) contractError(`${path}.excluded_count`, 'only views values are left out by a definition.');
  let epoch: string | null = null;
  if (name === 'views') {
    const undefinedValues = count(metric.unknown_definition_epoch_count, `${path}.unknown_definition_epoch_count`);
    if (metric.definition_epoch !== null) epoch = text(metric.definition_epoch, `${path}.definition_epoch`, 96);
    if ((epoch !== null) !== (state === 'known')) contractError(`${path}.definition_epoch`, 'a known views summary names its definition.');
    if (state === 'known' && undefinedValues !== excluded) contractError(`${path}.excluded_count`, 'excluded views are the undefined values.');
    if (state === 'no_known_values' && excluded !== 0) contractError(`${path}.excluded_count`, 'no reported values to exclude.');
    if (undefinedValues > excluded) contractError(`${path}.unknown_definition_epoch_count`, 'more undefined values than excluded.');
  } else if (metric.definition_epoch !== null) {
    contractError(`${path}.definition_epoch`, 'only views carry a definition.');
  }
  const values: Partial<Record<typeof SUMMARY_VALUES[number], string | null>> = {};
  const scaled: Partial<Record<typeof SUMMARY_VALUES[number], bigint>> = {};
  for (const key of SUMMARY_VALUES) {
    if (known === 0) {
      if (metric[key] !== null) contractError(`${path}.${key}`, 'no known values, no summary.');
      values[key] = null;
    } else {
      scaled[key] = hundredths(metric[key], `${path}.${key}`);
      values[key] = metric[key] as string;
    }
  }
  if (known > 0) {
    const ordered = [scaled.min!, scaled.q1!, scaled.median!, scaled.q3!, scaled.max!];
    if (ordered.some((item, index) => index > 0 && item < ordered[index - 1])) contractError(path, 'quartiles out of order.');
    if (scaled.mean! < scaled.min! || scaled.mean! > scaled.max!) contractError(`${path}.mean`, 'outside the range.');
    if (scaled.total! < scaled.max!) contractError(`${path}.total`, 'smaller than the largest value.');
  }
  const rawBuckets = array(metric.buckets, `${path}.buckets`, 64);
  if (!presets) contractError(`${path}.buckets`, 'no bucket set for this type.');
  if (rawBuckets.length !== presets!.length) contractError(`${path}.buckets`, 'unexpected bucket set.');
  const buckets = rawBuckets.map((item, index) => {
    const bucket = closedObject(item, `${path}.buckets.${index}`, ['key', 'label', 'count']);
    const preset = presets![index];
    // Order, keys and labels of the type's set: a bucket opens the same preset in Videos.
    if (bucket.key !== preset.key) contractError(`${path}.buckets.${index}.key`, `expected ${preset.key}.`);
    if (bucket.label !== preset.label) contractError(`${path}.buckets.${index}.label`, `expected ${preset.label}.`);
    return { key: preset.key, label: preset.label, count: count(bucket.count, `${path}.buckets.${index}.count`) };
  });
  if (buckets.reduce((sum, bucket) => sum + bucket.count, 0) !== known) contractError(`${path}.buckets`, 'buckets must add up to the known values.');
  return {
    summary: {
      state, unavailable_reason: state === 'known' ? null : METRIC_STATE_TEXT[state], known_count: known, missing_count: missing,
      excluded_count: excluded, definition_epoch: epoch,
      total: values.total ?? null, mean: values.mean ?? null, median: values.median ?? null, q1: values.q1 ?? null,
      q3: values.q3 ?? null, min: values.min ?? null, max: values.max ?? null,
    },
    buckets,
  };
}

function parseFingerprintRaw(raw: unknown, expect: FingerprintExpectation): FingerprintView {
  const envelope = operationEnvelope(raw, OVERVIEW_OPERATIONS.fingerprint, VIDEO_SOURCE);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, ['period', 'population', 'uploads', 'metrics']);
  const shownPeriod = period(result.period, `${path}.period`, expect.period, envelope.evaluatedAt);
  const population = closedObject(result.population, `${path}.population`, ['admitted', 'matched', 'publication_unknown_excluded',
    'scanned', 'stale_count', 'stale_counter_count', 'stale_metadata_count', 'rejected']);
  const base = basePopulation(population, `${path}.population`, expect.period);
  const scanned = count(population.scanned, `${path}.population.scanned`);
  const stale = count(population.stale_count, `${path}.population.stale_count`);
  const staleCounters = count(population.stale_counter_count, `${path}.population.stale_counter_count`);
  const staleMetadata = count(population.stale_metadata_count, `${path}.population.stale_metadata_count`);
  if (stale > base.admitted || staleCounters > stale || staleMetadata > stale || stale > staleCounters + staleMetadata) {
    contractError(`${path}.population`, 'inconsistent stale counts.');
  }
  const rejectedValue = closedObject(population.rejected, `${path}.population.rejected`, REJECTIONS);
  const rejected = Object.fromEntries(REJECTIONS.map((reason) => [reason,
    count(rejectedValue[reason], `${path}.population.rejected.${reason}`)])) as Record<Rejection, number>;
  if (base.admitted + Object.values(rejected).reduce((sum, item) => sum + item, 0) > scanned) {
    contractError(`${path}.population`, 'more videos than scanned.');
  }
  const uploads = closedObject(result.uploads, `${path}.uploads`, ['known_count', 'unknown_count']);
  const knownUploads = count(uploads.known_count, `${path}.uploads.known_count`);
  const unknownUploads = count(uploads.unknown_count, `${path}.uploads.unknown_count`);
  if (knownUploads + unknownUploads !== base.matched) contractError(`${path}.uploads`, 'uploads must cover the matched videos.');
  if (expect.period !== 'max' && unknownUploads !== 0) contractError(`${path}.uploads`, 'a rolling period places only known dates.');
  const metrics = closedObject(result.metrics, `${path}.metrics`, FINGERPRINT_METRICS);
  const statistics = {} as Record<OverviewMetricKey, OverviewMetricSummary>;
  const distributions = {} as Record<OverviewMetricKey, OverviewBucket[]>;
  const distributionCoverage = {} as Record<OverviewMetricKey, OverviewDistributionCoverage>;
  for (const name of FINGERPRINT_METRICS) {
    const { summary, buckets } = metricSummary(metrics[name], `${path}.metrics.${name}`, name, base.matched,
      expect.presets(METRIC_GROUP[name]));
    const key = METRIC_KEY[name];
    statistics[key] = summary;
    distributions[key] = buckets;
    distributionCoverage[key] = { known_count: summary.known_count, missing_count: summary.missing_count, excluded_count: summary.excluded_count };
  }
  const coverage: OverviewCoverage = {
    scanned, admitted: base.admitted, matched: base.matched, publication_unknown_excluded: base.publication_unknown_excluded,
    stale_count: stale, stale_counter_count: staleCounters, stale_metadata_count: staleMetadata, rejected,
    partial: envelope.coverage.partial, inventory_incomplete: envelope.coverage.inventoryIncomplete,
  };
  return {
    operation_fingerprint: envelope.operationFingerprint,
    evaluated_at: envelope.evaluatedAt,
    video_type: expect.videoType,
    period: shownPeriod,
    sample: { matched_video_count: base.matched, scanned_inventory_count: scanned, inventory_complete: coverage.inventory_incomplete === 0 },
    coverage,
    statistics,
    distributions,
    distribution_coverage: distributionCoverage,
    publication_cadence: { known_publication_count: knownUploads, unknown_publication_count: unknownUploads },
  };
}

// ---------------------------------------------------------------------------
// video.publication_rhythm.v1

/** What the rhythm rings draw: counts only; no list of points. */
export type RhythmView = {
  operation_fingerprint: string;
  evaluated_at: string;
  period: OverviewPeriod;
  time_zone: string;
  total: number;
  first_published_at: string | null;
  last_published_at: string | null;
  /** 24 local hours. */
  byHour: number[];
  /** 7 weekdays, index 0 = Monday (ISO 1). */
  byWeekday: number[];
  /** weekday × hour. */
  grid: number[][];
};

function denseCounts(value: unknown, path: string, key: 'hour' | 'weekday', size: number, first: number): number[] {
  const items = array(value, path, size);
  if (items.length !== size) contractError(path, `expected ${size} items.`);
  return items.map((item, index) => {
    const entry = closedObject(item, `${path}.${index}`, [key, 'count']);
    if (entry[key] !== index + first) contractError(`${path}.${index}.${key}`, 'out of order.');
    return count(entry.count, `${path}.${index}.count`);
  });
}

function peak(value: unknown, path: string, key: 'hour' | 'weekday', counts: number[], first: number): void {
  const best = Math.max(0, ...counts);
  if (best === 0) {
    if (value !== null) contractError(path, 'no publications, no peak.');
    return;
  }
  const item = closedObject(value, path, [key, 'count']);
  if (item[key] !== counts.indexOf(best) + first || item.count !== best) contractError(path, 'expected the first maximum.');
}

function parseRhythmRaw(raw: unknown, expect: { period: OverviewPeriodKey; timeZone: string }): RhythmView {
  const envelope = operationEnvelope(raw, OVERVIEW_OPERATIONS.rhythm, VIDEO_SOURCE);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, ['period', 'population', 'time_zone', 'known_count', 'first_published_at',
    'last_published_at', 'hours', 'weekdays', 'grid', 'peak_hour', 'peak_weekday']);
  const shownPeriod = period(result.period, `${path}.period`, expect.period, envelope.evaluatedAt);
  const population = basePopulation(closedObject(result.population, `${path}.population`,
    ['admitted', 'matched', 'publication_unknown_excluded']), `${path}.population`, expect.period);
  if (result.time_zone !== expect.timeZone) contractError(`${path}.time_zone`, 'another time zone.');
  const known = count(result.known_count, `${path}.known_count`);
  if (known > population.matched) contractError(`${path}.known_count`, 'more publications than videos.');
  const byHour = denseCounts(result.hours, `${path}.hours`, 'hour', 24, 0);
  const byWeekday = denseCounts(result.weekdays, `${path}.weekdays`, 'weekday', 7, 1);
  const rows = array(result.grid, `${path}.grid`, 7);
  if (rows.length !== 7) contractError(`${path}.grid`, 'expected 7 weekdays.');
  const grid = rows.map((item, index) => {
    const row = closedObject(item, `${path}.grid.${index}`, ['weekday', 'hours']);
    if (row.weekday !== index + 1) contractError(`${path}.grid.${index}.weekday`, 'out of order.');
    const hours = array(row.hours, `${path}.grid.${index}.hours`, 24);
    if (hours.length !== 24) contractError(`${path}.grid.${index}.hours`, 'expected 24 hours.');
    return hours.map((cell, hour) => count(cell, `${path}.grid.${index}.hours.${hour}`));
  });
  const sum = (values: number[]) => values.reduce((total, item) => total + item, 0);
  if (sum(byHour) !== known || sum(byWeekday) !== known) contractError(path, 'counts must add up to the known publications.');
  if (grid.some((row, day) => sum(row) !== byWeekday[day]) || byHour.some((value, hour) => sum(grid.map((row) => row[hour])) !== value)) {
    contractError(`${path}.grid`, 'disagrees with the rings.');
  }
  peak(result.peak_hour, `${path}.peak_hour`, 'hour', byHour, 0);
  peak(result.peak_weekday, `${path}.peak_weekday`, 'weekday', byWeekday, 1);
  let first: string | null = null;
  let last: string | null = null;
  if (known === 0) {
    if (result.first_published_at !== null || result.last_published_at !== null) contractError(path, 'no publications, no range.');
  } else {
    first = utcInstant(result.first_published_at, `${path}.first_published_at`);
    last = utcInstant(result.last_published_at, `${path}.last_published_at`);
    const from = micros(first, `${path}.first_published_at`);
    const to = micros(last, `${path}.last_published_at`);
    if (from > to || to > micros(shownPeriod.end_at, `${path}.period.end_at`)
      || (shownPeriod.start_at !== null && from < micros(shownPeriod.start_at, `${path}.period.start_at`))) {
      contractError(path, 'publications outside the period.');
    }
  }
  return {
    operation_fingerprint: envelope.operationFingerprint, evaluated_at: envelope.evaluatedAt, period: shownPeriod,
    time_zone: expect.timeZone, total: known, first_published_at: first, last_published_at: last, byHour, byWeekday, grid,
  };
}

// ---------------------------------------------------------------------------
// video.publication_years.v1

export type CalendarYearView = {
  time_zone: string;
  default_year: number;
  available_years: number[];
  year: number;
  days: Array<{ date: string; count: number }>;
};

function realDate(value: string): boolean {
  const [year, month, day] = value.split('-').map(Number);
  const check = new Date(0);
  check.setUTCFullYear(year, month - 1, day);
  return check.getUTCFullYear() === year && check.getUTCMonth() === month - 1 && check.getUTCDate() === day;
}

function parseYearsRaw(raw: unknown, expect: { timeZone: string; year?: number }): CalendarYearView {
  const envelope = operationEnvelope(raw, OVERVIEW_OPERATIONS.years, VIDEO_SOURCE);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, ['time_zone', 'population', 'known_count', 'default_year', 'year',
    'available_years', 'days']);
  if (result.time_zone !== expect.timeZone) contractError(`${path}.time_zone`, 'another time zone.');
  const population = count(result.population, `${path}.population`);
  const known = count(result.known_count, `${path}.known_count`);
  if (known > population) contractError(`${path}.known_count`, 'more dates than videos.');
  const yearValue = (value: unknown, at: string) => {
    const year = count(value, at);
    if (year < 1 || year > 9999) contractError(at, 'unsupported year.');
    return year;
  };
  const years = array(result.available_years, `${path}.available_years`, 9999)
    .map((item, index) => yearValue(item, `${path}.available_years.${index}`));
  if (!years.length || years.some((year, index) => index > 0 && year >= years[index - 1])) {
    contractError(`${path}.available_years`, 'expected distinct years, newest first.');
  }
  const defaultYear = yearValue(result.default_year, `${path}.default_year`);
  if (!years.includes(defaultYear)) contractError(`${path}.default_year`, 'the current year is always offered.');
  // The default year is the current year of the zone at the evaluation time.
  const zonedYear = Number(new Intl.DateTimeFormat('en-US', { timeZone: expect.timeZone, year: 'numeric' })
    .format(new Date(Date.parse(envelope.evaluatedAt.replace(/(\.\d{3})\d+Z$/, '$1Z')))));
  if (Number.isFinite(zonedYear) && zonedYear !== defaultYear) contractError(`${path}.default_year`, 'not the current year of the zone.');
  const year = yearValue(result.year, `${path}.year`);
  if (year !== (expect.year ?? defaultYear)) contractError(`${path}.year`, 'another year.');
  const prefix = String(year).padStart(4, '0');
  let previous = '';
  let total = 0;
  const days = array(result.days, `${path}.days`, 366).map((item, index) => {
    const day = closedObject(item, `${path}.days.${index}`, ['date', 'count']);
    const date = text(day.date, `${path}.days.${index}.date`, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !date.startsWith(`${prefix}-`) || !realDate(date) || date <= previous) {
      contractError(`${path}.days.${index}.date`, 'expected ordered dates of the year.');
    }
    const value = count(day.count, `${path}.days.${index}.count`);
    if (value === 0) contractError(`${path}.days.${index}.count`, 'a listed day has publications.');
    previous = date;
    total += value;
    return { date, count: value };
  });
  if (total > known) contractError(`${path}.days`, 'more publications than known dates.');
  return { time_zone: expect.timeZone, default_year: defaultYear, available_years: years, year, days };
}

// ---------------------------------------------------------------------------
// video.content_breakdown.v1

const BREAKDOWN_SECTIONS = {
  official_categories: 'youtube.video.snippet.categoryId',
  youtube_topics: 'youtube.video.topicDetails.topicCategories',
  audio_languages: 'youtube.video.snippet.defaultAudioLanguage',
  metadata_languages: 'youtube.video.snippet.defaultLanguage',
  captions: 'youtube.video.contentDetails.caption',
  catalogue_age: 'leti_cook.video_age_from_published_at',
} as const;
type BreakdownSectionKey = keyof typeof BREAKDOWN_SECTIONS;
const FIXED_LABELS: Partial<Record<BreakdownSectionKey, Record<string, string>>> = {
  captions: { available: 'Captions available', unavailable: 'No captions' },
  catalogue_age: { under_1y: 'Under 1 year', '1_3y': '1-3 years', '3_5y': '3-5 years', '5y_plus': '5+ years' },
};
export const BREAKDOWN_GROUP_LIMIT = 250;

export type BreakdownView = {
  operation_fingerprint: string;
  evaluated_at: string;
  period: OverviewPeriod;
  sample_count: number;
  sections: ChannelBaselineContentBreakdown;
};

function breakdownSection(value: unknown, path: string, key: BreakdownSectionKey, sample: number): ChannelBaselineBreakdownSection {
  const multi = key === 'youtube_topics';
  const required = ['kind', 'source', 'share_basis', 'sample_count', 'known_count', 'missing_count', 'groups', 'omitted_group_count',
    ...(multi ? ['assigned_count', 'empty_count'] : []), ...(key === 'official_categories' ? ['label_expires_at'] : [])];
  const section = closedObject(value, path, required);
  if (section.kind !== (multi ? 'multi_label' : 'single_label')) contractError(`${path}.kind`, 'unexpected kind.');
  if (section.source !== BREAKDOWN_SECTIONS[key]) contractError(`${path}.source`, 'unexpected source.');
  // Topics: the share of videos with collected topics; single labels: of known rows.
  if (section.share_basis !== (multi ? 'collected_rows' : 'known_rows')) contractError(`${path}.share_basis`, 'unexpected share basis.');
  if (count(section.sample_count, `${path}.sample_count`) !== sample) contractError(`${path}.sample_count`, 'another sample.');
  const known = count(section.known_count, `${path}.known_count`);
  const missing = count(section.missing_count, `${path}.missing_count`);
  if (known + missing !== sample) contractError(path, 'known and missing must cover the sample.');
  const omitted = count(section.omitted_group_count, `${path}.omitted_group_count`);
  const rawGroups = array(section.groups, `${path}.groups`, BREAKDOWN_GROUP_LIMIT);
  if (omitted > 0 && rawGroups.length < BREAKDOWN_GROUP_LIMIT) contractError(`${path}.omitted_group_count`, 'groups left out below the limit.');
  const seen = new Set<string>();
  const groups = rawGroups.map((item, index) => {
    const group = closedObject(item, `${path}.groups.${index}`, ['key', 'label', 'count', 'share_percent']);
    const groupKey = text(group.key, `${path}.groups.${index}.key`, 2048);
    if (!groupKey || seen.has(groupKey)) contractError(`${path}.groups.${index}.key`, 'expected a distinct key.');
    seen.add(groupKey);
    const label = text(group.label, `${path}.groups.${index}.label`, 2048);
    const fixed = FIXED_LABELS[key];
    if (fixed && fixed[groupKey] !== label) contractError(`${path}.groups.${index}`, 'unexpected fixed group.');
    const value = count(group.count, `${path}.groups.${index}.count`);
    if (value === 0 || value > known) contractError(`${path}.groups.${index}.count`, 'outside the known rows.');
    const share = typeof group.share_percent === 'string' && /^(0|[1-9][0-9]{0,2})(?:\.[0-9]{1,2})?$/.test(group.share_percent)
      ? Number(group.share_percent) : NaN;
    // The server rounds count × 100 / basis to two places.
    if (!(share >= 0 && share <= 100) || Math.abs(share - value * 100 / known) > 0.0051) {
      contractError(`${path}.groups.${index}.share_percent`, 'disagrees with the count.');
    }
    return { key: groupKey, label, count: value, share_percent: group.share_percent as string };
  });
  if (groups.some((group, index) => index > 0 && group.count > groups[index - 1].count)) {
    contractError(`${path}.groups`, 'expected the largest groups first.');
  }
  if (!multi && groups.reduce((sum, group) => sum + group.count, 0) > known) contractError(`${path}.groups`, 'more videos than known.');
  const parsed: ChannelBaselineBreakdownSection = {
    kind: multi ? 'multi_label' : 'single_label', source: BREAKDOWN_SECTIONS[key], share_basis: multi ? 'collected_rows' : 'known_rows',
    sample_count: sample, known_count: known, missing_count: missing, groups, omitted_group_count: omitted,
  };
  if (multi) {
    const assigned = count(section.assigned_count, `${path}.assigned_count`);
    const empty = count(section.empty_count, `${path}.empty_count`);
    if (assigned + empty !== known) contractError(path, 'assigned and empty must cover the collected rows.');
    parsed.assigned_count = assigned;
    parsed.empty_count = empty;
  }
  if (key === 'official_categories' && section.label_expires_at !== null) utcInstant(section.label_expires_at, `${path}.label_expires_at`);
  return parsed;
}

function parseBreakdownRaw(raw: unknown, expect: { period: OverviewPeriodKey }): BreakdownView {
  const envelope = operationEnvelope(raw, OVERVIEW_OPERATIONS.breakdown, VIDEO_SOURCE);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, ['period', 'population', ...Object.keys(BREAKDOWN_SECTIONS)]);
  const shownPeriod = period(result.period, `${path}.period`, expect.period, envelope.evaluatedAt);
  const population = basePopulation(closedObject(result.population, `${path}.population`,
    ['admitted', 'matched', 'publication_unknown_excluded']), `${path}.population`, expect.period);
  const sections = Object.fromEntries((Object.keys(BREAKDOWN_SECTIONS) as BreakdownSectionKey[]).map((key) => [key,
    breakdownSection(result[key], `${path}.${key}`, key, population.matched)])) as ChannelBaselineContentBreakdown;
  return {
    operation_fingerprint: envelope.operationFingerprint, evaluated_at: envelope.evaluatedAt, period: shownPeriod,
    sample_count: population.matched, sections,
  };
}

// ---------------------------------------------------------------------------
// The Overview presentation (one object the components read)

export type OverviewPayload = {
  video_type: LibraryVideoType;
  /** Evaluation time of the fingerprint (the Coverage block's clock). */
  generated_at: string;
  period: OverviewPeriod;
  sample: FingerprintView['sample'];
  coverage: OverviewCoverage;
  statistics: FingerprintView['statistics'];
  distributions: FingerprintView['distributions'];
  distribution_coverage: FingerprintView['distribution_coverage'];
  publication_cadence: FingerprintView['publication_cadence'];
  rhythm: RhythmView;
  publication_calendar: ChannelBaselinePublicationCalendar;
  content_breakdown: ChannelBaselineContentBreakdown;
};

/**
 * One Overview from the four replies. Every part keeps its own reply's
 * lifetime (the root is only a container); the calendar lists the loaded years.
 */
export function overviewPayload(
  fingerprint: FingerprintView,
  rhythm: RhythmView,
  calendar: CalendarYearView,
  breakdown: BreakdownView,
  otherYears: readonly CalendarYearView[] = [],
): OverviewPayload {
  if (fingerprint.period.key !== rhythm.period.key || fingerprint.period.key !== breakdown.period.key) {
    throw new ExplorerContractError('Overview parts belong to different periods.');
  }
  if (rhythm.time_zone !== calendar.time_zone) throw new ExplorerContractError('Overview parts belong to different time zones.');
  const years = [calendar, ...otherYears.filter((item) => item.year !== calendar.year && item.time_zone === calendar.time_zone)]
    .map((item) => ({ year: item.year, days: item.days }));
  const publicationCalendar: ChannelBaselinePublicationCalendar = transferSourceLifetime(calendar, {
    timezone: calendar.time_zone, default_year: calendar.default_year, available_years: calendar.available_years, years,
  });
  return {
    video_type: fingerprint.video_type,
    generated_at: fingerprint.evaluated_at,
    period: fingerprint.period,
    sample: fingerprint.sample,
    coverage: fingerprint.coverage,
    statistics: fingerprint.statistics,
    distributions: fingerprint.distributions,
    distribution_coverage: fingerprint.distribution_coverage,
    publication_cadence: fingerprint.publication_cadence,
    rhythm,
    publication_calendar: publicationCalendar,
    content_breakdown: breakdown.sections,
  };
}

// ---------------------------------------------------------------------------
// channel.counter_change.v1 → the header's 7D/28D badges

export const COUNTER_STATUSES = ['ready', 'partial', 'zero_baseline', 'not_enough_history', 'stale_current', 'not_reported'] as const;
export const COUNTER_REASON_CODES = ['no_observation', 'counter_not_reported', 'latest_observation_too_old',
  'no_earlier_observation_in_window', 'zero_start_value', 'start_after_window_boundary'] as const;
/** Texts of the server's reason codes (the badge tooltip). */
export const COUNTER_REASON_TEXT: Readonly<Record<typeof COUNTER_REASON_CODES[number], string>> = {
  no_observation: 'No stored channel observation is available.',
  counter_not_reported: 'The current counter is not reported.',
  latest_observation_too_old: 'The current stored observation is stale.',
  no_earlier_observation_in_window: 'Not enough stored history for the selected comparison period.',
  zero_start_value: 'Percentage change is unavailable because the starting value is zero.',
  start_after_window_boundary: 'Partial window: the earliest stored observation is later than the window start.',
};
const WINDOWS: Readonly<Record<ChannelExplorerCounterDynamicsPeriodKey, number>> = { '7d': 7 * 24 * 3600, '28d': 28 * 24 * 3600 };

export type CounterChangeMetric = ChannelExplorerCounterDynamicsMetric & {
  coverage: 'full' | 'partial' | 'unavailable';
  reason_code: typeof COUNTER_REASON_CODES[number] | null;
};
export type CounterChangeView = {
  channel_id: string;
  generated_at: string;
  current_max_age_seconds: number;
  periods: Record<ChannelExplorerCounterDynamicsPeriodKey, {
    period: ChannelExplorerCounterDynamicsPeriodKey;
    window_seconds: number;
    tolerance_seconds: number;
    metrics: { subscribers: CounterChangeMetric; views: CounterChangeMetric };
  }>;
};

const PERCENT = /^-?(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,6})?$/;

function counterMetric(
  value: unknown, path: string, windowSeconds: number, toleranceSeconds: number, evaluatedAt: bigint, maximumAge: number,
): CounterChangeMetric {
  const metric = closedObject(value, path, ['status', 'coverage', 'start_value', 'end_value', 'absolute_change', 'percent_change',
    'observed_start_at', 'observed_end_at', 'actual_interval_seconds', 'expires_at', 'reason_code']);
  const status = enumValue(metric.status, `${path}.status`, COUNTER_STATUSES);
  const coverage = enumValue(metric.coverage, `${path}.coverage`, ['full', 'partial', 'unavailable'] as const);
  const reason = metric.reason_code === null ? null : enumValue(metric.reason_code, `${path}.reason_code`, COUNTER_REASON_CODES);
  const nullable = <T>(raw: unknown, read: (item: unknown, at: string) => T, at: string): T | null => (raw === null ? null : read(raw, at));
  const start = nullable(metric.start_value, uintString, `${path}.start_value`);
  const end = nullable(metric.end_value, uintString, `${path}.end_value`);
  const change = metric.absolute_change === null ? null : text(metric.absolute_change, `${path}.absolute_change`, 32);
  if (change !== null && !/^-?(?:0|[1-9][0-9]{0,19})$/.test(change)) contractError(`${path}.absolute_change`, 'expected a signed whole number.');
  const percent = metric.percent_change === null ? null : text(metric.percent_change, `${path}.percent_change`, 40);
  if (percent !== null && (!PERCENT.test(percent) || /^-0(?:\.0+)?$/.test(percent))) contractError(`${path}.percent_change`, 'expected a decimal percentage.');
  const startAt = nullable(metric.observed_start_at, utcInstant, `${path}.observed_start_at`);
  const endAt = nullable(metric.observed_end_at, utcInstant, `${path}.observed_end_at`);
  const expiresAt = nullable(metric.expires_at, utcInstant, `${path}.expires_at`);
  const interval = nullable(metric.actual_interval_seconds, count, `${path}.actual_interval_seconds`);
  const compared = status === 'ready' || status === 'partial' || status === 'zero_baseline';
  const expect = (condition: boolean, message: string) => { if (!condition) contractError(path, message); };
  // Each status carries exactly its own evidence.
  const expectedReason: Record<typeof status, ReadonlyArray<string | null>> = {
    ready: [null], partial: ['start_after_window_boundary'], zero_baseline: ['zero_start_value'],
    not_enough_history: ['no_earlier_observation_in_window'], stale_current: ['latest_observation_too_old'],
    not_reported: ['no_observation', 'counter_not_reported'],
  };
  expect(expectedReason[status].includes(reason), 'the reason does not match the status.');
  const expectedCoverage: Record<typeof status, ReadonlyArray<string>> = {
    ready: ['full'], partial: ['partial'], zero_baseline: ['full', 'partial'],
    not_enough_history: ['unavailable'], stale_current: ['unavailable'], not_reported: ['unavailable'],
  };
  expect(expectedCoverage[status].includes(coverage), 'the coverage does not match the status.');
  if (reason === 'no_observation') {
    expect(endAt === null && expiresAt === null && end === null, 'no observation, no end point.');
  } else {
    expect(endAt !== null && expiresAt !== null, 'an observed end needs its clocks.');
    expect(micros(endAt!, `${path}.observed_end_at`) <= evaluatedAt, 'the end is after the evaluation.');
    expect((end === null) === (reason === 'counter_not_reported'), 'the end value does not match the status.');
  }
  if (status === 'stale_current') {
    expect(evaluatedAt - micros(endAt!, `${path}.observed_end_at`) > BigInt(maximumAge) * MICROSECOND, 'a current end is not stale.');
  } else if (endAt !== null && end !== null) {
    expect(evaluatedAt - micros(endAt, `${path}.observed_end_at`) <= BigInt(maximumAge) * MICROSECOND, 'a stale end is shown as current.');
  }
  if (!compared) {
    expect(start === null && startAt === null && change === null && percent === null && interval === null, 'no comparison, no start.');
  } else {
    expect(start !== null && startAt !== null && change !== null && interval !== null && end !== null, 'a comparison needs both ends.');
    const from = micros(startAt!, `${path}.observed_start_at`);
    const to = micros(endAt!, `${path}.observed_end_at`);
    expect(from < to && BigInt(interval!) === (to - from) / MICROSECOND, 'the actual interval disagrees with the observations.');
    expect(BigInt(change!) === BigInt(end!) - BigInt(start!), 'the change disagrees with the values.');
    // A full window lies between its length and its length plus the tolerance; a partial one is shorter.
    expect(coverage === 'full' ? interval! >= windowSeconds && interval! <= windowSeconds + toleranceSeconds : interval! < windowSeconds,
      'the interval does not match the window length.');
    if (status === 'zero_baseline') expect(start === '0' && percent === null, 'a zero baseline has no percentage.');
    else {
      expect(start !== '0' && percent !== null, 'a comparison needs its percentage.');
      const sign = (raw: string) => (raw.startsWith('-') ? -1 : /^0(?:\.0+)?$/.test(raw.replace('-', '')) ? 0 : 1);
      expect(sign(change!) === sign(percent!) || (sign(percent!) === 0 && BigInt(change!) !== 0n), 'the percentage has the wrong sign.');
    }
  }
  return {
    status, coverage, start_value: start, end_value: end, absolute_change: change, percent_change: percent,
    observed_start_at: startAt, observed_end_at: endAt, actual_interval_seconds: interval,
    reason: reason === null ? null : COUNTER_REASON_TEXT[reason], reason_code: reason,
  };
}

function parseCounterChangeRaw(raw: unknown, channelId: string): CounterChangeView {
  const envelope = operationEnvelope(raw, HEADER_OPERATIONS.counterChange, CHANNEL_SOURCE);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, ['definition_epoch', 'definition_rule', 'current_max_age_seconds', 'windows']);
  // YouTube reports channel counters without a definition version: none is recorded or required.
  if (result.definition_epoch !== null) contractError(`${path}.definition_epoch`, 'channel counters carry no definition.');
  if (result.definition_rule !== 'reported_channel_counter_without_definition_v1') contractError(`${path}.definition_rule`, 'unexpected rule.');
  const maximumAge = count(result.current_max_age_seconds, `${path}.current_max_age_seconds`);
  const windows = closedObject(result.windows, `${path}.windows`, Object.keys(WINDOWS));
  const evaluated = micros(envelope.evaluatedAt, 'operation.context.evaluated_at');
  const periods = {} as CounterChangeView['periods'];
  let end: string | null | undefined;
  for (const key of Object.keys(WINDOWS) as ChannelExplorerCounterDynamicsPeriodKey[]) {
    const at = `${path}.windows.${key}`;
    const entry = closedObject(windows[key], at, ['period', 'window_seconds', 'tolerance_seconds', 'metrics']);
    if (entry.period !== key) contractError(`${at}.period`, `expected ${key}.`);
    if (entry.window_seconds !== WINDOWS[key]) contractError(`${at}.window_seconds`, 'unexpected window length.');
    const tolerance = count(entry.tolerance_seconds, `${at}.tolerance_seconds`);
    const metrics = closedObject(entry.metrics, `${at}.metrics`, ['subscribers', 'views']);
    const read = (name: 'subscribers' | 'views') => counterMetric(metrics[name], `${at}.metrics.${name}`, WINDOWS[key], tolerance, evaluated, maximumAge);
    const subscribers = read('subscribers');
    const views = read('views');
    // Every change ends at the same latest snapshot.
    for (const metric of [subscribers, views]) {
      if (end === undefined) end = metric.observed_end_at;
      else if (end !== metric.observed_end_at) contractError(at, 'changes end at different snapshots.');
    }
    periods[key] = { period: key, window_seconds: WINDOWS[key], tolerance_seconds: tolerance, metrics: { subscribers, views } };
  }
  return { channel_id: channelId, generated_at: envelope.evaluatedAt, current_max_age_seconds: maximumAge, periods };
}

// ---------------------------------------------------------------------------
// channel.format_totals.v1 → the header's Videos / Shorts / Live views

export const FORMAT_REASON_CODES = ['inventory_not_completed', 'inventory_stale', 'playlist_not_found', 'inventory_sources_incomplete',
  'format_turned_off'] as const;
const FORMAT_EXCLUSIONS = ['stale_inventory_count', 'missing_video_row_count', 'availability_withheld_count',
  'stream_completion_withheld', 'stale_counter_count', 'stale_metadata_count', 'unknown_view_count',
  'unavailable_excluded_count', 'type_resolution_withheld'] as const;
export type FormatExclusion = typeof FORMAT_EXCLUSIONS[number];

export type FormatTotalView = {
  status: ChannelExplorerFormatStatus;
  /** Decimal string; only when ready. */
  views: string | null;
  reason: typeof FORMAT_REASON_CODES[number] | null;
  scanned_inventory_count: number | null;
  canonical_inventory_count: number | null;
  known_view_count: number | null;
  missing_view_count: number | null;
  excluded: Record<FormatExclusion, number> | null;
};
export type FormatTotalsView = {
  channel_id: string;
  generated_at: string;
  formats: Record<LibraryVideoType, FormatTotalView>;
};

function formatTotal(value: unknown, path: string, listLimit: number): FormatTotalView {
  const item = closedObject(value, path, ['status', 'views', 'reason_code', 'scanned_inventory_count', 'canonical_inventory_count',
    'known_view_count', 'missing_view_count', 'excluded', 'expires_at']);
  // turned_off: a format the owner turned off in config.ini; its total is not shown.
  const status = enumValue(item.status, `${path}.status`, ['ready', 'stale', 'incomplete', 'absent', 'not_collected', 'turned_off'] as const);
  const reason = item.reason_code === null ? null : enumValue(item.reason_code, `${path}.reason_code`, FORMAT_REASON_CODES);
  if (item.expires_at !== null) utcInstant(item.expires_at, `${path}.expires_at`);
  if (item.scanned_inventory_count === null) {
    // A format whose coverage is unconfirmed has no list counts.
    for (const key of ['views', 'canonical_inventory_count', 'known_view_count', 'missing_view_count', 'excluded'] as const) {
      if (item[key] !== null) contractError(`${path}.${key}`, 'a gated list has no counts.');
    }
    const gate: Record<typeof FORMAT_REASON_CODES[number], ReadonlyArray<string>> = {
      inventory_not_completed: ['incomplete', 'not_collected'], inventory_stale: ['stale'], playlist_not_found: ['absent'],
      inventory_sources_incomplete: [], format_turned_off: ['turned_off'],
    };
    if (reason === null || !gate[reason].includes(status)) contractError(`${path}.reason_code`, 'the gate does not match the status.');
    if (item.expires_at !== null && status !== 'absent') contractError(`${path}.expires_at`, 'only a confirmed absent list has a lifetime.');
    return { status, views: null, reason, scanned_inventory_count: null, canonical_inventory_count: null, known_view_count: null,
      missing_view_count: null, excluded: null };
  }
  if (status === 'absent' || status === 'not_collected' || status === 'turned_off') contractError(`${path}.status`, 'a counted list is not absent.');
  const scanned = count(item.scanned_inventory_count, `${path}.scanned_inventory_count`);
  const canonical = count(item.canonical_inventory_count, `${path}.canonical_inventory_count`);
  const known = count(item.known_view_count, `${path}.known_view_count`);
  const missing = count(item.missing_view_count, `${path}.missing_view_count`);
  const excludedValue = closedObject(item.excluded, `${path}.excluded`, FORMAT_EXCLUSIONS);
  const excluded = Object.fromEntries(FORMAT_EXCLUSIONS.map((key) => [key, count(excludedValue[key], `${path}.excluded.${key}`)])) as Record<FormatExclusion, number>;
  if (excluded.type_resolution_withheld !== 0) contractError(`${path}.excluded.type_resolution_withheld`, 'the list proves the type.');
  const gaps = FORMAT_EXCLUSIONS.filter((key) => key !== 'unavailable_excluded_count' && key !== 'type_resolution_withheld')
    .reduce((sum, key) => sum + excluded[key], 0);
  if (known + gaps + excluded.unavailable_excluded_count !== scanned) contractError(path, 'the classes must cover the list.');
  if ((reason === 'inventory_sources_incomplete') !== (canonical !== scanned)) contractError(`${path}.reason_code`, 'disagrees with the list size.');
  if (reason !== null && reason !== 'inventory_sources_incomplete') contractError(`${path}.reason_code`, 'a gate on a counted list.');
  // Completeness: the sum only when every listed public video has a current known counter;
  // gaps that are only stale clocks make the total stale, any other gap incomplete.
  const scopeComplete = canonical === scanned && scanned <= listLimit;
  const staleGaps = excluded.stale_inventory_count + excluded.stale_counter_count + excluded.stale_metadata_count;
  const expected = scopeComplete && gaps === 0 ? 'ready' : scopeComplete && gaps === staleGaps ? 'stale' : 'incomplete';
  if (status !== expected) contractError(`${path}.status`, 'disagrees with the list coverage.');
  let views: string | null = null;
  if (status === 'ready') views = uintString(item.views, `${path}.views`);
  else if (item.views !== null) contractError(`${path}.views`, 'an incomplete list has no total.');
  if (missing !== gaps) {
    contractError(`${path}.missing_view_count`, 'disagrees with the classes.');
  }
  return { status, views, reason, scanned_inventory_count: scanned, canonical_inventory_count: canonical, known_view_count: known,
    missing_view_count: missing, excluded };
}

function parseFormatTotalsRaw(raw: unknown, channelId: string): FormatTotalsView {
  const envelope = operationEnvelope(raw, HEADER_OPERATIONS.formatTotals, CHANNEL_SOURCE);
  const path = 'operation.result';
  const result = closedObject(envelope.result, path, ['list_limit', 'formats']);
  const listLimit = count(result.list_limit, `${path}.list_limit`);
  const formats = closedObject(result.formats, `${path}.formats`, [...LIBRARY_VIDEO_TYPES]);
  return {
    channel_id: channelId,
    generated_at: envelope.evaluatedAt,
    formats: Object.fromEntries(LIBRARY_VIDEO_TYPES.map((type) => [type,
      formatTotal(formats[type], `${path}.formats.${type}`, listLimit)])) as Record<LibraryVideoType, FormatTotalView>,
  };
}

/** Header tooltip of a format total that is not shown: what the stored list is missing (unfinished Streams are reported separately). */
export function formatTotalDetail(total: FormatTotalView | undefined): string {
  if (!total || total.status === 'ready') return '';
  if (total.reason === 'playlist_not_found') return 'YouTube reports no public list of this format.';
  if (total.reason === 'format_turned_off') return 'This format is turned off in config/config.ini (formats).';
  if (total.reason === 'inventory_stale') return 'The stored list needs refresh.';
  if (total.reason === 'inventory_not_completed') return 'The list of this format has not been collected completely.';
  const parts: string[] = [];
  if (total.reason === 'inventory_sources_incomplete') parts.push('The stored list does not cover every published video yet.');
  const excluded = total.excluded;
  if (excluded) {
    const add = (value: number, singular: string, plural: string) => { if (value > 0) parts.push(`${value} ${value === 1 ? singular : plural}`); };
    add(excluded.stream_completion_withheld, 'stream has not ended or its end is not confirmed yet.', 'streams have not ended or their end is not confirmed yet.');
    add(excluded.stale_counter_count + excluded.stale_metadata_count + excluded.stale_inventory_count,
      'video needs refreshed values.', 'videos need refreshed values.');
    add(excluded.missing_video_row_count + excluded.availability_withheld_count + excluded.unknown_view_count,
      'video has no current counter yet.', 'videos have no current counter yet.');
  }
  return parts.join(' ');
}

// ---------------------------------------------------------------------------
// Lifetime-carrying parsers (each reply keeps the clock captured by requestJson)

export function parseFingerprint(raw: unknown, expect: FingerprintExpectation): FingerprintView {
  return transferSourceLifetime(raw, parseFingerprintRaw(raw, expect));
}
export function parseRhythm(raw: unknown, expect: { period: OverviewPeriodKey; timeZone: string }): RhythmView {
  return transferSourceLifetime(raw, parseRhythmRaw(raw, expect));
}
export function parseYears(raw: unknown, expect: { timeZone: string; year?: number }): CalendarYearView {
  return transferSourceLifetime(raw, parseYearsRaw(raw, expect));
}
export function parseBreakdown(raw: unknown, expect: { period: OverviewPeriodKey }): BreakdownView {
  return transferSourceLifetime(raw, parseBreakdownRaw(raw, expect));
}
export function parseCounterChange(raw: unknown, channelId: string): CounterChangeView {
  return transferSourceLifetime(raw, parseCounterChangeRaw(raw, channelId));
}
export function parseFormatTotals(raw: unknown, channelId: string): FormatTotalsView {
  return transferSourceLifetime(raw, parseFormatTotalsRaw(raw, channelId));
}

// ---------------------------------------------------------------------------
// Client: retries of the error table (surface "overview")

export type OverviewRequest = (path: string, signal?: AbortSignal, init?: RequestInit) => Promise<unknown>;
export type OverviewWait = (milliseconds: number, signal: AbortSignal) => Promise<void>;

function abortError(): Error {
  const error = new Error('The read was cancelled.');
  error.name = 'AbortError';
  return error;
}

const defaultWait: OverviewWait = (milliseconds, signal) => new Promise((resolve, reject) => {
  if (signal.aborted) { reject(abortError()); return; }
  const timer = setTimeout(() => { signal.removeEventListener('abort', cancel); resolve(); }, milliseconds);
  const cancel = () => { clearTimeout(timer); reject(abortError()); };
  signal.addEventListener('abort', cancel, { once: true });
});

/** The delay before the next attempt, or null when the error is final (`catalogue_preparing` ≤ 2, a context change once). */
export function overviewRetryDelay(error: unknown, attempts: { preparing: number; changed: number }): number | null {
  if (error instanceof ExplorerContractError) return null;
  const state = libraryErrorState(error, 'overview');
  if (state.action === 'preparing') return attempts.preparing < PREPARING_RETRIES ? PREPARING_DELAY_MS : null;
  if (state.action === 'retry_once') return attempts.changed < 1 ? 0 : null;
  return null;
}

export type OverviewClient = {
  capabilities(signal: AbortSignal): Promise<OverviewCapabilities>;
  fingerprint(channelId: string, type: LibraryVideoType, timeZone: string, period: OverviewPeriodKey,
    presets: FingerprintExpectation['presets'], signal: AbortSignal): Promise<FingerprintView>;
  rhythm(channelId: string, type: LibraryVideoType, timeZone: string, period: OverviewPeriodKey, signal: AbortSignal): Promise<RhythmView>;
  years(channelId: string, type: LibraryVideoType, timeZone: string, year: number | undefined, signal: AbortSignal): Promise<CalendarYearView>;
  breakdown(channelId: string, type: LibraryVideoType, timeZone: string, period: OverviewPeriodKey, signal: AbortSignal): Promise<BreakdownView>;
  counterChange(channelId: string, signal: AbortSignal): Promise<CounterChangeView>;
  formatTotals(channelId: string, signal: AbortSignal): Promise<FormatTotalsView>;
};

export function createOverviewClient(request: OverviewRequest, options: { wait?: OverviewWait } = {}): OverviewClient {
  const wait = options.wait ?? defaultWait;
  async function send(path: string, body: unknown, signal: AbortSignal): Promise<unknown> {
    const attempts = { preparing: 0, changed: 0 };
    for (;;) {
      if (signal.aborted) throw abortError();
      try {
        return await request(path, signal, body === undefined ? undefined
          : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      } catch (error) {
        if (signal.aborted) throw abortError();
        const delay = overviewRetryDelay(error, attempts);
        if (delay === null) throw error;
        if (libraryErrorState(error, 'overview').action === 'preparing') attempts.preparing += 1;
        else attempts.changed += 1;
        await wait(delay, signal);
      }
    }
  }
  const operation = (body: unknown, signal: AbortSignal) => send(ROUTES.operations, body, signal);
  return {
    async capabilities(signal) {
      return parseOverviewCapabilities(await send(ROUTES.capabilities, undefined, signal));
    },
    async fingerprint(channelId, type, timeZone, periodKey, presets, signal) {
      const raw = await operation(overviewRequest(OVERVIEW_OPERATIONS.fingerprint, channelId, type, timeZone, { period: periodKey }), signal);
      return parseFingerprint(raw, { videoType: type, period: periodKey, presets });
    },
    async rhythm(channelId, type, timeZone, periodKey, signal) {
      const raw = await operation(overviewRequest(OVERVIEW_OPERATIONS.rhythm, channelId, type, timeZone, { period: periodKey }), signal);
      return parseRhythm(raw, { period: periodKey, timeZone });
    },
    async years(channelId, type, timeZone, year, signal) {
      const raw = await operation(overviewRequest(OVERVIEW_OPERATIONS.years, channelId, type, timeZone, year === undefined ? {} : { year }), signal);
      return parseYears(raw, { timeZone, ...(year === undefined ? {} : { year }) });
    },
    async breakdown(channelId, type, timeZone, periodKey, signal) {
      const raw = await operation(overviewRequest(OVERVIEW_OPERATIONS.breakdown, channelId, type, timeZone, { period: periodKey }), signal);
      return parseBreakdown(raw, { period: periodKey });
    },
    async counterChange(channelId, signal) {
      return parseCounterChange(await operation(headerRequest(HEADER_OPERATIONS.counterChange, channelId), signal), channelId);
    },
    async formatTotals(channelId, signal) {
      return parseFormatTotals(await operation(headerRequest(HEADER_OPERATIONS.formatTotals, channelId), signal), channelId);
    },
  };
}

// ---------------------------------------------------------------------------
// The bucket jump into Videos

/**
 * Library conditions that open one Overview bucket in Videos: the type's
 * preset within the exact period of the reply. The cohort is
 * [start_at, end_at]; MAX has no start and so keeps unknown publication dates,
 * as the Overview population does (`published_at gte/lte`, or
 * `is_unknown or lte` without a start). Null when the key is not in the type's
 * set or the Videos codec does not know it.
 */
export function overviewBucketConditions(
  period: Pick<OverviewPeriod, 'start_at' | 'end_at'> | undefined,
  group: LibraryPresetGroup,
  key: string,
  presets: readonly LibraryPreset[] | null,
): CatalogueConditions | null {
  if (!period?.end_at || !presets?.some((preset) => preset.key === key)) return null;
  try {
    const next = applyCataloguePreset(createInitialCatalogueConditions(), group, key);
    next.facets.cohort = { kind: 'overview', published_from: period.start_at ?? null, published_through: period.end_at };
    return normalizeCatalogueConditions(next);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Words by type: Long texts stay exactly as they were.

export type OverviewFormatWords = {
  /** "long-form" | "short-form" | "live-stream": stored long-form rows. */
  adjective: string;
  /** "long-form videos" | "Shorts" | "Streams". */
  plural: string;
  /** Tab name: "Videos" | "Shorts" | "Streams". */
  tab: string;
};
export const OVERVIEW_FORMAT_WORDS: Readonly<Record<LibraryVideoType, OverviewFormatWords>> = {
  long: { adjective: 'long-form', plural: 'long-form videos', tab: 'Videos' },
  short: { adjective: 'short-form', plural: 'Shorts', tab: 'Shorts' },
  stream: { adjective: 'live-stream', plural: 'Streams', tab: 'Streams' },
};

// ---------------------------------------------------------------------------
// Readiness per type: which Overview state the hydration allows
