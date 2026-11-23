// `/research/capabilities` → Library gates for one video type.
// Input is the parsed capabilities object (researchQuery.loadResearchCapabilities
// shape plus 's `consistency_modes` and `frozen`). Pure TypeScript.
import { DERIVED_FIELDS, FIELDS, FROZEN_MAX_ROWS, FROZEN_TTL_SECONDS, OPERATIONS, type LibraryVideoType } from './wire.ts';

export type ResearchCapabilitiesInput = {
  fields: ReadonlyArray<{ id: string; enabled_video_types?: readonly string[] }>;
  operations?: readonly string[];
  consistency_modes?: readonly string[];
  frozen?: { ttl_seconds?: number; max_rows?: number; max_page_size?: number };
};

export type LibraryGates = {
  videoType: LibraryVideoType;
  field: (id: string) => boolean;
  operation: (id: string) => boolean;
  frozen: { available: boolean; ttlSeconds: number; maxRows: number };
  /** Old `filter_facets` (BASELINE). */
  facets: boolean;
  /** Old `baseline` gate of the computed filters: all four computed fields. */
  derivedFilters: boolean;
  /** Old `video_growth` (DERIVED); a refused attempt still turns it off (videoGrowth.ts). */
  growth: boolean;
  /** Old `video_history`. */
  history: boolean;
  /** Old `video_launch` (LAUNCH ∧ BASELINE ∧ DERIVED). */
  launch: boolean;
  /** Old `baseline` of the Overview: the four Overview operations (one policy, descriptive_analytics). */
  overview: boolean;
};

export function libraryGates(capabilities: ResearchCapabilitiesInput, videoType: LibraryVideoType): LibraryGates {
  const fields = new Set(capabilities.fields
    .filter((item) => !item.enabled_video_types || item.enabled_video_types.includes(videoType))
    .map((item) => item.id));
  const operations = new Set(capabilities.operations ?? []);
  const field = (id: string) => fields.has(id);
  const operation = (id: string) => operations.has(id);
  const limits = capabilities.frozen ?? {};
  const positive = (value: unknown, fallback: number) => (Number.isSafeInteger(value) && Number(value) > 0 ? Number(value) : fallback);
  return {
    videoType,
    field,
    operation,
    frozen: {
      available: (capabilities.consistency_modes ?? []).includes('frozen'),
      ttlSeconds: positive(limits.ttl_seconds, FROZEN_TTL_SECONDS),
      maxRows: positive(limits.max_rows, FROZEN_MAX_ROWS),
    },
    facets: operation(OPERATIONS.facets),
    derivedFilters: DERIVED_FIELDS.every(field),
    growth: field(FIELDS.viewsGained28d),
    history: operation(OPERATIONS.observedHistory),
    launch: operation(OPERATIONS.launchComparison),
    overview: [OPERATIONS.fingerprint, OPERATIONS.rhythm, OPERATIONS.years, OPERATIONS.breakdown].every(operation),
  };
}
