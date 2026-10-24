import type { PipelineHealthQuota, QuotaSummary } from './types';

export type QuotaPresentation = {
  status: 'ok' | 'partial' | 'unavailable';
  used: number | null;
  limit: number | null;
  remaining: number | null;
  usedPercent: number | null;
  remainingPercent: number | null;
};

function nonnegativeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function quotaPresentation(quota?: QuotaSummary): QuotaPresentation {
  const used = nonnegativeNumber(quota?.used === undefined ? quota?.units_used : quota.used);
  const limit = nonnegativeNumber(quota?.limit === undefined ? 10000 : quota.limit);
  const status = quota?.read_status ?? 'ok';
  if (used === null || (status !== 'ok' && status !== 'partial')) {
    return { status: 'unavailable', used: null, limit, remaining: null, usedPercent: null, remainingPercent: null };
  }
  if (status === 'partial') {
    return { status, used, limit, remaining: null, usedPercent: null, remainingPercent: null };
  }
  // Only old payloads without a status/remaining field use the legacy estimate.
  // Explicit null is unknown, never zero or the full daily allowance.
  const remaining = quota?.read_status === undefined && quota?.remaining === undefined && limit !== null
    ? Math.max(0, limit - used)
    : nonnegativeNumber(quota?.remaining);
  return {
    status,
    used,
    limit,
    remaining,
    usedPercent: limit !== null && limit > 0 ? (used / limit) * 100 : null,
    remainingPercent: remaining !== null && limit !== null && limit > 0
      ? Math.min(100, (remaining / limit) * 100) : null,
  };
}

export function pipelineQuotaPresentation(quota: PipelineHealthQuota | undefined, bucket: string): QuotaPresentation {
  const readStatus = quota?.read_status ?? (quota?.truncated ? 'partial' : undefined);
  const readable = readStatus === 'ok' || readStatus === 'partial'
    || (readStatus === undefined && quota?.bucket_units !== undefined);
  return quotaPresentation({
    read_status: readStatus,
    used: readable ? quota?.bucket_units?.[bucket] ?? 0 : null,
    limit: quota?.configured_bucket_limits?.[bucket],
    remaining: quota?.estimated_bucket_remaining?.[bucket],
  });
}
