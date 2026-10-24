import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { ChannelExplorerVideo } from '../types';
import { YouTubeAttribution } from '../components/YouTubeAttribution';
import { isAbortError } from './api';
import { CollectionTransferDialog } from './CollectionTransferDialog';
import { VideoCard, VideoInspector } from './ExplorerVideos';
import { VirtualVideoGrid, type VirtualVideoGridHandle } from './VirtualVideoGrid';
import type { VideoGridAnchor } from './videoGridGeometry';
import { COLLECTION_SOURCE_REASONS, COLLECTIONS_INDEX, collectionSourceDelay, expireCollectionSources, loadCollectionMembers, mergeCollectionMembers, withoutCollectionSources, type CollectionMembersPage, type SavedCollection } from './savedCollections';
import { savedFailure, SavedWorkRequestFence } from './savedWork';
import { videoNoun } from './format';

export function SavedCollectionDetail({ id, navigation, onNavigate }: { id: string; navigation: ReactNode; onNavigate: (path: string) => void }) {
  const [page, setPage] = useState<CollectionMembersPage>();
  const current = useRef<CollectionMembersPage | undefined>(undefined);
  const [busy, setBusy] = useState(false), [mustReload, setMustReload] = useState(false), [error, setError] = useState('');
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [removal, setRemoval] = useState<{ ids: string[]; collection: SavedCollection }>();
  const [inspected, setInspected] = useState<ChannelExplorerVideo | null>(null);
  const grid = useRef<VirtualVideoGridHandle>(null);
  const inspectorReturn = useRef<{ anchor: VideoGridAnchor | null; videoId: string; trigger: 'thumbnail' | 'title' } | undefined>(undefined);
  const fence = useRef(new SavedWorkRequestFence()), controller = useRef<AbortController | undefined>(undefined);
  const lastRefresh = useRef(-Infinity);
  const load = async (cursor?: string, refreshLoaded = false) => {
    const ticket = fence.current.begin(); if (!ticket) return;
    lastRefresh.current = performance.now();
    const request = new AbortController(); controller.current = request; setBusy(true); setError('');
    try {
      let incoming = await loadCollectionMembers(id, cursor, request.signal);
      // Revalidate only the already loaded prefix, sequentially, with the same
      // cancellation/revision fence as paging. Never fetch the whole collection.
      const wanted = refreshLoaded ? current.current?.members.length ?? 0 : 0;
      while (incoming.next_cursor && incoming.members.length < wanted && !request.signal.aborted) {
        const next = await loadCollectionMembers(id, incoming.next_cursor, request.signal);
        incoming = mergeCollectionMembers(incoming, next);
      }
      if (!fence.current.accepts(ticket)) return;
      const previous = current.current && incoming.source_read_at === null ? withoutCollectionSources(current.current) : current.current;
      const result = expireCollectionSources(cursor && previous ? mergeCollectionMembers(previous, incoming) : incoming);
      current.current = result; setPage(result); setMustReload(false);
      setInspected((previous) => previous ? result.members.find((member) => member.video_id === previous.video_id)?.video ?? null : null);
    } catch (failure) {
      if (fence.current.accepts(ticket) && !isAbortError(failure)) {
        const info = savedFailure(failure); setError(info.message);
        if (info.conflict || info.deleted || (cursor && !(failure instanceof Error && 'status' in failure))) setMustReload(true);
      }
    } finally { if (fence.current.accepts(ticket)) { fence.current.finish(ticket); setBusy(false); } }
  };
  useEffect(() => { void load(); return () => { controller.current?.abort(); fence.current.reset(); }; }, [id]);
  useEffect(() => {
    if (!page) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      if (document.hidden) return;
      const remaining = 5_000 - (performance.now() - lastRefresh.current);
      if (remaining > 0) {
        if (refreshTimer === undefined) refreshTimer = setTimeout(() => { refreshTimer = undefined; refresh(); }, remaining);
      } else void load(undefined, true);
    };
    const expire = () => {
      if (!current.current) return;
      const safe = expireCollectionSources(current.current);
      if (safe !== current.current) {
        current.current = safe; setPage(safe);
        setInspected((previous) => previous ? safe.members.find((member) => member.video_id === previous.video_id)?.video ?? null : null);
        refresh();
      }
      if (timer !== undefined) clearTimeout(timer);
      const delay = collectionSourceDelay(safe);
      if (delay !== null) timer = setTimeout(expire, Math.min(2147483647, Math.max(1, delay + 1)));
    };
    expire();
    const visible = () => { expire(); refresh(); };
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('pageshow', visible);
    return () => {
      if (timer !== undefined) clearTimeout(timer);
      if (refreshTimer !== undefined) clearTimeout(refreshTimer);
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('pageshow', visible);
    };
  }, [page]);
  const members = useMemo(() => new Map(page?.members.map((member) => [member.video_id, member]) ?? []), [page]);
  const gridVideos = useMemo(() => page?.members.map((member) => member.video ?? { video_id: member.video_id }) ?? [], [page]);
  const toggle = useCallback((video: ChannelExplorerVideo, checked: boolean) => {
    const videoId = video.video_id; if (!videoId) return;
    setSelected((currentSelection) => { const next = new Set(currentSelection); if (checked) next.add(videoId); else next.delete(videoId); return next; });
  }, []);
  const inspect = useCallback((video: ChannelExplorerVideo, trigger: 'thumbnail' | 'title') => {
    inspectorReturn.current = { anchor: grid.current?.captureAnchor() ?? null, videoId: video.video_id!, trigger };
    setInspected(video);
  }, []);
  const closeInspector = useCallback(() => setInspected(null), []);
  const restoreInspector = useCallback(() => {
    const target = inspectorReturn.current;
    if (target) grid.current?.restore(target.anchor, { videoId: target.videoId, trigger: target.trigger });
  }, []);
  const allLoaded = Boolean(page?.members.length && page.members.every((member) => selected.has(member.video_id)));
  return <main className="saved-views-v2 saved-collection-detail" aria-labelledby="collection-detail-title">
    <header className="saved-views-heading"><h1 id="collection-detail-title">{page?.collection.name || 'Collection'}</h1>{navigation}
      <button type="button" disabled={busy} onClick={() => void load()}>Reload collection</button></header>
    <button type="button" className="collection-back" onClick={() => onNavigate(COLLECTIONS_INDEX)}>All collections</button>
    {error && <p className="saved-work-error" role="alert">{error}{page ? ' Loaded members and selection are kept.' : ''}</p>}
    {mustReload && <p role="status">Collection changed or a write is not confirmed. Reload before continuing.</p>}
    {!page && busy && <p role="status">Loading collection…</p>}
    {page && <>
      <div className="collection-member-controls">
        <span>{page.collection.member_count.toLocaleString('en')} {videoNoun(page.collection.member_count)} · {page.members.length.toLocaleString('en')} loaded</span>
        <button type="button" disabled={!page.members.length} onClick={() => setSelected((currentSelection) => {
          const next = new Set(currentSelection); page.members.forEach((member) => { if (allLoaded) next.delete(member.video_id); else next.add(member.video_id); }); return next;
        })}>{allLoaded ? 'Unselect loaded' : 'Select loaded'}</button>
        {selected.size > 0 && <><span>{selected.size} selected</span><button type="button" disabled={busy || mustReload} onClick={() => setRemoval({ ids: [...selected], collection: page.collection })}>Remove selected</button>
          <button type="button" onClick={() => setSelected(new Set())}>Clear selection</button></>}
      </div>
      <p className="saved-work-hint">Saved selection · available videos show current stored metrics</p>
      {page.collection.member_count === 0 && <div className="saved-views-empty"><h2>This collection is empty</h2><p>Select videos in a channel to add them.</p></div>}
      <VirtualVideoGrid ref={grid} videos={gridVideos} density="large" scopeKey={`collection:${id}`} inspectedVideoId={inspected?.video_id}
        renderCard={(video) => {
          const member = members.get(video.video_id!)!;
          if (member.status === 'available') return <VideoCard key={member.video_id} video={member.video} growthAvailable={false}
            selected={selected.has(member.video_id)} onToggleSelected={toggle} onInspect={inspect} />;
          return <article key={member.video_id} className="explorer-v2-video-card collection-unavailable-member" data-video-id={member.video_id}>
            <label><input type="checkbox" data-grid-focus="selection" checked={selected.has(member.video_id)} onChange={(event) => toggle(video, event.target.checked)} /> {member.reason === 'api_paused' ? 'Select saved video' : 'Select unavailable video'}</label>
            <h2>{member.reason === 'api_paused' ? 'Saved video' : 'Video unavailable'}</h2><code>{member.video_id}</code><p>{COLLECTION_SOURCE_REASONS[member.reason]} The video ID stays saved.</p>
            <button type="button" data-grid-focus="remove" disabled={busy || mustReload} onClick={() => setRemoval({ ids: [member.video_id], collection: page.collection })}>Remove from collection</button>
          </article>;
        }} />
      {page.next_cursor && <button type="button" disabled={busy || mustReload} onClick={() => void load(page.next_cursor!)}>{busy ? 'Loading…' : 'Load more members'}</button>}
    </>}
    {inspected && <VideoInspector video={inspected} onClose={closeInspector} onRestoreFocus={restoreInspector} />}
    {removal && <CollectionTransferDialog ids={removal.ids} collection={removal.collection} mode="remove" onClose={(result) => {
      setRemoval(undefined);
      if (!result) return;
      setMustReload(true);
      if (result.complete) {
        setSelected((currentSelection) => new Set([...currentSelection].filter((videoId) => !removal.ids.includes(videoId))));
        void load();
      } else setError('Removal may be partial. Your selected IDs are kept; reload the collection before another action.');
    }} />}
    <footer><YouTubeAttribution label="Open YouTube" /></footer>
  </main>;
}
