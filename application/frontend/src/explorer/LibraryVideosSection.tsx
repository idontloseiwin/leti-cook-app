import { expireResourceState } from './resourceState';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired } from '../sourceLifetime.ts';
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { isAbortError, requestJson } from './api';
import {
  createInitialCatalogueConditions, normalizeCatalogueConditions, catalogueAnalysisKey,
  clearCatalogueFilterGroup,
  catalogueUsesDerivedStaticFilters, catalogueUsesGrowthFilters, CatalogueQueryError, type CatalogueConditions,
  type CatalogueFilterGroup,
} from './catalogueQuery';
import { useCatalogueBrowse, type LibraryRequest } from './useCatalogueBrowse';
import {
  VIDEO_GROWTH_SORT_FIELD,
  validateExplorerFilters,
} from './filters';
import {
  isVideoGrowthEffectivelyAvailable,
  videoGrowthAttemptKey,
} from './videoGrowth';
import { ExplorerVideosWorkspace } from './ExplorerVideosWorkspace';
import { WatchSelection } from './AlertEditor';
import { SaveViewDialog } from './SaveViewDialog';
import type { SavedViewSnapshot } from './savedViews';
import type {
  ExplorerFilterErrors,
  ExplorerFilterKey,
  ResourceState,
} from './model';
import { resourcePayload } from './resourceState';
import type { LibraryShellContext } from './libraryShell';
import type { BrowseChannelFallback } from './ChannelHeader';
import { readBrowserPreference } from '../state/browserPreferences';
import type { VideoType } from '../types';
import { catalogueDensityPreference } from './cataloguePreferences';
import { BLOCKED_TEXT, pageTranslationClient } from './libraryData/conditions.ts';
import { libraryGates } from './libraryData/capabilities.ts';
import { librarySavedDefinition, savedLibraryVideoType } from './libraryData/savedLibrary.ts';
import { presetsFor } from './libraryData/presets.ts';
import { ROUTES } from './libraryData/wire.ts';
import { libraryErrorState } from './libraryData/errors.ts';
import { inspectorGates } from './libraryData/inspector.ts';
import {
  categoryLabels, facetGroups, facetsRequest, parseFacets, parseLibraryCapabilities,
  type LibraryCapabilities, type LibraryFacets,
} from './libraryData/videos.ts';
import {
  isWholeFrozenList, selectedIdsHandoff, selectionHandoff, type HandoffRefusal, type WorkshopHandoff,
} from './workshopHandoff';

// Videos of the Library page: applied and draft filters, the frozen session
// on the shared backend, group counts (facets), Save view, the workshop
// hand-off and the video grid workspace — for Long, Shorts and Streams.


/** The page's shared-backend reads: POST JSON through the one request function. */
export const libraryRequest: LibraryRequest = (path, body, signal) => requestJson(path, signal, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

/** Blocking text when the applied duration presets belong to another format. */
export const DURATION_OTHER_FORMAT_TEXT = 'These duration presets belong to another format. Choose this format’s duration presets; your other filters have been kept.';

// The last capabilities this page read: the next channel starts from them while they are re-read.
let knownCapabilities: LibraryCapabilities | undefined;

/** `/research/capabilities` of the Videos section, re-read on Reload. */
function useLibraryCapabilities(reloadToken: number) {
  const [state, setState] = useState<ResourceState<LibraryCapabilities>>(() => knownCapabilities && !sourceExpired(knownCapabilities)
    ? { kind: 'ready', scopeKey: 'capabilities', payload: knownCapabilities } : { kind: 'loading', scopeKey: 'capabilities' });
  useEffect(() => {
    const controller = new AbortController();
    setState((previous) => previous.kind === 'ready' ? previous : { kind: 'loading', scopeKey: 'capabilities' });
    void requestJson(ROUTES.capabilities, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        knownCapabilities = parseLibraryCapabilities(value);
        setState({ kind: 'ready', scopeKey: 'capabilities', payload: knownCapabilities });
      })
      .catch((error) => {
        if (!controller.signal.aborted && !isAbortError(error)) setState({ kind: 'error', scopeKey: 'capabilities', message: error instanceof Error ? error.message : String(error) });
      });
    return () => controller.abort();
  }, [reloadToken]);
  return state;
}

