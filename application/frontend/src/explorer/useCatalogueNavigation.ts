import { useEffect, useState } from 'react';
import {
  createInitialCatalogueConditions,
  readCatalogueUrl,
  writeCatalogueUrl,
  type CatalogueUrlState,
} from './catalogueQuery';

function readLocation(): { state: CatalogueUrlState; error: string } {
  try {
    return { state: readCatalogueUrl(new URL(window.location.href)), error: '' };
  } catch (error) {
    return {
      state: { section: 'videos', overviewPeriod: 'max', conditions: createInitialCatalogueConditions() },
      error: error instanceof Error ? error.message : 'This catalogue URL is not supported.',
    };
  }
}

// URL owns applied work; drafts, selection and fetched pages have separate lifetimes.
// No synchronization effect writes history: Back must never become another push.
export function useCatalogueNavigation() {
  const [location, setLocation] = useState(readLocation);
  const [restored, setRestored] = useState(0);
  useEffect(() => {
    const pathname = window.location.pathname;
    const restore = () => {
      if (window.location.pathname !== pathname) return;
      setLocation(readLocation());
      setRestored((value) => value + 1);
    };
    window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, []);

  const navigate = (state: CatalogueUrlState, mode: 'push' | 'replace' = 'push') => {
    const next = writeCatalogueUrl(new URL(window.location.href), state);
    if (next.href !== window.location.href) {
      if (mode === 'replace') window.history.replaceState(window.history.state, '', next);
      else window.history.pushState({}, '', next);
    }
    setLocation({ state: readCatalogueUrl(next), error: '' });
  };
  return { ...location, restored, navigate };
}
