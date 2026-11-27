import { captureSourceLifetime, sourceLifetimeParser } from '../sourceLifetime.ts';
import { serverErrorMessage, unreadableAnswerError } from '../serverText.ts';
import { observeDiscoveryResponse } from '../discoveryJournal';
import type {
  ExplorerChannelLookupResult,
  ChannelExplorerListInventoryPayload,
  ChannelExplorerProfilePayload,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder as ChannelFolder,
  SidebarData,
  SystemSummary,
} from '../types';
import {
  ExplorerContractError,
  parseExplorerAddChannel as decodeExplorerAddChannel,
  parseExplorerHydration as decodeExplorerHydration,
  parseExplorerChannelLookup as decodeExplorerChannelLookup,
  parseExplorerHomeSummary as decodeExplorerHomeSummary,
  parseExplorerFolder as decodeExplorerFolder,
  parseExplorerListInventoryV1 as decodeExplorerListInventoryV1,
  parseExplorerLabel as decodeExplorerLabel,
  parseExplorerProfileV1 as decodeExplorerProfileV1,
  parseExplorerSystemSummary as decodeExplorerSystemSummary,
  parseSidebarData as decodeSidebarData,
} from './contracts';
import type {
  ExplorerAddChannelResult,
  ExplorerHomeSummary,
  ExplorerHydrationEstimate,
  ExplorerHydrationPayload,
} from './model';

const parseExplorerAddChannel = sourceLifetimeParser(decodeExplorerAddChannel);
const parseExplorerChannelLookup = sourceLifetimeParser(decodeExplorerChannelLookup);
const parseExplorerFolder = sourceLifetimeParser(decodeExplorerFolder);
const parseExplorerListInventoryV1 = sourceLifetimeParser(decodeExplorerListInventoryV1);
const parseExplorerHomeSummary = sourceLifetimeParser(decodeExplorerHomeSummary);
const parseExplorerHydration = sourceLifetimeParser(decodeExplorerHydration);
const parseExplorerLabel = sourceLifetimeParser(decodeExplorerLabel);
const parseExplorerProfileV1 = sourceLifetimeParser(decodeExplorerProfileV1);
const parseExplorerSystemSummary = sourceLifetimeParser(decodeExplorerSystemSummary);
const parseSidebarData = sourceLifetimeParser(decodeSidebarData);
const MAX_ERROR_LENGTH = 360;
const REQUEST_TIMEOUT_MS = 120_000;

/**
 * Which bound a `query_budget_exceeded` refusal reached, when the server names it:
 * `request` — the conditions are too large or too complex; `response` — the answer is too large to send.
 */
export type ExplorerBudgetLimit = 'rows' | 'memory' | 'time' | 'request' | 'response';
const BUDGET_LIMITS: readonly string[] = ['rows', 'memory', 'time', 'request', 'response'];

export class ExplorerApiError extends Error {
  status: number;
  code?: string;
  limit?: ExplorerBudgetLimit;

  constructor(message: string, status: number, code?: string, limit?: ExplorerBudgetLimit) {
    super(message.slice(0, MAX_ERROR_LENGTH));
    this.name = 'ExplorerApiError';
    this.status = status;
    this.code = code;
    if (limit) this.limit = limit;
  }
}

/**
 * The browser could not reach the server (offline, refused or dropped
 * connection). The page shows this sentence instead of the browser's own
 * "Failed to fetch". It has no HTTP status: a write stays uncertain.
 */
