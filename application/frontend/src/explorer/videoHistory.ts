import type { VideoHistoryInterval, VideoHistoryPayload, VideoInteractionHistoryInterval, VideoInteractionHistorySeries } from '../types';
import { ExplorerContractError, utcTimestampMicroseconds } from './contracts';
import { formatCount } from './format';
import { formatExactLocal } from './localTime';
import { freshSourceRows, sourceExpired, transferSourceLifetime } from '../sourceLifetime';

const integer = (value: unknown): value is string => typeof value === 'string' && /^(?:0|[1-9]\d*)$/.test(value);
const signedInteger = (value: unknown): value is string => typeof value === 'string' && /^(?:0|-?[1-9]\d*)$/.test(value);
const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const invalid = (): never => { throw new ExplorerContractError('The server returned incompatible video snapshot history.'); };
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 500;

export function historySecondsMicroseconds(value: unknown): bigint | null {
  if (typeof value !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d{1,6})?$/.test(value)) return null;
  const [whole, fraction = ''] = value.split('.');
  return BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, '0'));
}

/** Validate source identity, actual ordering and adjacency; never fill missing metrics. */
export function parseVideoHistoryV1(value: unknown, channelId: string, videoId: string, browseSession: string): VideoHistoryPayload {
  if (!record(value)) return invalid();
  const { window, coverage, methods, source, points, intervals } = value;
  const generated = utcTimestampMicroseconds(value.generated_at), created = utcTimestampMicroseconds(value.browse_created_at);
  const expires = utcTimestampMicroseconds(value.expires_at);
  if (value.contract_version !== 1 || value.channel_id !== channelId || value.video_id !== videoId || value.browse_session !== browseSession
    || generated === null || created === null || expires === null || generated < created || expires <= generated
    || !record(window) || !record(coverage) || !record(methods) || !record(source)
    || !Array.isArray(points) || !Array.isArray(intervals) || points.length > 500 || intervals.length > 499) return invalid();
  if (window.requested_days !== 28 || window.endpoint !== 'frozen_catalogue_checked_at'
    || coverage.complete !== true || coverage.row_limit !== 500
    || !['scanned_count', 'point_count', 'valid_view_count', 'missing_view_count', 'duplicate_timestamp_count'].every((key) => count(coverage[key]))
    || coverage.point_count !== points.length || Number(coverage.valid_view_count) + Number(coverage.missing_view_count) !== points.length
    || Number(coverage.scanned_count) < points.length + Number(coverage.duplicate_timestamp_count)
    || methods.ordering !== 'recorded_at_ascending' || methods.same_timestamp !== 'identical_views_collapsed_conflicts_rejected'
    || methods.gaps !== 'no_interpolation_or_daily_allocation' || methods.rate !== 'signed_delta_per_exact_elapsed_second_times_86400_rounded_2dp_half_even'
    || methods.window !== 'strict_28d_not_card_comparison_interval'
    || source.kind !== 'derived_video_snapshot_history' || source.snapshots_used !== true || source.youtube_requests !== false
    || source.database_writes !== false || source.attribution !== 'Leti Cook') return invalid();
  const from = utcTimestampMicroseconds(window.from_at), to = utcTimestampMicroseconds(window.to_at);
  const invalidEndpoint = value.status === 'unavailable'
    && ['invalid_catalogue_endpoint', 'catalogue_endpoint_outside_hot_window'].includes(String(value.reason));
  if (invalidEndpoint ? window.from_at !== null || window.to_at !== null
    : from === null || to === null || generated - from !== 28n * 86400n * 1000000n || to < from || to > generated) return invalid();
  validateHistorySeries(value, window, Number(coverage.scanned_count), {
    value: 'views', delta: 'delta_views', rate: 'views_per_day', valid: 'valid_view_count', missing: 'missing_view_count',
  });
  const interactions = value.interaction_metrics;
  if (interactions === undefined) {
    if (methods.interaction_metrics !== undefined) return invalid();
  } else {
    if (!record(interactions) || !record(interactions.window) || !count(interactions.scanned_count)
      || methods.interaction_metrics !== 'independent_stored_counters_same_observation_window') return invalid();
    const shared = interactions.window;
    const sharedFrom = utcTimestampMicroseconds(shared.from_at), sharedTo = utcTimestampMicroseconds(shared.to_at);
    const nullWindow = shared.from_at === null && shared.to_at === null;
    if (shared.requested_days !== 28 || shared.endpoint !== 'frozen_catalogue_checked_at'
      || (nullWindow ? !invalidEndpoint || from !== null || to !== null
        || interactions.scanned_count !== 0 || !['likes', 'comments'].every((metric) => record(interactions[metric])
          && interactions[metric].status === 'unavailable' && interactions[metric].reason === 'invalid_catalogue_endpoint')
        : sharedFrom === null || sharedTo === null || generated - sharedFrom !== 28n * 86400n * 1000000n || sharedTo < sharedFrom || sharedTo > generated
          || from !== null && sharedFrom !== from || to !== null && sharedTo !== to)) return invalid();
    for (const metric of ['likes', 'comments']) validateHistorySeries(interactions[metric], shared, interactions.scanned_count,
      { value: 'value', delta: 'delta', rate: 'per_day', valid: 'valid_count', missing: 'missing_count' }, true);
  }
  return value as unknown as VideoHistoryPayload;
}

