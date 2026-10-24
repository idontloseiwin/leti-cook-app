export const EXPLORER_BOOKMARKS_STORAGE_KEY = 'leti-cook.explorer.bookmarks.v1';
export const LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY = 'let-it-cook.explorer.bookmarks.v1';

type BookmarkStorage = Pick<Storage, 'getItem' | 'setItem'>;

function normalizeChannelIds(values: Iterable<unknown>): string[] {
  const channelIds = new Set<string>();
  for (const value of values) {
    if (typeof value !== 'string') continue;
    const channelId = value.trim();
    if (channelId) channelIds.add(channelId);
  }
  return [...channelIds].sort();
}

function browserStorage(): BookmarkStorage | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

export function parseExplorerBookmarks(serialized?: string | null): Set<string> {
  if (!serialized) return new Set();
  try {
    const parsed: unknown = JSON.parse(serialized);
    return Array.isArray(parsed)
      ? new Set(normalizeChannelIds(parsed))
      : new Set();
  } catch {
    return new Set();
  }
}

export function loadExplorerBookmarks(
  storage: BookmarkStorage | undefined = browserStorage(),
): Set<string> {
  if (!storage) return new Set();
  try {
    return parseExplorerBookmarks(storage.getItem(EXPLORER_BOOKMARKS_STORAGE_KEY)
      ?? storage.getItem(LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY));
  } catch {
    return new Set();
  }
}

export function saveExplorerBookmarks(
  channelIds: Iterable<string>,
  storage: BookmarkStorage | undefined = browserStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(
      EXPLORER_BOOKMARKS_STORAGE_KEY,
      JSON.stringify(normalizeChannelIds(channelIds)),
    );
    return true;
  } catch {
    return false;
  }
}

export function toggleExplorerBookmark(
  channelIds: ReadonlySet<string>,
  channelId: string,
): Set<string> {
  const next = new Set(channelIds);
  const normalizedChannelId = channelId.trim();
  if (!normalizedChannelId) return next;
  if (next.has(normalizedChannelId)) next.delete(normalizedChannelId);
  else next.add(normalizedChannelId);
  return next;
}
