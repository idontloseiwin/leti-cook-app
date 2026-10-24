// Pure time-zone arithmetic for publication analysis; no React or storage.
export const TIME_ZONE_PATTERN = /^(?:UTC|[A-Za-z]+(?:\/[A-Za-z0-9_+-]+){1,2})$/;

/** Short label for headings: "UTC" or "local time". */
export function timeZoneLabel(zone: string): string {
  return zone === 'UTC' ? 'UTC' : 'local time';
}

// Offsets change only on whole hours in practice, so one lookup per hour and
// zone keeps large catalogues fast.
const offsetFormatters = new Map<string, Intl.DateTimeFormat>();
const offsetCache = new Map<string, Map<number, number>>();
const HOUR_MS = 3_600_000;

function offsetAt(epochMs: number, zone: string): number {
  let formatter = offsetFormatters.get(zone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: zone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', second: 'numeric',
    });
    offsetFormatters.set(zone, formatter);
  }
  const part: Record<string, number> = {};
  for (const { type, value } of formatter.formatToParts(new Date(epochMs))) part[type] = Number(value);
  const wall = Date.UTC(part.year, part.month - 1, part.day, part.hour % 24, part.minute, part.second);
  return wall - Math.floor(epochMs / 1000) * 1000;
}

export function zoneOffsetMs(epochMs: number, zone: string): number {
  if (zone === 'UTC') return 0;
  let cache = offsetCache.get(zone);
  if (!cache) { cache = new Map(); offsetCache.set(zone, cache); }
  const hour = Math.floor(epochMs / HOUR_MS);
  let offset = cache.get(hour);
  if (offset === undefined) {
    offset = offsetAt(hour * HOUR_MS, zone);
    if (cache.size > 200_000) cache.clear();
    cache.set(hour, offset);
  }
  return offset;
}

/** Wall-clock parts of a moment in a zone; weekday 0 is Monday. */
export function zonedParts(epochMs: number, zone: string) {
  const date = new Date(epochMs + zoneOffsetMs(epochMs, zone));
  return {
    year: date.getUTCFullYear(),
    month: date.getUTCMonth(),
    day: date.getUTCDate(),
    weekday: (date.getUTCDay() + 6) % 7,
    hour: date.getUTCHours(),
    minute: date.getUTCMinutes(),
  };
}

/** The moment a calendar day starts in a zone (month is 0-based). */
export function zonedDayStart(year: number, month: number, day: number, zone: string): number {
  const wall = Date.UTC(year, month, day);
  const first = wall - zoneOffsetMs(wall, zone);
  return wall - zoneOffsetMs(first, zone);
}
