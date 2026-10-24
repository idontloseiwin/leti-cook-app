import { useEffect, useRef } from 'react';
import { sourceDeadlines } from './sourceLifetime.ts';

/** One bounded timer per resource; expire first, then re-read only visible data. */
export function useSourceExpiry(value: unknown, onExpired: () => void, onRefresh?: () => void): void {
  const callbacks = useRef({ onExpired, onRefresh }); callbacks.current = { onExpired, onRefresh };
  const reported = useRef(new Set<string>());
  const lastRefresh = useRef(-Infinity);
  useEffect(() => {
    const deadlines = sourceDeadlines(value);
    const keys = new Set(deadlines.map(item => `${item.expiresAt}:${item.monotonicDeadline}`));
    reported.current = new Set([...reported.current].filter(key => keys.has(key)));
    let timer: ReturnType<typeof setTimeout> | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    const refresh = () => {
      if (disposed || document.hidden || !callbacks.current.onRefresh) return;
      const remaining = 5_000 - (performance.now() - lastRefresh.current);
      if (remaining > 0) {
        if (refreshTimer === undefined) refreshTimer = setTimeout(() => { refreshTimer = undefined; refresh(); }, remaining);
        return;
      }
      lastRefresh.current = performance.now(); callbacks.current.onRefresh();
    };
    const check = (visible = false) => {
      if (disposed) return;
      if (timer !== undefined) clearTimeout(timer);
      const now = Date.now(), monotonic = performance.now();
      let expired = false, next = Infinity;
      for (const item of deadlines) {
        const key = `${item.expiresAt}:${item.monotonicDeadline}`;
        const delay = Math.min(item.expiresAt - now, item.monotonicDeadline - monotonic);
        if (delay <= 0) {
          if (!reported.current.has(key)) { reported.current.add(key); expired = true; }
        } else next = Math.min(next, delay);
      }
      if (expired) callbacks.current.onExpired();
      if (expired || visible) refresh();
      if (Number.isFinite(next)) timer = setTimeout(() => check(), Math.min(2147483647, Math.max(1, next + 1)));
    };
    const visible = () => { if (!document.hidden) check(true); };
    check();
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('pageshow', visible);
    return () => {
      disposed = true;
      if (timer !== undefined) clearTimeout(timer);
      if (refreshTimer !== undefined) clearTimeout(refreshTimer);
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('pageshow', visible);
    };
  }, [value]);
}
