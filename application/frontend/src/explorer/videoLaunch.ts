import type { VideoLaunchComparison, VideoLaunchHorizon, VideoLaunchPayload } from '../types';
import { ExplorerContractError, utcTimestampMicroseconds } from './contracts';
import { formatCount } from './format';

const MICROSECOND = 1000000n;
const DAY = 86400n * MICROSECOND;
export const LAUNCH_HORIZONS = [
  { key: '24h', label: 'First 24 hours', seconds: 86400, tolerance: 7200, evidence: '22–24 hours after publication' },
  { key: '7d', label: 'First 7 days', seconds: 604800, tolerance: 86400, evidence: '6–7 days after publication' },
  { key: '28d', label: 'First 28 days', seconds: 2419200, tolerance: 86400, evidence: '27–28 days after publication' },
] as const;
const record = (value: unknown): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const integer = (value: unknown): value is string => typeof value === 'string' && /^(?:0|[1-9]\d*)$/.test(value);
const invalid = (): never => { throw new ExplorerContractError('The server returned incompatible measured launch performance.'); };
const quality = (size: number) => size < 4 ? 'insufficient_data' : size < 12 ? 'preliminary' : 'ready';
const statuses = new Set(['ready', 'not_reached', 'missing_publication', 'outside_observation_window', 'missing_snapshot',
  'invalid_snapshot', 'conflicting_snapshots', 'ineligible_video']);

/** Exact fixed decimal units, used for validation only, never to synthesize analytics. */
function hundredths(value: unknown): bigint | null {
  if (typeof value !== 'string') return null;
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(value);
  return match ? BigInt(match[1]) * 100n + BigInt((match[2] ?? '').padEnd(2, '0')) : null;
}
export function launchAgeMicroseconds(value: unknown): bigint | null {
  if (typeof value !== 'string') return null;
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,6}))?$/.exec(value);
  return match ? BigInt(match[1]) * MICROSECOND + BigInt((match[2] ?? '').padEnd(6, '0')) : null;
}

function validateComparison(value: unknown, views: string | null, size: number) {
  if (!record(value) || !count(value.reference_count) || value.reference_count !== size - (views === null ? 0 : 1)) return invalid();
  const expected = views === null ? 'unavailable' : quality(size);
  if (value.status !== expected) return invalid();
  if (expected === 'unavailable' || expected === 'insufficient_data') {
    if (['median', 'p25', 'p75', 'classification', 'multiplier', 'multiplier_reason'].some((key) => value[key] !== null)) return invalid();
    return;
  }
  const median = hundredths(value.median), low = hundredths(value.p25), high = hundredths(value.p75);
  if (median === null || low === null || high === null || median % 25n || low % 25n || high % 25n || low > median || median > high) return invalid();
  const observed = BigInt(views!) * 100n;
  const classification = observed < low ? 'below_typical' : observed > high ? 'above_typical' : 'typical';
  if (value.classification !== classification) return invalid();
  if (median === 0n) {
    if (value.multiplier !== null || value.multiplier_reason !== 'zero_reference_median') return invalid();
  } else {
    const ratio = hundredths(value.multiplier);
    if (ratio === null || value.multiplier_reason !== null) return invalid();
    const scaled = observed * 100n, whole = scaled / median, remainder = scaled % median;
    const rounded = whole + (remainder * 2n > median || remainder * 2n === median && whole % 2n !== 0n ? 1n : 0n);
    if (ratio !== rounded) return invalid();
  }
}

