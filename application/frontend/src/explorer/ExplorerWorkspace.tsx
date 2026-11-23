import { ManualListCheckControl } from './ManualListCheckControl';
import {
  AlertTriangle,
  LoaderCircle,
  RefreshCw,
} from 'lucide-react';
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import type {
  Channel,
  ChannelBaselinePeriodKey,
  CreatedFolder,
  CreatedLabel,
  ExplorerLabel,
  Folder,
  VideoType,
} from '../types';
import {
  confirmExplorerHydration,
  isAbortError,
  loadExplorerHydration,
} from './api';
import { newSavedWorkId } from './savedWork';
import type { CataloguePresetGroup } from './catalogueQuery';
import { useCatalogueNavigation } from './useCatalogueNavigation';
import {
  formatCount,
  videoNoun,
} from './format';
import { numberValue } from '../format';
import { useTimeBasis } from './timeBasis';
import type {
  ExplorerHydrationPayload,
  ExplorerHydrationState,
  ExplorerMedia,
  ExplorerSection,
} from './model';
import {
  EXPLORER_HYDRATION_BACKGROUND_POLL_MS,
  EXPLORER_HYDRATION_POLL_MS,
  hydrationInitialBecameComplete,
  hydrationInitialComplete,
  hydrationNeedsPolling,
  hydrationPayload,
  hydrationPhaseTitle,
  hydrationProfileReady,
  hydrationNeedsRestart,
  hydrationStoppedReason,
} from './hydration';
import { ChannelFooter, ChannelHeader, ChannelNotes, useChannelHeader } from './ChannelHeader';
import {
  LibraryOverviewSection,
  OverviewCoverageSummary,
  OverviewPeriodSelector,
  useLibraryOverview,
} from './LibraryOverviewSection';
import type { WorkshopHandoff } from './workshopHandoff';
import { LibraryVideosSaveDialog, LibraryVideosSection, useLibraryVideos } from './LibraryVideosSection';
import type { LibraryShellContext } from './libraryShell';
import { loadResearchCapabilities } from './researchQuery';
import { formatTurnedOffHint, formatTurnedOffTitle, type OffFormat } from './formatsOff';

// Channel page shell: address (section, period, media type), profile loading,
// reload tokens, tabs and layout. The header, Overview and Videos keep their
// own state and requests in ChannelHeader.tsx, LibraryOverviewSection.tsx and
// LibraryVideosSection.tsx. Long, Shorts and Streams mount the same Overview
// and Videos sections; the address's `type` chooses the format.

const mediaTabs: Array<{ id: ExplorerMedia; type: VideoType; label: string; detail: string }> = [
  { id: 'videos', type: 'long', label: 'Videos', detail: 'long-form' },
  { id: 'shorts', type: 'short', label: 'Shorts', detail: 'short-form' },
  { id: 'streams', type: 'stream', label: 'Streams', detail: 'live' },
];

type WorkspaceSection = Extract<ExplorerSection, 'overview' | 'videos'>;

const sectionTabs: Array<{
  id: WorkspaceSection;
  label: string;
}> = [
  { id: 'overview', label: 'Overview' },
  { id: 'videos', label: 'Videos' },
];

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

