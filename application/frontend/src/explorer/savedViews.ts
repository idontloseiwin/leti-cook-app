import { requestJson } from './api';
import { EXPLORER_V3_ROOT, explorerChannelPath, parseExplorerChannelPath, parseExplorerOperationalPath, type ExplorerOperationalPage } from '../explorerRoutes';
import { normalizeCatalogueConditions, serializeCatalogueConditions, writeCatalogueUrl, type CatalogueConditions } from './catalogueQuery';
import { SavedWorkContractError, newSavedWorkId, savedId, savedName, savedRecord, savedRevision, savedTimestamp } from './savedWork';

export const SAVED_VIEWS_PATH = `${EXPLORER_V3_ROOT}/saved`;
export type ExplorerDestination = { kind: 'home' | 'research' } | { kind: ExplorerOperationalPage } | { kind: 'saved'; tab?: 'collections'; collectionId?: string; error?: string } | { kind: 'channel'; channelId: string };
export function readExplorerDestination(pathname: string, search = ''): ExplorerDestination {
  if (pathname.replace(/\/$/, '') === `${EXPLORER_V3_ROOT}/research`) return { kind: 'research' };
  const operationalPage = parseExplorerOperationalPath(pathname);
  if (operationalPage) return { kind: operationalPage };
  if (pathname.startsWith(`${SAVED_VIEWS_PATH}/collections/`)) {
    try { return { kind: 'saved', collectionId: savedId(decodeURIComponent(pathname.slice(`${SAVED_VIEWS_PATH}/collections/`.length).replace(/\/$/, ''))) }; }
    catch { return { kind: 'saved', error: 'This collection address is not supported.' }; }
  }
  if (pathname.replace(/\/$/, '') === SAVED_VIEWS_PATH) {
    const parameters = new URLSearchParams(search), tab = parameters.get('tab');
    if (parameters.getAll('tab').length > 1 || (tab !== null && tab !== 'views' && tab !== 'collections')) return { kind: 'saved', error: 'This Saved tab is not supported.' };
    return tab === 'collections' ? { kind: 'saved', tab: 'collections' } : { kind: 'saved' };
  }
  const channelId = parseExplorerChannelPath(pathname);
  return channelId ? { kind: 'channel', channelId } : { kind: 'home' };
}
export type SavedView = {
  id: string; kind: 'view'; name: string; channel_id: string; conditions: CatalogueConditions;
  revision: number; created_at: string; updated_at: string; channel_available: boolean;
};
export type SavedViewsPage = { contract_version: 1; views: SavedView[]; next_cursor: string | null };
export type SavedViewCreate = Pick<SavedView, 'id' | 'name' | 'channel_id' | 'conditions'>;
export type SavedViewSnapshot = { channelId: string; channelName?: string; conditions: CatalogueConditions };
function channelId(value: unknown): string {
  if (typeof value !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(value)) throw new SavedWorkContractError('invalid channel identity.');
  return value;
}
function envelope(value: unknown): Record<string, unknown> {
  const root = savedRecord(value);
  if (root.contract_version !== 1) throw new SavedWorkContractError('unsupported contract version.');
  return root;
}
export function parseSavedView(value: unknown): SavedView {
  const row = savedRecord(value);
  if (row.kind !== 'view' || typeof row.channel_available !== 'boolean') throw new SavedWorkContractError('invalid saved view.');
  let name: string, conditions: CatalogueConditions;
  try { name = savedName(row.name); conditions = normalizeCatalogueConditions(row.conditions); serializeCatalogueConditions(conditions); }
  catch { throw new SavedWorkContractError('invalid name or catalogue conditions.'); }
  const created = savedTimestamp(row.created_at), updated = savedTimestamp(row.updated_at);
  if (Date.parse(updated) < Date.parse(created)) throw new SavedWorkContractError('invalid revision timestamps.');
  return { id: savedId(row.id), kind: 'view', name, channel_id: channelId(row.channel_id), conditions,
    revision: savedRevision(row.revision), created_at: created, updated_at: updated, channel_available: row.channel_available };
}
export function parseSavedViewEnvelope(value: unknown, expectedId?: string): SavedView {
  const view = parseSavedView(envelope(value).view);
  if (expectedId && view.id !== savedId(expectedId)) throw new SavedWorkContractError('different saved identity.');
  return view;
}
export function parseSavedViewsPage(value: unknown, requestedCursor?: string): SavedViewsPage {
  const root = envelope(value);
  if (!Array.isArray(root.views) || root.views.length > 50 || !(root.next_cursor === null || (
    typeof root.next_cursor === 'string' && root.next_cursor.length > 0 && root.next_cursor.length <= 4096 && !/[\u0000-\u0020\u007f]/.test(root.next_cursor)
  ))) throw new SavedWorkContractError('invalid saved views page.');
  if (root.next_cursor !== null && (!root.views.length || root.next_cursor === requestedCursor)) throw new SavedWorkContractError('list cursor did not advance.');
  const views = root.views.map(parseSavedView);
  if (new Set(views.map((view) => view.id)).size !== views.length) throw new SavedWorkContractError('duplicate saved identity.');
  return { contract_version: 1, views, next_cursor: root.next_cursor as string | null };
}

