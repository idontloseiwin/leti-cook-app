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

export async function loadWebSubStatus(signal?: AbortSignal): Promise<WebSubStatus> {
  return readJson<WebSubStatus>('/system/websub', signal);
}
