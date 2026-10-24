import presetRegistry from '../../../shared/catalogue-filter-presets.json' with { type: 'json' };
import type { ChannelBaselinePeriodKey, ChannelExplorerQuery } from '../types';
import type { ExplorerFilters } from './model';
import {
  INITIAL_EXPLORER_FILTERS,
  analysisScopeKey,
  normalizeExplorerFilters,
  rawQueryFromFilters,
  validateExplorerFilters,
  STATIC_CATALOGUE_FILTER_KEYS, staticCatalogueBounds,
} from './filters.ts';
import { TIME_ZONE_PATTERN } from './timeZones.ts';

// The same condition value belongs in an address or a future Saved view.
// Interaction state, fetched pages and saved-object identity do not belong here.
export type CatalogueCohort = {
  kind: 'overview';
  published_from: string | null;
  published_through: string;
};

export type CatalogueGrowthFilters = {
  min_views_gained?: string;
  max_views_gained?: string;
  min_tracked_days?: string;
  full_history?: true;
};

export type CatalogueFacets = {
  version: 1;
  view_buckets: string[];
  duration_buckets: string[];
  published_years: number[];
  published_older_than: number | null;
  cohort: CatalogueCohort | null;
  like_buckets?: string[];
  comment_buckets?: string[];
  title_length_buckets?: string[];
  tag_count_buckets?: string[];
  growth?: CatalogueGrowthFilters;
};

export type CatalogueConditions = {
  version: 1;
  filters: ExplorerFilters;
  facets: CatalogueFacets;
};

export type CatalogueUrlState = {
  section: 'overview' | 'videos';
  overviewPeriod: ChannelBaselinePeriodKey;
  conditions: CatalogueConditions;
};

export type CataloguePreset = {
  key: string;
  label: string;
  min: number | null;
  max: number | null;
};

export const CATALOGUE_VIEW_PRESETS: readonly CataloguePreset[] = presetRegistry.views;
export const CATALOGUE_DURATION_PRESETS: readonly CataloguePreset[] = presetRegistry.duration;
export type CataloguePresetGroup = 'views' | 'duration' | 'likes' | 'comments' | 'title_length' | 'tag_count';
export type CatalogueNumericGroup = CataloguePresetGroup | 'like_rate' | 'comment_rate';
export type CatalogueFilterGroup = CatalogueNumericGroup | 'q' | 'published' | 'category' | 'cohort' | 'views_gained' | 'tracked_days' | 'full_history';
type NumericFilterKey = Exclude<keyof ExplorerFilters, 'q' | 'category_id' | 'published_from' | 'published_before' | 'sort_by' | 'sort_order'>;
type NumericGroup = { group: CatalogueNumericGroup; label: string; min: NumericFilterKey; max: NumericFilterKey; facet?: keyof Pick<CatalogueFacets, 'view_buckets' | 'duration_buckets' | 'like_buckets' | 'comment_buckets' | 'title_length_buckets' | 'tag_count_buckets'>; presets?: readonly CataloguePreset[]; unit: string; derived: boolean };
const registry = presetRegistry as unknown as Record<CataloguePresetGroup, CataloguePreset[]>;
export const CATALOGUE_NUMERIC_GROUPS: readonly NumericGroup[] = [
  { group: 'views', label: 'Views', min: 'min_views', max: 'max_views', facet: 'view_buckets', presets: registry.views, unit: '', derived: false },
  { group: 'duration', label: 'Duration', min: 'min_duration_seconds', max: 'max_duration_seconds', facet: 'duration_buckets', presets: registry.duration, unit: 'sec', derived: false },
  { group: 'likes', label: 'Likes', min: 'min_likes', max: 'max_likes', facet: 'like_buckets', presets: registry.likes, unit: '', derived: false },
  { group: 'comments', label: 'Comments', min: 'min_comments', max: 'max_comments', facet: 'comment_buckets', presets: registry.comments, unit: '', derived: false },
  { group: 'title_length', label: 'Title length', min: 'min_title_length', max: 'max_title_length', facet: 'title_length_buckets', presets: registry.title_length, unit: 'characters', derived: true },
  { group: 'tag_count', label: 'Tags', min: 'min_tag_count', max: 'max_tag_count', facet: 'tag_count_buckets', presets: registry.tag_count, unit: 'tags', derived: true },
  { group: 'like_rate', label: 'Like rate', min: 'min_like_rate', max: 'max_like_rate', unit: '%', derived: true },
  { group: 'comment_rate', label: 'Comment rate', min: 'min_comment_rate', max: 'max_comment_rate', unit: '%', derived: true },
];
export const MAX_CATALOGUE_QUERY_LENGTH = 8192;

