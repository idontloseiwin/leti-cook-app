// Saved Library views in the shared format: v2 + `presentation`
// (`surface: "library"`). Two owners with separate places in this file:
// The Videos tab writes ("Save view" from Videos), the Saved page opens (Saved list → channel address).
// Old v1 views keep opening through `savedViewUrl` and the server translation.
// Pure TypeScript: no React, window or network.
import type { LibraryVideoType, TranslatedQuery } from './wire.ts';
// Used by the opening part:
import { DEFAULT_CATALOGUE_FACETS, normalizeCatalogueConditions, type CatalogueConditions, type CatalogueGrowthFilters } from '../catalogueQuery.ts';
import { INITIAL_EXPLORER_FILTERS } from '../filters.ts';
import { TIME_ZONE_PATTERN } from '../timeZones.ts';
import { translationBlockedMessage, type LibraryTranslation, type MissingCapability } from './conditions.ts';
import { presetsFor, type LibraryPreset, type LibraryPresetGroup } from './presets.ts';
import { LIBRARY_VIDEO_TYPES, VIDEO_SOURCE, libraryQuery, type LibraryPage, type LibraryQueryRequest } from './wire.ts';

export const LIBRARY_SURFACE = 'library';
export const PRESENTATION_VERSION = 1;

/** The closed `presentation` block of (`research_query/saved.py`, `savedQueryDefinition.ts`). */
export type LibraryPresentation = {
  version: 1;
  surface: 'library';
  view: 'grid' | 'table';
  density?: 'large' | 'compact';
  columns?: string[];
  time_zone?: string;
};

/** A saved Library view: the last translated query (scope id `$channel`) and how to show it. */
export type LibrarySavedDefinition = {
  version: 2;
  query: Omit<TranslatedQuery, 'scope'> & { scope: { kind: 'channel'; id: '$channel' } };
  presentation: LibraryPresentation;
};

// ---------------------------------------------------------------------------
// Writing (Save view). Build `LibrarySavedDefinition` from the last
// translated query; reuse the request UUID after an uncertain reply
// (savedViews.ts prepareSavedViewCreate).
// ---------------------------------------------------------------------------

/** Execution-only parts of a query: never saved (a saved definition keeps no read mode). */
const EXECUTION_KEYS = ['consistency', 'page'] as const;

/**
 * "Save view" from Library: the server's translation
 * exactly as it was returned (node order matters for recognition), bound to
 * `$channel`, without paging or a read mode; `presentation` says it opens in
 * Library's grid and names the viewer's zone its dates were entered in.
 */
export function librarySavedDefinition(translated: TranslatedQuery, options: {
  channelId: string; timeZone: string; density?: 'large' | 'compact';
}): LibrarySavedDefinition {
  const query = structuredClone(translated) as unknown as Record<string, unknown>;
  const scope = query.scope as { kind?: unknown; id?: unknown } | undefined;
  if ((query.source as { id?: unknown } | undefined)?.id !== VIDEO_SOURCE || scope?.kind !== 'channel' || scope.id !== options.channelId
    || !Array.isArray(query.video_types) || query.video_types.length !== 1 || !LIBRARY_VIDEO_TYPES.includes(query.video_types[0])) {
    throw new Error('Only the channel query of this Library tab can be saved as a Library view.');
  }
  for (const key of EXECUTION_KEYS) delete query[key];
  const size = (translated as { page?: { size?: unknown } }).page?.size;
  if (Number.isSafeInteger(size)) query.page = { size };
  query.scope = { kind: 'channel', id: '$channel' };
  return {
    version: 2,
    query: query as unknown as LibrarySavedDefinition['query'],
    presentation: {
      version: PRESENTATION_VERSION, surface: LIBRARY_SURFACE, view: 'grid',
      ...(options.density ? { density: options.density } : {}), time_zone: options.timeZone,
    },
  };
}

// ---------------------------------------------------------------------------
// Opening (Saved list). Recognise the translator's canonical form and
// open `videos=` + `type=` in the channel; otherwise open the workshop.
// ---------------------------------------------------------------------------

/**
 * Where a saved Library view opens. `channel`: the Library filters the channel
 * address carries (`videos=` + `type=`) and the zone its dates were saved in;
 * `workshop`: the research workshop opens the saved query as a study.
 */