export function ExplorerWorkspace({
  apiAvailable = false,
  channelId,
  channel,
  folders = [],
  labels = [],
  onAssignFolder,
  onCreateFolder,
  onCreateLabel,
  onAssignLabel,
  onBack,
  onOpenWorkshop,
}: {
  /** False while the HOT API is closed (runtime notices). */
  apiAvailable?: boolean;
  channelId: string;
  channel?: Channel;
  folders?: Folder[];
  labels?: ExplorerLabel[];
  onAssignFolder?: (channelId: string, folderId: string) => Promise<void>;
  onCreateFolder?: (name: string) => Promise<CreatedFolder>;
  onCreateLabel?: (name: string) => Promise<CreatedLabel>;
  onAssignLabel?: (channelId: string, labelKey: string, enabled: boolean) => Promise<void>;
  onBack: () => void;
  // Library's "Open in research workshop" / "Send selected to workshop".
  onOpenWorkshop?: (handoff: WorkshopHandoff) => void;
}) {
  const navigation = useCatalogueNavigation();
  // The media tab is the address's video type: Long is the absent `type`.
  const activeMedia: ExplorerMedia = mediaTabs.find((tab) => tab.type === navigation.state.type)?.id ?? 'videos';
  const activeSection = navigation.state.section;
  const overviewPeriod = navigation.state.overviewPeriod;
  // Publication hours, days and years: the viewer's own time zone, or UTC.
  const { basis: timeBasis, zone: timeZone, setBasis: setTimeBasis } = useTimeBasis();
  const setActiveSection = (section: WorkspaceSection) => navigation.navigate({ ...navigation.state, section });
  const setActiveMedia = (media: ExplorerMedia) => {
    const type = mediaTabs.find((tab) => tab.id === media)?.type ?? 'long';
    if (type === navigation.state.type) return;
    // A media tab change is navigation like a section change: it writes `type`
    // only for Shorts and Streams. An omitted type selects Long.
    navigation.navigate({ ...navigation.state, type });
  };
  const setOverviewPeriod = (period: ChannelBaselinePeriodKey) => navigation.navigate({ ...navigation.state, overviewPeriod: period });
  const [profileReloadToken, setProfileReloadToken] = useState(0);
  const [formatTotalsReloadToken, setFormatTotalsReloadToken] = useState(0);
  const [listInventoryReloadToken, setListInventoryReloadToken] = useState(0);
  const [counterDynamicsReloadToken, setCounterDynamicsReloadToken] = useState(0);
  const [catalogueReloadToken, setCatalogueReloadToken] = useState(0);
  const [baselineReloadToken, setBaselineReloadToken] = useState(0);
  const [announcement, setAnnouncement] = useState('');
  const [hydrationState, setHydrationState] = useState<ExplorerHydrationState>({ kind: 'loading' });
  const [hydrationChecking, setHydrationChecking] = useState(false);
  const [hydrationRequestToken, setHydrationRequestToken] = useState(0);
  // Formats the owner turned off in config.ini. Their tab shows a
  // stub instead of Overview and Videos, and those sections send no request.
  const [turnedOff, setTurnedOff] = useState<OffFormat[]>([]);
  useEffect(() => {
    if (!apiAvailable) return undefined;
    const controller = new AbortController();
    loadResearchCapabilities(controller.signal)
      .then((value) => { if (!controller.signal.aborted) setTurnedOff(value.turned_off_video_types); })
      .catch(() => {});
    return () => controller.abort();
  }, [apiAvailable, channelId]);
  const formatOff = turnedOff.includes(navigation.state.type as OffFormat);
  // The same Overview and Videos sections for every format.
  const overviewVisible = activeSection === 'overview' && !formatOff;
  const videosVisible = activeSection === 'videos' && !formatOff;
  const currentHydration = hydrationPayload(hydrationState);
  const hydrationPayloadRef = useRef<ExplorerHydrationPayload | undefined>(undefined);
  // One idempotency key per estimate: retrying after an uncertain outcome
  // repeats the same confirmation instead of sending an independent one.
  const hydrationRetryRef = useRef<{ estimate: string; id: string } | undefined>(undefined);

  const context: LibraryShellContext = {
    channelId, channel, videoType: navigation.state.type, apiAvailable,
    navigation, activeSection, overviewPeriod, timeZone,
    overviewVisible, videosVisible,
    profileReloadToken, setProfileReloadToken, formatTotalsReloadToken, setFormatTotalsReloadToken, listInventoryReloadToken,
    counterDynamicsReloadToken, setCounterDynamicsReloadToken, catalogueReloadToken, setCatalogueReloadToken,
    baselineReloadToken, setBaselineReloadToken, setAnnouncement,
  };
  const videos = useLibraryVideos(context);

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
        const wasLoading = lastPayload && hydrationNeedsPolling(lastPayload);
        const initialBecameComplete = hydrationInitialBecameComplete(lastPayload, payload);
        lastPayload = payload;
        hydrationPayloadRef.current = payload;
        setHydrationState({ kind: 'ready', payload });
        if (wasLoading && initialBecameComplete) {
          setProfileReloadToken(value => value + 1);
          setListInventoryReloadToken(value => value + 1);
          setFormatTotalsReloadToken(value => value + 1);
          setCounterDynamicsReloadToken(value => value + 1);
          setCatalogueReloadToken(value => value + 1);
          setBaselineReloadToken(value => value + 1);
          setAnnouncement('Initial channel loading finished.');
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

  const header = useChannelHeader(
    context,
    { folders, labels, onAssignFolder, onCreateFolder, onCreateLabel, onAssignLabel },
    videos.browseFallback,
  );
  const overview = useLibraryOverview(context);

  const openOverviewBucket = (field: CataloguePresetGroup, key: string) => {
    // The bucket opens in the Videos section of the same format (its own duration presets).
    const normalized = overview.bucketConditions(field, key);
    if (!normalized) return;
    videos.setDraftConditions(normalized);
    navigation.navigate({ ...navigation.state, section: 'videos', conditions: normalized });
  };

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
      if (hydrationInitialComplete(payload)) {
        setListInventoryReloadToken(value => value + 1);
        setProfileReloadToken((value) => value + 1);
        setCounterDynamicsReloadToken((value) => value + 1);
        setCatalogueReloadToken((value) => value + 1);
        setBaselineReloadToken((value) => value + 1);
      }
      setHydrationRequestToken((value) => value + 1);
      setAnnouncement(hydrationInitialComplete(payload)
        ? 'Initial channel loading finished.' : 'Full profile loading started or resumed.');
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

  const reloadBusy = videos.busy
    || overview.busy
    || header.loading;

  const reloadCurrentView = () => {
    setProfileReloadToken((value) => value + 1);
    setFormatTotalsReloadToken((value) => value + 1);
    setListInventoryReloadToken((value) => value + 1);
    setCounterDynamicsReloadToken((value) => value + 1);
    if (overviewVisible) setBaselineReloadToken((value) => value + 1);
    if (videosVisible) {
      setCatalogueReloadToken((value) => value + 1);
    }
  };

  const showOverviewPeriod = activeSection === 'overview';
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
              <small>{turnedOff.includes(tab.type as OffFormat) ? 'turned off' : tab.detail}</small>
            </button>
          ))}
        </div>
      </nav>

      {showOverviewPeriod && (
        <OverviewPeriodSelector
          overview={overview}
          overviewPeriod={overviewPeriod}
          setOverviewPeriod={setOverviewPeriod}
          timeBasis={timeBasis}
          setTimeBasis={setTimeBasis}
          setAnnouncement={setAnnouncement}
        />
      )}
    </div>
  );

  return (
    <main className="explorer-workspace explorer-v2-workspace">
      <ChannelHeader header={header} />

      <ExplorerHydrationBanner
        state={hydrationState}
        checking={hydrationChecking}
        youtubeVideoTotal={numberValue(videos.browseFallback ? videos.browseFallback.channel?.video_count : channel?.video_count)}
        onConfirm={() => void handleConfirmHydration()}
        onRetry={retryHydrationStatus}
      />
      <ChannelNotes header={header} />

      <div className="explorer-v2-prepanel-controls">
        <nav className="explorer-section-nav explorer-v2-page-tabs" aria-label="Explorer workspace sections">
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
      {!formatOff && header.currentFormat?.manual_check && <ManualListCheckControl
        key={channelId + navigation.state.type} channelId={channelId} type={navigation.state.type}
        coverage={header.currentFormat.status} check={header.currentFormat.manual_check}
        onRead={() => setListInventoryReloadToken(value => value + 1)}
        onComplete={() => { setBaselineReloadToken(value => value + 1); setCatalogueReloadToken(value => value + 1); }}
      />}
      {!formatOff && header.coverageMessage && (
        <p className="explorer-inline-warning" role="status">{header.coverageMessage}</p>
      )}

      {/* The active format's panel stays in one place, so Videos keeps its grid and per-format
          selection across format tabs; the other tabs' panels are present and empty, so every
          media tab controls a real panel and the section tabs control this panel's sections. */}
      <section
        className="explorer-v2-media-panel"
        role="tabpanel"
        id={`explorer-media-panel-${activeMedia}`}
        aria-labelledby={`explorer-media-${activeMedia}`}
      >
        {formatOff && (
          <div className="explorer-v2-empty-state explorer-format-turned-off" role="status">
            <strong>{formatTurnedOffTitle(navigation.state.type as OffFormat)}</strong>
            <p>{formatTurnedOffHint(navigation.state.type as OffFormat)}</p>
          </div>
        )}
        <div
          role="tabpanel"
          id="explorer-section-panel-overview"
          aria-labelledby="explorer-section-overview"
          hidden={activeSection !== 'overview' || formatOff}
        >
          <LibraryOverviewSection overview={overview} context={context} onOverviewBucket={openOverviewBucket} />
        </div>
        <div
          role="tabpanel"
          id="explorer-section-panel-videos"
          aria-labelledby="explorer-section-videos"
          hidden={activeSection !== 'videos' || formatOff}
        >
          <LibraryVideosSection videos={videos} context={context} channelTitle={header.resolvedTitle} onOpenWorkshop={onOpenWorkshop} />
        </div>
      </section>

      {mediaTabs.filter((tab) => tab.id !== activeMedia).map((tab) => (
        <section
          key={tab.id}
          className="explorer-v2-media-panel"
          role="tabpanel"
          id={`explorer-media-panel-${tab.id}`}
          aria-labelledby={`explorer-media-${tab.id}`}
          hidden
        />
      ))}

      <ChannelFooter
        header={header}
        onBack={onBack}
        reloadCurrentView={reloadCurrentView}
        reloadBusy={reloadBusy}
        overviewVisible={overviewVisible}
        coverage={<OverviewCoverageSummary overview={overview} />}
      />
      <LibraryVideosSaveDialog videos={videos} />
      <span className="explorer-sr-only" aria-live="polite">{announcement}</span>
    </main>
  );
}
