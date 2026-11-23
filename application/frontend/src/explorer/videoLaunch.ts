import type { VideoLaunchComparison, VideoLaunchHorizon, VideoType } from '../types';
import { formatCount } from './format';

const MICROSECOND = 1000000n;
const DAY = 86400n * MICROSECOND;
export const LAUNCH_HORIZONS = [
  { key: '24h', label: 'First 24 hours', seconds: 86400, tolerance: 7200, evidence: '22–24 hours after publication' },
  { key: '7d', label: 'First 7 days', seconds: 604800, tolerance: 86400, evidence: '6–7 days after publication' },
  { key: '28d', label: 'First 28 days', seconds: 2419200, tolerance: 86400, evidence: '27–28 days after publication' },
] as const;

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

/** Comparison states of the shared operation; adds the three definition states. */
export type LaunchComparisonStatus = VideoLaunchComparison['status'] | 'unknown_definition' | 'mixed_definition_epochs' | 'no_known_values';
export type LaunchComparisonView = Omit<VideoLaunchComparison, 'status'> & { status: LaunchComparisonStatus };
export type LaunchHorizonView = Omit<VideoLaunchHorizon, 'target'> & {
  target: Omit<VideoLaunchHorizon['target'], 'comparison'> & { comparison: LaunchComparisonView };
  definition_state: 'known' | 'mixed_definition_epochs' | 'no_known_values';
  unknown_definition_count: number;
};
/** What the launch panel reads: the adapter's view of `video.launch_comparison.v1` (libraryData/inspector.ts). */
export type VideoLaunchView = {
  video_type: VideoType;
  window: { from_at: string; to_at: string };
  coverage: { eligible_video_count: number; excluded_video_count: number };
  horizons: LaunchHorizonView[];
};

const LAUNCH_TYPE_NOUNS: Record<VideoType, string> = { long: 'long-form videos', short: 'Shorts', stream: 'streams' };
/** "other long-form videos" / "other Shorts" / "other streams" on this channel. */
export function launchTypeNoun(type: VideoType): string { return LAUNCH_TYPE_NOUNS[type]; }
/** Streams are measured from the actual end of the broadcast. */
export function launchEvidence(spec: typeof LAUNCH_HORIZONS[number], type: VideoType): string {
  return type === 'stream' ? spec.evidence.replace('after publication', 'after the stream ended') : spec.evidence;
}
export function launchReferenceText(type: VideoType): string {
  return type === 'stream' ? 'All-time completed streams, measured from the actual end of each stream'
    : type === 'short' ? 'All-time Shorts publications' : 'All-time long-form publications';
}
/** Why a measured video has no assessment; null when it has one or the reason is the sample size. */
export function launchComparisonNote(comparison: Pick<LaunchComparisonView, 'status'>): string | null {
  return comparison.status === 'unknown_definition' ? 'This observation has no recorded views definition. No assessment.'
    : comparison.status === 'mixed_definition_epochs' ? 'Views definition changed among these observations. No assessment.'
      : comparison.status === 'no_known_values' ? 'No observations with a recorded views definition. No assessment.' : null;
}

export function launchStatusLabel(horizon: { target: Pick<VideoLaunchHorizon['target'], 'status'> }): string {
  const labels = { not_reached: 'This age has not been reached yet.', missing_publication: 'Publication date unavailable.',
    outside_observation_window: 'This launch is outside the available 28-day observation window.',
    missing_snapshot: 'No real snapshot in the required age interval.', invalid_snapshot: 'The available snapshot has no usable views.',
    conflicting_snapshots: 'Stored snapshots conflict at the same timestamp.', ineligible_video: 'This video is not eligible for launch analysis.' };
  return horizon.target.status === 'ready' ? '' : labels[horizon.target.status];
}
export function launchClassificationLabel(value: VideoLaunchComparison['classification']): string {
  return value === 'above_typical' ? 'Above Typical' : value === 'below_typical' ? 'Below Typical' : value === 'typical' ? 'Typical' : 'Not assessed';
}
export function launchMultiplierLabel(comparison: Pick<VideoLaunchComparison, 'multiplier' | 'multiplier_reason'>, views: string | null): string | null {
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
