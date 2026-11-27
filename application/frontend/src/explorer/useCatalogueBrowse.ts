import { sourceContainer } from '../sourceLifetime.ts';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  REFRESH_AHEAD_MS, REFRESH_BUDGET_MS, REFRESH_CHUNK, REFRESH_TICK_MS, browserRefreshEnvironment, refreshChunks, refreshDue,
  refreshFailed, refreshOrder, refreshPause, refreshProblemText, type RefreshEnvironment, type RefreshMemory, type RefreshReason,
} from './catalogueRefresh.ts';
import type { CatalogueAnchor, CatalogueRestoreAnchor, BrowseDirection } from './catalogueBrowse';
import { CatalogueRequestGate } from './catalogueBrowse';
import type { ResourceState } from './model';
import type { CollectMatching } from './catalogueSelection';
import { catalogueUsesGrowthFilters, type CatalogueConditions } from './catalogueQuery';
import { videoNoun } from './format';
import { ROUTES, frozenIdsRequest, type LibraryQueryRequest, type LibraryVideoType, type TranslatedQuery } from './libraryData/wire.ts';
import { planLibraryOpen, type TranslateFunction } from './libraryData/savedLibrary.ts';
import {
  LIBRARY_ERROR_TEXT, PREPARING_DELAY_MS, PREPARING_RETRIES, libraryErrorState, orderLimitText, type LibraryErrorState,
} from './libraryData/errors.ts';
import type { LibraryGates } from './libraryData/capabilities.ts';
import {
  expiringCatalogueIds, libraryFrozenFields, libraryGrowthFields, listHasMore, listPosition, listSlice, mergeLibraryPages, mergeLibrarySlice,
  nextCatalogueExpiry, parseFrozenIds, parseLibraryPage, refreshLibraryRows, selectionRecords, shownCatalogueVideos,
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
  /** Channel + type + conditions + sort + zone + reload token: one browse request. */
  scopeKey: string;
  /**
   * Channel + type + conditions + sort + zone: one list. Its remembered card survives Reload and
   * "Refresh order" only; another sort, filter or format opens the new list at its top. Default: scopeKey.
   */
  listKey?: string;
  /** Channel + type + conditions (without sort): the selection. */
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
  /** The clocks and timers of the background update (default: the browser's). */
  refreshEnvironment?: RefreshEnvironment;
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
  anchorMissing?: boolean;
  /** Loaded cards a background read no longer returned (they stopped matching or are no longer available): they stay. */
  departed?: ReadonlySet<string>;
  /** The last background update failed: said once in words; the cards are unchanged. */
  refreshProblem?: string;
};
/** The background update of one read of the list. */
type RefreshState = RefreshMemory & {
  /** A loaded value ended since the last pass: read again soon. */
  wanted: boolean;
  /** When each loaded row was last read (monotonic). */
  readAt: Map<string, number>;
  controller: AbortController | null;
  /** Counts the list's own requests: a background reply from before one is never applied. */
  epoch: number;
};

const RENDER_FIELDS_MESSAGE = 'The video catalogue reply could not be verified.';
/** A list past the ordered-list limit continues from its last card; that card no longer matches. */
export const LIST_CHANGED_MESSAGE = 'The list changed while you were scrolling. Use Refresh order to continue.';

/** "Send selected" of the whole list: the workshop's fixed list no longer holds exactly the selected videos. */
export function selectionChangedText(selected: number, now: number): string {
  return `The list changed since you selected it: ${selected.toLocaleString('en')} selected, ${now.toLocaleString('en')} match now. `
    + 'Nothing was sent. Use Refresh order and select again, or Open in research workshop for the current list.';
}

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
 * One list per query: the first current page brings the ordered IDs of every matching video, later
 * pages are slices of that list (nothing skipped or repeated while counters change), and the list is
 * never restarted by time alone. Past the server's limit the list continues page by page and says so.
 *
 * Loaded cards stay for as long as the page is open. While the tab is shown, the loaded rows are read again
 * in the background by their IDs (catalogueRefresh.ts), and newer values take the place of older ones; the
 * order, the window and the list change only when the list itself is read again (Refresh order, Reload,
 * another sort or filter).
 */