export type LibraryOpenTarget =
  | { kind: 'channel'; channelId: string; videoType: LibraryVideoType; url: string; conditions: CatalogueConditions; savedZone: string | null }
  | { kind: 'workshop'; url: string; reason: string };

/** A saved query as `parseSavedDefinition` returns it (only the parts recognition reads). */
export type LibraryRecognitionInput = {
  version: number;
  query: {
    source?: { id?: unknown };
    scope?: { kind?: unknown; id?: unknown };
    video_types?: unknown;
    where?: unknown;
    order_by?: unknown;
    time?: { mode?: unknown; timezone?: unknown };
    coverage?: { mode?: unknown; required_fields?: unknown };
  };
  presentation?: { surface?: unknown; time_zone?: unknown };
};

export type LibraryRecognition =
  | { status: 'recognized'; conditions: CatalogueConditions; videoType: LibraryVideoType; savedZone: string }
  | { status: 'unrecognized'; reason: string };

const NOT_LIBRARY = 'This view was not saved in Library.';
const UNRECOGNIZED = 'Library filters cannot show these conditions.';

type Item = Record<string, unknown>;
type Bound = string | null;
type Draft = { filters: Record<string, string>; facets: Record<string, unknown> };
type Candidate = { slot: number; apply: (draft: Draft) => void };

const isItem = (value: unknown): value is Item => Boolean(value) && typeof value === 'object' && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value));

/** One predicate `{field, op[, value]}` of `field`, or null. */
function leaf(node: unknown, field?: string): { field: string; op: string; value?: unknown } | null {
  if (!isItem(node) || typeof node.field !== 'string' || typeof node.op !== 'string'
    || Object.keys(node).some((key) => key !== 'field' && key !== 'op' && key !== 'value')) return null;
  if (field !== undefined && node.field !== field) return null;
  return node as { field: string; op: string; value?: unknown };
}

/** The children of `{all: [...]}` / `{any: [...]}` with at least two members (the translator never wraps one). */
function members(node: unknown, key: 'all' | 'any'): unknown[] | null {
  if (!isItem(node) || Object.keys(node).length !== 1 || !Array.isArray(node[key]) || (node[key] as unknown[]).length < 2) return null;
  return node[key] as unknown[];
}

const textValue = (value: unknown): value is string => typeof value === 'string' && value.length > 0;

/**
 * One translator range of `field` (`_range`): `lower <= value < upper`;
 * null is an open end. `is_known` is the range open at both ends.
 */
function range(node: unknown, field: string): [Bound, Bound] | null {
  const single = leaf(node, field);
  if (single) {
    if (single.op === 'is_known' && !Object.hasOwn(single, 'value')) return [null, null];
    if (!textValue(single.value)) return null;
    if (single.op === 'gte') return [single.value, null];
    if (single.op === 'lt') return [null, single.value];
    return null;
  }
  const pair = members(node, 'all');
  if (!pair || pair.length !== 2) return null;
  const lower = leaf(pair[0], field), upper = leaf(pair[1], field);
  if (!lower || !upper || lower.op !== 'gte' || upper.op !== 'lt' || !textValue(lower.value) || !textValue(upper.value)) return null;
  return [lower.value, upper.value];
}

/** Ascending alternatives of one field (`_alternatives`): one range, or `any` of two or more. */
function ranges(node: unknown, field: string): [Bound, Bound][] | null {
  const alternatives = members(node, 'any');
  if (!alternatives) { const one = range(node, field); return one ? [one] : null; }
  const result: [Bound, Bound][] = [];
  for (const item of alternatives) { const one = range(item, field); if (!one) return null; result.push(one); }
  return result;
}

const presetText = (value: number | null): Bound => value === null ? null : String(value);

/**
 * Preset keys whose union is exactly these ranges. The translator merges
 * touching presets into one range, so each range is a run of neighbouring
 * presets and two ranges never touch.
 */
