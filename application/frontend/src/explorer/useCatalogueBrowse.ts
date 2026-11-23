import { useSourceExpiry } from '../sourceLifetimeReact';
import { freshSourceRows, sourceContainer } from '../sourceLifetime.ts';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { CatalogueAnchor, CatalogueRestoreAnchor, BrowseDirection } from './catalogueBrowse';
import { CatalogueRequestGate } from './catalogueBrowse';
import type { ResourceState } from './model';
import type { CollectMatching } from './catalogueSelection';
import { catalogueUsesGrowthFilters, type CatalogueConditions } from './catalogueQuery';
import { videoNoun } from './format';
import { ROUTES, frozenIdsRequest, type LibraryQueryRequest, type LibraryVideoType, type TranslatedQuery } from './libraryData/wire.ts';
import { planLibraryOpen, type TranslateFunction } from './libraryData/savedLibrary.ts';
import {
  LIBRARY_ERROR_TEXT, PREPARING_DELAY_MS, PREPARING_RETRIES, libraryErrorState, type LibraryErrorState,
} from './libraryData/errors.ts';
import type { LibraryGates } from './libraryData/capabilities.ts';
import {
  libraryFrozenFields, libraryGrowthFields, mergeLibraryPages, parseFrozenIds, parseLibraryPage, selectionRecords,
  type LibraryVideosPayload,
} from './libraryData/videos.ts';

/** POST one JSON body to the shared backend (requestJson in the app, a fake in tests). */
export type LibraryRequest = (path: string, body: unknown, signal: AbortSignal) => Promise<unknown>;
export type LibraryVideosState = ResourceState<LibraryVideosPayload>;

type Inputs = {
  channelId: string;
  videoType: LibraryVideoType;
  conditions: CatalogueConditions;
  /** Publication dates, years and "today": the viewer's zone or UTC. */
  timeZone: string;
  /** Channel + type + conditions + sort + zone + reload token: one frozen session. */
  scopeKey: string;
  /** Channel + type + conditions (without sort): selection and the anchor memory. */
  selectionScopeKey: string;
  enabled: boolean;
  /** `/research/capabilities` gates of this type; null while unknown (card fields only). */
  gates: LibraryGates | null;
  /** Growth fields are selected in this attempt (a refused attempt turns them off). */
  growthAllowed: boolean;
  onGrowthUnavailable: () => void;
  onAnnouncement: (message: string) => void;
  translate: TranslateFunction;
  request: LibraryRequest;
};
type Snapshot = {
  key: string;
  payload?: LibraryVideosPayload;
  /** The server translation of the conditions (membership) and the first page request of this session. */
  translated?: TranslatedQuery;
  request?: LibraryQueryRequest;
  initialLoading: boolean;
  preparing?: boolean;
  initialError?: string;
  incompatible?: boolean;
  /** `invalid_query` names the condition. */
  errorPath?: string;
  /** The server cannot translate these conditions yet (needs_capability): nothing runs, results are kept. */
  blocked?: string;
  isLoadingMore: boolean;
  loadMoreError?: string;
  loadMoreErrorDirection?: BrowseDirection;
  expired: boolean;
  restoreAnchor?: CatalogueRestoreAnchor;
  /** Every id of this session after "Select all matching" (a whole-list selection). */
  frozenIds?: { sessionKey: string; ids: string[] };
};

const RENDER_FIELDS_MESSAGE = 'The video catalogue reply could not be verified.';

/** The text of a first-page failure (column "Videos: first page"). */
function firstPageMessage(state: LibraryErrorState, error: unknown): string {
  switch (state.action) {
    case 'too_large': case 'runtime_not_ready': return state.message!;
    case 'channel_unavailable': return 'This channel is not available in the stored catalogue.';
    case 'unavailable': return 'The stored video catalogue is unavailable on this server.';
    case 'invalid_field': return `Check the filters and try again.${state.path ? ` (${state.path})` : ''}`;
    case 'incompatible': return error instanceof Error ? error.message : RENDER_FIELDS_MESSAGE;
    default: return state.message ?? (error instanceof Error ? error.message : String(error));
  }
}

class BlockedTranslation extends Error {}

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
    const timer = setTimeout(() => { signal.removeEventListener('abort', cancel); resolve(); }, milliseconds);
    const cancel = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
    signal.addEventListener('abort', cancel, { once: true });
  });
}