export const DEFAULT_CATALOGUE_FACETS: CatalogueFacets = {
  version: 1,
  view_buckets: [],
  duration_buckets: [],
  published_years: [],
  published_older_than: null,
  cohort: null,
};

export class CatalogueQueryError extends Error {
  readonly field: string;

  constructor(field: string, message: string) {
    super(message);
    this.name = 'CatalogueQueryError';
    this.field = field;
  }
}

const filterKeys = Object.keys(INITIAL_EXPLORER_FILTERS) as Array<keyof ExplorerFilters>;
const sortFields = new Set([
  'published_at', 'views', 'like_count', 'comment_count', 'duration', 'title', 'views_gained',
]);
const periods = new Set(['7d', '28d', '3m', '1y', 'max']);
const integerFilterKeys = [
  'category_id', 'min_views', 'max_views', 'min_duration_seconds', 'max_duration_seconds',
] as const;

function fail(field: string, message: string): never {
  throw new CatalogueQueryError(field, message);
}

function record(value: unknown, field: string, allowedKeys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    return fail(field, `${field} must be an object.`);
  }
  const data = value as Record<string, unknown>;
  for (const key of Object.keys(data)) {
    if (!allowedKeys.includes(key)) fail(`${field}.${key}`, `Unsupported ${field} field: ${key}.`);
  }
  return data;
}

function validYear(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > 9999) {
    return fail(field, 'Publication years must be integers between 1 and 9999.');
  }
  return value;
}

function validCalendarDate(value: string, field: string): Date {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-')) {
    return fail(field, 'Use a valid calendar date in YYYY-MM-DD format.');
  }
  const date = new Date(`${value}T00:00:00.000Z`);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
    return fail(field, 'Use a valid calendar date in YYYY-MM-DD format.');
  }
  return date;
}

function shiftCalendarDay(value: string, difference: number, field: string): string {
  if (value === '') return '';
  const date = validCalendarDate(value, field);
  date.setUTCDate(date.getUTCDate() + difference);
  const result = date.toISOString().slice(0, 10);
  validCalendarDate(result, field);
  return result;
}

/** Convert the UI's inclusive calendar end into the existing exclusive API date. */
export function catalogueExclusiveEnd(inclusive: string): string {
  return shiftCalendarDay(inclusive, 1, 'published_before');
}

/** Convert an existing exclusive API end into the displayed inclusive date. */
export function catalogueCalendarEnd(exclusive: string): string {
  return shiftCalendarDay(exclusive, -1, 'published_before');
}

