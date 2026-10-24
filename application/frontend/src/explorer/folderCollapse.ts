// Existing browser values remain readable; new writes use the current name.
export const COLLAPSED_FOLDERS_STORAGE_KEY =
  'leti-cook.explorer-v2.collapsed-folders.v1';
export const LEGACY_COLLAPSED_FOLDERS_STORAGE_KEY =
  'let-it-cook.explorer-v2.collapsed-folders.v1';

const COLLAPSED_FOLDERS_STORAGE_VERSION = 1;
const MAX_STORED_FOLDER_KEYS = 500;

type FolderStorage = Pick<Storage, 'getItem' | 'setItem'>;

function normalizeFolderKeys(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  const normalized = new Set<string>();
  for (const value of values) {
    if (typeof value !== 'string') continue;
    const key = value.trim();
    if (!key) continue;
    normalized.add(key);
    if (normalized.size >= MAX_STORED_FOLDER_KEYS) break;
  }
  return [...normalized];
}

export function parseCollapsedFolders(raw: string | null): Set<string> {
  if (!raw) return new Set();
  try {
    const parsed = JSON.parse(raw) as { version?: unknown; collapsed?: unknown };
    if (parsed?.version !== COLLAPSED_FOLDERS_STORAGE_VERSION) return new Set();
    return new Set(normalizeFolderKeys(parsed.collapsed));
  } catch {
    return new Set();
  }
}

export function serializeCollapsedFolders(keys: ReadonlySet<string>): string {
  return JSON.stringify({
    version: COLLAPSED_FOLDERS_STORAGE_VERSION,
    collapsed: normalizeFolderKeys([...keys]),
  });
}

function browserStorage(): FolderStorage | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

export function loadCollapsedFolders(
  storage: FolderStorage | undefined = browserStorage(),
): Set<string> {
  if (!storage) return new Set();
  try {
    return parseCollapsedFolders(
      storage.getItem(COLLAPSED_FOLDERS_STORAGE_KEY)
        ?? storage.getItem(LEGACY_COLLAPSED_FOLDERS_STORAGE_KEY),
    );
  } catch {
    return new Set();
  }
}

export function saveCollapsedFolders(
  keys: ReadonlySet<string>,
  storage: FolderStorage | undefined = browserStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(
      COLLAPSED_FOLDERS_STORAGE_KEY,
      serializeCollapsedFolders(keys),
    );
  } catch {
    // A blocked/full localStorage must not break channel navigation.
  }
}

export function toggleCollapsedFolder(
  keys: ReadonlySet<string>,
  folderKey: string,
): Set<string> {
  const next = new Set(keys);
  if (next.has(folderKey)) next.delete(folderKey);
  else next.add(folderKey);
  return next;
}

export function expandFolder(
  keys: ReadonlySet<string>,
  folderKey: string | undefined,
): ReadonlySet<string> {
  if (!folderKey || !keys.has(folderKey)) return keys;
  const next = new Set(keys);
  next.delete(folderKey);
  return next;
}
