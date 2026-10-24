/** Transient source lifetimes. Symbols survive spread, but never JSON/storage. */
const SOURCE_LIFETIME = Symbol('sourceLifetime');
const SOURCE_CLOCK = Symbol('sourceClock');
const SOURCE_CONTAINER = Symbol('sourceContainer');
type Lifetime = Readonly<{ expiresAt: number; monotonicDeadline: number }>;
type Clock = Readonly<{ readAt: number; requestStarted: number }>;
type Tagged = { [SOURCE_LIFETIME]?: Lifetime; [SOURCE_CLOCK]?: Clock };
export const SOURCE_EXPIRED_MESSAGE = 'Current data could not be refreshed. Reload to try again.';

function earlier(left: Lifetime | undefined, right: Lifetime | undefined): Lifetime | undefined {
  if (!left) return right;
  if (!right) return left;
  return { expiresAt: Math.min(left.expiresAt, right.expiresAt), monotonicDeadline: Math.min(left.monotonicDeadline, right.monotonicDeadline) };
}
function instant(value: unknown): number {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return NaN;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 19) === value.slice(0, 19) ? parsed : NaN;
}
function values(item: object): unknown[] {
  return item instanceof Map || item instanceof Set ? [...item.values()] : Object.values(item);
}
export function ownSourceLifetime(value: unknown): Lifetime | undefined {
  return value && typeof value === 'object' ? (value as Tagged)[SOURCE_LIFETIME] : undefined;
}
/** Earliest deadline is a refresh trigger, not a cap on independent rows. */
export function sourceLifetime(value: unknown): Lifetime | undefined {
  const seen = new WeakSet<object>();
  const visit = (item: unknown): Lifetime | undefined => {
    if (!item || typeof item !== 'object' || seen.has(item)) return undefined;
    seen.add(item);
    let result = ownSourceLifetime(item);
    for (const nested of values(item)) result = earlier(result, visit(nested));
    return result;
  };
  return visit(value);
}
function tag(item: object, lifetime: Lifetime | undefined, clock?: Clock): void {
  if (lifetime) Object.defineProperty(item, SOURCE_LIFETIME, { value: lifetime, enumerable: true, configurable: true });
  else delete (item as Tagged)[SOURCE_LIFETIME];
  if (clock) Object.defineProperty(item, SOURCE_CLOCK, { value: clock, enumerable: true, configurable: true });
}
function attach<T>(value: T, inherited: Lifetime | undefined, clock?: Clock): T {
  const seen = new WeakSet<object>();
  const visit = (item: unknown, fallback: Lifetime | undefined, root = false) => {
    if (!item || typeof item !== 'object' || seen.has(item)) return;
    seen.add(item);
    let lifetime = ownSourceLifetime(item) ?? fallback;
    if (Object.hasOwn(item, 'source_expires_at')) {
      const raw = (item as { source_expires_at: unknown }).source_expires_at;
      // Only a source-free envelope may omit its proof. A row with no proof
      // cannot use null to exempt provider fields from the inherited deadline.
      if (raw === null) lifetime = fallback ?? (root ? undefined : { expiresAt: 0, monotonicDeadline: 0 });
      else {
        const expiresAt = instant(raw);
        lifetime = clock && Number.isFinite(expiresAt) && Number.isFinite(clock.readAt)
          ? { expiresAt, monotonicDeadline: clock.requestStarted + expiresAt - clock.readAt }
          : { expiresAt: 0, monotonicDeadline: 0 };
      }
    }
    tag(item, lifetime, clock);
    for (const nested of values(item)) visit(nested, lifetime);
  };
  visit(value, inherited, true); return value;
}
export function captureSourceLifetime<T>(value: T, headers: Pick<Headers, 'get'> | undefined, requestStarted: number): T {
  const body = value && typeof value === 'object' ? value as Record<string, unknown> : undefined;
  const expiry = body && Object.hasOwn(body, 'source_expires_at') ? body.source_expires_at : headers?.get('X-Source-Expires-At');
  const rawRead = body?.source_read_at ?? headers?.get('X-Source-Read-At');
  if (expiry === undefined || expiry === null && !rawRead) return value;
  let readAt = instant(rawRead);
  const expiresAt = instant(expiry);
  const headerExpiry = headers?.get('X-Source-Expires-At'), headerRead = headers?.get('X-Source-Read-At');
  if (headerExpiry && (instant(headerExpiry) !== expiresAt || headerRead && instant(headerRead) !== readAt)) readAt = NaN;
  const clock = { readAt, requestStarted };
  const valid = Number.isFinite(expiresAt) && Number.isFinite(readAt);
  return attach(value, expiry === null ? undefined : { expiresAt: valid ? expiresAt : 0,
    monotonicDeadline: valid ? requestStarted + expiresAt - readAt : 0 }, clock);
}
/** Transfer the acquisition clock, never a newly calculated lifetime. */
export function transferSourceLifetime<T>(source: unknown, target: T): T {
  const clock = source && typeof source === 'object' ? (source as Tagged)[SOURCE_CLOCK] : undefined;
  const copy = (from: unknown, to: unknown) => {
    if (!from || !to || typeof from !== 'object' || typeof to !== 'object') return;
    tag(to, ownSourceLifetime(from), (from as Tagged)[SOURCE_CLOCK] ?? clock);
    for (const [key, child] of Object.entries(to)) copy((from as Record<string, unknown>)[key], child);
  };
  copy(source, target);
  return attach(target, ownSourceLifetime(source) ?? sourceLifetime(source), clock);
}
export function sourceLifetimeParser<A extends unknown[], T>(parser: (value: unknown, ...args: A) => T) {
  return (value: unknown, ...args: A): T => transferSourceLifetime(value, parser(value, ...args));
}
export function sourceExpiryDelay(value: unknown, now = Date.now(), monotonic = performance.now()): number | undefined {
  const lifetime = sourceLifetime(value);
  return lifetime ? Math.min(lifetime.expiresAt - now, lifetime.monotonicDeadline - monotonic) : undefined;
}
export function sourceDeadlines(value: unknown): Lifetime[] {
  const seen = new WeakSet<object>(), found = new Map<string, Lifetime>();
  const visit = (item: unknown) => {
    if (!item || typeof item !== 'object' || seen.has(item)) return;
    seen.add(item);
    const lifetime = ownSourceLifetime(item);
    if (lifetime) found.set(`${lifetime.expiresAt}:${lifetime.monotonicDeadline}`, lifetime);
    for (const nested of values(item)) visit(nested);
  };
  visit(value); return [...found.values()];
}
export function sourceExpired(value: unknown, now = Date.now(), monotonic = performance.now()): boolean {
  if (value && typeof value === 'object' && (value as { [SOURCE_CONTAINER]?: boolean })[SOURCE_CONTAINER]) return false;
  return sourceOwnExpired(value, now, monotonic);
}
export function sourceOwnExpired(value: unknown, now = Date.now(), monotonic = performance.now()): boolean {
  const lifetime = ownSourceLifetime(value) ?? sourceLifetime(value);
  return Boolean(lifetime && (now >= lifetime.expiresAt || monotonic >= lifetime.monotonicDeadline));
}
/** Presentation container only; callers must project/guard every source child. */
export function sourceContainer<T extends object>(value: T): T {
  Object.defineProperty(value, SOURCE_CONTAINER, { value: true, enumerable: true }); return value;
}
export function freshSourceRows<T>(rows: readonly T[], now = Date.now(), monotonic = performance.now()): T[] {
  return rows.filter(row => !sourceExpired(row, now, monotonic));
}
/** Only for an explicit user-only projection; never use to refresh API data. */
export function withoutSourceLifetime<T>(value: T): T {
  if (Array.isArray(value)) return value.map(withoutSourceLifetime) as T;
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, withoutSourceLifetime(item)])) as T;
  return value;
}