/** Validate exact opening identity, measured evidence windows and independent horizon cohorts. */
export function parseVideoLaunchV1(value: unknown, channelId: string, videoId: string, browseSession: string): VideoLaunchPayload {
  if (!record(value)) return invalid();
  const generated = utcTimestampMicroseconds(value.generated_at), created = utcTimestampMicroseconds(value.browse_created_at);
  const expires = utcTimestampMicroseconds(value.expires_at);
  const { window, reference, coverage, source, horizons } = value;
  if (value.contract_version !== 1 || value.channel_id !== channelId || value.video_id !== videoId || value.browse_session !== browseSession
    || value.method !== 'measured_launch_before_horizon_v1' || generated === null || created === null || expires === null
    || generated < created || expires <= generated || !record(window) || !record(reference) || !record(coverage) || !record(source)
    || !Array.isArray(horizons) || horizons.length !== 3) return invalid();
  const from = utcTimestampMicroseconds(window.from_at), to = utcTimestampMicroseconds(window.to_at);
  if (window.days !== 28 || from === null || to === null || to !== created || generated - from !== 28n * DAY || to < from || to > generated
    || reference.video_type !== 'long' || reference.publication_period !== 'all_time'
    || reference.browsing_filters_applied !== false || reference.leave_one_out !== true
    || coverage.complete !== true || coverage.snapshot_row_limit !== 20000
    || !['inventory_count', 'eligible_video_count', 'excluded_video_count', 'snapshot_rows'].every((key) => count(coverage[key]))
    || BigInt(coverage.eligible_video_count as number) + BigInt(coverage.excluded_video_count as number) !== BigInt(coverage.inventory_count as number)
    || (coverage.snapshot_rows as number) > 20000 || source.snapshots_used !== true || source.youtube_requests !== false
    || source.database_writes !== false || source.attribution !== 'Leti Cook') return invalid();
  let publication: bigint | null = null;
  horizons.forEach((horizon, index) => {
    const spec = LAUNCH_HORIZONS[index];
    if (!record(horizon) || horizon.key !== spec.key || horizon.horizon_seconds !== spec.seconds
      || horizon.tolerance_before_seconds !== spec.tolerance || !count(horizon.eligible_count)
      || horizon.eligible_count > (coverage.eligible_video_count as number) || horizon.eligible_count > (coverage.snapshot_rows as number)
      || horizon.quality !== quality(horizon.eligible_count) || !record(horizon.counts) || !record(horizon.target)) return invalid();
    const { counts, target } = horizon;
    if (!['below_typical', 'typical', 'above_typical'].every((key) => count(counts[key]))
      || BigInt(counts.below_typical as number) + BigInt(counts.typical as number) + BigInt(counts.above_typical as number)
        !== BigInt(horizon.eligible_count < 4 ? 0 : horizon.eligible_count)
      || typeof target.status !== 'string' || !statuses.has(target.status)) return invalid();
    const targetAt = utcTimestampMicroseconds(target.target_at), observedAt = utcTimestampMicroseconds(target.observed_at);
    if (target.target_at !== null && targetAt === null) return invalid();
    if (targetAt !== null) {
      const published = targetAt - BigInt(spec.seconds) * MICROSECOND;
      if (publication !== null && publication !== published) return invalid();
      publication = published;
    }
    if (target.status === 'ready') {
      const age = launchAgeMicroseconds(target.actual_age_seconds);
      if (targetAt === null || observedAt === null || !integer(target.views) || age === null || horizon.eligible_count < 1
        || targetAt > to || targetAt < from || observedAt < from || observedAt > to
        || observedAt > targetAt || targetAt - observedAt > BigInt(spec.tolerance) * MICROSECOND
        || age !== BigInt(spec.seconds) * MICROSECOND - (targetAt - observedAt)) return invalid();
    } else {
      if (target.observed_at !== null || target.actual_age_seconds !== null || target.views !== null) return invalid();
      if (target.status === 'missing_publication' && target.target_at !== null) return invalid();
      if (target.status === 'not_reached' && (targetAt === null || targetAt <= to)) return invalid();
      if (target.status === 'outside_observation_window' && (targetAt === null || targetAt >= from)) return invalid();
      if (['missing_snapshot', 'invalid_snapshot', 'conflicting_snapshots'].includes(target.status)
        && (targetAt === null || targetAt < from || targetAt > to)) return invalid();
    }
    validateComparison(target.comparison, target.views as string | null, horizon.eligible_count);
  });
  return value as unknown as VideoLaunchPayload;
}

export function launchStatusLabel(horizon: VideoLaunchHorizon): string {
  const labels = { not_reached: 'This age has not been reached yet.', missing_publication: 'Publication date unavailable.',
    outside_observation_window: 'This launch is outside the available 28-day observation window.',
    missing_snapshot: 'No real snapshot in the required age interval.', invalid_snapshot: 'The available snapshot has no usable views.',
    conflicting_snapshots: 'Stored snapshots conflict at the same timestamp.', ineligible_video: 'This video is not eligible for launch analysis.' };
  return horizon.target.status === 'ready' ? '' : labels[horizon.target.status];
}
export function launchClassificationLabel(value: VideoLaunchComparison['classification']): string {
  return value === 'above_typical' ? 'Above Typical' : value === 'below_typical' ? 'Below Typical' : value === 'typical' ? 'Typical' : 'Not assessed';
}
export function launchMultiplierLabel(comparison: VideoLaunchComparison, views: string | null): string | null {
  if (comparison.multiplier === null) return comparison.multiplier_reason === 'zero_reference_median' ? 'Ratio unavailable: median is zero.' : null;
  return `${hundredths(comparison.multiplier) === 0n && views !== null && BigInt(views) > 0n ? '<0.01'
    : formatCount(comparison.multiplier, { compact: false })}× typical`;
}
export function launchAgeLabel(value: string | null): string {
  const micros = launchAgeMicroseconds(value);
  if (micros === null) return 'Not available';
  let remainder = micros;
  const parts: string[] = [];
  for (const [size, label] of [[DAY, 'd'], [3600n * MICROSECOND, 'h'], [60n * MICROSECOND, 'm']] as const) {
    const units = remainder / size;
    if (units) parts.push(`${units}${label}`);
    remainder %= size;
  }
  // Whole seconds are enough for an age; the microseconds are kept for comparisons only.
  const seconds = remainder / MICROSECOND;
  if (seconds) parts.push(`${seconds}s`);
  return parts.join(' ') || (micros ? '<1s' : '0s');
}
