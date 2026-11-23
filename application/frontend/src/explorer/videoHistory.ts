import type { ChannelVideoViewGrowth, VideoHistoryInterval, VideoInteractionHistoryInterval, VideoInteractionHistorySeries } from '../types';
import { utcTimestampMicroseconds } from './contracts';
import { formatCount } from './format';
import { formatExactLocal, formatTimestampLocal } from './localTime';
import { GROWTH_STATUS_LABELS, observedInterval } from './videoGrowth';
import { freshSourceRows, sourceExpired, transferSourceLifetime } from '../sourceLifetime';

export function historySecondsMicroseconds(value: unknown): bigint | null {
  if (typeof value !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(value)) return null;
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, '0'));
}

export type HistoryMetric = 'views' | 'likes' | 'comments';
export const HISTORY_METRICS = ['views', 'likes', 'comments'] as const;

/** Views intervals of the shared operation say why a change is withheld. */
export type HistoryIntervalDefinition = 'known' | 'unknown_definition' | 'incompatible_definition';
export type HistoryViewInterval = VideoInteractionHistoryInterval & { definition?: HistoryIntervalDefinition };
/** One metric's series as the panel draws it (the old interaction series, any reason code). */
export type HistoryViewSeries = Omit<VideoInteractionHistorySeries, 'reason' | 'intervals'> & {
  reason: string | null;
  intervals: HistoryViewInterval[];
};
type HistoryWindow = { from_at: string | null; to_at: string | null };
/** What the history panel reads: the adapter's view of `video.observed_history.v1` (libraryData/inspector.ts). */
export type VideoHistoryView = {
  status: 'ready' | 'unavailable';
  reason: string | null;
  window: HistoryWindow;
  points: { recorded_at: string; views: string | null }[];
  intervals: (VideoHistoryInterval & { definition?: HistoryIntervalDefinition })[];
  latest_interval: VideoHistoryInterval | null;
  coverage: { point_count: number; valid_view_count: number; missing_view_count: number; duplicate_timestamp_count: number };
  interaction_metrics?: { window: HistoryWindow; likes: HistoryViewSeries; comments: HistoryViewSeries };
};
export type HistorySeries = HistoryViewSeries & { window: HistoryWindow };

/** Map wire names only; every metric uses the same presentation and geometry without new calculations. */
export function videoHistorySeries(payload: VideoHistoryView, metric: HistoryMetric = 'views'): HistorySeries | null {
  const fresh = (series: HistorySeries): HistorySeries => ({ ...series,
    points: freshSourceRows(series.points), intervals: freshSourceRows(series.intervals),
    latest_interval: sourceExpired(series.latest_interval) ? null : series.latest_interval,
  });
  if (metric !== 'views') {
    const interactions = payload.interaction_metrics;
    return interactions ? fresh({ ...interactions[metric], window: interactions.window }) : null;
  }
  const interval = (item: VideoHistoryInterval & { definition?: HistoryIntervalDefinition }): HistoryViewInterval => transferSourceLifetime(item, {
    from_at: item.from_at, to_at: item.to_at, actual_interval_seconds: item.actual_interval_seconds, delta: item.delta_views,
    per_day: item.views_per_day, ...(item.definition ? { definition: item.definition } : {}) });
  return fresh({ status: payload.status, reason: payload.reason, window: payload.window,
    points: payload.points.map((point) => transferSourceLifetime(point, { recorded_at: point.recorded_at, value: point.views })),
    intervals: payload.intervals.map(interval), latest_interval: payload.latest_interval ? interval(payload.latest_interval) : null,
    coverage: { point_count: payload.coverage.point_count, valid_count: payload.coverage.valid_view_count,
      missing_count: payload.coverage.missing_view_count, duplicate_timestamp_count: payload.coverage.duplicate_timestamp_count } });
}

export function historyExactTime(value: string): string { return formatExactLocal(value, 'Not recorded'); }
export function historyExactCount(value: string | null, signed = false): string {
  return value === null ? 'Not reported' : `${signed && BigInt(value) > 0n ? '+' : ''}${formatCount(value, { compact: false })}`;
}
export function historyIntervalLabel(value: string): string {
  const micros = historySecondsMicroseconds(value);
  if (micros === null) return 'Not available';
  let remaining = micros;
  const parts: string[] = [];
  for (const [size, label] of [[86400000000n, 'd'], [3600000000n, 'h'], [60000000n, 'm']] as const) {
    const units = remaining / size;
    if (units) parts.push(`${units}${label}`);
    remaining %= size;
  }
  if (remaining || !parts.length) {
    const fraction = (remaining % 1000000n).toString().padStart(6, '0').replace(/0+$/, '');
    parts.push(`${remaining / 1000000n}${fraction ? `.${fraction}` : ''}s`);
  }
  return parts.join(' ');
}
/** Why an interval has no change although both values were stored; null when it has one. */
export function historyDefinitionNote(interval: HistoryViewInterval | null): string | null {
  if (!interval?.definition || interval.definition === 'known') return null;
  return interval.definition === 'incompatible_definition' ? 'Views definition changed across this interval.'
    : 'Views definition not recorded at one end of this interval.';
}
/** The status line of an unavailable series. */
export function historyUnavailableText(metric: HistoryMetric, reason: string | null): string {
  if (reason === 'conflicting_simultaneous_observations') return `Stored ${metric} observations conflict at the same timestamp.`;
  if (reason === 'observation_limit_exceeded') return `More than 500 stored ${metric} observations fall in this window; the history is not shown.`;
  return `Stored ${metric} history cannot be matched to this video\u2019s catalogue observation.`;
}
export function historyIntervalRate(interval: VideoHistoryInterval | VideoInteractionHistoryInterval | null, metric: HistoryMetric = 'views'): string {
  if (!interval) return 'Not available';
  const rawDelta = 'delta_views' in interval ? interval.delta_views : interval.delta;
  const rate = 'views_per_day' in interval ? interval.views_per_day : interval.per_day;
  if (rate === null || rawDelta === null) return 'Not available';
  const delta = BigInt(rawDelta);
  if (/^0(?:\.0+)?$/.test(rate)) return delta === 0n ? `0 ${metric}/day` : `${delta > 0n ? '+' : '-'}<0.01 ${metric}/day`;
  return `~${delta > 0n ? '+' : '-'}${formatCount(rate.replace(/^-/, ''), { compact: false })} ${metric}/day`;
}