function presetKeys(found: [Bound, Bound][], presets: readonly LibraryPreset[]): string[] | null {
  const keys: string[] = [];
  let last = -1;
  let previousUpper: Bound | undefined;
  for (const [lower, upper] of found) {
    if (previousUpper !== undefined && (previousUpper === null || previousUpper === lower)) return null;
    let index = presets.findIndex((preset, at) => at > last && presetText(preset.min) === lower);
    if (index < 0) return null;
    keys.push(presets[index].key);
    while (presetText(presets[index].max) !== upper) {
      index += 1;
      if (index >= presets.length || presetText(presets[index].min) !== presetText(presets[index - 1].max)) return null;
      keys.push(presets[index].key);
    }
    last = index; previousUpper = upper;
  }
  return keys;
}

// ---- Calendar arithmetic of the translator (Python zoneinfo, fold 0). ----

const SECOND = 1000, DAY = 86_400_000;
function utcMs(year: number, month: number, day: number): number {
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  return date.getTime();
}
const FIRST_INSTANT = utcMs(1, 1, 1);
const LAST_INSTANT = utcMs(10000, 1, 1) - 1;
const formatters = new Map<string, Intl.DateTimeFormat>();

/** Wall-clock offset of `zone` at one instant, to the second (historic local mean time included). */
function offsetAt(instant: number, zone: string): number {
  let formatter = formatters.get(zone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-US', { timeZone: zone, hourCycle: 'h23', era: 'short', year: 'numeric',
      month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric' });
    formatters.set(zone, formatter);
  }
  const probe = Math.min(Math.max(instant, FIRST_INSTANT + DAY), LAST_INSTANT - DAY);
  const part: Record<string, string> = {};
  for (const { type, value } of formatter.formatToParts(new Date(probe))) part[type] = value;
  const year = part.era === 'BC' || part.era === 'B' ? 1 - Number(part.year) : Number(part.year);
  const wall = utcMs(year, Number(part.month), Number(part.day)) + ((Number(part.hour) % 24) * 3600 + Number(part.minute) * 60 + Number(part.second)) * SECOND;
  return wall - Math.floor(probe / SECOND) * SECOND;
}

/** Open ends of `midnight`: local midnights outside 0001-01-01Z through 9999-12-31Z. */
const BEFORE_ALL = -Infinity, AFTER_ALL = Infinity;

/**
 * Local midnight of a calendar day as `midnight` builds it: Python
 * `datetime(y, m, d, tzinfo=zone)` (fold 0) in UTC. An ambiguous midnight is
 * its first occurrence; a midnight inside a gap uses the offset before the gap.
 */
export function libraryMidnight(year: number, month: number, day: number, zone: string): number {
  if (year > 9999) return AFTER_ALL;
  const wall = utcMs(year, month, day);
  const offsets = [...new Set([offsetAt(wall - DAY, zone), offsetAt(wall, zone), offsetAt(wall + DAY, zone)])];
  const solutions = offsets.filter((offset) => offsetAt(wall - offset, zone) === offset).map((offset) => wall - offset);
  const instant = solutions.length ? Math.min(...solutions) : wall - Math.min(...offsets);
  return instant < FIRST_INSTANT ? BEFORE_ALL : instant > LAST_INSTANT ? AFTER_ALL : instant;
}

const isoSeconds = (instant: number): string => `${new Date(instant).toISOString().slice(0, 19)}Z`;
const INSTANT_TEXT = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{6}))?Z$/;

/** The calendar day whose local midnight is exactly this instant text, or null. */
function midnightDay(value: Bound, zone: string): { year: number; month: number; day: number; text: string } | null {
  if (value === null || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) return null;
  const instant = Date.parse(value);
  if (!Number.isFinite(instant) || isoSeconds(instant) !== value) return null;
  const local = new Date(instant + offsetAt(instant, zone));
  const year = local.getUTCFullYear(), month = local.getUTCMonth() + 1, day = local.getUTCDate();
  if (year < 1 || year > 9999 || libraryMidnight(year, month, day, zone) !== instant) return null;
  return { year, month, day, text: `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}` };
}

/**
 * Years whose ranges are exactly these (`_year_intervals` + `_alternatives`):
 * "older than" first (open below), then runs of consecutive selected years in
 * ascending order. The translator merges a range only into the one before it
 * when they touch, so a year below "older than" stays a range of its own.
 */
