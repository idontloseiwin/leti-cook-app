import type {
  ChannelBaselinePeriodKey,
} from '../types';
import type {
  ExplorerFilterErrors,
  ExplorerFilters,
} from './model';

export const MAX_EXPLORER_METRIC_FILTER = 10 ** 15;
export const STATIC_CATALOGUE_FILTER_KEYS = [
  'min_likes', 'max_likes', 'min_comments', 'max_comments',
  'min_title_length', 'max_title_length', 'min_tag_count', 'max_tag_count',
  'min_like_rate', 'max_like_rate', 'min_comment_rate', 'max_comment_rate',
] as const;
export type StaticCatalogueFilterKey = typeof STATIC_CATALOGUE_FILTER_KEYS[number];
/**
 * Library's further filters (the server translator research_query/library_v1.py
 * owns their meaning): Captions, Description contains, declared paid promotion and a
 * stream's actual end. Optional keys, absent when empty.
 */
export const MORE_CATALOGUE_FILTER_KEYS = ['captions', 'description_q', 'paid_promotion', 'stream_ended_from', 'stream_ended_before'] as const;
export type MoreCatalogueFilterKey = typeof MORE_CATALOGUE_FILTER_KEYS[number];
export function moreCatalogueFilters(filters: ExplorerFilters): Partial<Record<MoreCatalogueFilterKey, string>> {
  return Object.fromEntries(MORE_CATALOGUE_FILTER_KEYS.filter((key) => filters[key]?.trim()).map((key) => [key, filters[key]!.trim()]));
}
function decimalUnits(value: string): bigint {
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, '0'));
}

export function staticCatalogueBounds(filters: ExplorerFilters): Partial<Record<StaticCatalogueFilterKey, string>> {
  return Object.fromEntries(STATIC_CATALOGUE_FILTER_KEYS.filter((key) => filters[key]?.trim()).map((key) => [key, filters[key]!.trim()]));
}

export const OVERVIEW_PERIOD_OPTIONS: Array<{
  key: ChannelBaselinePeriodKey;
  label: string;
  description: string;
}> = [
  { key: '7d', label: '7D', description: 'Videos published in the last 7 days' },
  { key: '28d', label: '28D', description: 'Videos published in the last 28 days' },
  { key: '3m', label: '3M', description: 'Videos published in the last 90 days' },
  { key: '1y', label: '1Y', description: 'Videos published in the last 365 days' },
  { key: 'max', label: 'MAX', description: 'All available published videos' },
];

export const INITIAL_EXPLORER_FILTERS: ExplorerFilters = {
  q: '',
  category_id: '',
  published_from: '',
  published_before: '',
  min_views: '',
  max_views: '',
  min_duration_seconds: '',
  max_duration_seconds: '',
  sort_by: 'published_at',
  sort_order: 'desc',
};

export const VIDEO_GROWTH_SORT_FIELD = 'views_gained';
export const EXPLORER_SORT_LABELS: Record<string, string> = {
  published_at: 'Published date',
  views: 'Views',
  like_count: 'Likes',
  comment_count: 'Comments',
  duration: 'Duration',
  title: 'Title',
};

const analysisKeys = [
  'q',
  'category_id',
  'published_from',
  'published_before',
  'min_views',
  'max_views',
  'min_duration_seconds',
  'max_duration_seconds',
] as const;

function normalizedValue(value: string): string {
  return value.trim();
}

export function normalizeExplorerFilters(filters: ExplorerFilters): ExplorerFilters {
  const normalized = {
    ...filters,
    q: normalizedValue(filters.q),
    category_id: normalizedValue(filters.category_id),
    published_from: normalizedValue(filters.published_from),
    published_before: normalizedValue(filters.published_before),
    min_views: normalizedValue(filters.min_views),
    max_views: normalizedValue(filters.max_views),
    min_duration_seconds: normalizedValue(filters.min_duration_seconds),
    max_duration_seconds: normalizedValue(filters.max_duration_seconds),
  };
  for (const key of [...STATIC_CATALOGUE_FILTER_KEYS, ...MORE_CATALOGUE_FILTER_KEYS]) {
    const value = filters[key]?.trim();
    if (value) normalized[key] = value; else delete normalized[key];
  }
  return normalized;
}

export function analysisScopeKey(
  channelId: string,
  filters: ExplorerFilters,
): string {
  const normalized = normalizeExplorerFilters(filters);
  const bounds = staticCatalogueBounds(normalized);
  const more = moreCatalogueFilters(normalized);
  return JSON.stringify([
    channelId,
    ...analysisKeys.map((key) => normalized[key]),
    ...(Object.keys(bounds).length ? [bounds] : []),
    ...(Object.keys(more).length ? [{ more }] : []),
  ]);
}

