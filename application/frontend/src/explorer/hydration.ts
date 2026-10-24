import type {
  ExplorerHydrationPayload,
  ExplorerHydrationState,
} from './model';

export const EXPLORER_HYDRATION_POLL_MS = 2_500;
export const EXPLORER_HYDRATION_BACKGROUND_POLL_MS = 10_000;

export function hydrationPayload(
  state: ExplorerHydrationState,
): ExplorerHydrationPayload | undefined {
  if (state.kind === 'ready' || state.kind === 'confirming') return state.payload;
  return state.kind === 'error' ? state.previous : undefined;
}

export function hydrationNeedsPolling(payload: ExplorerHydrationPayload): boolean {
  return payload.status === 'queued' || payload.status === 'running';
}

export function hydrationProfileReady(payload: ExplorerHydrationPayload): boolean {
  return payload.current_profile_ready;
}

// Initial work can finish while some videos await publication. The separate
// profile-ready gate opens Long analytics over admitted, published rows only.
export function hydrationInitialComplete(payload: ExplorerHydrationPayload): boolean {
  return (payload.status === 'ready' && hydrationProfileReady(payload))
    || (payload.status === 'completed' && payload.all_initial_checks_complete === true);
}

export function hydrationInitialBecameComplete(
  previous: ExplorerHydrationPayload | undefined,
  next: ExplorerHydrationPayload,
): boolean {
  // The first hydration response can arrive after the parallel totals request
  // captured pre-completion coverage, so it also invalidates that early read.
  return (!previous || !hydrationInitialComplete(previous)) && hydrationInitialComplete(next);
}

export function hydrationProfileBecameReady(
  previous: ExplorerHydrationPayload | undefined,
  next: ExplorerHydrationPayload,
): boolean {
  return Boolean(
    previous && !hydrationProfileReady(previous) && hydrationProfileReady(next),
  );
}

export function hydrationInventoryReady(payload: ExplorerHydrationPayload): boolean {
  return payload.inventory_ready;
}

export function hydrationPhaseTitle(payload: ExplorerHydrationPayload): string {
  if (payload.confirmation_required) return 'Full channel loading needs confirmation';
  if (payload.status === 'blocked') return 'Profile loading is blocked';
  if (payload.status === 'error') return 'Profile verification needs attention';
  if (payload.status === 'suspended') return 'Profile loading is paused';
  return 'Loading channel profile';
}

export function hydrationNeedsRestart(payload: ExplorerHydrationPayload): boolean {
  return payload.job?.error_code === 'initial_inventory_restart_required'
    || payload.job?.error_code === 'source_scope_expired';
}

export function hydrationStoppedReason(payload: ExplorerHydrationPayload): string | null {
  if (!['blocked', 'error', 'suspended'].includes(payload.status)) return null;
  if (hydrationNeedsRestart(payload)) {
    return 'The saved loading checkpoint is no longer usable. Restart loading to read the channel lists from the beginning. Already saved data is kept.';
  }
  switch (payload.job?.error_code) {
    case 'channel_sync_not_ready':
      return 'Collection is paused for this channel. Check its collection settings before continuing.';
    case 'youtube_daily_quota_exceeded_error':
    case 'youtube_quota_exhausted':
    case 'quota_reservation_exhausted':
      return 'YouTube quota is used up. Loading can continue after the daily quota resets.';
    case 'initial_hydration_estimate_exceeded':
    case 'initial_inventory_confirmation_required':
      return 'The channel contains more videos than the accepted estimate. Confirm full loading to continue.';
    case 'database_unavailable':
      return 'The database is unavailable. Loading can continue after the connection recovers.';
    case 'clock_changed':
    case 'time_mismatch':
      return 'The server clock needs verification before loading can continue.';
    default:
      return 'Loading could not confirm its last step. Check Health for the reported problem before retrying.';
  }
}
