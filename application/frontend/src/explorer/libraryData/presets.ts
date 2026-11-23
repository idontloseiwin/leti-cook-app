// Bucket presets per video type: the shared JSON the server reads
// too (research_query/registry.py, presets; the same sets reach /research/capabilities as
// presets). Shorts and Streams share every group except duration, whose sets are
// the file's by_video_type overrides. Pure TypeScript.
import presetRegistry from '../../../../shared/catalogue-filter-presets.json' with { type: 'json' };
import type { LibraryVideoType } from './wire.ts';

export type LibraryPreset = { key: string; label: string; min: number | null; max: number | null };
export const PRESET_GROUPS = ['views', 'duration', 'likes', 'comments', 'title_length', 'tag_count'] as const;
export type LibraryPresetGroup = typeof PRESET_GROUPS[number];

const shared = presetRegistry as unknown as Record<LibraryPresetGroup, LibraryPreset[]> & {
  by_video_type?: Partial<Record<LibraryVideoType, Partial<Record<LibraryPresetGroup, LibraryPreset[]>>>>;
};

/** Duration sets by type: the shared file's per-type overrides. */
const typedDuration: Record<LibraryVideoType, readonly LibraryPreset[] | null> = {
  long: shared.duration,
  short: shared.by_video_type?.short?.duration ?? null,
  stream: shared.by_video_type?.stream?.duration ?? null,
};

/** Presets of one group for one type, in display order; null when this type has none yet. */
export function presetsFor(type: LibraryVideoType, group: LibraryPresetGroup): readonly LibraryPreset[] | null {
  if (!PRESET_GROUPS.includes(group)) throw new Error(`Unknown preset group: ${String(group)}`);
  if (group === 'duration') return typedDuration[type] ?? null;
  return shared[group];
}
