import { useCallback, useEffect, useState } from 'react';
import { readBrowserPreference, writeBrowserPreference, type BrowserPreference } from '../state/browserPreferences';
import { TIME_ZONE_PATTERN } from './timeZones';

// Publication analysis (hours, weekdays, days, years and date filters) is
// counted in the viewer's own time zone by default, or in UTC on request.
export type TimeBasis = 'local' | 'utc';

export const TIME_BASIS_PREFERENCE: BrowserPreference<TimeBasis> = {
  key: 'lic.explorer.time-basis',
  version: 1,
  defaultValue: 'local',
  isValid: (value): value is TimeBasis => value === 'local' || value === 'utc',
};
const CHANGED = 'lic-time-basis-changed';

export function localTimeZone(): string {
  try {
    const zone = new Intl.DateTimeFormat().resolvedOptions().timeZone;
    return zone && TIME_ZONE_PATTERN.test(zone) && zone !== 'Etc/UTC' ? zone : 'UTC';
  } catch {
    return 'UTC';
  }
}

export function timeZoneFor(basis: TimeBasis): string {
  return basis === 'utc' ? 'UTC' : localTimeZone();
}

export function useTimeBasis(): { basis: TimeBasis; zone: string; setBasis: (next: TimeBasis) => void } {
  const [basis, setState] = useState<TimeBasis>(() => readBrowserPreference(TIME_BASIS_PREFERENCE));
  useEffect(() => {
    const reload = () => setState(readBrowserPreference(TIME_BASIS_PREFERENCE));
    const onStorage = (event: StorageEvent) => { if (event.key === TIME_BASIS_PREFERENCE.key) reload(); };
    window.addEventListener(CHANGED, reload);
    window.addEventListener('storage', onStorage);
    return () => { window.removeEventListener(CHANGED, reload); window.removeEventListener('storage', onStorage); };
  }, []);
  const setBasis = useCallback((next: TimeBasis) => {
    writeBrowserPreference(TIME_BASIS_PREFERENCE, next);
    setState(next);
    window.dispatchEvent(new Event(CHANGED));
  }, []);
  return { basis, zone: timeZoneFor(basis), setBasis };
}
