import { EXPLORER_BOOKMARKS_STORAGE_KEY, LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY } from './bookmarks.ts';
import { COLLAPSED_FOLDERS_STORAGE_KEY, LEGACY_COLLAPSED_FOLDERS_STORAGE_KEY } from './folderCollapse.ts';

type StorageEventTarget = Pick<Window, 'addEventListener' | 'removeEventListener'>;

const SHARED_PREFERENCE_KEYS: ReadonlySet<string> = new Set([
  EXPLORER_BOOKMARKS_STORAGE_KEY,
  COLLAPSED_FOLDERS_STORAGE_KEY,
  LEGACY_EXPLORER_BOOKMARKS_STORAGE_KEY,
  LEGACY_COLLAPSED_FOLDERS_STORAGE_KEY,
]);

// Bookmarks and collapsed folders are saved whole from memory on every change.
// When another tab changes them, reload them here so this tab's next change
// does not write its older copy back. Returns the unsubscribe function.
export function watchSharedPreferences(target: StorageEventTarget, reload: () => void): () => void {
  const listener = (event: StorageEvent) => {
    // A null key means another tab cleared the whole storage.
    if (event.key === null || SHARED_PREFERENCE_KEYS.has(event.key)) reload();
  };
  target.addEventListener('storage', listener);
  return () => target.removeEventListener('storage', listener);
}