export function overviewBaselineScopeKey(
  channelId: string,
  period: ChannelBaselinePeriodKey = 'max',
): string {
  return JSON.stringify([channelId, 'overview-published-window-v1', period]);
}

function validateNonNegativeInteger(
  value: string,
  label: string,
): string | undefined {
  if (!value) return undefined;
  if (!/^\d+$/.test(value)) return `${label} must be a whole number.`;
  const numeric = Number(value);
  if (
    !Number.isSafeInteger(numeric)
    || numeric < 0
    || numeric > MAX_EXPLORER_METRIC_FILTER
  ) {
    return `${label} is outside the supported range.`;
  }
  return undefined;
}

function validateDate(value: string, label: string): string | undefined {
  if (!value) return undefined;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${label} must be a valid date.`;
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) {
    return `${label} must be a valid date.`;
  }
  return undefined;
}

function numericValue(value: string): number | undefined {
  return value ? Number(value) : undefined;
}

export function validateExplorerFilters(
  filters: ExplorerFilters,
): ExplorerFilterErrors {
  const normalized = normalizeExplorerFilters(filters);
  const errors: ExplorerFilterErrors = {};

  for (let index = 0; index < STATIC_CATALOGUE_FILTER_KEYS.length; index += 2) {
    const min = STATIC_CATALOGUE_FILTER_KEYS[index], max = STATIC_CATALOGUE_FILTER_KEYS[index + 1];
    for (const key of [min, max]) {
      const value = normalized[key] || '';
      if (value.length > 32) {
        errors[key] = 'Use at most 32 characters.';
        continue;
      }
      if (key.includes('_rate')) {
        if (value && (!/^\d+(?:\.\d{1,6})?$/.test(value) || decimalUnits(value) > 1_000_000_000_000_000_000_000n)) {
          errors[key] = 'Use a nonnegative percentage up to 1,000,000,000,000,000 with at most six decimal places.';
        }
      } else errors[key] = validateNonNegativeInteger(value, key.replaceAll('_', ' '));
    }
    if (!errors[min] && !errors[max] && normalized[min] && normalized[max]) {
      if (decimalUnits(normalized[min]!) > decimalUnits(normalized[max]!)) errors[max] = 'Maximum must be at least the minimum.';
    }
  }

  if (normalized.q.length > 100) errors.q = 'Search is limited to 100 characters.';
  if (normalized.category_id && !/^\d{1,3}$/.test(normalized.category_id)) {
    errors.category_id = 'Category ID must contain one to three digits.';
  }

  errors.published_from = validateDate(normalized.published_from, 'Published from');
  errors.published_before = validateDate(normalized.published_before, 'Published before');
  errors.min_views = validateNonNegativeInteger(normalized.min_views, 'Minimum views');
  errors.max_views = validateNonNegativeInteger(normalized.max_views, 'Maximum views');
  errors.min_duration_seconds = validateNonNegativeInteger(
    normalized.min_duration_seconds,
    'Minimum duration',
  );
  errors.max_duration_seconds = validateNonNegativeInteger(
    normalized.max_duration_seconds,
    'Maximum duration',
  );

  if (
    !errors.published_from
    && !errors.published_before
    && normalized.published_from
    && normalized.published_before
    && normalized.published_from >= normalized.published_before
  ) {
    errors.published_before = 'Published before must be later than Published from.';
  }

  const minViews = numericValue(normalized.min_views);
  const maxViews = numericValue(normalized.max_views);
  if (
    !errors.min_views
    && !errors.max_views
    && minViews !== undefined
    && maxViews !== undefined
    && minViews > maxViews
  ) {
    errors.max_views = 'Maximum views must be at least the minimum.';
  }

  const minDuration = numericValue(normalized.min_duration_seconds);
  const maxDuration = numericValue(normalized.max_duration_seconds);
  if (
    !errors.min_duration_seconds
    && !errors.max_duration_seconds
    && minDuration !== undefined
    && maxDuration !== undefined
    && minDuration > maxDuration
  ) {
    errors.max_duration_seconds = 'Maximum duration must be at least the minimum.';
  }

  for (const key of Object.keys(errors) as Array<keyof ExplorerFilterErrors>) {
    if (!errors[key]) delete errors[key];
  }
  return errors;
}
