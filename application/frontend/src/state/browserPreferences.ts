// Display preferences only: never store saved work or applied queries here.
export type BrowserPreference<T> = {
  key: string;
  legacyKey?: string;
  version: number;
  defaultValue: T;
  isValid: (value: unknown) => value is T;
};

type PreferenceStorage = Pick<Storage, 'getItem' | 'setItem'>;

function browserStorage(): PreferenceStorage | undefined {
  try {
    return typeof window === 'undefined' ? undefined : window.localStorage;
  } catch {
    return undefined;
  }
}

export function readBrowserPreference<T>(
  preference: BrowserPreference<T>,
  storage: PreferenceStorage | undefined = browserStorage(),
): T {
  try {
    const raw = storage?.getItem(preference.key)
      ?? (preference.legacyKey ? storage?.getItem(preference.legacyKey) : null);
    const stored: unknown = raw ? JSON.parse(raw) : null;
    if (stored && typeof stored === 'object' && 'version' in stored
      && stored.version === preference.version && 'value' in stored
      && preference.isValid(stored.value)) return stored.value;
  } catch {
    // Unavailable/corrupt browser storage must not block the application.
  }
  return preference.defaultValue;
}

export function writeBrowserPreference<T>(
  preference: BrowserPreference<T>,
  value: T,
  storage: PreferenceStorage | undefined = browserStorage(),
): void {
  if (!preference.isValid(value)) return;
  try {
    storage?.setItem(preference.key, JSON.stringify({ version: preference.version, value }));
  } catch {
    // Keep the user's in-session choice even when persistence is unavailable.
  }
}