function yearsOf(found: [Bound, Bound][], zone: string): { years: number[]; older: number | null } | null {
  const years: number[] = [];
  let older: number | null = null, previousEnd: number | null = null, previousRunEnd: number | null = null;
  for (const [index, [lower, upper]] of found.entries()) {
    let end = 10000;
    if (upper !== null) { const day = midnightDay(upper, zone); if (!day || day.month !== 1 || day.day !== 1) return null; end = day.year; }
    if (lower === null) {
      // Only the first range is open below: "older than".
      if (index !== 0 || end > 9999) return null;
      older = end;
    } else {
      const day = midnightDay(lower, zone);
      if (!day || day.month !== 1 || day.day !== 1 || day.year >= end || day.year === previousEnd
        || (previousRunEnd !== null && day.year <= previousRunEnd)) return null;
      for (let year = day.year; year < end; year += 1) years.push(year);
      if (years.length > 100) return null;
      previousRunEnd = end;
    }
    previousEnd = end;
  }
  return { years: years.sort((left, right) => right - left), older };
}

// ---- The Library filter slots, in the translator's table order. ----

// library_v1.translate order: search, category, dates, years, bounds (BOUNDS then the
// calculated DERIVED_BOUNDS), presets (PRESET_GROUPS then DERIVED_PRESETS), cohort, growth.
const BOUND_SLOTS: readonly [string, 'gte' | 'lte', string][] = [
  ['video.views', 'gte', 'min_views'], ['video.views', 'lte', 'max_views'],
  ['video.duration_seconds', 'gte', 'min_duration_seconds'], ['video.duration_seconds', 'lte', 'max_duration_seconds'],
  ['video.likes', 'gte', 'min_likes'], ['video.likes', 'lte', 'max_likes'],
  ['video.comments', 'gte', 'min_comments'], ['video.comments', 'lte', 'max_comments'],
  // The Library's calculated values (computed registry fields).
  ['video.title_length', 'gte', 'min_title_length'], ['video.title_length', 'lte', 'max_title_length'],
  ['video.tag_count', 'gte', 'min_tag_count'], ['video.tag_count', 'lte', 'max_tag_count'],
  ['video.like_rate', 'gte', 'min_like_rate'], ['video.like_rate', 'lte', 'max_like_rate'],
  ['video.comment_rate', 'gte', 'min_comment_rate'], ['video.comment_rate', 'lte', 'max_comment_rate'],
];
const PRESET_SLOTS: readonly [string, LibraryPresetGroup, string][] = [
  ['video.views', 'views', 'view_buckets'], ['video.duration_seconds', 'duration', 'duration_buckets'],
  ['video.likes', 'likes', 'like_buckets'], ['video.comments', 'comments', 'comment_buckets'],
  ['video.title_length', 'title_length', 'title_length_buckets'], ['video.tag_count', 'tag_count', 'tag_count_buckets'],
];
const SLOT = {
  q: 0, category: 1, publishedFrom: 2, publishedBefore: 3, years: 4, bounds: 5,
  presets: 5 + BOUND_SLOTS.length, cohort: 5 + BOUND_SLOTS.length + PRESET_SLOTS.length,
  growth: 6 + BOUND_SLOTS.length + PRESET_SLOTS.length,
} as const;
// 28-day growth (growth fields; growth.legacy_conditions and LEGACY_SORT_FIELDS on the server).
const GAINED = 'observation.views_gained_28d', INTERVAL = 'observation.growth_28d_interval_seconds', STATUS = 'observation.growth_28d_status';
const SORTS: Record<string, string> = {
  'video.published_at': 'published_at', 'video.views': 'views', 'video.likes': 'like_count',
  'video.comments': 'comment_count', 'video.duration_seconds': 'duration', 'video.title': 'title',
  [GAINED]: 'views_gained',
};
const PUBLISHED_AT = 'video.published_at';

const setFilter = (key: string, value: string) => (draft: Draft) => { draft.filters[key] = value; };
const setYears = (years: number[], older: number | null) => (draft: Draft) => {
  draft.facets.published_years = years; draft.facets.published_older_than = older;
};

/**
 * Recognise calendar dates in the query's time zone. Stored UTC instant ranges
 * that represent local midnight reopen as the same publication filters.
 */
export const PUBLICATION_DATE_FIELDS = true;

