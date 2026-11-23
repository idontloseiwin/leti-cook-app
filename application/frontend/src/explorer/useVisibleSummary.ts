import { useCallback, useEffect, useRef, useState } from 'react';
import { SOURCE_EXPIRED_MESSAGE, sourceExpired } from '../sourceLifetime';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { createVisibleRead } from './visibleRead';

export type SummaryState<T> =
  | { kind: 'loading' }
  | { kind: 'ready'; payload: T }
  | { kind: 'error'; message: string };

const complete = () => false;

/** Home summaries are fetched only while Home is visible, with bounded recovery. */
export function useVisibleSummary<T>(
  load: (signal?: AbortSignal) => Promise<T>,
  enabled: boolean,
  incomplete: (payload: T) => boolean = complete,
) {
  const [state, setState] = useState<SummaryState<T>>({ kind: 'loading' });
  const refreshRef = useRef(() => {});
  const refresh = useCallback(() => refreshRef.current(), []);
  useSourceExpiry(state, () => setState({ kind: 'loading' }), refresh);
  useEffect(() => {
    setState({ kind: 'loading' });
    if (!enabled) return;
    const reader = createVisibleRead({
      visible: () => document.visibilityState === 'visible',
      load: async (signal) => {
        const payload = await load(signal);
        if (sourceExpired(payload)) throw new Error(SOURCE_EXPIRED_MESSAGE);
        return payload;
      },
      started: () => setState((current) => current.kind === 'ready' && !sourceExpired(current.payload)
        ? current : { kind: 'loading' }),
      received: (payload) => setState({ kind: 'ready', payload }),
      failed: (error) => setState({ kind: 'error', message: error instanceof Error
        ? error.message : 'Current data could not be loaded. Try again.' }),
      incomplete,
    });
    const onVisibility = () => reader.visibilityChanged();
    refreshRef.current = () => reader.refresh();
    document.addEventListener('visibilitychange', onVisibility);
    reader.refresh();
    return () => {
      refreshRef.current = () => {};
      document.removeEventListener('visibilitychange', onVisibility);
      reader.stop();
    };
  }, [load, enabled, incomplete]);
  // Rendering must also guard the original acquisition deadline.
  const visibleState: SummaryState<T> = !enabled || state.kind === 'ready' && sourceExpired(state.payload)
    ? { kind: 'loading' } : state;
  return { state: visibleState, refresh };
}
