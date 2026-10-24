import type { Primitive } from './types';

const compactFormatter = new Intl.NumberFormat('en', {
  notation: 'compact',
  maximumFractionDigits: 1
});

const integerFormatter = new Intl.NumberFormat('en');

export function numberValue(value: Primitive): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

export function formatNumber(value: Primitive, compact = true): string {
  const numeric = numberValue(value);
  return compact ? compactFormatter.format(numeric) : integerFormatter.format(numeric);
}

export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return '0%';
  return `${Math.round(value)}%`;
}

export function formatDateTime(value?: string | null): string {
  if (!value) return 'Not recorded';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Invalid date';
  return new Intl.DateTimeFormat('en', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date);
}

export function formatRelative(value?: string | null): string {
  if (!value) return 'No timestamp';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Invalid date';
  const diffMs = Date.now() - date.getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function ago(value: number, unit: string): string {
  return String(value) + ' ' + unit + (value === 1 ? '' : 's') + ' ago';
}

export function formatYouTubeVideoAge(value?: string | null, nowMs = Date.now()): string {
  if (!value) return 'unknown';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'unknown';

  const diffMs = Math.max(0, nowMs - date.getTime());
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return ago(minutes, 'minute');

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return ago(hours, 'hour');

  const days = Math.floor(hours / 24);
  if (days < 14) return ago(Math.max(1, days), 'day');
  if (days < 29) return ago(Math.floor(days / 7), 'week');
  if (days < 365) return ago(Math.min(11, Math.max(1, Math.floor(days / 30))), 'month');
  return ago(Math.floor(days / 365), 'year');
}

export function formatYouTubeDuration(value?: Primitive | null): string {
  if (value === null || value === undefined || value === '') return 'n/a';
  const raw = String(value).trim();
  if (!raw) return 'n/a';

  const isoMatch = raw.match(/^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/);
  if (isoMatch) {
    const hours = Number(isoMatch[1] || 0);
    const minutes = Number(isoMatch[2] || 0);
    const seconds = Number(isoMatch[3] || 0);
    const paddedSeconds = String(seconds).padStart(2, '0');
    if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${paddedSeconds}`;
    return `${minutes}:${paddedSeconds}`;
  }

  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) {
    const wholeSeconds = Math.floor(seconds);
    const hours = Math.floor(wholeSeconds / 3600);
    const minutes = Math.floor((wholeSeconds % 3600) / 60);
    const remainingSeconds = wholeSeconds % 60;
    if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`;
    return `${minutes}:${String(remainingSeconds).padStart(2, '0')}`;
  }

  return raw;
}

export function firstText(...values: Array<string | null | undefined>): string {
  for (const value of values) {
    if (value && value.trim()) return value.trim();
  }
  return 'Untitled';
}