/**
 * The one place that recognises publication dates and years. Returns
 * the ways one condition node can be Library's "Published" filter, preferred
 * first: a Jan 1 bound opens as "Older than"/years, not as custom dates.
 */
export function publicationDateCandidates(node: unknown, zone: string, fields = PUBLICATION_DATE_FIELDS): Candidate[] {
  return [
    ...(fields ? publicationFieldCandidates(node) : []),
    ...publicationInstantCandidates(node, zone),
  ];
}

/** `publication_conditions` (v1 filters): UTC instants of local midnights in the saved zone. */
function publicationInstantCandidates(node: unknown, zone: string): Candidate[] {
  const result: Candidate[] = [];
  const single = leaf(node, PUBLISHED_AT);
  const found = ranges(node, PUBLISHED_AT), years = found && yearsOf(found, zone);
  const asYears = years ? { slot: SLOT.years, apply: setYears(years.years, years.older) } : null;
  if (single && single.op === 'gte' && typeof single.value === 'string') {
    const day = midnightDay(single.value, zone);
    if (day) result.push({ slot: SLOT.publishedFrom, apply: setFilter('published_from', day.text) });
  }
  if (asYears) result.push(asYears);
  if (single && single.op === 'lt' && typeof single.value === 'string') {
    const day = midnightDay(single.value, zone);
    if (day) result.push({ slot: SLOT.publishedBefore, apply: setFilter('published_before', day.text) });
  }
  return result;
}

/** `publication_date_conditions` (calendar fields): calendar fields in the query's zone. */
function publicationFieldCandidates(node: unknown): Candidate[] {
  const date = leaf(node, 'video.published_date');
  const DATE = /^\d{4}-\d{2}-\d{2}$/;
  if (date && typeof date.value === 'string' && DATE.test(date.value)) {
    if (date.op === 'gte') return [{ slot: SLOT.publishedFrom, apply: setFilter('published_from', date.value) }];
    if (date.op === 'lt') return [{ slot: SLOT.publishedBefore, apply: setFilter('published_before', date.value) }];
    return [];
  }
  const parts = members(node, 'any') ?? [node];
  if (parts.length > 2) return [];
  let years: number[] | null = null, older: number | null = null;
  const YEAR = (value: unknown) => typeof value === 'string' && /^[1-9]\d{0,3}$/.test(value);
  for (const [index, part] of parts.entries()) {
    const year = leaf(part, 'video.published_year');
    if (year?.op === 'in' && index === 0 && Array.isArray(year.value) && year.value.length && year.value.every(YEAR)) {
      const values = (year.value as string[]).map(Number);
      if (values.some((value, at) => at > 0 && value <= values[at - 1])) return [];
      years = values;
    } else if (year?.op === 'lt' && index === parts.length - 1 && YEAR(year.value)) older = Number(year.value);
    else return [];
  }
  if (parts.length === 2 && (years === null || older === null)) return [];
  return [{ slot: SLOT.years, apply: setYears((years ?? []).sort((left, right) => right - left), older) }];
}

/** The ways one condition node can be one Library filter, preferred first (presets before custom bounds). */
function candidates(node: unknown, zone: string, type: LibraryVideoType, useCalendarFields: boolean): Candidate[] {
  const result: Candidate[] = [];
  const title = leaf(node, 'video.title');
  if (title?.op === 'contains_casefold_v1' && textValue(title.value)) result.push({ slot: SLOT.q, apply: setFilter('q', title.value) });
  const category = leaf(node, 'video.category_id');
  if (category?.op === 'eq' && textValue(category.value)) result.push({ slot: SLOT.category, apply: setFilter('category_id', category.value) });
  result.push(...publicationDateCandidates(node, zone, useCalendarFields));
  PRESET_SLOTS.forEach(([field, group, facet], index) => {
    const presets = presetsFor(type, group), found = presets && ranges(node, field), keys = found && presetKeys(found, presets!);
    if (keys?.length) result.push({ slot: SLOT.presets + index, apply: (draft) => { draft.facets[facet] = keys; } });
  });
  const bound = leaf(node);
  BOUND_SLOTS.forEach(([field, op, key], index) => {
    if (bound && bound.field === field && bound.op === op && textValue(bound.value)) result.push({ slot: SLOT.bounds + index, apply: setFilter(key, bound.value) });
  });
  const cohort = cohortOf(node);
  if (cohort) result.push({ slot: SLOT.cohort, apply: (draft) => { draft.facets.cohort = cohort; } });
  const growth = growthOf(node);
  if (growth) result.push({ slot: SLOT.growth, apply: (draft) => { draft.facets.growth = growth; } });
  return result;
}

