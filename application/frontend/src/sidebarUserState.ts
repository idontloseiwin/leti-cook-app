import type { SidebarData } from './types';
import { sourceExpired, withoutSourceLifetime } from './sourceLifetime.ts';

/** A channel's provider fields expire independently from neighbouring channels. */
export function retainFreshSidebarSources(value: SidebarData): SidebarData {
  const local = retainSidebarUserState(value);
  return { ...value, channels: (value.channels ?? []).map((channel, index) => sourceExpired(channel) ? local.channels?.[index] ?? {} : channel) };
}

/** Preserve tracking/user controls, without retaining provider facts. */
export function retainSidebarUserState(value: SidebarData): SidebarData {
  return withoutSourceLifetime({
    channels: (value.channels ?? []).map((channel) => ({
      channel_id: channel.channel_id, folder_id: channel.folder_id, added_at: channel.added_at,
      label_keys: channel.label_keys, parent_channel_id: channel.parent_channel_id,
      initial_long_inventory_status: channel.initial_long_inventory_status,
      initial_28d_stats_status: channel.initial_28d_stats_status,
      deep_inventory_status: channel.deep_inventory_status,
      channel_metadata_status: channel.channel_metadata_status,
      channel_discovery_status: channel.channel_discovery_status,
    })),
    folders: value.folders, labels: value.labels,
  });
}
