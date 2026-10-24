/** Local runtime state only. Never persist API data or server error text. */
export type RuntimeState = {
  status?: 'ready' | 'blocked'; reason?: string;
  api_read?: { allowed: boolean; reason?: string };
  collection?: { allowed: boolean; reason?: string };
  user_work?: { allowed: boolean; reason?: string };
  maintenance_failed?: boolean;
  update_copy?: { status: 'disabled' | 'absent' | 'current' | 'removed' | 'error'; expires_at: number | null };
  time?: { state?: 'trusted' | 'mismatch' | 'unverified'; reason?: 'not_checked_yet' };
  startup_notice?: { id: string; records: number; complete: boolean } | null;
};
export type RuntimeNotice = { id: string; text: string; tone: 'warning' | 'info'; dismissible?: boolean };
export const DISMISSED_RUNTIME_NOTICES_KEY = 'lic-runtime-dismissed-notices-v1';
const NOTICE_ID = /^[a-f0-9]{32}$/;
const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;

export function parseRuntimeState(value: unknown): RuntimeState {
  const root = record(value);
  if (!root || !['ready', 'blocked'].includes(String(root.status))) throw new Error('runtime_status_invalid');
  const state: RuntimeState = { status: root.status as RuntimeState['status'] };
  if (typeof root.reason === 'string') state.reason = root.reason;
  for (const key of ['api_read', 'collection', 'user_work'] as const) {
    const item = record(root[key]);
    if (item && typeof item.allowed === 'boolean') state[key] = {
      allowed: item.allowed, ...(typeof item.reason === 'string' ? { reason: item.reason } : {}),
    };
  }
  state.maintenance_failed = root.maintenance_failed === true;
  const copy = record(root.update_copy);
  if (copy && ['disabled', 'absent', 'current', 'removed', 'error'].includes(String(copy.status))
      && (copy.expires_at === null || (Number.isSafeInteger(copy.expires_at) && Number(copy.expires_at) > 0))) {
    state.update_copy = { status: copy.status as NonNullable<RuntimeState['update_copy']>['status'],
      expires_at: copy.expires_at as number | null };
  }
  const time = record(root.time);
  if (time && ['trusted', 'mismatch', 'unverified'].includes(String(time.state))) {
    state.time = { state: time.state as NonNullable<RuntimeState['time']>['state'],
      ...(time.reason === 'not_checked_yet' ? { reason: 'not_checked_yet' as const } : {}) };
  }
  const notice = record(root.startup_notice);
  if (notice && typeof notice.id === 'string' && NOTICE_ID.test(notice.id)
      && Number.isSafeInteger(notice.records) && Number(notice.records) >= 0 && typeof notice.complete === 'boolean') {
    state.startup_notice = { id: notice.id, records: notice.records as number, complete: notice.complete };
  }
  return state;
}

export function apiReadAllowed(state: RuntimeState): boolean {
  return state.api_read?.allowed ?? state.status === 'ready';
}

export function runtimeBlockedText(state: RuntimeState): string {
  const reason = state.api_read?.reason || state.reason;
  if (state.time?.state === 'mismatch' || reason === 'time_mismatch' || reason === 'clock_changed') {
    return 'YouTube data and collection are paused. Check the server clock.';
  }
  if (reason === 'schema_invalid') {
    return 'The database needs attention. Check the installation.';
  }
  if (reason === 'database_unavailable') {
    return 'The database is unavailable. Reconnecting…';
  }
  if (reason === 'startup_cleanup') return 'Removing expired YouTube data. Current data is being checked.';
  return 'YouTube data is temporarily unavailable while the application gets ready.';
}

export function runtimeNotices(state: RuntimeState, dismissed: readonly string[] = []): RuntimeNotice[] {
  const notices: RuntimeNotice[] = [];
  if (!apiReadAllowed(state)) {
    const userWork = state.user_work?.allowed === true
      ? ' Your collections, backups and settings are still available.' : '';
    notices.push({ id: 'api', tone: 'warning', text: runtimeBlockedText(state) + userWork });
  } else if (state.collection?.allowed === false) {
    notices.push({ id: 'collection', tone: 'info', text: 'Collection is temporarily paused. It will resume automatically.' });
  }
  if (state.maintenance_failed) {
    notices.push({ id: 'maintenance', tone: 'warning', text: 'Data cleanup needs attention. It will retry automatically; expired data stays hidden.' });
  }
  if (state.time?.state === 'unverified') {
    notices.push(state.time.reason === 'not_checked_yet'
      ? { id: 'clock', tone: 'info', text: 'The server clock has not been checked yet. The application is working; verification will run automatically.' }
      : { id: 'clock', tone: 'warning', text: 'The server clock could not be verified. The application is still working; verification will retry.' });
  }
  if (state.update_copy?.status === 'current') {
    notices.push({ id: 'update-copy', tone: 'warning', text: 'A temporary database copy exists while an update is being completed.' });
  } else if (state.update_copy?.status === 'error') {
    notices.push({ id: 'update-copy', tone: 'warning', text: 'The temporary update copy needs attention. Check the installation.' });
  }
  const notice = state.startup_notice;
  if (notice?.complete && !dismissed.includes(notice.id)) {
    notices.push({ id: notice.id, tone: 'info', dismissible: true,
      text: `${notice.records.toLocaleString('en-US')} stored ${notice.records === 1 ? 'record expired' : 'records expired'} while the application was offline and ${notice.records === 1 ? 'was' : 'were'} removed.` });
  }
  return notices;
}

export function readDismissedNotices(storage?: Pick<Storage, 'getItem'>): string[] {
  try {
    const value: unknown = JSON.parse(storage?.getItem(DISMISSED_RUNTIME_NOTICES_KEY) || '[]');
    return Array.isArray(value) ? [...new Set(value.filter((id): id is string => typeof id === 'string' && NOTICE_ID.test(id)))].slice(-20) : [];
  } catch { return []; }
}

export function dismissRuntimeNotice(id: string, current: readonly string[], storage?: Pick<Storage, 'setItem'>): string[] {
  if (!NOTICE_ID.test(id)) return [...current];
  const next = [...current.filter(value => value !== id && NOTICE_ID.test(value)), id].slice(-20);
  try { storage?.setItem(DISMISSED_RUNTIME_NOTICES_KEY, JSON.stringify(next)); } catch { /* Dismiss for this page even if storage is disabled. */ }
  return next;
}