export type HistoryMode = 'cumulative' | 'interval';
/** Geometry only. Counters stay exact until conversion of normalized SVG positions. */
export function videoHistoryGeometry(payload: VideoHistoryView, mode: HistoryMode, metric: HistoryMetric = 'views') {
  const series = videoHistorySeries(payload, metric);
  if (!series || series.status !== 'ready') return null;
  const start = utcTimestampMicroseconds(series.window.from_at), end = utcTimestampMicroseconds(series.window.to_at);
  if (start === null || end === null || end <= start) return null;
  const source = mode === 'cumulative' ? series.points.map((point) => ({ from: point.recorded_at, to: point.recorded_at, value: point.value }))
    : series.intervals.map((interval) => ({ from: interval.from_at, to: interval.to_at, value: interval.delta }));
  const values = source.flatMap((point) => point.value === null ? [] : [BigInt(point.value)]);
  if (!values.length) return null;
  let low = values.reduce((a, b) => a < b ? a : b), high = values.reduce((a, b) => a > b ? a : b);
  if (mode === 'interval') { if (low > 0n) low = 0n; if (high < 0n) high = 0n; }
  if (low === high) { if (low > 0n) low--; else high++; }
  const ratio = (value: bigint, lower: bigint, upper: bigint) => Number((value - lower) * 1000000n / (upper - lower)) / 1000000;
  const x = (at: string) => 54 + ratio(utcTimestampMicroseconds(at)!, start, end) * 330;
  const y = (value: string) => 160 - ratio(BigInt(value), low, high) * 140;
  return { low: low.toString(), high: high.toString(), zeroY: mode === 'interval' ? y('0') : null,
    points: source.map((point, index) => ({ index, fromX: x(point.from), x: x(point.to), y: point.value === null ? null : y(point.value), ...point })) };
}

// "Observed views" of the Inspector: the 28-day growth evidence of the opened row.

const unavailable = 'Not available';
// The status words and the interval are shared with the workshop's rows (videoGrowth.ts).
const statusLabels = GROWTH_STATUS_LABELS;

function observationTimestamp(value: string | null): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)
    || !Number.isFinite(Date.parse(value))) return unavailable;
  // Date.parse normalizes impossible calendar dates; do not display them as evidence.
  const datePart = value.slice(0, 10), calendar = new Date(`${datePart}T00:00:00Z`);
  if (!Number.isFinite(calendar.getTime()) || calendar.toISOString().slice(0, 10) !== datePart) return unavailable;
  return formatTimestampLocal(value, unavailable);
}

function exactDelta(value: ChannelVideoViewGrowth['delta_views']): string | null {
  if (!(typeof value === 'number' && Number.isSafeInteger(value))
    && !(typeof value === 'string' && /^-?(?:0|[1-9]\d*)$/.test(value))) return null;
  const integer = BigInt(value);
  return `${integer > 0n ? '+' : ''}${formatCount(integer.toString(), { compact: false })} views`;
}

function averageRate(growth: ChannelVideoViewGrowth): string {
  const rate = growth.average_views_per_day;
  if (typeof rate !== 'string' || !/^-?(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(rate)
    || /^-0(?:\.0+)?$/.test(rate) || exactDelta(growth.delta_views) === null
    || growth.actual_interval_seconds === null || !Number.isSafeInteger(growth.actual_interval_seconds)
    || growth.actual_interval_seconds <= 0) return unavailable;
  const delta = BigInt(growth.delta_views!);
  // Preserve tiny signed corrections instead of displaying a rounded false zero.
  if (/^0(?:\.0+)?$/.test(rate)) return delta === 0n ? '0 views/day'
    : `${delta > 0n ? '+' : '-'}<0.01 views/day`;
  if (delta === 0n || rate.startsWith('-') !== (delta < 0n)) return unavailable;
  const magnitude = rate.startsWith('-') ? rate.slice(1) : rate;
  return `~${delta > 0n ? '+' : '-'}${formatCount(magnitude, { compact: false })} views/day`;
}

/**
 * Presentation of server-delivered evidence; no browser rate calculation or freshness rule.
 * "Today" is a date in the request's zone: the shared evidence names it, and the
 * Inspector line shows it next to "Tracking started today".
 */
export function presentVideoInspectorGrowth(growth: ChannelVideoViewGrowth & { time_zone?: string }) {
  const calculable = growth.status === 'ready' || growth.status === 'partial';
  const zone = growth.status === 'started_today' && typeof growth.time_zone === 'string' && growth.time_zone
    ? ` (${growth.time_zone})` : '';
  return {
    status: `${statusLabels[growth.status] ?? statusLabels.unavailable}${zone}`,
    delta: calculable ? exactDelta(growth.delta_views) ?? unavailable : null,
    average: calculable ? averageRate(growth) : null,
    start: observationTimestamp(growth.observed_start_at),
    end: observationTimestamp(growth.observed_end_at),
    interval: observedInterval(growth.actual_interval_seconds),
  };
}
