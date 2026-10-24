import { useSourceExpiry } from '../sourceLifetimeReact';
import { freshSourceRows, sourceExpired, sourceOwnExpired } from '../sourceLifetime.ts';
import {
  AlertTriangle,
  ExternalLink,
  Film,
  X,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type {
  ChannelExplorerPayload,
  ChannelExplorerVideo,
  ChannelVideoGrowthVideo,
  ChannelVideoViewGrowth,
} from '../types';
import { numberValue } from '../format';
import {
  formatCount,
  formatDuration,
  hasMetricValue,
  youtubeVideoUrl,
} from './format';
import { presentVideoViewGrowth } from './videoGrowth';
import { presentVideoInspectorGrowth } from './videoInspectorGrowth';
import { formatDateLocal, formatTimestampLocal } from './localTime';
import { formatVideoCategory, safeVideoThumbnailUrl } from './videoPresentation';
import type {
  VideoCatalogueState,
} from './model';
import { ErrorState, LoadingState } from './ModuleState';
import { resourcePayload } from './resourceState';
import type { CatalogueDensity } from './cataloguePreferences';
import {
  VirtualVideoGrid, type VideoGridRestoreAnchor, type VirtualVideoGridHandle,
} from './VirtualVideoGrid';
import type { VideoGridAnchor } from './videoGridGeometry';
import { addSelectedRecords, type CollectMatching, type SelectionProgress } from './catalogueSelection';
import { VideoStaticDetailsPanel, type StaticInspectorContext } from './VideoStaticDetailsPanel';
import { VideoHistoryPanel, type HistoryInspectorContext } from './VideoHistoryPanel';
import { VideoLifetimeBaselinePanel } from './VideoLifetimeBaselinePanel';
import { VideoLaunchPanel } from './VideoLaunchPanel';

function CatalogueNotice({
  state,
  onRetry,
}: {
  state: VideoCatalogueState;
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

function ScopeNotice({ payload }: { payload: ChannelExplorerPayload }) {
  if (sourceExpired(payload.coverage)) return null;
  const truncated = payload.coverage?.truncated;
  const withheld = numberValue(payload.coverage?.video_rows_withheld_stale);
  const stale = payload.coverage?.channel_data_fresh === false || withheld > 0;
  if (!truncated && !stale) return null;
  return (
    <div className="explorer-scope-notice explorer-v2-inline-notice is-warning" role="status">
      <AlertTriangle size={18} aria-hidden="true" />
      <div>
        <strong>{truncated ? 'Partial inventory scope' : 'Freshness exclusions apply'}</strong>
        <span>
          {truncated
            ? `The first ${formatCount(payload.coverage?.scan_limit, { compact: false })} typed long-form IDs were scanned.`
            : 'Some source fields are withheld because their collection evidence is stale or missing.'}
          {withheld > 0
            ? ` ${formatCount(withheld, { compact: false })} video rows were withheld.`
            : ''}
        </span>
      </div>
    </div>
  );
}

function videoTitle(video: ChannelExplorerVideo): string {
  return video.title?.trim() || 'Title not reported';
}

function VideoThumbnail({ video }: { video: ChannelExplorerVideo }) {
  const thumbnailUrl = safeVideoThumbnailUrl(video.thumbnail_url);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [thumbnailUrl]);
  const duration = formatDuration(video.duration);
  return (
    <span className="explorer-v2-video-thumbnail">
      {thumbnailUrl && !failed ? (
        <img
          src={thumbnailUrl}
          alt=""
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      ) : (
        <span
          className="explorer-v2-video-thumbnail-fallback"
          role="img"
          aria-label="Thumbnail unavailable"
        >
          <Film size={21} aria-hidden="true" />
          <small>Thumbnail unavailable</small>
        </span>
      )}
      <span
        className={`explorer-v2-video-duration${hasMetricValue(video.duration) ? '' : ' is-missing'}`}
        title={hasMetricValue(video.duration) ? `Duration ${duration}` : 'Duration not reported'}
      >
        {duration}
      </span>
    </span>
  );
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
  const published = formatDateLocal(video.published_at);
  const views = formatCount(video.views);
  const growth = growthAvailable
    ? presentVideoViewGrowth(
      (video as Partial<ChannelVideoGrowthVideo>).view_growth,
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
          <span title={published}>{published}</span>
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
            ) : 'Views not reported'}
          </span>
          {growth && (
            <span
              className={`explorer-v2-video-card-growth is-${growth.tone}`}
              aria-label={`View growth: ${growth.text}`}
              title={growth.text}
            >
              {growth.text}
            </span>
          )}
        </div>
      </div>
    </article>
  );
}

