import type { ChannelExplorerVideo } from '../types';
import { requestJson } from './api';
import { EXPLORER_V3_ROOT } from '../explorerRoutes';
import { parseStoredExplorerVideo } from './contracts';
import { SavedWorkContractError, savedId, savedName, savedRecord, savedRevision, savedTimestamp } from './savedWork';

const SAVED_VIEWS_PATH = `${EXPLORER_V3_ROOT}/saved`;

export const COLLECTION_COMMAND_SIZE = 500;
export const COLLECTIONS_INDEX = `${SAVED_VIEWS_PATH}?tab=collections`;
export const collectionDetailPath = (id: string) => `${SAVED_VIEWS_PATH}/collections/${savedId(id)}`;
export type SavedCollection = { id: string; kind: 'collection'; name: string; revision: number; member_count: number; created_at: string; updated_at: string };
export const COLLECTION_SOURCE_REASONS = {
  inventory_missing: 'This video is not in the tracked catalogue.',
  type_unverified: 'The video type has not been confirmed.',
  type_conflict: 'Conflicting video types need a manual decision.',
  type_unsupported: 'This video type is not available in Saved yet.',
  channel_unavailable: 'The source channel is hidden or unavailable.',
  source_missing: 'No current video data is stored.',
  source_mismatch: 'The stored video and channel do not match.',
  source_unavailable: 'The video is not currently available.',
  source_unpublished: 'The video has not been published yet.',
  source_stale: 'Current video data needs to be refreshed.',
  api_paused: 'YouTube data is temporarily unavailable. Your saved selection can still be edited.',
} as const;
export type CollectionSourceReason = keyof typeof COLLECTION_SOURCE_REASONS;
export type CollectionMember = { video_id: string; added_at: string } & (
  { status: 'available'; reason: null; channel_id: string; video: ChannelExplorerVideo; source_expires_at: string }
  | { status: 'unavailable'; reason: CollectionSourceReason; channel_id: null; video: null; source_expires_at: null }
);
export type CollectionPage = { collections: SavedCollection[]; next_cursor: string | null };
export type CollectionMembersPage = { collection: SavedCollection; members: CollectionMember[]; next_cursor: string | null; source_read_at: string | null };
export type CollectionMemberCommand = { request_id: string; expected_revision: number; video_ids: string[] };
export type CollectionMutation = { collection: SavedCollection; mutation: { request_id: string; replayed: boolean; requested_count: number; changed_count: number; applied_revision: number } };
const fail = (message: string): never => { throw new SavedWorkContractError(message); };
const count = (value: unknown): number => Number.isSafeInteger(value) && Number(value) >= 0 ? value as number : fail('invalid collection count.');
// YouTube data is kept 29 days from fetch (application/hot_storage.py RETENTION_HOURS);
// a member's deadline can be at most that far past its counter clock.
export const SOURCE_RETENTION_MS = 29 * 86_400_000;

