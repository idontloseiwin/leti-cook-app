import { useSourceExpiry } from '../sourceLifetimeReact';
import { freshSourceRows, sourceExpired } from '../sourceLifetime';
import { SOURCE_EXPIRED_MESSAGE } from '../sourceLifetime.ts';
import {
  Eye,
  LoaderCircle,
  MessageCircle,
  ThumbsUp,
  Video,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { Channel } from '../types';
import {
  isAbortError,
  isContractError,
  loadExplorerRecentVideos,
} from './api';
import { formatCount, formatTimestamp } from './format';
import { safeVideoThumbnailUrl } from './videoPresentation';
import type {
  ExplorerRecentVideo,
  ExplorerRecentVideosPage,
  ExplorerRecentWindow,
} from './model';

const WINDOWS: Array<{ key: ExplorerRecentWindow; label: string }> = [
  { key: '24h', label: '24 hours' },
  { key: '7d', label: '7 days' },
  { key: '28d', label: '28 days' },
];
const REFRESH_INTERVAL_MS = 30_000;

type FeedStatus = 'loading' | 'ready' | 'error';

function metricNumber(value: number | string | null): number | undefined {
  if (value === null || value === '') return undefined;
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : undefined;
}

function videoPace(video: ExplorerRecentVideo, nowMs: number): number | undefined {
  const views = metricNumber(video.views);
  const publishedMs = Date.parse(video.published_at);
  if (views === undefined || !Number.isFinite(publishedMs) || publishedMs > nowMs) return undefined;
  const ageHours = Math.max(1, (nowMs - publishedMs) / 3_600_000);
  return views / ageHours;
}

// One decimal hides a slow but real pace: 0.04 is not zero.
function paceLabel(ratio: number): string {
  return ratio > 0 && ratio < 0.1 ? '<0.1' : ratio.toFixed(1);
}

function median(values: number[]): number | undefined {
  if (!values.length) return undefined;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function relativeAge(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return 'Publication time unavailable';
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
  if (minutes < 1) return 'Published just now';
  if (minutes < 60) return `Published ${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `Published ${hours}h ago`;
  return `Published ${Math.floor(hours / 24)}d ago`;
}

function contractMessage(error: unknown): string {
  const prefix = isContractError(error) ? 'The server returned an incompatible feed. ' : '';
  return `${prefix}${error instanceof Error ? error.message : String(error)}`;
}

export function ExplorerRecentVideos({
  channels,
  channelPath,
}: {
  channels: Channel[];
  channelPath: (channelId: string) => string;
}) {
  const [windowKey, setWindowKey] = useState<ExplorerRecentWindow>('7d');
  const [status, setStatus] = useState<FeedStatus>('loading');
  const [message, setMessage] = useState('');
  const [items, setItems] = useState<ExplorerRecentVideo[]>([]);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState('');
  const [pendingPage, setPendingPage] = useState<ExplorerRecentVideosPage | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const sectionRef = useRef<HTMLElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const itemsRef = useRef<ExplorerRecentVideo[]>([]);
  const firstPageIdsRef = useRef<Set<string>>(new Set());
  const loadedTailIdsRef = useRef<Set<string>>(new Set());
  const feedRevisionRef = useRef(0);
  const windowRef = useRef<ExplorerRecentWindow>(windowKey);
  const statusRef = useRef<FeedStatus>('loading');
  const initialControllerRef = useRef<AbortController | null>(null);
  const pollControllerRef = useRef<AbortController | null>(null);
  const loadMoreControllerRef = useRef<AbortController | null>(null);
  useSourceExpiry(items, () => {
    const previous = itemsRef.current;
    const fresh = freshSourceRows(previous);
    itemsRef.current = fresh;
    setItems(fresh);
    if (previous.length > 0 && fresh.length === 0) {
      statusRef.current = 'error';
      setStatus('error');
      setMessage(SOURCE_EXPIRED_MESSAGE);
    }
  }, () => { void refreshFirstPage(); });
  useSourceExpiry(pendingPage, () => setPendingPage(null));


  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  useEffect(() => {
    windowRef.current = windowKey;
  }, [windowKey]);

  const channelById = useMemo(() => new Map(
    channels
      .filter((channel) => channel.channel_id)
      .map((channel) => [channel.channel_id as string, channel]),
  ), [channels]);

  const applyFirstPage = useCallback((page: ExplorerRecentVideosPage) => {
    const previousFirstPageIds = firstPageIdsRef.current;
    const nextFirstPageIds = new Set(page.items.map((video) => video.video_id));
    const previouslyLoadedTailIds = loadedTailIdsRef.current;
    const publishedFromMs = Date.parse(page.published_from);
    const retainedTail = previouslyLoadedTailIds.size > 0
      ? itemsRef.current.filter((video) => (
        previouslyLoadedTailIds.has(video.video_id)
        && !previousFirstPageIds.has(video.video_id)
        && !nextFirstPageIds.has(video.video_id)
        && (
          !Number.isFinite(publishedFromMs)
          || Date.parse(video.published_at) >= publishedFromMs
        )
      ))
      : [];
    const nextItems = freshSourceRows([...page.items, ...retainedTail]);
    const preserveLoadedCursor = retainedTail.length > 0;
    feedRevisionRef.current += 1;
    loadMoreControllerRef.current?.abort();
    loadMoreControllerRef.current = null;
    setLoadingMore(false);
    setLoadMoreError('');
    itemsRef.current = nextItems;
    setItems(nextItems);
    firstPageIdsRef.current = nextFirstPageIds;
    loadedTailIdsRef.current = new Set(retainedTail.map((video) => video.video_id));
    setGeneratedAt(page.generated_at);
    if (!preserveLoadedCursor) {
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
    }
    setPendingPage(null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    initialControllerRef.current = controller;
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    feedRevisionRef.current += 1;
    loadMoreControllerRef.current?.abort();
    firstPageIdsRef.current = new Set();
    loadedTailIdsRef.current = new Set();
    itemsRef.current = [];
    statusRef.current = 'loading';
    setStatus('loading');
    setMessage('');
    setItems([]);
    setGeneratedAt(null);
    setNextCursor(null);
    setHasMore(false);
    setLoadingMore(false);
    setLoadMoreError('');
    setPendingPage(null);
    const current = () => !controller.signal.aborted && initialControllerRef.current === controller;
    if (document.visibilityState === 'visible') void loadExplorerRecentVideos(windowKey, undefined, controller.signal)
      .then((page) => {
        if (!current()) return;
        if (sourceExpired(page)) throw new Error(SOURCE_EXPIRED_MESSAGE);
        applyFirstPage(page);
        setMessage('');
        statusRef.current = 'ready';
        setStatus('ready');
      })
      .catch((error) => {
        if (!current() || isAbortError(error)) return;
        setMessage(contractMessage(error));
        statusRef.current = 'error';
        setStatus('error');
      })
      .finally(() => {
        if (initialControllerRef.current === controller) initialControllerRef.current = null;
      });
    else initialControllerRef.current = null;
    return () => {
      controller.abort();
      if (initialControllerRef.current === controller) initialControllerRef.current = null;
      pollControllerRef.current?.abort();
      pollControllerRef.current = null;
      loadMoreControllerRef.current?.abort();
      loadMoreControllerRef.current = null;
    };
  }, [applyFirstPage, reloadKey, windowKey]);

  const refreshFirstPage = useCallback(async () => {
    if (document.visibilityState !== 'visible' || initialControllerRef.current || pollControllerRef.current) return;
    const controller = new AbortController();
    pollControllerRef.current = controller;
    const requestedWindow = windowRef.current;
    try {
      const page = await loadExplorerRecentVideos(requestedWindow, undefined, controller.signal);
      if (controller.signal.aborted || pollControllerRef.current !== controller || requestedWindow !== windowRef.current) return;
      if (sourceExpired(page)) throw new Error(SOURCE_EXPIRED_MESSAGE);
      const recoveringFromError = statusRef.current !== 'ready';
      const previousFirstPageIds = firstPageIdsRef.current;
      const nextFirstPageIds = page.items.map((video) => video.video_id);
      const previousFirstPageOrder = [...previousFirstPageIds];
      const structureChanged = (
        previousFirstPageOrder.length !== nextFirstPageIds.length
        || nextFirstPageIds.some((videoId, index) => previousFirstPageOrder[index] !== videoId)
      );
      if (recoveringFromError) {
        applyFirstPage(page);
      } else if (structureChanged) {
        const sectionTop = sectionRef.current?.getBoundingClientRect().top ?? 0;
        if (sectionTop >= -180) {
          applyFirstPage(page);
        } else {
          setPendingPage(page);
        }
      } else {
        const hasLoadedTail = loadedTailIdsRef.current.size > 0;
        const refreshedById = new Map(page.items.map((video) => [video.video_id, video]));
        setItems((existing) => {
          const refreshed = freshSourceRows(existing.map((video) => refreshedById.get(video.video_id) ?? video));
          itemsRef.current = refreshed;
          return refreshed;
        });
        setGeneratedAt(page.generated_at);
        if (!hasLoadedTail) {
          setNextCursor(page.next_cursor);
          setHasMore(page.has_more);
        }
      }
      setMessage('');
      statusRef.current = 'ready';
      setStatus('ready');
    } catch (error) {
      if (controller.signal.aborted || pollControllerRef.current !== controller || requestedWindow !== windowRef.current || isAbortError(error)) return;
      setMessage(contractMessage(error));
      if (statusRef.current === 'loading') {
        statusRef.current = 'error';
        setStatus('error');
      }
    } finally {
      if (pollControllerRef.current === controller) pollControllerRef.current = null;
    }
  }, [applyFirstPage]);

  useEffect(() => {
    const interval = window.setInterval(() => void refreshFirstPage(), REFRESH_INTERVAL_MS);
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refreshFirstPage();
      else {
        initialControllerRef.current?.abort();
        initialControllerRef.current = null;
        pollControllerRef.current?.abort();
        pollControllerRef.current = null;
        loadMoreControllerRef.current?.abort();
        loadMoreControllerRef.current = null;
        setLoadingMore(false);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibility);
      pollControllerRef.current?.abort();
      pollControllerRef.current = null;
    };
  }, [refreshFirstPage, windowKey]);

  const loadMore = useCallback(async () => {
    if (document.visibilityState !== 'visible' || !hasMore || !nextCursor || loadingMore) return;
    setLoadingMore(true);
    setLoadMoreError('');
    const requestedWindow = windowKey;
    const requestedRevision = feedRevisionRef.current;
    loadMoreControllerRef.current?.abort();
    const controller = new AbortController();
    loadMoreControllerRef.current = controller;
    try {
      const page = await loadExplorerRecentVideos(requestedWindow, nextCursor, controller.signal);
      if (
        controller.signal.aborted
        || loadMoreControllerRef.current !== controller
        || requestedWindow !== windowRef.current
        || requestedRevision !== feedRevisionRef.current
      ) return;
      if (sourceExpired(page)) throw new Error(SOURCE_EXPIRED_MESSAGE);
      setItems((existing) => {
        const knownIds = new Set(existing.map((video) => video.video_id));
        const appendedItems = page.items.filter((video) => !knownIds.has(video.video_id));
        const appended = freshSourceRows([...existing, ...appendedItems]);
        for (const video of appendedItems) {
          loadedTailIdsRef.current.add(video.video_id);
        }
        itemsRef.current = appended;
        return appended;
      });
      setGeneratedAt(page.generated_at);
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (error) {
      if (!controller.signal.aborted && loadMoreControllerRef.current === controller
        && requestedWindow === windowRef.current && requestedRevision === feedRevisionRef.current
        && !isAbortError(error)) setLoadMoreError(contractMessage(error));
    } finally {
      if (loadMoreControllerRef.current === controller && requestedRevision === feedRevisionRef.current) {
        setLoadingMore(false);
        if (loadMoreControllerRef.current === controller) {
          loadMoreControllerRef.current = null;
        }
      }
    }
  }, [hasMore, loadingMore, nextCursor, windowKey]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore || loadingMore || loadMoreError || status !== 'ready') return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMore();
      },
      { rootMargin: '500px 0px' },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadMore, loadMoreError, loadingMore, status]);

  const visibleItems = freshSourceRows(items);
  // The render guard closes the interval before the expiry effect runs. Losing
  // every current row is unavailable data, not a confirmed empty catalogue.
  const expiredAll = status === 'ready' && items.length > 0 && visibleItems.length === 0;
  const visibleStatus = expiredAll ? 'error' : status;
  const visibleMessage = expiredAll ? message || SOURCE_EXPIRED_MESSAGE : message;
  const comparisonNow = generatedAt ? Date.parse(generatedAt) : Date.now();
  const medianPace = median(visibleItems.flatMap((video) => {
    const pace = videoPace(video, comparisonNow);
    return pace === undefined ? [] : [pace];
  }));

  const applyPendingPage = () => {
    if (!pendingPage) return;
    if (sourceExpired(pendingPage)) {
      setPendingPage(null);
      void refreshFirstPage();
      return;
    }
    applyFirstPage(pendingPage);
    sectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  const pendingCount = pendingPage
    ? pendingPage.items.filter((video) => !itemsRef.current.some(
      (current) => current.video_id === video.video_id,
    )).length
    : 0;

  return (
    <section className="explorer-recent" ref={sectionRef} aria-labelledby="explorer-recent-title">
      <header className="explorer-recent-header">
        <div>
          <span className="explorer-eyebrow">Stored long-form videos</span>
          <h2 id="explorer-recent-title">Latest videos</h2>
          <p>Newly published videos appear after the next completed auto-sync.</p>
        </div>
        <div className="explorer-window-switch" aria-label="Latest videos period">
          {WINDOWS.map((option) => (
            <button
              type="button"
              key={option.key}
              className={windowKey === option.key ? 'is-active' : undefined}
              aria-pressed={windowKey === option.key}
              onClick={() => setWindowKey(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </header>

      {pendingPage && (
        <button type="button" className="explorer-new-videos" onClick={applyPendingPage}>
          {pendingCount > 0
            ? pendingCount >= 20 ? '20+ new videos' : `${pendingCount} new ${pendingCount === 1 ? 'video' : 'videos'}`
            : 'Feed updated'}
        </button>
      )}

      {visibleStatus === 'loading' && (
        <div className="explorer-recent-loading">
          <LoaderCircle className="explorer-spin" size={20} aria-hidden="true" />
          Loading latest videos…
        </div>
      )}

      {visibleStatus === 'error' && (
        <div className="explorer-recent-error">
          <strong>Latest videos unavailable</strong>
          <span>{visibleMessage}</span>
          <button type="button" onClick={() => setReloadKey((value) => value + 1)}>Try again</button>
        </div>
      )}

      {visibleStatus === 'ready' && visibleItems.length === 0 && (
        <div className="explorer-recent-empty">
          No stored long-form videos were published in this period.
        </div>
      )}
      {visibleStatus === 'ready' && visibleMessage && <p role="status">{visibleMessage} <button type="button" onClick={() => void refreshFirstPage()}>Reload</button></p>}

      {visibleStatus === 'ready' && visibleItems.length > 0 && (
        <div className="explorer-recent-list">
          {visibleItems.map((video) => (
            <RecentVideoRow
              key={video.video_id}
              video={video}
              channel={channelById.get(video.channel_id)}
              medianPace={medianPace}
              comparisonNow={comparisonNow}
              channelPath={channelPath}
            />
          ))}
        </div>
      )}

      {visibleStatus === 'ready' && (
        <div className="explorer-recent-footer" ref={sentinelRef}>
          {loadingMore && <span><LoaderCircle className="explorer-spin" size={16} /> Loading 20 more…</span>}
          {loadMoreError && <span className="is-error">{loadMoreError}</span>}
          {hasMore && !loadingMore && (
            <button type="button" onClick={() => void loadMore()}>Load 20 more</button>
          )}
          {!hasMore && visibleItems.length > 0 && <span>End of the selected period</span>}
          {generatedAt && <small title={formatTimestamp(generatedAt)}>Live check every 30 seconds</small>}
        </div>
      )}
    </section>
  );
}

function RecentVideoRow({
  video,
  channel,
  medianPace,
  comparisonNow,
  channelPath,
}: {
  video: ExplorerRecentVideo;
  channel?: Channel;
  medianPace?: number;
  comparisonNow: number;
  channelPath: (channelId: string) => string;
}) {
  const pace = videoPace(video, comparisonNow);
  const ratio = pace !== undefined && medianPace !== undefined && medianPace > 0
    ? pace / medianPace
    : undefined;
  const markerPosition = ratio === undefined
    ? 50
    : Math.max(4, Math.min(96, 50 + Math.log2(Math.max(ratio, 0.01)) * 18));
  const title = video.title?.trim() || 'Untitled video';
  const channelTitle = channel?.title?.trim() || video.channel_id;
  const thumbnailUrl = safeVideoThumbnailUrl(video.thumbnail_url);

  return (
    <article className="explorer-recent-row">
      <div className="explorer-recent-thumb">
        {thumbnailUrl
          ? <img src={thumbnailUrl} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" />
          : <Video size={24} aria-hidden="true" />}
      </div>
      <div className="explorer-recent-main">
        <h3>{title}</h3>
        <div className="explorer-recent-byline">
          <a href={channelPath(video.channel_id)}>
            {channelTitle}
          </a>
          <span title={formatTimestamp(video.published_at)}>{relativeAge(video.published_at)}</span>
        </div>
      </div>
      <dl className="explorer-recent-metrics">
        <div><dt><Eye size={14} /> Views</dt><dd>{formatCount(video.views)}</dd></div>
        <div><dt><ThumbsUp size={14} /> Likes</dt><dd>{formatCount(video.like_count)}</dd></div>
        <div><dt><MessageCircle size={14} /> Comments</dt><dd>{formatCount(video.comment_count)}</dd></div>
      </dl>
      <div
        className="explorer-relative-pace"
        title="Views divided by video age, with a minimum age of one hour, compared with the median of the loaded videos."
      >
        <div>
          <span>Relative view pace</span>
          <strong>{ratio === undefined ? 'Not enough data' : `${paceLabel(ratio)}× median`}</strong>
        </div>
        <div className="explorer-relative-track" aria-hidden="true">
          <span className="explorer-relative-median" />
          {ratio !== undefined && (
            <span className="explorer-relative-marker" style={{ left: `${markerPosition}%` }} />
          )}
        </div>
      </div>
    </article>
  );
}