/** Workshop refusals in plain words: nothing is cut silently. */
export function handoffRefusalText(refusal: HandoffRefusal): string {
  switch (refusal.refused) {
    case 'nothing_selected': return refusal.count
      ? `The selected videos belong to another format (${refusal.count.toLocaleString('en')}). A study has one format.`
      : 'Select videos to send to the research workshop.';
    case 'too_many': return `At most 1,000 selected videos can be sent (${(refusal.count ?? 0).toLocaleString('en')} selected). Use “Open in research workshop” for the whole list.`;
    case 'too_large': return `This study would be too large for the research workshop (${(refusal.bytes ?? 0).toLocaleString('en')} bytes).`;
    default: return 'These conditions cannot open in the research workshop yet. Your selection is kept.';
  }
}

/** State and requests of the Videos section (called by the shell). */
export function useLibraryVideos(context: LibraryShellContext) {
  const {
    channelId, videoType, navigation, timeZone, videosVisible,
    catalogueReloadToken, setAnnouncement,
  } = context;
  const conditions = navigation.state.conditions;
  const applied = conditions.filters;
  const [draftConditions, setDraftConditions] = useState<CatalogueConditions>(conditions);
  const [saveSnapshot, setSaveSnapshot] = useState<SavedViewSnapshot>();
  const [savedMessage, setSavedMessage] = useState('');
  useEffect(() => setSavedMessage(''), [conditions]);
  const [filterError, setFilterError] = useState('');
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [facetReload, setFacetReload] = useState(0);
  const [facetState, setFacetState] = useState<ResourceState<LibraryFacets>>({ kind: 'idle' });
  const [filterErrors, setFilterErrors] = useState<ExplorerFilterErrors>({});
  const [failedGrowthAttemptKey, setFailedGrowthAttemptKey] = useState<string | null>(null);
  useSourceExpiry(facetState, () => setFacetState(expireResourceState), () => { if (videosVisible) setFacetReload(value => value + 1); });
  const capabilityState = useLibraryCapabilities(catalogueReloadToken);
  const capabilitiesSettled = capabilityState.kind !== 'loading';
  const gates = useMemo(() => capabilityState.kind === 'ready' ? libraryGates(capabilityState.payload, videoType) : null,
    [capabilityState, videoType]);
  const facetsAvailable = Boolean(gates?.facets);
  const derivedFiltersAvailable = Boolean(gates?.derivedFilters);
  const staticFiltersBlocked = capabilitiesSettled && catalogueUsesDerivedStaticFilters(conditions) && !derivedFiltersAvailable;
  const currentGrowthAttemptKey = videoGrowthAttemptKey(channelId, catalogueReloadToken);
  const growthSortAvailable = isVideoGrowthEffectivelyAvailable(
    Boolean(gates?.growth),
    failedGrowthAttemptKey,
    currentGrowthAttemptKey,
  );
  const growthFiltersActive = catalogueUsesGrowthFilters(conditions);
  const growthFiltersBlocked = capabilitiesSettled && growthFiltersActive && !growthSortAvailable;
  const durationPresets = presetsFor(videoType, 'duration');
  const durationOtherFormat = conditions.facets.duration_buckets.some((key) => !durationPresets?.some((preset) => preset.key === key));

  // One list per channel, format, conditions, sort and zone; one browse query per list and Reload.
  const currentListKey = useMemo(
    () => `${catalogueAnalysisKey(channelId, conditions)}\ntype:${videoType}\n${applied.sort_by}:${applied.sort_order}\nzone:${timeZone}`,
    [applied, conditions, channelId, videoType, timeZone],
  );
  const currentCatalogueScope = `${currentListKey}\nreload:${catalogueReloadToken}`;
  useEffect(() => {
    setDraftConditions(navigation.state.conditions);
    setFilterErrors({});
    setFilterError('');
  }, [navigation.restored]);

  // Selection follows the matched set (channel, format, conditions), not the sort; the remembered card follows the list.
  const currentAnalysisScope = useMemo(
    // Long keeps the key it always had; Shorts and Streams add their format.
    () => `${catalogueAnalysisKey(channelId, conditions)}${videoType === 'long' ? '' : `\ntype:${videoType}`}`,
    [conditions, channelId, videoType],
  );
  const browse = useCatalogueBrowse({
    channelId, videoType, conditions, timeZone, scopeKey: currentCatalogueScope, listKey: currentListKey, selectionScopeKey: currentAnalysisScope,
    enabled: videosVisible && capabilitiesSettled && !navigation.error && !staticFiltersBlocked && !growthFiltersBlocked
      && !durationOtherFormat && (growthSortAvailable || applied.sort_by !== VIDEO_GROWTH_SORT_FIELD),
    gates, growthAllowed: growthSortAvailable,
    onGrowthUnavailable: () => setFailedGrowthAttemptKey(currentGrowthAttemptKey),
    onAnnouncement: setAnnouncement,
    translate: (...args) => pageTranslationClient(requestJson).translate(...args),
    request: libraryRequest,
  });

  // Group counts: the same membership as the list, each group without its own condition.
  const facetReadsAvailable = facetsAvailable && Boolean(browse.translated) && !staticFiltersBlocked && !growthFiltersBlocked
    && !browse.pending;
  // Category labels name the chip, the cards and the Inspector line even with the panel closed.
  const facetGroupsWanted = gates ? (filtersOpen ? facetGroups(gates) : ['category' as const]) : [];
  const facetScope = `${browse.sessionKey ?? ''}\n${facetGroupsWanted.join(',')}\n${facetReload}:${catalogueReloadToken}`;
  const translated = browse.translated;
  useEffect(() => {
    if (!videosVisible || !facetReadsAvailable || !translated || navigation.error || !facetGroupsWanted.length) return;
    const controller = new AbortController();
    let cancelled = false;
    const groups = facetGroupsWanted;
    setFacetState({ kind: 'loading', scopeKey: facetScope });
    const body = facetsRequest(translated, { timeZone, groups, frozenRef: browse.frozenRef });
    void libraryRequest(ROUTES.operations, body, controller.signal)
      .then((raw) => {
        if (cancelled) return;
        const frozen = (body.parameters.population as { kind?: string } | undefined)?.kind === 'frozen';
        setFacetState({ kind: 'ready', scopeKey: facetScope, payload: parseFacets(raw, {
          videoType, groups, timeZone, ...(frozen && browse.state.kind !== 'loading' ? { frozenTotal: resourcePayload(browse.state)?.browse.total_count } : {}),
        }) });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        const state = libraryErrorState(error, 'facets');
        setFacetState({ kind: state.action === 'incompatible' ? 'incompatible' : 'error', scopeKey: facetScope, message: state.message ?? 'Group counts could not load.' });
      });
    return () => { cancelled = true; controller.abort(); };
  }, [videosVisible, facetReadsAvailable, facetScope, navigation.error, translated]);
  const facetsForScope = facetState.kind !== 'idle' && facetState.scopeKey === facetScope ? facetState : { kind: 'idle' as const };
  const visibleFacetState: ResourceState<LibraryFacets> = facetReadsAvailable && filtersOpen ? facetsForScope : { kind: 'idle' };
  // The last labels stay known while a new read runs.
  const [labels, setLabels] = useState<{ key: string; categories: LibraryFacets['categories'] }>({ key: '', categories: [] });
  const loadedFacets = resourcePayload(facetState);
  useEffect(() => {
    if (loadedFacets && !sourceExpired(loadedFacets)) setLabels({ key: channelId, categories: loadedFacets.categories });
  }, [loadedFacets, channelId]);
  const categories = labels.key === channelId ? labels.categories : [];

  const visibleRawState = browse.state;
  const rawPayload = resourcePayload(visibleRawState);
  const labelMap = useMemo(() => categoryLabels({ categories } as LibraryFacets), [categories]);
  const presentedState = useMemo(() => {
    if (!labelMap.size) return visibleRawState;
    const label = (payload: NonNullable<typeof rawPayload>) => ({ ...payload, videos: payload.videos.map((video) => (
      video.category_id && labelMap.has(video.category_id) ? { ...video, category_name: labelMap.get(video.category_id) } : video)) });
    if (visibleRawState.kind === 'ready' || visibleRawState.kind === 'updating') return { ...visibleRawState, payload: label(visibleRawState.payload) };
    if ((visibleRawState.kind === 'error' || visibleRawState.kind === 'incompatible') && visibleRawState.previous) {
      return { ...visibleRawState, previous: label(visibleRawState.previous) };
    }
    return visibleRawState;
  }, [visibleRawState, labelMap]);

  const commitConditions = (next: CatalogueConditions): boolean => {
    const errors = validateExplorerFilters(next.filters);
    setFilterErrors(errors);
    const firstInvalid = Object.keys(errors)[0] as ExplorerFilterKey | undefined;
    if (firstInvalid) {
      window.requestAnimationFrame(() => {
        const field = document.getElementById(`explorer-filter-${firstInvalid}`);
        const advanced = field?.closest('details');
        if (advanced) advanced.open = true;
        field?.focus();
      });
      return false;
    }
    let normalized: CatalogueConditions;
    try { normalized = normalizeCatalogueConditions(next); }
    catch (error) {
      setFilterError(error instanceof Error ? error.message : String(error));
      if (error instanceof CatalogueQueryError && error.field.startsWith('facets.growth.')) {
        window.requestAnimationFrame(() => document.getElementById(`explorer-filter-${error.field.slice('facets.growth.'.length)}`)?.focus());
      }
      return false;
    }
    navigation.navigate({ ...navigation.state, conditions: normalized });
    setFilterError('');
    setAnnouncement('Filters applied. Updating Explorer results.');
    return true;
  };
  const applyFilters = () => {
    const accepted = commitConditions(draftConditions);
    if (accepted) setDraftConditions(normalizeCatalogueConditions(draftConditions));
    return accepted;
  };
  const clearFilters = () => {
    const next = createInitialCatalogueConditions();
    next.filters.sort_by = applied.sort_by;
    next.filters.sort_order = applied.sort_order;
    commitConditions(next);
    setDraftConditions(next);
  };
  const removeFilter = (group: CatalogueFilterGroup) => {
    const next = clearCatalogueFilterGroup(conditions, group);
    if (commitConditions(next)) setDraftConditions(next);
  };

  /** Save view: v2 with `presentation` from the server translation; v1 only when no translation exists. */
  const saveSnapshotFor = useCallback((channelName: string): SavedViewSnapshot => {
    if (translated) {
      const density = readBrowserPreference(catalogueDensityPreference);
      return { channelId, channelName, conditions: librarySavedDefinition(translated, { channelId, timeZone, density }) as unknown as SavedViewSnapshot['conditions'] };
    }
    return { channelId, channelName, conditions: normalizeCatalogueConditions(conditions) };
  }, [translated, channelId, timeZone, conditions]);

  const busy = videosVisible
    && (visibleRawState.kind === 'loading' || visibleRawState.kind === 'updating');

  return {
    conditions, applied, draftConditions, setDraftConditions, saveSnapshot, setSaveSnapshot,
    savedMessage, setSavedMessage, filterError, setFilterError, setFiltersOpen, setFacetReload,
    filterErrors, setFilterErrors, derivedFiltersAvailable, staticFiltersBlocked, growthSortAvailable, growthFiltersBlocked,
    durationOtherFormat, currentAnalysisScope, browse, gates, facetReadsAvailable, visibleFacetState, visibleRawState: presentedState,
    rawPayload, categories, commitConditions, applyFilters, clearFilters, removeFilter, busy, saveSnapshotFor, capabilitiesSettled,
    /** The frozen page carries no channel fields: the header uses the sidebar until the profile responds. */
    browseFallback: undefined as BrowseChannelFallback | undefined,
  };
}