export function useCatalogueBrowse(inputs: Inputs) {
  const latest = useRef(inputs);
  latest.current = inputs;
  const listKey = inputs.listKey ?? inputs.scopeKey;
  const initial: Snapshot = { key: inputs.scopeKey, initialLoading: false, isLoadingMore: false, expired: false };
  const [snapshot, setSnapshot] = useState<Snapshot>(initial);
  const [paints, repaint] = useState(0);
  const current = useRef(snapshot);
  const environment = inputs.refreshEnvironment ?? browserRefreshEnvironment;
  const clock = useCallback(() => ({ now: environment.now(), monotonic: environment.monotonic() }), [environment]);
  const refreshing = useRef<RefreshState>({ last: clock(), lastFull: clock(), started: clock(), failures: 0, paused: false, wanted: false, readAt: new Map(), controller: null, epoch: 0 });
  /** The rows of the last render: a difference means a value ended since (a timer that fired late, a computer that slept). */
  const rendered = useRef<{ payload: LibraryVideosPayload; videos: unknown } | undefined>(undefined);
  const accessRevision = useRef(0);
  const gate = useRef(new CatalogueRequestGate());
  const handoffController = useRef<AbortController | null>(null);
  const requestController = useRef<AbortController | null>(null);
  // The remembered card belongs to one list (sort included): a new sort, filter or format starts at the top.
  // `atTop`: the remembered card was the list's first video; reading the list again then starts at its top,
  // so videos that arrived above it are in view.
  const anchorMemory = useRef<{ scope: string; anchor?: CatalogueAnchor; atTop?: boolean }>({ scope: listKey });
  if (anchorMemory.current.scope !== listKey) anchorMemory.current = { scope: listKey };

  const publish = useCallback((value: Snapshot) => {
    if (current.current.key !== value.key || current.current.payload?.browse.query_fingerprint !== value.payload?.browse.query_fingerprint) {
      handoffController.current?.abort();
    }
    current.current = value; setSnapshot(value);
  }, []);
  const discardAccess = useCallback((scope: string, error: unknown, cancelBrowse = false) => {
    if (scope !== latest.current.scopeKey || current.current.key !== scope) return false;
    const state = libraryErrorState(error, 'videos_first');
    const status = error && typeof error === 'object' ? (error as { status?: number }).status : undefined;
    if (status !== 401 && status !== 403 && !['cursor_context_changed', 'policy_denied', 'grant_required', 'source_unavailable',
      'field_disabled', 'dependency_disabled', 'format_turned_off'].includes(state.code)) return false;
    if (cancelBrowse) {
      requestController.current?.abort();
      gate.current.reset();
    }
    if (current.current.payload) accessRevision.current += 1;
    anchorMemory.current = { scope: latest.current.listKey ?? latest.current.scopeKey };
    publish({ key: scope, initialLoading: false, isLoadingMore: false, expired: false,
      initialError: state.code === 'cursor_context_changed' ? LIBRARY_ERROR_TEXT.accessChanged : firstPageMessage(state, error) });
    return true;
  }, [publish]);
  const request = useCallback(async (direction?: BrowseDirection, fromStart = false) => {
    const args = latest.current;
    if (!args.enabled) return;
    // A new request replaces the previous one's "more" error: every outcome below starts from `base`
    // (a successful Retry kept the old error, which also kept automatic loading paused).
    const base: Snapshot = { ...(current.current.key === args.scopeKey ? current.current : {
      key: args.scopeKey, initialLoading: false, isLoadingMore: false, expired: false,
    } as Snapshot), loadMoreError: undefined, loadMoreErrorDirection: undefined };
    // The ordered list pages by slices of its IDs; past its limit, forward by the keyset cursor.
    const listed = Boolean(base.payload?.browse.order?.ids);
    const slice = direction && base.payload && base.request && listed ? listSlice(base.payload, direction, base.request.page.size) : [];
    const cursor = direction && !listed && direction === 'next' ? base.payload?.browse.next_cursor : undefined;
    if (direction && (!base.payload || !base.request || (listed ? !slice.length : !cursor))) return;
    const ticket = gate.current.begin();
    if (!ticket) return;
    // The list's own request comes first: the background pass stops, and a reply it still gets is not applied.
    refreshing.current.epoch += 1;
    refreshing.current.controller?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    const isCurrent = () => !controller.signal.aborted && latest.current.scopeKey === args.scopeKey && gate.current.accepts(ticket);
    publish({ ...base, initialLoading: !direction, preparing: false, initialError: undefined, incompatible: false, errorPath: undefined, blocked: undefined,
      isLoadingMore: Boolean(direction), loadMoreError: undefined, loadMoreErrorDirection: direction });
    const signal = controller.signal;
    try {
      if (direction) {
        const loaded = base.payload!, size = base.request!.page.size;
        // Past the limit a keyset cursor lives as long as its page; then the list resumes at its last card. The page
        // keeps its size (the size is part of the query, and its next cursor continues it): the card itself is dropped.
        const last = loaded.videos[loaded.videos.length - 1]?.video_id;
        const resume = !listed && Date.parse(loaded.browse.expires_at) <= Date.now() && last ? last : undefined;
        const pageRequest: LibraryQueryRequest['page'] = listed ? { size, growth_lifetime: 'page', ids: slice }
          : resume ? { size, growth_lifetime: 'page', anchor_id: resume } : { size, growth_lifetime: 'page', after: cursor! };
        // The same current query (select, sort, zone): a slice or cursor never crosses queries.
        const next: LibraryQueryRequest = { ...base.request!, page: pageRequest };
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
        let payload: LibraryVideosPayload;
        if (listed) payload = mergeLibrarySlice(loaded, page, direction, slice);
        else if (resume) {
          if (page.browse.anchor_video_id !== resume) {
            publish({ ...base, initialLoading: false, isLoadingMore: false, loadMoreError: LIST_CHANGED_MESSAGE, loadMoreErrorDirection: direction });
            return;
          }
          payload = mergeLibraryPages(loaded, { ...page, videos: page.videos.slice(1) }, 'next');
        } else payload = mergeLibraryPages(loaded, page, 'next');
        const read = environment.monotonic();
        for (const video of page.videos) refreshing.current.readAt.set(video.video_id, read);
        publish({ ...base, payload, initialLoading: false, isLoadingMore: false, expired: false });
        args.onAnnouncement(`${payload.videos.length} ${videoNoun(payload.videos.length)} loaded.`);
        return;
      }
      // Translate once for the first current page; it also brings the ordered list.
      const anchor = fromStart || anchorMemory.current.atTop ? undefined : anchorMemory.current.anchor;
      const fields = libraryFrozenFields(args.gates);
      let growthFields = libraryGrowthFields(args.growthAllowed);
      let retried = false, preparing = 0;
      let page: LibraryVideosPayload | undefined, translated: TranslatedQuery | undefined, firstRequest: LibraryQueryRequest | undefined;
      while (!page) {
        const plan = await planLibraryOpen(args.translate, {
          channelId: args.channelId, conditions: args.conditions, videoType: args.videoType, viewerZone: args.timeZone,
          growthFields, fields,
        }, signal);
        if (!isCurrent()) return;
        if (plan.status === 'blocked') throw new BlockedTranslation(plan.blockedText || plan.message);
        try {
          firstRequest = { ...plan.request, consistency: 'current', page: { size: plan.request.page.size, growth_lifetime: 'page', order: 'ids',
            ...(anchor ? { anchor_id: anchor.videoId } : {}) } };
          page = parseLibraryPage(await args.request(ROUTES.execute, firstRequest, signal), { request: firstRequest, channelId: args.channelId });
          translated = plan.translation.query;
        } catch (error) {
          if (!isCurrent()) return;
          const state = libraryErrorState(error, 'videos_first');
          // A reason to drop growth can arrive while old cards are still visible.
          if (discardAccess(args.scopeKey, error)) {
            base.payload = undefined; base.translated = undefined; base.request = undefined;
          }
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
        ? { ...anchor, requestKey: `${args.scopeKey}:${page.browse.session_key}` } : undefined;
      // A new read of the list: its background update starts over, one period from now.
      const read = clock();
      refreshing.current = { last: read, lastFull: read, started: read, failures: 0, paused: false, wanted: false, controller: null, epoch: refreshing.current.epoch,
        readAt: new Map(page.videos.map((video) => [video.video_id, read.monotonic])) };
      publish({ key: args.scopeKey, payload: page, translated, request: firstRequest, initialLoading: false, isLoadingMore: false,
        expired: false, restoreAnchor: restored, anchorMissing: Boolean(anchor && !restored) });
      args.onAnnouncement(`${page.videos.length} ${videoNoun(page.videos.length)} loaded.`);
    } catch (error) {
      if (!isCurrent()) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (error instanceof BlockedTranslation) {
        publish({ ...base, initialLoading: false, isLoadingMore: false, preparing: false, blocked: error.message });
        return;
      }
      const state = libraryErrorState(error, direction ? 'videos_more' : 'videos_first');
      if (state.action === 'aborted') return;
      if (discardAccess(args.scopeKey, error)) return;
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
  }, [publish, discardAccess, environment, clock]);

  /**
   * One background pass: the loaded rows are read again by their IDs, a read at a time, and put in their
   * places. Nothing else changes: not the order, the window, the list, the selection or the scroll position.
   * A failure leaves every card as it was and is said in words; the retries are few and far apart.
   * `soon`: one read only (the rows with an ending value and the cards around the viewed one).
   */
  const refresh = useCallback(async (kind: 'full' | 'soon') => {
    const args = latest.current, memory = refreshing.current, start = current.current;
    const loaded = start.payload;
    if (memory.controller || !args.enabled || start.key !== args.scopeKey || !loaded || !start.request
      || loaded.browse.consistency !== 'current' || start.initialLoading || start.isLoadingMore || start.initialError
      || !loaded.videos.length || !environment.visible()) return;
    const controller = new AbortController();
    memory.controller = controller;
    // Also a pass that is cancelled counts as started: the next one waits its minute and reads the other rows first.
    memory.started = clock();
    const epoch = memory.epoch, session = loaded.browse.session_key;
    // The same read of the same list, and no request of the list since this pass began.
    const live = () => !controller.signal.aborted && refreshing.current === memory && memory.epoch === epoch
      && latest.current.scopeKey === args.scopeKey && current.current.key === args.scopeKey
      && current.current.payload?.browse.session_key === session;
    try {
      const at = clock();
      const urgent = expiringCatalogueIds(loaded, REFRESH_AHEAD_MS, at.now, at.monotonic);
      for (const id of start.departed ?? []) urgent.delete(id);
      const order = refreshOrder(loaded.videos.map((video) => video.video_id),
        { near: anchorMemory.current.anchor?.videoId, urgent, readAt: memory.readAt, now: kind === 'full' ? at.monotonic : undefined });
      const chunks = refreshChunks(order).slice(0, kind === 'soon' ? 1 : undefined);
      let spent = 0;
      for (const [index, ids] of chunks.entries()) {
        if (!live() || !environment.visible()) break;
        // The same query (membership, fields, sort, zone) for exactly these loaded rows.
        const body: LibraryQueryRequest = { ...start.request, page: { size: REFRESH_CHUNK, growth_lifetime: 'page', ids } };
        const began = environment.monotonic();
        const page = parseLibraryPage(await args.request(ROUTES.execute, body, controller.signal), { request: body, channelId: args.channelId });
        const took = environment.monotonic() - began;
        spent += took;
        if (!live()) return;
        const now = current.current;
        const result = refreshLibraryRows(now.payload!, page, ids);
        const departed = new Set(now.departed);
        for (const id of result.absent) departed.add(id);
        for (const video of page.videos) departed.delete(video.video_id);
        for (const id of ids) memory.readAt.set(id, began);
        publish({ ...now, payload: result.payload, departed: departed.size ? departed : undefined, refreshProblem: undefined });
        if (index === chunks.length - 1 || (spent >= REFRESH_BUDGET_MS && index > 0)) break;
        await new Promise<void>((resolve) => {
          const cancel = environment.after(refreshPause(took), () => { controller.signal.removeEventListener('abort', stop); resolve(); });
          const stop = () => { cancel(); resolve(); };
          controller.signal.addEventListener('abort', stop, { once: true });
        });
      }
      if (!live()) return;
      memory.last = clock(); memory.failures = 0; memory.wanted = false;
      if (kind === 'full') memory.lastFull = memory.last;
      if (current.current.refreshProblem) publish({ ...current.current, refreshProblem: undefined });
    } catch (error) {
      if (!live()) return;
      const state = libraryErrorState(error, 'videos_more');
      if (state.action === 'aborted') return;
      // Withdrawn access removes the loaded data, as for every other read of the list.
      if (discardAccess(args.scopeKey, error, true)) return;
      Object.assign(memory, refreshFailed(memory, clock(), state.action === 'incompatible'));
      publish({ ...current.current, refreshProblem: refreshProblemText(memory) });
    } finally {
      if (memory.controller === controller) memory.controller = null;
    }
  }, [publish, discardAccess, environment, clock]);

  /** Start a pass when one is due. A value that ended asks for one soon; otherwise the period decides. */
  const checkRefresh = useCallback((reason: RefreshReason) => {
    const memory = refreshing.current;
    if (reason === 'expiry') memory.wanted = true;
    if (!latest.current.enabled || !current.current.payload || !environment.visible()) return;
    const due = refreshDue(memory, clock(), memory.wanted && reason === 'tick' ? 'expiry' : reason);
    if (due) void refresh(due);
  }, [refresh, environment, clock]);

  // While the Videos tab is open: a regular check, and one when the tab is shown again. The check also
  // repaints when a value ended that no timer reported (a timer that fired late, a computer that slept).
  useEffect(() => {
    if (!inputs.enabled) return undefined;
    const check = (shown: boolean) => {
      const last = rendered.current;
      if (last && last.payload === current.current.payload
        && shownCatalogueVideos(last.payload, environment.now(), environment.monotonic()) !== last.videos) {
        repaint((value) => value + 1);
        refreshing.current.wanted = true;
      }
      checkRefresh(shown ? 'visible' : 'tick');
    };
    // Coming back to the Videos tab from another tab of the page counts as the tab shown again.
    check(true);
    return environment.watch(check, REFRESH_TICK_MS);
  }, [inputs.enabled, environment, checkRefresh]);

  // The next end of a loaded value: the card then shows that value as expired, and the row is read again soon.
  useEffect(() => {
    if (!inputs.enabled || !snapshot.payload) return undefined;
    const wait = nextCatalogueExpiry(snapshot.payload, environment.now(), environment.monotonic());
    if (!Number.isFinite(wait)) return undefined;
    return environment.after(wait + 1, () => { repaint((value) => value + 1); checkRefresh('expiry'); });
  }, [inputs.enabled, snapshot.payload, paints, environment, checkRefresh]);

  useEffect(() => {
    gate.current.reset();
    requestController.current?.abort();
    requestController.current = null;
    refreshing.current.epoch += 1;
    refreshing.current.controller?.abort();
    if (current.current.key !== inputs.scopeKey) {
      publish({ key: inputs.scopeKey, initialLoading: false, isLoadingMore: false, expired: false });
    } else if (current.current.initialLoading || current.current.isLoadingMore) {
      publish({ ...current.current, initialLoading: false, isLoadingMore: false });
    }
    if (inputs.enabled && !current.current.payload && !current.current.initialError && !current.current.expired) void request();
    return () => {
      requestController.current?.abort(); handoffController.current?.abort(); gate.current.reset();
      refreshing.current.epoch += 1; refreshing.current.controller?.abort();
    };
  }, [inputs.scopeKey, inputs.enabled, publish, request]);

  /** Fix the entire translated query only for "Open in research workshop". */
  const freeze = useCallback(async (signal: AbortSignal) => {
    const args = latest.current, base = current.current;
    if (!args.enabled || !base.payload || !base.request || base.key !== args.scopeKey || base.initialLoading || base.expired) {
      throw new Error('Load the catalogue before requesting a fixed list.');
    }
    const query: LibraryQueryRequest = { ...base.request, consistency: 'frozen', page: { size: base.request.page.size } };
    const reply = await args.request(ROUTES.execute, query, signal).catch(error => {
      if (!signal.aborted) discardAccess(base.key, error, true);
      throw error;
    });
    if (signal.aborted || current.current.payload?.browse.query_fingerprint !== base.payload.browse.query_fingerprint || latest.current.scopeKey !== base.key) {
      throw new DOMException('Catalogue changed.', 'AbortError');
    }
    return parseLibraryPage(reply, { request: query, channelId: args.channelId });
  }, [discardAccess]);

  /**
   * The fixed list of this query for the research workshop. With `selected`, it must hold exactly those
   * videos ("Send selected" after "Select all matching"): its IDs are read and compared, and a list that
   * changed since the selection is refused in plain words; nothing else is sent in its place.
   */
  const freezeForWorkshop = useCallback(async (selected?: ReadonlySet<string>) => {
    if (handoffController.current) throw new Error('A fixed list is already being prepared.');
    const controller = new AbortController();
    handoffController.current = controller;
    try {
      const fixed = await freeze(controller.signal);
      if (selected && fixed.browse.consistency === 'frozen') {
        const ids = parseFrozenIds(await latest.current.request(ROUTES.frozenIds, frozenIdsRequest(fixed.browse.frozen_ref), controller.signal), fixed);
        if (controller.signal.aborted) throw new DOMException('Catalogue changed.', 'AbortError');
        if (ids.length !== selected.size || ids.some((id) => !selected.has(id))) {
          throw new Error(selectionChangedText(selected.size, ids.length));
        }
      }
      return fixed;
    }
    finally { if (handoffController.current === controller) handoffController.current = null; }
  }, [freeze]);

  /** "Select all matching": every video of the loaded list, in its order; no further read. */
  const collectMatching: CollectMatching = useCallback(async (signal, onProgress) => {
    const args = latest.current, base = current.current;
    const seed = base.payload;
    if (!args.enabled || base.key !== args.scopeKey || !seed || base.expired || base.initialLoading) {
      throw new Error('Load the current catalogue before selecting all matching videos. Existing selection is kept.');
    }
    const order = seed.browse.order;
    if (!order?.ids) {
      throw new Error(order ? `${orderLimitText(order.total, order.limit)} Existing selection is kept.`
        : 'The complete matching catalogue could not be verified. Existing selection is kept.');
    }
    if (signal.aborted) throw new DOMException('Selection cancelled.', 'AbortError');
    onProgress({ received: order.ids.length, total: order.total });
    return selectionRecords(order.ids, seed.videos, args.videoType);
  }, []);

  const visible = snapshot.key === inputs.scopeKey ? snapshot : initial;
  // Every loaded row is shown, in its place. Only this presentation copy shows a value past its own lifetime
  // as expired; the next background read, or Refresh order, brings what the program has by then.
  const shownVideos = visible.payload ? shownCatalogueVideos(visible.payload, environment.now(), environment.monotonic()) : undefined;
  rendered.current = snapshot.payload ? { payload: snapshot.payload,
    videos: shownCatalogueVideos(snapshot.payload, environment.now(), environment.monotonic()) } : undefined;
  const payload = visible.payload && shownVideos ? sourceContainer({ ...visible.payload, videos: shownVideos }) : undefined;
  let state: LibraryVideosState;
  if (visible.initialError) state = { kind: visible.incompatible ? 'incompatible' : 'error', scopeKey: inputs.scopeKey, message: visible.initialError, previous: payload };
  else if (payload) state = { kind: visible.initialLoading ? 'updating' : 'ready', scopeKey: inputs.scopeKey, payload };
  else state = { kind: 'loading', scopeKey: inputs.scopeKey };
  const order = visible.payload?.browse.order;

  return {
    state,
    accessRevision: accessRevision.current,
    // The read's identity refreshes live Inspector panels and facets.
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
    /** The ordered list of this read: its IDs (null past the limit), total and limit. */
    order,
    /** Every video ID of the list, in order (Select all matching, Send selected); none past the limit. */
    listIds: order?.ids ?? undefined,
    collectMatching, freezeForWorkshop,
    hasMore: listHasMore(visible.payload, 'next'),
    hasPrevious: listHasMore(visible.payload, 'previous'),
    isLoadingMore: visible.isLoadingMore,
    loadMoreError: visible.loadMoreError,
    loadMoreErrorDirection: visible.loadMoreErrorDirection,
    expired: visible.expired,
    restoreAnchor: visible.restoreAnchor,
    anchorMissing: visible.anchorMissing,
    /** The loaded part of the list starts below its top ("Show from the beginning"). */
    // A list without its IDs (past the limit) cannot load what is above a remembered card: the button says so.
    startsBelowTop: Boolean(visible.payload && (visible.payload.browse.start_index > 0
      || (!visible.payload.browse.order?.ids && visible.payload.browse.anchor_video_id !== null))),
    /** Shown cards that no longer match these filters or are no longer available (found by a background read). */
    departedShown: visible.departed?.size ?? 0,
    /** The last background update failed (the cards are unchanged), in words. */
    refreshProblem: visible.refreshProblem,
    /** Videos of the loaded part that left the results since the list was read. */
    leftResults: visible.payload?.browse.left_results ?? 0,
    onStartFromBeginning: useCallback(() => {
      anchorMemory.current = { scope: latest.current.listKey ?? latest.current.scopeKey };
      void request(undefined, true);
    }, [request]),
    /** Read the list again (order, total, values) at the remembered card. */
    onRefreshOrder: useCallback(() => { void request(); }, [request]),
    onLoadMore: useCallback(() => { void request('next'); }, [request]),
    onLoadPrevious: useCallback(() => { void request('previous'); }, [request]),
    onAnchorChange: useCallback((anchor: CatalogueAnchor) => {
      const scope = latest.current.listKey ?? latest.current.scopeKey;
      if (scope === listKey && anchor.videoId && Number.isFinite(anchor.offsetPx)) {
        const loaded = current.current.key === latest.current.scopeKey ? current.current.payload : undefined;
        const atTop = loaded?.browse.order?.ids ? listPosition(loaded.browse.order, anchor.videoId) === 0
          : Boolean(loaded && loaded.browse.start_index === 0 && loaded.browse.anchor_video_id === null && loaded.videos[0]?.video_id === anchor.videoId);
        anchorMemory.current = { scope: listKey, anchor, atTop };
      }
    }, [listKey]),
  };
}

export type LibraryBrowse = ReturnType<typeof useCatalogueBrowse>;
