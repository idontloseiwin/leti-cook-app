import { useSourceExpiry } from '../sourceLifetimeReact';
import { freshSourceRows, sourceContainer, sourceExpired } from '../sourceLifetime.ts';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChannelExplorerQuery } from '../types';
import { ExplorerApiError, isAbortError, isContractError, loadExplorerVideoGrowthBrowse, loadRawExplorerBrowse } from './api';
import {
  CatalogueRequestGate, mergeCatalogueBrowse,
  type BrowseDirection, type CatalogueAnchor, type CatalogueBrowsePayload, type CatalogueRestoreAnchor,
} from './catalogueBrowse';
import type { VideoCatalogueState } from './model';
import { collectCatalogueSelection, type CollectMatching } from './catalogueSelection';
import { catalogueRequestNeedsGrowth } from './catalogueQuery';
import { videoNoun } from './format';

type Inputs = {
  channelId: string;
  scopeKey: string;
  selectionScopeKey: string;
  query: ChannelExplorerQuery;
  enabled: boolean;
  preferGrowth: boolean;
  onGrowthUnavailable: () => void;
  onAnnouncement: (message: string) => void;
};
type Snapshot = {
  key: string;
  payload?: CatalogueBrowsePayload;
  initialLoading: boolean;
  initialError?: string;
  incompatible?: boolean;
  isLoadingMore: boolean;
  loadMoreError?: string;
  loadMoreErrorDirection?: BrowseDirection;
  expired: boolean;
  restoreAnchor?: CatalogueRestoreAnchor;
};