export type LibraryVideosModel = ReturnType<typeof useLibraryVideos>;

/** Content of the Videos tab panel, for the tab's format. */
export function LibraryVideosSection({
  videos,
  context,
  channelTitle,
  onOpenWorkshop,
}: {
  videos: LibraryVideosModel;
  context: LibraryShellContext;
  /** Channel name stored with a Saved view. */
  channelTitle: string;
  /** "Open in research workshop" / "Send selected to workshop". */
  onOpenWorkshop?: (handoff: WorkshopHandoff) => void;
}) {
  const {
    channelId, videoType, navigation, activeSection, timeZone,
    setCatalogueReloadToken,
  } = context;
  const {
    conditions, applied, draftConditions, setDraftConditions, setSaveSnapshot, savedMessage, setSavedMessage,
    filterError, setFilterError, setFiltersOpen, setFacetReload, filterErrors, setFilterErrors,
    derivedFiltersAvailable, staticFiltersBlocked, growthSortAvailable, growthFiltersBlocked, durationOtherFormat,
    currentAnalysisScope, browse, gates, facetReadsAvailable, visibleFacetState, visibleRawState, rawPayload, categories,
    commitConditions, applyFilters, clearFilters, removeFilter, saveSnapshotFor, capabilitiesSettled,
  } = videos;
  const translatedQuery = browse.translated;
  const resolvedTitle = channelTitle;
  const origin = { surface: 'library' as const, channel_id: channelId, ...(resolvedTitle ? { channel_title: resolvedTitle } : {}), format: videoType, view: 'grid' as const };
  /** The whole query as a fixed list; with `selected`, only when that list holds exactly the selected videos. */
  const openWhole = async (selected?: ReadonlySet<string>): Promise<string | undefined> => {
    if (!onOpenWorkshop || !browse.executed) return 'Load the catalogue before opening it in the research workshop.';
    try {
      const fixed = (await browse.freezeForWorkshop(selected)).browse;
      if (fixed.consistency !== 'frozen') return 'The fixed list could not be verified.';
      const handoff = selectionHandoff(browse.executed, {
        context: { frozen_ref: fixed.frozen_ref, evaluated_at: fixed.created_at, valid_before: fixed.expires_at },
        page: { total_count: fixed.total_count },
      }, origin);
      if ('refused' in handoff) return handoffRefusalText(handoff);
      onOpenWorkshop(handoff);
    } catch (error) {
      if (!isAbortError(error)) return libraryErrorState(error, 'select_all').message ?? 'The fixed list could not be opened.';
    }
    return undefined;
  };
  const workshop = onOpenWorkshop ? {
    openSelection: () => openWhole(),
    sendSelected: async (selected: ReadonlyArray<{ id: string; type?: 'long' | 'short' | 'stream' }>): Promise<string | undefined> => {
      if (!browse.executed) return 'Load the catalogue before sending videos to the research workshop.';
      // The whole list selected ("Select all matching", in any sort of it) travels as the fixed list of the query,
      // and only if that list holds exactly the selected videos (checked by ID); otherwise it is refused in words.
      const chosen = new Set(selected.map((item) => item.id));
      if (browse.listIds && isWholeFrozenList(chosen, browse.listIds)) return openWhole(chosen);
      const handoff = selectedIdsHandoff(browse.executed, selected, origin);
      if ('refused' in handoff) return handoffRefusalText(handoff);
      onOpenWorkshop(handoff);
      return undefined;
    },
  } : undefined;
  const blockedMessage = navigation.error || (staticFiltersBlocked ? BLOCKED_TEXT.staticFilters : '')
    || (growthFiltersBlocked ? BLOCKED_TEXT.growthFilters : '')
    || (!growthSortAvailable && applied.sort_by === VIDEO_GROWTH_SORT_FIELD && capabilitiesSettled ? BLOCKED_TEXT.growthSort : '')
    || (durationOtherFormat ? DURATION_OTHER_FORMAT_TEXT : '') || (browse.blocked ?? '');
  return (
    <>{translatedQuery && !blockedMessage && !navigation.error && <WatchSelection key={JSON.stringify(translatedQuery)} query={translatedQuery}/>}
    <ExplorerVideosWorkspace key={browse.accessRevision}
      channelId={channelId}
      videoType={videoType}
      selectionScopeKey={currentAnalysisScope}
      rawState={visibleRawState}
      blockedMessage={blockedMessage}
      onResetQuery={() => {
        const next = createInitialCatalogueConditions();
        navigation.navigate({ ...navigation.state, section: 'videos', conditions: next });
        setDraftConditions(next); setFilterErrors({}); setFilterError('');
      }}
      toolbar={{
        draft: draftConditions, applied: conditions, categories, errors: filterErrors, error: filterError,
        videoType, gates,
        // While the gates are read the panel keeps today's controls; requests wait for them.
        growthSortAvailable: growthSortAvailable || !capabilitiesSettled,
        facetsAvailable: !capabilitiesSettled || facetReadsAvailable || (Boolean(gates?.facets) && !browse.translated && !blockedMessage),
        derivedFiltersAvailable: derivedFiltersAvailable || !capabilitiesSettled, facetState: visibleFacetState,
        matchedCount: visibleFacetState.kind === 'ready' && !sourceExpired(visibleFacetState.payload)
          ? visibleFacetState.payload.matched_count
          // The list's own total stays for as long as the list does (the bar above the grid says what time its order is from).
          : browse.order ? browse.order.total
          : rawPayload && !sourceExpired(rawPayload.pagination) ? rawPayload.pagination.matched_count : undefined,
        incomplete: Boolean(rawPayload) && !sourceExpired(rawPayload?.coverage) && rawPayload?.coverage.complete === false,
        resetKey: `${navigation.restored}:${activeSection}`,
        onChange: (next) => { setDraftConditions(next); setFilterErrors({}); setFilterError(''); },
        onApply: applyFilters,
        onCancel: () => { setDraftConditions(conditions); setFilterErrors({}); setFilterError(''); },
        onImmediate: (key, value) => {
          const next = { ...conditions, filters: { ...applied, [key]: value } } as CatalogueConditions;
          if (commitConditions(next)) setDraftConditions((current) => ({ ...current, filters: { ...current.filters, [key]: value } } as CatalogueConditions));
        },
        onRemove: removeFilter, onClear: clearFilters, onOpen: setFiltersOpen,
        onRetryFacets: () => setFacetReload((value) => value + 1),
        // A v1 fallback (no translation) stores no format: from Shorts or Streams it is saved only
        // when its duration presets name the format, or it would reopen as Long.
        saveViewDisabled: Boolean(navigation.error) || (!translatedQuery && videoType !== 'long' && savedLibraryVideoType(conditions) !== videoType),
        savedViewMessage: savedMessage,
        onSaveView: () => {
          if (navigation.error) return;
          setSavedMessage('');
          setSaveSnapshot(saveSnapshotFor(resolvedTitle));
        },
      }}
      growthSortAvailable={growthSortAvailable}
      browse={browse}
      inspector={gates ? { gates: inspectorGates(gates), timeZone } : undefined}
      workshop={workshop}
      onRetryCatalogue={() => setCatalogueReloadToken((value) => value + 1)}
    /></>
  );
}

/** Save view dialog, rendered by the shell at the end of the page. */
export function LibraryVideosSaveDialog({ videos }: { videos: LibraryVideosModel }) {
  const { saveSnapshot, setSaveSnapshot, setSavedMessage } = videos;
  return saveSnapshot ? <SaveViewDialog snapshot={saveSnapshot} onClose={() => setSaveSnapshot(undefined)}
        onSaved={() => { setSaveSnapshot(undefined); setSavedMessage('View saved'); }} /> : null;
}
