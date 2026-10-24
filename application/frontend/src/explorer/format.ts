import type { Primitive } from '../types';
import { formatYouTubeDuration } from '../format';

const exactInteger = new Intl.NumberFormat('en');
const compactInteger = new Intl.NumberFormat('en', {
  notation: 'compact',
  maximumFractionDigits: 1,
});
const decimalFormatter = new Intl.NumberFormat('en', {
  maximumFractionDigits: 2,
});

export function hasMetricValue(value: Primitive): boolean {
  return value !== null && value !== undefined && value !== '';
}

// Derived counts (medians, percentiles, per-day rates) carry fractions that are
// noise at scale: whole numbers from 1,000 up, at most two places below that.
const countDecimals = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });
const wholeCount = new Intl.NumberFormat('en', { maximumFractionDigits: 0 });

function roundedDecimal(integerDigits: string, fractionDigits: string): { integer: bigint; fraction: string } {
  const places = integerDigits.replace(/^0+(?=\d)/, '').length > 3 ? 0 : 2;
  let scaled = BigInt(integerDigits + fractionDigits.slice(0, places).padEnd(places, '0'));
  if (fractionDigits.charAt(places) >= '5') scaled += 1n;
  const factor = 10n ** BigInt(places);
  const fraction = places ? (scaled % factor).toString().padStart(places, '0').replace(/0+$/, '') : '';
  return { integer: scaled / factor, fraction };
}

// "1 video" but "2 videos": the noun follows the count however it is written.
export function videoNoun(count: unknown): 'video' | 'videos' {
  return Number(String(count ?? '').replaceAll(',', '')) === 1 ? 'video' : 'videos';
}

export function formatCount(
  value: Primitive,
  options: { compact?: boolean; missing?: string } = {},
): string {
  const { compact = true, missing = 'Not reported' } = options;
  if (!hasMetricValue(value)) return missing;

  if (typeof value === 'string') {
    const normalized = value.trim();
    const decimal = normalized.match(/^(-?)(\d+)(?:\.(\d+))?$/);
    if (decimal) {
      const [, sign, integerDigits, fractionDigits] = decimal;
      try {
        if (compact || !fractionDigits) {
          return (compact ? compactInteger : exactInteger).format(BigInt(`${sign}${integerDigits}`));
        }
        const { integer, fraction } = roundedDecimal(integerDigits, fractionDigits);
        // BigInt has no -0, so a negative value below one keeps its sign by hand.
        const rendered = exactInteger.format(sign ? -integer : integer);
        return `${sign && !integer && fraction ? '-' : ''}${rendered}${fraction ? `.${fraction}` : ''}`;
      } catch {
        return normalized;
      }
    }
    return normalized;
  }

  if (typeof value === 'number' && Number.isFinite(value)) {
    if (compact) return compactInteger.format(value);
    return (Math.abs(value) >= 1000 ? wholeCount : countDecimals).format(value);
  }
  return String(value);
}

export function formatDecimal(
  value: Primitive,
  missing = 'Not measured',
): string {
  if (!hasMetricValue(value)) return missing;
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? decimalFormatter.format(numeric) : String(value);
}

export function formatTimestamp(
  value?: string | null,
  missing = 'Not recorded',
): string {
  if (!value) return missing;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Invalid timestamp';
  return new Intl.DateTimeFormat('en', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function formatDate(value?: string | null): string {
  if (!value) return 'Not reported';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Invalid date';
  return new Intl.DateTimeFormat('en', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(date);
}

export function formatDuration(value: Primitive): string {
  return hasMetricValue(value) ? formatYouTubeDuration(value) : 'Not reported';
}

export function initials(value?: string | null): string {
  const words = (value || '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
  return words.map((word) => word[0]?.toUpperCase()).join('') || 'YT';
}

export function youtubeChannelUrl(channelId?: string): string {
  return channelId
    ? `https://www.youtube.com/channel/${encodeURIComponent(channelId)}`
    : 'https://www.youtube.com/';
}

export function youtubeVideoUrl(videoId?: string): string | undefined {
  return videoId
    ? `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}`
    : undefined;
}
