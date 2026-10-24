import { ExplorerContractError } from './contracts.ts';
import {
  CATALOGUE_VIEW_PRESETS,
  CATALOGUE_DURATION_PRESETS,
  normalizeCatalogueTimestamp,
  CATALOGUE_NUMERIC_GROUPS, normalizeCatalogueRequestFilters,
} from './catalogueQuery.ts';

export type CatalogueFacetPayload = {
  contract_version: 1;
  channel_id: string;
  generated_at: string;
  count_scope: 'applied';
  matched_count: number;
  facets: {
    views: Array<{ key: string; label: string; min: number | null; max: number | null; count: number }>;
    duration: Array<{ key: string; label: string; min: number | null; max: number | null; count: number }>;
    likes?: Array<{ key: string; label: string; min: number | null; max: number | null; count: number }>;
    comments?: Array<{ key: string; label: string; min: number | null; max: number | null; count: number }>;
    title_length?: Array<{ key: string; label: string; min: number | null; max: number | null; count: number }>;
    tag_count?: Array<{ key: string; label: string; min: number | null; max: number | null; count: number }>;
    published: { years: Array<{ year: number; count: number }>; older: { before_year: number; count: number } | null };
  };
  coverage: {
    scope: 'typed_long_inventory';
    complete: boolean;
    truncated: boolean;
    scan_limit: number | null;
    scan_mode?: 'full_catalogue';
    scanned_count: number;
    eligible_count: number;
    missing_video_row_count: number;
    video_rows_withheld_stale: number;
    availability_excluded_count: number;
    future_excluded_count: number;
    transactional_snapshot: false;
  };
};

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail();
  return value as Record<string, unknown>;
}
function fail(): never { throw new ExplorerContractError('The server returned an incompatible catalogue filter contract.'); }
function count(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) >= 0; }

// Old servers ignore unknown query parameters. An echo is mandatory before using
// opt-in results, otherwise a filtered URL could silently show an unfiltered set.
export function validateCatalogueEcho(payload: unknown, requested: string): void {
  const root = record(payload);
  const filters = record(root.filters);
  try {
    if (JSON.stringify(normalizeCatalogueRequestFilters(filters.catalogue_filters)) !== requested) fail();
  } catch { fail(); }
  if ('pagination' in root) {
    const pagination = record(root.pagination);
    if (!count(pagination.matched_count)
      || (pagination.returned_count !== undefined
        && (!count(pagination.returned_count) || pagination.returned_count > pagination.matched_count))) fail();
  }
}

export function parseCatalogueFacets(payload: unknown, channelId: string, requested: string): CatalogueFacetPayload {
  const root = record(payload);
  if (root.contract_version !== 1 || root.channel_id !== channelId || root.count_scope !== 'applied'
    || !count(root.matched_count)) fail();
  try { normalizeCatalogueTimestamp(root.generated_at, 'generated_at'); } catch { fail(); }
  validateCatalogueEcho(payload, requested);
  const facets = record(root.facets);
  for (const [name, presets] of [['views', CATALOGUE_VIEW_PRESETS], ['duration', CATALOGUE_DURATION_PRESETS]] as const) {
    const rows = facets[name];
    if (!Array.isArray(rows) || rows.length !== presets.length) fail();
    rows.forEach((value, index) => {
      const row = record(value), preset = presets[index];
      if (row.key !== preset.key || row.label !== preset.label || row.min !== preset.min
        || row.max !== preset.max || !count(row.count)) fail();
    });
  }
  for (const group of CATALOGUE_NUMERIC_GROUPS.slice(2)) {
    if (!group.facet || facets[group.group] === undefined) continue;
    const rows = facets[group.group];
    if (!Array.isArray(rows) || rows.length !== group.presets?.length) fail();
    rows.forEach((value, index) => {
      const row = record(value), preset = group.presets![index];
      if (row.key !== preset.key || row.label !== preset.label || row.min !== preset.min || row.max !== preset.max || !count(row.count)) fail();
    });
  }
  const published = record(facets.published);
  if (!Array.isArray(published.years) || published.years.length > 3) fail();
  let previous = 10000;
  published.years.forEach((value) => {
    const row = record(value);
    if (!count(row.year) || row.year < 1 || row.year >= previous || !count(row.count)) fail();
    previous = row.year;
  });
  if (published.older !== null) {
    const older = record(published.older);
    if (published.years.length !== 3 || older.before_year !== previous || !count(older.count)) fail();
  }
  const coverage = record(root.coverage), source = record(root.source);
  const needsGrowth = Boolean(normalizeCatalogueRequestFilters(JSON.parse(requested)).growth);
  if (typeof coverage.complete !== 'boolean' || typeof coverage.truncated !== 'boolean'
    || coverage.scope !== 'typed_long_inventory'
    || (coverage.scan_mode === 'full_catalogue'
      ? coverage.scan_limit !== null
      : !count(coverage.scan_limit) || coverage.scan_limit < 1)
    || !count(coverage.scanned_count) || !count(coverage.eligible_count)
    || !count(coverage.missing_video_row_count) || !count(coverage.video_rows_withheld_stale)
    || !count(coverage.availability_excluded_count) || !count(coverage.future_excluded_count)
    || coverage.transactional_snapshot !== false
    || coverage.complete !== (!coverage.truncated && coverage.missing_video_row_count === 0 && coverage.video_rows_withheld_stale === 0)
    || (coverage.scan_limit !== null && coverage.scanned_count > Number(coverage.scan_limit))
    || coverage.eligible_count + coverage.missing_video_row_count + coverage.video_rows_withheld_stale
      + coverage.availability_excluded_count + coverage.future_excluded_count > coverage.scanned_count
    || root.matched_count > coverage.eligible_count
    || source.kind !== 'derived_catalogue_filter_facets' || source.youtube_requests !== false
    || source.database_writes !== false || source.snapshots_used !== needsGrowth
    || (needsGrowth && source.growth_evidence !== 'same_browse_generation')) fail();
  for (const group of CATALOGUE_NUMERIC_GROUPS.filter((group) => group.facet)) {
    const rows = facets[group.group] as Array<{ count: number }> | undefined;
    const total = (rows ?? []).reduce((sum, row) => sum + row.count, 0);
    if (total > coverage.eligible_count) fail();
  }
  const publishedTotal = (published.years as Array<{ count: number }>).reduce((sum, row) => sum + row.count, 0)
    + (published.older === null ? 0 : (published.older as { count: number }).count);
  if (publishedTotal > coverage.eligible_count) fail();
  return root as unknown as CatalogueFacetPayload;
}
