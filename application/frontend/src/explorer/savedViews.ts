import { requestJson } from './api';
import { parseResearchDefinition,parseSavedCanvas,type ResearchDefinition,type SavedCanvas } from './researchGraph';
import { parseSavedDefinition, canonicalSaved, type SavedQueryDefinition } from './savedQueryDefinition';
import { EXPLORER_V3_ROOT, explorerChannelPath, parseExplorerChannelPath, parseExplorerOperationalPath, type ExplorerOperationalPage } from '../explorerRoutes';
import { MAX_CATALOGUE_QUERY_LENGTH, normalizeCatalogueConditions, serializeCatalogueConditions, writeCatalogueUrl, type CatalogueConditions } from './catalogueQuery';
import { SavedWorkContractError, newSavedWorkId, savedId, savedName, savedRecord, savedRevision, savedTimestamp } from './savedWork';
import { TIME_ZONE_PATTERN } from './timeZones';
import { recognizeLibraryView, savedLibraryVideoType, type LibraryOpenTarget } from './libraryData/savedLibrary.ts';

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
/** A Saved v1 view: Library conditions; the server may also keep the zone its dates were counted in (`facets.time_zone`). */
export type SavedLibraryConditions = Omit<CatalogueConditions, 'facets'> & { facets: CatalogueConditions['facets'] & { time_zone?: string } };
export type SavedConditions = SavedLibraryConditions | SavedQueryDefinition | ResearchDefinition | SavedCanvas;
export type SavedView = {
  id: string; kind: 'view'; name: string; channel_id: string | null; conditions: SavedConditions;
  revision: number; created_at: string; updated_at: string; channel_available: boolean;
};
/**
 * A listed item this version of the page cannot read, for example one saved by a newer
 * version or after an update of the server only. It is shown by its name when that reads, and never
 * opened, renamed or deleted: deleting cannot be undone, and a reload often makes it readable.
 */
export type UnreadableSavedView = { kind: 'unreadable'; id: string | null; name: string | null };
export const UNREADABLE_SAVED_ITEM = 'This item cannot be opened by this version of the page. Reload the page; if it stays, it was saved by a different version.';
export type SavedViewsPage = { contract_version: 1; views: (SavedView | UnreadableSavedView)[]; next_cursor: string | null };
export type SavedViewCreate = Pick<SavedView, 'id' | 'name' | 'channel_id' | 'conditions'>;
/** What Save view stores: Library's filters (v1) or, from the Videos tab, the shared query with its presentation. */
export type SavedViewSnapshot = { channelId: string; channelName?: string; conditions: CatalogueConditions | SavedQueryDefinition };
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
  let name: string, conditions: SavedConditions;
  try { name = savedName(row.name); conditions = normalizeSavedConditions(row.conditions); serializeSavedConditions(conditions); }
  catch { throw new SavedWorkContractError('invalid name or catalogue conditions.'); }
  const created = savedTimestamp(row.created_at), updated = savedTimestamp(row.updated_at);
  if (Date.parse(updated) < Date.parse(created)) throw new SavedWorkContractError('invalid revision timestamps.');
  return { id: savedId(row.id), kind: 'view', name, channel_id: row.channel_id === null ? null : channelId(row.channel_id), conditions,
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
  // One item at a time: an item the page cannot read is listed as such, the others stay usable.
  // The page's own checks (size, cursor, repeated identity) still refuse the whole page.
  const views = root.views.map(readSavedListItem);
  const ids = views.flatMap((view) => (view.id === null ? [] : [view.id]));
  if (new Set(ids).size !== ids.length) throw new SavedWorkContractError('duplicate saved identity.');
  return { contract_version: 1, views, next_cursor: root.next_cursor as string | null };
}

function readSavedListItem(value: unknown): SavedView | UnreadableSavedView {
  try { return parseSavedView(value); } catch {
    const row = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
    let id: string | null = null, name: string | null = null;
    try { id = savedId(row.id); } catch { /* left out, never invented */ }
    try { name = savedName(row.name); } catch { /* left out, never invented */ }
    return { kind: 'unreadable', id, name };
  }
}

