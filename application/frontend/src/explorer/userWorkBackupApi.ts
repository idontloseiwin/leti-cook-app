import { parseUserWorkBackup, parseUserWorkPreferences, parseUserWorkPreview, parseUserWorkStatus,
  captureUserWorkPreferences, parseReconnectReceipt, reconnectSnapshot, USER_WORK_MAX_BYTES, type UserWorkBackup, type PendingUserChannel, type UserWorkStatus } from './userWorkBackup.ts';
import { savedId } from './savedWork.ts';
import { plainServerText } from '../serverText.ts';

export class UserWorkRequestError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) { super(message); this.name = 'UserWorkRequestError'; this.status = status; this.code = code; }
}
const errorMessages: Record<string, string> = {
  workspace_not_empty: 'This installation already holds channels, folders or labels. Restore the backup into a new installation. Nothing was changed.',
  conflict: 'This saved item changed. Refresh status before trying again.',
  parent_pending: 'Reconnect the parent channel first, or remove its parent link.',
  parent_missing: 'The parent channel is unavailable. Refresh status and check its link.',
  channel_unavailable: 'The channel could not be reconnected. Check the link and try again.',
  needs_input: 'Enter the original channel link before reconnecting.',
  hidden: 'Hidden channels are excluded from reconnection.',
  database_unavailable: 'Storage is temporarily unavailable. Refresh status before trying again.',
  database_write_outcome_unknown: 'The request may have been saved. Check status before another reconnect request.',
  not_found: 'This saved item is no longer pending. Refresh status.',
  busy: 'Another operation is still running. Refresh status before trying again.',
  user_work_limit_exceeded: 'This backup exceeds the supported size or item limit.',
  user_work_unavailable: 'User-work storage is unavailable. Your file was kept; refresh status before retrying.',
  resolution_unavailable: 'Channel reconnection is unavailable. Try again after the installation is ready.',
  invalid_user_work_backup: 'This is not a supported user-work backup. Nothing was restored.',
  invalid_user_work_request: 'The request was not accepted. Refresh status and try again.',
  cyclic_channel_relationship: 'A channel cannot be its own parent.',
};
const rootObject = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('The server returned an invalid user-work response.');
  return value as Record<string, unknown>;
};
async function request(path: string, signal?: AbortSignal, method = 'GET', value?: unknown, expectedStatus?: number): Promise<unknown> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort(); signal?.addEventListener('abort', abort, { once: true });
  const timeout = setTimeout(abort, 60000);
  try {
    const body = value === undefined ? undefined : JSON.stringify(value);
    if (body && new TextEncoder().encode(body).length > USER_WORK_MAX_BYTES) throw new Error('The backup is larger than 16 MiB.');
    const response = await fetch(path, { method, body, signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
      headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json', 'X-Saved-Work': '1' } : {}) } });
    const maxResponse = USER_WORK_MAX_BYTES + 65536;
    const advertised = response.headers.get('Content-Length');
    if (advertised && Number(advertised) > maxResponse) { await response.body?.cancel(); throw new Error('The server response exceeds the backup size limit.'); }
    const reader = response.body?.getReader(); let received = 0, contents = '';
    if (reader) {
      const decoder = new TextDecoder('utf-8', { fatal: true });
      try {
        while (true) {
          const chunk = await reader.read(); if (chunk.done) break;
          received += chunk.value.length;
          if (received > maxResponse) { await reader.cancel(); throw new Error('The server response exceeds the backup size limit.'); }
          contents += decoder.decode(chunk.value, { stream: true });
        }
        contents += decoder.decode();
      } finally { reader.releaseLock(); }
    } else contents = await response.text();
    if (new TextEncoder().encode(contents).length > maxResponse) throw new Error('The server response exceeds the backup size limit.');
    let payload: unknown;
    try { payload = JSON.parse(contents); } catch { throw new UserWorkRequestError('The server response was not confirmed. Check status before retrying.', response.status, 'invalid_response'); }
    if (!response.ok) {
      const error = rootObject(payload);
      const detail = error.detail && typeof error.detail === 'object' ? rootObject(error.detail) : error;
      const code = typeof detail.code === 'string' && /^[a-z_]{1,100}$/.test(detail.code) ? detail.code : 'request_failed';
      const message = errorMessages[code] ?? plainServerText(detail.message, 400) ?? plainServerText(detail.error, 400)
        ?? 'The operation was not confirmed. Refresh status before retrying.';
      throw new UserWorkRequestError(message, response.status, code);
    }
    if (expectedStatus !== undefined && response.status !== expectedStatus) throw new UserWorkRequestError('Reconnection was not confirmed. Refresh status before retrying.', response.status, 'invalid_response');
    return payload;
  } finally { clearTimeout(timeout); signal?.removeEventListener('abort', abort); }
}
export async function loadUserWorkStatus(signal?: AbortSignal, pendingOffset = 0) {
  if (!Number.isSafeInteger(pendingOffset) || pendingOffset < 0 || pendingOffset > 10000 || pendingOffset % 100 !== 0) throw new Error('Invalid pending-channel page.');
  return parseUserWorkStatus(await request(`/user-work/status${pendingOffset ? `?pending_offset=${pendingOffset}` : ''}`, signal));
}
export async function createUserWorkBackup(signal?: AbortSignal) {
  const root = rootObject(await request('/user-work/backup', signal, 'POST', { browser_preferences: captureUserWorkPreferences() }));
  if (Object.keys(root).some((key) => !['backup', 'warnings'].includes(key)) || !Array.isArray(root.warnings) || root.warnings.length > 100) throw new Error('The backup response is incompatible.');
  const warnings = root.warnings.map((value: unknown) => {
    const item = rootObject(value);
    if (Object.keys(item).sort().join(',') !== 'code,count' || typeof item.code !== 'string' || !/^[a-z_]{1,100}$/.test(item.code)
      || !Number.isSafeInteger(item.count) || Number(item.count) < 0) throw new Error('The backup warning response is incompatible.');
    return { code: item.code, count: item.count as number };
  });
  return { backup: parseUserWorkBackup(root.backup), warnings };
}
export async function previewUserWorkRestore(backup: UserWorkBackup, signal?: AbortSignal) {
  return parseUserWorkPreview(await request('/user-work/restore/preview', signal, 'POST', parseUserWorkBackup(backup)));
}
export async function restoreUserWorkBackup(backup: UserWorkBackup, signal?: AbortSignal) {
  const root = rootObject(await request('/user-work/restore', signal, 'POST', parseUserWorkBackup(backup)));
  if (Object.keys(root).sort().join(',') !== 'browser_preferences,pending_count,status' || root.status !== 'restored'
    || !Number.isSafeInteger(root.pending_count) || Number(root.pending_count) < 0) throw new Error('Restoration was not confirmed. Refresh status before retrying.');
  return { status: 'restored' as const, pending_count: root.pending_count as number, browser_preferences: parseUserWorkPreferences(root.browser_preferences) };
}
export async function resolvePendingUserChannel(item: PendingUserChannel, signal?: AbortSignal) {
  return parseReconnectReceipt(await request(`/user-work/pending/${encodeURIComponent(savedId(item.id))}/resolve`, signal,
    'POST', { expected_revision: item.revision }, 202));
}
export async function reconnectAllUserChannels(snapshot: string, signal?: AbortSignal) {
  return parseReconnectReceipt(await request('/user-work/pending/reconnect', signal, 'POST', { expected_snapshot: reconnectSnapshot(snapshot) }, 202));
}
/** One read at a time; never retries a mutation. The dialog owns cancellation. */
export function pollUserWorkStatus({ pendingOffset, onStatus, onStopped, isBusy }: {
  pendingOffset: number; onStatus: (status: UserWorkStatus) => void; onStopped: () => void; isBusy: () => boolean;
}) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout>, failures = 0;
  const schedule = () => { timer = setTimeout(() => void tick(), 5000); };
  const tick = async () => {
    if (controller.signal.aborted) return;
    if (isBusy()) { schedule(); return; }
    try {
      const status = await loadUserWorkStatus(controller.signal, pendingOffset);
      if (controller.signal.aborted) return;
      if (!isBusy()) onStatus(status);
      failures = 0;
      const { queued, running, waiting } = status.reconnect.counts;
      if (!queued && !running && !waiting) return;
    } catch {
      if (controller.signal.aborted) return;
      if (++failures >= 3) { onStopped(); return; }
    }
    schedule();
  };
  schedule();
  return () => { controller.abort(); clearTimeout(timer); };
}
export async function updatePendingUserChannel(item: PendingUserChannel, userInput: string | null, parentRef: string | null, signal?: AbortSignal) {
  // Successful PATCH is followed by an independent status read; no assumed revision.
  await request(`/user-work/pending/${encodeURIComponent(savedId(item.id))}`, signal, 'PATCH',
    { expected_revision: item.revision, user_input: userInput, parent_ref: parentRef });
}