/** The same frozen payload reuses its UUID after an uncertain response. */
export function prepareSavedViewCreate(previous: SavedViewCreate | undefined, snapshot: SavedViewSnapshot, name: string, uuid: () => string = newSavedWorkId): SavedViewCreate {
  const next = { name: savedName(name), channel_id: channelId(snapshot.channelId), conditions: normalizeCatalogueConditions(snapshot.conditions) };
  serializeCatalogueConditions(next.conditions);
  if (previous && previous.name === next.name && previous.channel_id === next.channel_id
    && serializeCatalogueConditions(previous.conditions) === serializeCatalogueConditions(next.conditions)) return previous;
  return { id: savedId(uuid()), ...next };
}
export function savedViewUrl(view: Pick<SavedView, 'channel_id' | 'conditions'>, origin: string): URL {
  return writeCatalogueUrl(new URL(explorerChannelPath(channelId(view.channel_id)), origin), {
    section: 'videos', overviewPeriod: 'max', conditions: view.conditions,
  });
}
const writeHeaders = { 'Content-Type': 'application/json', 'X-Saved-Work': '1' };
const path = (id: string) => `/saved/views/${encodeURIComponent(savedId(id))}`;
export async function loadSavedViews(cursor?: string, signal?: AbortSignal): Promise<SavedViewsPage> {
  const query = new URLSearchParams({ limit: '50' });
  if (cursor) query.set('cursor', cursor);
  return parseSavedViewsPage(await requestJson(`/saved/views?${query}`, signal), cursor);
}
export async function loadSavedView(id: string, signal?: AbortSignal): Promise<SavedView> {
  return parseSavedViewEnvelope(await requestJson(path(id), signal), id);
}
export async function createSavedView(command: SavedViewCreate): Promise<SavedView> {
  const view = parseSavedViewEnvelope(await requestJson('/saved/views', undefined, {
    method: 'POST', headers: writeHeaders, body: JSON.stringify(command),
  }), command.id);
  // Replay may return a later explicit rename, never another channel/query.
  if (view.channel_id !== command.channel_id || serializeCatalogueConditions(view.conditions) !== serializeCatalogueConditions(command.conditions)
    || (view.revision === 1 && view.name !== command.name)) throw new SavedWorkContractError('saved conditions do not match the command.');
  return view;
}
export async function renameSavedView(view: SavedView, name: string): Promise<SavedView> {
  const normalizedName = savedName(name);
  const updated = parseSavedViewEnvelope(await requestJson(path(view.id), undefined, {
    method: 'PATCH', headers: writeHeaders, body: JSON.stringify({ name: normalizedName, expected_revision: view.revision }),
  }), view.id);
  if (updated.channel_id !== view.channel_id || serializeCatalogueConditions(updated.conditions) !== serializeCatalogueConditions(view.conditions)
    || updated.revision <= view.revision || updated.name !== normalizedName) throw new SavedWorkContractError('rename was not confirmed.');
  return updated;
}
export async function deleteSavedView(view: SavedView): Promise<void> {
  const root = envelope(await requestJson(`${path(view.id)}?expected_revision=${savedRevision(view.revision)}`, undefined, { method: 'DELETE', headers: writeHeaders }));
  if (root.deleted !== true || savedId(root.id) !== view.id) throw new SavedWorkContractError('deletion was not confirmed.');
}