/** Shared per-counter validation: a null or unavailable series cannot borrow its neighbor's evidence. */
function validateHistorySeries(value: unknown, window: Record<string, unknown>, scanned: number,
  keys: { value: string; delta: string; rate: string; valid: string; missing: string }, allowConflict = false): void {
  if (!record(value) || !record(value.coverage) || !Array.isArray(value.points) || !Array.isArray(value.intervals)) return invalid();
  const { coverage, points, intervals } = value;
  if (points.length > 500 || intervals.length > 499 || !['point_count', keys.valid, keys.missing, 'duplicate_timestamp_count'].every((key) => count(coverage[key]))
    || coverage.point_count !== points.length || Number(coverage[keys.valid]) + Number(coverage[keys.missing]) !== points.length
    || scanned < points.length + Number(coverage.duplicate_timestamp_count)) return invalid();
  if (value.status === 'unavailable') {
    const reasons = ['endpoint_snapshot_missing', 'endpoint_counter_mismatch', 'invalid_catalogue_endpoint', 'catalogue_endpoint_outside_hot_window'];
    if (allowConflict) reasons.push('conflicting_simultaneous_observations');
    if (!reasons.includes(String(value.reason)) || points.length || intervals.length || value.latest_interval !== null) return invalid();
    return;
  }
  const from = utcTimestampMicroseconds(window.from_at), to = utcTimestampMicroseconds(window.to_at);
  if (value.status !== 'ready' || value.reason !== null || !points.length || from === null || to === null || intervals.length !== points.length - 1) return invalid();
  const times: bigint[] = [];
  let known = 0;
  for (const point of points) {
    if (!record(point) || !(point[keys.value] === null || integer(point[keys.value]))) return invalid();
    const at = utcTimestampMicroseconds(point.recorded_at);
    if (at === null || at < from || at > to || (times.length && at <= times[times.length - 1])) return invalid();
    times.push(at);
    if (point[keys.value] !== null) known++;
  }
  if (known !== coverage[keys.valid] || times.at(-1) !== to) return invalid();
  for (let index = 0; index < intervals.length; index++) {
    const interval = intervals[index], left = points[index], right = points[index + 1];
    if (!record(interval) || interval.from_at !== left.recorded_at || interval.to_at !== right.recorded_at
      || historySecondsMicroseconds(interval.actual_interval_seconds) !== times[index + 1] - times[index]) return invalid();
    if (left[keys.value] === null || right[keys.value] === null) {
      if (interval[keys.delta] !== null || interval[keys.rate] !== null) return invalid();
    } else {
      const rawDelta = interval[keys.delta], rate = interval[keys.rate];
      if (!signedInteger(rawDelta) || BigInt(rawDelta) !== BigInt(right[keys.value]) - BigInt(left[keys.value])
        || typeof rate !== 'string' || !/^-?(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(rate)
        || /^-0(?:\.0+)?$/.test(rate)) return invalid();
      const delta = BigInt(rawDelta), nonzeroRate = !/^0(?:\.0+)?$/.test(rate);
      if (nonzeroRate && (delta === 0n || rate.startsWith('-') !== (delta < 0n))) return invalid();
    }
  }
  const latest = intervals.at(-1);
  if (!latest || latest[keys.delta] === null) {
    if (value.latest_interval !== null) return invalid();
  } else if (!record(value.latest_interval) || ['from_at', 'to_at', 'actual_interval_seconds', keys.delta, keys.rate]
    .some((key) => value.latest_interval && (value.latest_interval as Record<string, unknown>)[key] !== latest[key])) return invalid();
}

export type HistoryMetric = 'views' | 'likes' | 'comments';
export const HISTORY_METRICS = ['views', 'likes', 'comments'] as const;
export type HistorySeries = VideoInteractionHistorySeries & { window: VideoHistoryPayload['window'] };

/** Map wire names only; every metric uses the same presentation and geometry without new calculations. */
export function videoHistorySeries(payload: VideoHistoryPayload, metric: HistoryMetric = 'views'): HistorySeries | null {
  const fresh = (series: HistorySeries): HistorySeries => ({ ...series,
    points: freshSourceRows(series.points), intervals: freshSourceRows(series.intervals),
    latest_interval: sourceExpired(series.latest_interval) ? null : series.latest_interval,
  });
  if (metric !== 'views') {
    const interactions = payload.interaction_metrics;
    return interactions ? fresh({ ...interactions[metric], window: interactions.window }) : null;
  }
  const interval = (item: VideoHistoryInterval): VideoInteractionHistoryInterval => transferSourceLifetime(item, { from_at: item.from_at, to_at: item.to_at,
    actual_interval_seconds: item.actual_interval_seconds, delta: item.delta_views, per_day: item.views_per_day });
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
export function videoHistoryGeometry(payload: VideoHistoryPayload, mode: HistoryMode, metric: HistoryMetric = 'views') {
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
