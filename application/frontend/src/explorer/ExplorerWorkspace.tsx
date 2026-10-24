import { expireResourceState } from './resourceState';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { freshSourceRows, sourceContainer, sourceExpired } from '../sourceLifetime.ts';
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronDown,
  Copy,
  Database,
  Folder as FolderIcon,
  FolderPlus,
  LoaderCircle,
  MoreHorizontal,
  RefreshCw,
  ShieldCheck,
  Tag,
} from 'lucide-react';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react';
import type {
  Channel,
  ChannelExplorerCounterDynamicsMetric,
  ChannelExplorerCounterDynamicsPeriodKey,
  ChannelExplorerCounterDynamicsPeriodsPayload,
  ChannelExplorerFormatTotalsPayload,
  ChannelBaselinePeriodKey,
  ChannelExplorerProfilePayload,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder,
} from '../types';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import {
  confirmExplorerHydration,
  isAbortError,
  isContractError,
  loadExplorerBaseline,
  loadExplorerCounterDynamicsPeriods,
  loadExplorerFormatTotals,
  loadExplorerHydration,
  loadExplorerProfile,
  loadCatalogueFacets,
} from './api';
import { newSavedWorkId } from './savedWork';
import {
  createInitialCatalogueConditions, normalizeCatalogueConditions, catalogueAnalysisKey,
  catalogueRequestQuery, clearCatalogueFilterGroup, applyCataloguePreset,
  catalogueUsesDerivedStaticFilters, catalogueUsesGrowthFilters, CatalogueQueryError, type CatalogueConditions,
  type CatalogueFilterGroup, type CataloguePresetGroup,
} from './catalogueQuery';
import { useCatalogueNavigation } from './useCatalogueNavigation';
import { useCatalogueBrowse } from './useCatalogueBrowse';
import type { CatalogueFacetPayload } from './catalogueFacets';
import {
  VIDEO_GROWTH_SORT_FIELD,
  OVERVIEW_PERIOD_OPTIONS,
  overviewBaselineQuery,
  overviewBaselineScopeKey,
  validateExplorerFilters,
} from './filters';
import {
  formatCount,
  initials,
  youtubeChannelUrl,
  videoNoun,
} from './format';
import { numberValue } from '../format';
import { unlistedVideoCount } from './unlistedVideos';
import { formatDateLocal, formatTimestampLocal } from './localTime';
import { localTimeZone, useTimeBasis } from './timeBasis';
import { copyText } from './clipboard';
import { preferredCounterDynamicsPeriod, presentCounterDynamicsPercent } from './counterDynamics';
import {
  isVideoGrowthEffectivelyAvailable,
  videoGrowthAttemptKey,
} from './videoGrowth';
import {
  formatChannelAge,
  formatCountryName,
} from './channelProfile';
import { safeExplorerAvatarUrl } from './sidebar';
import { OverviewPanel } from './ExplorerPanels';
import { ExplorerVideosWorkspace } from './ExplorerVideosWorkspace';
import { SaveViewDialog } from './SaveViewDialog';
import type { SavedViewSnapshot } from './savedViews';
import type {
  BaselineState,
  CapabilityState,
  ExplorerFilterErrors,
  ExplorerFilterKey,
  ExplorerHydrationPayload,
  ExplorerHydrationState,
  ExplorerMedia,
  ExplorerSection,
  VideoCatalogueState,
  ResourceState,
} from './model';
import {
  beginResourceLoad,
  completeResourceLoad,
  failResourceLoad,
  rejectResourceContract,
  resourcePayload,
  stateForPresentationScope,
} from './resourceState';
import {
  EXPLORER_HYDRATION_BACKGROUND_POLL_MS,
  EXPLORER_HYDRATION_POLL_MS,
  hydrationInitialBecameComplete,
  hydrationInitialComplete,
  hydrationInventoryReady,
  hydrationNeedsPolling,
  hydrationPayload,
  hydrationProfileBecameReady,
  hydrationPhaseTitle,
  hydrationProfileReady,
  hydrationNeedsRestart,
  hydrationStoppedReason,
} from './hydration';

const mediaTabs: Array<{ id: ExplorerMedia; label: string; detail: string }> = [
  { id: 'videos', label: 'Videos', detail: 'long-form' },
  { id: 'shorts', label: 'Shorts', detail: 'coming soon' },
  { id: 'streams', label: 'Streams', detail: 'coming soon' },
];

type WorkspaceSection = Extract<ExplorerSection, 'overview' | 'videos'>;

const sectionTabs: Array<{
  id: WorkspaceSection;
  label: string;
}> = [
  { id: 'overview', label: 'Overview' },
  { id: 'videos', label: 'Videos' },
];

const MISSING_VALUE = '\u2014';

function selectTabByKeyboard<T extends string>(
  event: KeyboardEvent<HTMLButtonElement>,
  tabs: Array<{ id: T }>,
  currentIndex: number,
  setActive: (tab: T) => void,
  idPrefix: string,
) {
  let nextIndex: number | null = null;
  if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
  if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
  if (event.key === 'Home') nextIndex = 0;
  if (event.key === 'End') nextIndex = tabs.length - 1;
  if (nextIndex === null) return;
  event.preventDefault();
  const next = tabs[nextIndex];
  setActive(next.id);
  event.currentTarget.parentElement
    ?.querySelector<HTMLButtonElement>(`#${idPrefix}-${next.id}`)
    ?.focus();
}

function capabilityAvailable(
  state: CapabilityState,
  key: 'baseline' | 'measured_change' | 'timeline' | 'video_growth',
): boolean {
  return state.kind === 'ready'
    && state.payload.channel_explorer[key] === 'available';
}

function hasReportedMetric(value: unknown): boolean {
  return value !== undefined && value !== null && value !== '';
}

type ProfileState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: ChannelExplorerProfilePayload }
  | { kind: 'error'; channelId: string; message: string };

type FormatTotalsState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: ChannelExplorerFormatTotalsPayload; longProfileReadyAtRead: boolean; refreshError?: string }
  | { kind: 'error'; channelId: string; message: string };

type CounterDynamicsState =
  | { kind: 'loading'; channelId: string }
  | { kind: 'ready'; channelId: string; payload: ChannelExplorerCounterDynamicsPeriodsPayload }
  | { kind: 'error'; channelId: string; message: string };

