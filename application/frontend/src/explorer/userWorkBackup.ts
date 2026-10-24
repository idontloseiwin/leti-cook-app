import { normalizeCatalogueConditions, serializeCatalogueConditions } from './catalogueQuery.ts';
import { savedId, savedRevision, savedTimestamp } from './savedWork.ts';
import { catalogueDensityPreference, type CatalogueDensity } from './cataloguePreferences.ts';
import { readBrowserPreference, writeBrowserPreference } from '../state/browserPreferences.ts';
import { EXPLORER_BOOKMARKS_STORAGE_KEY, loadExplorerBookmarks, saveExplorerBookmarks } from './bookmarks.ts';
import { COLLAPSED_FOLDERS_STORAGE_KEY, loadCollapsedFolders, saveCollapsedFolders } from './folderCollapse.ts';

export const USER_WORK_MAX_BYTES = 16 * 1024 * 1024;
export const PENDING_BOOKMARKS_STORAGE_KEY = 'leti-cook.user-work.pending-bookmarks.v1';
const LEGACY_PENDING_BOOKMARKS_STORAGE_KEY = 'let-it-cook.user-work.pending-bookmarks.v1';
export type UserWorkStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const channelPattern = /^UC[A-Za-z0-9_-]{22}$/;
const videoPattern = /^[A-Za-z0-9_-]{11}$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const fail = (message = 'This is not a supported user-work backup.'): never => { throw new Error(message); };
function object(value: unknown, required: readonly string[], optional: readonly string[] = []): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail();
  const row = value as Record<string, unknown>;
  if (required.some((key) => !Object.hasOwn(row, key)) || Object.keys(row).some((key) => !required.includes(key) && !optional.includes(key))) return fail('The file contains unsupported fields. Nothing was restored.');
  return row;
}
function rows(value: unknown, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) return fail('The backup exceeds a supported item limit.');
  return value;
}
function text(value: unknown, max: number, empty = false): string {
  if (typeof value !== 'string' || (!empty && !value.trim()) || [...value].length > max || /[\u0000-\u001f\u007f]/.test(value)
    || [...value].some((char) => { const point = char.codePointAt(0)!; return point >= 0xd800 && point <= 0xdfff; })) return fail();
  return value;
}
const nullableText = (value: unknown, max: number) => value === null ? null : text(value, max);
const uuid = (value: unknown): string => typeof value === 'string' && uuidPattern.test(value) ? value.toLowerCase() : fail();
const nullableUuid = (value: unknown) => value === null ? null : uuid(value);
const channelId = (value: unknown): string => typeof value === 'string' && channelPattern.test(value) ? value : fail();
const videoId = (value: unknown): string => typeof value === 'string' && videoPattern.test(value) ? value : fail();
const count = (value: unknown): number => Number.isSafeInteger(value) && Number(value) >= 0 ? value as number : fail();
function unique<T>(values: T[], key: (value: T) => string): T[] {
  if (new Set(values.map(key)).size !== values.length) return fail('The backup contains duplicate identities.');
  return values;
}
function density(value: unknown): CatalogueDensity {
  return catalogueDensityPreference.isValid(value) ? value : fail();
}
const owner = (row: Record<string, unknown>) => row.channel_ref === undefined || row.channel_ref === null ? {} : { channel_ref: savedId(row.channel_ref) };
const folderKey = (value: unknown): string => value === 'unassigned' ? value : uuid(value);
export function parseUserWorkPreferences(value: unknown) {
  const row = object(value, ['catalogue_density', 'bookmarked_channel_refs', 'collapsed_folder_ids']);
  return {
    catalogue_density: density(row.catalogue_density),
    bookmarked_channel_refs: unique(rows(row.bookmarked_channel_refs, 10000).map(savedId), (id) => id),
    collapsed_folder_ids: unique(rows(row.collapsed_folder_ids, 500).map(folderKey), (id) => id),
  };
}
export type UserWorkPreferences = ReturnType<typeof parseUserWorkPreferences>;

