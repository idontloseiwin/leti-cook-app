import { useSourceExpiry } from '../sourceLifetimeReact';
import { sourceExpired, sourceOwnExpired } from '../sourceLifetime.ts';
import { AlertTriangle } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from 'react';
import type {
  ChannelExplorerVideo,
  VideoType,
} from '../types';
import {
  formatCount,
  youtubeVideoUrl,
} from './format';
import {
  CARD_DASH, cardAge, cardCount, cardDateWords, cardDuration, cardDurationSeconds, cardGrowth, exactCount,
  isSingleCount, videoCardLabel,
} from './videoCardFormat';
import { formatDateLocal, formatExactLocal, formatTimestampLocal } from './localTime';
import { safeVideoThumbnailUrl } from './videoPresentation';
import type { LibraryVideosState } from './useCatalogueBrowse';
import { listPosition, type LibraryCardField, type LibraryValueState, type LibraryVideo, type LibraryVideosPayload } from './libraryData/videos.ts';
import type { InspectorGates } from './libraryData/inspector.ts';
import { LIBRARY_ERROR_TEXT, orderLimitText } from './libraryData/errors.ts';
import { ErrorState, LoadingState } from './ModuleState';
import { resourcePayload } from './resourceState';
import type { CatalogueDensity } from './cataloguePreferences';
import {
  VirtualVideoGrid, type VideoGridRestoreAnchor, type VirtualVideoGridHandle,
} from './VirtualVideoGrid';
import { videoCardForm, type VideoCardForm, type VideoGridAnchor } from './videoGridGeometry';
import { addSelectedRecords, type CollectMatching, type SelectionProgress } from './catalogueSelection';
import { videoTitle } from './VideoThumbnail';
import { VideoInspector } from './VideoInspector';

/** The selection of a format nobody has selected in yet (never written). */
const NO_SELECTION: ReadonlyMap<string, ChannelExplorerVideo> = new Map();

// Saved collections import the Inspector from here as well.
export { VideoInspector };

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
export type LibrarySessionView = { frozenRef?: string; sessionKey: string; pending: boolean };
/** Both return a refusal text, or nothing when the workshop opened. */
export type LibraryWorkshopActions = {
  openSelection: () => Promise<string | undefined> | string | undefined;
  sendSelected: (selected: ReadonlyArray<{ id: string; type?: VideoType }>) => Promise<string | undefined> | string | undefined;
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

/** The state of a card value the frozen row has but cannot show ("—" with its word), if any. */
function valueState(video: ChannelExplorerVideo, field: LibraryCardField): LibraryValueState | undefined {
  return (video as Partial<LibraryVideo>).value_states?.[field];
}

/** The word next to "—" and the hover text that explains it; a value without state is "Not reported". */
const STATE_WORD: Record<LibraryValueState, string> = { expired: 'Needs refresh', unavailable: 'Unavailable' };
const STATE_HINT: Record<LibraryValueState, string> = {
  expired: 'Data is out of date. New numbers appear after the next refresh.',
  unavailable: 'YouTube no longer returns this value.',
};
const NOT_REPORTED = 'Not reported';

// Card icons; decorative, the controls carry the names.
const LIKE_ICON = (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden="true">
    <path d="M5 7.2 7.6 2c1 0 1.7.8 1.7 1.8v2.4h3c.8 0 1.4.7 1.2 1.5l-1 4.6c-.1.6-.6 1-1.2 1H5zM5 7.2H2.5v6.1H5" />
  </svg>
);
const COMMENT_ICON = (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden="true">
    <path d="M2.5 3.5h11v7.5H8l-3 2.5V11H2.5z" />
  </svg>
);
const EXTERNAL_ICON = (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M9 3h4v4M13 3 7.5 8.5M11 9.5V13H3V5h3.5" />
  </svg>
);
const CHECK_ICON = (
  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m3.5 8.3 3 3 6-6.5" />
  </svg>
);
const NO_THUMBNAIL_ICON = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M4 5h16v14H4zM4 16l4.5-4.5 3 3M3 3l18 18" />
  </svg>
);