function counterDynamicsObservationDetails(metric: ChannelExplorerCounterDynamicsMetric): string {
  const end = metric.observed_end_at;
  if (!end || !Number.isFinite(Date.parse(end))) return '';
  const start = metric.observed_start_at;
  const seconds = Number(metric.actual_interval_seconds);
  if (
    (metric.status !== 'ready' && metric.status !== 'partial' && metric.status !== 'zero_baseline')
    || !start || !Number.isFinite(Date.parse(start)) || Date.parse(end) <= Date.parse(start)
    || !Number.isFinite(seconds) || seconds <= 0
  ) return `Last observation: ${formatTimestampLocal(end)}.`;

  let remaining = Math.floor(seconds);
  const parts: string[] = [];
  for (const [size, unit] of [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']] as const) {
    const count = Math.floor(remaining / size);
    if (count) parts.push(`${count}${unit}`);
    remaining %= size;
  }
  return `Observed: ${formatTimestampLocal(start)} → ${formatTimestampLocal(end)}. Actual interval: ${parts.join(' ') || '<1s'}.`;
}

function CounterDynamicsTrend({
  state,
  metricName,
  label,
  period,
  onPeriodChange,
}: {
  state: CounterDynamicsState;
  metricName: 'subscribers' | 'views';
  label: string;
  period: ChannelExplorerCounterDynamicsPeriodKey;
  onPeriodChange: (period: ChannelExplorerCounterDynamicsPeriodKey) => void;
}) {
  const nextPeriod: ChannelExplorerCounterDynamicsPeriodKey = period === '28d' ? '7d' : '28d';
  const periodDays = period === '7d' ? 7 : 28;
  const periodLabel = period.toUpperCase();
  const toggleLabel = `Show ${nextPeriod.toUpperCase()} ${label.toLowerCase()} change`;
  if (state.kind === 'loading') {
    return (
      <button
        key={period}
        type="button"
        className="explorer-v2-kpi-trend is-loading"
        aria-label={`Loading ${periodDays}-day ${label.toLowerCase()} change. ${toggleLabel}.`}
        onClick={() => onPeriodChange(nextPeriod)}
      >
        <i aria-hidden="true" />
        <small aria-hidden="true">{periodLabel}</small>
      </button>
    );
  }
  if (state.kind === 'error') {
    return (
      <button
        key={period}
        type="button"
        className="explorer-v2-kpi-trend is-unavailable"
        aria-label={`${periodDays}-day ${label.toLowerCase()} change unavailable. ${toggleLabel}.`}
        title={state.message}
        onClick={() => onPeriodChange(nextPeriod)}
      >
        <span aria-hidden="true">—</span>
        <small aria-hidden="true">{periodLabel}</small>
      </button>
    );
  }
  const metric = state.payload.periods[period].metrics[metricName];
  if (sourceExpired(metric)) return <button type="button" className="explorer-v2-kpi-trend is-unavailable"
    aria-label={`${label} change is being refreshed. ${toggleLabel}.`} onClick={() => onPeriodChange(nextPeriod)}>
    <span aria-hidden="true">—</span><small>{periodLabel}</small></button>;
  const partial = metric.coverage === 'partial' || metric.status === 'partial';
  const presentation = presentCounterDynamicsPercent(partial ? {
    ...metric, status: 'not_enough_history',
    reason: `Not enough stored history for the ${periodDays}-day comparison.`,
  } : metric);
  const observationDetails = counterDynamicsObservationDetails(metric);
  const trendGlyph = presentation.kind === 'positive'
    ? '▲'
    : presentation.kind === 'negative'
      ? '▼'
      : '';
  const provenance = 'Change calculated by Leti Cook from stored YouTube API snapshots.';
  const context = metricName === 'subscribers'
    ? "Change in YouTube's reported public subscriber counter."
    : 'Change in the channel lifetime view counter; negative values can reflect source corrections.';
  const changeLabel = presentation.kind === 'unavailable'
    ? `Change over ${periodDays} days unavailable.`
    : `${presentation.text} over ${periodDays} days.`;
  return (
    <button
      key={period}
      type="button"
      className={`explorer-v2-kpi-trend is-${presentation.kind}`}
      aria-label={`${label}: ${changeLabel} ${toggleLabel}.`}
      title={[context, provenance, presentation.unavailableReason, observationDetails, `Click to show ${nextPeriod.toUpperCase()}.`].filter(Boolean).join('\n')}
      onClick={() => onPeriodChange(nextPeriod)}
    >
      <span aria-hidden="true">{trendGlyph ? `${trendGlyph} ` : ''}{presentation.text}</span>
      <small aria-hidden="true">{periodLabel}</small>
    </button>
  );
}

function ExplorerHydrationBanner({
  state,
  checking,
  youtubeVideoTotal,
  onConfirm,
  onRetry,
}: {
  state: ExplorerHydrationState;
  checking: boolean;
  youtubeVideoTotal: number;
  onConfirm: () => void;
  onRetry: () => void;
}) {
  // The YouTube Data API lists only a channel's latest 20,000 uploads (Google issue 166292064).
  const YOUTUBE_UPLOADS_LIST_LIMIT = 20_000;
  const payload = hydrationPayload(state);
  const failed = state.kind === 'error';
  if (!failed && payload && hydrationInitialComplete(payload)) return null;
  const confirming = state.kind === 'confirming';
  const actionRequired = payload?.action_required ?? null;
  const stopped = payload?.status === 'blocked'
    || payload?.status === 'error'
    || payload?.status === 'suspended';
  const idleWithoutAction = payload?.status === 'idle' && !actionRequired;
  const finalizing = payload?.status === 'running' && payload.phase === 'verify';
  const canStart = Boolean(payload?.estimate && actionRequired);
  const canRecheck = !confirming && !canStart && (failed || stopped || idleWithoutAction);
  const progress = payload?.progress;
  const stoppedReason = payload ? hydrationStoppedReason(payload) : null;
  const title = failed
    ? 'Profile loading status is unavailable'
    : payload
      ? idleWithoutAction
        ? 'Profile loading has not started'
        : stopped || payload.confirmation_required
          ? hydrationPhaseTitle(payload)
          : 'Loading channel profile'
      : 'Checking profile loading status';
  const estimate = payload?.estimate;
  const progressDetail = progress && progress.total !== null && progress.total > 0
    ? stopped && progress.processed >= progress.total
      ? `${formatCount(progress.processed, { compact: false })} of ${formatCount(progress.total, { compact: false })} loaded; verification did not complete`
      : finalizing && progress.processed >= progress.total
        ? `${formatCount(progress.total, { compact: false })} ${videoNoun(progress.total)} loaded; finalizing profile`
      : `${formatCount(progress.processed, { compact: false })} of ${formatCount(progress.total, { compact: false })} loaded`
    : progress?.total === null && progress.discovered !== undefined
      ? `${formatCount(progress.discovered, { compact: false })} ${videoNoun(progress.discovered)} discovered; ${estimate?.items !== null && estimate?.items !== undefined
        ? `estimated ${estimate.estimate_kind === 'upper_bound' ? 'up to ' : ''}${formatCount(estimate.items, { compact: false })} ${videoNoun(estimate.items)}`
        : 'total size unknown'}`
    : payload?.status === 'queued'
      ? 'Waiting to begin'
      : idleWithoutAction
        ? 'Re-check the profile status to continue.'
      : 'Preparing channel data';
  return (
    <section
      className={`explorer-v2-hydration-banner${failed || stopped || idleWithoutAction ? ' is-warning' : ''}`}
      role={failed || stopped || idleWithoutAction ? 'alert' : 'region'}
      aria-label="Channel profile loading"
      aria-busy={checking || state.kind === 'loading' || confirming || Boolean(payload && hydrationNeedsPolling(payload))}
    >
      <div className="explorer-v2-hydration-icon" aria-hidden="true">
        {failed || stopped || idleWithoutAction
          ? <AlertTriangle size={18} />
          : <LoaderCircle className="explorer-spin" size={18} />}
      </div>
      <div className="explorer-v2-hydration-copy">
        <strong>{title}</strong>
        {actionRequired && estimate && (
          <span>
            {estimate.estimate_kind === 'unknown' ? 'Size unknown. Confirm to load the full channel.' : (
              <>
                {estimate.estimate_kind === 'upper_bound' ? 'Up to ' : ''}
                {formatCount(estimate.items, { compact: false })} {estimate.items === 1 ? 'video' : 'videos'} ·
                {' '}{formatCount(estimate.pages, { compact: false })} estimated pages ·
                {' '}{formatCount(estimate.quota_units, { compact: false })} {estimate.quota_basis === 'before_start' ? 'API units estimated before starting' : 'estimated API units'}
              </>
            )}
          </span>
        )}
        {youtubeVideoTotal > YOUTUBE_UPLOADS_LIST_LIMIT && (
          <span>
            YouTube gives apps only the latest {formatCount(YOUTUBE_UPLOADS_LIST_LIMIT, { compact: false })} videos
            {' '}of a channel. This channel has {formatCount(youtubeVideoTotal, { compact: false })} on YouTube, so
            {' '}the oldest {formatCount(youtubeVideoTotal - YOUTUBE_UPLOADS_LIST_LIMIT, { compact: false })} cannot be loaded.
          </span>
        )}
        {failed ? (
          <span>{state.message}</span>
        ) : (
          <span>{progressDetail}</span>
        )}
        {!failed && stoppedReason && <span>{stoppedReason}</span>}
        {progress && progress.total !== null && progress.total > 0 && progress.percent !== null && (
          <div
            className="explorer-v2-hydration-progress"
            role="progressbar"
            aria-label={title}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress.percent)}
          >
            <i style={{ width: `${Math.max(0, Math.min(100, progress.percent))}%` }} />
          </div>
        )}
      </div>
      {canStart && estimate && (
        <button
          type="button"
          className="explorer-secondary-button explorer-v2-hydration-action"
          disabled={confirming}
          onClick={onConfirm}
        >
          {confirming
            ? <LoaderCircle className="explorer-spin" size={15} aria-hidden="true" />
            : null}
          {confirming
            ? actionRequired === 'retry' ? 'Retrying…' : 'Starting…'
            : payload && hydrationNeedsRestart(payload)
              ? 'Restart loading'
            : actionRequired === 'confirm'
              ? payload?.job ? 'Continue full channel' : 'Load full channel'
              : actionRequired === 'retry'
                ? 'Retry profile loading'
                : 'Load full channel'}
        </button>
      )}
      {canRecheck && (
        <button
          type="button"
          className="explorer-secondary-button explorer-v2-hydration-action"
          disabled={checking}
          onClick={onRetry}
        >
          {checking
            ? <LoaderCircle className="explorer-spin" size={15} aria-hidden="true" />
            : <RefreshCw size={15} aria-hidden="true" />}
          {checking ? 'Checking loading status…' : 'Re-check loading status'}
        </button>
      )}
    </section>
  );
}



function HydrationDependentPanel({ sectionName }: { sectionName: string }) {
  return (
    <div className="explorer-v2-hydration-dependent">
      <div className="explorer-v2-hydration-icon" aria-hidden="true">
        <Database size={18} />
      </div>
      <div>
        <span>{sectionName} will appear after the channel profile is ready.</span>
      </div>
    </div>
  );
}

