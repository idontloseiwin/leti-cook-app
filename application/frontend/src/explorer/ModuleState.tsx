import {
  AlertTriangle,
  RefreshCw,
  ServerOff,
} from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';

export function LoadingState({
  label,
  compact = false,
  hidden = false,
}: {
  label: string;
  compact?: boolean;
  /** Keeps the row's box without showing or announcing it (DelayedLoadingState). */
  hidden?: boolean;
}) {
  return (
    <div
      className={`explorer-module-state explorer-v2-status-row is-loading ${compact ? 'is-compact' : ''}`}
      {...(hidden ? { 'aria-hidden': true, style: { visibility: 'hidden' as const } } : { role: 'status' })}
    >
      <RefreshCw className="explorer-spin explorer-module-state-icon" size={20} aria-hidden="true" />
      <span className="explorer-module-state-copy">{label}</span>
    </div>
  );
}

/** A normal load replaces the placeholder before people can read it (≤ 0.2 s): wait this long. */
export const LOADING_TEXT_DELAY_MS = 300;

/**
 * A loading row that appears only when loading lasts longer than `delayMs`. Until then its box is kept, so a
 * normal load shows no flash and the layout does not move; a slow (cold) load shows the usual text.
 */
export function DelayedLoadingState({ label, delayMs = LOADING_TEXT_DELAY_MS }: { label: string; delayMs?: number }) {
  const [shown, setShown] = useState(delayMs <= 0);
  useEffect(() => {
    if (shown) return undefined;
    const timer = window.setTimeout(() => setShown(true), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, shown]);
  return <LoadingState label={label} hidden={!shown} />;
}

export function ErrorState({
  title,
  message,
  onRetry,
  children,
}: {
  title: string;
  message: string;
  onRetry?: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="explorer-module-state explorer-v2-status-row is-error" role="alert">
      <AlertTriangle className="explorer-module-state-icon" size={20} aria-hidden="true" />
      <div className="explorer-module-state-copy">
        <strong>{title}</strong>
        <span>{message}</span>
        {children}
      </div>
      {onRetry && (
        <button className="explorer-module-state-action" type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function UnavailableState({
  title,
  message,
  onRetryDiscovery,
}: {
  title: string;
  message: string;
  onRetryDiscovery?: () => void;
}) {
  return (
    <div
      className="explorer-module-state explorer-v2-status-row is-unavailable"
      role="status"
    >
      <ServerOff className="explorer-module-state-icon" size={20} aria-hidden="true" />
      <div className="explorer-module-state-copy">
        <strong>{title}</strong>
        <span>{message}</span>
      </div>
      {onRetryDiscovery && (
        <button className="explorer-module-state-action" type="button" onClick={onRetryDiscovery}>
          Check availability again
        </button>
      )}
    </div>
  );
}

/**
 * A part of the page shipped in its own chunk (the main chunk stays
 * under Vite's 500 kB). Its import starts once (`start`, safe to call early as a
 * prefetch); `useDeferredModule` renders it as soon as it is loaded, on the very
 * first render when the prefetch already finished, so a loaded part never flashes.
 */
export type DeferredModule<T> = { start: () => Promise<T>; current: () => T | null; failed: () => boolean };

export function deferredModule<T>(load: () => Promise<T>): DeferredModule<T> {
  let value: T | null = null;
  let failure = false;
  let pending: Promise<T> | null = null;
  return {
    start: () => (pending ??= load().then((module) => { value = module; return module; },
      (error: unknown) => { failure = true; throw error; })),
    current: () => value,
    failed: () => failure,
  };
}

/** The deferred module once loaded (null while loading); `failed` when its chunk could not load. */
export function useDeferredModule<T>(deferred: DeferredModule<T>, wanted = true): { module: T | null; failed: boolean } {
  const [, repaint] = useState(0);
  const module = deferred.current();
  useEffect(() => {
    if (module || !wanted) return undefined;
    let active = true;
    const settle = () => { if (active) repaint((value) => value + 1); };
    deferred.start().then(settle, settle);
    return () => { active = false; };
  }, [deferred, module, wanted]);
  return { module, failed: !module && deferred.failed() };
}

/** What a deferred part shows when its chunk could not load (an update replaced the files, or the network failed). */
export function DeferredModuleError({ name }: { name: string }) {
  return <ErrorState title={`${name} could not load`} message="Reload the page to load this part again." />;
}