/**
 * The 28-day growth conditions (`growth.legacy_conditions`): one `all` of
 * `views_gained is_known`, then in order min/max views gained, the measured
 * interval of whole tracked days and `status eq ready` (full history).
 */
function growthOf(node: unknown): CatalogueGrowthFilters | null {
  const parts = members(node, 'all');
  const known = parts && leaf(parts[0], GAINED);
  if (!parts || known?.op !== 'is_known' || Object.hasOwn(known, 'value')) return null;
  const result: CatalogueGrowthFilters = {};
  let at = 1;
  const next = (field: string, op: string, pattern: RegExp): string | null => {
    const item = leaf(parts[at], field);
    if (item?.op !== op || typeof item.value !== 'string' || !pattern.test(item.value)) return null;
    at += 1;
    return item.value;
  };
  const SIGNED = /^-?(?:0|[1-9]\d{0,31})$/, WHOLE = /^(?:0|[1-9]\d{0,31})$/;
  const lower = next(GAINED, 'gte', SIGNED);
  if (lower !== null) result.min_views_gained = lower;
  const upper = next(GAINED, 'lte', SIGNED);
  if (upper !== null) result.max_views_gained = upper;
  const seconds = next(INTERVAL, 'gte', WHOLE);
  if (seconds !== null) {
    // Whole days only; the server's 2^64 - 1 cap of an enormous bound is not a day count.
    if (BigInt(seconds) % 86_400n !== 0n) return null;
    result.min_tracked_days = String(BigInt(seconds) / 86_400n);
  }
  const ready = leaf(parts[at], STATUS);
  if (ready?.op === 'eq' && ready.value === 'ready') { result.full_history = true; at += 1; }
  return at === parts.length && at > 1 ? result : null;
}

/** The Overview cohort: `published_from <= published <= published_through`; without a start an unknown date matches. */
function cohortOf(node: unknown): { kind: 'overview'; published_from: string | null; published_through: string } | null {
  const instant = (value: unknown): string | null => {
    const match = typeof value === 'string' ? INSTANT_TEXT.exec(value) : null;
    return match ? `${match[1]}.${match[2] ?? '000000'}Z` : null;
  };
  const bounded = members(node, 'all'), open = members(node, 'any');
  const pair = bounded ?? open;
  if (!pair || pair.length !== 2) return null;
  const end = leaf(pair[1], PUBLISHED_AT), through = end?.op === 'lte' ? instant(end.value) : null;
  if (!through) return null;
  if (open) {
    const unknown = leaf(pair[0], PUBLISHED_AT);
    return unknown?.op === 'is_unknown' && !Object.hasOwn(unknown, 'value') ? { kind: 'overview', published_from: null, published_through: through } : null;
  }
  const start = leaf(pair[0], PUBLISHED_AT), from = start?.op === 'gte' ? instant(start.value) : null;
  return from ? { kind: 'overview', published_from: from, published_through: through } : null;
}

const sortedJson = (value: unknown): string => JSON.stringify(value, (_key, item) => isItem(item)
  ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);

/** The browser state of a complete parse, only when the codec keeps it exactly (canonical). */
function finish(draft: Draft, sort: { sort_by: string; sort_order: string }): CatalogueConditions | null {
  const built = {
    version: 1,
    filters: { ...INITIAL_EXPLORER_FILTERS, ...draft.filters, ...sort },
    facets: { ...structuredClone(DEFAULT_CATALOGUE_FACETS), ...draft.facets },
  };
  try {
    const normalized = normalizeCatalogueConditions(built);
    return sortedJson(normalized) === sortedJson(built) ? normalized : null;
  } catch { return null; }
}