export function ExplorerWorkspace({
  channelId,
  channel,
  folders = [],
  labels = [],
  onAssignFolder,
  onCreateFolder,
  onCreateLabel,
  onAssignLabel,
  capabilities,
  onRetryCapabilities,
  onBack,
}: {
  channelId: string;
  channel?: Channel;
  folders?: Folder[];
  labels?: ExplorerLabel[];
  onAssignFolder?: (channelId: string, folderId: string) => Promise<void>;
  onCreateFolder?: (name: string) => Promise<CreatedFolder>;
  onCreateLabel?: (name: string) => Promise<CreatedLabel>;
  onAssignLabel?: (channelId: string, labelKey: string, enabled: boolean) => Promise<void>;
  capabilities: CapabilityState;
  onRetryCapabilities: () => void;
  onBack: () => void;
}) {
  const [activeMedia, setActiveMedia] = useState<ExplorerMedia>('videos');
  const navigation = useCatalogueNavigation();
  const activeSection = navigation.state.section;
  const overviewPeriod = navigation.state.overviewPeriod;
  // Publication hours, days and years: the viewer's own time zone, or UTC.
  const { basis: timeBasis, zone: timeZone, setBasis: setTimeBasis } = useTimeBasis();
  const conditions = navigation.state.conditions;
  const applied = conditions.filters;
  const [draftConditions, setDraftConditions] = useState<CatalogueConditions>(conditions);
  const [saveSnapshot, setSaveSnapshot] = useState<SavedViewSnapshot>();
  const [savedMessage, setSavedMessage] = useState('');
  useEffect(() => setSavedMessage(''), [conditions]);
  const [filterError, setFilterError] = useState('');
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [facetReload, setFacetReload] = useState(0);
  const [facetState, setFacetState] = useState<ResourceState<CatalogueFacetPayload>>({ kind: 'idle' });
  const setActiveSection = (section: WorkspaceSection) => navigation.navigate({ ...navigation.state, section });
  const setOverviewPeriod = (period: ChannelBaselinePeriodKey) => navigation.navigate({ ...navigation.state, overviewPeriod: period });
  const [filterErrors, setFilterErrors] = useState<ExplorerFilterErrors>({});
  const [profileReloadToken, setProfileReloadToken] = useState(0);
  const [formatTotalsReloadToken, setFormatTotalsReloadToken] = useState(0);
  const [counterDynamicsReloadToken, setCounterDynamicsReloadToken] = useState(0);
  const [catalogueReloadToken, setCatalogueReloadToken] = useState(0);
  const [failedGrowthAttemptKey, setFailedGrowthAttemptKey] = useState<string | null>(null);
  const [baselineReloadToken, setBaselineReloadToken] = useState(0);
  const [avatarFailed, setAvatarFailed] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [folderDraftId, setFolderDraftId] = useState(channel?.folder_id || '');
  const [folderSaving, setFolderSaving] = useState(false);
  const [folderError, setFolderError] = useState('');
  const [folderCreationOpen, setFolderCreationOpen] = useState(false);
  const [folderName, setFolderName] = useState('');
  const [labelName, setLabelName] = useState('');
  const [labelSavingKey, setLabelSavingKey] = useState('');
  const [labelError, setLabelError] = useState('');
  const [announcement, setAnnouncement] = useState('');
  const [hydrationState, setHydrationState] = useState<ExplorerHydrationState>({ kind: 'loading' });
  const [hydrationChecking, setHydrationChecking] = useState(false);
  const [hydrationRequestToken, setHydrationRequestToken] = useState(0);
  const [profileState, setProfileState] = useState<ProfileState>({ kind: 'loading', channelId });
  const [formatTotalsState, setFormatTotalsState] = useState<FormatTotalsState>(
    { kind: 'loading', channelId },
  );
  const [counterDynamicsState, setCounterDynamicsState] = useState<CounterDynamicsState>(
    { kind: 'loading', channelId },
  );
  const [counterDynamicsPeriodChoices, setCounterDynamicsPeriodChoices] = useState<{
    channelId: string;
    subscribers: ChannelExplorerCounterDynamicsPeriodKey | null;
    views: ChannelExplorerCounterDynamicsPeriodKey | null;
  }>({ channelId, subscribers: null, views: null });
  useEffect(() => {
    setCounterDynamicsPeriodChoices({ channelId, subscribers: null, views: null });
  }, [channelId]);
  const chooseCounterDynamicsPeriod = (metricName: 'subscribers' | 'views', period: ChannelExplorerCounterDynamicsPeriodKey) => {
    setCounterDynamicsPeriodChoices((current) => ({
      channelId,
      subscribers: current.channelId === channelId ? current.subscribers : null,
      views: current.channelId === channelId ? current.views : null,
      [metricName]: period,
    }));
  };
  const [baselineState, setBaselineState] = useState<BaselineState>({ kind: 'idle' });
  const [, repaintSources] = useState(0);
  useSourceExpiry(facetState, () => setFacetState(expireResourceState), () => { if (videosVisible && filtersOpen) setFacetReload(value => value + 1); });
  useSourceExpiry(baselineState, () => repaintSources(value => value + 1), () => { if (overviewVisible) void calculateBaseline(); });
  useSourceExpiry(profileState, () => setProfileState({ kind: 'loading', channelId }), () => setProfileReloadToken(value => value + 1));
  useSourceExpiry(formatTotalsState, () => repaintSources(value => value + 1), () => setFormatTotalsReloadToken(value => value + 1));
  useSourceExpiry(counterDynamicsState, () => repaintSources(value => value + 1), () => setCounterDynamicsReloadToken(value => value + 1));
  const overviewVisible = activeMedia === 'videos' && activeSection === 'overview';
  const videosVisible = activeMedia === 'videos' && activeSection === 'videos';
  const facetsAvailable = capabilities.kind === 'ready' && capabilities.payload.channel_explorer.filter_facets === 'available';
  const derivedFiltersAvailable = capabilityAvailable(capabilities, 'baseline');
  const staticDetailsAvailable = capabilities.kind === 'ready'
    && derivedFiltersAvailable
    && capabilities.payload.channel_explorer.static_video_details === 'available';
  const historyAvailable = capabilities.kind === 'ready' && capabilities.payload.channel_explorer.video_history === 'available';
  const launchAvailable = capabilities.kind === 'ready' && capabilities.payload.channel_explorer.video_launch === 'available';
  const staticFiltersBlocked = capabilities.kind !== 'loading'
    && catalogueUsesDerivedStaticFilters(conditions) && !derivedFiltersAvailable;
  const growthCapabilityAvailable = capabilityAvailable(capabilities, 'video_growth');
  const currentGrowthAttemptKey = videoGrowthAttemptKey(channelId, catalogueReloadToken);
  const growthSortAvailable = isVideoGrowthEffectivelyAvailable(
    growthCapabilityAvailable,
    failedGrowthAttemptKey,
    currentGrowthAttemptKey,
  );
  const growthFiltersActive = catalogueUsesGrowthFilters(conditions);
  const growthFiltersBlocked = capabilities.kind !== 'loading' && growthFiltersActive && !growthSortAvailable;
  const currentHydration = hydrationPayload(hydrationState);
  const initialComplete = currentHydration ? hydrationInitialComplete(currentHydration) : false;
  const currentProfileReady = currentHydration
    ? hydrationProfileReady(currentHydration)
    : false;
  const currentInventoryReady = currentHydration
    ? hydrationInventoryReady(currentHydration)
    : false;

  const currentCatalogueScope = useMemo(
    () => `${catalogueAnalysisKey(channelId, conditions)}\n${applied.sort_by}:${applied.sort_order}\nreload:${catalogueReloadToken}`,
    [applied, conditions, channelId, catalogueReloadToken],
  );
  useEffect(() => {
    setDraftConditions(navigation.state.conditions);
    setFilterErrors({});
    setFilterError('');
  }, [navigation.restored]);

  const currentAnalysisScope = useMemo(
    () => catalogueAnalysisKey(channelId, conditions),
    [conditions, channelId],
  );
  const browseQuery = useMemo(() => catalogueRequestQuery(conditions, 0, timeZone), [conditions, timeZone]);
  const browse = useCatalogueBrowse({
    channelId, scopeKey: currentCatalogueScope, selectionScopeKey: currentAnalysisScope,
    query: browseQuery,
    enabled: videosVisible && currentProfileReady && capabilities.kind !== 'loading' && !navigation.error && !staticFiltersBlocked && !growthFiltersBlocked
      && (growthSortAvailable || applied.sort_by !== VIDEO_GROWTH_SORT_FIELD),
    preferGrowth: growthSortAvailable,
    onGrowthUnavailable: () => setFailedGrowthAttemptKey(currentGrowthAttemptKey),
    onAnnouncement: setAnnouncement,
  });
  const facetReadsAvailable = facetsAvailable && !staticFiltersBlocked && !growthFiltersBlocked
    && (!growthFiltersActive || Boolean(browse.sessionId));
  const facetScope = `${currentAnalysisScope}\n${facetReload}:${catalogueReloadToken}\nsession:${browse.sessionId ?? ''}\nzone:${timeZone}`;
  useEffect(() => {
    if (!videosVisible || !filtersOpen || !currentProfileReady || !facetReadsAvailable || navigation.error) return;
    const controller = new AbortController();
    let cancelled = false;
    setFacetState({ kind: 'loading', scopeKey: facetScope });
    void loadCatalogueFacets(channelId, { ...catalogueRequestQuery(conditions, 0, timeZone), full_catalogue: true, browse_session: browse.sessionId }, controller.signal)
      .then((payload) => { if (!cancelled) setFacetState({ kind: 'ready', scopeKey: facetScope, payload }); })
      .catch((error) => {
        if (!cancelled && !isAbortError(error)) setFacetState({ kind: 'error', scopeKey: facetScope, message: error instanceof Error ? error.message : String(error) });
      });
    return () => { cancelled = true; controller.abort(); };
  }, [videosVisible, filtersOpen, currentProfileReady, facetReadsAvailable, facetScope, channelId, navigation.error]);
  const visibleFacetState: ResourceState<CatalogueFacetPayload> = facetReadsAvailable && facetState.kind !== 'idle' && facetState.scopeKey === facetScope
    ? facetState : { kind: 'idle' };
  const currentBaselineScope = useMemo(
    () => `${overviewBaselineScopeKey(channelId, overviewPeriod)}\nzone:${timeZone}\nreload:${baselineReloadToken}`,
    [baselineReloadToken, channelId, overviewPeriod, timeZone],
  );
  const baselineRequestScopeRef = useRef({ key: currentBaselineScope });
  if (baselineRequestScopeRef.current.key !== currentBaselineScope) {
    baselineRequestScopeRef.current = { key: currentBaselineScope };
  }
  const baselinePresentationScopeRef = useRef<object | null>(null);
  const baselineRequestRef = useRef<AbortController | null>(null);
  const baselineAutoStartedRef = useRef('');
  const hydrationPayloadRef = useRef<ExplorerHydrationPayload | undefined>(undefined);
  // One idempotency key per estimate: retrying after an uncertain outcome
  // repeats the same confirmation instead of sending an independent one.
  const hydrationRetryRef = useRef<{ estimate: string; id: string } | undefined>(undefined);
  const copyResetRef = useRef<number | undefined>(undefined);
  const folderMenuRef = useRef<HTMLDetailsElement | null>(null);
  const labelMenuRef = useRef<HTMLDetailsElement | null>(null);
  const operationsMenuRef = useRef<HTMLDetailsElement | null>(null);

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!(event.target instanceof Node)) return;
      const folderMenu = folderMenuRef.current;
      if (folderMenu?.open && !folderMenu.contains(event.target)) {
        folderMenu.open = false;
        setFolderCreationOpen(false);
      }
      const labelMenu = labelMenuRef.current;
      if (labelMenu?.open && !labelMenu.contains(event.target)) {
        labelMenu.open = false;
      }
      const operationsMenu = operationsMenuRef.current;
      if (operationsMenu?.open && !operationsMenu.contains(event.target)) {
        operationsMenu.open = false;
      }
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    return () => document.removeEventListener('pointerdown', closeOnOutsidePointer);
  }, []);

  useEffect(() => {
    hydrationPayloadRef.current = undefined;
    setHydrationState({ kind: 'loading' });
  }, [channelId]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    let lastPayload = hydrationPayloadRef.current;
    let poll: () => Promise<void>;
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(
        () => void poll(),
        document.hidden
          ? EXPLORER_HYDRATION_BACKGROUND_POLL_MS
          : EXPLORER_HYDRATION_POLL_MS,
      );
    };
    poll = async () => {
      controller?.abort();
      controller = new AbortController();
      setHydrationChecking(true);
      try {
        const payload = await loadExplorerHydration(channelId, controller.signal);
        if (cancelled) return;
        const profileBecameReady = hydrationProfileBecameReady(lastPayload, payload);
        const initialBecameComplete = hydrationInitialBecameComplete(lastPayload, payload);
        lastPayload = payload;
        hydrationPayloadRef.current = payload;
        setHydrationState({ kind: 'ready', payload });
        if (initialBecameComplete) setFormatTotalsReloadToken((value) => value + 1);
        if (hydrationProfileReady(payload)) {
          if (profileBecameReady) {
            setProfileReloadToken((value) => value + 1);
            setCounterDynamicsReloadToken((value) => value + 1);
            setCatalogueReloadToken((value) => value + 1);
            setBaselineReloadToken((value) => value + 1);
            setAnnouncement('The published long-form catalogue is ready.');
          }
        }
        if (hydrationNeedsPolling(payload)) schedule();
      } catch (error) {
        if (cancelled || isAbortError(error)) return;
        setHydrationState({
          kind: 'error',
          message: error instanceof Error ? error.message : String(error),
          previous: lastPayload,
        });
        if (lastPayload && hydrationNeedsPolling(lastPayload)) schedule();
      } finally {
        if (!cancelled) setHydrationChecking(false);
      }
    };
    if (!lastPayload) setHydrationState({ kind: 'loading' });
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller?.abort();
    };
  }, [channelId, hydrationRequestToken]);


  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setProfileState({ kind: 'loading', channelId: requestedChannelId });
    void loadExplorerProfile(requestedChannelId, controller.signal)
      .then((payload) => {
        if (!cancelled) setProfileState({ kind: 'ready', channelId: requestedChannelId, payload });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setProfileState({
          kind: 'error',
          channelId: requestedChannelId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, profileReloadToken]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setFormatTotalsState(previous => previous.kind === 'ready' && previous.channelId === requestedChannelId ? previous : { kind: 'loading', channelId: requestedChannelId });
    void loadExplorerFormatTotals(requestedChannelId, controller.signal)
      .then((payload) => {
        if (cancelled) return;
        setFormatTotalsState({
          kind: 'ready', channelId: requestedChannelId, payload,
          longProfileReadyAtRead: currentProfileReady,
        });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setFormatTotalsState({
          kind: 'error',
          channelId: requestedChannelId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, currentProfileReady, formatTotalsReloadToken]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const requestedChannelId = channelId;
    setCounterDynamicsState(previous => previous.kind === 'ready' && previous.channelId === requestedChannelId ? previous : { kind: 'loading', channelId: requestedChannelId });
    void loadExplorerCounterDynamicsPeriods(requestedChannelId, controller.signal)
      .then((payload) => {
        if (cancelled) return;
        setCounterDynamicsState({ kind: 'ready', channelId: requestedChannelId, payload });
      })
      .catch((error) => {
        if (cancelled || isAbortError(error)) return;
        setCounterDynamicsState({
          kind: 'error',
          channelId: requestedChannelId,
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [channelId, counterDynamicsReloadToken]);

  useEffect(() => {
    baselineRequestRef.current?.abort();
    return () => baselineRequestRef.current?.abort();
  }, [currentBaselineScope]);

  useEffect(() => {
    if (!overviewVisible) baselineRequestRef.current?.abort();
  }, [overviewVisible]);

  useEffect(() => {
    setFolderDraftId(channel?.folder_id || '');
    setFolderError('');
  }, [channel?.folder_id, channelId]);

  useEffect(() => () => window.clearTimeout(copyResetRef.current), []);

  const visibleRawState: VideoCatalogueState = browse.state;
  const visibleBaselineState: BaselineState = stateForPresentationScope(
    baselineState,
    baselinePresentationScopeRef.current,
    baselineRequestScopeRef.current,
    'idle',
  );
  const visibleProfileState: ProfileState = profileState.channelId === channelId
    ? profileState
    : { kind: 'loading', channelId };
  const rawPayload = resourcePayload(visibleRawState);
  const baselinePayload = resourcePayload(visibleBaselineState);
  const profilePayload = visibleProfileState.kind === 'ready'
    && !sourceExpired(visibleProfileState.payload)
    ? visibleProfileState.payload
    : undefined;
  const formatTotalsPayload = formatTotalsState.kind === 'ready'
    && formatTotalsState.channelId === channelId
    ? formatTotalsState.payload
    : undefined;
  const categories = freshSourceRows(rawPayload?.options?.categories ?? []);
  const resolvedTitle = profilePayload
    ? profilePayload.channel.title || channelId
    : rawPayload?.channel?.title || channel?.title || channelId;
  const avatarUrl = safeExplorerAvatarUrl(channel?.avatar_url);
  const subscriberCount = profilePayload
    ? profilePayload.channel.subscribers
    : rawPayload ? rawPayload.channel?.subscribers : channel?.subscribers;
  const channelViewCount = profilePayload
    ? profilePayload.channel.views
    : rawPayload ? rawPayload.channel?.views : channel?.views;
  const longFormat = sourceExpired(formatTotalsPayload?.formats.long) ? undefined : formatTotalsPayload?.formats.long;
  const unlistedVideos = unlistedVideoCount(
    sourceExpired(formatTotalsPayload?.inventory) ? undefined : formatTotalsPayload,
  );
  // Long totals still need the complete-profile gate. Never reveal an earlier
  // partial total in the render before the post-readiness request finishes.
  const longTotalsReady = currentProfileReady && formatTotalsState.kind === 'ready'
    && formatTotalsState.longProfileReadyAtRead;
  const videosViewCount = longTotalsReady && longFormat?.status === 'ready' ? longFormat.views : undefined;
  const shortFormat = sourceExpired(formatTotalsPayload?.formats.short) ? undefined : formatTotalsPayload?.formats.short;
  const streamFormat = sourceExpired(formatTotalsPayload?.formats.stream) ? undefined : formatTotalsPayload?.formats.stream;
  const typedViewsLoading = formatTotalsState.kind === 'loading'
    || formatTotalsState.channelId !== channelId;
  const formatTotalsError = formatTotalsState.kind === 'error'
    && formatTotalsState.channelId === channelId
    ? formatTotalsState.message
    : '';
  const formatTotalsCoverageIssue = currentProfileReady && longFormat && longFormat.status !== 'ready'
    ? longFormat.status === 'stale' ? 'Stored video counters need refresh.' : 'Stored video coverage is incomplete.'
    : '';
  const countryCode = profilePayload
    ? profilePayload.channel.country_code
    : rawPayload ? rawPayload.channel?.country_code : channel?.country_code;
  const channelCreatedAt = profilePayload
    ? profilePayload.channel.channel_created_at
    : channel?.channel_created_at;
  const metadataUpdatedAt = profilePayload
    ? profilePayload.channel.channel_metadata_checked_at
    : rawPayload
      ? rawPayload.channel?.channel_metadata_checked_at
      : channel?.channel_metadata_checked_at;
  useEffect(() => {
    setAvatarFailed(false);
  }, [avatarUrl]);

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
  const openOverviewBucket = (field: CataloguePresetGroup, key: string) => {
    if (!baselinePayload?.period?.end_at) return;
    const next = applyCataloguePreset(createInitialCatalogueConditions(), field, key);
    next.facets.cohort = { kind: 'overview', published_from: baselinePayload.period.start_at ?? null, published_through: baselinePayload.period.end_at };
    const normalized = normalizeCatalogueConditions(next);
    setDraftConditions(normalized);
    navigation.navigate({ ...navigation.state, section: 'videos', conditions: normalized });
  };

  const calculateBaseline = async () => {
    if (
      !currentInventoryReady
      || !capabilityAvailable(capabilities, 'baseline')
    ) return;
    baselineRequestRef.current?.abort();
    const controller = new AbortController();
    baselineRequestRef.current = controller;
    const requestScope = baselineRequestScopeRef.current;
    baselinePresentationScopeRef.current = requestScope;
    setBaselineState((current) => beginResourceLoad(current, requestScope.key));
    try {
      const payload = await loadExplorerBaseline(
        channelId,
        { ...overviewBaselineQuery(overviewPeriod, timeZone), full_catalogue: true },
        controller.signal,
      );
      if (baselineRequestScopeRef.current !== requestScope) return;
      setBaselineState(completeResourceLoad(requestScope.key, sourceContainer(payload)));
      setAnnouncement('Baseline overview calculated.');
    } catch (error) {
      if (isAbortError(error)) {
        if (baselineRequestScopeRef.current === requestScope) {
          baselineAutoStartedRef.current = '';
          setBaselineState((current) => {
            const previous = resourcePayload(current);
            return previous
              ? completeResourceLoad(requestScope.key, previous)
              : { kind: 'idle' };
          });
        }
        return;
      }
      if (baselineRequestScopeRef.current !== requestScope) return;
      const message = error instanceof Error ? error.message : String(error);
      setBaselineState((current) => (
        isContractError(error)
          ? rejectResourceContract(current, requestScope.key, message)
          : failResourceLoad(current, requestScope.key, message)
      ));
    } finally {
      if (baselineRequestRef.current === controller) {
        baselineRequestRef.current = null;
      }
    }
  };

  useEffect(() => {
    if (
      !overviewVisible
      || !currentInventoryReady
      || !capabilityAvailable(capabilities, 'baseline')
      || visibleBaselineState.kind !== 'idle'
      || baselineAutoStartedRef.current === currentBaselineScope
    ) return;
    baselineAutoStartedRef.current = currentBaselineScope;
    void calculateBaseline();
  }, [capabilities, currentBaselineScope, currentInventoryReady, overviewVisible, visibleBaselineState.kind]);

  const handleConfirmHydration = async () => {
    const estimate = currentHydration?.estimate;
    if (
      !estimate
      || !currentHydration
      || !currentHydration.action_required
      || hydrationProfileReady(currentHydration)
      || hydrationNeedsPolling(currentHydration)
    ) return;
    setHydrationState({ kind: 'confirming', payload: currentHydration });
    const estimateKey = `${estimate.items}:${estimate.pages}:${estimate.quota_units}`;
    if (hydrationRetryRef.current?.estimate !== estimateKey) {
      hydrationRetryRef.current = { estimate: estimateKey, id: newSavedWorkId() };
    }
    try {
      const payload = await confirmExplorerHydration(channelId, estimate, hydrationRetryRef.current.id);
      hydrationRetryRef.current = undefined;
      hydrationPayloadRef.current = payload;
      setHydrationState({ kind: 'ready', payload });
      if (hydrationInitialBecameComplete(currentHydration, payload)) {
        setFormatTotalsReloadToken((value) => value + 1);
      }
      if (hydrationProfileReady(payload)) {
        setProfileReloadToken((value) => value + 1);
        setCounterDynamicsReloadToken((value) => value + 1);
        setCatalogueReloadToken((value) => value + 1);
        setBaselineReloadToken((value) => value + 1);
      }
      setHydrationRequestToken((value) => value + 1);
      setAnnouncement(hydrationProfileReady(payload)
        ? 'The full current long-form profile is ready.'
        : hydrationInitialComplete(payload) ? '' : 'Full profile loading started or resumed.');
    } catch (error) {
      setHydrationState({
        kind: 'error',
        message: error instanceof Error ? error.message : String(error),
        previous: currentHydration,
      });
      setAnnouncement('Full profile loading could not be started.');
    }
  };

  const retryHydrationStatus = () => {
    if (hydrationChecking) return;
    setHydrationChecking(true);
    setHydrationRequestToken((value) => value + 1);
    setAnnouncement('Checking profile loading status again.');
  };


  const handleCopyChannelId = async () => {
    const copied = await copyText(channelId);
    setCopyState(copied ? 'copied' : 'failed');
    setAnnouncement(copied ? 'Channel ID copied.' : 'Channel ID could not be copied.');
    window.clearTimeout(copyResetRef.current);
    copyResetRef.current = window.setTimeout(() => setCopyState('idle'), 1600);
  };

  const closeFolderMenu = () => {
    if (folderMenuRef.current) folderMenuRef.current.open = false;
    setFolderCreationOpen(false);
  };

  const closeLabelMenu = () => {
    if (labelMenuRef.current) labelMenuRef.current.open = false;
  };

  const handleFolderChange = async (folderId: string): Promise<boolean> => {
    if (!onAssignFolder || folderSaving) return false;
    if (folderId === folderDraftId) return true;
    const previousFolderId = folderDraftId;
    setFolderDraftId(folderId);
    setFolderSaving(true);
    setFolderError('');
    try {
      await onAssignFolder(channelId, folderId);
      const selectedFolder = folders.find((folder) => folder.id === folderId);
      setAnnouncement(
        folderId
          ? `Channel moved to ${selectedFolder?.name || 'the selected folder'}.`
          : 'Channel removed from its folder.',
      );
      return true;
    } catch (error) {
      setFolderDraftId(previousFolderId);
      setFolderError(error instanceof Error ? error.message : String(error));
      setAnnouncement('Folder assignment failed.');
      return false;
    } finally {
      setFolderSaving(false);
    }
  };

  const handleFolderSelection = async (folderId: string) => {
    if (await handleFolderChange(folderId)) closeFolderMenu();
  };

  const handleCreateFolder = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = folderName.trim();
    if (!name || !onCreateFolder || !onAssignFolder || folderSaving) return;
    let createdFolder: CreatedFolder | undefined;
    setFolderSaving(true);
    setFolderError('');
    try {
      createdFolder = await onCreateFolder(name);
      const createdFolderId = String(createdFolder.id || '').trim();
      if (!createdFolderId) throw new Error('The created folder did not return an id.');
      await onAssignFolder(channelId, createdFolderId);
      setFolderDraftId(createdFolderId);
      setFolderName('');
      closeFolderMenu();
      // A name that already exists reuses that folder instead of making a second one.
      setAnnouncement(createdFolder.created === false
        ? `Moved to the existing ${createdFolder.name || name} folder.`
        : `${createdFolder.name || name} folder created and assigned.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFolderError(
        createdFolder?.id
          ? `Folder created, but the channel could not be assigned. ${message}`
          : message,
      );
      setAnnouncement(createdFolder?.id ? 'Folder assignment failed.' : 'Folder creation failed.');
    } finally {
      setFolderSaving(false);
    }
  };

  const assignedLabelKeys = new Set(channel?.label_keys ?? []);
  const assignedLabels = labels.filter((label) => assignedLabelKeys.has(label.label_key));
  const visibleAssignedLabels = assignedLabels.slice(0, 2);
  const hiddenAssignedLabelCount = Math.max(0, assignedLabels.length - visibleAssignedLabels.length);
  const selectedFolderName = folderDraftId
    ? folders.find((folder) => folder.id === folderDraftId)?.name || 'Unknown folder'
    : 'No folder';

  const handleLabelToggle = async (labelKey: string, enabled: boolean) => {
    if (!onAssignLabel || labelSavingKey) return;
    setLabelSavingKey(labelKey);
    setLabelError('');
    try {
      await onAssignLabel(channelId, labelKey, enabled);
      setAnnouncement(enabled ? 'Label added.' : 'Label removed.');
    } catch (error) {
      setLabelError(error instanceof Error ? error.message : String(error));
      setAnnouncement('Label update failed.');
    } finally {
      setLabelSavingKey('');
    }
  };

  const handleCreateLabel = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = labelName.trim();
    if (!name || !onCreateLabel || !onAssignLabel || labelSavingKey) return;
    setLabelSavingKey('creating');
    setLabelError('');
    try {
      const label = await onCreateLabel(name);
      await onAssignLabel(channelId, label.label_key, true);
      setLabelName('');
      setAnnouncement(label.created === false
        ? `The existing ${label.name} label was added.`
        : `${label.name} label created and added.`);
    } catch (error) {
      setLabelError(error instanceof Error ? error.message : String(error));
      setAnnouncement('Label creation failed.');
    } finally {
      setLabelSavingKey('');
    }
  };

  const visibleCounterDynamicsState: CounterDynamicsState = counterDynamicsState.channelId === channelId
    ? counterDynamicsState
    : { kind: 'loading', channelId };
  const availableCounterDynamicsPeriods = visibleCounterDynamicsState.kind === 'ready'
    ? visibleCounterDynamicsState.payload.periods : undefined;
  const counterDynamicsPeriods = {
    subscribers: (counterDynamicsPeriodChoices.channelId === channelId ? counterDynamicsPeriodChoices.subscribers : null)
      ?? preferredCounterDynamicsPeriod(availableCounterDynamicsPeriods, 'subscribers'),
    views: (counterDynamicsPeriodChoices.channelId === channelId ? counterDynamicsPeriodChoices.views : null)
      ?? preferredCounterDynamicsPeriod(availableCounterDynamicsPeriods, 'views'),
  };
  const rawBusy = videosVisible
    && (visibleRawState.kind === 'loading' || visibleRawState.kind === 'updating');
  const baselineBusy = overviewVisible
    && (visibleBaselineState.kind === 'loading' || visibleBaselineState.kind === 'updating');
  const reloadBusy = rawBusy
    || baselineBusy
    || visibleProfileState.kind === 'loading'
    || typedViewsLoading
    || visibleCounterDynamicsState.kind === 'loading';
  const profileFresh = visibleProfileState.kind === 'ready'
    && visibleProfileState.payload.freshness.channel_data_fresh;
  const profileStatusTitle = visibleProfileState.kind === 'loading'
    ? 'Checking stored channel data'
    : visibleProfileState.kind === 'error'
      ? 'Stored profile check failed'
      : profileFresh
        ? `Current data checked ${formatTimestampLocal(metadataUpdatedAt, 'recently')}`
        : `Stored data checked ${formatTimestampLocal(metadataUpdatedAt, 'at an unknown time')}`;
  const profileStatusDetail = visibleProfileState.kind === 'error'
    ? `Header totals use fallback stored values. ${visibleProfileState.message}`
    : visibleProfileState.kind === 'loading'
      ? 'Fallback values remain marked until the stored profile responds.'
      : `Freshness limit: ${visibleProfileState.payload.freshness.maximum_age_days} days.`;

  const reloadCurrentView = () => {
    setProfileReloadToken((value) => value + 1);
    setFormatTotalsReloadToken((value) => value + 1);
    setCounterDynamicsReloadToken((value) => value + 1);
    if (overviewVisible) setBaselineReloadToken((value) => value + 1);
    if (videosVisible) {
      setCatalogueReloadToken((value) => value + 1);
    }
  };

  const showOverviewPeriod = activeMedia === 'videos' && activeSection === 'overview';
  const mediaControls = (
    <div className={`explorer-v2-analysis-toolbar${showOverviewPeriod ? ' has-period' : ''}`}>
      <nav className="explorer-v2-media-selector" aria-label="Media type">
        <div
          className="explorer-media-tabs explorer-v2-media-tabs explorer-v2-media-tablist"
          role="tablist"
          aria-label="Explorer media"
        >
          {mediaTabs.map((tab, index) => (
            <button
              key={tab.id}
              id={`explorer-media-${tab.id}`}
              type="button"
              role="tab"
              aria-selected={activeMedia === tab.id}
              aria-controls={`explorer-media-panel-${tab.id}`}
              tabIndex={activeMedia === tab.id ? 0 : -1}
              className={`explorer-v2-media-tab ${activeMedia === tab.id ? 'is-active' : ''}`}
              onClick={() => setActiveMedia(tab.id)}
              onKeyDown={(event) => selectTabByKeyboard(
                event,
                mediaTabs,
                index,
                setActiveMedia,
                'explorer-media',
              )}
            >
              <span>{tab.label}</span>
              <small>{tab.detail}</small>
            </button>
          ))}
        </div>
      </nav>

      {showOverviewPeriod && (
        <div
          className="explorer-v2-period-selector"
          aria-busy={visibleBaselineState.kind === 'loading' || visibleBaselineState.kind === 'updating'}
        >
          <span>Period</span>
          <div
            className="explorer-v2-period-options"
            role="group"
            aria-label="Overview publication period"
            aria-describedby="explorer-v2-period-note"
            title="Filters the analysis by video publication date. Channel counters above remain current totals."
          >
            {OVERVIEW_PERIOD_OPTIONS.map((period) => (
              <button
                key={period.key}
                type="button"
                aria-label={period.description}
                aria-pressed={overviewPeriod === period.key}
                className={overviewPeriod === period.key ? 'is-active' : undefined}
                onClick={() => {
                  if (overviewPeriod === period.key) return;
                  setOverviewPeriod(period.key);
                  setAnnouncement(`${period.description} selected. Updating publication-period analysis.`);
                }}
              >
                {period.label}
              </button>
            ))}
          </div>
          <span className="explorer-sr-only" id="explorer-v2-period-note">
            Filters Publishing rhythm, Channel fingerprint and Content breakdown by video publication date.
            The publication calendar has its own independent calendar-year selection. Channel counters above remain current totals.
          </span>
          <span>Time</span>
          <div
            className="explorer-v2-period-options"
            role="group"
            aria-label="Time used for publication hours, weekdays, days and years"
          >
            {(['local', 'utc'] as const).map((basis) => (
              <button
                key={basis}
                type="button"
                aria-pressed={timeBasis === basis}
                className={timeBasis === basis ? 'is-active' : undefined}
                title={basis === 'local' ? `Your time zone: ${localTimeZone()}` : 'Coordinated Universal Time'}
                onClick={() => {
                  if (timeBasis === basis) return;
                  setTimeBasis(basis);
                  setAnnouncement(`${basis === 'local' ? 'Local time' : 'UTC'} selected for publication hours, days and years.`);
                }}
              >
                {basis === 'local' ? 'Local' : 'UTC'}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );

  const workspaceFooter = (
      <footer className="explorer-page-controls explorer-v2-topbar">
        <button
          type="button"
          className="explorer-back-button explorer-v2-breadcrumb"
          onClick={onBack}
          aria-label={`Back to channels from ${resolvedTitle}`}
        >
          <ArrowLeft size={16} aria-hidden="true" />
          <span>Channels</span>
          <small aria-hidden="true">/</small>
          <strong>{resolvedTitle}</strong>
        </button>
        <div className="explorer-v2-topbar-actions">
          <div
            className={`explorer-v2-profile-presence is-${visibleProfileState.kind === 'ready' ? profileFresh ? 'current' : 'stale' : visibleProfileState.kind}`}
            role={visibleProfileState.kind === 'error' ? 'alert' : 'status'}
          >
            <span className="explorer-v2-status-dot" aria-hidden="true" />
            <span>{profileStatusTitle}</span>
          </div>
          <p className="explorer-v2-footer-note">
            Movement uses stored observations. Missing coverage stays unavailable rather than being estimated.
          </p>
          <div className="explorer-v2-footer-actions">
            <div className="explorer-v2-youtube-link">
              <YouTubeAttribution
                href={youtubeChannelUrl(channelId)}
                label={`Open ${resolvedTitle} on YouTube`}
              />
            </div>
            <details
              ref={operationsMenuRef}
              className="explorer-v2-operations"
              onKeyDown={(event) => {
                if (event.key !== 'Escape') return;
                event.preventDefault();
                const menu = operationsMenuRef.current;
                if (!menu) return;
                menu.open = false;
                menu.querySelector<HTMLElement>('summary')?.focus();
              }}
            >
            <summary aria-label="More channel actions">
              <MoreHorizontal size={19} aria-hidden="true" />
              <span className="explorer-sr-only">More channel actions</span>
            </summary>
            <div className="explorer-v2-operations-popover">
              <section className="explorer-v2-operation-section" aria-labelledby="explorer-v2-actions-title">
                <div className="explorer-v2-operation-heading">
                  <div>
                    <span className="explorer-eyebrow">Workspace</span>
                    <h2 id="explorer-v2-actions-title">Channel actions</h2>
                  </div>
                </div>
                <div className="explorer-v2-operation-actions">
                  <button
                    type="button"
                    className={`explorer-secondary-button explorer-copy-profile-id is-${copyState}`}
                    onClick={() => void handleCopyChannelId()}
                    aria-label={`Copy channel ID for ${resolvedTitle}`}
                  >
                    {copyState === 'copied'
                      ? <Check size={14} aria-hidden="true" />
                      : <Copy size={14} aria-hidden="true" />}
                    {copyState === 'copied'
                      ? 'Copied'
                      : copyState === 'failed' ? 'Retry copy' : 'Copy channel ID'}
                  </button>
                  <button
                    type="button"
                    className="explorer-secondary-button explorer-v2-reload"
                    onClick={reloadCurrentView}
                    disabled={reloadBusy}
                    aria-label={overviewVisible ? 'Reload profile' : 'Reload current'}
                  >
                    <RefreshCw className={reloadBusy ? 'explorer-spin' : ''} size={16} aria-hidden="true" />
                    <span>{overviewVisible ? 'Reload profile' : 'Reload current'}</span>
                  </button>
                </div>

              </section>

              <section
                className={`explorer-profile-status explorer-v2-operation-section is-${visibleProfileState.kind === 'ready' ? profileFresh ? 'current' : 'stale' : visibleProfileState.kind}`}
                aria-labelledby="explorer-v2-data-status-title"
              >
                {visibleProfileState.kind === 'ready' && profileFresh
                  ? <ShieldCheck size={16} aria-hidden="true" />
                  : visibleProfileState.kind === 'loading'
                    ? <RefreshCw className="explorer-spin" size={16} aria-hidden="true" />
                    : <AlertTriangle size={16} aria-hidden="true" />}
                <div>
                  <strong id="explorer-v2-data-status-title">{profileStatusTitle}</strong>
                  <span>{profileStatusDetail}</span>
                </div>
                {visibleProfileState.kind === 'error' && (
                  <button type="button" onClick={() => setProfileReloadToken((value) => value + 1)}>Retry profile</button>
                )}
              </section>

              {baselinePayload && !sourceExpired(baselinePayload.sample) && (
                <section className="explorer-v2-operation-section explorer-v2-coverage-summary" aria-labelledby="explorer-v2-coverage-title">
                  <div className="explorer-v2-operation-heading">
                    <div>
                      <span className="explorer-eyebrow">Overview sample</span>
                      <h2 id="explorer-v2-coverage-title">Coverage</h2>
                    </div>
                    <small>{formatTimestampLocal(baselinePayload.generated_at, 'Unknown')}</small>
                  </div>
                  <dl>
                    <div><dt>Matched videos</dt><dd>{formatCount(baselinePayload.sample?.matched_video_count, { compact: false })}</dd></div>
                    <div><dt>Scanned inventory</dt><dd>{formatCount(baselinePayload.sample?.scanned_inventory_count, { compact: false })}</dd></div>
                    <div><dt>Missing rows</dt><dd>{formatCount(baselinePayload.sample?.missing_video_row_count, { compact: false })}</dd></div>
                    <div><dt>Stale withheld</dt><dd>{formatCount(baselinePayload.sample?.withheld_stale_count, { compact: false })}</dd></div>
                    <div><dt>Availability exclusions</dt><dd>{formatCount(baselinePayload.sample?.excluded_by_availability_count, { compact: false })}</dd></div>
                  </dl>
                </section>
              )}

            </div>
            </details>
          </div>
        </div>
      </footer>
  );

  return (
    <main className="explorer-workspace explorer-v2-workspace">
      <header className="explorer-channel-header explorer-channel-profile explorer-v2-channel-hero">
        <div className="explorer-v2-profile-main">
          <a
            className="explorer-avatar explorer-profile-avatar"
            href={youtubeChannelUrl(channelId)}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open ${resolvedTitle} on YouTube`}
          >
            {avatarUrl && !avatarFailed ? (
              <img src={avatarUrl} alt="" referrerPolicy="no-referrer" onError={() => setAvatarFailed(true)} />
            ) : initials(resolvedTitle)}
          </a>
          <div className="explorer-channel-identity">
            <span className="explorer-eyebrow">Channel Explorer</span>
            <h1>{resolvedTitle}</h1>
            <div className="explorer-channel-meta" aria-label="Channel metadata">
              <span>{formatCountryName(countryCode)}</span>
              <span title={channelCreatedAt ? `Created ${formatDateLocal(channelCreatedAt)}` : undefined}>
                {formatChannelAge(channelCreatedAt)}
              </span>
              <section className="explorer-v2-hero-organizer" aria-label={`Organize ${resolvedTitle}`}>
                <div className="explorer-folder-control">
                  <details
                    ref={folderMenuRef}
                    className="explorer-folder-menu"
                    onToggle={(event) => {
                      if (event.currentTarget.open) closeLabelMenu();
                      else setFolderCreationOpen(false);
                    }}
                    onKeyDown={(event) => {
                      if (event.key !== 'Escape') return;
                      event.preventDefault();
                      closeFolderMenu();
                      folderMenuRef.current?.querySelector<HTMLElement>('summary')?.focus();
                    }}
                  >
                    <summary
                      aria-label={`Folder for ${resolvedTitle}: ${selectedFolderName}`}
                      aria-disabled={!onAssignFolder || folderSaving}
                      onClick={(event) => {
                        if (!onAssignFolder || folderSaving) event.preventDefault();
                      }}
                    >
                      <span className="explorer-organizer-action-label">
                        <FolderIcon size={14} aria-hidden="true" />
                        <span>Folder</span>
                      </span>
                      <span className="explorer-organizer-current-value" title={selectedFolderName}>
                        {selectedFolderName}
                      </span>
                      <ChevronDown size={13} aria-hidden="true" />
                    </summary>
                    <div className="explorer-folder-popover">
                      {!folderCreationOpen ? (
                        <button
                          type="button"
                          className="explorer-folder-create-trigger"
                          disabled={!onCreateFolder || !onAssignFolder || folderSaving}
                          onClick={() => {
                            setFolderError('');
                            setFolderCreationOpen(true);
                          }}
                        >
                          <FolderPlus size={14} aria-hidden="true" />
                          <span>Add new folder</span>
                        </button>
                      ) : (
                        <form
                          className="explorer-folder-create-form"
                          onSubmit={(event) => void handleCreateFolder(event)}
                        >
                          <input
                            autoFocus
                            value={folderName}
                            onChange={(event) => setFolderName(event.target.value)}
                            maxLength={80}
                            placeholder="New folder"
                            aria-label="New folder name"
                          />
                          <button type="submit" disabled={!folderName.trim() || folderSaving}>
                            {folderSaving ? 'Adding...' : 'Add'}
                          </button>
                          <button
                            type="button"
                            className="explorer-folder-create-cancel"
                            disabled={folderSaving}
                            onClick={() => {
                              setFolderName('');
                              setFolderCreationOpen(false);
                            }}
                          >
                            Cancel
                          </button>
                        </form>
                      )}
                      <div className="explorer-folder-options" aria-label="Available folders">
                        <button
                          type="button"
                          className={!folderDraftId ? 'is-active' : ''}
                          disabled={!onAssignFolder || folderSaving}
                          onClick={() => void handleFolderSelection('')}
                        >
                          <span>No folder</span>
                          {!folderDraftId && <Check size={12} aria-hidden="true" />}
                        </button>
                        {folders.map((folder) => folder.id ? (
                          <button
                            type="button"
                            key={folder.id}
                            className={folder.id === folderDraftId ? 'is-active' : ''}
                            disabled={!onAssignFolder || folderSaving}
                            onClick={() => void handleFolderSelection(folder.id || '')}
                          >
                            <span>{folder.name || 'Untitled folder'}</span>
                            {folder.id === folderDraftId && <Check size={12} aria-hidden="true" />}
                          </button>
                        ) : null)}
                      </div>
                    </div>
                  </details>
                  {folderSaving && <small>Saving…</small>}
                  {folderError && <small className="is-error">{folderError}</small>}
                </div>

                <div className="explorer-label-editor" aria-label="Channel labels">
                  <details
                    ref={labelMenuRef}
                    className={`explorer-label-menu ${assignedLabels.length > 0 ? 'has-labels' : 'is-empty'}`}
                    onToggle={(event) => {
                      if (event.currentTarget.open) closeFolderMenu();
                    }}
                    onKeyDown={(event) => {
                      if (event.key !== 'Escape') return;
                      event.preventDefault();
                      closeLabelMenu();
                      labelMenuRef.current?.querySelector<HTMLElement>('summary')?.focus();
                    }}
                  >
                    <summary aria-label={`Manage channel labels (${assignedLabels.length} assigned)`}>
                      {assignedLabels.length > 0 ? (
                        <>
                          {visibleAssignedLabels.map((label) => (
                            <span key={label.label_key} className="explorer-label-chip">{label.name}</span>
                          ))}
                          {hiddenAssignedLabelCount > 0 && (
                            <span
                              className="explorer-label-overflow"
                              title={assignedLabels.slice(2).map((label) => label.name).join(', ')}
                            >
                              +{hiddenAssignedLabelCount}
                            </span>
                          )}
                        </>
                      ) : (
                        <>
                          <Tag size={13} aria-hidden="true" />
                          <span>Add label</span>
                        </>
                      )}
                    </summary>
                    <div className="explorer-label-popover">
                      {labels.length > 0 && (
                        <div className="explorer-label-options" aria-label="Available labels">
                          {labels.map((label) => {
                            const enabled = assignedLabelKeys.has(label.label_key);
                            return (
                              <button
                                type="button"
                                key={label.label_key}
                                className={enabled ? 'is-active' : ''}
                                aria-pressed={enabled}
                                disabled={!onAssignLabel || Boolean(labelSavingKey)}
                                onClick={() => void handleLabelToggle(label.label_key, !enabled)}
                              >
                                <span>{label.name}</span>
                                {enabled && <Check size={12} aria-hidden="true" />}
                              </button>
                            );
                          })}
                        </div>
                      )}
                      <form onSubmit={(event) => void handleCreateLabel(event)}>
                        <input
                          value={labelName}
                          onChange={(event) => setLabelName(event.target.value)}
                          maxLength={40}
                          placeholder="New label"
                          aria-label="New custom label"
                        />
                        <button
                          type="submit"
                          disabled={!labelName.trim() || !onCreateLabel || Boolean(labelSavingKey)}
                        >
                          {labelSavingKey === 'creating' ? 'Adding…' : 'Add'}
                        </button>
                      </form>
                    </div>
                  </details>
                  {labelError && <small className="is-error">{labelError}</small>}
                </div>
              </section>
            </div>
          </div>
        </div>
        <section
          className="explorer-v2-hero-kpis"
          aria-label="Channel source totals by format"
          aria-busy={
            typedViewsLoading
            || visibleProfileState.kind === 'loading'
            || visibleCounterDynamicsState.kind === 'loading'
          }
        >
          <div className="explorer-channel-kpis explorer-v2-kpi-grid" aria-label="Channel and stored format view totals">
            <article className="explorer-v2-kpi-card">
              <div className="explorer-v2-kpi-heading">
                <span>Subscribers</span>
                <small className="explorer-v2-kpi-source" title="YouTube API">YouTube API</small>
              </div>
              <div className="explorer-v2-kpi-value-row">
                <strong>{hasReportedMetric(subscriberCount) ? formatCount(subscriberCount) : MISSING_VALUE}</strong>
                <CounterDynamicsTrend
                  state={visibleCounterDynamicsState}
                  metricName="subscribers"
                  label="Subscribers"
                  period={counterDynamicsPeriods.subscribers}
                  onPeriodChange={(period) => chooseCounterDynamicsPeriod('subscribers', period)}
                />
              </div>
            </article>
            <article className="explorer-v2-kpi-card">
              <div className="explorer-v2-kpi-heading">
                <span>Total views</span>
                <small className="explorer-v2-kpi-source" title="YouTube API">YouTube API</small>
              </div>
              <div className="explorer-v2-kpi-value-row">
                <strong>{hasReportedMetric(channelViewCount) ? formatCount(channelViewCount) : MISSING_VALUE}</strong>
                <CounterDynamicsTrend
                  state={visibleCounterDynamicsState}
                  metricName="views"
                  label="Total views"
                  period={counterDynamicsPeriods.views}
                  onPeriodChange={(period) => chooseCounterDynamicsPeriod('views', period)}
                />
              </div>
            </article>
            <article className="explorer-v2-kpi-card">
              <div className="explorer-v2-kpi-heading">
                <span>Videos views</span>
                <small className="explorer-v2-kpi-source is-product" title="Computed by product analysis over stored videos">Analysis</small>
              </div>
              <div className="explorer-v2-kpi-value-row">
                <strong>
                  {typedViewsLoading ? (
                    <span
                      className="explorer-v2-kpi-value-skeleton"
                      aria-label="Loading current long-form views"
                    />
                  ) : hasReportedMetric(videosViewCount) ? formatCount(videosViewCount) : MISSING_VALUE}
                </strong>
                {!typedViewsLoading && !hasReportedMetric(videosViewCount) && (
                  formatTotalsError ? (
                    <button
                      type="button"
                      className="explorer-v2-kpi-inline-retry"
                      title={formatTotalsError}
                      onClick={() => setFormatTotalsReloadToken((value) => value + 1)}
                    >
                      <RefreshCw size={11} aria-hidden="true" />
                      Retry
                    </button>
                  ) : formatTotalsCoverageIssue ? (
                    <small className="explorer-v2-kpi-state" title={formatTotalsCoverageIssue}>
                      {longFormat?.status === 'stale' ? 'Needs refresh' : 'Incomplete'}
                    </small>
                  ) : null
                )}
                <small className="explorer-v2-kpi-format-note">long-form</small>
              </div>
            </article>
            {([
              { key: 'short', label: 'Shorts views', note: 'short-form', format: shortFormat },
              { key: 'stream', label: 'Live views', note: 'live', format: streamFormat },
            ] as const).map(({ key, label, note, format }) => (
              <article className="explorer-v2-kpi-card" key={key}>
                <div className="explorer-v2-kpi-heading">
                  <span>{label}</span>
                  <small className="explorer-v2-kpi-source is-product" title="Computed by product analysis over stored videos">Analysis</small>
                </div>
                <div className="explorer-v2-kpi-value-row">
                  <strong>
                    {typedViewsLoading ? (
                      <span className="explorer-v2-kpi-value-skeleton" aria-label={`Loading current ${label.toLowerCase()}`} />
                    ) : format?.status === 'ready' ? formatCount(format.views) : MISSING_VALUE}
                  </strong>
                  {!typedViewsLoading && (
                    formatTotalsError ? (
                      <button
                        type="button"
                        className="explorer-v2-kpi-inline-retry"
                        title={formatTotalsError}
                        onClick={() => setFormatTotalsReloadToken((value) => value + 1)}
                      >
                        <RefreshCw size={11} aria-hidden="true" />
                        Retry
                      </button>
                    ) : (
                      <small className={format?.status === 'ready' ? 'explorer-v2-kpi-format-note' : 'explorer-v2-kpi-state'}>
                        {format?.status === 'ready' ? note
                          : format?.status === 'absent' ? 'No public playlist'
                            : format?.status === 'stale' ? 'Needs refresh'
                              : format?.status === 'incomplete' ? 'Incomplete' : 'Not collected'}
                      </small>
                    )
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      </header>

      <ExplorerHydrationBanner
        state={hydrationState}
        checking={hydrationChecking}
        youtubeVideoTotal={numberValue(rawPayload ? rawPayload.channel?.video_count : channel?.video_count)}
        onConfirm={() => void handleConfirmHydration()}
        onRetry={retryHydrationStatus}
      />
      {profilePayload?.public_video_count_notice
        && !sourceExpired(profilePayload.public_video_count_notice) ? (
          <p className="explorer-v2-unlisted-note" role="note">
            Public video count decreased since the previous check.
          </p>
        ) : null}
      {unlistedVideos ? (
        <p className="explorer-v2-unlisted-note" role="note">
          YouTube counts {formatCount(unlistedVideos, { compact: false })} more {videoNoun(unlistedVideos)} for this
          {' '}channel than its public lists contain. The difference may include members-only videos or new uploads
          {' '}awaiting the next collection pass; it does not identify which videos are missing.
        </p>
      ) : null}

      <div className="explorer-v2-prepanel-controls">
        <nav className="explorer-section-nav explorer-v2-page-tabs" aria-label="Explorer workspace sections" hidden={activeMedia !== 'videos'}>
          <div
            className="explorer-section-tabs explorer-v2-page-tablist"
            role="tablist"
            aria-label="Explorer workspace sections"
          >
            {sectionTabs.map((tab, index) => {
              return (
                <button
                  key={tab.id}
                  id={`explorer-section-${tab.id}`}
                  type="button"
                  role="tab"
                  aria-selected={activeSection === tab.id}
                  aria-controls={`explorer-section-panel-${tab.id}`}
                  tabIndex={activeSection === tab.id ? 0 : -1}
                  className={`explorer-v2-page-tab ${activeSection === tab.id ? 'is-active' : ''}`}
                  onClick={() => setActiveSection(tab.id)}
                  onKeyDown={(event) => selectTabByKeyboard(
                    event,
                    sectionTabs,
                    index,
                    setActiveSection,
                    'explorer-section',
                  )}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>
        </nav>

      </div>

      {mediaControls}

      <section
        className="explorer-v2-media-panel"
        role="tabpanel"
        id="explorer-media-panel-videos"
        aria-labelledby="explorer-media-videos"
        hidden={activeMedia !== 'videos'}
      >
        <div
          role="tabpanel"
          id="explorer-section-panel-overview"
          aria-labelledby="explorer-section-overview"
          hidden={activeSection !== 'overview'}
        >
          <OverviewPanel
            key={channelId}
            channelId={channelId}
            baselineState={visibleBaselineState}
            capabilities={capabilities}
            hydrationState={hydrationState}
            onBaselineRetry={() => void calculateBaseline()}
            onRetryCapabilities={onRetryCapabilities}
            onOverviewBucket={baselinePayload?.period?.end_at && visibleBaselineState.kind === 'ready' ? openOverviewBucket : undefined}
          />
        </div>
        <div
          role="tabpanel"
          id="explorer-section-panel-videos"
          aria-labelledby="explorer-section-videos"
          hidden={activeSection !== 'videos'}
        >
          {currentProfileReady ? (
            <ExplorerVideosWorkspace
              channelId={channelId}
              selectionScopeKey={currentAnalysisScope}
              rawState={visibleRawState}
              blockedMessage={navigation.error || (staticFiltersBlocked
                ? 'Static analysis filters are unavailable. Remove these filters to continue; your conditions have been kept.' : '') || (growthFiltersBlocked
                ? 'Growth filters are unavailable. Reload or remove these filters to continue; your conditions have been kept.' : '') || (!growthSortAvailable && applied.sort_by === VIDEO_GROWTH_SORT_FIELD && capabilities.kind !== 'loading'
                ? 'Growth sorting is unavailable. Choose another sort; your filters have been kept.' : '')}
              onResetQuery={() => {
                const next = createInitialCatalogueConditions();
                navigation.navigate({ ...navigation.state, section: 'videos', conditions: next });
                setDraftConditions(next); setFilterErrors({}); setFilterError('');
              }}
              toolbar={{
                draft: draftConditions, applied: conditions, categories, errors: filterErrors, error: filterError,
                growthSortAvailable, facetsAvailable: facetReadsAvailable, derivedFiltersAvailable, facetState: visibleFacetState,
                matchedCount: visibleRawState.kind === 'ready' && !sourceExpired(rawPayload?.pagination) ? rawPayload?.pagination?.matched_count : undefined,
                incomplete: !sourceExpired(rawPayload?.coverage) && rawPayload?.coverage?.complete === false,
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
                saveViewDisabled: Boolean(navigation.error),
                savedViewMessage: savedMessage,
                onSaveView: () => {
                  if (navigation.error) return;
                  setSavedMessage('');
                  setSaveSnapshot({ channelId, channelName: resolvedTitle, conditions: normalizeCatalogueConditions(conditions) });
                },
              }}
              growthSortAvailable={growthSortAvailable}
              staticDetailsAvailable={staticDetailsAvailable}
              launchAvailable={launchAvailable}
              historyAvailable={historyAvailable}
              browse={browse}
              onRetryCatalogue={() => setCatalogueReloadToken((value) => value + 1)}
            />
          ) : (
            initialComplete
              ? <div className="explorer-empty-copy" role="status">No data available.</div>
              : <HydrationDependentPanel sectionName="Videos" />
          )}
        </div>
      </section>

      {mediaTabs.filter((tab) => tab.id !== 'videos').map((tab) => (
        <section
          key={tab.id}
          className="explorer-coming-soon explorer-v2-media-panel"
          role="tabpanel"
          id={`explorer-media-panel-${tab.id}`}
          aria-labelledby={`explorer-media-${tab.id}`}
          hidden={activeMedia !== tab.id}
        >
          <Database size={28} aria-hidden="true" />
          <span className="explorer-eyebrow">{tab.label}</span>
          <h2>Coming soon</h2>
          <p>This media contour is intentionally separate. Opening this placeholder starts no analytics request.</p>
        </section>
      ))}

      {workspaceFooter}
      {saveSnapshot && <SaveViewDialog snapshot={saveSnapshot} onClose={() => setSaveSnapshot(undefined)}
        onSaved={() => { setSaveSnapshot(undefined); setSavedMessage('View saved'); }} />}
      <span className="explorer-sr-only" aria-live="polite">{announcement}</span>
    </main>
  );
}
