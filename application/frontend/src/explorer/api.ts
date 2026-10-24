import { captureSourceLifetime, sourceLifetimeParser } from '../sourceLifetime.ts';
import { serverErrorMessage } from '../serverText.ts';
import { parseChannelPublications, type ChannelPublicationsPage } from './publicationStatus';
import type {
  ChannelBaselinePayload,
  ChannelBaselineQuery,
  ExplorerChannelLookupResult,
  ChannelExplorerCounterDynamicsPeriodsPayload,
  ChannelExplorerFormatTotalsPayload,
  ChannelExplorerProfilePayload,
  ChannelExplorerQuery,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder as ChannelFolder,
  SidebarData,
  SystemSummary,
  VideoStaticDetailsPayload,
  VideoHistoryPayload,
  VideoLifetimeBaselinePayload,
  VideoLaunchPayload,
} from '../types';
import {
  ExplorerContractError,
  parseBaselineV1 as decodeBaselineV1,
  parseExplorerAddChannel as decodeExplorerAddChannel,
  parseExplorerHydration as decodeExplorerHydration,
  parseExplorerChannelLookup as decodeExplorerChannelLookup,
  parseExplorerCapabilities as decodeExplorerCapabilities,
  parseExplorerHomeSummary as decodeExplorerHomeSummary,
  parseExplorerFolder as decodeExplorerFolder,
  parseExplorerFormatTotalsV1 as decodeExplorerFormatTotalsV1,
  parseExplorerLabel as decodeExplorerLabel,
  parseExplorerProfileV1 as decodeExplorerProfileV1,
  parseExplorerCounterDynamicsPeriodsV1 as decodeExplorerCounterDynamicsPeriodsV1,
  parseExplorerRecentVideos as decodeExplorerRecentVideos,
  parseExplorerSystemSummary as decodeExplorerSystemSummary,
  parseSidebarData as decodeSidebarData,
  parseVideoStaticDetailsV1 as decodeVideoStaticDetailsV1,
} from './contracts';
import type {
  ExplorerAddChannelResult,
  ExplorerCapabilities,
  ExplorerHomeSummary,
  ExplorerHydrationEstimate,
  ExplorerHydrationPayload,
  ExplorerRecentVideosPage,
  ExplorerRecentWindow,
} from './model';

import { parseCatalogueFacets as decodeCatalogueFacets } from './catalogueFacets';
import { parseVideoHistoryV1 as decodeVideoHistoryV1 } from './videoHistory';
import { parseVideoLifetimeBaselineV1 as decodeVideoLifetimeBaselineV1 } from './videoLifetimeBaseline';
import { parseVideoLaunchV1 as decodeVideoLaunchV1 } from './videoLaunch';
import { parseCatalogueBrowse as decodeCatalogueBrowse, type CatalogueBrowsePayload, type CatalogueBrowseQuery } from './catalogueBrowse';
const parseBaselineV1 = sourceLifetimeParser(decodeBaselineV1);
const parseCatalogueBrowse = sourceLifetimeParser(decodeCatalogueBrowse);
const parseCatalogueFacets = sourceLifetimeParser(decodeCatalogueFacets);
const parseExplorerAddChannel = sourceLifetimeParser(decodeExplorerAddChannel);
const parseExplorerCapabilities = sourceLifetimeParser(decodeExplorerCapabilities);
const parseExplorerChannelLookup = sourceLifetimeParser(decodeExplorerChannelLookup);
const parseExplorerCounterDynamicsPeriodsV1 = sourceLifetimeParser(decodeExplorerCounterDynamicsPeriodsV1);
const parseExplorerFolder = sourceLifetimeParser(decodeExplorerFolder);
const parseExplorerFormatTotalsV1 = sourceLifetimeParser(decodeExplorerFormatTotalsV1);
const parseExplorerHomeSummary = sourceLifetimeParser(decodeExplorerHomeSummary);
const parseExplorerHydration = sourceLifetimeParser(decodeExplorerHydration);
const parseExplorerLabel = sourceLifetimeParser(decodeExplorerLabel);
const parseExplorerProfileV1 = sourceLifetimeParser(decodeExplorerProfileV1);
const parseExplorerRecentVideos = sourceLifetimeParser(decodeExplorerRecentVideos);
const parseExplorerSystemSummary = sourceLifetimeParser(decodeExplorerSystemSummary);
const parseSidebarData = sourceLifetimeParser(decodeSidebarData);
const parseVideoHistoryV1 = sourceLifetimeParser(decodeVideoHistoryV1);
const parseVideoLaunchV1 = sourceLifetimeParser(decodeVideoLaunchV1);
const parseVideoLifetimeBaselineV1 = sourceLifetimeParser(decodeVideoLifetimeBaselineV1);
const parseVideoStaticDetailsV1 = sourceLifetimeParser(decodeVideoStaticDetailsV1);
const MAX_ERROR_LENGTH = 360;
const REQUEST_TIMEOUT_MS = 120_000;