/** Likes or comments: icon and short count; "—" with the reason when there is no number. */
function CardStat({ value, state, noun, icon }: {
  value: ChannelExplorerVideo['like_count'];
  state?: LibraryValueState;
  noun: 'like' | 'comment';
  icon: ReactNode;
}) {
  const text = cardCount(value);
  const nouns = isSingleCount(value) ? noun : `${noun}s`;
  const reason = state ? STATE_WORD[state] : NOT_REPORTED;
  return (
    <span className="video-card-stat" title={text === null ? (state ? STATE_HINT[state] : NOT_REPORTED) : `${exactCount(value)} ${nouns}`}>
      {icon}{text ?? CARD_DASH}
      <span className="explorer-sr-only">{text === null ? ` ${nouns}: ${reason.toLowerCase()}` : ` ${nouns}`}</span>
    </span>
  );
}

/**
 * One video of the Videos grid and of a collection: the thumbnail (only scaled) with its duration, a title
 * of two lines and rows of fixed height, so every card of a grid has the same height.
 * Long form (16:9; every collection card): Large: views and growth; date, likes, comments and the
 * YouTube link. Compact: views and date; growth and the link.
 * Shorts form (2:3, centre-cropped; the Videos tab's Shorts): Large: views; growth; likes and comments;
 * date and the link. Compact: views and the link; growth.
 * Tab order: thumbnail, title, checkbox, link. Only numbers from the data and their difference; a missing
 * value is "—", never 0.
 */
