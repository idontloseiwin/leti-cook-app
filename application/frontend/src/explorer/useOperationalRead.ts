import { useSourceExpiry } from '../sourceLifetimeReact';
import { expireResourceState } from './resourceState';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ResourceState } from './model';
import { beginResourceLoad, completeResourceLoad, failResourceLoad, resourcePayload } from './resourceState';

export const OPERATIONAL_READ_TIMEOUT_MS = 30_000;

type ReadOptions = { intervalMs: number; errorMessage: string; enabled?: boolean };
type ReadSnapshot<T> = {
  resource: ResourceState<T>;
  loader?: (signal: AbortSignal) => Promise<T>;
  updatedAt?: number;
  error?: string;
};

export type OperationalRead<T> = {
  payload?: T;
  loading: boolean;
  refreshing: boolean;
  error?: string;
  updatedAt?: number;
  refresh: () => void;
};

/** A single, cancellable operational read loop. Pass a stable loader function. */
export function useOperationalRead<T>(
  load: (signal: AbortSignal) => Promise<T>,
  { intervalMs, errorMessage, enabled = true }: ReadOptions,
): OperationalRead<T> {
  const [snapshot, setSnapshot] = useState<ReadSnapshot<T>>({ resource: { kind: 'idle' } });
  const refreshRef = useRef<() => void>(() => {});
  useSourceExpiry(snapshot.resource, () => setSnapshot((previous) => ({ ...previous, resource: expireResourceState(previous.resource), error: undefined })), () => refreshRef.current());
  const generation = useRef(0);
  const refresh = useCallback(() => refreshRef.current(), []);

  useEffect(() => {
    const ticket = ++generation.current;
    const scope = `operational:${ticket}`;
    let disposed = false;
    let request: AbortController | undefined;
    let nextTimer: ReturnType<typeof setTimeout> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const current = () => !disposed && generation.current === ticket;
    const delay = Number.isFinite(intervalMs) ? Math.max(1_000, intervalMs) : 30_000;

    setSnapshot({ resource: { kind: 'idle' }, loader: load });
    if (!enabled) {
      refreshRef.current = () => {};
      return () => { disposed = true; };
    }

    const run = async () => {
      if (!current() || request) return;
      if (nextTimer !== undefined) clearTimeout(nextTimer);
      nextTimer = undefined;
      const controller = new AbortController();
      request = controller;
      setSnapshot((previous) => ({ ...previous, loader: load,
        resource: beginResourceLoad(previous.resource, scope) }));
      // Reject even if a custom loader ignores cancellation. A late response
      // can never publish after its deadline, unmount or a replacement loader.
      let removeAbort = () => {};
      const aborted = new Promise<never>((_resolve, reject) => {
        const onAbort = () => reject(new Error('Operational read cancelled'));
        controller.signal.addEventListener('abort', onAbort, { once: true });
        removeAbort = () => controller.signal.removeEventListener('abort', onAbort);
      });
      deadline = setTimeout(() => controller.abort(), OPERATIONAL_READ_TIMEOUT_MS);
      try {
        const pending = Promise.resolve().then(() => {
          if (!current() || controller.signal.aborted) throw new Error('Operational read cancelled');
          return load(controller.signal);
        });
        const payload = await Promise.race([pending, aborted]);
        if (!current() || controller.signal.aborted) return;
        setSnapshot({ resource: completeResourceLoad(scope, payload), loader: load, updatedAt: Date.now() });
      } catch {
        if (!current()) return;
        // Do not expose transport, database or arbitrary error strings.
        setSnapshot((previous) => ({ ...previous, error: errorMessage,
          resource: failResourceLoad(previous.resource, scope, errorMessage) }));
      } finally {
        removeAbort();
        if (deadline !== undefined) clearTimeout(deadline);
        deadline = undefined;
        request = undefined;
        if (current()) nextTimer = setTimeout(() => void run(), delay);
      }
    };
    refreshRef.current = () => { void run(); };
    void run();
    return () => {
      disposed = true;
      refreshRef.current = () => {};
      if (nextTimer !== undefined) clearTimeout(nextTimer);
      if (deadline !== undefined) clearTimeout(deadline);
      request?.abort();
    };
  }, [load, enabled, intervalMs, errorMessage]);

  const visible = enabled && snapshot.loader === load ? snapshot : undefined;
  return {
    payload: visible ? resourcePayload(visible.resource) : undefined,
    loading: enabled && (!visible || visible.resource.kind === 'idle' || visible.resource.kind === 'loading'),
    refreshing: visible?.resource.kind === 'updating',
    error: visible?.error,
    updatedAt: visible?.updatedAt,
    refresh,
  };
}