export class ExplorerApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message.slice(0, MAX_ERROR_LENGTH));
    this.name = 'ExplorerApiError';
    this.status = status;
    this.code = code;
  }
}

function safeText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const normalized = value.trim().replace(/\s+/g, ' ');
  return normalized ? normalized.slice(0, MAX_ERROR_LENGTH) : undefined;
}

function validationMessage(detail: unknown): string | undefined {
  if (!Array.isArray(detail)) return undefined;
  const messages = detail.slice(0, 3).flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const record = item as { loc?: unknown; msg?: unknown };
    const message = safeText(record.msg);
    if (!message) return [];
    const location = Array.isArray(record.loc)
      ? record.loc.filter((part) => typeof part === 'string' || typeof part === 'number')
        .slice(-2)
        .join('.')
      : '';
    return [location ? `${location}: ${message}` : message];
  });
  return messages.length ? messages.join(' · ') : undefined;
}

function errorDetails(payload: unknown): { message?: string; code?: string } {
  if (!payload || typeof payload !== 'object') return {};
  const record = payload as {
    error?: unknown;
    message?: unknown;
    code?: unknown;
    detail?: unknown;
  };
  const detail = record.detail;
  const detailRecord = detail && typeof detail === 'object' && !Array.isArray(detail)
    ? detail as { message?: unknown; code?: unknown }
    : undefined;
  return {
    message:
      safeText(record.error)
      ?? safeText(record.message)
      ?? safeText(detail)
      ?? safeText(detailRecord?.message)
      ?? validationMessage(detail),
    code: safeText(record.code) ?? safeText(detailRecord?.code),
  };
}

export async function requestJson(
  path: string,
  signal?: AbortSignal,
  init?: RequestInit,
): Promise<unknown> {
  const requestStarted = performance.now();
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  try {
    const headers = new Headers(init?.headers);
    headers.set('Accept', 'application/json');
    const response = await fetch(path, {
      ...init,
      cache: 'no-store',
      headers,
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => undefined);
    if (!response.ok) {
      const details = errorDetails(payload);
      throw new ExplorerApiError(
        serverErrorMessage(response.status, details.code, details.message),
        response.status,
        details.code,
      );
    }
    return captureSourceLifetime(payload, response.headers, requestStarted);
  } catch (error) {
    if (timedOut && isAbortError(error)) {
      throw new ExplorerApiError(
        'The Explorer request timed out. Try again.',
        408,
        'client_timeout',
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abortFromCaller);
  }
}

function queryString(query: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params.set(key, String(value));
  }
  const rendered = params.toString();
  return rendered ? `?${rendered}` : '';
}

export async function loadExplorerCapabilities(signal?: AbortSignal): Promise<ExplorerCapabilities> {
  return parseExplorerCapabilities(await requestJson('/explorer/capabilities', signal));
}

export async function loadExplorerSidebar(signal?: AbortSignal): Promise<SidebarData> {
  return parseSidebarData(await requestJson('/sidebar-data', signal));
}

/** Local identities and organization are usable while YouTube reads are paused. */
export async function loadExplorerUserSidebar(signal?: AbortSignal): Promise<SidebarData> {
  const value = decodeSidebarData(await requestJson('/sidebar-user-data', signal));
  return {
    channels: value.channels?.map(({ channel_id, folder_id, label_keys, added_at }) => ({ channel_id, folder_id, label_keys, added_at })),
    folders: value.folders?.map(({ id, name }) => ({ id, name })),
    labels: value.labels?.map(({ label_key, name, label_type }) => ({ label_key, name, label_type })),
  };
}

export async function addExplorerChannel(
  url: string,
  signal?: AbortSignal,
): Promise<ExplorerAddChannelResult> {
  const result = parseExplorerAddChannel(await requestJson('/add-channel', signal, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  }));
  if (result.error || result.detail) {
    throw new ExplorerApiError(
      serverErrorMessage(422, result.code, result.error || result.detail),
      422,
      result.code ?? 'channel_not_found',
    );
  }
  return result;
}

function explorerHydrationPath(channelId: string): string {
  return `/channels/${encodeURIComponent(channelId)}/hydration`;
}