export function VideoInspector({
  video,
  growth,
  staticDetails,
  launchContext,
  history,
  onClose,
  onRestoreFocus,
}: {
  video: ChannelExplorerVideo;
  growth?: ChannelVideoViewGrowth;
  staticDetails?: StaticInspectorContext;
  launchContext?: StaticInspectorContext;
  history?: HistoryInspectorContext;
  onClose: () => void;
  onRestoreFocus: () => void;
}) {
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const href = youtubeVideoUrl(video.video_id);
  const observation = growth ? presentVideoInspectorGrowth(growth) : null;

  useEffect(() => {
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeButtonRef.current?.focus({ preventScroll: true });
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(
        drawerRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousBodyOverflow;
      // StrictMode's effect replay leaves the dialog connected; only return
      // focus after a real close, once body scrolling has been unlocked.
      queueMicrotask(() => {
        if (!drawerRef.current?.isConnected) onRestoreFocus();
      });
    };
  }, [onClose, onRestoreFocus]);

  return (
    <div
      className="explorer-v2-video-drawer-backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside
        ref={drawerRef}
        className="explorer-v2-video-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="explorer-v2-video-drawer-title"
      >
        <header>
          <div>
            <span className="explorer-eyebrow">Current raw record</span>
            <h2 id="explorer-v2-video-drawer-title">{videoTitle(video)}</h2>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="explorer-v2-video-drawer-close"
            onClick={onClose}
            aria-label="Close video inspector"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <VideoThumbnail video={video} />

        <dl className="explorer-v2-video-drawer-metrics">
          <div><dt>Views</dt><dd>{formatCount(video.views, { compact: false })}</dd></div>
          <div><dt>Likes</dt><dd>{formatCount(video.like_count, { compact: false })}</dd></div>
          <div><dt>Comments</dt><dd>{formatCount(video.comment_count, { compact: false })}</dd></div>
          <div><dt>Duration</dt><dd>{formatDuration(video.duration)}</dd></div>
        </dl>

        {staticDetails && video.video_id && <VideoLifetimeBaselinePanel {...staticDetails} videoId={video.video_id} />}
        {launchContext && video.video_id && <VideoLaunchPanel {...launchContext} videoId={video.video_id} />}

        {observation && <section className="explorer-v2-video-observation" aria-labelledby="explorer-v2-video-observation-title">
          <header><h3 id="explorer-v2-video-observation-title">Observed views</h3><span>Requested window: 28 days</span></header>
          <p className="explorer-v2-video-observation-status">{observation.status}</p>
          {observation.delta !== null && <p className="explorer-v2-video-observation-delta">{observation.delta}</p>}
          <dl className="explorer-v2-video-drawer-details">
            {observation.average !== null && <div><dt title="Observed change divided by the actual interval, not the latest growth rate.">Average views/day</dt><dd>{observation.average}</dd></div>}
            <div><dt>Observed from</dt><dd>{observation.start}</dd></div>
            <div><dt>Observed to <span className="explorer-v2-video-observation-end-label">Latest observation</span></dt><dd>{observation.end}</dd></div>
            <div><dt>Actual interval</dt><dd>{observation.interval}</dd></div>
          </dl>
        </section>}

        {history && video.video_id && <VideoHistoryPanel {...history} videoId={video.video_id} />}

        <dl className="explorer-v2-video-drawer-details">
          <div><dt>Published</dt><dd>{formatDateLocal(video.published_at)}</dd></div>
          <div><dt>Category</dt><dd>{formatVideoCategory(video.category_name)}</dd></div>
          <div><dt>Counters checked</dt><dd>{formatTimestampLocal(video.checked_at)}</dd></div>
          <div><dt>Video ID</dt><dd>{video.video_id || 'Not reported'}</dd></div>
        </dl>

        {staticDetails && video.video_id && <VideoStaticDetailsPanel {...staticDetails} videoId={video.video_id} />}

        <div className="explorer-v2-video-description-unavailable">
          <strong>Description unavailable</strong>
          <p>
            Description is not exposed by the current raw Explorer contract yet.
            This view keeps that data boundary visible instead of inventing content.
          </p>
        </div>

        {href && (
          <a
            className="explorer-primary-button explorer-v2-video-drawer-link"
            href={href}
            target="_blank"
            rel="noreferrer"
          >
            Open on YouTube <ExternalLink size={15} aria-hidden="true" />
          </a>
        )}
        <small className="explorer-v2-video-drawer-source">
          Source: stored public long-form Explorer fields.
          {observation && ' Observed views: Leti Cook calculation from stored snapshots.'}
          {' No owner-authorized analytics are mixed into this view.'}
        </small>
      </aside>
    </div>
  );
}