/** The same frozen payload reuses its UUID after an uncertain response. */
export function prepareSavedViewCreate(previous: SavedViewCreate | undefined, snapshot: SavedViewSnapshot, name: string, uuid: () => string = newSavedWorkId): SavedViewCreate {
  const conditions: SavedConditions = snapshot.conditions.version === 2 ? parseSavedDefinition(snapshot.conditions) : normalizeCatalogueConditions(snapshot.conditions);
  const next = { name: savedName(name), channel_id: channelId(snapshot.channelId), conditions };
  serializeSavedConditions(next.conditions);
  if (previous && previous.name === next.name && previous.channel_id === next.channel_id
    && serializeSavedConditions(previous.conditions) === serializeSavedConditions(next.conditions)) return previous;
  return { id: savedId(uuid()), ...next };
}
type OpenableView = Pick<SavedView, 'channel_id' | 'conditions' | 'id'> & { channel_available?: boolean };
/**
 * Where a Saved item opens. v1: the channel's Library Videos with its conditions.
 * Library v2 (`presentation.surface: "library"`): the channel's Library tab of its
 * type with the recognised conditions; a view Library cannot show, or whose
 * channel is unavailable, opens in the workshop. v2 queries and v3 studies: the workshop.
 */
export function savedLibraryTarget(view: OpenableView, origin: string): LibraryOpenTarget {
  const workshop = (reason: string): LibraryOpenTarget => {
    const target = new URL(RESEARCH_WORKSHOP_PATH, origin); target.searchParams.set('saved', savedId(view.id));
    return { kind: 'workshop', url: target.href, reason };
  };
  if (view.conditions.version === 1) {
    const { conditions, savedZone } = splitSavedLibraryConditions(view.conditions), channel = channelId(view.channel_id);
    const videoType = savedLibraryVideoType(conditions);
    const url = writeCatalogueUrl(new URL(explorerChannelPath(channel), origin), { section: 'videos', overviewPeriod: 'max', type: videoType, conditions });
    return { kind: 'channel', channelId: channel, videoType, url: url.href, conditions, savedZone };
  }
  if (view.conditions.version === 3) return workshop('A research study opens in the workshop.');
  if (view.conditions.version === 4) return workshop('A research canvas opens in the workshop.');
  const recognized = recognizeLibraryView(view.conditions);
  if (recognized.status !== 'recognized') return workshop(recognized.reason);
  if (view.channel_id === null || view.channel_available === false) return workshop('The source channel is unavailable.');
  try {
    const channel = channelId(view.channel_id);
    const url = writeCatalogueUrl(new URL(explorerChannelPath(channel), origin), {
      section: 'videos', overviewPeriod: 'max', type: recognized.videoType, conditions: recognized.conditions,
    });
    return { kind: 'channel', channelId: channel, videoType: recognized.videoType, url: url.href, conditions: recognized.conditions, savedZone: recognized.savedZone };
  } catch { return workshop('Library filters cannot show these conditions.'); }
}
export function savedViewUrl(view: OpenableView, origin: string): URL {
  return new URL(savedLibraryTarget(view, origin).url);
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
  if (view.channel_id !== command.channel_id || serializeSavedConditions(view.conditions) !== serializeSavedConditions(command.conditions)
    || (view.revision === 1 && view.name !== command.name)) throw new SavedWorkContractError('saved conditions do not match the command.');
  return view;
}
export async function renameSavedView(view: SavedView, name: string): Promise<SavedView> {
  const normalizedName = savedName(name);
  const updated = parseSavedViewEnvelope(await requestJson(path(view.id), undefined, {
    method: 'PATCH', headers: writeHeaders, body: JSON.stringify({ name: normalizedName, expected_revision: view.revision }),
  }), view.id);
  if (updated.channel_id !== view.channel_id || serializeSavedConditions(updated.conditions) !== serializeSavedConditions(view.conditions)
    || updated.revision <= view.revision || updated.name !== normalizedName) throw new SavedWorkContractError('rename was not confirmed.');
  return updated;
}
export async function deleteSavedView(view: SavedView): Promise<void> {
  const root = envelope(await requestJson(`${path(view.id)}?expected_revision=${savedRevision(view.revision)}`, undefined, { method: 'DELETE', headers: writeHeaders }));
  if (root.deleted !== true || savedId(root.id) !== view.id) throw new SavedWorkContractError('deletion was not confirmed.');
}