export async function loadExplorerHydration(
  channelId: string,
  signal?: AbortSignal,
): Promise<ExplorerHydrationPayload> {
  return parseExplorerHydration(
    await requestJson(explorerHydrationPath(channelId), signal),
    channelId,
  );
}

export async function confirmExplorerHydration(
  channelId: string,
  estimate: ExplorerHydrationEstimate,
  retryRequestId: string,
  signal?: AbortSignal,
): Promise<ExplorerHydrationPayload> {
  return parseExplorerHydration(await requestJson(
    explorerHydrationPath(channelId),
    signal,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_items: estimate.items,
        expected_pages: estimate.pages,
        expected_quota_units: estimate.quota_units,
        retry_request_id: retryRequestId,
        confirmed: true,
      }),
    },
  ), channelId);
}

export async function lookupExplorerChannelByVideo(
  videoId: string,
  signal?: AbortSignal,
): Promise<ExplorerChannelLookupResult> {
  return parseExplorerChannelLookup(await requestJson(
    `/explorer/channel-library/video-channel${queryString({ video_id: videoId })}`,
    signal,
  ));
}

export async function loadExplorerHomeSummary(signal?: AbortSignal): Promise<ExplorerHomeSummary> {
  return parseExplorerHomeSummary(await requestJson('/explorer/home-summary', signal));
}

export async function loadExplorerSystemSummary(signal?: AbortSignal): Promise<SystemSummary> {
  return parseExplorerSystemSummary(await requestJson('/system-summary', signal));
}

export async function loadExplorerRecentVideos(
  window: ExplorerRecentWindow,
  cursor?: string,
  signal?: AbortSignal,
): Promise<ExplorerRecentVideosPage> {
  return parseExplorerRecentVideos(
    await requestJson(`/explorer/recent-videos${queryString({ window, cursor })}`, signal),
  );
}

export async function loadVideoHistory(channelId: string, videoId: string, browseSession: string, signal?: AbortSignal): Promise<VideoHistoryPayload> {
  if (!browseSession) throw new ExplorerContractError('A catalogue browsing session is required for video history.');
  return parseVideoHistoryV1(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/videos/long/${encodeURIComponent(videoId)}/history${queryString({ browse_session: browseSession })}`, signal,
  ), channelId, videoId, browseSession);
}

export async function loadVideoStaticDetails(channelId: string, videoId: string, browseSession: string, signal?: AbortSignal): Promise<VideoStaticDetailsPayload> {
  if (!browseSession) throw new ExplorerContractError('A catalogue browsing session is required for video details.');
  return parseVideoStaticDetailsV1(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/videos/long/${encodeURIComponent(videoId)}/static-details${queryString({ browse_session: browseSession })}`, signal,
  ), channelId, videoId, browseSession);
}

export async function loadVideoLifetimeBaseline(channelId: string, videoId: string, browseSession: string, signal?: AbortSignal): Promise<VideoLifetimeBaselinePayload> {
  if (!browseSession) throw new ExplorerContractError('A catalogue browsing session is required for the channel baseline.');
  return parseVideoLifetimeBaselineV1(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/videos/long/${encodeURIComponent(videoId)}/lifetime-baseline${queryString({ browse_session: browseSession })}`, signal,
  ), channelId, videoId, browseSession);
}

export async function loadVideoLaunch(channelId: string, videoId: string, browseSession: string, signal?: AbortSignal): Promise<VideoLaunchPayload> {
  if (!browseSession) throw new ExplorerContractError('A catalogue browsing session is required for launch performance.');
  return parseVideoLaunchV1(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/videos/long/${encodeURIComponent(videoId)}/launch${queryString({ browse_session: browseSession })}`, signal,
  ), channelId, videoId, browseSession);
}

export async function loadRawExplorerBrowse(channelId: string, query: CatalogueBrowseQuery, signal?: AbortSignal): Promise<CatalogueBrowsePayload> {
  return parseCatalogueBrowse(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/videos/long/browse${queryString(query)}`, signal,
  ), 'raw', channelId, query);
}

export async function loadExplorerVideoGrowthBrowse(channelId: string, query: CatalogueBrowseQuery, signal?: AbortSignal): Promise<CatalogueBrowsePayload> {
  return parseCatalogueBrowse(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/video-growth/browse${queryString(query)}`, signal,
  ), 'growth', channelId, query);
}