type ParseContext = { zone: string; type: LibraryVideoType; sort: { sort_by: string; sort_order: string }; useCalendarFields: boolean };
function parse(items: unknown[], at: number, lastSlot: number, draft: Draft, context: ParseContext): CatalogueConditions | null {
  if (at === items.length) return finish(draft, context.sort);
  for (const candidate of candidates(items[at], context.zone, context.type, context.useCalendarFields)) {
    if (candidate.slot <= lastSlot) continue;
    const next: Draft = { filters: { ...draft.filters }, facets: { ...draft.facets } };
    candidate.apply(next);
    const result = parse(items, at + 1, candidate.slot, next, context);
    if (result) return result;
  }
  return null;
}

/** The zone a saved Library view's dates were entered in: `presentation.time_zone`, else the query's zone. */
export function savedLibraryZone(definition: LibraryRecognitionInput): string {
  const zone = definition.presentation?.time_zone ?? definition.query.time?.timezone ?? 'UTC';
  return typeof zone === 'string' ? zone : '';
}

/**
 * A Saved v1 view stores no format. Its duration presets name one when they all
 * belong to Shorts or to Streams (preset keys never repeat across formats); any other
 * v1 view opens in Long, as every v1 view did before.
 */
export function savedLibraryVideoType(conditions: CatalogueConditions): LibraryVideoType {
  const keys = conditions.facets.duration_buckets;
  if (!keys.length) return 'long';
  for (const type of ['short', 'stream'] as const) {
    const own = new Set((presetsFor(type, 'duration') ?? []).map((preset) => preset.key));
    if (keys.every((key) => own.has(key))) return type;
  }
  return 'long';
}

/**
 * Reopen a saved Library v2 view (`presentation.surface: "library"`) as
 * Library filters. A closed grammar of the Library translator's output: every
 * node must be one Library filter in the translator's order, and the result
 * must be canonical browser state. It recognises nodes and never computes a
 * new query; anything else opens in the workshop.
 */
export function recognizeLibraryView(definition: LibraryRecognitionInput, useCalendarFields = PUBLICATION_DATE_FIELDS): LibraryRecognition {
  if (definition.version !== 2 || definition.presentation?.surface !== LIBRARY_SURFACE) return { status: 'unrecognized', reason: NOT_LIBRARY };
  const query = definition.query, zone = savedLibraryZone(definition);
  const unrecognized = (): LibraryRecognition => ({ status: 'unrecognized', reason: UNRECOGNIZED });
  const types = query.video_types;
  if (query.source?.id !== VIDEO_SOURCE || query.scope?.kind !== 'channel' || query.scope.id !== '$channel'
    || !Array.isArray(types) || types.length !== 1 || !LIBRARY_VIDEO_TYPES.includes(types[0])) return unrecognized();
  // Library reads what is available; a required-complete query is a workshop study.
  const required = query.coverage?.required_fields;
  if ((query.coverage?.mode ?? 'available') !== 'available' || (Array.isArray(required) && required.length)) return unrecognized();
  if (!TIME_ZONE_PATTERN.test(zone) || zone.length > 64) return unrecognized();
  try { new Intl.DateTimeFormat('en-US', { timeZone: zone }); } catch { return unrecognized(); }
  const order = query.order_by;
  if (!Array.isArray(order) || order.length !== 1 || !isItem(order[0])) return unrecognized();
  const { field, direction, nulls } = order[0] as Record<string, unknown>;
  if (typeof field !== 'string' || !Object.hasOwn(SORTS, field) || (direction !== 'asc' && direction !== 'desc')
    || (nulls !== undefined && nulls !== 'last') || Object.keys(order[0]).some((key) => !['field', 'direction', 'nulls'].includes(key))) return unrecognized();
  const type = types[0] as LibraryVideoType, sort = { sort_by: SORTS[field], sort_order: direction };
  const where = query.where, context: ParseContext = { zone, type, sort, useCalendarFields };
  const draft: Draft = { filters: {}, facets: {} };
  // The whole tree as one filter (one year, one preset range, one cohort) before its parts as a conjunction.
  const conditions = where === undefined || where === null ? finish(draft, sort)
    : parse([where], 0, -1, draft, context) ?? (members(where, 'all') ? parse(members(where, 'all')!, 0, -1, draft, context) : null);
  return conditions ? { status: 'recognized', conditions, videoType: type, savedZone: zone } : unrecognized();
}

