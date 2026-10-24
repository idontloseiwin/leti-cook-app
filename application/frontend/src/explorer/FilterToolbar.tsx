import { Search, SlidersHorizontal, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ChannelExplorerCategory } from '../types';
import { EXPLORER_SORT_LABELS, VIDEO_GROWTH_SORT_FIELD } from './filters';
import {
  CATALOGUE_NUMERIC_GROUPS,
  catalogueCalendarEnd, catalogueExclusiveEnd, normalizeCatalogueGrowth, CatalogueQueryError,
  type CatalogueGrowthFilters,
  type CatalogueConditions, type CatalogueNumericGroup, type CataloguePresetGroup, type CatalogueFilterGroup,
} from './catalogueQuery';
import type { CatalogueFacetPayload } from './catalogueFacets';
import type { CatalogueDensity } from './cataloguePreferences';
import type { ExplorerFilterErrors, ExplorerFilterKey, ResourceState } from './model';
import { resourcePayload } from './resourceState';
import { formatVideoCategory } from './videoPresentation';
import { videoNoun } from './format';
import { useTimeBasis } from './timeBasis';
import { timeZoneLabel } from './timeZones';

type Group = CatalogueFilterGroup;
type ImmediateField = 'q' | 'sort_by' | 'sort_order';
export type FilterToolbarProps = {
  draft: CatalogueConditions;
  applied: CatalogueConditions;
  categories: ChannelExplorerCategory[];
  errors: ExplorerFilterErrors;
  error: string;
  growthSortAvailable: boolean;
  derivedFiltersAvailable?: boolean;
  facetState: ResourceState<CatalogueFacetPayload>;
  facetsAvailable: boolean;
  matchedCount?: number;
  incomplete: boolean;
  resetKey: string;
  onChange: (draft: CatalogueConditions) => void;
  onApply: () => boolean;
  onCancel: () => void;
  onImmediate: (key: ImmediateField, value: string) => void;
  onRemove: (group: Group) => void;
  onClear: () => void;
  onOpen: (open: boolean) => void;
  onRetryFacets: () => void;
  onSaveView?: () => void;
  saveViewDisabled?: boolean;
  savedViewMessage?: string;
};