export function normalizeSavedConditions(value: unknown): SavedConditions {
  if (savedRecord(value).version === 4) return parseSavedCanvas(value);
  return savedRecord(value).version===3 ? parseResearchDefinition(value) : savedRecord(value).version === 2 ? parseSavedDefinition(value) : normalizeSavedLibraryConditions(value);
}
export function serializeSavedConditions(value: SavedConditions): string {
  if (value.version === 4) return canonicalSaved(parseSavedCanvas(value));
  if (value.version === 3) return canonicalSaved(parseResearchDefinition(value));
  if (value.version === 2) return canonicalSaved(parseSavedDefinition(value));
  const { conditions, savedZone } = splitSavedLibraryConditions(normalizeSavedLibraryConditions(value));
  if (!savedZone) return serializeCatalogueConditions(conditions);
  const serialized = JSON.stringify({ ...conditions, facets: { ...conditions.facets, time_zone: savedZone } });
  if (serialized.length > MAX_CATALOGUE_QUERY_LENGTH) throw new SavedWorkContractError('saved conditions exceed the supported size.');
  return serialized;
}
/**
 * The server keeps `facets.time_zone` in a v1 view (a non-UTC IANA zone,
 * catalogue_filters.normalize_catalogue_filters); the browser codec must accept
 * it, or one such object makes the whole Saved list and a backup unreadable.
 * The zone is kept as stored (a backup round trip does not drop it).
 */
export function normalizeSavedLibraryConditions(value: unknown): SavedLibraryConditions {
  const row = savedRecord(value), facets = row.facets;
  if (!facets || typeof facets !== 'object' || Array.isArray(facets) || !Object.hasOwn(facets, 'time_zone')) return normalizeCatalogueConditions(value);
  const { time_zone: zone, ...rest } = facets as Record<string, unknown>;
  if (typeof zone !== 'string' || zone.length > 64 || zone === 'UTC' || !TIME_ZONE_PATTERN.test(zone)) throw new SavedWorkContractError('invalid saved time zone.');
  const conditions = normalizeCatalogueConditions({ ...row, facets: rest });
  return { ...conditions, facets: { ...conditions.facets, time_zone: zone } };
}
/** The Library part of a v1 view (an address carries no zone) and the zone it was saved with, if any. */
export function splitSavedLibraryConditions(value: SavedLibraryConditions): { conditions: CatalogueConditions; savedZone: string | null } {
  const { time_zone: zone, ...facets } = value.facets;
  return { conditions: { ...value, facets }, savedZone: zone ?? null };
}

/** Edit a study (v3) or a canvas (v4) in place; the server refuses turning one into the other. */
export async function updateSavedResearch(view:SavedView,name:string,conditions:ResearchDefinition|SavedCanvas):Promise<SavedView>{
  const normalized=conditions.version===4?parseSavedCanvas(conditions):parseResearchDefinition(conditions);
  const updated=parseSavedViewEnvelope(await requestJson(path(view.id),undefined,{method:'PATCH',headers:writeHeaders,body:JSON.stringify({name:savedName(name),conditions:normalized,expected_revision:view.revision})}),view.id);
  if(updated.revision!==view.revision+1||serializeSavedConditions(updated.conditions)!==canonicalSaved(normalized)||updated.channel_id!==view.channel_id||updated.name!==savedName(name))throw new SavedWorkContractError('research update was not confirmed.');
  return updated;
}

// The research workshop's address: /research, ?saved=<id>, ?settings=fields (Library's link to Collection fields).
export const RESEARCH_WORKSHOP_PATH = `${EXPLORER_V3_ROOT}/research`;
export function readWorkshopAddress(search: string): { saved: string | null; error?: string; fieldSettings: boolean } {
  const parameters = new URLSearchParams(search), saved = parameters.getAll('saved');
  const fieldSettings = parameters.getAll('settings').includes('fields');
  if (!saved.length) return { saved: null, fieldSettings };
  // A repeated or malformed id is an address error, never a guess between items.
  try { if (saved.length !== 1) throw new Error(); return { saved: savedId(saved[0]), fieldSettings }; }
  catch { return { saved: null, error: 'Invalid saved query address.', fieldSettings }; }
}
/** Where a Saved item opens: v1 and Library v2 views in Library, a v2 query as a first study, v3 as itself. */
export function savedOpenTarget(conditions: SavedView['conditions']): 'library' | 'query_study' | 'study' | 'canvas' {
  if (conditions.version === 4) return 'canvas';
  if (conditions.version === 3) return 'study';
  if (conditions.version !== 2) return 'library';
  return (conditions as { presentation?: { surface?: unknown } }).presentation?.surface === 'library' ? 'library' : 'query_study';
}
