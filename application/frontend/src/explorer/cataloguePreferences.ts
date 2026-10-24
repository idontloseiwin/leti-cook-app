import type { BrowserPreference } from '../state/browserPreferences';

export type CatalogueDensity = 'large' | 'compact';

export const catalogueDensityPreference: BrowserPreference<CatalogueDensity> = {
  key: 'leti-cook.explorer-v2.catalogue-density.v1',
  legacyKey: 'let-it-cook.explorer-v2.catalogue-density.v1',
  version: 1,
  defaultValue: 'large',
  isValid: (value): value is CatalogueDensity => value === 'large' || value === 'compact',
};