export async function loadCatalogueFacets(channelId: string, query: ChannelExplorerQuery, signal?: AbortSignal) {
  const payload = parseCatalogueFacets(
    await requestJson(`/channels/${encodeURIComponent(channelId)}/explorer/videos/long/filter-facets${queryString(query)}`, signal),
    channelId, query.catalogue_filters ?? '',
  );
  if (query.full_catalogue && payload.coverage.scan_mode !== 'full_catalogue') {
    throw new ExplorerContractError('The server did not return full-catalogue filter counts.');
  }
  return payload;
}

export async function loadChannelPublications(channelId: string, cursor?: string, signal?: AbortSignal): Promise<ChannelPublicationsPage> {
  return parseChannelPublications(await requestJson(
    `/channels/${encodeURIComponent(channelId)}/explorer/publications${queryString({ cursor })}`, signal,
  ), channelId, cursor);
}

export async function loadExplorerProfile(
  channelId: string,
  signal?: AbortSignal,
): Promise<ChannelExplorerProfilePayload> {
  const encoded = encodeURIComponent(channelId);
  return parseExplorerProfileV1(
    await requestJson(`/channels/${encoded}/explorer/profile`, signal),
    channelId,
  );
}

export async function loadExplorerFormatTotals(
  channelId: string,
  signal?: AbortSignal,
): Promise<ChannelExplorerFormatTotalsPayload> {
  const encoded = encodeURIComponent(channelId);
  return parseExplorerFormatTotalsV1(
    await requestJson(`/channels/${encoded}/explorer/format-totals?include_typed=true`, signal),
    channelId,
  );
}

export async function loadExplorerCounterDynamicsPeriods(
  channelId: string,
  signal?: AbortSignal,
): Promise<ChannelExplorerCounterDynamicsPeriodsPayload> {
  const encoded = encodeURIComponent(channelId);
  return parseExplorerCounterDynamicsPeriodsV1(
    await requestJson(`/channels/${encoded}/explorer/counter-dynamics`, signal),
    channelId,
  );
}

export async function createExplorerFolder(
  name: string,
  signal?: AbortSignal,
): Promise<CreatedFolder> {
  return parseExplorerFolder(await requestJson('/folders', signal, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }));
}

export async function createExplorerLabel(
  name: string,
  signal?: AbortSignal,
): Promise<CreatedLabel> {
  return parseExplorerLabel(await requestJson('/labels', signal, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  }));
}

// Renames answer with the new name; deletes keep the channels (a deleted
// folder's channels move to no folder, a deleted label is removed from them).
export async function renameExplorerFolder(folderId: string, name: string, signal?: AbortSignal): Promise<void> {
  await requestJson(`/folders/${encodeURIComponent(folderId)}`, signal, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  });
}

export async function deleteExplorerFolder(folderId: string, signal?: AbortSignal): Promise<void> {
  await requestJson(`/folders/${encodeURIComponent(folderId)}`, signal, { method: 'DELETE' });
}

export async function renameExplorerLabel(labelKey: string, name: string, signal?: AbortSignal): Promise<void> {
  await requestJson(`/labels/${encodeURIComponent(labelKey)}`, signal, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  });
}

export async function deleteExplorerLabel(labelKey: string, signal?: AbortSignal): Promise<void> {
  await requestJson(`/labels/${encodeURIComponent(labelKey)}`, signal, { method: 'DELETE' });
}

export async function assignExplorerChannelLabel(
  channelId: string,
  labelKey: string,
  enabled: boolean,
  signal?: AbortSignal,
): Promise<void> {
  await requestJson(
    `/channels/${encodeURIComponent(channelId)}/labels/${encodeURIComponent(labelKey)}`,
    signal,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    },
  );
}

export async function assignExplorerChannelFolder(
  channelId: string,
  folderId: string,
  signal?: AbortSignal,
): Promise<void> {
  const encoded = encodeURIComponent(channelId);
  await requestJson(`/channels/${encoded}/folder`, signal, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_id: folderId }),
  });
}

export async function loadExplorerBaseline(
  channelId: string,
  query: ChannelBaselineQuery,
  signal?: AbortSignal,
): Promise<ChannelBaselinePayload> {
  const encoded = encodeURIComponent(channelId);
  const payload = parseBaselineV1(
    await requestJson(
      `/channels/${encoded}/explorer/videos/long/baseline${queryString(query)}`,
      signal,
    ),
    query.period ?? 'max',
  );
  if (query.full_catalogue && (payload.sample as { scan_mode?: string } | undefined)?.scan_mode !== 'full_catalogue') {
    throw new ExplorerContractError('The server did not return full-catalogue Overview data.');
  }
  return payload;
}

export function isContractError(error: unknown): error is ExplorerContractError {
  return error instanceof ExplorerContractError;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}
