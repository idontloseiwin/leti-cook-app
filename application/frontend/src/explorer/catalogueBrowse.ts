import type { ChannelExplorerPayload, ChannelExplorerQuery, ChannelVideoGrowthPayload } from '../types';
import { ExplorerContractError, parseRawExplorerV1, parseVideoGrowthV1 } from './contracts.ts';
import { validateCatalogueEcho } from './catalogueFacets.ts';
import { normalizeCatalogueTimestamp } from './catalogueQuery.ts';

export type BrowseDirection = 'next' | 'previous';
export type CatalogueAnchor = { videoId: string; offsetPx: number };
export type CatalogueRestoreAnchor = CatalogueAnchor & { requestKey: string };
export type CatalogueBrowseMetadata = {
  contract_version: 1;
  session_id: string;
  created_at: string;
  expires_at: string;
  next_cursor: string | null;
  previous_cursor: string | null;
  start_index: number;
  total_count: number;
  anchor_video_id: string | null;
  consistency: 'frozen_assembled_rows';
};
export type CatalogueBrowsePayload = (ChannelExplorerPayload | ChannelVideoGrowthPayload) & {
  browse: CatalogueBrowseMetadata;
  videos: NonNullable<ChannelExplorerPayload['videos']>;
};
export type CatalogueBrowseQuery = ChannelExplorerQuery & { cursor?: string; anchor_video_id?: string };

function fail(message: string): never {
  throw new ExplorerContractError(`The server returned an incompatible catalogue browse contract: ${message}.`);
}
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('expected an object');
  return value as Record<string, unknown>;
}
const count = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const token = (value: unknown): value is string => typeof value === 'string' && value.length > 0
  && value.length <= 4096 && !/[\u0000-\u0020\u007f]/.test(value);

export function parseCatalogueBrowse(
  input: unknown,
  source: 'raw' | 'growth',
  channelId: string,
  query: CatalogueBrowseQuery,
): CatalogueBrowsePayload {
  const payload = source === 'growth' ? parseVideoGrowthV1(input, channelId) : parseRawExplorerV1(input);
  if (payload.channel?.channel_id !== channelId) fail('different channel');
  if (query.catalogue_filters) validateCatalogueEcho(payload, query.catalogue_filters);
  const root = record(payload), browse = record(root.browse), pagination = record(root.pagination);
  const coverage = record(root.coverage);
  if (browse.contract_version !== 1 || browse.consistency !== 'frozen_assembled_rows'
    || !token(browse.session_id) || !count(browse.start_index) || !count(browse.total_count)
    || !(browse.next_cursor === null || token(browse.next_cursor))
    || !(browse.previous_cursor === null || token(browse.previous_cursor))
    || !(browse.anchor_video_id === null || token(browse.anchor_video_id))
    || coverage.scan_mode !== 'full_catalogue' || coverage.scan_limit !== null || coverage.truncated !== false) fail('missing full-catalogue session metadata');
  try {
    normalizeCatalogueTimestamp(browse.created_at, 'created_at');
    normalizeCatalogueTimestamp(browse.expires_at, 'expires_at');
  } catch { fail('invalid session timestamps'); }
  if (Date.parse(String(browse.expires_at)) <= Date.parse(String(browse.created_at))) fail('invalid expiry');
  const videos = payload.videos ?? [];
  const seen = new Set<string>();
  for (const video of videos) {
    if (!token(video.video_id) || seen.has(video.video_id)) fail('invalid or duplicate video identity');
    seen.add(video.video_id);
  }
  if (videos.length > (query.limit ?? 50) || pagination.returned_count !== videos.length
    || pagination.offset !== browse.start_index || pagination.matched_count !== browse.total_count
    || browse.start_index + videos.length > browse.total_count
    || (browse.next_cursor !== null) !== (browse.start_index + videos.length < browse.total_count)
    || (browse.previous_cursor !== null) !== (browse.start_index > 0)
    || (browse.total_count > 0 && videos.length === 0)) fail('inconsistent page bounds');
  if (query.cursor && (browse.next_cursor === query.cursor || browse.previous_cursor === query.cursor)) fail('cursor did not advance');
  return payload as CatalogueBrowsePayload;
}

function queryIdentity(payload: CatalogueBrowsePayload): string {
  const { limit: _limit, offset: _offset, ...filters } = payload.filters ?? {};
  return JSON.stringify(Object.keys(filters).sort().map((key) => [key, (filters as Record<string, unknown>)[key]]));
}

/** Merge adjacent immutable pages. Never deduplicate around a gap or mix sources. */
export function mergeCatalogueBrowse(
  current: CatalogueBrowsePayload,
  page: CatalogueBrowsePayload,
  direction: BrowseDirection,
): CatalogueBrowsePayload {
  const before = current.browse, next = page.browse;
  if (before.session_id !== next.session_id || before.created_at !== next.created_at
    || before.expires_at !== next.expires_at || before.total_count !== next.total_count
    || current.channel?.channel_id !== page.channel?.channel_id
    || current.source?.kind !== page.source?.kind || queryIdentity(current) !== queryIdentity(page)) fail('page belongs to a different session or query');
  if (direction === 'next'
    ? next.start_index !== before.start_index + current.videos.length
    : next.start_index + page.videos.length !== before.start_index) fail('non-adjacent page');
  const ids = new Set(current.videos.map((video) => video.video_id));
  if (page.videos.some((video) => ids.has(video.video_id))) fail('repeated video across pages');
  const videos = direction === 'next' ? [...current.videos, ...page.videos] : [...page.videos, ...current.videos];
  const browse = {
    ...before,
    start_index: direction === 'previous' ? next.start_index : before.start_index,
    previous_cursor: direction === 'previous' ? next.previous_cursor : before.previous_cursor,
    next_cursor: direction === 'next' ? next.next_cursor : before.next_cursor,
  };
  return {
    ...current,
    videos,
    browse,
    pagination: { ...current.pagination, offset: browse.start_index, returned_count: videos.length, has_more_in_scan: browse.next_cursor !== null },
  } as CatalogueBrowsePayload;
}

/** Identity fence shared by initial, next and previous requests. */
export class CatalogueRequestGate {
  private generation = 0;
  private nextRequest = 0;
  private active: { generation: number; request: number } | null = null;
  reset(): void { this.generation += 1; this.active = null; }
  begin(): { generation: number; request: number } | null {
    if (this.active) return null;
    this.active = { generation: this.generation, request: ++this.nextRequest };
    return this.active;
  }
  accepts(ticket: { generation: number; request: number }): boolean { return this.active === ticket && ticket.generation === this.generation; }
  finish(ticket: { generation: number; request: number }): boolean {
    if (!this.accepts(ticket)) return false;
    this.active = null;
    return true;
  }
}
