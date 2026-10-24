export type ExplorerOperationalPage = 'health' | 'telemetry';

// The only served workspace. Retired interface roots answer 410 on the server.
export const EXPLORER_V3_ROOT = '/app-explorer-v3';

export function explorerOperationalPath(page: ExplorerOperationalPage): string {
  return `${EXPLORER_V3_ROOT}/${page}`;
}

export function parseExplorerOperationalPath(pathname: string): ExplorerOperationalPage | undefined {
  const normalized = pathname.replace(/\/$/, '');
  for (const page of ['health', 'telemetry'] as const) {
    if (normalized === explorerOperationalPath(page)) return page;
  }
  return undefined;
}

export function explorerChannelPath(channelId: string): string {
  return `${EXPLORER_V3_ROOT}/channels/${encodeURIComponent(channelId)}`;
}

export function parseExplorerChannelPath(pathname: string): string | undefined {
  const prefix = `${EXPLORER_V3_ROOT}/channels/`;
  if (!pathname.startsWith(prefix)) return undefined;
  const encodedChannelId = pathname.slice(prefix.length).split('/')[0];
  if (!encodedChannelId) return undefined;
  try {
    return decodeURIComponent(encodedChannelId);
  } catch {
    return undefined;
  }
}
