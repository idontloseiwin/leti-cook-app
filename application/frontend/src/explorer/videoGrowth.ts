import type { ChannelVideoViewGrowth, Primitive } from '../types';

const compactCountFormatter = new Intl.NumberFormat('en', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

export type VideoGrowthTone =
  | 'positive'
  | 'observed'
  | 'correction'
  | 'neutral'
  | 'muted';

export type VideoGrowthPresentation = {
  text: string;
  tone: VideoGrowthTone;
};

export function videoGrowthAttemptKey(channelId: string, reloadToken: number): string {
  return `${channelId}\nreload:${reloadToken}`;
}

export function isVideoGrowthEffectivelyAvailable(
  capabilityAvailable: boolean,
  failedAttemptKey: string | null,
  currentAttemptKey: string,
): boolean {
  return capabilityAvailable && failedAttemptKey !== currentAttemptKey;
}

function numericSign(value: Primitive): -1 | 0 | 1 | null {
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return value === 0 ? 0 : value < 0 ? -1 : 1;
  }
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  if (!/^-?(?:0|[1-9]\d*)$/.test(normalized)) return null;
  if (normalized.startsWith('-')) return -1;
  return /^0(?:\.0+)?$/.test(normalized) ? 0 : 1;
}

function signedCompactCount(value: Primitive): string | null {
  const sign = numericSign(value);
  if (sign === null) return null;
  let rendered: string;
  if (typeof value === 'string') {
    try {
      rendered = compactCountFormatter.format(BigInt(value.trim()));
    } catch {
      return null;
    }
  } else if (typeof value === 'number' && Number.isInteger(value)) {
    rendered = compactCountFormatter.format(value);
  } else {
    return null;
  }
  return sign > 0 ? `+${rendered}` : rendered;
}

function observedPeriod(seconds: number | null): string | null {
  if (seconds === null || !Number.isSafeInteger(seconds) || seconds <= 0) return null;
  // Presentation tolerance only; it never changes snapshot admission or status.
  if (seconds >= 27 * 86400 && seconds <= 29 * 86400) return 'over ~28 days';
  if (seconds < 86400) return 'over <1 day';
  const days = Math.round(seconds / 86400 * 10) / 10;
  const approximate = seconds % 86400 === 0 ? '' : '~';
  return `over ${approximate}${days} ${days === 1 ? 'day' : 'days'}`;
}

function deltaTone(
  growth: ChannelVideoViewGrowth,
  sign: -1 | 0 | 1,
): VideoGrowthTone {
  if (sign < 0) return 'correction';
  if (sign === 0) return 'neutral';
  return growth.status === 'partial' ? 'observed' : 'positive';
}

export function presentVideoViewGrowth(
  growth?: ChannelVideoViewGrowth | null,
): VideoGrowthPresentation {
  if (!growth || growth.status === 'unavailable') {
    return { text: 'Growth unavailable', tone: 'muted' };
  }
  if (growth.status === 'started_today') {
    return { text: 'Tracking started today', tone: 'muted' };
  }
  if (growth.status === 'waiting_next_snapshot') {
    return { text: 'Waiting for next snapshot', tone: 'muted' };
  }

  const delta = signedCompactCount(growth.delta_views);
  const sign = numericSign(growth.delta_views);
  if (delta === null || sign === null) {
    return { text: 'Growth unavailable', tone: 'muted' };
  }
  const period = observedPeriod(growth.actual_interval_seconds);
  if (period === null) return { text: 'Growth unavailable', tone: 'muted' };
  return {
    text: `${delta} ${period}`,
    tone: deltaTone(growth, sign),
  };
}
