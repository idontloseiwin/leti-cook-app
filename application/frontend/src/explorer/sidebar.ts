import type { Channel, Folder, Primitive } from '../types';
import { safeImageUrl } from './imageHosts';

export type ExplorerSidebarSortMetric = 'name' | 'video_count' | 'subscribers' | 'added_at';
export type ExplorerSidebarSortDirection = 'asc' | 'desc';
export type ExplorerSidebarSort = {
  metric: ExplorerSidebarSortMetric;
  direction: ExplorerSidebarSortDirection;
};

export const DEFAULT_EXPLORER_SIDEBAR_SORT: ExplorerSidebarSort = {
  metric: 'name',
  direction: 'asc',
};

export type ExplorerSidebarGroup = {
  key: string;
  folderId: string | null;
  label: string;
  channels: Channel[];
};

const NO_FOLDER_KEY = 'unassigned';

function channelName(channel: Channel): string {
  return String(channel.title || channel.channel_id || 'Untitled channel').trim();
}

function numberValue(value: Primitive): number | undefined {
  if (value === null || value === undefined || value === '') return undefined;
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function addedTimestamp(channel: Channel): number | undefined {
  if (!channel.added_at) return undefined;
  const timestamp = Date.parse(channel.added_at);
  return Number.isFinite(timestamp) ? timestamp : undefined;
}

function nameOrder(left: Channel, right: Channel): number {
  const byTitle = channelName(left).localeCompare(channelName(right), 'en', {
    numeric: true,
    sensitivity: 'base',
  });
  if (byTitle !== 0) return byTitle;
  return String(left.channel_id || '').localeCompare(String(right.channel_id || ''));
}

export function sortExplorerSidebarChannels(
  channels: Channel[],
  sort: ExplorerSidebarSort,
): Channel[] {
  return [...channels].sort((left, right) => {
    if (sort.metric === 'name') {
      const order = nameOrder(left, right);
      return sort.direction === 'asc' ? order : -order;
    }

    const leftValue = sort.metric === 'added_at' ? addedTimestamp(left) : numberValue(
      sort.metric === 'subscribers' ? left.subscribers : left.video_count,
    );
    const rightValue = sort.metric === 'added_at' ? addedTimestamp(right) : numberValue(
      sort.metric === 'subscribers' ? right.subscribers : right.video_count,
    );
    if (leftValue === undefined && rightValue !== undefined) return 1;
    if (leftValue !== undefined && rightValue === undefined) return -1;
    if (leftValue !== undefined && rightValue !== undefined && leftValue !== rightValue) {
      return sort.direction === 'asc'
        ? leftValue - rightValue
        : rightValue - leftValue;
    }
    return nameOrder(left, right);
  });
}

export function nextExplorerSidebarSort(
  current: ExplorerSidebarSort,
  metric: ExplorerSidebarSortMetric,
): ExplorerSidebarSort {
  if (current.metric === metric) {
    return {
      metric,
      direction: current.direction === 'asc' ? 'desc' : 'asc',
    };
  }
  return {
    metric,
    direction: metric === 'name' ? 'asc' : 'desc',
  };
}

export function groupExplorerSidebarChannels(
  channels: Channel[],
  folders: Folder[],
  sort: ExplorerSidebarSort,
  query = '',
): ExplorerSidebarGroup[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleChannels = normalizedQuery
    ? channels.filter((channel) => (
      `${channel.title || ''} ${channel.channel_id || ''}`
        .toLocaleLowerCase()
        .includes(normalizedQuery)
    ))
    : channels;
  // Date order is global: moving a channel into a folder must not hide it
  // among older additions or change its position in this view.
  if (sort.metric === 'added_at') return [{
    key: 'recently-added', folderId: null,
    label: sort.direction === 'desc' ? 'Recently added' : 'First added',
    channels: sortExplorerSidebarChannels(visibleChannels, sort),
  }];
  const channelsByFolder = new Map<string, Channel[]>();
  for (const folder of folders) {
    if (folder.id) channelsByFolder.set(folder.id, []);
  }
  const unassigned: Channel[] = [];
  for (const channel of visibleChannels) {
    const folderId = channel.folder_id || '';
    const folderChannels = folderId ? channelsByFolder.get(folderId) : undefined;
    if (folderChannels) folderChannels.push(channel);
    else unassigned.push(channel);
  }

  const groups: ExplorerSidebarGroup[] = folders.flatMap((folder) => {
    if (!folder.id) return [];
    const grouped = sortExplorerSidebarChannels(channelsByFolder.get(folder.id) ?? [], sort);
    if (normalizedQuery && grouped.length === 0) return [];
    return [{
      key: folder.id,
      folderId: folder.id,
      label: String(folder.name || 'Untitled folder'),
      channels: grouped,
    }];
  });
  const sortedUnassigned = sortExplorerSidebarChannels(unassigned, sort);
  if (!normalizedQuery || sortedUnassigned.length > 0) {
    groups.push({
      key: NO_FOLDER_KEY,
      folderId: null,
      label: folders.length ? 'No folder' : 'Channels',
      channels: sortedUnassigned,
    });
  }
  return groups;
}

export function safeExplorerAvatarUrl(value: unknown): string | undefined {
  return safeImageUrl(value);
}

export function mergeExplorerSidebarChannel(channels: Channel[], next?: Channel): Channel[] {
  const channelId = next?.channel_id;
  if (!channelId || !next) return channels;
  const existingIndex = channels.findIndex((channel) => channel.channel_id === channelId);
  if (existingIndex < 0) return [...channels, next];
  return channels.map((channel, index) => (
    index === existingIndex ? { ...channel, ...next } : channel
  ));
}

export function mergeExplorerSidebarFolder(folders: Folder[], next?: Folder): Folder[] {
  const folderId = next?.id;
  if (!folderId || !next) return folders;
  const byId = new Map(folders.flatMap((folder) => (
    folder.id ? [[folder.id, folder] as const] : []
  )));
  byId.set(folderId, { ...byId.get(folderId), ...next });
  return [...byId.values()].sort((left, right) => {
    const byName = String(left.name || '').localeCompare(String(right.name || ''), 'en', {
      numeric: true,
      sensitivity: 'base',
    });
    return byName || String(left.id || '').localeCompare(String(right.id || ''));
  });
}

export function assignExplorerSidebarChannelFolder(
  channels: Channel[],
  channelId: string,
  folderId: string,
): Channel[] {
  return channels.map((channel) => (
    channel.channel_id === channelId
      ? { ...channel, folder_id: folderId || null }
      : channel
  ));
}
