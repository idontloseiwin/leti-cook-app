import type { SidebarData } from '../types';

/**
 * The channel list's reads. One read runs at a time; a read asked for while
 * one runs is not dropped: exactly one more read follows the running one. A local
 * change (a channel added, a folder or label created or assigned) makes the running
 * read stale: its answer, which may predate the change, is not shown, and the read
 * that follows brings the server's list with the change.
 */
export type SidebarReader = { load(): Promise<boolean>; changedLocally(): void };

export function createSidebarReader(steps: {
  /** The sidebar the page shows now (full, or local work only while YouTube reads are paused). */
  read: () => Promise<SidebarData>;
  /** Local identities and organization, when the first read failed. */
  fallback: () => Promise<SidebarData>;
  show: (payload: SidebarData) => void;
  fail: (message: string) => void;
}): SidebarReader {
  let running: Promise<boolean> | undefined;
  let again = false, generation = 0;
  const readOnce = async (): Promise<boolean> => {
    const ticket = ++generation;
    const current = () => ticket === generation;
    let failure: unknown;
    try {
      const payload = await steps.read();
      if (current()) steps.show(payload);
      return current();
    } catch (error) { failure = error; }
    if (!current()) return false;
    try {
      const payload = await steps.fallback();
      if (current()) steps.show(payload);
      return current();
    } catch { /* Keep the original read error if local work is also unavailable. */ }
    if (current()) steps.fail(failure instanceof Error ? failure.message : String(failure));
    return false;
  };
  const drain = async () => {
    try {
      let shown;
      do {
        again = false;
        shown = await readOnce();
      } while (again);
      return shown;
    } finally { running = undefined; }
  };
  return {
    load() {
      if (running) again = true;
      else running = drain();
      return running;
    },
    changedLocally() {
      generation += 1;
      if (running) again = true;
    },
  };
}

export class UnconfirmedOrganizationChange extends Error {
  constructor(error: unknown, refreshed: boolean) {
    const reason = error instanceof Error && (error.name === 'UnreadableAnswer' || error.name === 'ExplorerNetworkError')
      ? error.message + ' '
      : error instanceof Error && 'code' in error && error.code === 'client_timeout' ? 'The request timed out. ' : '';
    const next = refreshed ? 'Check the current folders and labels before trying again.'
      : 'The folders and labels could not be reloaded. Reload Library before trying again.';
    super(reason + 'The change could not be confirmed. ' + next, { cause: error });
  }
}

/** A write with a missing receipt must never be submitted again by recovery. */
export async function organizationWrite<T>(write: () => Promise<T>, refresh: () => Promise<boolean>): Promise<T> {
  try { return await write(); }
  catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error;
    const status = error && typeof error === 'object' && 'status' in error ? Number(error.status) : undefined;
    if (status !== undefined && status !== 408 && status < 500) throw error;
    const refreshed = await refresh();
    throw new UnconfirmedOrganizationChange(error, refreshed);
  }
}