function envelope(value: unknown): Record<string, unknown> {
  const root = savedRecord(value);
  if (root.contract_version !== 1) fail('unsupported collection contract.');
  return root;
}
export function collectionVideoId(value: unknown): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(value)) fail('invalid collection video ID.');
  return value as string;
}
export function collectionVideoIds(values: readonly string[]): string[] {
  if (!Array.isArray(values) || !values.length) fail('Select at least one video.');
  return [...new Set(values.map(collectionVideoId))].sort();
}
export function parseSavedCollection(value: unknown): SavedCollection {
  const row = savedRecord(value);
  if (row.kind !== 'collection') fail('invalid saved collection kind.');
  const created = savedTimestamp(row.created_at), updated = savedTimestamp(row.updated_at);
  if (Date.parse(updated) < Date.parse(created)) fail('invalid collection timestamps.');
  return { id: savedId(row.id), kind: 'collection', name: savedName(row.name), revision: savedRevision(row.revision), member_count: count(row.member_count), created_at: created, updated_at: updated };
}
export function parseCollectionEnvelope(value: unknown, expectedId?: string): SavedCollection {
  const collection = parseSavedCollection(envelope(value).collection);
  if (expectedId && collection.id !== savedId(expectedId)) fail('different collection identity.');
  return collection;
}
function cursor(value: unknown, length: number, previous?: string): string | null {
  if (value === null) return null;
  if (typeof value !== 'string' || !value.length || value.length > 4096 || /[\u0000-\u0020\u007f]/.test(value) || value === previous || !length) fail('invalid collection continuation cursor.');
  return value as string;
}
function pageRows(value: unknown): unknown[] {
  if (!Array.isArray(value) || value.length > 50) fail('invalid collection page.');
  return value as unknown[];
}
export function parseCollectionsPage(value: unknown, previous?: string): CollectionPage {
  const root = envelope(value), collections = pageRows(root.collections).map(parseSavedCollection);
  if (new Set(collections.map((row) => row.id)).size !== collections.length) fail('repeated collection.');
  return { collections, next_cursor: cursor(root.next_cursor, collections.length, previous) };
}
export function parseCollectionMember(value: unknown): CollectionMember {
  const row = savedRecord(value), video_id = collectionVideoId(row.video_id), added_at = savedTimestamp(row.added_at);
  if (Object.keys(row).some((field) => !['video_id', 'added_at', 'status', 'reason', 'channel_id', 'video', 'source_expires_at'].includes(field))) fail('unexpected collection member fields.');
  if (row.status === 'unavailable') {
    if (row.channel_id !== null || row.video !== null || row.source_expires_at !== null || typeof row.reason !== 'string' || !Object.hasOwn(COLLECTION_SOURCE_REASONS, row.reason)) fail('unavailable member exposed source fields.');
    return { video_id, added_at, status: 'unavailable', reason: row.reason as CollectionSourceReason, channel_id: null, video: null, source_expires_at: null };
  }
  if (row.status !== 'available' || row.reason !== null || typeof row.channel_id !== 'string' || !/^UC[A-Za-z0-9_-]{22}$/.test(row.channel_id)) fail('invalid available member.');
  const video = savedRecord(row.video);
  const fields = ['video_id', 'title', 'thumbnail_url', 'published_at', 'checked_at', 'duration', 'views', 'like_count', 'comment_count', 'category_id', 'category_name', 'duration_seconds'];
  if (fields.some((field) => !(field in video)) || Object.keys(video).some((field) => !fields.includes(field) && field !== 'source_expires_at') || video.video_id !== video_id) fail('invalid current raw video fields.');
  if (video.duration_seconds !== null && (typeof video.duration_seconds !== 'number' || !Number.isFinite(video.duration_seconds) || video.duration_seconds < 0)) fail('invalid raw duration.');
  const source_expires_at = savedTimestamp(row.source_expires_at);
  const counterClock = Date.parse(savedTimestamp(video.checked_at));
  if (Date.parse(source_expires_at) > counterClock + SOURCE_RETENTION_MS) fail('source deadline exceeds counter lifetime.');
  return { video_id, added_at, status: 'available', reason: null, channel_id: row.channel_id as string, video: parseStoredExplorerVideo(video), source_expires_at };
}
// Weak keys avoid introducing another durable store of source payloads. The
// monotonic budget is anchored to server time and subtracts the whole request;
// changing the browser wall clock backwards cannot extend source lifetime.
const sourceDeadlines = new WeakMap<CollectionMember, number>();
export function parseMembersPage(value: unknown, id: string, previous?: string, requestStarted = performance.now()): CollectionMembersPage {
  const root = envelope(value), collection = parseCollectionEnvelope(value, id), members = pageRows(root.members).map(parseCollectionMember);
  if (new Set(members.map((row) => row.video_id)).size !== members.length || members.length > collection.member_count) fail('invalid collection membership page.');
  if (!previous && root.next_cursor === null && members.length !== collection.member_count) fail('incomplete collection membership.');
  const source_read_at = savedTimestamp(root.source_read_at), readAt = Date.parse(source_read_at);
  for (const member of members) {
    if (member.status !== 'available') continue;
    if (Date.parse(member.video.checked_at!) > readAt || Date.parse(member.source_expires_at) < readAt) fail('source clocks exceed read boundary.');
    sourceDeadlines.set(member, requestStarted + Date.parse(member.source_expires_at) - readAt);
  }
  return expireCollectionSources({ collection, members, next_cursor: cursor(root.next_cursor, members.length, previous), source_read_at });
}
export function parseMemberIdentitiesPage(value: unknown, id: string, previous?: string): CollectionMembersPage {
  const root = envelope(value), collection = parseCollectionEnvelope(value, id);
  const members: CollectionMember[] = pageRows(root.members).map(value => {
    const row = savedRecord(value);
    if (Object.keys(row).some(field => !['video_id', 'added_at'].includes(field))) fail('unexpected saved identity fields.');
    return {video_id: collectionVideoId(row.video_id), added_at: savedTimestamp(row.added_at),
      status: 'unavailable', reason: 'api_paused', channel_id: null, video: null, source_expires_at: null};
  });
  if (new Set(members.map(row => row.video_id)).size !== members.length || members.length > collection.member_count) fail('invalid collection membership page.');
  if (!previous && root.next_cursor === null && members.length !== collection.member_count) fail('incomplete collection membership.');
  return {collection, members, next_cursor: cursor(root.next_cursor, members.length, previous), source_read_at: null};
}
export function withoutCollectionSources(page: CollectionMembersPage): CollectionMembersPage {
  return {...page, source_read_at: null, members: page.members.map(member => ({
    video_id: member.video_id, added_at: member.added_at, status: 'unavailable', reason: 'api_paused',
    channel_id: null, video: null, source_expires_at: null,
  }))};
}
export function mergeCollectionMembers(current: CollectionMembersPage, page: CollectionMembersPage): CollectionMembersPage {
  if (current.collection.id !== page.collection.id || current.collection.revision !== page.collection.revision || current.collection.member_count !== page.collection.member_count) fail('Collection changed. Reload before continuing.');
  const ids = new Set(current.members.map((row) => row.video_id));
  if (page.members.some((row) => ids.has(row.video_id)) || current.members.length + page.members.length > page.collection.member_count) fail('repeated or invalid collection members.');
  if (!page.next_cursor && current.members.length + page.members.length !== page.collection.member_count) fail('incomplete collection membership.');
  return { collection: page.collection, members: [...current.members, ...page.members], next_cursor: page.next_cursor, source_read_at: page.source_read_at };
}
export function collectionSourceDelay(page: CollectionMembersPage, now = Date.now(), monotonic = performance.now()): number | null {
  const delays = page.members.flatMap((member) => member.status === 'available'
    ? [Math.min(Date.parse(member.source_expires_at) - now, (sourceDeadlines.get(member) ?? Infinity) - monotonic)] : []);
  return delays.length ? Math.max(0, Math.min(...delays)) : null;
}
export function expireCollectionSources(page: CollectionMembersPage, now = Date.now(), monotonic = performance.now()): CollectionMembersPage {
  let changed = false;
  const members = page.members.map((member): CollectionMember => {
    if (member.status !== 'available' || (now < Date.parse(member.source_expires_at)
      && monotonic < (sourceDeadlines.get(member) ?? Infinity))) return member;
    changed = true;
    return { video_id: member.video_id, added_at: member.added_at, status: 'unavailable',
      reason: 'source_stale', channel_id: null, video: null, source_expires_at: null };
  });
  return changed ? { ...page, members } : page;
}
export function parseCollectionMutation(value: unknown, id: string, command: CollectionMemberCommand): CollectionMutation {
  const root = envelope(value), collection = parseCollectionEnvelope(value, id), receipt = savedRecord(root.mutation);
  const applied_revision = savedRevision(receipt.applied_revision), requested_count = count(receipt.requested_count), changed_count = count(receipt.changed_count);
  if (savedId(receipt.request_id) !== command.request_id || typeof receipt.replayed !== 'boolean' || requested_count !== command.video_ids.length
    // A command that changed no member keeps the revision it expected.
    || changed_count > requested_count || applied_revision !== command.expected_revision + (changed_count > 0 ? 1 : 0)
    || applied_revision > collection.revision) fail('membership write was not confirmed.');
  return { collection, mutation: { request_id: command.request_id, replayed: receipt.replayed as boolean, requested_count, changed_count, applied_revision } };
}
const headers = { 'Content-Type': 'application/json', 'X-Saved-Work': '1' };
const path = (id: string) => `/saved/collections/${savedId(id)}`;
const pageQuery = (value?: string) => { const query = new URLSearchParams({ limit: '50' }); if (value) query.set('cursor', value); return query; };
export async function loadSavedCollections(next?: string, signal?: AbortSignal): Promise<CollectionPage> {
  return parseCollectionsPage(await requestJson(`/saved/collections?${pageQuery(next)}`, signal), next);
}
export async function loadSavedCollection(id: string, signal?: AbortSignal): Promise<SavedCollection> { return parseCollectionEnvelope(await requestJson(path(id), signal), id); }
export async function loadCollectionMembers(id: string, next?: string, signal?: AbortSignal): Promise<CollectionMembersPage> {
  const requestStarted = performance.now();
  try {
    return parseMembersPage(await requestJson(`${path(id)}/members?${pageQuery(next)}`, signal), id, next, requestStarted);
  } catch (error) {
    if (!(error instanceof Error) || !('status' in error) || error.status !== 503
        || !('code' in error) || error.code !== 'hot_runtime_not_ready') throw error;
    return parseMemberIdentitiesPage(await requestJson(`${path(id)}/member-identities?${pageQuery(next)}`, signal), id, next);
  }
}
export async function createSavedCollection(command: { id: string; name: string }): Promise<SavedCollection> {
  const collection = parseCollectionEnvelope(await requestJson('/saved/collections', undefined, { method: 'POST', headers, body: JSON.stringify(command) }), command.id);
  if (collection.revision === 1 && (collection.member_count !== 0 || collection.name !== command.name)) fail('empty collection creation was not confirmed.');
  return collection;
}
export async function renameSavedCollection(collection: SavedCollection, name: string): Promise<SavedCollection> {
  const normalized = savedName(name);
  const updated = parseCollectionEnvelope(await requestJson(path(collection.id), undefined, { method: 'PATCH', headers, body: JSON.stringify({ name: normalized, expected_revision: collection.revision }) }), collection.id);
  if (updated.name !== normalized || updated.revision !== collection.revision + 1 || updated.member_count !== collection.member_count) fail('rename was not confirmed.');
  return updated;
}
export async function deleteSavedCollection(collection: SavedCollection): Promise<void> {
  const root = envelope(await requestJson(`${path(collection.id)}?expected_revision=${collection.revision}`, undefined, { method: 'DELETE', headers }));
  if (root.deleted !== true || savedId(root.id) !== collection.id) fail('collection deletion was not confirmed.');
}
export async function mutateCollectionMembers(id: string, command: CollectionMemberCommand, mode: 'add' | 'remove'): Promise<CollectionMutation> {
  if (!command.video_ids.length || command.video_ids.length > COLLECTION_COMMAND_SIZE || collectionVideoIds(command.video_ids).length !== command.video_ids.length) fail('invalid membership command size.');
  return parseCollectionMutation(await requestJson(`${path(id)}/members${mode === 'remove' ? '/remove' : ''}`, undefined, { method: 'POST', headers, body: JSON.stringify(command) }), id, command);
}
