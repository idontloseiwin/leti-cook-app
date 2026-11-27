// Numbers, dates and growth as the video card writes them, the way the YouTube site does:
// counts cut (not rounded) to K, M and B with one decimal below 10 of a unit and no ".0",
// growth with its sign and period, and short relative dates. Only the card uses these;
// the Inspector and the workshop keep formatCount and signedCompactCount.
// Pure TypeScript without runtime imports: no React, window or network.
import type { ChannelVideoViewGrowth, Primitive, VideoType } from '../types';

/** What the card shows for a value it has no number for (never 0). */
export const CARD_DASH = '—';
const MINUS = '−';
const DAY = 86_400;

const UNITS = [[9, 'B', 'billion'], [6, 'M', 'million'], [3, 'K', 'thousand']] as const;

/** A whole count as the rows carry it (safe integer or decimal string) → its sign and digits. */
function wholeCount(value: Primitive): { sign: -1 | 0 | 1; digits: string } | null {
  let text: string;
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) return null;
    text = String(value);
  } else if (typeof value === 'string') {
    text = value.trim();
  } else {
    return null;
  }
  const match = /^(-?)(\d+)$/.exec(text);
  if (!match) return null;
  const digits = match[2].replace(/^0+(?=\d)/, '');
  return { sign: digits === '0' ? 0 : match[1] ? -1 : 1, digits };
}

/** "1.2M" (or "1.2 million"): one decimal below 10 of the unit, cut, never ".0". */
function shortened(digits: string, words: boolean): string {
  for (const [power, letter, word] of UNITS) {
    if (digits.length <= power) continue;
    const whole = digits.slice(0, digits.length - power);
    const tenth = whole.length === 1 ? digits.charAt(1) : '0';
    const number = tenth === '0' ? whole : `${whole}.${tenth}`;
    return words ? `${number} ${word}` : `${number}${letter}`;
  }
  return digits;
}

