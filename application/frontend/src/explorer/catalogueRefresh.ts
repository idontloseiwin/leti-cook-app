// Background update of the loaded Videos cards: how often the loaded rows are read again, which rows go
// first and when to stop. The reads go to the program's own storage (`page.ids`), never to YouTube.
// Pure TypeScript: no React and no network; the browser clocks and timers are behind `RefreshEnvironment`.

/**
 * A pass over the loaded cards starts this long after the previous one finished. The collector brings new
 * numbers about every 10 minutes, so a card on screen is at most one collector pass behind.
 */
export const REFRESH_PERIOD_MS = 5 * 60_000;
/**
 * Back on the tab, or a loaded value has just ended: one read now (those rows and the cards around the viewed
 * one), unless the last read is this recent. The full pass keeps its period. Nothing starts sooner than this
 * after the previous start either, also not a retry after a failure: a pass that a list request cancelled
 * (the user is scrolling and pages load) goes on a minute later with the rows it has not read, not at once
 * from its beginning.
 */
export const REFRESH_SOON_MS = 60_000;
/**
 * A pass also reads first the rows with a value that ends before the next pass can reach them, so a value
 * rarely ends on screen (28-day growth ends about daily, each card at its own time).
 */
export const REFRESH_AHEAD_MS = REFRESH_PERIOD_MS + 60_000;
/** Videos per read: the server's limit for `page.ids`. One read is one pass over the channel, whatever its size. */
export const REFRESH_CHUNK = 200;
/**
 * Request time one pass may spend (it always makes its first two reads). A list too long for it is read in
 * turns: the cards around the viewed one in every pass, the others least recently read first.
 */
export const REFRESH_BUDGET_MS = 6_000;
/** How often the page checks whether a pass is due. The check compares clocks, so it also notices a computer that slept. */
export const REFRESH_TICK_MS = 15_000;
/** After a failed pass: the waits before the next attempts. After the last one the updates stop until the list is read again. */
export const REFRESH_RETRY_MS: readonly number[] = [5 * 60_000, 10 * 60_000, 20 * 60_000, 30 * 60_000];
/** The pause between two reads of one pass is the time the first took, within these bounds: other reads get their turn. */
export const REFRESH_PAUSE_MS = { min: 250, max: 2_000 } as const;

/** Wall and monotonic time together: a computer that sleeps stops one of them, a changed clock moves the other. */
export type RefreshClock = { now: number; monotonic: number };
export type RefreshMemory = {
  /** When the last pass or single read finished or failed (for a new list: when it was read). */
  last: RefreshClock;
  /** When the last full pass finished (for a new list: when it was read). */
  lastFull: RefreshClock;
  /** When the last pass or single read started, finished or not (for a new list: when it was read). */
  started: RefreshClock;
  /** Failed passes in a row. */
  failures: number;
  /** No further automatic passes for this read of the list. */
  paused: boolean;
};
/** Why the check runs: the regular tick, the tab shown again, or a loaded value that has ended. */
export type RefreshReason = 'tick' | 'visible' | 'expiry';

/** Time since `last` on whichever clock moved further. */
export function refreshElapsed(last: RefreshClock, clock: RefreshClock): number {
  return Math.max(clock.now - last.now, clock.monotonic - last.monotonic);
}

/** What starts now: a full pass, one read (`soon`), or nothing. */
export function refreshDue(memory: RefreshMemory, clock: RefreshClock, reason: RefreshReason): 'full' | 'soon' | null {
  if (memory.paused) return null;
  if (refreshElapsed(memory.started, clock) < REFRESH_SOON_MS) return null;
  const elapsed = refreshElapsed(memory.last, clock);
  if (memory.failures > 0) return elapsed >= REFRESH_RETRY_MS[Math.min(memory.failures, REFRESH_RETRY_MS.length) - 1] ? 'full' : null;
  if (refreshElapsed(memory.lastFull, clock) >= REFRESH_PERIOD_MS) return 'full';
  return reason !== 'tick' && elapsed >= REFRESH_SOON_MS ? 'soon' : null;
}

/** The state after a failed pass: one more failure; past the last retry the updates pause. */
export function refreshFailed(memory: RefreshMemory, clock: RefreshClock, final = false): RefreshMemory {
  const failures = memory.failures + 1;
  return { last: clock, lastFull: memory.lastFull, started: memory.started, failures, paused: final || failures > REFRESH_RETRY_MS.length };
}

