import type { ChannelVideoViewGrowth } from '../types';
import { formatCount } from './format';
import { formatTimestampLocal } from './localTime';

const unavailable = 'Not available';
const statusLabels: Record<ChannelVideoViewGrowth['status'], string> = {
  ready: '28-day comparison available',
  partial: 'Partial history',
  started_today: 'Tracking started today',
  waiting_next_snapshot: 'Waiting for next snapshot',
  unavailable: 'Growth unavailable',
};

function observationTimestamp(value: string | null): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)
    || !Number.isFinite(Date.parse(value))) return unavailable;
  // Date.parse normalizes impossible calendar dates; do not display them as evidence.
  const datePart = value.slice(0, 10), calendar = new Date(`${datePart}T00:00:00Z`);
  if (!Number.isFinite(calendar.getTime()) || calendar.toISOString().slice(0, 10) !== datePart) return unavailable;
  return formatTimestampLocal(value, unavailable);
}

function observedInterval(seconds: number | null): string {
  if (!Number.isSafeInteger(seconds) || seconds === null || seconds < 0) return unavailable;
  let remaining = seconds;
  const parts: string[] = [];
  for (const [size, label] of [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']] as const) {
    const units = Math.floor(remaining / size);
    if (units) parts.push(`${units}${label}`);
    remaining %= size;
  }
  return parts.join(' ') || '0s';
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

/** Presentation of server-delivered evidence; no browser rate calculation or freshness rule. */
export function presentVideoInspectorGrowth(growth: ChannelVideoViewGrowth) {
  const calculable = growth.status === 'ready' || growth.status === 'partial';
  return {
    status: statusLabels[growth.status] ?? statusLabels.unavailable,
    delta: calculable ? exactDelta(growth.delta_views) ?? unavailable : null,
    average: calculable ? averageRate(growth) : null,
    start: observationTimestamp(growth.observed_start_at),
    end: observationTimestamp(growth.observed_end_at),
    interval: observedInterval(growth.actual_interval_seconds),
  };
}
