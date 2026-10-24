import type { VideoLifetimeBaselinePayload } from '../types';
import { ExplorerContractError, utcTimestampMicroseconds } from './contracts';

const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const invalid = (): never => { throw new ExplorerContractError('The server returned an incompatible channel lifetime baseline.'); };

/** Integer-input linear quartiles are multiples of 0.25; compare without rounding large decimal strings. */
function quarterUnits(value: unknown): bigint | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  if (typeof value === 'number' && (!Number.isFinite(value) || value < 0 || value > Number.MAX_SAFE_INTEGER)) return null;
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(String(value));
  if (!match) return null;
  const fraction = Number((match[2] ?? '').padEnd(2, '0'));
  if (fraction % 25 !== 0) return null;
  return BigInt(match[1]) * 4n + BigInt(fraction / 25);
}

/** Channel-wide descriptive summary only: no per-video rating or age adjustment. */
export function parseVideoLifetimeBaselineV1(value: unknown, channelId: string, videoId: string, browseSession: string): VideoLifetimeBaselinePayload {
  if (!record(value)) return invalid();
  const generated = utcTimestampMicroseconds(value.generated_at), expires = utcTimestampMicroseconds(value.expires_at);
  const { coverage, reference } = value;
  if (value.contract_version !== 1 || value.channel_id !== channelId || value.video_id !== videoId || value.browse_session !== browseSession
    || generated === null || expires === null || expires <= generated || value.method !== 'current_lifetime_views_median_iqr_v1'
    || !record(coverage) || !record(reference) || reference.video_type !== 'long' || reference.publication_period !== 'all_time'
    || reference.browsing_filters_applied !== false || !count(value.sample_size) || !count(value.missing_views)
    || !count(coverage.inventory_count) || !count(coverage.eligible_count) || !count(coverage.excluded_count)
    || BigInt(value.sample_size) + BigInt(value.missing_views) !== BigInt(coverage.eligible_count)
    || BigInt(coverage.eligible_count) + BigInt(coverage.excluded_count) !== BigInt(coverage.inventory_count)) return invalid();
  const status = value.sample_size < 4 ? 'insufficient_data' : value.sample_size < 12 ? 'preliminary' : 'ready';
  if (value.status !== status) return invalid();
  if (status === 'insufficient_data') {
    if (value.median !== null || value.p25 !== null || value.p75 !== null) return invalid();
  } else {
    const median = quarterUnits(value.median), low = quarterUnits(value.p25), high = quarterUnits(value.p75);
    if (median === null || low === null || high === null || low > median || median > high) return invalid();
  }
  return value as unknown as VideoLifetimeBaselinePayload;
}