/**
 * The Videos tab's frozen session on the shared backend: the
 * conditions are translated by the server, the matched list is frozen for
 * 600 s, and pages, totals, "back", the anchor and "Select all matching" all
 * read that one copy. Transport cursors never enter URL state.
 */
export function useCatalogueBrowse(inputs: Inputs) {
  const latest = useRef(inputs);
  latest.current = inputs;
  const initial: Snapshot = { key: inputs.scopeKey, initialLoading: false, isLoadingMore: false, expired: false };
  const [snapshot, setSnapshot] = useState<Snapshot>(initial);
  const [, repaint] = useState(0);
  const current = useRef(snapshot);
  const gate = useRef(new CatalogueRequestGate());
  const requestController = useRef<AbortController | null>(null);
  const selectionController = useRef<AbortController | null>(null);
  const anchorMemory = useRef<{ scope: string; anchor?: CatalogueAnchor }>({ scope: inputs.selectionScopeKey });
  if (anchorMemory.current.scope !== inputs.selectionScopeKey) anchorMemory.current = { scope: inputs.selectionScopeKey };

  const publish = useCallback((value: Snapshot) => {
    if (current.current.key !== value.key || current.current.payload?.browse.session_key !== value.payload?.browse.session_key) selectionController.current?.abort();
    current.current = value; setSnapshot(value);
  }, []);
  const request = useCallback(async (direction?: BrowseDirection) => {
    const args = latest.current;
    if (!args.enabled) return;
    // A new request replaces the previous one's "more" error: every outcome below starts from `base`
    // (a successful Retry kept the old error, which also kept automatic loading paused).
    const base: Snapshot = { ...(current.current.key === args.scopeKey ? current.current : {
      key: args.scopeKey, initialLoading: false, isLoadingMore: false, expired: false,
    } as Snapshot), loadMoreError: undefined, loadMoreErrorDirection: undefined };
    const cursor = direction && base.payload?.browse[direction === 'next' ? 'next_cursor' : 'previous_cursor'];
    if (direction && (!base.payload || !cursor || !base.request)) return;
    const ticket = gate.current.begin();
    if (!ticket) return;
    const controller = new AbortController();
    requestController.current = controller;
    const isCurrent = () => !controller.signal.aborted && latest.current.scopeKey === args.scopeKey && gate.current.accepts(ticket);
    publish({ ...base, initialLoading: !direction, preparing: false, initialError: undefined, incompatible: false, errorPath: undefined, blocked: undefined,
      isLoadingMore: Boolean(direction), loadMoreError: undefined, loadMoreErrorDirection: direction });
    const signal = controller.signal;
    try {
      if (direction) {
        // The same frozen query (select, sort, zone, size): a cursor never crosses queries.
        const next: LibraryQueryRequest = { ...base.request!, page: { size: base.request!.page.size, after: cursor! } };
        let page: LibraryVideosPayload | undefined;
        for (let preparing = 0; !page; preparing += 1) {
          try {
            page = parseLibraryPage(await args.request(ROUTES.execute, next, signal), { request: next, channelId: args.channelId });
          } catch (error) {
            const state = libraryErrorState(error, 'videos_more');
            if (state.action !== 'preparing' || preparing >= PREPARING_RETRIES || !isCurrent()) throw error;
            await wait(PREPARING_DELAY_MS, signal);
          }
        }
        if (!isCurrent()) return;
        const payload = mergeLibraryPages(base.payload!, page, direction);
        publish({ ...base, payload, initialLoading: false, isLoadingMore: false, expired: false });
        args.onAnnouncement(`${payload.videos.length} of ${payload.browse.total_count} ${videoNoun(payload.browse.total_count)} loaded.`);
        return;
      }
      // First page: translate (cached), then a new frozen session at the remembered anchor.
      const anchor = anchorMemory.current.anchor;
      const fields = libraryFrozenFields(args.gates);
      let growthFields = libraryGrowthFields(args.growthAllowed);
      let retried = false, preparing = 0;
      let page: LibraryVideosPayload | undefined, translated: TranslatedQuery | undefined, firstRequest: LibraryQueryRequest | undefined;
      while (!page) {
        const plan = await planLibraryOpen(args.translate, {
          channelId: args.channelId, conditions: args.conditions, videoType: args.videoType, viewerZone: args.timeZone,
          page: anchor ? { anchorId: anchor.videoId } : {}, growthFields, fields,
        }, signal);
        if (!isCurrent()) return;
        if (plan.status === 'blocked') throw new BlockedTranslation(plan.blockedText || plan.message);
        try {
          page = parseLibraryPage(await args.request(ROUTES.execute, plan.request, signal), { request: plan.request, channelId: args.channelId });
          translated = plan.translation.query; firstRequest = plan.request;
        } catch (error) {
          if (!isCurrent()) return;
          const state = libraryErrorState(error, 'videos_first');
          if (state.action === 'retry_once' && !retried) { retried = true; continue; }
          if (state.action === 'preparing' && preparing < PREPARING_RETRIES) {
            preparing += 1;
            publish({ ...current.current, preparing: true });
            args.onAnnouncement(LIBRARY_ERROR_TEXT.preparing);
            await wait(PREPARING_DELAY_MS, signal);
            continue;
          }
          // Growth refused for this attempt → the same list without growth fields.
          if (state.action === 'unavailable' && growthFields.length && !catalogueUsesGrowthFilters(args.conditions)
            && args.conditions.filters.sort_by !== 'views_gained') {
            args.onGrowthUnavailable();
            args.onAnnouncement('Growth data is unavailable. Loading the current catalogue instead.');
            growthFields = [];
            continue;
          }
          throw error;
        }
      }
      if (!isCurrent()) return;
      const restored = anchor && page.videos.some((video) => video.video_id === anchor.videoId)
        ? { ...anchor, requestKey: `${args.scopeKey}:${page.browse.session_key}` } : base.restoreAnchor;
      publish({ key: args.scopeKey, payload: page, translated, request: firstRequest, initialLoading: false, isLoadingMore: false,
        expired: false, restoreAnchor: restored,
        frozenIds: base.frozenIds?.sessionKey === page.browse.session_key ? base.frozenIds : undefined });
      args.onAnnouncement(`${page.videos.length} of ${page.browse.total_count} ${videoNoun(page.browse.total_count)} loaded.`);
    } catch (error) {
      if (!isCurrent()) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (error instanceof BlockedTranslation) {
        publish({ ...base, initialLoading: false, isLoadingMore: false, preparing: false, blocked: error.message });
        return;
      }
      const state = libraryErrorState(error, direction ? 'videos_more' : 'videos_first');
      if (state.action === 'aborted') return;
      if (direction) {
        const expired = state.action === 'expired' || state.action === 'expired_changed';
        if (state.action === 'expired_changed') args.onAnnouncement(LIBRARY_ERROR_TEXT.accessChanged);
        publish({ ...base, initialLoading: false, isLoadingMore: false, expired,
          loadMoreError: expired ? undefined : state.action === 'incompatible'
            ? 'Video catalogue contract is incompatible. Reload catalogue.' : state.message ?? 'More videos could not load.',
          loadMoreErrorDirection: direction });
        return;
      }
      publish({ ...base, initialLoading: false, isLoadingMore: false, preparing: false, expired: false,
        initialError: firstPageMessage(state, error), incompatible: state.action === 'incompatible',
        errorPath: state.path });
    } finally {
      gate.current.finish(ticket);
      if (requestController.current === controller) requestController.current = null;
    }
  }, [publish]);
  // Expiry or a return to the tab opens a new session at the anchor; selection is kept.
  useSourceExpiry(snapshot.payload, () => repaint(value => value + 1), () => { void request(); });

  useEffect(() => {
    gate.current.reset();
    requestController.current?.abort();
    requestController.current = null;
    if (current.current.key !== inputs.scopeKey) {
      publish({ key: inputs.scopeKey, initialLoading: false, isLoadingMore: false, expired: false });
    } else if (current.current.initialLoading || current.current.isLoadingMore) {
      publish({ ...current.current, initialLoading: false, isLoadingMore: false });
    }
    if (inputs.enabled && !current.current.payload && !current.current.initialError && !current.current.expired) void request();
    return () => { requestController.current?.abort(); selectionController.current?.abort(); gate.current.reset(); };
  }, [inputs.scopeKey, inputs.enabled, publish, request]);

  /** "Select all matching": one id export of the frozen session, not a page walk. */
  const collectMatching: CollectMatching = useCallback(async (signal, onProgress) => {
    const args = latest.current, base = current.current;
    const seed = base.payload;
    if (!args.enabled || base.key !== args.scopeKey || !seed || base.expired || base.initialLoading) {
      throw new Error('Load the current catalogue before selecting all matching videos. Existing selection is kept.');
    }
    if (Date.parse(seed.browse.expires_at) <= Date.now()) throw new Error(LIBRARY_ERROR_TEXT.selectAllExpired);
    if (selectionController.current) throw new Error('A complete selection is already being loaded.');
    const controller = new AbortController();
    selectionController.current = controller;
    const cancel = () => controller.abort();
    if (signal.aborted) cancel();
    signal.addEventListener('abort', cancel, { once: true });
    const total = seed.browse.total_count;
    try {
      onProgress({ received: 0, total });
      let ids: string[];
      try {
        ids = parseFrozenIds(await args.request(ROUTES.frozenIds, frozenIdsRequest(seed.browse.frozen_ref), controller.signal), seed);
      } catch (error) {
        const state = libraryErrorState(error, 'select_all');
        if (state.action === 'aborted' || controller.signal.aborted) throw new DOMException('Selection cancelled.', 'AbortError');
        throw new Error(state.message && state.code !== 'contract' ? state.message
          : 'The complete matching catalogue could not be verified. Existing selection is kept.');
      }
      const still = current.current;
      if (controller.signal.aborted || still.key !== args.scopeKey || still.payload?.browse.session_key !== seed.browse.session_key) {
        throw new DOMException('Selection cancelled because the catalogue changed.', 'AbortError');
      }
      onProgress({ received: ids.length, total });
      const next = { ...still, frozenIds: { sessionKey: seed.browse.session_key, ids } };
      current.current = next; setSnapshot(next);
      return selectionRecords(ids, still.payload!.videos, args.videoType);
    } finally {
      signal.removeEventListener('abort', cancel);
      if (selectionController.current === controller) selectionController.current = null;
    }
  }, []);

  const visible = snapshot.key === inputs.scopeKey ? snapshot : initial;
  // Keep frozen transport rows/indexes intact for cursor adjacency. Only this
  // presentation copy loses expired rows; the next read opens a new session.
  const payload = visible.payload ? sourceContainer({ ...visible.payload, videos: freshSourceRows(visible.payload.videos) }) : undefined;
  let state: LibraryVideosState;
  if (visible.initialError) state = { kind: visible.incompatible ? 'incompatible' : 'error', scopeKey: inputs.scopeKey, message: visible.initialError, previous: payload };
  else if (payload) state = { kind: visible.initialLoading ? 'updating' : 'ready', scopeKey: inputs.scopeKey, payload };
  else state = { kind: 'loading', scopeKey: inputs.scopeKey };

  return {
    state,
    /** The frozen session's identity (`session_key`): facets, Inspector and the selection follow it. */
    sessionKey: visible.payload?.browse.session_key,
    /** Latest `frozen_ref` of the session; read at call time (tokens differ per page). */
    frozenRef: visible.payload?.browse.frozen_ref,
    /** A new session is being built (Inspector panels wait). */
    pending: visible.initialLoading,
    preparing: Boolean(visible.preparing),
    translated: visible.translated,
    /** What Library executed for its first page. */
    executed: visible.request,
    errorPath: visible.errorPath,
    blocked: visible.blocked,
    frozenIds: visible.frozenIds && visible.frozenIds.sessionKey === visible.payload?.browse.session_key ? visible.frozenIds.ids : undefined,
    collectMatching,
    hasMore: Boolean(visible.payload?.browse.next_cursor),
    hasPrevious: Boolean(visible.payload?.browse.previous_cursor),
    isLoadingMore: visible.isLoadingMore,
    loadMoreError: visible.loadMoreError,
    loadMoreErrorDirection: visible.loadMoreErrorDirection,
    expired: visible.expired,
    restoreAnchor: visible.restoreAnchor,
    onLoadMore: useCallback(() => { void request('next'); }, [request]),
    onLoadPrevious: useCallback(() => { void request('previous'); }, [request]),
    onAnchorChange: useCallback((anchor: CatalogueAnchor) => {
      if (latest.current.selectionScopeKey === inputs.selectionScopeKey && anchor.videoId && Number.isFinite(anchor.offsetPx)) {
        anchorMemory.current = { scope: inputs.selectionScopeKey, anchor };
      }
    }, [inputs.selectionScopeKey]),
  };
}

export type LibraryBrowse = ReturnType<typeof useCatalogueBrowse>;
