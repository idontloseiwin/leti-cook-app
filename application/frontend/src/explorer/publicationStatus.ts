import { sourceLifetimeParser } from '../sourceLifetime.ts';

export type PublicationStatus = 'upcoming' | 'live' | 'awaiting_completion';
export type PublicationObservation = {
  video_id: string;
  video_type: 'long' | 'short' | 'stream' | null;
  title: string | null;
  publication_status: PublicationStatus;
  scheduled_start_at: string | null;
  checked_at: string;
  source_expires_at: string;
};
export type ChannelPublicationsPage = {
  contract_version: 1;
  channel_id: string;
  videos: PublicationObservation[];
  next_cursor: string | null;
};

const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
function invalid(): never { throw new Error('Publication status response is invalid.'); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid();
  return value as Record<string, unknown>;
}
function timestamp(value: unknown): string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)
      || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0, 19) !== value.slice(0, 19)) return invalid();
  return value;
}

function decodeChannelPublications(value: unknown, channelId: string, cursor?: string): ChannelPublicationsPage {
  const page = record(value), source = record(page.source);
  if (page.contract_version !== 1 || page.channel_id !== channelId || !Array.isArray(page.videos)
      || page.videos.length > 50 || source.kind !== 'stored_publication_observations'
      || source.youtube_requests !== false || source.database_writes !== false) return invalid();
  let previous = cursor;
  const videos = page.videos.map((raw): PublicationObservation => {
    const row = record(raw);
    if (typeof row.video_id !== 'string' || !VIDEO_ID.test(row.video_id)
        || previous !== undefined && row.video_id <= previous
        || !['upcoming', 'live', 'awaiting_completion'].includes(String(row.publication_status))
        || ![null, 'long', 'short', 'stream'].includes(row.video_type as string | null)
        || row.title !== null && (typeof row.title !== 'string' || row.title.length > 1000)) return invalid();
    const checkedAt = timestamp(row.checked_at), expiry = timestamp(row.source_expires_at);
    if (Date.parse(expiry) <= Date.parse(checkedAt)) return invalid();
    previous = row.video_id;
    return { video_id: row.video_id, video_type: row.video_type as PublicationObservation['video_type'],
      title: row.title as string | null, publication_status: row.publication_status as PublicationStatus,
      scheduled_start_at: row.scheduled_start_at === null ? null : timestamp(row.scheduled_start_at),
      checked_at: checkedAt, source_expires_at: expiry };
  });
  if (page.next_cursor !== null && (videos.length !== 50 || page.next_cursor !== videos.at(-1)?.video_id)) return invalid();
  return { contract_version: 1, channel_id: channelId, videos, next_cursor: page.next_cursor as string | null };
}

export const parseChannelPublications = sourceLifetimeParser(decodeChannelPublications);

// The server's observation is authoritative even after a scheduled date passes.
export function publicationStatusLabel(status: PublicationStatus): string {
  return { upcoming: 'Upcoming', live: 'Live', awaiting_completion: 'Awaiting completion' }[status];
}

export function publicationTypeLabel(type: PublicationObservation['video_type']): string {
  return type === null ? 'Type unconfirmed' : { long: 'Long', short: 'Shorts', stream: 'Stream' }[type];
}
