import { captureSourceLifetime, sourceExpired, sourceLifetime, transferSourceLifetime } from '../sourceLifetime';
import type { Channel } from '../types';

export type DeletedChannel = {
  id: string; user_input: string | null; removed_at: string | null;
  restore_until: string | null; can_restore: boolean;
};
export type TrashPage = { items: DeletedChannel[]; next_cursor: string | null };
export type TrashTitle = { id: string; title: string; source_expires_at: string };
export type TrashTitles = { items: TrashTitle[] };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const messages: Record<string, string> = {
  expired: 'The recovery period has ended. This channel is awaiting permanent cleanup.',
  conflict: 'This channel changed. Refresh the list before trying again.',
  not_found: 'This channel has already been permanently removed.',
};
export function parseTrashPage(raw: unknown): TrashPage {
  if (!raw || typeof raw !== 'object') throw new Error('Invalid deleted-channel list.');
  const value = raw as TrashPage;
  if (!Array.isArray(value.items) || value.items.length > 100
    || !(value.next_cursor === null || (typeof value.next_cursor === 'string' && uuid.test(value.next_cursor)))) throw new Error('Invalid deleted-channel list.');
  const items = value.items.map((item) => {
    if (!item || typeof item.id !== 'string' || !uuid.test(item.id)
      || !(item.user_input === null || (typeof item.user_input === 'string' && item.user_input.length <= 2048))
      || typeof item.can_restore !== 'boolean'
      || ![item.removed_at, item.restore_until].every((v) => v === null || (typeof v === 'string' && v.length <= 48 && Number.isFinite(Date.parse(v))))
      || (item.can_restore && (!item.removed_at || !item.restore_until))) throw new Error('Invalid deleted-channel list.');
    return { id: item.id, user_input: item.user_input, removed_at: item.removed_at,
      restore_until: item.restore_until, can_restore: item.can_restore };
  });
  return { items, next_cursor: value.next_cursor };
}
async function request(path: string, signal: AbortSignal, method = 'GET', body?: unknown) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true }); if (signal.aborted) abort();
  const timeout = setTimeout(abort, 15000);
  const requestStarted = performance.now();
  try {
    const response = await fetch('/user-work/deleted-channels' + path, {
      method, signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
      headers: { Accept: 'application/json', ...(method === 'POST' ? { 'X-Saved-Work': '1', 'Content-Type': 'application/json' } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const reader = response.body?.getReader(); let text = '', bytes = 0;
    if (!reader) throw new Error('The server response was not confirmed. Refresh before retrying.');
    const decoder = new TextDecoder('utf-8', { fatal: true });
    try { while (true) {
      const chunk = await reader.read(); if (chunk.done) break;
      bytes += chunk.value.length;
      if (bytes > 512 * 1024) { await reader.cancel(); throw new Error('The response is too large.'); }
      text += decoder.decode(chunk.value, { stream: true });
    } text += decoder.decode(); } finally { reader.releaseLock(); }
    const result = JSON.parse(text);
    if (!response.ok) throw new Error(messages[result?.code] ?? 'The operation was not confirmed. Refresh status before retrying.');
    return captureSourceLifetime(result, response.headers, requestStarted);
  } finally { clearTimeout(timeout); signal.removeEventListener('abort', abort); }
}
export function currentChannelName(channel: Channel | TrashTitle | undefined): string | undefined {
  return channel && sourceLifetime(channel) && !sourceExpired(channel) && channel.title ? channel.title : undefined;
}
export function parseTrashTitles(raw: unknown, requested: string[]): TrashTitles {
  if (!raw || typeof raw !== 'object') throw new Error('Invalid channel names.');
  const value = raw as TrashTitles;
  const seen = new Set<string>();
  if (!Array.isArray(value.items) || value.items.length > requested.length) throw new Error('Invalid channel names.');
  for (const item of value.items) {
    if (!item || !requested.includes(item.id) || seen.has(item.id)
      || typeof item.title !== 'string' || !item.title || item.title.length > 2048
      || typeof item.source_expires_at !== 'string' || !Number.isFinite(Date.parse(item.source_expires_at))) throw new Error('Invalid channel names.');
    seen.add(item.id);
  }
  return transferSourceLifetime(raw, { items: value.items.map(item => ({ id: item.id, title: item.title, source_expires_at: item.source_expires_at })) });
}
export async function loadDeletedChannelTitles(items: DeletedChannel[], signal: AbortSignal): Promise<TrashTitles> {
  const identities = items.map(item => item.id);
  if (!identities.length) return { items: [] };
  if (identities.length > 100 || new Set(identities).size !== identities.length || identities.some(id => !uuid.test(id))) throw new Error('Invalid page.');
  return parseTrashTitles(await request('/titles?ids=' + identities.join(','), signal), identities);
}
export async function loadDeletedChannels(signal: AbortSignal, after?: string): Promise<TrashPage> {
  if (after && !uuid.test(after)) throw new Error('Invalid page.');
  return parseTrashPage(await request(after ? '?after=' + after : '', signal));
}
export async function removeTrackedChannel(channelId: string, signal: AbortSignal) {
  if (!/^UC[A-Za-z0-9_-]{22}$/.test(channelId)) throw new Error('Invalid channel.');
  const result = await request('/remove/' + channelId, signal, 'POST', {});
  if (result?.status !== 'removed') throw new Error('Removal was not confirmed. Refresh before retrying.');
}
export async function restoreDeletedChannel(item: DeletedChannel, signal: AbortSignal) {
  if (!uuid.test(item.id) || !item.removed_at || !item.can_restore) throw new Error('This channel cannot be restored.');
  const result = await request('/' + item.id + '/restore', signal, 'POST', { removed_at: item.removed_at });
  if (result?.status !== 'restored') throw new Error('Restoration was not confirmed. Refresh before retrying.');
}
