// The server translates Library conditions at POST /saved/views/translate.
// The browser keeps editing its v1
// filter model and never carries a second translator. This module builds the
// request, parses the reply strictly, caches it in memory and turns a
// `needs_capability` reply into the existing blocking messages.
// The reverse direction (recognising a saved v2 Library query) is
// `recognizeLibraryView` in savedLibrary.ts. Pure TypeScript: the request
// function is passed in (requestJson in the app, a stub in tests).
import { serializeCatalogueConditions, type CatalogueConditions } from '../catalogueQuery.ts';
import { TIME_ZONE_PATTERN } from '../timeZones.ts';
import { ROUTES, VIDEO_SOURCE, usesLocalDateFields, type LibraryVideoType, type TranslatedQuery } from './wire.ts';
import { array, closedObject, contractError, enumValue, openObject, text } from './strict.ts';

export type MissingCapability = { capability: string; conditions: string[] };
export type LibraryTranslation =
  | { status: 'translated'; query: TranslatedQuery; timeZone: string; missing: [] }
  | { status: 'needs_capability'; query: null; timeZone: string; missing: MissingCapability[] };

export type TranslationRequest = {
  channel_id: string;
  conditions: CatalogueConditions;
  time_zone: string;
  video_type: LibraryVideoType;
};

/** Body of one translation. The route requires the Library tab's `video_type` (a Saved v1 view is Long). */
export function translationRequest(
  channelId: string,
  conditions: CatalogueConditions,
  timeZone: string,
  videoType: LibraryVideoType = 'long',
): TranslationRequest {
  if (!/^UC[A-Za-z0-9_-]{22}$/.test(channelId)) throw new Error('A channel id is required.');
  if (!TIME_ZONE_PATTERN.test(timeZone)) throw new Error('Expected UTC or an IANA time zone.');
  return {
    channel_id: channelId,
    conditions: JSON.parse(serializeCatalogueConditions(conditions)) as CatalogueConditions,
    time_zone: timeZone,
    video_type: videoType,
  };
}

/** Strict reply parser; the query must be this channel's Library query of this type. */
export function parseTranslation(value: unknown, request: TranslationRequest): LibraryTranslation {
  const root = closedObject(value, 'translate', ['contract_version', 'translation']);
  if (root.contract_version !== 1) contractError('translate.contract_version', 'unsupported version.');
  const body = closedObject(root.translation, 'translation', ['status', 'query', 'time_zone', 'missing']);
  const status = enumValue(body.status, 'translation.status', ['translated', 'needs_capability'] as const);
  const timeZone = text(body.time_zone, 'translation.time_zone', 64);
  if (timeZone !== request.time_zone) contractError('translation.time_zone', 'answered for another time zone.');
  const missing = array(body.missing, 'translation.missing', 16).map((item, index) => {
    const entry = closedObject(item, `translation.missing[${index}]`, ['capability', 'conditions']);
    const conditions = array(entry.conditions, `translation.missing[${index}].conditions`, 64)
      .map((condition, at) => text(condition, `translation.missing[${index}].conditions[${at}]`, 128));
    if (!conditions.length) contractError(`translation.missing[${index}].conditions`, 'names no condition.');
    return { capability: text(entry.capability, `translation.missing[${index}].capability`, 128), conditions };
  });
  if (status === 'needs_capability') {
    if (body.query !== null || !missing.length) contractError('translation', 'needs_capability names what is missing and has no query.');
    return { status, query: null, timeZone, missing };
  }
  if (missing.length) contractError('translation.missing', 'a translated query misses nothing.');
  const query = openObject(body.query, 'translation.query');
  const source = openObject(query.source, 'translation.query.source');
  const scope = openObject(query.scope, 'translation.query.scope');
  const type = request.video_type ?? 'long';
  if (query.query_version !== 1 || source.id !== VIDEO_SOURCE || scope.kind !== 'channel' || scope.id !== request.channel_id
    || !Array.isArray(query.video_types) || query.video_types.length !== 1 || query.video_types[0] !== type
    || !Array.isArray(query.order_by) || !query.order_by.length) {
    contractError('translation.query', 'is not this channel\'s Library query.');
  }
  const time = query.time === undefined || query.time === null ? undefined : openObject(query.time, 'translation.query.time');
  // For v1 filters the query stays at UTC with dates already resolved to instants; local-date fields need the viewer zone.
  const queryZone = time?.timezone ?? 'UTC';
  if (queryZone !== timeZone && (queryZone !== 'UTC' || usesLocalDateFields(query.where))) {
    contractError('translation.query.time', 'another time zone.');
  }
  return { status, query: query as unknown as TranslatedQuery, timeZone, missing: [] };
}

