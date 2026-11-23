import type { VideoType } from '../types';

/** What the baseline panel reads: the adapter's view of `video.lifetime_baseline.v1` (libraryData/inspector.ts). */
export type LifetimeBaselineView = {
  video_type: VideoType;
  status: 'insufficient_data' | 'preliminary' | 'ready';
  sample_size: number;
  missing_views: number;
  median: number | string | null;
  p25: number | string | null;
  p75: number | string | null;
  /** Rows of the type withheld from the population. */
  excluded_count: number;
  /** Views count only under one known definition epoch. */
  definition: 'known' | 'mixed_definition_epochs' | 'unknown_definition_epoch' | 'no_known_values';
  /** Stored views left out because no recorded definition matches them (part of missing_views). */
  unknown_definition_count?: number;
};

/** Why there is no baseline although views were stored; null for the sample-size reason. */
export function baselineDefinitionNote(view: Pick<LifetimeBaselineView, 'definition'>): string | null {
  return view.definition === 'mixed_definition_epochs' ? 'Views definition changed among these videos, so no baseline is shown.'
    : view.definition === 'unknown_definition_epoch' ? 'Some views have no recorded definition, so no baseline is shown.' : null;
}

const BASELINE_TYPE_LABELS: Record<VideoType, string> = { long: 'Long-form', short: 'Shorts', stream: 'Streams' };
export function baselineTypeLabel(type: VideoType): string { return BASELINE_TYPE_LABELS[type]; }
