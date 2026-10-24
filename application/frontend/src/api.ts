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
