import { captureSourceLifetime, sourceExpired, SOURCE_EXPIRED_MESSAGE } from './sourceLifetime.ts';
import type { PipelineHealthReport } from './types';

async function readJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const requestStarted = performance.now();
  const response = await fetch(path, {
    cache: 'no-store',
    signal,
    headers: { Accept: 'application/json' }
  });

  if (!response.ok) {
    throw new Error(`${path} returned HTTP ${response.status}`);
  }

  const payload = captureSourceLifetime((await response.json()) as T, response.headers, requestStarted);
  if (sourceExpired(payload)) throw new Error(SOURCE_EXPIRED_MESSAGE);
  return payload;
}

export async function loadPipelineHealth(signal?: AbortSignal): Promise<PipelineHealthReport> {
  return readJson<PipelineHealthReport>('/system/pipeline-health', signal);
}

export type WebSubSummary = {
  subscriptions: Record<string, number>;
  expiring_24h: number;
  last_delivery_at: string | null;
  deliveries_24h: number;
  events_24h: Record<string, number>;
  events_pending: number;
  hub_requests_24h: Record<string, number>;
  hold_until: string | null;
  breaker_open: boolean;
  ramp_stage: number;
};
export type WebSubStatus = { configured: boolean; setting_error?: string; public_host?: string; summary?: WebSubSummary };

export function parseWebSubStatus(value: unknown): WebSubStatus {
  const fail = (): never => { throw new Error('Invalid WebSub health response'); };
  const record = (input: unknown): input is Record<string, unknown> =>
    typeof input === 'object' && input !== null && !Array.isArray(input);
  const count = (input: unknown): boolean =>
    typeof input === 'number' && Number.isSafeInteger(input) && input >= 0;
  const instantOrNull = (input: unknown): boolean => input === null ||
    (typeof input === 'string' && /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(input) && Number.isFinite(Date.parse(input)));
  if (!record(value) || typeof value.configured !== 'boolean') return fail();
  if (!value.configured) {
    if (value.summary !== undefined || value.public_host !== undefined ||
        (value.setting_error !== undefined && typeof value.setting_error !== 'string')) return fail();
  } else {
    const summary = value.summary;
    if (typeof value.public_host !== 'string' || !value.public_host || !record(summary) ||
        typeof summary.breaker_open !== 'boolean' || !instantOrNull(summary.last_delivery_at) ||
        !instantOrNull(summary.hold_until)) return fail();
    for (const field of ['expiring_24h', 'deliveries_24h', 'events_pending', 'ramp_stage']) {
      if (!count(summary[field])) return fail();
    }
    for (const field of ['subscriptions', 'events_24h', 'hub_requests_24h']) {
      const counts = summary[field];
      if (!record(counts) || !Object.values(counts).every(count)) return fail();
    }
  }
  return value as WebSubStatus;
}

export async function loadWebSubStatus(signal?: AbortSignal): Promise<WebSubStatus> {
  return parseWebSubStatus(await readJson<unknown>('/system/websub', signal));
}