export function FilterToolbar(props: FilterToolbarProps & {
  density?: CatalogueDensity;
  onDensityChange?: (density: CatalogueDensity) => void;
}) {
  // Publication dates and years count in the chosen time: local by default.
  const { zone } = useTimeBasis();
  const { draft, applied, errors, categories, onChange } = props;
  const [search, setSearch] = useState(applied.filters.q);
  const [calendarError, setCalendarError] = useState('');
  const [custom, setCustom] = useState<Set<string>>(() => new Set());
  const details = useRef<HTMLDetailsElement>(null);
  const immediate = useRef(props.onImmediate);
  immediate.current = props.onImmediate;
  useEffect(() => setSearch(applied.filters.q), [applied.filters.q, props.resetKey]);
  useEffect(() => setCalendarError(''), [props.resetKey]);
  useEffect(() => {
    if (search === applied.filters.q) return;
    const timeout = window.setTimeout(() => immediate.current('q', search), 350);
    return () => window.clearTimeout(timeout);
  }, [search, applied.filters.q, props.resetKey]);
  const facets = resourcePayload(props.facetState);
  const visibleErrors = { ...errors, published_before: calendarError || errors.published_before };
  const fieldProps = (key: ExplorerFilterKey) => ({
    id: `explorer-filter-${key}`, 'aria-invalid': Boolean(visibleErrors[key]),
    'aria-describedby': visibleErrors[key] ? `explorer-filter-${key}-error` : undefined,
  });
  const errorFor = (key: ExplorerFilterKey) => visibleErrors[key]
    ? <small role="alert" id={`explorer-filter-${key}-error`}>{visibleErrors[key]}</small> : null;
  const changeField = (key: ExplorerFilterKey, value: string) => {
    const facets = { ...draft.facets };
    if (key === 'published_from' || key === 'published_before') { facets.published_years = []; facets.published_older_than = null; }
    for (const group of CATALOGUE_NUMERIC_GROUPS) {
      if (group.facet && (key === group.min || key === group.max)) facets[group.facet] = [];
    }
    onChange({ ...draft, filters: { ...draft.filters, [key]: value }, facets });
  };
  const close = (cancel: boolean) => {
    if (cancel) props.onCancel();
    setCalendarError('');
    setCustom(new Set());
    if (details.current) details.current.open = false;
  };
  const growthDraft = draft.facets.growth ?? {};
  const changeGrowth = (key: keyof CatalogueGrowthFilters, value: string | true | undefined) => {
    const growth = { ...growthDraft };
    if (value === undefined || value === '') delete growth[key];
    else if (key === 'full_history') growth.full_history = true;
    else growth[key] = String(value);
    onChange({ ...draft, facets: { ...draft.facets, growth } });
  };
  let growthErrorField = '';
  if (props.error) {
    try { normalizeCatalogueGrowth(growthDraft); }
    catch (error) { if (error instanceof CatalogueQueryError) growthErrorField = error.field; }
  }
  const presets = (group: CatalogueNumericGroup) => {
    const definition = CATALOGUE_NUMERIC_GROUPS.find((item) => item.group === group)!;
    const { min, max, facet: key, presets: source = [], label, unit } = definition;
    const selected = key ? draft.facets[key] ?? [] : [];
    const unavailable = definition.derived && !props.derivedFiltersAvailable;
    const toggle = (id: string) => {
      if (!key) return;
      onChange({ ...draft,
        filters: { ...draft.filters, [min]: '', [max]: '' },
        facets: { ...draft.facets, [key]: selected.includes(id) ? selected.filter((value) => value !== id) : [...selected, id] },
      });
      setCustom((current) => new Set([...current].filter((value) => value !== group)));
    };
    return <fieldset key={group} className="catalogue-filter-group" disabled={unavailable}>
      <legend>{label}{unit === '%' ? ' · % of views' : ''}</legend>
      {unavailable && <small>Calculated filters unavailable</small>}
      <div className="catalogue-preset-options">
        {source.map((preset) => <label key={preset.key} className={selected.includes(preset.key) ? 'is-selected' : ''}>
          <input type="checkbox" checked={selected.includes(preset.key)} onChange={() => toggle(preset.key)} />
          <span>{preset.label}</span>
          <small>{facets?.facets[group as CataloguePresetGroup]?.find((row) => row.key === preset.key)?.count.toLocaleString('en') ?? '—'}</small>
        </label>)}
      </div>
      {key && <button type="button" className="catalogue-custom-toggle" aria-expanded={custom.has(group) || Boolean(draft.filters[min] || draft.filters[max])}
        onClick={() => {
          onChange({ ...draft, facets: { ...draft.facets, [key]: [] } });
          setCustom((current) => new Set([...current, group]));
        }}>Custom</button>}
      {(!key || custom.has(group) || draft.filters[min] || draft.filters[max]) && <div className="catalogue-custom-range">
        {([min, max] as ExplorerFilterKey[]).map((field, index) => <label key={field}>
          <span>{index === 0 ? 'Minimum' : 'Maximum'}{unit ? `, ${unit}` : ''}</span>
          <input {...fieldProps(field)} type="number" min="0" step={unit === '%' ? '0.000001' : '1'} placeholder="Any" value={draft.filters[field] ?? ''}
            onChange={(event) => changeField(field, event.target.value)} />{errorFor(field)}
        </label>)}
      </div>}
      {group === 'title_length' && <small>Unicode characters</small>}
      {unit === '%' && <small>Current lifetime counters; zero views or missing counters are unavailable.</small>}
    </fieldset>;
  };
  const chips: Array<{ group: Group; label: string }> = [];
  if (applied.filters.q) chips.push({ group: 'q', label: `Search: ${applied.filters.q}` });
  for (const definition of CATALOGUE_NUMERIC_GROUPS) {
    const { group, label, presets: source = [], unit } = definition;
    const selected = definition.facet ? applied.facets[definition.facet] ?? [] : [];
    const min = applied.filters[definition.min], max = applied.filters[definition.max];
    if (selected.length || min || max) chips.push({ group, label: `${label}: ${
      selected.length ? source.filter((row) => selected.includes(row.key)).map((row) => row.label).join(' or ')
        : `${min || '0'}–${max || '∞'}${unit ? ` ${unit}` : ''}`}` });
  }
  const years = [...applied.facets.published_years.map(String), ...(applied.facets.published_older_than ? [`before ${applied.facets.published_older_than}`] : [])];
  const growthApplied = applied.facets.growth;
  if (growthApplied?.min_views_gained !== undefined || growthApplied?.max_views_gained !== undefined) {
    chips.push({ group: 'views_gained', label: `Views gained: ${growthApplied.min_views_gained ?? '−∞'}–${growthApplied.max_views_gained ?? '∞'}` });
  }
  if (growthApplied?.min_tracked_days !== undefined) chips.push({ group: 'tracked_days', label: `Tracked days: ≥${growthApplied.min_tracked_days}` });
  if (growthApplied?.full_history) chips.push({ group: 'full_history', label: 'Full history (28-day comparison available)' });
  if (years.length || applied.filters.published_from || applied.filters.published_before) chips.push({ group: 'published', label: `Published: ${years.length ? years.join(' or ') : `${applied.filters.published_from || 'Any'} – ${catalogueCalendarEnd(applied.filters.published_before) || 'Any'} (${timeZoneLabel(zone)})`}` });
  if (applied.filters.category_id) chips.push({ group: 'category', label: `Category: ${formatVideoCategory(categories.find((row) => row.category_id === applied.filters.category_id)?.category_name)}` });
  if (applied.facets.cohort) chips.push({ group: 'cohort', label: 'Overview publication scope' });
  const availableYears = [...new Set([...(facets?.facets.published.years.map((row) => row.year) ?? []), ...draft.facets.published_years])].sort((a, b) => b - a);
  const older = draft.facets.published_older_than ?? facets?.facets.published.older?.before_year;
  const setPublication = (years: number[], before: number | null) => {
    setCalendarError('');
    onChange({ ...draft,
      filters: { ...draft.filters, published_from: '', published_before: '' },
      facets: { ...draft.facets, published_years: years, published_older_than: before },
    });
  };

  return <section className="explorer-scope explorer-v2-filter-toolbar" aria-label="Explorer scope and filters">
    <form className="explorer-filter-form explorer-v2-filter-form" noValidate onSubmit={(event) => {
      event.preventDefault();
      if (details.current?.open) { if (!calendarError && props.onApply()) close(false); }
      else immediate.current('q', search);
    }}>
      <div className="explorer-filter-primary explorer-v2-filter-primary">
        <label className="explorer-v2-filter-search">
          <span className="explorer-sr-only">Search videos</span>
          <span className="explorer-input-with-icon"><Search size={16} aria-hidden="true" />
            <input {...fieldProps('q')} value={search} placeholder="Search videos" maxLength={100}
              onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => {
                if (event.key === 'Enter') { event.preventDefault(); immediate.current('q', search); }
              }} />
          </span>{errorFor('q')}
        </label>
        <details ref={details} className="explorer-advanced-filters explorer-v2-advanced-filters" onToggle={() => {
          props.onOpen(Boolean(details.current?.open));
          if (!details.current?.open) { props.onCancel(); setCustom(new Set()); setCalendarError(''); }
        }} onKeyDown={(event) => {
          if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); details.current?.querySelector('summary')?.focus(); }
        }}>
          <summary><SlidersHorizontal size={16} aria-hidden="true" />Filters{chips.filter((chip) => chip.group !== 'q').length > 0 ? ` · ${chips.filter((chip) => chip.group !== 'q').length}` : ''}</summary>
          <div className="explorer-filter-grid explorer-v2-filter-grid catalogue-facet-panel">
            <div className="catalogue-facet-status" role="status">
              {props.facetsAvailable ? (facets ? `Counts for applied search and other filters${facets.coverage.complete ? '' : ' · Partial catalogue'}` : 'Loading group counts…') : 'Group counts are unavailable. Custom filters remain available.'}
              {(props.facetState.kind === 'error' || props.facetState.kind === 'incompatible') && <button type="button" onClick={props.onRetryFacets}>Retry counts</button>}
            </div>
            {CATALOGUE_NUMERIC_GROUPS.map((group) => presets(group.group))}
            <fieldset className="catalogue-filter-group" disabled={!props.growthSortAvailable}>
              <legend>Observed growth · 28-day target</legend>
              {!props.growthSortAvailable && <small>Growth filters unavailable</small>}
              <div className="catalogue-custom-range">
                {(['min_views_gained', 'max_views_gained', 'min_tracked_days'] as const).map((field) => <label key={field}>
                  <span>{field === 'min_views_gained' ? 'Minimum views gained' : field === 'max_views_gained' ? 'Maximum views gained' : 'Minimum tracked days'}</span>
                  <input id={`explorer-filter-${field}`} type="text" inputMode={field === 'min_tracked_days' ? 'numeric' : 'text'} maxLength={32}
                    aria-invalid={growthErrorField === `facets.growth.${field}`} aria-describedby={growthErrorField === `facets.growth.${field}` ? 'catalogue-filter-error' : undefined}
                    placeholder="Any" value={growthDraft[field] ?? ''} onChange={(event) => changeGrowth(field, event.target.value)} />
                </label>)}
              </div>
              <div className="catalogue-preset-options"><label className={growthDraft.full_history ? 'is-selected' : ''}>
                <input id="explorer-filter-full_history" type="checkbox" checked={growthDraft.full_history === true}
                  onChange={(event) => changeGrowth('full_history', event.target.checked ? true : undefined)} />
                <span>Full history (28-day comparison available)</span>
              </label></div>
              <small>Measured change, not growth speed. Tracked days use the actual interval; full history does not require daily snapshots.</small>
            </fieldset>
            <fieldset className="catalogue-filter-group"><legend>Published · {timeZoneLabel(zone)}</legend>
              <div className="catalogue-preset-options">
                {availableYears.map((year) => <label key={year} className={draft.facets.published_years.includes(year) ? 'is-selected' : ''}>
                  <input type="checkbox" checked={draft.facets.published_years.includes(year)} onChange={() => setPublication(draft.facets.published_years.includes(year) ? draft.facets.published_years.filter((value) => value !== year) : [...draft.facets.published_years, year], draft.facets.published_older_than)} />
                  <span>{year}</span><small>{facets?.facets.published.years.find((row) => row.year === year)?.count.toLocaleString('en') ?? '—'}</small>
                </label>)}
                {older && <label className={draft.facets.published_older_than ? 'is-selected' : ''}>
                  <input type="checkbox" checked={Boolean(draft.facets.published_older_than)} onChange={() => setPublication(draft.facets.published_years, draft.facets.published_older_than ? null : older)} />
                  <span>Older <small>(before {older})</small></span><small>{facets?.facets.published.older?.before_year === older ? facets.facets.published.older.count.toLocaleString('en') : '—'}</small>
                </label>}
              </div>
              <button type="button" className="catalogue-custom-toggle" onClick={() => { setPublication([], null); setCustom((current) => new Set([...current, 'published'])); }}>Custom</button>
              {(custom.has('published') || draft.filters.published_from || draft.filters.published_before) && <div className="catalogue-custom-range">
                <label><span>From</span><input {...fieldProps('published_from')} type="date" min="0001-01-01" max="9999-12-31" value={draft.filters.published_from} onChange={(event) => changeField('published_from', event.target.value)} />{errorFor('published_from')}</label>
                <label><span>Through (inclusive)</span><input {...fieldProps('published_before')} type="date" min="0001-01-01" max="9999-12-30" value={catalogueCalendarEnd(draft.filters.published_before)} onChange={(event) => {
                  try {
                    const exclusive = catalogueExclusiveEnd(event.target.value);
                    setCalendarError(''); changeField('published_before', exclusive);
                  } catch (error) { setCalendarError(error instanceof Error ? error.message : 'Choose a supported calendar end date.'); }
                }} />{errorFor('published_before')}</label>
              </div>}
              <label className="catalogue-category"><span>Official category</span><select {...fieldProps('category_id')} value={draft.filters.category_id} onChange={(event) => changeField('category_id', event.target.value)}>
                <option value="">All categories</option>
                {draft.filters.category_id && !categories.some((row) => row.category_id === draft.filters.category_id) && <option value={draft.filters.category_id}>Unknown</option>}
                {categories.map((row) => <option key={row.category_id} value={row.category_id}>{formatVideoCategory(row.category_name)}</option>)}
              </select></label>
            </fieldset>
            <div className="catalogue-filter-panel-actions">
              {props.error && <span role="alert" id="catalogue-filter-error">{props.error}</span>}
              <button type="button" onClick={() => close(true)}>Cancel</button><button type="submit" className="explorer-primary-button">Apply filters</button>
            </div>
          </div>
        </details>
        <label><span className="explorer-sr-only">Sort catalogue</span><select {...fieldProps('sort_by')} value={applied.filters.sort_by} onChange={(event) => props.onImmediate('sort_by', event.target.value)}>
          {(props.growthSortAvailable || applied.filters.sort_by === VIDEO_GROWTH_SORT_FIELD) && <option value={VIDEO_GROWTH_SORT_FIELD}>Sort: Views gained</option>}
          {Object.entries(EXPLORER_SORT_LABELS).map(([value, label]) => <option key={value} value={value}>Sort: {label}</option>)}
        </select></label>
        <label><span className="explorer-sr-only">Sort direction</span><select {...fieldProps('sort_order')} value={applied.filters.sort_order} onChange={(event) => props.onImmediate('sort_order', event.target.value)}><option value="desc">Descending</option><option value="asc">Ascending</option></select></label>
        <div className="catalogue-display-controls">
          {props.onSaveView && <button type="button" className="catalogue-save-view" disabled={props.saveViewDisabled} onClick={props.onSaveView}>Save view</button>}
          {props.savedViewMessage && <span className="saved-view-confirmation" role="status">{props.savedViewMessage}</span>}
          <span className="catalogue-result-count" role="status">{props.matchedCount === undefined ? '— videos' : `${props.matchedCount.toLocaleString('en')} ${videoNoun(props.matchedCount)}${props.incomplete ? ' · partial' : ''}`}</span>
          {props.onDensityChange && <div className="catalogue-density-toggle" role="group" aria-label="Video grid density">
            <button type="button" aria-label="Large grid" aria-pressed={(props.density ?? 'large') === 'large'} onClick={() => props.onDensityChange?.('large')}>Large</button>
            <button type="button" aria-label="Compact grid" aria-pressed={props.density === 'compact'} onClick={() => props.onDensityChange?.('compact')}>Compact</button>
          </div>}
        </div>
      </div>
      {chips.length > 0 && <div className="explorer-filter-chips explorer-v2-applied-filters">
        {chips.map((chip) => <button type="button" className="explorer-filter-chip" key={chip.group} onClick={() => props.onRemove(chip.group)} aria-label={`Remove ${chip.label}`}>{chip.label}<X size={12} aria-hidden="true" /></button>)}
        <button type="button" className="catalogue-clear" onClick={() => { setSearch(''); setCalendarError(''); props.onClear(); }}>Clear all</button>
      </div>}
    </form>
  </section>;
}