export function ExplorerVideosPanel({
  state,
  channelId = '',
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
  browseSessionId,
  staticDetailsAvailable = false,
  historyAvailable = false,
  launchAvailable = false,
}: {
  state: VideoCatalogueState;
  channelId?: string;
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
  /** Kept optional while isolated fixtures transition to the browse contract. */
  offset?: number;
  onPrevious?: () => void;
  onNext?: () => void;
  onRetry: () => void;
  onAddToCollection?: (videoIds: string[]) => void;
  onCollectMatching?: CollectMatching;
  browseSessionId?: string;
  staticDetailsAvailable?: boolean;
  historyAvailable?: boolean;
  launchAvailable?: boolean;
}) {
  const payload = resourcePayload(state);
  const videos = freshSourceRows(payload?.videos ?? []);
  const selectionChannel = channelId || payload?.channel?.channel_id || 'preview';
  const selectionIdentity = useRef({ channel: selectionChannel, scope: selectionScopeKey });
  selectionIdentity.current = { channel: selectionChannel, scope: selectionScopeKey };
  const [selectedVideos, setSelectedVideos] = useState<Map<string, ChannelExplorerVideo>>(
    () => new Map(),
  );
  const [inspectedVideo, setInspectedVideo] = useState<ChannelExplorerVideo | null>(null);
  useSourceExpiry(inspectedVideo, () => setInspectedVideo(null));
  useSourceExpiry(selectedVideos, () => setSelectedVideos((previous) => new Map([...previous].map(([id, video]) =>
    [id, sourceExpired(video) ? { video_id: id } : video]))));

  const [inspectorSession, setInspectorSession] = useState<string>();
  const [inspectorHistorySession, setInspectorHistorySession] = useState<string>();
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
    setSelectedVideos(new Map());
    setInspectedVideo(null);
    setInspectorSession(undefined);
    setInspectorHistorySession(undefined);
    inspectorReturnRef.current = null;
  }, [selectionChannel]);

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
  }, [onCollectMatching, payload, keepSelectionViewport]);

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined' || isLoadingMore || loadMoreError || expired || inspectedVideo || selectionProgress || state.kind !== 'ready') return;
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
    }, { rootMargin: '600px 0px' });
    if (hasPrevious && previousBoundaryRef.current) observer.observe(previousBoundaryRef.current);
    if (hasMore && nextBoundaryRef.current) observer.observe(nextBoundaryRef.current);
    return () => observer.disconnect();
  }, [hasMore, hasPrevious, isLoadingMore, loadMoreError, expired, inspectedVideo, selectionProgress, state.kind, videos.length, onLoadMore, onLoadPrevious]);

  const toggleSelected = useCallback((video: ChannelExplorerVideo, checked: boolean) => {
    const videoId = video.video_id?.trim();
    if (!videoId) return;
    cancelSelection();
    setSelectedVideos((current) => {
      const next = checked ? addSelectedRecords(current, [video]) : new Map(current);
      if (!checked) next.delete(videoId);
      return next;
    });
  }, [cancelSelection]);

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
  }, [allLoadedSelected, loadedVideoIds, videos, cancelSelection]);

  const closeInspector = useCallback(() => setInspectedVideo(null), []);
  const inspectVideo = useCallback((video: ChannelExplorerVideo, trigger: 'thumbnail' | 'title') => {
    inspectorReturnRef.current = {
      scope: scopeRef.current, anchor: gridRef.current?.captureAnchor() ?? null,
      videoId: video.video_id || '', trigger,
    };
    setInspectedVideo(sourceExpired(video) ? null : video);
    setInspectorSession(browseSessionId);
    setInspectorHistorySession(historyAvailable && growthAvailable && payload?.source?.kind === 'derived_video_view_growth_from_stored_snapshots'
      ? browseSessionId : undefined);
  }, [browseSessionId, historyAvailable, growthAvailable, payload]);
  const restoreInspectorFocus = useCallback(() => {
    const saved = inspectorReturnRef.current;
    if (saved && saved.scope === scopeRef.current) {
      gridRef.current?.restore(saved.anchor, { videoId: saved.videoId, trigger: saved.trigger });
    }
  }, []);

  return (
    <section
      className="explorer-v2-section explorer-v2-video-catalogue"
      aria-label="Video catalogue"
    >

      {(state.kind === 'idle' || state.kind === 'loading') && !payload && (
        <LoadingState label="Loading stored long-form catalogue…" />
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
              {onCollectMatching && <button type="button" className="explorer-secondary-button"
                disabled={Boolean(selectionProgress) || expired || state.kind !== 'ready' || !videos.length}
                onClick={() => { void selectAllMatching(); }}>Select all matching</button>}
              {selectionProgress && <>
                <span>{selectionProgress.received.toLocaleString('en')} / {selectionProgress.total.toLocaleString('en')}</span>
                <button type="button" className="explorer-secondary-button" onClick={() => {
                  cancelSelection(); setSelectionMessage('Selection cancelled. Existing selection is kept.');
                }}>Cancel selection</button>
              </>}
              {selectedVideos.size > 0 && (
                <>
                  <span className="explorer-v2-video-selected-count">
                    {selectedVideos.size} selected
                  </span>
                  {/* Not in this release: marked like the Shorts and Streams tabs. */}
                  <button type="button" className="explorer-secondary-button" disabled>Compare <small>coming soon</small></button>
                  <button type="button" className="explorer-secondary-button" disabled>Send to Canvas <small>coming soon</small></button>
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
            {hasPrevious && !expired && (
              <button type="button" className="explorer-secondary-button" disabled={isLoadingMore || state.kind !== 'ready'} onClick={onLoadPrevious}>
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
            {hasMore && !expired && (
              <button type="button" className="explorer-secondary-button" disabled={isLoadingMore || state.kind !== 'ready'} onClick={onLoadMore}>
                {loadMoreError && loadMoreErrorDirection === 'next' ? 'Retry loading videos' : 'Load more videos'}
              </button>
            )}
          </div>
        </>
      )}
      {inspectedVideo && (
        <VideoInspector video={inspectedVideo}
          growth={growthAvailable ? (inspectedVideo as Partial<ChannelVideoGrowthVideo>).view_growth : undefined}
          staticDetails={staticDetailsAvailable && inspectorSession && channelId ? {
            channelId, browseSession: inspectorSession, active: inspectorSession === browseSessionId && !expired,
          } : undefined}
          launchContext={launchAvailable && inspectorSession && channelId ? {
            channelId, browseSession: inspectorSession, active: inspectorSession === browseSessionId && !expired,
          } : undefined}
          history={historyAvailable && inspectorHistorySession && channelId ? {
            channelId, browseSession: inspectorHistorySession, checkedAt: typeof inspectedVideo.checked_at === 'string' ? inspectedVideo.checked_at : null,
            active: inspectorHistorySession === browseSessionId && growthAvailable && !expired,
          } : undefined}
          onClose={closeInspector} onRestoreFocus={restoreInspectorFocus} />
      )}
    </section>
  );
}