/** Closed projection; even a forged file cannot add arbitrary API fields to a download. */
export function parseUserWorkBackup(value: unknown) {
  const root = object(value, ['format', 'version', 'created_at', 'user_work', 'browser_preferences']);
  // Format 2 lists the preset Monetized label when it was kept; format 1 implies it.
  if ((root.format !== 'leti-cook-user-work' && root.format !== 'let-it-cook-user-work') || (root.version !== 1 && root.version !== 2)) return fail('Unsupported backup format or version.');
  const version = root.version as 1 | 2;
  const work = object(root.user_work, ['folders', 'labels', 'channels', 'collections', 'reviews']);
  const folders = unique(rows(work.folders, 1000).map((value) => {
    const row = object(value, ['id', 'name', 'created_at']);
    return { id: uuid(row.id), name: text(row.name, 120), created_at: savedTimestamp(row.created_at) };
  }), (row) => row.id);
  const labels = unique(rows(work.labels, 1001).map((value) => {
    const row = object(value, ['label_key', 'name', 'label_type', 'created_at']);
    const key = text(row.label_key, 64);
    if (key === 'monetized') {
      if (row.label_type !== 'preset' || version !== 2) return fail();
      return { label_key: key, name: text(row.name, 40), label_type: 'preset' as 'preset' | 'custom', created_at: savedTimestamp(row.created_at) };
    }
    if (row.label_type !== 'custom' || !key.startsWith('custom:')) return fail();
    uuid(key.slice(7));
    return { label_key: key, name: text(row.name, 40), label_type: 'custom' as 'preset' | 'custom', created_at: savedTimestamp(row.created_at) };
  }), (row) => row.label_key);
  if (labels.filter((row) => row.label_type === 'custom').length > 1000) return fail('The backup exceeds the label limit.');
  let views = 0, members = 0, history = 0, assignments = 0;
  const channels = unique(rows(work.channels, 10000).map((value) => {
    // hidden_at: the original trash time of a channel in the trash; older backups omit it.
    const row = object(value, ['id', 'user_input', 'folder_id', 'parent_ref', 'is_hidden', 'added_at', 'saved_views', 'label_keys'], ['hidden_at']);
    if (typeof row.is_hidden !== 'boolean') return fail();
    const hiddenAt = row.hidden_at === undefined || row.hidden_at === null ? null : row.is_hidden ? savedTimestamp(row.hidden_at) : fail();
    const savedViews = rows(row.saved_views, 1000).map((value) => {
      if (++views > 1000) return fail();
      const view = object(value, ['id', 'name', 'conditions', 'revision', 'created_at', 'updated_at']);
      const conditions = normalizeCatalogueConditions(view.conditions); serializeCatalogueConditions(conditions);
      return { id: savedId(view.id), name: text(view.name, 120), conditions, revision: savedRevision(view.revision),
        created_at: savedTimestamp(view.created_at), updated_at: savedTimestamp(view.updated_at) };
    });
    const labelKeys = unique(rows(row.label_keys, 1001).map((value) => {
      const key = text(value, 64); if (key !== 'monetized') { if (!key.startsWith('custom:')) return fail(); uuid(key.slice(7)); } return key;
    }), (key) => key);
    assignments += labelKeys.length;
    if (assignments > 100000) return fail('The backup exceeds the label-assignment limit.');
    return { id: savedId(row.id), user_input: nullableText(row.user_input, 2048), folder_id: nullableUuid(row.folder_id), parent_ref: nullableUuid(row.parent_ref),
      is_hidden: row.is_hidden, added_at: row.added_at === null ? null : savedTimestamp(row.added_at), saved_views: savedViews, label_keys: labelKeys,
      ...(hiddenAt === null ? {} : { hidden_at: hiddenAt }) };
  }), (row) => row.id);
  const collections = unique(rows(work.collections, 1000).map((value) => {
    const row = object(value, ['id', 'name', 'revision', 'created_at', 'updated_at', 'members']);
    return { id: savedId(row.id), name: text(row.name, 120), revision: savedRevision(row.revision),
      created_at: savedTimestamp(row.created_at), updated_at: savedTimestamp(row.updated_at),
      members: unique(rows(row.members, 100000).map((value) => {
        if (++members > 100000) return fail();
        // channel_ref: the owning channel in this backup; older backups omit it.
        const member = object(value, ['video_id', 'added_at'], ['channel_ref']);
        return { video_id: videoId(member.video_id), added_at: savedTimestamp(member.added_at), ...owner(member) };
      }), (row) => row.video_id) };
  }), (row) => row.id);
  if (views + collections.length > 1000) return fail('The backup exceeds the saved-work item limit.');
  const channelRefs = new Set(channels.map((row) => row.id));
  const reviewType = (value: unknown): 'long' | 'short' | 'stream' => value === 'long' || value === 'short' || value === 'stream' ? value : fail();
  const reviews = unique(rows(work.reviews, 1000).map((value) => {
    const row = object(value, ['case_id', 'revision', 'state', 'decision_type', 'decision_reason', 'decision_at', 'decision_revision', 'history'], ['channel_ref']);
    if (row.state !== 'open' && row.state !== 'resolved') return fail();
    return { case_id: savedId(row.case_id), revision: savedRevision(row.revision), state: row.state,
      decision_type: row.decision_type === null ? null : reviewType(row.decision_type), decision_reason: nullableText(row.decision_reason, 1000),
      decision_at: row.decision_at === null ? null : savedTimestamp(row.decision_at), decision_revision: row.decision_revision === null ? null : savedRevision(row.decision_revision),
      history: rows(row.history, 10000).map((value) => {
        if (++history > 10000) return fail();
        const item = object(value, ['revision', 'video_type', 'reason', 'decided_at', 'request_id', 'expected_revision']);
        return { revision: savedRevision(item.revision), video_type: reviewType(item.video_type), reason: text(item.reason, 1000),
          decided_at: savedTimestamp(item.decided_at), request_id: uuid(item.request_id), expected_revision: savedRevision(item.expected_revision) };
      }), ...owner(row) };
  }), (row) => row.case_id);
  if ([...collections.flatMap((row) => row.members), ...reviews].some((row) => 'channel_ref' in row && !channelRefs.has(row.channel_ref as string))) return fail('The backup refers to a missing channel.');
  return { format: 'leti-cook-user-work' as const, version, created_at: savedTimestamp(root.created_at),
    user_work: { folders, labels, channels, collections, reviews }, browser_preferences: parseUserWorkPreferences(root.browser_preferences) };
}
export type UserWorkBackup = ReturnType<typeof parseUserWorkBackup>;
export async function readUserWorkFile(file: Pick<File, 'size' | 'text'>): Promise<UserWorkBackup> {
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > USER_WORK_MAX_BYTES) return fail('Choose a JSON backup no larger than 16 MiB.');
  const contents = await file.text();
  if (new TextEncoder().encode(contents).length > USER_WORK_MAX_BYTES) return fail('The backup is larger than 16 MiB.');
  try { return parseUserWorkBackup(JSON.parse(contents)); }
  catch (error) { if (error instanceof SyntaxError) return fail('The backup is not valid JSON.'); throw error; }
}
export function downloadUserWorkBackup(value: unknown): void {
  const backup = parseUserWorkBackup(value);
  const contents = JSON.stringify(backup);
  if (new TextEncoder().encode(contents).length > USER_WORK_MAX_BYTES) return fail('The backup is larger than 16 MiB.');
  const url = URL.createObjectURL(new Blob([contents], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url; link.download = 'leti-cook-user-work.json';
  document.body.appendChild(link);
  try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
}
const reconnectStates = ['idle', 'queued', 'running', 'waiting', 'blocked'] as const;
export function reconnectSnapshot(value: unknown): string {
  return typeof value === 'string' && /^reconnect-v1:[0-9a-f]{64}$/.test(value) ? value : fail('The reconnect status is incompatible. Refresh status.');
}
const reconnectCount = (value: unknown) => count(value) <= 10000 ? value as number : fail();
export function parseReconnectReceipt(value: unknown) {
  const row = object(value, ['status', 'snapshot', 'queued_count', 'blocked_count', 'already_requested_count', 'excluded_hidden']);
  if (row.status !== 'queued') return fail('Reconnection was not confirmed. Refresh status before retrying.');
  const result = { status: 'queued' as const, snapshot: reconnectSnapshot(row.snapshot), queued_count: reconnectCount(row.queued_count),
    blocked_count: reconnectCount(row.blocked_count), already_requested_count: reconnectCount(row.already_requested_count),
    excluded_hidden: reconnectCount(row.excluded_hidden) };
  if (result.queued_count + result.blocked_count + result.already_requested_count + result.excluded_hidden > 10000) return fail();
  return result;
}
function parseReconnectStatus(value: unknown) {
  const row = object(value, ['snapshot', 'counts', 'excluded_hidden']);
  const values = object(row.counts, reconnectStates);
  const counts = { idle: reconnectCount(values.idle), queued: reconnectCount(values.queued), running: reconnectCount(values.running),
    waiting: reconnectCount(values.waiting), blocked: reconnectCount(values.blocked) };
  const excluded_hidden = reconnectCount(row.excluded_hidden);
  if (Object.values(counts).reduce((sum, value) => sum + value, excluded_hidden) > 10000) return fail();
  return { snapshot: reconnectSnapshot(row.snapshot), counts, excluded_hidden };
}
export function parseUserWorkStatus(value: unknown) {
  const row = object(value, ['available', 'can_restore', 'pending', 'channel_mappings', 'user_channel_refs', 'folder_ids', 'pending_more', 'reconnect']);
  if (row.available !== true || typeof row.can_restore !== 'boolean' || typeof row.pending_more !== 'boolean') return fail();
  return { available: true as const, can_restore: row.can_restore, pending_more: row.pending_more === true,
    reconnect: parseReconnectStatus(row.reconnect),
    user_channel_refs: unique(rows(row.user_channel_refs, 10000).map(savedId), (id) => id),
    folder_ids: unique(rows(row.folder_ids, 1000).map(savedId), (id) => id),
    pending: unique(rows(row.pending, 100).map((value) => {
      const item = object(value, ['id', 'revision', 'user_input', 'parent_ref', 'reason', 'is_hidden',
        'reconnect_state', 'reconnect_not_before', 'reconnect_error_code']);
      const state = item.reconnect_state as typeof reconnectStates[number];
      if (!reconnectStates.includes(state) || typeof item.is_hidden !== 'boolean'
        || (item.is_hidden && state !== 'idle') || (state === 'waiting') !== (item.reconnect_not_before !== null)) return fail();
      const error = item.reconnect_error_code;
      if (error !== null && (typeof error !== 'string' || !/^[a-z][a-z0-9_]{0,119}$/.test(error))) return fail();
      if (!['needs_input', 'parent_pending', 'ready'].includes(String(item.reason))) return fail();
      return { id: savedId(item.id), revision: savedRevision(item.revision), user_input: nullableText(item.user_input, 2048),
        parent_ref: nullableUuid(item.parent_ref), reason: item.reason as 'needs_input' | 'parent_pending' | 'ready',
        is_hidden: item.is_hidden, reconnect_state: state, reconnect_error_code: error as string | null,
        reconnect_not_before: item.reconnect_not_before === null ? null : savedTimestamp(item.reconnect_not_before) };
    }), (item) => item.id),
    channel_mappings: unique(rows(row.channel_mappings, 10000).map((value) => {
      const item = object(value, ['id', 'channel_id']); return { id: savedId(item.id), channel_id: channelId(item.channel_id) };
    }), (item) => item.id) };
}
export type UserWorkStatus = ReturnType<typeof parseUserWorkStatus>;
export type PendingUserChannel = UserWorkStatus['pending'][number];
function browserStorage(): UserWorkStorage | undefined { try { return window.localStorage; } catch { return undefined; } }
export function readPendingBookmarks(storage = browserStorage()): string[] {
  try { const value = JSON.parse(storage?.getItem(PENDING_BOOKMARKS_STORAGE_KEY) ?? storage?.getItem(LEGACY_PENDING_BOOKMARKS_STORAGE_KEY) ?? '[]'); return unique(rows(value, 10000).map(savedId), (id) => id); }
  catch { return []; }
}
export function captureUserWorkPreferences(storage = browserStorage()) {
  const bookmarks = [...loadExplorerBookmarks(storage)].filter((id) => channelPattern.test(id));
  if (bookmarks.length > 10000) return fail('There are more than 10,000 bookmarks. No incomplete backup was created.');
  return { catalogue_density: readBrowserPreference(catalogueDensityPreference, storage),
    bookmarked_channel_ids: bookmarks,
    pending_bookmark_refs: readPendingBookmarks(storage),
    collapsed_folder_ids: [...loadCollapsedFolders(storage)].filter((id) => id === 'unassigned' || uuidPattern.test(id)).slice(0, 500) };
}
export function installUserWorkPreferences(value: unknown, storage = browserStorage()): boolean {
  const preferences = parseUserWorkPreferences(value);
  if (!storage) return false;
  try {
    // Only known codecs/keys are writable; no source rows or caller-chosen keys.
    writeBrowserPreference(catalogueDensityPreference, preferences.catalogue_density, storage);
    saveCollapsedFolders(new Set(preferences.collapsed_folder_ids), storage);
    storage.setItem(PENDING_BOOKMARKS_STORAGE_KEY, JSON.stringify(preferences.bookmarked_channel_refs));
    if (!saveExplorerBookmarks([], storage)) return false;
    return readBrowserPreference(catalogueDensityPreference, storage) === preferences.catalogue_density
      && JSON.stringify([...loadCollapsedFolders(storage)]) === JSON.stringify(preferences.collapsed_folder_ids)
      && loadExplorerBookmarks(storage).size === 0
      && JSON.stringify(readPendingBookmarks(storage)) === JSON.stringify(preferences.bookmarked_channel_refs);
  } catch { return false; }
}
export function resolveUserWorkBookmarks(status: UserWorkStatus, storage = browserStorage()): boolean {
  if (!storage) return false;
  const pending = readPendingBookmarks(storage), mappings = new Map(status.channel_mappings.map((row) => [row.id, row.channel_id]));
  const resolved = pending.filter((id) => mappings.has(id));
  // A channel removed for good leaves no identity; its bookmark is dropped.
  const known = new Set(status.user_channel_refs);
  const waiting = pending.filter((id) => !mappings.has(id) && known.has(id));
  if (!resolved.length && waiting.length === pending.length) return true;
  if (resolved.length) {
    const actual = new Set([...loadExplorerBookmarks(storage)].filter((id) => channelPattern.test(id)));
    resolved.forEach((id) => actual.add(mappings.get(id)!));
    if (!saveExplorerBookmarks(actual, storage)) return false;
  }
  try { storage.setItem(PENDING_BOOKMARKS_STORAGE_KEY, JSON.stringify(waiting)); return true; }
  catch { return false; }
}
/** Local recovery is scoped to current user identities; it never imports database rows. */
export function applyScopedUserWorkPreferences(value: unknown, status: UserWorkStatus, storage = browserStorage()) {
  const preferences = parseUserWorkPreferences(value);
  const channels = new Set(status.user_channel_refs), folders = new Set(status.folder_ids);
  const scoped = { catalogue_density: preferences.catalogue_density,
    bookmarked_channel_refs: preferences.bookmarked_channel_refs.filter((id) => channels.has(id)),
    collapsed_folder_ids: preferences.collapsed_folder_ids.filter((id) => id === 'unassigned' || folders.has(id)) };
  const ignored_count = preferences.bookmarked_channel_refs.length - scoped.bookmarked_channel_refs.length
    + preferences.collapsed_folder_ids.length - scoped.collapsed_folder_ids.length;
  const applied = installUserWorkPreferences(scoped, storage) && resolveUserWorkBookmarks(status, storage);
  return { applied, ignored_count };
}
export function parseUserWorkPreview(value: unknown) {
  const row = object(value, ['counts', 'can_restore'], ['reason', 'backup']);
  const totals = object(row.counts, ['folders', 'labels', 'channels', 'collections', 'reviews']);
  if (typeof row.can_restore !== 'boolean') return fail();
  return { counts: { folders: count(totals.folders), labels: count(totals.labels), channels: count(totals.channels), collections: count(totals.collections), reviews: count(totals.reviews) },
    can_restore: row.can_restore, reason: row.reason === undefined || row.reason === null ? undefined : text(row.reason, 240),
    backup: row.backup === undefined ? undefined : parseUserWorkBackup(row.backup) };
}
// Named here so tests can verify the exact storage allowlist without reading all storage.
export const USER_WORK_STORAGE_KEYS = [catalogueDensityPreference.key, EXPLORER_BOOKMARKS_STORAGE_KEY,
  COLLAPSED_FOLDERS_STORAGE_KEY, PENDING_BOOKMARKS_STORAGE_KEY] as const;