export class ExplorerNetworkError extends Error {
  constructor(cause: unknown) {
    super('No connection to the Leti Cook server.', { cause });
    this.name = 'ExplorerNetworkError';
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

function errorDetails(payload: unknown): { message?: string; code?: string; limit?: ExplorerBudgetLimit } {
  if (!payload || typeof payload !== 'object') return {};
  const record = payload as {
    error?: unknown;
    message?: unknown;
    code?: unknown;
    detail?: unknown;
    limit?: unknown;
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
    ...(typeof record.limit === 'string' && BUDGET_LIMITS.includes(record.limit) ? { limit: record.limit as ExplorerBudgetLimit } : {}),
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
    if (!controller.signal.aborted) {
      timedOut = true;
      controller.abort();
    }
  }, REQUEST_TIMEOUT_MS);
  try {
    const headers = new Headers(init?.headers);
    headers.set('Accept', 'application/json');
    const response = await fetch(path, {
      ...init,
      cache: 'no-store',
      headers,
      signal: controller.signal,
    }).catch((error: unknown) => {
      // fetch rejects with a TypeError only when no answer arrived.
      throw error instanceof TypeError && !isAbortError(error) ? new ExplorerNetworkError(error) : error;
    });
    if (!response.ok) {
      // The refusal status still applies when the server sends no JSON error detail.
      const details = errorDetails(await response.json().catch(() => undefined));
      throw new ExplorerApiError(
        serverErrorMessage(response.status, details.code, details.message),
        response.status,
        details.code,
        details.limit,
      );
    }
    const payload = await response.json().catch((error: unknown) => {
      if (isAbortError(error)) throw error;
      if (error instanceof TypeError) throw new ExplorerNetworkError(error);
      throw unreadableAnswerError(error);
    });
    controller.signal.throwIfAborted();
    observeDiscoveryResponse(payload, response.headers);
    return captureSourceLifetime(payload, response.headers, requestStarted);
  } catch (error) {
    if (signal?.aborted) throw controller.signal.reason;
    if (timedOut && !(error instanceof ExplorerApiError)) {
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

/** The list proof of the header's unlisted note; totals come from the shared backend. */
export async function loadExplorerListInventory(
  channelId: string,
  signal?: AbortSignal,
): Promise<ChannelExplorerListInventoryPayload> {
  const encoded = encodeURIComponent(channelId);
  return parseExplorerListInventoryV1(
    await requestJson(`/channels/${encoded}/explorer/list-inventory`, signal),
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

function checkOrganizationReceipt(payload: unknown, expected: Record<string, unknown>) {
  if (!payload || typeof payload !== 'object'
    || Object.entries(expected).some(([key, value]) => (payload as Record<string, unknown>)[key] !== value)) {
    throw new ExplorerContractError('The server did not confirm the requested change.');
  }
}

// Renames answer with the new name; deletes keep the channels (a deleted
// folder's channels move to no folder, a deleted label is removed from them).
export async function renameExplorerFolder(folderId: string, name: string, signal?: AbortSignal): Promise<void> {
  const folder = parseExplorerFolder(await requestJson(`/folders/${encodeURIComponent(folderId)}`, signal, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  }));
  checkOrganizationReceipt(folder, { id: folderId, name: name.trim() });
}

export async function deleteExplorerFolder(folderId: string, signal?: AbortSignal): Promise<void> {
  const result = await requestJson(`/folders/${encodeURIComponent(folderId)}`, signal, { method: 'DELETE' });
  checkOrganizationReceipt(result, { status: 'deleted', id: folderId });
}

export async function renameExplorerLabel(labelKey: string, name: string, signal?: AbortSignal): Promise<void> {
  const label = parseExplorerLabel(await requestJson(`/labels/${encodeURIComponent(labelKey)}`, signal, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  }));
  // The label endpoint collapses Python whitespace, including the four ASCII separators.
  const savedName = name.replace(/[\p{White_Space}\u001c-\u001f]+/gu, ' ').replace(/^ | $/g, '');
  checkOrganizationReceipt(label, { label_key: labelKey, name: savedName });
}

export async function deleteExplorerLabel(labelKey: string, signal?: AbortSignal): Promise<void> {
  const result = await requestJson(`/labels/${encodeURIComponent(labelKey)}`, signal, { method: 'DELETE' });
  checkOrganizationReceipt(result, { status: 'deleted', label_key: labelKey });
}

export async function assignExplorerChannelLabel(
  channelId: string,
  labelKey: string,
  enabled: boolean,
  signal?: AbortSignal,
): Promise<void> {
  const result = await requestJson(
    `/channels/${encodeURIComponent(channelId)}/labels/${encodeURIComponent(labelKey)}`,
    signal,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    },
  );
  checkOrganizationReceipt(result, { status: 'ok', enabled });
}

export async function assignExplorerChannelFolder(
  channelId: string,
  folderId: string,
  signal?: AbortSignal,
): Promise<void> {
  const encoded = encodeURIComponent(channelId);
  const result = await requestJson(`/channels/${encoded}/folder`, signal, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder_id: folderId }),
  });
  checkOrganizationReceipt(result, { status: 'ok' });
}

export function isContractError(error: unknown): error is ExplorerContractError {
  return error instanceof ExplorerContractError;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}
