import type { SidebarData } from '../types';

/**
 * The channel list's reads. One read runs at a time; a read asked for while
 * one runs is not dropped: exactly one more read follows the running one. A local
 * change (a channel added, a folder or label created or assigned) makes the running
 * read stale: its answer, which may predate the change, is not shown, and the read
 * that follows brings the server's list with the change.
 */
export type SidebarReader = { load(): Promise<void>; changedLocally(): void };

export function createSidebarReader(steps: {
  /** The sidebar the page shows now (full, or local work only while YouTube reads are paused). */
  read: () => Promise<SidebarData>;
  /** Local identities and organization, when the first read failed. */
  fallback: () => Promise<SidebarData>;
  show: (payload: SidebarData) => void;
  fail: (message: string) => void;
}): SidebarReader {
  let running = false, again = false, generation = 0;
  const load = async (): Promise<void> => {
    if (running) { again = true; return; }
    running = true;
    const ticket = ++generation;
    const current = () => ticket === generation;
    try {
      let failure: unknown;
      try {
        const payload = await steps.read();
        if (current()) steps.show(payload);
        return;
      } catch (error) { failure = error; }
      if (!current()) return;
      try {
        const payload = await steps.fallback();
        if (current()) steps.show(payload);
        return;
      } catch { /* Keep the original read error if local work is also unavailable. */ }
      if (current()) steps.fail(failure instanceof Error ? failure.message : String(failure));
    } finally {
      running = false;
      if (again) { again = false; void load(); }
    }
  };
  return {
    load,
    changedLocally() {
      generation += 1;
      if (running) again = true;
    },
  };
}
