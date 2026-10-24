import type { ChannelExplorerFormatTotalsPayload } from '../types';

// Existing channel threshold: beyond it the uploads-list limit owns the message.
const YOUTUBE_UPLOADS_LIST_LIMIT = 20_000;

/** A difference between fresh channel/list counts, never a video classification. */
export function unlistedVideoCount(
  totals: ChannelExplorerFormatTotalsPayload | undefined,
  now = Date.now(),
): number | null {
  const inventory = totals?.inventory;
  if (!inventory?.complete || !inventory.source_expires_at
      || !(Date.parse(inventory.source_expires_at) > now)) return null;
  const youtubeTotal = inventory.youtube_video_count;
  const listed = inventory.unique_video_count;
  if (typeof youtubeTotal !== 'number' || !Number.isSafeInteger(youtubeTotal)
      || youtubeTotal <= 0 || youtubeTotal > YOUTUBE_UPLOADS_LIST_LIMIT
      || typeof listed !== 'number' || !Number.isSafeInteger(listed) || listed < 0) return null;
  for (const key of ['long', 'short', 'stream'] as const) {
    const format = inventory.formats[key];
    if (!format || !['complete', 'absent'].includes(format.status)
        || !format.source_expires_at || !(Date.parse(format.source_expires_at) > now)) return null;
  }
  return Math.max(0, youtubeTotal - listed);
}