const STATIC_CAPABILITIES = new Set(['video.title_length', 'video.tag_count', 'video.like_rate', 'video.comment_rate']);
// The 28-day growth fields (growth fields): a growth condition the server cannot run names one of them.
const GROWTH_CAPABILITIES = new Set(['observation.views_gained_28d', 'observation.growth_28d_interval_seconds', 'observation.growth_28d_status']);

/** The existing blocking texts, in the order the Videos section shows them today. */
export const BLOCKED_TEXT = {
  staticFilters: 'Static analysis filters are unavailable. Remove these filters to continue; your conditions have been kept.',
  growthFilters: 'Growth filters are unavailable. Reload or remove these filters to continue; your conditions have been kept.',
  growthSort: 'Growth sorting is unavailable. Choose another sort; your filters have been kept.',
  tooManyConditions: 'Too many conditions for one query. Remove some conditions; your conditions have been kept.',
} as const;

/** One blocking message for a `needs_capability` reply ("" when nothing blocks). */
export function translationBlockedMessage(missing: readonly MissingCapability[]): string {
  if (missing.some((item) => STATIC_CAPABILITIES.has(item.capability))) return BLOCKED_TEXT.staticFilters;
  const growth = missing.filter((item) => GROWTH_CAPABILITIES.has(item.capability)).flatMap((item) => item.conditions);
  if (growth.some((condition) => condition !== 'filters.sort_by')) return BLOCKED_TEXT.growthFilters;
  if (growth.length) return BLOCKED_TEXT.growthSort;
  if (missing.some((item) => item.capability === 'query.where_leaf_budget')) return BLOCKED_TEXT.tooManyConditions;
  return missing.length
    ? `Some conditions cannot be opened here yet (${missing.map((item) => item.capability).join(', ')}). Your conditions have been kept.`
    : '';
}

export type RequestFunction = (path: string, signal: AbortSignal | undefined, init: RequestInit) => Promise<unknown>;

let pageClient: { request: RequestFunction; client: ReturnType<typeof createTranslationClient> } | undefined;
/**
 * The page's one translation cache: the Saved list
 * and the Library Videos tab pass the same request function and share the
 * client, so a view opened from Saved is not translated twice.
 */
export function pageTranslationClient(request: RequestFunction): ReturnType<typeof createTranslationClient> {
  if (!pageClient || pageClient.request !== request) pageClient = { request, client: createTranslationClient(request) };
  return pageClient.client;
}

/**
 * Translation client with an in-memory cache by (channel, conditions, zone,
 * type): the translation is a pure function of these, so a repeat needs no
 * request. Failures are not cached.
 */
export function createTranslationClient(request: RequestFunction, maximumEntries = 64) {
  const cache = new Map<string, LibraryTranslation>();
  return {
    key(body: TranslationRequest): string {
      return JSON.stringify([body.channel_id, body.conditions, body.time_zone, body.video_type ?? 'long']);
    },
    async translate(
      channelId: string,
      conditions: CatalogueConditions,
      timeZone: string,
      videoType: LibraryVideoType = 'long',
      signal?: AbortSignal,
    ): Promise<LibraryTranslation> {
      const body = translationRequest(channelId, conditions, timeZone, videoType);
      const key = this.key(body);
      const cached = cache.get(key);
      if (cached) { cache.delete(key); cache.set(key, cached); return cached; }
      const result = parseTranslation(await request(ROUTES.translate, signal, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }), body);
      cache.set(key, result);
      while (cache.size > maximumEntries) cache.delete(cache.keys().next().value as string);
      return result;
    },
    size: () => cache.size,
  };
}