/** True when these conditions count calendar dates or years (they depend on the zone). */
export function usesCalendarConditions(conditions: CatalogueConditions): boolean {
  return Boolean(conditions.filters.published_from || conditions.filters.published_before
    || conditions.facets.published_years.length || conditions.facets.published_older_than !== null);
}

/** A saved view opens in the viewer's zone; one note when its dates were saved in another zone. */
export function libraryZoneNote(conditions: CatalogueConditions, savedZone: string | null | undefined, viewerZone: string): string {
  return savedZone && savedZone !== viewerZone && usesCalendarConditions(conditions)
    ? `Saved with dates in ${savedZone}; shown in ${viewerZone}.` : '';
}

// ---- Opening through the server translation. ----

/** Plain names of the v1 conditions a `needs_capability` reply lists. */
const CONDITION_LABELS: Record<string, string> = {
  'filters.min_title_length': 'Title length (minimum)', 'filters.max_title_length': 'Title length (maximum)',
  'filters.min_tag_count': 'Tags (minimum)', 'filters.max_tag_count': 'Tags (maximum)',
  'filters.min_like_rate': 'Like rate (minimum)', 'filters.max_like_rate': 'Like rate (maximum)',
  'filters.min_comment_rate': 'Comment rate (minimum)', 'filters.max_comment_rate': 'Comment rate (maximum)',
  'facets.title_length_buckets': 'Title length presets', 'facets.tag_count_buckets': 'Tags presets',
  'facets.growth.min_views_gained': 'Minimum views gained', 'facets.growth.max_views_gained': 'Maximum views gained',
  'facets.growth.min_tracked_days': 'Minimum tracked days', 'facets.growth.full_history': 'Full history (28-day comparison available)',
  'filters.sort_by': 'Sort: Views gained', conditions: 'The number of conditions',
};
export function conditionLabel(path: string): string {
  return CONDITION_LABELS[path] ?? path;
}

export type MissingConditions = MissingCapability & { labels: string[] };
export type TranslateFunction = (channelId: string, conditions: CatalogueConditions, timeZone: string,
  videoType: LibraryVideoType, signal?: AbortSignal) => Promise<LibraryTranslation>;
export type LibraryOpenPlan =
  | { status: 'run'; translation: Extract<LibraryTranslation, { status: 'translated' }>; request: LibraryQueryRequest; note: string }
  | { status: 'blocked'; missing: MissingConditions[]; message: string; blockedText: string };

/** What a Saved row says when its conditions cannot run here yet; nothing runs and the view is kept. */
export function savedOpenBlockedMessage(missing: readonly MissingConditions[]): string {
  const labels = [...new Set(missing.flatMap((item) => item.labels))];
  return `This saved view cannot open yet: ${labels.join(', ')} need data this server does not provide. The saved view is kept.`;
}

/**
 * Open Library conditions (a Saved v1 view, a recognised Library v2 view or an
 * old `?videos=` address) through `POST /saved/views/translate` with the tab's
 * video type and the viewer's zone. `translated`: the first frozen page request
 * of exactly the translated query (`libraryQuery`). `needs_capability`: the
 * missing conditions in plain words; no partial query is built or run.
 */
export async function planLibraryOpen(translate: TranslateFunction, input: {
  channelId: string; conditions: CatalogueConditions; videoType: LibraryVideoType; viewerZone: string;
  savedZone?: string | null; page?: LibraryPage; growthFields?: readonly string[]; fields?: readonly string[];
}, signal?: AbortSignal): Promise<LibraryOpenPlan> {
  const translation = await translate(input.channelId, input.conditions, input.viewerZone, input.videoType, signal);
  if (translation.status === 'needs_capability') {
    const missing = translation.missing.map((item) => ({ ...item, labels: item.conditions.map(conditionLabel) }));
    return { status: 'blocked', missing, message: savedOpenBlockedMessage(missing), blockedText: translationBlockedMessage(translation.missing) };
  }
  return {
    status: 'run', translation,
    request: libraryQuery(translation.query, { timeZone: input.viewerZone, page: input.page, growthFields: input.growthFields, fields: input.fields }),
    note: libraryZoneNote(input.conditions, input.savedZone, input.viewerZone),
  };
}
