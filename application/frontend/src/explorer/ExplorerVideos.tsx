import { useSourceExpiry } from '../sourceLifetimeReact';
import { freshSourceRows, sourceExpired, sourceOwnExpired } from '../sourceLifetime.ts';
import {
  AlertTriangle,
  ExternalLink,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import type {
  ChannelExplorerVideo,
  VideoType,
} from '../types';
import {
  formatCount,
  formatDuration,
  hasMetricValue,
  youtubeVideoUrl,
} from './format';
import { presentVideoViewGrowth } from './videoGrowth';
import { formatDateLocal } from './localTime';
import type { LibraryVideosState } from './useCatalogueBrowse';
import type { LibraryCardField, LibraryVideo, LibraryVideosPayload } from './libraryData/videos.ts';
import type { InspectorGates } from './libraryData/inspector.ts';
import { LIBRARY_ERROR_TEXT } from './libraryData/errors.ts';
import { ErrorState, LoadingState } from './ModuleState';
import { resourcePayload } from './resourceState';
import type { CatalogueDensity } from './cataloguePreferences';
import {
  VirtualVideoGrid, type VideoGridRestoreAnchor, type VirtualVideoGridHandle,
} from './VirtualVideoGrid';
import type { VideoGridAnchor } from './videoGridGeometry';
import { addSelectedRecords, type CollectMatching, type SelectionProgress } from './catalogueSelection';
import { VideoThumbnail, videoTitle } from './VideoThumbnail';
import { VideoInspector } from './VideoInspector';

/** The selection of a format nobody has selected in yet (never written). */
const NO_SELECTION: ReadonlyMap<string, ChannelExplorerVideo> = new Map();

// Saved collections import the Inspector from here as well.
export { VideoInspector };

const MISSING_VALUE = '\u2014';
/**
 * The next page loads well before its boundary reaches the window, so the
 * "Load more videos" button never sits under the pointer while cards arrive.
 * While a page loads the button is replaced by a status line.
 */
export const LOAD_AHEAD_MARGIN = '2400px 0px';
/**
 * When the boundary is already in the window as a page is requested (a jump with End,
 * PageDown or the scrollbar, or a click on "Load more videos"), the new cards appear where the pointer
 * rests. For this long after they appear, a pointer click on a new card is ignored unless the pointer
 * has moved away first: the click was aimed at the button, not at a card the user has not seen yet.
 * Layout, keyboard and assistive activation are unchanged.
 */
export const INSERTED_CARD_GUARD_MS = 800;
export const INSERTED_CARD_GUARD_MOVE_PX = 8;
export type InsertedCardGuard = { firstIndex: number; until: number; x: number; y: number };

/** True when a pointer click on the card at `index` falls into the guard after new cards appeared. */
export function insertedCardClickBlocked(guard: InsertedCardGuard | null, click: { index: number; now: number; x: number; y: number; detail: number }): boolean {
  if (!guard || click.detail === 0 || click.index < guard.firstIndex || click.now >= guard.until) return false;
  return Math.hypot(click.x - guard.x, click.y - guard.y) < INSERTED_CARD_GUARD_MOVE_PX;
}

/** While the matching ids load, "Select all matching" stays focusable (aria-disabled), so keyboard focus is not lost to the page. */
export function selectAllMatchingButtonState(busy: boolean, unavailable: boolean): { disabled: boolean; 'aria-disabled'?: true } {
  return { disabled: unavailable, ...(busy ? { 'aria-disabled': true as const } : {}) };
}

/** The Inspector's shared-backend setup for this tab (`library` prop): gates and the viewer's zone. */
export type LibraryInspectorSetup = { gates: InspectorGates; timeZone: string };
/** The frozen session the grid shows (its latest reference and identity; `pending` while a new one is built). */
export type LibrarySessionView = { frozenRef: string; sessionKey: string; pending: boolean };
/** Both return a refusal text, or nothing when the workshop opened. */
export type LibraryWorkshopActions = {
  openSelection: () => string | undefined;
  sendSelected: (selected: ReadonlyArray<{ id: string; type?: VideoType }>) => string | undefined;
};

function CatalogueNotice({
  state,
  onRetry,
}: {
  state: LibraryVideosState;
  onRetry: () => void;
}) {
  if (state.kind === 'error' || state.kind === 'incompatible') {
    return (
      <ErrorState
        title={state.kind === 'incompatible'
          ? 'Video catalogue contract is incompatible'
          : 'Video catalogue could not load'}
        message={state.message}
        onRetry={onRetry}
      >
        {state.previous && <small>Showing the last successful result for this exact scope.</small>}
      </ErrorState>
    );
  }
  if (state.kind === 'updating') {
    return <LoadingState label="Updating video catalogue…" compact />;
  }
  return null;
}

/** Nouns of the tab's format for notices and loading texts (Long keeps today's words). */
const FORMAT_NOUNS: Record<VideoType, { list: string; catalogue: string }> = {
  long: { list: 'long-form', catalogue: 'long-form' },
  short: { list: 'Shorts', catalogue: 'Shorts' },
  stream: { list: 'streams', catalogue: 'streams' },
};

/** What the frozen result left out or shows as "—". */
function ScopeNotice({ payload }: { payload: LibraryVideosPayload }) {
  if (sourceExpired(payload.coverage)) return null;
  const notice = payload.coverage.notice;
  const other = notice.other.filter((item) => item.count > 0);
  if (!notice.inventoryIncomplete && !notice.withheldRows && !notice.staleValueRows && !other.length) return null;
  const count = (value: number) => formatCount(value, { compact: false });
  return (
    <div className="explorer-scope-notice explorer-v2-inline-notice is-warning" role="status">
      <AlertTriangle size={18} aria-hidden="true" />
      <div>
        <strong>{notice.inventoryIncomplete ? 'Partial inventory scope' : 'Freshness exclusions apply'}</strong>
        <span>
          {notice.inventoryIncomplete
            ? `The stored ${FORMAT_NOUNS[payload.video_type].list} list is still incomplete.`
            : 'Some source fields are withheld because their collection evidence is stale or missing.'}
          {notice.withheldRows > 0 ? ` ${count(notice.withheldRows)} ${notice.withheldRows === 1 ? 'video row was' : 'video rows were'} withheld.` : ''}
          {notice.staleValueRows > 0 ? ` ${count(notice.staleValueRows)} ${notice.staleValueRows === 1 ? 'video shows' : 'videos show'} — for values that need refresh.` : ''}
          {other.map((item) => ` ${count(item.count)} other ${item.count === 1 ? 'row' : 'rows'} excluded (${item.code}).`).join('')}
        </span>
      </div>
    </div>
  );
}

/** A value the frozen row has but cannot show: "—" with the reason. */
function missingValue(video: ChannelExplorerVideo, field: LibraryCardField): string | undefined {
  const state = (video as Partial<LibraryVideo>).value_states?.[field];
  return state === 'expired' ? 'Needs refresh' : state === 'unavailable' ? 'Unavailable' : undefined;
}

function VideoSelectionToggle({
  video,
  checked,
  onChange,
}: {
  video: ChannelExplorerVideo;
  checked: boolean;
  onChange: (video: ChannelExplorerVideo, checked: boolean) => void;
}) {
  const id = video.video_id?.trim();
  const title = videoTitle(video);
  return (
    <label className="explorer-v2-video-checkbox">
      <input
        type="checkbox"
        data-grid-focus="selection"
        checked={checked}
        disabled={!id}
        onChange={(event) => onChange(video, event.currentTarget.checked)}
        aria-label={`Select ${title}`}
      />
    </label>
  );
}

export function VideoCard({
  video,
  growthAvailable,
  selected,
  onToggleSelected,
  onInspect,
}: {
  video: ChannelExplorerVideo;
  growthAvailable: boolean;
  selected: boolean;
  onToggleSelected: (video: ChannelExplorerVideo, checked: boolean) => void;
  onInspect: (video: ChannelExplorerVideo, trigger: 'thumbnail' | 'title') => void;
}) {
  const title = videoTitle(video);
  const href = youtubeVideoUrl(video.video_id);
  const publishedMissing = missingValue(video, 'published_at');
  const published = publishedMissing ? MISSING_VALUE : formatDateLocal(video.published_at);
  const views = formatCount(video.views);
  const viewsMissing = missingValue(video, 'views');
  const growth = growthAvailable
    ? presentVideoViewGrowth(
      (video as Partial<LibraryVideo>).view_growth,
    )
    : null;
  const durationLabel = hasMetricValue(video.duration)
    ? `Duration ${formatDuration(video.duration)}`
    : 'Duration not reported';

  return (
    <article
      className={`explorer-v2-video-card${selected ? ' is-selected' : ''}`}
      aria-label={title}
      data-video-id={video.video_id}
    >
      <div className="explorer-v2-video-card-media">
        <button
          type="button"
          className="explorer-v2-video-card-thumbnail-button"
          data-inspector-trigger="thumbnail"
          data-grid-focus="thumbnail"
          onClick={() => onInspect(video, 'thumbnail')}
          aria-label={`Inspect ${title}. ${durationLabel}`}
        >
          <VideoThumbnail video={video} />
        </button>
        <VideoSelectionToggle
          video={video}
          checked={selected}
          onChange={onToggleSelected}
        />
      </div>

      <div className="explorer-v2-video-card-body">
        <button
          type="button"
          className="explorer-v2-video-card-title"
          data-inspector-trigger="title"
          data-grid-focus="title"
          onClick={() => onInspect(video, 'title')}
          aria-label={`Inspect ${title}`}
        >
          <strong title={title}>{title}</strong>
        </button>

        <div className="explorer-v2-video-card-meta">
          <span title={publishedMissing ?? published}>{published}</span>
          {href && (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              data-grid-focus="youtube"
              aria-label={`Open ${title} on YouTube`}
            >
              YouTube <ExternalLink size={12} aria-hidden="true" />
            </a>
          )}
        </div>

        <div className="explorer-v2-video-card-views">
          <span className="explorer-v2-video-card-total-views">
            {hasMetricValue(video.views) ? (
              <><strong>{views}</strong>{' total views'}</>
            ) : viewsMissing ? <span title={viewsMissing}><strong>{MISSING_VALUE}</strong>{' total views'}</span> : 'Views not reported'}
          </span>
          {growth && (
            <span
              className={`explorer-v2-video-card-growth is-${growth.tone}`}
              aria-label={`View growth: ${growth.text}`}
              title={growth.title ?? growth.text}
            >
              {growth.text}
            </span>
          )}
        </div>
      </div>
    </article>
  );
}

export function ExplorerVideosPanel({
  state,
  channelId = '',
  videoType = 'long',
  density = 'large',
  growthAvailable,
  selectionScopeKey,
  hasMore = false,
  hasPrevious = false,
  isLoadingMore = false,
  loadMoreError,
  loadMoreErrorDirection = 'next',
  onLoadMore,
  onLoadPrevious,
  onAnchorChange,
  restoreAnchor,
  expired = false,
  onReload,
  onRetry,
  onAddToCollection,
  onCollectMatching,
  session,
  inspector,
  workshop,
  preparing = false,
}: {
  state: LibraryVideosState;
  channelId?: string;
  videoType?: VideoType;
  density?: CatalogueDensity;
  growthAvailable: boolean;
  selectionScopeKey: string;
  hasMore?: boolean;
  hasPrevious?: boolean;
  isLoadingMore?: boolean;
  loadMoreError?: string | null;
  loadMoreErrorDirection?: 'next' | 'previous';
  onLoadMore?: () => void;
  onLoadPrevious?: () => void;
  onAnchorChange?: (anchor: VideoGridAnchor) => void;
  restoreAnchor?: VideoGridRestoreAnchor;
  expired?: boolean;
  onReload?: () => void;
  onRetry: () => void;
  onAddToCollection?: (videoIds: string[]) => void;
  onCollectMatching?: CollectMatching;
  session?: LibrarySessionView;
  inspector?: LibraryInspectorSetup;
  workshop?: LibraryWorkshopActions;
  /** The server is preparing its stored catalogue (`catalogue_preparing`): automatic retries run. */
  preparing?: boolean;
}) {
  const payload = resourcePayload(state);
  const videos = freshSourceRows(payload?.videos ?? []);
  const selectionChannel = channelId || payload?.channel?.channel_id || 'preview';
  const selectionIdentity = useRef({ channel: selectionChannel, scope: selectionScopeKey });
  selectionIdentity.current = { channel: selectionChannel, scope: selectionScopeKey };
  // Selection per format: each format tab keeps and shows its own selection while
  // the grid stays mounted across format tabs; a channel change clears every format.
  const [selectionByType, setSelectionByType] = useState<ReadonlyMap<VideoType, ReadonlyMap<string, ChannelExplorerVideo>>>(
    () => new Map(),
  );
  const selectedVideos = selectionByType.get(videoType) ?? NO_SELECTION;
  const setSelectedVideos = useCallback((next: ReadonlyMap<string, ChannelExplorerVideo>
    | ((current: ReadonlyMap<string, ChannelExplorerVideo>) => ReadonlyMap<string, ChannelExplorerVideo>)) => {
    setSelectionByType((store) => {
      const current = store.get(videoType) ?? NO_SELECTION;
      const value = typeof next === 'function' ? next(current) : next;
      if (value === current) return store;
      const updated = new Map(store);
      updated.set(videoType, value);
      return updated;
    });
  }, [videoType]);
  const [inspectedVideo, setInspectedVideo] = useState<ChannelExplorerVideo | null>(null);
  useSourceExpiry(inspectedVideo, () => setInspectedVideo(null));
  useSourceExpiry(selectedVideos, () => setSelectedVideos((previous) => new Map([...previous].map(([id, video]) =>
    [id, sourceExpired(video) ? { video_id: id, video_type: video.video_type } : video]))));

  // The session the Inspector was opened in: its panels stay active only while it is the grid's session.
  const [inspectorSession, setInspectorSession] = useState<string>();
  const [selectionProgress, setSelectionProgress] = useState<SelectionProgress | null>(null);
  const [selectionMessage, setSelectionMessage] = useState('');
  const selectionRequest = useRef<AbortController | null>(null);
  const gridRef = useRef<VirtualVideoGridHandle>(null);
  const feedbackAnchor = useRef<VideoGridAnchor | null>(null);
  const [feedbackRevision, setFeedbackRevision] = useState(0);
  const keepSelectionViewport = useCallback(() => {
    feedbackAnchor.current ??= gridRef.current?.captureAnchor() ?? null;
    // Complete the anchor cycle even when repeated feedback has the same text.
    setFeedbackRevision((revision) => revision + 1);
  }, []);
  useLayoutEffect(() => {
    const anchor = feedbackAnchor.current;
    feedbackAnchor.current = null;
    if (anchor) gridRef.current?.restore(anchor);
  }, [feedbackRevision]);
  const nextBoundaryRef = useRef<HTMLDivElement>(null);
  const previousBoundaryRef = useRef<HTMLDivElement>(null);
  const selectAllRef = useRef<HTMLButtonElement>(null);
  // The pointer's last position, a next page requested while its boundary was in the window, and the guard.
  const pointer = useRef({ x: -1, y: -1 });
  const visibleRequest = useRef<number | null>(null);
  const insertGuard = useRef<InsertedCardGuard | null>(null);
  const scopeRef = useRef(selectionScopeKey);
  scopeRef.current = selectionScopeKey;
  const inspectorReturnRef = useRef<{
    scope: string; anchor: VideoGridAnchor | null; videoId: string; trigger: 'thumbnail' | 'title';
  } | null>(null);
  const loadedVideoIds = useMemo(
    () => videos
      .map((video) => video.video_id?.trim())
      .filter((videoId): videoId is string => Boolean(videoId)),
    [videos],
  );
  const allLoadedSelected = loadedVideoIds.length > 0
    && loadedVideoIds.every((videoId) => selectedVideos.has(videoId));

  useEffect(() => {
    setSelectionByType(new Map());
    setInspectedVideo(null);
    setInspectorSession(undefined);
    inspectorReturnRef.current = null;
  }, [selectionChannel]);
  // The Inspector shows a video of the format it was opened in: another format tab closes it.
  useEffect(() => {
    setInspectedVideo(null);
    setInspectorSession(undefined);
    inspectorReturnRef.current = null;
  }, [videoType]);

  useEffect(() => {
    selectionRequest.current?.abort();
    selectionRequest.current = null;
    setSelectionProgress(null);
    setSelectionMessage('');
    return () => { selectionRequest.current?.abort(); selectionRequest.current = null; };
  }, [selectionChannel, selectionScopeKey]);

  const cancelSelection = useCallback(() => {
    keepSelectionViewport();
    selectionRequest.current?.abort();
    selectionRequest.current = null;
    setSelectionProgress(null);
  }, [keepSelectionViewport]);

  const selectAllMatching = useCallback(async () => {
    if (!onCollectMatching || selectionRequest.current) return;
    keepSelectionViewport();
    const controller = new AbortController();
    const identity = selectionIdentity.current;
    const stillCurrent = () => selectionRequest.current === controller && !controller.signal.aborted
      && selectionIdentity.current.channel === identity.channel && selectionIdentity.current.scope === identity.scope;
    selectionRequest.current = controller;
    setSelectionMessage('');
    setSelectionProgress({ received: 0, total: Number(payload?.pagination?.matched_count) || 0 });
    try {
      const records = await onCollectMatching(controller.signal, (progress) => {
        if (stillCurrent()) { keepSelectionViewport(); setSelectionProgress(progress); }
      });
      if (!stillCurrent()) return;
      keepSelectionViewport();
      setSelectedVideos((current) => addSelectedRecords(current, records));
      setSelectionMessage(`All ${records.length.toLocaleString('en')} matching videos are included in the selection.`);
    } catch (error) {
      if (!stillCurrent()) return;
      keepSelectionViewport();
      setSelectionMessage(error instanceof Error && error.name === 'AbortError'
        ? 'Selection cancelled. Existing selection is kept.'
        : error instanceof Error ? error.message : 'Selection failed. Existing selection is kept.');
    } finally {
      if (selectionRequest.current === controller) { keepSelectionViewport(); selectionRequest.current = null; setSelectionProgress(null); }
    }
  }, [onCollectMatching, payload, keepSelectionViewport, setSelectedVideos]);

  // When the progress controls go, keyboard focus that fell to the page returns to "Select all matching".
  const selectionRan = useRef(false);
  useLayoutEffect(() => {
    if (selectionProgress) { selectionRan.current = true; return; }
    if (!selectionRan.current) return;
    selectionRan.current = false;
    const active = typeof document === 'undefined' ? null : document.activeElement;
    if (!active || active === document.body) selectAllRef.current?.focus({ preventScroll: true });
  }, [selectionProgress]);

  // Remember where the pointer is (no layout reads), and note a next page requested while its boundary shows.
  useEffect(() => {
    const move = (event: PointerEvent) => { pointer.current = { x: event.clientX, y: event.clientY }; };
    window.addEventListener('pointermove', move, { passive: true });
    return () => window.removeEventListener('pointermove', move);
  }, []);
  useLayoutEffect(() => {
    if (!isLoadingMore) return;
    const boundary = nextBoundaryRef.current?.getBoundingClientRect();
    visibleRequest.current = boundary && boundary.top < window.innerHeight && boundary.bottom > 0 ? videos.length : null;
  }, [isLoadingMore]);
  useLayoutEffect(() => {
    const firstIndex = visibleRequest.current;
    if (firstIndex === null || isLoadingMore) return;
    visibleRequest.current = null;
    insertGuard.current = videos.length > firstIndex
      ? { firstIndex, until: performance.now() + INSERTED_CARD_GUARD_MS, ...pointer.current } : null;
  }, [isLoadingMore, videos.length]);
  const guardInsertedCards = useCallback((event: ReactMouseEvent<HTMLElement>) => {
    const guard = insertGuard.current;
    if (!guard) return;
    const card = (event.target as HTMLElement).closest<HTMLElement>('[data-video-id]');
    const index = card ? loadedVideoIds.indexOf(card.dataset.videoId ?? '') : -1;
    if (insertedCardClickBlocked(guard, { index, now: performance.now(), x: event.clientX, y: event.clientY, detail: event.detail })) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, [loadedVideoIds]);

  const autoLoading = typeof IntersectionObserver !== 'undefined' && !loadMoreError && !expired && !inspectedVideo
    && !selectionProgress && state.kind === 'ready';
  useEffect(() => {
    if (!autoLoading || isLoadingMore) return;
    // Latch also covers two boundaries intersecting before parent state commits.
    let requested = false;
    const observer = new IntersectionObserver((entries) => {
      if (requested || !entries.some((entry) => entry.isIntersecting)) return;
      const previous = entries.find((entry) => entry.isIntersecting && entry.target === previousBoundaryRef.current);
      const next = entries.find((entry) => entry.isIntersecting && entry.target === nextBoundaryRef.current);
      if (previous && hasPrevious && onLoadPrevious) {
        requested = true;
        onLoadPrevious();
      } else if (next && hasMore && onLoadMore) {
        requested = true;
        onLoadMore();
      }
    }, { rootMargin: LOAD_AHEAD_MARGIN });
    if (hasPrevious && previousBoundaryRef.current) observer.observe(previousBoundaryRef.current);
    if (hasMore && nextBoundaryRef.current) observer.observe(nextBoundaryRef.current);
    return () => observer.disconnect();
  }, [autoLoading, hasMore, hasPrevious, isLoadingMore, videos.length, onLoadMore, onLoadPrevious]);

  const toggleSelected = useCallback((video: ChannelExplorerVideo, checked: boolean) => {
    const videoId = video.video_id?.trim();
    if (!videoId) return;
    cancelSelection();
    setSelectedVideos((current) => {
      const next = checked ? addSelectedRecords(current, [video]) : new Map(current);
      if (!checked) next.delete(videoId);
      return next;
    });
  }, [cancelSelection, setSelectedVideos]);

  const toggleLoadedSelection = useCallback(() => {
    cancelSelection();
    setSelectedVideos((current) => {
      const next = new Map(current);
      if (allLoadedSelected) {
        loadedVideoIds.forEach((videoId) => next.delete(videoId));
      } else {
        return addSelectedRecords(current, videos);
      }
      return next;
    });
  }, [allLoadedSelected, loadedVideoIds, videos, cancelSelection, setSelectedVideos]);

  const closeInspector = useCallback(() => setInspectedVideo(null), []);
  const inspectVideo = useCallback((video: ChannelExplorerVideo, trigger: 'thumbnail' | 'title') => {
    inspectorReturnRef.current = {
      scope: scopeRef.current, anchor: gridRef.current?.captureAnchor() ?? null,
      videoId: video.video_id || '', trigger,
    };
    setInspectedVideo(sourceExpired(video) ? null : video);
    setInspectorSession(session?.sessionKey);
  }, [session?.sessionKey]);
  const restoreInspectorFocus = useCallback(() => {
    const saved = inspectorReturnRef.current;
    if (saved && saved.scope === scopeRef.current) {
      gridRef.current?.restore(saved.anchor, { videoId: saved.videoId, trigger: saved.trigger });
    }
  }, []);
  const workshopFeedback = (message: string | undefined) => {
    keepSelectionViewport();
    setSelectionMessage(message ?? '');
  };
  const loadingLabel = preparing ? LIBRARY_ERROR_TEXT.preparing : `Loading stored ${FORMAT_NOUNS[videoType].catalogue} catalogue…`;

  return (
    <section
      className="explorer-v2-section explorer-v2-video-catalogue"
      aria-label="Video catalogue"
      onClickCapture={guardInsertedCards}
    >

      {(state.kind === 'idle' || state.kind === 'loading') && !payload && (
        <LoadingState label={loadingLabel} />
      )}
      <CatalogueNotice state={state} onRetry={onRetry} />

      {(payload || selectedVideos.size > 0) && (
          <div className="explorer-v2-video-selection-bar" aria-live="polite">
            <span>{payload && <><strong>{formatCount(sourceOwnExpired(payload) ? videos.length : payload.pagination?.matched_count ?? videos.length, { compact: false })}</strong> {sourceOwnExpired(payload) ? 'visible videos' : 'videos'}</>}</span>
            <div className="explorer-v2-video-selection-actions">
              <button
                type="button"
                className="explorer-secondary-button"
                disabled={loadedVideoIds.length === 0}
                onClick={toggleLoadedSelection}
              >
                {allLoadedSelected ? 'Unselect loaded' : 'Select loaded'}
              </button>
              {onCollectMatching && <button ref={selectAllRef} type="button" className="explorer-secondary-button"
                {...selectAllMatchingButtonState(Boolean(selectionProgress), expired || state.kind !== 'ready' || !videos.length)}
                onClick={() => { if (!selectionProgress) void selectAllMatching(); }}>Select all matching</button>}
              {workshop && payload && <button type="button" className="explorer-secondary-button"
                disabled={state.kind !== 'ready'}
                onClick={() => workshopFeedback(workshop.openSelection())}>Open in research workshop</button>}
              {selectionProgress && <>
                <span>{selectionProgress.received.toLocaleString('en')} / {selectionProgress.total.toLocaleString('en')}</span>
                <button type="button" className="explorer-secondary-button" onClick={() => {
                  cancelSelection(); setSelectionMessage('Selection cancelled. Existing selection is kept.');
                  selectAllRef.current?.focus({ preventScroll: true });
                }}>Cancel selection</button>
              </>}
              {selectedVideos.size > 0 && (
                <>
                  <span className="explorer-v2-video-selected-count">
                    {selectedVideos.size} selected
                  </span>
                  {workshop && <button type="button" className="explorer-secondary-button" disabled={Boolean(selectionProgress)}
                    onClick={() => workshopFeedback(workshop.sendSelected([...selectedVideos.values()].map((video) => ({
                      id: video.video_id!, ...(video.video_type ? { type: video.video_type } : {}),
                    }))))}>Send selected to workshop</button>}
                  {onAddToCollection && <button type="button" className="explorer-secondary-button" onClick={() => onAddToCollection([...selectedVideos.keys()])}>Add to collection</button>}
                  <button
                    type="button"
                    className="explorer-secondary-button"
                    onClick={() => { cancelSelection(); setSelectedVideos(new Map()); setSelectionMessage(''); }}
                  >
                    Clear
                  </button>
                </>
              )}
            </div>
          </div>
      )}
      {selectionMessage && <p className="saved-view-confirmation" role="status">{selectionMessage}</p>}
      {payload && (
        <>
          {!sourceOwnExpired(payload) && <ScopeNotice payload={payload} />}
          {expired && (
            <div className="explorer-v2-catalogue-load-boundary" role="status">
              <span>This browsing session expired. Loaded videos and selection are kept.</span>
              <button type="button" className="explorer-secondary-button" onClick={onReload ?? onRetry}>Reload catalogue</button>
            </div>
          )}
          <div ref={previousBoundaryRef} className="explorer-v2-catalogue-load-boundary">
            {loadMoreError && loadMoreErrorDirection === 'previous' && <span role="status">{loadMoreError}</span>}
            {hasPrevious && !expired && !isLoadingMore && (
              <button type="button" className="explorer-secondary-button" disabled={state.kind !== 'ready'} onClick={onLoadPrevious}>
                {loadMoreError && loadMoreErrorDirection === 'previous' ? 'Retry previous videos' : 'Load previous videos'}
              </button>
            )}
          </div>
          {videos.length > 0 ? (
            <VirtualVideoGrid
              ref={gridRef}
              videos={videos}
              density={density}
              scopeKey={selectionScopeKey}
              inspectedVideoId={inspectedVideo?.video_id || undefined}
              onAnchorChange={onAnchorChange}
              restoreAnchor={restoreAnchor}
              renderCard={(video) => (
                  <VideoCard
                    key={video.video_id || video.title}
                    video={video}
                    growthAvailable={growthAvailable}
                    selected={selectedVideos.has(video.video_id?.trim() || '')}
                    onToggleSelected={toggleSelected}
                    onInspect={inspectVideo}
                  />
              )}
            />
          ) : (
            <div className="explorer-empty-copy explorer-v2-empty-row" role="status">
              No stored videos match the applied filters.
            </div>
          )}
          <div ref={nextBoundaryRef} className="explorer-v2-catalogue-load-boundary" aria-live="polite">
            {isLoadingMore && <span>Loading videos…</span>}
            {loadMoreError && loadMoreErrorDirection === 'next' && <span role="status">{loadMoreError}</span>}
            {hasMore && !expired && !isLoadingMore && (
              <button type="button" className="explorer-secondary-button" disabled={state.kind !== 'ready'} onClick={onLoadMore}>
                {loadMoreError && loadMoreErrorDirection === 'next' ? 'Retry loading videos' : 'Load more videos'}
              </button>
            )}
          </div>
        </>
      )}
      {inspectedVideo && (
        <VideoInspector video={inspectedVideo}
          growth={growthAvailable ? (inspectedVideo as Partial<LibraryVideo>).view_growth : undefined}
          library={inspector && inspectorSession && channelId && session ? {
            gates: inspector.gates,
            opening: {
              channelId, videoType, frozenRef: session.frozenRef, sessionKey: inspectorSession,
              checkedAt: typeof inspectedVideo.checked_at === 'string' ? inspectedVideo.checked_at : null,
              active: inspectorSession === session.sessionKey && !expired,
              pending: session.pending, timeZone: inspector.timeZone,
            },
          } : undefined}
          onClose={closeInspector} onRestoreFocus={restoreInspectorFocus} />
      )}
    </section>
  );
}
