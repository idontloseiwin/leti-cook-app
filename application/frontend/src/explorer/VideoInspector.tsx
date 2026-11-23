import { useEffect, useRef, useState } from 'react';
import { ExternalLink, X } from 'lucide-react';
import type {
  ChannelExplorerVideo,
  ChannelVideoViewGrowth,
  VideoType,
} from '../types';
import { sourceExpired } from '../sourceLifetime.ts';
import { useSourceExpiry } from '../sourceLifetimeReact';
import { requestJson } from './api';
import {
  formatCount,
  formatDuration,
  youtubeVideoUrl,
} from './format';
import { presentVideoInspectorGrowth } from './videoHistory';
import { formatDateLocal, formatTimestampLocal } from './localTime';
import { formatVideoCategory } from './videoPresentation';
import { VideoThumbnail, videoTitle } from './VideoThumbnail';
import { VideoStaticDetailsPanel } from './VideoStaticDetailsPanel';
import { VideoHistoryPanel } from './VideoHistoryPanel';
import { VideoLifetimeBaselinePanel } from './VideoLifetimeBaselinePanel';
import { VideoLaunchPanel } from './VideoLaunchPanel';
import { launchTypeNoun } from './videoLaunch';
import {
  createInspectorClient,
  inspectorSources,
  type InspectorClient,
  type InspectorGates,
  type InspectorGrowth,
  type InspectorOpening,
  type InspectorPanelSource,
  type InspectorSources,
} from './libraryData/inspector.ts';

// Video Inspector drawer: one video of the Library grid or a Saved collection.

/** The shared backend's Inspector reads: one opening, the server's gates. */
export type LibraryInspectorProps = {
  /** A frozen Library page (its `frozen_ref`, session key, row clock) or a query opening. */
  opening: InspectorOpening;
  gates: InspectorGates;
  /** The page uses the shared request function; tests may pass their own. */
  client?: InspectorClient;
};

const libraryClient = createInspectorClient((path, body, signal) => requestJson(path, signal, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
}));

type GrowthState = { key: string; kind: 'loading' | 'quiet' | 'error'; growth?: undefined }
  | { key: string; kind: 'ready'; growth: InspectorGrowth };

/** Without a frozen row (query opening): the one video's growth evidence. */
function useInspectorGrowth(source: InspectorPanelSource<InspectorGrowth> | undefined) {
  const key = source?.key ?? '', active = source?.active ?? false, pending = source?.pending ?? false;
  const reader = useRef(source);
  reader.current = source;
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<GrowthState>({ key, kind: 'loading' });
  useSourceExpiry(state, () => setState({ key, kind: 'loading' }), () => { if (active) setRetry((value) => value + 1); });
  useEffect(() => {
    if (!reader.current || !active || pending) return;
    const controller = new AbortController();
    setState({ key, kind: 'loading' });
    reader.current.load(controller.signal).then((growth) => {
      if (!controller.signal.aborted) setState({ key, kind: 'ready', growth });
    }).catch((error) => {
      const failure = reader.current?.failure(error);
      // Unavailable or inactive: the section stays hidden, as for a row without growth.
      if (!controller.signal.aborted && failure) setState({ key, kind: failure.kind === 'error' ? 'error' : 'quiet' });
    });
    return () => { controller.abort(); };
  }, [key, active, pending, retry]);
  const current = state.key === key ? state : null;
  return {
    growth: current?.kind === 'ready' && !sourceExpired(current.growth) ? current.growth : undefined,
    failed: Boolean(source) && current?.kind === 'error',
    retry: () => setRetry((value) => value + 1),
  };
}

const SOURCE_NOUNS: Record<VideoType, string> = { long: 'long-form', short: 'Shorts', stream: 'stream' };

export function VideoInspector({
  video,
  growth,
  library,
  onClose,
  onRestoreFocus,
}: {
  video: ChannelExplorerVideo;
  /** The opened row's growth; `time_zone` (from the shared evidence) labels "today". */
  growth?: ChannelVideoViewGrowth & { time_zone?: string };
  /** Shared-backend panels; without them (a saved collection) the drawer shows the base record only. */
  library?: LibraryInspectorProps;
  onClose: () => void;
  onRestoreFocus: () => void;
}) {
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const href = youtubeVideoUrl(video.video_id);
  const videoType: VideoType = library?.opening.videoType ?? video.video_type ?? 'long';
  const sources: InspectorSources = video.video_id && library
    ? inspectorSources(library.opening, video.video_id, library.gates, library.client ?? libraryClient)
    : { description: false };
  const queried = useInspectorGrowth(growth ? undefined : sources.growth);
  const shownGrowth = growth ?? queried.growth;
  const observation = shownGrowth ? presentVideoInspectorGrowth(shownGrowth) : null;
  const checkedAt = library?.opening.checkedAt ?? null;
  const descriptionOff = Boolean(library?.gates.staticDetails && !library.gates.description);

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

        {sources.baseline && <VideoLifetimeBaselinePanel source={sources.baseline} />}
        {sources.launch && <VideoLaunchPanel source={sources.launch} />}

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
        {!observation && queried.failed && <section className="explorer-v2-video-observation" aria-label="Observed views">
          <header><h3>Observed views</h3><span>Requested window: 28 days</span></header>
          <p role="status">Observed views could not load.</p>
          <button type="button" className="explorer-secondary-button" onClick={queried.retry}>Retry observed views</button>
        </section>}

        {sources.history && <VideoHistoryPanel source={sources.history} checkedAt={checkedAt} />}

        <dl className="explorer-v2-video-drawer-details">
          <div><dt>Published</dt><dd>{formatDateLocal(video.published_at)}</dd></div>
          <div><dt>Category</dt><dd>{formatVideoCategory(video.category_name)}</dd></div>
          <div><dt>Counters checked</dt><dd>{formatTimestampLocal(video.checked_at)}</dd></div>
          <div><dt>Video ID</dt><dd>{video.video_id || 'Not reported'}</dd></div>
        </dl>

        {sources.staticDetails && <VideoStaticDetailsPanel source={sources.staticDetails} />}

        {!(sources.staticDetails && sources.description) && <div className="explorer-v2-video-description-unavailable">
          <strong>Description unavailable</strong>
          {descriptionOff
            ? <p>
              Descriptions of {launchTypeNoun(videoType)} are not collected on this server.
              The Collection fields setting can turn them on.
            </p>
            : <p>
              Description is not exposed by the current raw Explorer contract yet.
              This view keeps that data boundary visible instead of inventing content.
            </p>}
        </div>}

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
          Source: stored public {SOURCE_NOUNS[videoType]} Explorer fields.
          {observation && ' Observed views: Leti Cook calculation from stored snapshots.'}
          {' No owner-authorized analytics are mixed into this view.'}
        </small>
      </aside>
    </div>
  );
}
