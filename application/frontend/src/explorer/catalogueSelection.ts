import type { ChannelExplorerVideo } from '../types';

export type SelectionProgress = { received: number; total: number };
export type CollectMatching = (signal: AbortSignal, onProgress: (progress: SelectionProgress) => void) => Promise<ChannelExplorerVideo[]>;

/** Existing selections retain the values explicitly selected earlier. */
export function addSelectedRecords(
  current: ReadonlyMap<string, ChannelExplorerVideo>, records: readonly ChannelExplorerVideo[],
): Map<string, ChannelExplorerVideo> {
  const next = new Map(current);
  for (const video of records) {
    const id = video.video_id?.trim();
    if (id && !next.has(id)) next.set(id, { ...video });
  }
  return next;
}
