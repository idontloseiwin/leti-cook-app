import { sourceExpired, SOURCE_EXPIRED_MESSAGE } from '../sourceLifetime.ts';
import type { ResourceState } from './model';

export type ResourceScopeIdentity = { key: string };

export function beginResourceLoad<T>(
  current: ResourceState<T>,
  scopeKey: string,
): ResourceState<T> {
  current = expireResourceState(current);
  if (
    (current.kind === 'ready' || current.kind === 'updating')
    && current.scopeKey === scopeKey
  ) {
    return { kind: 'updating', scopeKey, payload: current.payload };
  }
  if (
    (current.kind === 'error' || current.kind === 'incompatible')
    && current.scopeKey === scopeKey
    && current.previous
  ) {
    return { kind: 'updating', scopeKey, payload: current.previous };
  }
  return { kind: 'loading', scopeKey };
}

export function completeResourceLoad<T>(
  scopeKey: string,
  payload: T,
): ResourceState<T> {
  return sourceExpired(payload) ? { kind: 'error', scopeKey, message: SOURCE_EXPIRED_MESSAGE } : { kind: 'ready', scopeKey, payload };
}

export function failResourceLoad<T>(
  current: ResourceState<T>,
  scopeKey: string,
  message: string,
): ResourceState<T> {
  const sameScope = current.kind !== 'idle' && current.scopeKey === scopeKey;
  const previous = sameScope && 'payload' in current
    ? current.payload
    : sameScope && 'previous' in current
      ? current.previous
      : undefined;
  return { kind: 'error', scopeKey, message, previous: sourceExpired(previous) ? undefined : previous };
}

export function rejectResourceContract<T>(
  current: ResourceState<T>,
  scopeKey: string,
  message: string,
): ResourceState<T> {
  const sameScope = current.kind !== 'idle' && current.scopeKey === scopeKey;
  const previous = sameScope && 'payload' in current
    ? current.payload
    : sameScope && 'previous' in current
      ? current.previous
      : undefined;
  return { kind: 'incompatible', scopeKey, message, previous: sourceExpired(previous) ? undefined : previous };
}

export function resourcePayload<T>(state: ResourceState<T>): T | undefined {
  if (state.kind === 'ready' || state.kind === 'updating') return sourceExpired(state.payload) ? undefined : state.payload;
  if (state.kind === 'error' || state.kind === 'incompatible') return sourceExpired(state.previous) ? undefined : state.previous;
  return undefined;
}

export function stateForPresentationScope<T>(
  state: ResourceState<T>,
  presentedScope: object | null,
  activeScope: ResourceScopeIdentity,
  fallback: 'idle' | 'loading',
): ResourceState<T> {
  if (presentedScope === activeScope) return expireResourceState(state);
  return fallback === 'loading'
    ? { kind: 'loading', scopeKey: activeScope.key }
    : { kind: 'idle' };
}

export function expireResourceState<T>(state: ResourceState<T>): ResourceState<T> {
  if (state.kind === 'idle' || state.kind === 'loading') return state;
  const payload = 'payload' in state ? state.payload : state.previous;
  if (!sourceExpired(payload)) return state;
  return { kind: 'loading', scopeKey: state.scopeKey };
}