export function VideoCard({
  video,
  growthAvailable,
  selected,
  selecting = false,
  density = 'large',
  form = 'long',
  onToggleSelected,
  onInspect,
}: {
  video: ChannelExplorerVideo;
  growthAvailable: boolean;
  selected: boolean;
  /** A card of the list is selected: every checkbox stays visible. */
  selecting?: boolean;
  density?: CatalogueDensity;
  /** The grid's form: Shorts only in the Videos tab's Shorts grid; collections keep Long for every type. */
  form?: VideoCardForm;
  onToggleSelected: (video: ChannelExplorerVideo, checked: boolean) => void;
  onInspect: (video: ChannelExplorerVideo, trigger: 'thumbnail' | 'title') => void;
}) {
  const title = videoTitle(video);
  const id = video.video_id?.trim();
  const href = youtubeVideoUrl(video.video_id);
  const large = density !== 'compact';
  const shorts = form === 'shorts';
  const viewsState = valueState(video, 'views');
  const unavailable = viewsState === 'unavailable';
  const views = cardCount(video.views);
  const viewNoun = isSingleCount(video.views) ? 'view' : 'views';
  const growth = growthAvailable ? cardGrowth((video as Partial<LibraryVideo>).view_growth) : null;
  const seconds = cardDurationSeconds(video.duration);
  const publishedState = valueState(video, 'published_at');
  // Counted from now at each render, without a timer: the frozen session keeps the instant, not the age.
  const age = publishedState ? null : cardAge(video.published_at, Date.now());
  const date = publishedState || !video.published_at ? CARD_DASH : age ?? formatDateLocal(video.published_at);
  const dateTitle = publishedState ? STATE_HINT[publishedState]
    : video.published_at ? `Published ${formatExactLocal(video.published_at)}` : NOT_REPORTED;
  // An unavailable video shows the placeholder instead of its thumbnail.
  const thumbnailUrl = unavailable ? undefined : safeVideoThumbnailUrl(video.thumbnail_url);
  const label = videoCardLabel({
    type: video.video_type,
    title,
    views: video.views,
    viewsState: (viewsState ? STATE_WORD[viewsState] : NOT_REPORTED).toLowerCase(),
    growthWords: growth?.words,
    durationSeconds: seconds,
    published: publishedState ? null : cardDateWords(video.published_at),
  });

  const viewsValue = views !== null ? (
    <span className="video-card-views" title={`${exactCount(video.views)} ${viewNoun}`}>
      <b>{views}</b><i>{viewNoun}</i>
    </span>
  ) : (
    <span className="video-card-views" title={viewsState ? STATE_HINT[viewsState] : NOT_REPORTED}>
      <b>{CARD_DASH}</b>
      <em className={`video-card-state${viewsState === 'expired' ? ' is-warning' : ''}`}>
        {viewsState ? STATE_WORD[viewsState] : NOT_REPORTED}
      </em>
    </span>
  );
  const growthChip = growth && (
    <span className={`video-card-growth is-${growth.tone}`} title={growth.title}>{growth.text}</span>
  );
  const dateText = <span className="video-card-date" title={dateTitle}>{date}</span>;
  const youtubeLink = href && (
    <a
      className="video-card-youtube"
      href={href}
      target="_blank"
      rel="noreferrer"
      data-grid-focus="youtube"
      title="Open on YouTube"
      aria-label={`Open ${title} on YouTube`}
    >
      {EXTERNAL_ICON}
    </a>
  );
  const likes = <CardStat value={video.like_count} state={valueState(video, 'like_count')} noun="like" icon={LIKE_ICON} />;
  const comments = <CardStat value={video.comment_count} state={valueState(video, 'comment_count')} noun="comment" icon={COMMENT_ICON} />;
  const classes = ['video-card', shorts ? 'is-shorts' : 'is-long', large ? 'is-large' : 'is-compact', selected && 'is-selected',
    selecting && 'is-selecting', unavailable && 'is-unavailable'].filter(Boolean).join(' ');

  return (
    <article className={classes} aria-label={label} data-video-id={video.video_id}>
      <div className="video-card-media">
        <button
          type="button"
          className="video-card-thumbnail"
          data-inspector-trigger="thumbnail"
          data-grid-focus="thumbnail"
          onClick={() => onInspect(video, 'thumbnail')}
          aria-label={`Inspect ${title}`}
        >
          <span className="video-card-placeholder">{NO_THUMBNAIL_ICON}</span>
          {/* A failed image hides itself and uncovers the placeholder (no state: the card stays a plain render). */}
          {thumbnailUrl && (
            <img
              key={thumbnailUrl}
              src={thumbnailUrl}
              alt=""
              loading="lazy"
              decoding="async"
              referrerPolicy="no-referrer"
              onError={(event) => { event.currentTarget.hidden = true; }}
            />
          )}
        </button>
        {/* YouTube reports 0 for a video without a length yet (a live or upcoming stream): "—", not "0:00". */}
        <span className="video-card-badge">{seconds ? cardDuration(seconds) : CARD_DASH}</span>
      </div>
      <button
        type="button"
        className="video-card-title"
        data-inspector-trigger="title"
        data-grid-focus="title"
        onClick={() => onInspect(video, 'title')}
        aria-label={`Inspect ${title}`}
        title={large ? title : `${title} · ${dateTitle}`}
      >
        <span>{title}</span>
      </button>
      <label className="video-card-check">
        <input
          type="checkbox"
          data-grid-focus="selection"
          checked={selected}
          disabled={!id}
          onChange={(event) => onToggleSelected(video, event.currentTarget.checked)}
          aria-label={`Select ${title}`}
        />
        <span aria-hidden="true">{CHECK_ICON}</span>
      </label>
      {shorts && large ? (
        <>
          <div className="video-card-row">{viewsValue}</div>
          <div className="video-card-row">{growthChip}</div>
          <div className="video-card-row is-small video-card-meta">{likes}{comments}</div>
          <div className="video-card-row video-card-meta">{dateText}{youtubeLink}</div>
        </>
      ) : shorts ? (
        <>
          <div className="video-card-row">{viewsValue}{youtubeLink}</div>
          <div className="video-card-row">{growthChip}</div>
        </>
      ) : large ? (
        <>
          <div className="video-card-row">{viewsValue}{growthChip}</div>
          <div className="video-card-row video-card-meta">{dateText}{likes}{comments}{youtubeLink}</div>
        </>
      ) : (
        <>
          <div className="video-card-row">{viewsValue}{dateText}</div>
          <div className="video-card-row">{growthChip}{youtubeLink}</div>
        </>
      )}
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
  anchorMissing,
  onStartFromBeginning,
  onRefreshOrder,
  departedShown = 0,
  refreshProblem,
  leftResults = 0,
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
  anchorMissing?: boolean;
  /** "Show from the beginning": the loaded part of the list starts below its top. */
  onStartFromBeginning?: () => void;
  /** Read the list again (its order, total and values) at the remembered card. */
  onRefreshOrder?: () => void;
  /** Shown cards that no longer match the filters or are no longer available: they stay until the list is read again. */
  departedShown?: number;
  /** The last background update of the numbers failed (the cards are unchanged), in words. */
  refreshProblem?: string;
  /** Videos of the loaded part that no longer match or are no longer available. */
  leftResults?: number;
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
  // Every loaded card stays: a value past its own lifetime arrives here already marked expired (useCatalogueBrowse).
  const videos = useMemo(() => payload?.videos ?? [], [payload?.videos]);
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
  const [inspectorTarget, setInspectedVideo] = useState<ChannelExplorerVideo | null>(null);
  const currentInspector = useRef(false);
  // A current card's last loaded values: another sort of the same matched set keeps the Inspector open
  // while the video is in the new list, even before its card loads there.
  const lastInspected = useRef<ChannelExplorerVideo | null>(null);
  const loadedInspected = inspectorTarget && currentInspector.current
    ? videos.find(video => video.video_id === inspectorTarget.video_id) ?? null : null;
  if (loadedInspected) lastInspected.current = loadedInspected;
  const inspectedListed = Boolean(inspectorTarget?.video_id && lastInspected.current?.video_id === inspectorTarget.video_id
    && listPosition(payload?.browse?.order, inspectorTarget.video_id) >= 0);
  const inspectedVideo = inspectorTarget && currentInspector.current
    ? loadedInspected ?? (inspectedListed ? lastInspected.current : null) : inspectorTarget;
  const [inspectorNotice, setInspectorNotice] = useState('');
  useEffect(() => {
    if (inspectorTarget && currentInspector.current && !inspectedVideo && state.kind === 'ready') {
      setInspectedVideo(null);
      setInspectorNotice('This video is no longer in these results. The Inspector was closed.');
    }
  }, [inspectorTarget, inspectedVideo, state.kind]);
  // Time alone never closes the Inspector; its panels read and refresh their own data.
  useSourceExpiry(selectedVideos, () => setSelectedVideos((previous) => new Map([...previous].map(([id, video]) =>
    [id, sourceExpired(video) ? { video_id: id, video_type: video.video_type } : video]))));

  // Frozen Inspector panels belong to the session in which their card was opened.
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
    setInspectorNotice('');
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
  useEffect(() => setInspectorNotice(''), [selectionScopeKey]);

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
    currentInspector.current = payload?.browse?.consistency === 'current';
    setInspectorNotice('');
    // A card of the current list opens by its ID (the panels read their own data); a fixed row only while it is current.
    setInspectedVideo(currentInspector.current ? { video_id: video.video_id } : sourceExpired(video) ? null : video);
    setInspectorSession(session?.sessionKey);
  }, [session?.sessionKey, payload?.browse?.consistency]);
  const restoreInspectorFocus = useCallback(() => {
    const saved = inspectorReturnRef.current;
    if (saved && saved.scope === scopeRef.current) {
      gridRef.current?.restore(saved.anchor, { videoId: saved.videoId, trigger: saved.trigger });
    }
  }, []);
  const workshopFeedback = async (result: Promise<string | undefined> | string | undefined) => {
    const scope = scopeRef.current;
    const message = await result;
    if (scope !== scopeRef.current) return;
    keepSelectionViewport();
    setSelectionMessage(message ?? '');
  };
  const loadingLabel = preparing ? LIBRARY_ERROR_TEXT.preparing : `Loading stored ${FORMAT_NOUNS[videoType].catalogue} catalogue…`;
  // The list of this read: its total always; after the read's own lifetime, the time its order is from.
  const order = payload?.browse?.order;
  const orderFrom = order?.ids && payload && sourceOwnExpired(payload.browse) ? payload.browse.created_at : undefined;
  const plural = (count: number, one: string, many: string) => `${count.toLocaleString('en')} ${count === 1 ? one : many}`;

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
            <span>{payload && (order
              ? <><strong>{formatCount(order.total, { compact: false })}</strong> {order.total === 1 ? 'video' : 'videos'}
                {orderFrom && <> · order from {formatTimestampLocal(orderFrom)}</>}</>
              : <><strong>{formatCount(sourceOwnExpired(payload) ? videos.length : payload.pagination?.matched_count ?? videos.length, { compact: false })}</strong> {sourceOwnExpired(payload) || payload.pagination.matched_count === undefined ? 'loaded videos' : 'videos'}</>)}</span>
            <div className="explorer-v2-video-selection-actions">
              {onRefreshOrder && order && <button type="button" className="explorer-secondary-button"
                title="Read this list again: its order, count and values" disabled={state.kind !== 'ready' || isLoadingMore}
                onClick={onRefreshOrder}>Refresh order</button>}
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
          {order && !order.ids && <p role="status">{orderLimitText(order.total, order.limit)}</p>}
          {leftResults > 0 && <p role="status">{plural(leftResults, 'video', 'videos')} of the loaded part no longer {leftResults === 1 ? 'matches' : 'match'} these
            filters or {leftResults === 1 ? 'is' : 'are'} no longer available. Refresh order to update the list.</p>}
          {departedShown > 0 && <p role="status">{plural(departedShown, 'shown video', 'shown videos')} no longer {departedShown === 1 ? 'matches' : 'match'} these
            filters or {departedShown === 1 ? 'is' : 'are'} no longer available. {departedShown === 1 ? 'Its card stays' : 'Their cards stay'} with the last
            values. Refresh order to update the list.</p>}
          {refreshProblem && <p role="status">{refreshProblem}</p>}
          {anchorMissing && <p role="status">The video you were viewing is no longer in these results. Showing the first page.</p>}
          {onStartFromBeginning && <button type="button" className="explorer-secondary-button"
            disabled={state.kind !== 'ready' || isLoadingMore} onClick={onStartFromBeginning}>Show from the beginning</button>}
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
          {/* One grid per read of the list: a new order never restores a card of the previous one. */}
          {videos.length > 0 ? (
            <VirtualVideoGrid
              key={session?.sessionKey}
              ref={gridRef}
              videos={videos}
              density={density}
              videoType={videoType}
              scopeKey={selectionScopeKey}
              inspectedVideoId={inspectedVideo?.video_id || undefined}
              onAnchorChange={onAnchorChange}
              restoreAnchor={restoreAnchor}
              renderCard={(video) => (
                  <VideoCard
                    key={video.video_id || video.title}
                    video={video}
                    growthAvailable={growthAvailable}
                    density={density}
                    form={videoCardForm(videoType)}
                    selected={selectedVideos.has(video.video_id?.trim() || '')}
                    selecting={selectedVideos.size > 0}
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
      {inspectorNotice && <p role="status">{inspectorNotice}</p>}
      {inspectedVideo && (
        <VideoInspector key={currentInspector.current ? session?.sessionKey : undefined} video={inspectedVideo}
          growth={growthAvailable ? (inspectedVideo as Partial<LibraryVideo>).view_growth : undefined}
          library={inspector && inspectorSession && channelId && session ? {
            gates: inspector.gates,
            opening: {
              channelId, videoType,
              ...(session.frozenRef ? { frozenRef: session.frozenRef, sessionKey: inspectorSession }
                : { kind: 'query' as const, scope: { kind: 'channel' as const, id: channelId } }),
              checkedAt: typeof inspectedVideo.checked_at === 'string' ? inspectedVideo.checked_at : null,
              active: (currentInspector.current || inspectorSession === session.sessionKey) && !expired,
              pending: session.pending, timeZone: inspector.timeZone,
            },
          } : undefined}
          onClose={closeInspector} onRestoreFocus={restoreInspectorFocus} />
      )}
    </section>
  );
}
