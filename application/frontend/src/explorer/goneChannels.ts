import type { Channel } from '../types';
import { formatDateLocal } from './localTime';

// A channel YouTube no longer returns (docs/DATA.md). Its stored profile is
// withdrawn on read, so lists show a plain label and never a stored title.
// collection.channel_metadata.CHANNEL_METADATA_STATUS_NOT_FOUND
export const CHANNEL_GONE_STATUS = 'channel_not_found';
export const CHANNEL_GONE_LABEL = 'No longer on YouTube';

export function channelGoneFromYouTube(channel: Channel | undefined): boolean {
  return channel?.channel_metadata_status === CHANNEL_GONE_STATUS;
}

/** A list's name: the current title, else the label for a gone channel, else the ID. */
export function channelDisplayName(channel: Channel): string {
  const title = String(channel.title || '').trim();
  if (title) return title;
  if (channelGoneFromYouTube(channel)) return CHANNEL_GONE_LABEL;
  return String(channel.channel_id || 'Untitled channel').trim();
}

/** A picker's name: gone channels keep their ID, so several of them stay distinct. */
export function channelChoiceName(channel: Channel): string {
  const name = channelDisplayName(channel);
  return name === CHANNEL_GONE_LABEL && channel.channel_id ? `${name} (${channel.channel_id})` : name;
}

type Removal = { kind: 'ahead'; at: string; time: number } | { kind: 'expired' } | { kind: 'unknown' };

// The server sends null when nothing readable is left, and no field when it
// could not confirm the status (then no date is claimed).
function removal(channel: Channel, now: number): Removal {
  const value = channel.youtube_data_expires_at;
  if (value === null) return { kind: 'expired' };
  if (typeof value !== 'string') return { kind: 'unknown' };
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return { kind: 'unknown' };
  return time > now ? { kind: 'ahead', at: value, time } : { kind: 'expired' };
}

/** The short line of a list entry; empty when no date is known. */
export function goneChannelRemovalLine(channel: Channel, now = Date.now()): string {
  const value = removal(channel, now);
  if (value.kind === 'ahead') return `Data removed by ${formatDateLocal(value.at)}`;
  return value.kind === 'expired' ? 'Stored data expired' : '';
}

const validDate = (value: string | null | undefined): value is string =>
  typeof value === 'string' && Number.isFinite(Date.parse(value));

/** The whole notice: the channel's page, and a list entry's tooltip and name for assistive technology. */
export function goneChannelNotice(channel: Channel, now = Date.now()): string {
  // The first day is unknown for a channel that was already missing before Leti Cook recorded it.
  const since = channel.channel_metadata_not_found_since;
  const checked = channel.channel_metadata_checked_at;
  const lead = validDate(since)
    ? `YouTube has not returned this channel since ${formatDateLocal(since)}`
    : 'YouTube no longer returns this channel';
  const parts = [validDate(checked) ? `${lead} (last checked ${formatDateLocal(checked)}).` : `${lead}.`];
  const value = removal(channel, now);
  if (value.kind === 'ahead') parts.push(`Its stored YouTube data is removed by ${formatDateLocal(value.at)} under the 29-day data rule.`);
  if (value.kind === 'expired') parts.push('Its stored YouTube data has expired under the 29-day data rule.');
  parts.push('Your folders, labels and saved work for it stay.');
  return parts.join(' ');
}

// unknown: channels whose removal date the server did not confirm (present only when there are some).
export type GoneChannelSummary = { count: number; earliest?: string; unknown?: number };

/** Home's summary: how many channels are gone, the earliest removal date still ahead, and how many dates are unknown. */
export function goneChannelSummary(channels: readonly Channel[], now = Date.now()): GoneChannelSummary {
  let count = 0;
  let unknown = 0;
  let earliest: Extract<Removal, { kind: 'ahead' }> | undefined;
  for (const channel of channels) {
    if (!channelGoneFromYouTube(channel)) continue;
    count += 1;
    const value = removal(channel, now);
    if (value.kind === 'unknown') unknown += 1;
    if (value.kind === 'ahead' && (!earliest || value.time < earliest.time)) earliest = value;
  }
  return { count, ...(earliest ? { earliest: earliest.at } : {}), ...(unknown ? { unknown } : {}) };
}

/** Home's summary sentence after the count. "Expired" only when it is confirmed for every channel counted. */
export function goneChannelSummaryText(summary: GoneChannelSummary): string {
  const one = summary.count === 1;
  const unknown = summary.unknown ?? 0;
  const owner = one ? 'Its' : 'Their';
  const parts = [`YouTube no longer returns ${one ? 'it' : 'them'}.`];
  if (summary.earliest) {
    const first = one ? '' : unknown ? 'the first confirmed one ' : 'the first ';
    parts.push(`${owner} stored YouTube data is removed under the 29-day data rule, ${first}by ${formatDateLocal(summary.earliest)}.`);
  } else if (!unknown) {
    parts.push(`${owner} stored YouTube data has expired under the 29-day data rule.`);
  }
  if (unknown) {
    parts.push(unknown === summary.count
      ? `The date for ${one ? 'its' : 'their'} stored YouTube data is not confirmed yet.`
      : `For ${unknown} of them the date is not confirmed yet.`);
  }
  parts.push('Your folders, labels and saved work stay.');
  return parts.join(' ');
}
