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
  /** Hover text when it says more than `text` (the zone of "today"). */
  title?: string;
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

/** A view change as the Library card shows it: "+583.3K" (also the workshop's "Views gained"). */
export function signedCompactCount(value: Primitive): string | null {
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

/** The 28-day growth status as the Library Inspector names it (also the workshop's rows). */
export const GROWTH_STATUS_LABELS: Record<ChannelVideoViewGrowth['status'], string> = {
  ready: '28-day comparison available',
  partial: 'Partial history',
  started_today: 'Tracking started today',
  waiting_next_snapshot: 'Waiting for next snapshot',
  unavailable: 'Growth unavailable',
};

/** An observed interval as the Library Inspector shows it: "14d 1h 12m 47s" (also the workshop's rows). */
export function observedInterval(seconds: number | null, missing = 'Not available'): string {
  if (!Number.isSafeInteger(seconds) || seconds === null || seconds < 0) return missing;
  let remaining = seconds;
  const parts: string[] = [];
  for (const [size, label] of [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']] as const) {
    const units = Math.floor(remaining / size);
    if (units) parts.push(`${units}${label}`);
    remaining %= size;
  }
  return parts.join(' ') || '0s';
}

/**
 * A 28-day average from the server ("386.53", at most two places) as the Library Inspector shows it:
 * "~+386.53 views/day"; zero reads "0 views/day". Null when the text is not such a number.
 */
export function averageViewsPerDay(rate: string): string | null {
  const match = /^(-?)((?:0|[1-9]\d*)(?:\.\d{1,2})?)$/.exec(rate.trim());
  if (!match) return null;
  if (/^0(?:\.0+)?$/.test(match[2])) return '0 views/day';
  return `~${match[1] ? '-' : '+'}${new Intl.NumberFormat('en', { maximumFractionDigits: 2 }).format(Number(match[2]))} views/day`;
}

function observedPeriod(seconds: number | null, status: ChannelVideoViewGrowth['status']): string | null {
  if (seconds === null || !Number.isSafeInteger(seconds) || seconds <= 0) return null;
  // Presentation only; it never changes snapshot admission or status. Only a
  // full window reads "~28 days"; a partial one names its actual days.
  if (status === 'ready' && seconds >= 27 * 86400 && seconds <= 29 * 86400) return 'over ~28 days';
  if (seconds < 86400) return 'over <1 day';
  const days = Math.round(seconds / 86400 * 10) / 10;
  const approximate = seconds % 86400 === 0 ? '' : '~';
  return `over ${approximate}${days} ${days === 1 ? 'day' : 'days'}`;
}

/** The zone "today" was counted in: the shared evidence names it; old rows were UTC. */
function todayZone(growth: ChannelVideoViewGrowth & { time_zone?: string }): string {
  const zone = growth.time_zone;
  return !zone || zone === 'UTC' ? 'UTC' : zone;
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
  growth?: (ChannelVideoViewGrowth & { time_zone?: string }) | null,
): VideoGrowthPresentation {
  if (!growth || growth.status === 'unavailable') {
    return { text: 'Growth unavailable', tone: 'muted' };
  }
  if (growth.status === 'started_today') {
    return { text: 'Tracking started today', tone: 'muted', title: `Tracking started today (${todayZone(growth)})` };
  }
  if (growth.status === 'waiting_next_snapshot') {
    return { text: 'Waiting for next snapshot', tone: 'muted' };
  }

  const delta = signedCompactCount(growth.delta_views);
  const sign = numericSign(growth.delta_views);
  if (delta === null || sign === null) {
    return { text: 'Growth unavailable', tone: 'muted' };
  }
  const period = observedPeriod(growth.actual_interval_seconds, growth.status);
  if (period === null) return { text: 'Growth unavailable', tone: 'muted' };
  return {
    text: `${delta} ${period}`,
    tone: deltaTone(growth, sign),
  };
}