function grouped(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 312 · 1.9K · 9.4K · 12K · 124K · 1.2M · 12M · 2.4B (999,950 → 999K); null when not a whole count. */
export function cardCount(value: Primitive): string | null {
  const count = wholeCount(value);
  return count ? shortened(count.digits, false) : null;
}

/** The same count for a screen reader: "1.2 million". */
export function cardCountWords(value: Primitive): string | null {
  const count = wholeCount(value);
  return count ? shortened(count.digits, true) : null;
}

/** The exact count for the hover text: "1,240,000" (without a sign). */
export function exactCount(value: Primitive): string | null {
  const count = wholeCount(value);
  return count ? grouped(count.digits) : null;
}

/** True for exactly one (so the noun is singular). */
export function isSingleCount(value: Primitive): boolean {
  return wholeCount(value)?.digits === '1';
}

export type CardGrowthTone = 'up' | 'down' | 'flat';
export type CardGrowth = {
  /** "+48K · 28d", or the status when there is no number. */
  text: string;
  /** By sign only: rise, fall, no change (statuses read as no change). */
  tone: CardGrowthTone;
  /** Hover text: the exact difference and period, or what the status means. */
  title: string;
  /** For the card's spoken label: "up 48 thousand over ~28 days"; absent at zero and for statuses. */
  words?: string;
};

/** The observed period: "28d" for a full window, the actual whole days otherwise, "<1d" under a day. */
function cardPeriod(seconds: number | null, status: ChannelVideoViewGrowth['status']): { short: string; long: string } | null {
  if (seconds === null || !Number.isSafeInteger(seconds) || seconds <= 0) return null;
  // Presentation only, as in the Inspector: only a full window reads 28 days; a partial one names its own days.
  if (status === 'ready' && seconds >= 27 * DAY && seconds <= 29 * DAY) return { short: '28d', long: '~28 days' };
  if (seconds < DAY) return { short: '<1d', long: 'less than a day' };
  const tenths = Math.round(seconds / DAY * 10) / 10;
  return { short: `${Math.floor(seconds / DAY)}d`, long: `${seconds % DAY ? '~' : ''}${tenths} ${tenths === 1 ? 'day' : 'days'}` };
}

function statusGrowth(text: string, title = text): CardGrowth {
  return { text, tone: 'flat', title };
}

/** "+48K · 28d" · "−1.2K · 28d" (a real minus) · "0 · 28d" · "+3.1K · 6d"; the status when there is no number. */
export function cardGrowth(growth?: (ChannelVideoViewGrowth & { time_zone?: string }) | null): CardGrowth {
  if (!growth || growth.status === 'unavailable') return statusGrowth('Growth unavailable');
  if (growth.status === 'started_today') {
    const zone = !growth.time_zone || growth.time_zone === 'UTC' ? 'UTC' : growth.time_zone;
    return statusGrowth('Tracking started today', `Tracking started today (${zone})`);
  }
  if (growth.status === 'waiting_next_snapshot') return statusGrowth('Waiting for next snapshot');
  const count = wholeCount(growth.delta_views);
  const period = cardPeriod(growth.actual_interval_seconds, growth.status);
  if (!count || !period) return statusGrowth('Growth unavailable');
  const sign = count.sign > 0 ? '+' : count.sign < 0 ? MINUS : '';
  return {
    text: `${sign}${shortened(count.digits, false)} · ${period.short}`,
    tone: count.sign > 0 ? 'up' : count.sign < 0 ? 'down' : 'flat',
    title: `${sign}${grouped(count.digits)} ${count.digits === '1' ? 'view' : 'views'} over ${period.long}`,
    ...(count.sign ? { words: `${count.sign > 0 ? 'up' : 'down'} ${shortened(count.digits, true)} over ${period.long}` } : {}),
  };
}

/**
 * Time since publication, counted from `now`: "just now", "42min ago", "5h ago", "13d ago",
 * then weeks from 14 days ("2w ago"), months from 30 days ("5mo ago"), years from 365 ("4y ago").
 * Null for an invalid instant or one more than a minute ahead (the caller shows the date).
 */
export function cardAge(value: string | null | undefined, now: number): string | null {
  if (!value) return null;
  const at = Date.parse(value);
  if (!Number.isFinite(at) || now - at < -60_000) return null;
  const minutes = Math.max(0, Math.floor((now - at) / 60_000));
  const days = Math.floor(minutes / 1440);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}min ago`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  if (days < 14) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

const longDate = new Intl.DateTimeFormat('en', { year: 'numeric', month: 'long', day: 'numeric' });

/** "November 23, 2026" in the viewer's time zone, for the spoken label; null for an invalid instant. */
export function cardDateWords(value: string | null | undefined): string | null {
  if (!value) return null;
  const at = Date.parse(value);
  return Number.isFinite(at) ? longDate.format(at) : null;
}

/** Seconds of a duration as the rows carry it (seconds, a decimal string or ISO "PT1H2M3S"). */
export function cardDurationSeconds(value: Primitive): number | null {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0 ? value : null;
  if (typeof value !== 'string') return null;
  const raw = value.trim();
  if (/^\d+$/.test(raw)) {
    const seconds = Number(raw);
    return Number.isSafeInteger(seconds) ? seconds : null;
  }
  const iso = /^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/.exec(raw);
  if (!iso || raw === 'PT') return null;
  return Number(iso[1] ?? 0) * 3600 + Number(iso[2] ?? 0) * 60 + Number(iso[3] ?? 0);
}

/** "0:28", "18:42", "1:02:10". */
export function cardDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const rest = String(seconds % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
}

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? '' : 's'}`;

/** "1 hour 2 minutes 10 seconds". */
export function cardDurationWords(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const rest = seconds % 60;
  return [hours && plural(hours, 'hour'), minutes && plural(minutes, 'minute'), rest && plural(rest, 'second')]
    .filter(Boolean).join(' ') || '0 seconds';
}

/**
 * The card's spoken summary: "Video: Title. 1.2 million views, up 48 thousand over ~28 days.
 * 18 minutes 42 seconds. Published November 23, 2026." A missing count names its state.
 */
export function videoCardLabel(input: {
  type?: VideoType;
  title: string;
  views: Primitive;
  /** Spoken state when there is no count: "needs refresh", "unavailable", "not reported". */
  viewsState?: string;
  growthWords?: string;
  durationSeconds: number | null;
  published?: string | null;
}): string {
  const kind = input.type === 'short' ? 'Short' : input.type === 'stream' ? 'Stream' : 'Video';
  const parts = [`${kind}: ${input.title}.`];
  const views = cardCountWords(input.views);
  parts.push(views === null
    ? `Views: ${input.viewsState ?? 'not reported'}.`
    : `${views} ${isSingleCount(input.views) ? 'view' : 'views'}${input.growthWords ? `, ${input.growthWords}` : ''}.`);
  if (input.durationSeconds) parts.push(`${cardDurationWords(input.durationSeconds)}.`);
  if (input.published) parts.push(`Published ${input.published}.`);
  return parts.join(' ');
}