/** Minutes until the next attempt after `failures` failed passes (for the notice). */
export function refreshRetryMinutes(failures: number): number {
  return Math.round(REFRESH_RETRY_MS[Math.min(Math.max(failures, 1), REFRESH_RETRY_MS.length) - 1] / 60_000);
}

/**
 * The loaded IDs in the order a pass reads them. The first read takes the rows with a value that has ended
 * or ends before the next pass (`urgent`) and the cards around the viewed one (`near`), nearest first. The
 * following reads take all the other rows, least recently read first, so a long list is read in turns and
 * no row waits forever. `readAt`: when each row was last read (monotonic); a row without a time is the oldest.
 * `now` (monotonic, a full pass gives it): a card around the viewed one that was read less than a period ago
 * has had its turn and waits with the others. So a pass that was cancelled after its first read goes on with
 * the rows read longest ago, not with the same cards, and the cards around the viewed one are still read
 * once a period.
 */
export function refreshOrder(ids: readonly string[], options: {
  near?: string; urgent?: ReadonlySet<string>; readAt?: ReadonlyMap<string, number>; now?: number;
} = {}, first = REFRESH_CHUNK): string[] {
  const viewed = options.near === undefined ? -1 : ids.indexOf(options.near);
  const centre = viewed < 0 ? 0 : viewed;
  const now = options.now;
  const rows = ids.map((id, index) => {
    const at = options.readAt?.get(id) ?? -Infinity;
    return { id, index, distance: Math.abs(index - centre), urgent: options.urgent?.has(id) ? 0 : 1, at,
      recent: now !== undefined && at <= now && now - at < REFRESH_PERIOD_MS };
  });
  const nearest = (left: typeof rows[number], right: typeof rows[number]) => left.urgent - right.urgent
    || left.distance - right.distance || left.index - right.index;
  rows.sort(nearest);
  const head = rows.slice(0, first).filter((row) => row.urgent === 0 || !row.recent);
  const taken = new Set(head);
  const rest = rows.filter((row) => !taken.has(row)).sort((left, right) => left.urgent - right.urgent
    || (left.at === right.at ? 0 : left.at < right.at ? -1 : 1) || nearest(left, right));
  return [...head, ...rest].map((row) => row.id);
}

/** Consecutive reads of at most `size` IDs. */
export function refreshChunks(ids: readonly string[], size = REFRESH_CHUNK): string[][] {
  const chunks: string[][] = [];
  for (let start = 0; start < ids.length; start += size) chunks.push(ids.slice(start, start + size));
  return chunks;
}

/** The pause after a read that took `took` milliseconds. */
export function refreshPause(took: number): number {
  return Math.min(REFRESH_PAUSE_MS.max, Math.max(REFRESH_PAUSE_MS.min, Math.round(took)));
}

/** What the notice says after a failed pass. The cards themselves never change because of a failure. */
export function refreshProblemText(memory: RefreshMemory): string {
  return memory.paused
    ? 'The numbers on these cards are no longer updated in the background: the updates failed. The cards keep their last values. Use Refresh order to read the list again.'
    : `The numbers on these cards could not be updated in the background. The cards keep their last values; the next attempt is in ${refreshRetryMinutes(memory.failures)} minutes.`;
}

/** The clocks, the tab's visibility and the timers the update runs on (the browser's, or a test's). */
export type RefreshEnvironment = {
  now(): number;
  monotonic(): number;
  /** The tab is shown: only then anything is read. */
  visible(): boolean;
  /** Call `check` every `everyMs` and when the tab is shown again (`shown` true); returns the stop function. */
  watch(check: (shown: boolean) => void, everyMs: number): () => void;
  /** Call `run` once after `ms`; returns the cancel function. */
  after(ms: number, run: () => void): () => void;
};

/** setTimeout's longest delay; a longer wait is re-armed by the caller. */
const LONGEST_TIMER_MS = 2_147_483_647;

export const browserRefreshEnvironment: RefreshEnvironment = {
  now: () => Date.now(),
  monotonic: () => performance.now(),
  visible: () => typeof document === 'undefined' || document.visibilityState === 'visible',
  watch(check, everyMs) {
    const timer = setInterval(() => check(false), everyMs);
    const shown = () => { if (document.visibilityState === 'visible') check(true); };
    document.addEventListener('visibilitychange', shown);
    window.addEventListener('pageshow', shown);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', shown);
      window.removeEventListener('pageshow', shown);
    };
  },
  after(ms, run) {
    const timer = setTimeout(run, Math.min(LONGEST_TIMER_MS, Math.max(0, ms)));
    return () => clearTimeout(timer);
  },
};