export function normalizeCatalogueTimestamp(value: unknown, field: string): string {
  if (typeof value !== 'string') return fail(field, 'An explicit observation timestamp is required.');
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return fail(field, 'Use an ISO timestamp with an explicit timezone and at most microsecond precision.');
  validCalendarDate(match[1], field);
  const hour = Number(match[2]);
  const minute = Number(match[3]);
  const second = Number(match[4]);
  const zone = match[6];
  const zoneHour = zone === 'Z' ? 0 : Number(zone.slice(1, 3));
  const zoneMinute = zone === 'Z' ? 0 : Number(zone.slice(4, 6));
  if (hour > 23 || minute > 59 || second > 59 || zoneHour > 23 || zoneMinute > 59) {
    return fail(field, 'Use a valid observation timestamp.');
  }
  const fraction = (match[5] ?? '').padEnd(6, '0');
  const localMilliseconds = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}.${fraction.slice(0, 3)}Z`);
  const zoneMinutes = (zoneHour * 60 + zoneMinute) * (zone.startsWith('-') ? -1 : 1);
  const utc = new Date(localMilliseconds - zoneMinutes * 60_000).toISOString();
  validCalendarDate(utc.slice(0, 10), field);
  // Date has millisecond precision. Preserve the provider's extra three digits.
  return `${utc.slice(0, -1)}${fraction.slice(3)}Z`;
}

function presetKeys(value: unknown, allowed: readonly CataloguePreset[], field: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 100) return fail(field, `${field} must be a bounded list.`);
  const knownKeys = new Set(allowed.map((preset) => preset.key));
  if (value.some((key) => typeof key !== 'string' || !knownKeys.has(key))) {
    return fail(field, `${field} contains an unsupported preset.`);
  }
  const selected = new Set(value as string[]);
  return allowed.map((preset) => preset.key).filter((key) => selected.has(key));
}

export function normalizeCatalogueGrowth(value: unknown): CatalogueGrowthFilters | undefined {
  if (value === undefined) return undefined;
  const data = record(value, 'facets.growth', ['min_views_gained', 'max_views_gained', 'min_tracked_days', 'full_history']);
  const result: CatalogueGrowthFilters = {};
  for (const key of ['min_views_gained', 'max_views_gained', 'min_tracked_days'] as const) {
    if (data[key] === undefined) continue;
    const field = `facets.growth.${key}`;
    if (typeof data[key] !== 'string') fail(field, 'Growth filter bounds must be whole-number strings.');
    const text = (data[key] as string).trim();
    if (!text) continue;
    if (text.length > 32 || !(key === 'min_tracked_days' ? /^[0-9]+$/ : /^-?[0-9]+$/).test(text)) fail(field, 'Use a whole number without decimal points or exponents; tracked days cannot be negative.');
    const number = BigInt(text);
    if (number < -1000000000000000n || number > 1000000000000000n
      || (key === 'min_tracked_days' && number < 0n)) fail(field, 'Growth bounds must be within ±1,000,000,000,000,000; tracked days cannot be negative.');
    result[key] = number.toString();
  }
  if (result.min_views_gained !== undefined && result.max_views_gained !== undefined
    && BigInt(result.min_views_gained) > BigInt(result.max_views_gained)) {
    fail('facets.growth.max_views_gained', 'Maximum views gained must not be below the minimum.');
  }
  if (data.full_history !== undefined && typeof data.full_history !== 'boolean') fail('facets.growth.full_history', 'Full history must be a checkbox value.');
  if (data.full_history === true) result.full_history = true;
  return Object.keys(result).length ? result : undefined;
}

export function normalizeCatalogueFacets(value: unknown): CatalogueFacets {
  const data = record(value, 'facets', [
    'version', 'view_buckets', 'duration_buckets', 'published_years', 'published_older_than', 'cohort',
    'like_buckets', 'comment_buckets', 'title_length_buckets', 'tag_count_buckets',
    'growth',
  ]);
  if (data.version !== 1) fail('facets.version', 'Unsupported catalogue filter version.');
  const rawYears = data.published_years === undefined ? [] : data.published_years;
  if (!Array.isArray(rawYears) || rawYears.length > 100) {
    fail('facets.published_years', 'At most 100 publication years are supported.');
  }
  const publishedYears = Array.from(new Set(rawYears.map((year) => validYear(year, 'facets.published_years'))))
    .sort((left, right) => right - left);
  let cohort: CatalogueCohort | null = null;
  if (data.cohort !== null && data.cohort !== undefined) {
    const rawCohort = record(data.cohort, 'facets.cohort', ['kind', 'published_from', 'published_through']);
    if (rawCohort.kind !== 'overview') fail('facets.cohort.kind', 'Unsupported catalogue cohort.');
    const from = rawCohort.published_from === null
      ? null : normalizeCatalogueTimestamp(rawCohort.published_from, 'facets.cohort.published_from');
    const through = normalizeCatalogueTimestamp(rawCohort.published_through, 'facets.cohort.published_through');
    if (from !== null && from > through) fail('facets.cohort', 'The cohort start cannot follow its end.');
    cohort = { kind: 'overview', published_from: from, published_through: through };
  }
  const additional: Partial<CatalogueFacets> = {};
  const growth = normalizeCatalogueGrowth(data.growth);
  if (growth) additional.growth = growth;
  for (const group of CATALOGUE_NUMERIC_GROUPS.slice(2)) {
    if (!group.facet) continue;
    const selected = presetKeys(data[group.facet], group.presets ?? [], `facets.${group.facet}`);
    if (selected.length) additional[group.facet] = selected;
  }
  return {
    version: 1,
    view_buckets: presetKeys(data.view_buckets, CATALOGUE_VIEW_PRESETS, 'facets.view_buckets'),
    duration_buckets: presetKeys(data.duration_buckets, CATALOGUE_DURATION_PRESETS, 'facets.duration_buckets'),
    published_years: publishedYears,
    published_older_than: data.published_older_than === null || data.published_older_than === undefined
      ? null : validYear(data.published_older_than, 'facets.published_older_than'),
    cohort,
    ...additional,
  };
}

function normalizedFilters(value: unknown): ExplorerFilters {
  const data = record(value, 'filters', [...filterKeys, ...STATIC_CATALOGUE_FILTER_KEYS]);
  for (const key of filterKeys) {
    if (typeof data[key] !== 'string') fail(`filters.${key}`, `${key} must be a string.`);
  }
  for (const key of STATIC_CATALOGUE_FILTER_KEYS) {
    if (data[key] !== undefined && typeof data[key] !== 'string') fail(`filters.${key}`, `${key} must be a string.`);
  }
  const filters = normalizeExplorerFilters(data as ExplorerFilters);
  if (!sortFields.has(filters.sort_by)) fail('filters.sort_by', 'Unsupported catalogue sort field.');
  if (!['asc', 'desc'].includes(filters.sort_order)) fail('filters.sort_order', 'Unsupported catalogue sort direction.');
  const errors = validateExplorerFilters(filters);
  for (const [key, message] of Object.entries(errors)) {
    if (message) fail(`filters.${key}`, message);
  }
  for (const key of ['published_from', 'published_before'] as const) {
    if (filters[key]) validCalendarDate(filters[key], `filters.${key}`);
  }
  if (filters.published_before) catalogueCalendarEnd(filters.published_before);
  for (const key of integerFilterKeys) {
    if (filters[key]) filters[key] = String(Number(filters[key]));
  }
  const extra = staticCatalogueBounds(filters);
  for (const key of STATIC_CATALOGUE_FILTER_KEYS) {
    if (extra[key]) extra[key] = extra[key]!.replace(/^0+(?=\d)/, '').replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
  }
  return { ...Object.fromEntries(filterKeys.map((key) => [key, filters[key]])), ...extra } as ExplorerFilters;
}

export function normalizeCatalogueConditions(value: unknown): CatalogueConditions {
  const data = record(value, 'conditions', ['version', 'filters', 'facets']);
  if (data.version !== 1) fail('conditions.version', 'Unsupported catalogue condition version.');
  const filters = normalizedFilters(data.filters);
  const facets = normalizeCatalogueFacets(data.facets);
  for (const group of CATALOGUE_NUMERIC_GROUPS.slice(2)) {
    if (group.facet && facets[group.facet]?.length && (filters[group.min] || filters[group.max])) {
      fail(`facets.${group.facet}`, `Choose ${group.label} presets or a Custom range, not both.`);
    }
  }
  if (facets.view_buckets.length && (filters.min_views || filters.max_views)) {
    fail('facets.view_buckets', 'Choose Views presets or a Custom range, not both.');
  }
  if (facets.duration_buckets.length && (filters.min_duration_seconds || filters.max_duration_seconds)) {
    fail('facets.duration_buckets', 'Choose Duration presets or a Custom range, not both.');
  }
  if ((facets.published_years.length || facets.published_older_than !== null)
    && (filters.published_from || filters.published_before)) {
    fail('facets.published_years', 'Choose publication years or Custom dates, not both.');
  }
  return { version: 1, filters, facets };
}

export function createInitialCatalogueConditions(): CatalogueConditions {
  return normalizeCatalogueConditions({
    version: 1,
    filters: { ...INITIAL_EXPLORER_FILTERS },
    facets: { version: 1 },
  });
}

export function serializeCatalogueConditions(value: CatalogueConditions): string {
  const serialized = JSON.stringify(normalizeCatalogueConditions(value));
  if (serialized.length > MAX_CATALOGUE_QUERY_LENGTH) fail('conditions', 'Saved conditions exceed the supported size.');
  return serialized;
}

export function parseCatalogueConditions(serialized: string): CatalogueConditions {
  if (typeof serialized !== 'string' || serialized.length > MAX_CATALOGUE_QUERY_LENGTH) {
    fail('conditions', 'Saved conditions exceed the supported size.');
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized);
  } catch {
    return fail('conditions', 'The saved catalogue conditions are not valid JSON.');
  }
  return normalizeCatalogueConditions(parsed);
}

export function serializeCatalogueFacets(value: CatalogueFacets): string {
  const serialized = JSON.stringify(normalizeCatalogueFacets(value));
  if (serialized.length > MAX_CATALOGUE_QUERY_LENGTH) fail('facets', 'Catalogue filters exceed the supported size.');
  return serialized;
}

/** HTTP-only extension: new bounds travel inside the validated opt-in echo, not unknown flat parameters. */
export function normalizeCatalogueRequestFilters(value: unknown): CatalogueFacets & { bounds?: Partial<ExplorerFilters>; time_zone?: string } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail('catalogue_filters', 'Expected catalogue filters.');
  const { bounds: rawBounds, time_zone: rawZone, ...rawFacets } = value as Record<string, unknown>;
  const facets = normalizeCatalogueFacets(rawFacets);
  // The zone publication years and dates are counted in; UTC travels as absent.
  if (rawZone !== undefined && (typeof rawZone !== 'string' || rawZone === 'UTC' || !TIME_ZONE_PATTERN.test(rawZone))) {
    return fail('catalogue_filters', 'Expected an IANA time zone.');
  }
  const zone = rawZone === undefined ? {} : { time_zone: rawZone as string };
  if (rawBounds === undefined) return { ...facets, ...zone };
  const boundsRecord = record(rawBounds, 'bounds', STATIC_CATALOGUE_FILTER_KEYS);
  const conditions = normalizeCatalogueConditions({ version: 1, filters: { ...INITIAL_EXPLORER_FILTERS, ...boundsRecord }, facets });
  const bounds = staticCatalogueBounds(conditions.filters);
  return { ...facets, ...(Object.keys(bounds).length ? { bounds } : {}), ...zone };
}

export function clearCatalogueFilterGroup(value: CatalogueConditions, group: CatalogueFilterGroup): CatalogueConditions {
  const next = structuredClone(value), numeric = CATALOGUE_NUMERIC_GROUPS.find((item) => item.group === group);
  if (numeric) {
    next.filters[numeric.min] = ''; next.filters[numeric.max] = '';
    if (numeric.facet) next.facets[numeric.facet] = [];
  } else if (group === 'q') next.filters.q = '';
  else if (group === 'category') next.filters.category_id = '';
  else if (group === 'cohort') next.facets.cohort = null;
  else if (group === 'published') { next.filters.published_from = ''; next.filters.published_before = ''; next.facets.published_years = []; next.facets.published_older_than = null; }
  else if (next.facets.growth) {
    if (group === 'views_gained') { delete next.facets.growth.min_views_gained; delete next.facets.growth.max_views_gained; }
    else if (group === 'tracked_days') delete next.facets.growth.min_tracked_days;
    else if (group === 'full_history') delete next.facets.growth.full_history;
  }
  return normalizeCatalogueConditions(next);
}

export function applyCataloguePreset(value: CatalogueConditions, field: CataloguePresetGroup, key: string): CatalogueConditions {
  const group = CATALOGUE_NUMERIC_GROUPS.find((item) => item.group === field);
  if (!group?.facet || !group.presets?.some((preset) => preset.key === key)) return fail('facets', 'Unknown preset.');
  const next = clearCatalogueFilterGroup(value, field); next.facets[group.facet] = [key];
  return normalizeCatalogueConditions(next);
}

export function catalogueUsesDerivedStaticFilters(value: CatalogueConditions): boolean {
  return CATALOGUE_NUMERIC_GROUPS.some((group) => group.derived && Boolean(value.filters[group.min] || value.filters[group.max] || (group.facet && value.facets[group.facet]?.length)));
}

export function catalogueUsesGrowthFilters(value: CatalogueConditions): boolean {
  return Boolean(normalizeCatalogueGrowth(value.facets.growth));
}

/** One decision shared by initial loading, recovery and full-selection requests. */
export function catalogueRequestNeedsGrowth(query: ChannelExplorerQuery): boolean {
  if (query.sort_by === 'views_gained') return true;
  if (!query.catalogue_filters) return false;
  return Boolean(normalizeCatalogueRequestFilters(JSON.parse(query.catalogue_filters)).growth);
}

export function catalogueRequestQuery(
  value: CatalogueConditions,
  offset: number,
  timeZone = 'UTC',
): ChannelExplorerQuery & { catalogue_filters: string } {
  if (!Number.isSafeInteger(offset) || offset < 0) fail('offset', 'Catalogue offset must be a non-negative integer.');
  const conditions = normalizeCatalogueConditions(value);
  const bounds = staticCatalogueBounds(conditions.filters);
  return {
    ...rawQueryFromFilters(conditions.filters, offset),
    catalogue_filters: JSON.stringify({ ...conditions.facets, ...(Object.keys(bounds).length ? { bounds } : {}),
      ...(timeZone === 'UTC' ? {} : { time_zone: timeZone }) }),
  };
}

/** Membership identity intentionally excludes order, page and UI state. */
export function catalogueAnalysisKey(channelId: string, value: CatalogueConditions): string {
  const conditions = normalizeCatalogueConditions(value);
  return JSON.stringify([
    'catalogue-conditions-v1',
    analysisScopeKey(channelId, conditions.filters),
    conditions.facets,
  ]);
}

function singleParameter(parameters: URLSearchParams, key: string): string | null {
  if (parameters.getAll(key).length > 1) fail(key, `The URL contains duplicate ${key} parameters.`);
  return parameters.get(key);
}

function urlNavigation(section: unknown, period: unknown): Pick<CatalogueUrlState, 'section' | 'overviewPeriod'> {
  if (section !== 'overview' && section !== 'videos') fail('section', 'Unsupported channel section.');
  if (typeof period !== 'string' || !periods.has(period)) fail('overviewPeriod', 'Unsupported Overview publication period.');
  return { section, overviewPeriod: period as ChannelBaselinePeriodKey };
}

export function readCatalogueUrl(input: URL | URLSearchParams | string): CatalogueUrlState {
  const parameters = input instanceof URL
    ? input.searchParams : input instanceof URLSearchParams ? input
      : /^[a-z][a-z\d+.-]*:\/\//i.test(input) ? new URL(input).searchParams : new URLSearchParams(input);
  const section = singleParameter(parameters, 'section') ?? 'overview';
  const overviewPeriod = singleParameter(parameters, 'overviewPeriod') ?? 'max';
  const serialized = singleParameter(parameters, 'videos');
  return {
    ...urlNavigation(section, overviewPeriod),
    conditions: serialized === null ? createInitialCatalogueConditions() : parseCatalogueConditions(serialized),
  };
}

/** Pure: no window/history access, and never changes a channel path or unrelated query keys. */
export function writeCatalogueUrl(input: URL, state: CatalogueUrlState): URL {
  const navigation = urlNavigation(state.section, state.overviewPeriod);
  const output = new URL(input.href);
  output.searchParams.set('section', navigation.section);
  output.searchParams.set('overviewPeriod', navigation.overviewPeriod);
  output.searchParams.set('videos', serializeCatalogueConditions(state.conditions));
  return output;
}
