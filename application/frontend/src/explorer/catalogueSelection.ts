import type { ChannelExplorerVideo } from '../types';
import { mergeCatalogueBrowse, type BrowseDirection, type CatalogueBrowsePayload } from './catalogueBrowse.ts';

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

/** Reads both sides of the existing frozen window; never publishes grid pages. */
export async function collectCatalogueSelection(
  seed: CatalogueBrowsePayload,
  load: (cursor: string, direction: BrowseDirection) => Promise<CatalogueBrowsePayload>,
  signal: AbortSignal,
  isCurrent: () => boolean,
  onProgress: (progress: SelectionProgress) => void,
  now: () => number = Date.now,
): Promise<ChannelExplorerVideo[]> {
  const assertCurrent = () => {
    if (signal.aborted || !isCurrent()) throw new DOMException('Selection cancelled because the catalogue changed.', 'AbortError');
    if (Date.parse(seed.browse.expires_at) <= now()) throw new Error('The catalogue session expired. Reload before selecting all matching videos.');
  };
  assertCurrent();
  const total = seed.browse.total_count;
  const seen = new Set<string>();
  const accept = (rows: readonly ChannelExplorerVideo[]) => {
    for (const video of rows) {
      const id = video.video_id?.trim();
      if (!id || seen.has(id)) throw new Error('Selection could not verify unique catalogue records. Existing selection is kept.');
      seen.add(id);
    }
    if (seen.size > total) throw new Error('Selection exceeded the frozen catalogue count. Existing selection is kept.');
    onProgress({ received: seen.size, total });
  };
  accept(seed.videos);
  const previousPages: ChannelExplorerVideo[][] = [], nextPages: ChannelExplorerVideo[][] = [];
  const visitedCursors = new Set<string>();
  let firstIndex = seed.browse.start_index, endIndex = firstIndex + seed.videos.length;
  for (const direction of ['previous', 'next'] as const) {
    let boundary = seed;
    while (true) {
      assertCurrent();
      const cursor = boundary.browse[direction === 'next' ? 'next_cursor' : 'previous_cursor'];
      if (!cursor) break;
      if (visitedCursors.has(cursor)) throw new Error('Selection received a repeated catalogue cursor. Existing selection is kept.');
      visitedCursors.add(cursor);
      const page = await load(cursor, direction);
      assertCurrent();
      // Validate a pair, not an ever-growing concatenation on every request.
      mergeCatalogueBrowse(boundary, page, direction);
      if (!page.videos.length) throw new Error('Selection received an empty continuation. Existing selection is kept.');
      accept(page.videos);
      (direction === 'previous' ? previousPages : nextPages).push(page.videos);
      if (direction === 'previous') firstIndex = page.browse.start_index;
      else endIndex = page.browse.start_index + page.videos.length;
      boundary = page;
    }
  }
  assertCurrent();
  if (firstIndex !== 0 || endIndex !== total || seen.size !== total) {
    throw new Error('The complete matching catalogue could not be verified. Existing selection is kept.');
  }
  return [...previousPages.reverse().flat(), ...seed.videos, ...nextPages.flat()];
}