/** One contiguous slice of one server session; transport cursors never enter URL state. */
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
    if (current.current.key !== value.key || current.current.payload?.browse.session_id !== value.payload?.browse.session_id) selectionController.current?.abort();
    current.current = value; setSnapshot(value);
  }, []);
  const request = useCallback(async (direction?: BrowseDirection) => {
    const args = latest.current;
    if (!args.enabled) return;
    const base = current.current.key === args.scopeKey ? current.current : {
      key: args.scopeKey, initialLoading: false, isLoadingMore: false, expired: false,
    } as Snapshot;
    const cursor = direction && base.payload?.browse[direction === 'next' ? 'next_cursor' : 'previous_cursor'];
    if (direction && (!base.payload || !cursor)) return;
    const ticket = gate.current.begin();
    if (!ticket) return;
    const controller = new AbortController();
    requestController.current = controller;
    const isCurrent = () => !controller.signal.aborted && latest.current.scopeKey === args.scopeKey && gate.current.accepts(ticket);
    publish({ ...base, initialLoading: !direction, initialError: undefined, incompatible: false,
      isLoadingMore: Boolean(direction), loadMoreError: undefined, loadMoreErrorDirection: direction });
    const anchor = !direction ? anchorMemory.current.anchor : undefined;
    const query = { ...args.query, offset: 0, ...(cursor ? { cursor } : {}), ...(!direction && anchor ? { anchor_video_id: anchor.videoId } : {}) };
    const usesGrowth = base.payload
      ? base.payload.source?.kind === 'derived_video_view_growth_from_stored_snapshots'
      : args.preferGrowth;
    let replacement = !direction;
    try {
      if (catalogueRequestNeedsGrowth(args.query) && !usesGrowth) {
        throw new Error('Growth data is required for these conditions. Reload or remove the growth filters/sort; your conditions are kept.');
      }
      let page: CatalogueBrowsePayload;
      try {
        page = await (usesGrowth ? loadExplorerVideoGrowthBrowse : loadRawExplorerBrowse)(args.channelId, query, controller.signal);
      } catch (error) {
        if (!isCurrent() || isAbortError(error)) return;
        // Expiration is not a source failure: preserve the immutable slice until Reload.
        if (error instanceof ExplorerApiError && error.status === 410) throw error;
        if (!usesGrowth || !(error instanceof ExplorerApiError || isContractError(error))) throw error;
        args.onGrowthUnavailable();
        if (catalogueRequestNeedsGrowth(args.query)) throw error;
        args.onAnnouncement('Growth data is unavailable. Loading the current catalogue instead.');
        const fallbackAnchor = anchorMemory.current.anchor;
        page = await loadRawExplorerBrowse(args.channelId, {
          ...args.query, offset: 0, ...(fallbackAnchor ? { anchor_video_id: fallbackAnchor.videoId } : {}),
        }, controller.signal);
        replacement = true;
      }
      if (!isCurrent()) return;
      const payload = replacement || !base.payload ? page : mergeCatalogueBrowse(base.payload, page, direction!);
      const restored = replacement ? anchorMemory.current.anchor : undefined;
      publish({ key: args.scopeKey, payload, initialLoading: false, isLoadingMore: false, expired: false,
        restoreAnchor: restored && page.videos.some((video) => video.video_id === restored.videoId)
          ? { ...restored, requestKey: `${args.scopeKey}:${page.browse.session_id}` }
          : base.restoreAnchor });
      args.onAnnouncement(`${payload.videos.length} of ${payload.browse.total_count} ${videoNoun(payload.browse.total_count)} loaded.`);
    } catch (error) {
      if (!isCurrent() || isAbortError(error)) return;
      const expired = error instanceof ExplorerApiError && error.status === 410;
      const message = expired ? 'This catalogue session has expired. Reload to continue; your selection is kept.'
        : error instanceof Error ? error.message : String(error);
      publish({ ...base, initialLoading: false, isLoadingMore: false, expired,
        ...(direction ? { loadMoreError: message, loadMoreErrorDirection: direction }
          : { initialError: message, incompatible: isContractError(error) }) });
    } finally {
      gate.current.finish(ticket);
      if (requestController.current === controller) requestController.current = null;
    }
  }, [publish]);
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

  const collectMatching: CollectMatching = useCallback(async (signal, onProgress) => {
    const args = latest.current, base = current.current;
    const seed = base.payload;
    if (!args.enabled || base.key !== args.scopeKey || !seed || base.expired || base.initialLoading) {
      throw new Error('Load the current catalogue before selecting all matching videos. Existing selection is kept.');
    }
    if (catalogueRequestNeedsGrowth(args.query) && seed.source?.kind !== 'derived_video_view_growth_from_stored_snapshots') {
      throw new Error('These conditions require a growth catalogue. Existing selection is kept.');
    }
    if (selectionController.current) throw new Error('A complete selection is already being loaded.');
    const controller = new AbortController();
    selectionController.current = controller;
    const cancel = () => controller.abort();
    if (signal.aborted) cancel();
    signal.addEventListener('abort', cancel, { once: true });
    const load = seed.source?.kind === 'derived_video_view_growth_from_stored_snapshots'
      ? loadExplorerVideoGrowthBrowse : loadRawExplorerBrowse;
    try {
      return await collectCatalogueSelection(seed,
        (cursor) => load(args.channelId, { ...args.query, offset: 0, cursor }, controller.signal),
        controller.signal,
        () => latest.current.enabled && latest.current.scopeKey === args.scopeKey
          && current.current.key === args.scopeKey && current.current.payload?.browse.session_id === seed.browse.session_id,
        onProgress);
    } finally {
      signal.removeEventListener('abort', cancel);
      if (selectionController.current === controller) selectionController.current = null;
    }
  }, []);

  const visible = snapshot.key === inputs.scopeKey ? snapshot : initial;
  // Keep frozen transport rows/indexes intact for cursor adjacency. Only this
  // presentation copy loses expired rows; the next read opens a new session.
  const payload = visible.payload ? sourceContainer({ ...visible.payload,
    videos: freshSourceRows(visible.payload.videos),
    channel: sourceExpired(visible.payload.channel) ? { channel_id: inputs.channelId } : visible.payload.channel,
  }) as CatalogueBrowsePayload : undefined;
  let state: VideoCatalogueState;
  if (visible.initialError) state = { kind: visible.incompatible ? 'incompatible' : 'error', scopeKey: inputs.scopeKey, message: visible.initialError, previous: payload };
  else if (payload) state = { kind: visible.initialLoading ? 'updating' : 'ready', scopeKey: inputs.scopeKey, payload };
  else state = { kind: 'loading', scopeKey: inputs.scopeKey };

  return {
    state,
    sessionId: visible.payload?.browse.session_id,
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
